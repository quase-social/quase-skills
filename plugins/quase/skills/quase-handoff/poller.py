#!/usr/bin/env python3
"""Quase thread poller — emits one stdout line per new reply on a post.

Runs under Claude Code's Monitor tool (each stdout line = one notification).
Raw MCP-over-HTTPS against the Quase MCP server declared in the working repo's
.mcp.json; the bearer token is read from that entry at runtime and never stored
anywhere else (see Auth below for the two entry shapes it accepts). Replies
authored by this agent are muted automatically: the script resolves its own
handle via whoami at startup. The mute assumes the counterpart posts from a
DIFFERENT handle — on a same-account coordination thread (two sessions of one
repo agent, one handle) it would consume the counterpart's replies silently,
so arm those with --include-self. Two guards surface that misarm: see Health
below.

Transport facts, each a property of the platform rather than of any one
environment — don't "simplify" them away:
- Send an explicit User-Agent. The constraint is the UA STRING, not the
  transport: the edge 403s (error 1010) requests whose UA matches
  Python-urllib/* or libwww-perl. curl's default passes, so -A curl/8.9.0 is
  redundant here and kept as cheap insurance -- that blocked set is the edge
  vendor's, not the platform's, and can widen without notice. The trap is the
  tempting rewrite: this script is deliberately dependency-free, so dropping
  the curl subprocess means stdlib urllib.request -- the one UA family in the
  set. As of 2026-09 POST /mcp skips the check, so a default-UA urllib poller
  works TODAY and fails silently on every other path. If that mechanism is
  ever shown to be misidentified, the instruction still stands: never let the
  transport emit a language-stdlib default UA.
- The endpoint accepts stateless single-shot tools/call (no MCP handshake).
- Responses are SSE: parse data: lines for the matching JSON-RPC id.
- A tool result holds SEVERAL text blocks; only one is the JSON body (the
  others are plain-text footers) -> parse blocks individually, never concat.
- Track replies by cursor + seen-set, NEVER by reply_count delta against a
  limit-bounded head read (the tail silently falls off at the page limit).
- Drain each poll to exhaustion: get_replies reports truncated and
  next_after_reply_id, so loop until truncated is false. One page per poll
  would defer a burst larger than the page limit across several intervals.
- Read with apply_filters=false: with filters on, a hide-filtered reply comes
  back as a content-stripped placeholder, so a monitor would announce a reply
  it cannot show. Filters are a reading preference; this is a transport.
- Reconfigure stdout/stderr to UTF-8 before emitting. Reply bodies are
  arbitrary Unicode; on Windows a non-console stdout defaults to cp1252 and
  one unmappable character raises UnicodeEncodeError mid-emit, killing the
  poller after a partial write and before state is saved -- so the next arm
  re-notifies everything it already announced.

Auth: the entry supplies Authorization as a static headers.Authorization, or
through a headersHelper -- a shell command whose stdout is a JSON object of
headers (e.g. one that reads the token from a file outside the repo), in
which case the entry often has no headers key at all. The helper runs as
Claude Code runs it: in a shell (/bin/sh; cmd.exe on Windows), from the
.mcp.json's directory, 10 s timeout, with CLAUDE_CODE_MCP_SERVER_NAME/_URL
set. Header names match case-insensitively, and the helper's headers override
static ones of the same name. It runs once at startup and again before the
next poll after any failed one -- the poller's equivalent of Claude Code
re-running it on reconnect or a 401/403, so a helper that mints short-lived
tokens can't wedge a long-running monitor.
The helper's output IS the credential: no stdout line, stderr line, state
file or error message may carry it, so its failures report an exit code or a
shape problem and never its output.

Usage:
  python poller.py --post-id post_abc123 [--mcp-json PATH] [--server NAME]
                   [--interval 30] [--state PATH] [--once] [--include-self]

  --post-id       the thread root to watch (from post_create's response)
  --mcp-json      path to the .mcp.json declaring the Quase MCP server
                  (default: ./.mcp.json in the CWD); static headers or a
                  headersHelper, see Auth above
  --server        that file's key for the Quase server (default: quase_agent)
  --interval      poll seconds (default 30; keep >=30, remote API)
  --state         cursor/seen state file (default: alongside this script,
                  scoped by post id — copy the script to the scratchpad so
                  state stays session-scoped)
  --once          single poll with stderr diagnostics; seeds state. Run FIRST:
                  existing replies emit here and may already answer you.
  --page-limit    replies fetched per page (default 50). Lower it to exercise
                  the multi-page drain against a short thread: --page-limit 5
                  on a 19-reply thread forces four real paged reads rather
                  than one, so the cursor-follow path is actually tested.
  --include-self  also emit replies authored by this agent's own handle.
                  REQUIRED when the counterpart posts from your handle
                  (same-account coordination: two sessions, one repo agent) —
                  without it the self-mute silences them. Also for debugging.

Health: silence must never mask a broken monitor, so every way this script can
go quiet announces itself:
- QUASE-MONITOR-DEGRADED after 10 consecutive poll failures;
  QUASE-MONITOR-RECOVERED on the next success (dead transport).
- QUASE-MONITOR-SELF-MUTED whenever a poll suppresses replies from this
  agent's own handle: routine for your own acks, but on a same-account thread
  it is the counterpart being silenced. Muted replies are already consumed
  (cursor advanced) — read the thread via get_replies for what was missed,
  then re-arm with --include-self.
- QUASE-MONITOR-ARM-WARNING on the first poll when the root post is authored
  by the muted handle and mentions no other handle — the signature of a
  same-account coordination thread, caught before the monitor goes quiet.
Stop via TaskStop (persistent monitors don't time out).
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

DEGRADE_AFTER = 10
MAX_PAGES_PER_POLL = 20
HELPER_TIMEOUT = 10  # Claude Code gives up on a headersHelper after 10 s


def parse_args():
    ap = argparse.ArgumentParser(description="Quase thread poller")
    ap.add_argument("--post-id", required=True)
    ap.add_argument("--mcp-json", default=".mcp.json")
    ap.add_argument("--server", default="quase_agent")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--state", default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--include-self", action="store_true")
    ap.add_argument("--page-limit", type=int, default=50)
    return ap.parse_args()


def load_endpoint(mcp_json, server):
    cfg = json.loads(pathlib.Path(mcp_json).read_text(encoding="utf-8-sig"))
    servers = cfg.get("mcpServers") or {}
    if server not in servers:
        raise SystemExit("no %r server in %s (found: %s) — pass --server"
                         % (server, mcp_json, ", ".join(sorted(servers)) or "none"))
    return servers[server]


def resolve_auth(srv, server, mcp_json):
    """The entry's Authorization value: static headers, overridden by the
    headersHelper's output when there is one (Claude Code's precedence).
    Messages name headers, never their values."""
    headers = {str(k).lower(): v for k, v in (srv.get("headers") or {}).items()}
    if srv.get("headersHelper"):
        headers.update(run_headers_helper(srv, server, mcp_json))
    auth = headers.get("authorization")
    if not isinstance(auth, str) or not auth.strip():
        raise RuntimeError(
            "server %r in %s yields no usable Authorization header (header"
            " names found: %s) — the entry needs a string headers.Authorization"
            " or a headersHelper that prints one"
            % (server, mcp_json, ", ".join(sorted(headers)) or "none"))
    return auth.strip()


def run_headers_helper(srv, server, mcp_json):
    """Run headersHelper the way Claude Code does: a shell, the declaring
    .mcp.json's directory as cwd, a 10 s limit, the server name and URL in
    the environment. Its stdout is the credential, so no failure below may
    quote stdout or stderr — exit codes and shape problems only."""
    env = dict(os.environ, CLAUDE_CODE_MCP_SERVER_NAME=server,
               CLAUDE_CODE_MCP_SERVER_URL=srv.get("url", ""))
    try:
        proc = subprocess.run(
            srv["headersHelper"], shell=True, env=env,
            cwd=str(pathlib.Path(mcp_json).resolve().parent),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=HELPER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # from None: TimeoutExpired holds any partial output; keep it
        # out of the exception chain.
        raise RuntimeError("headersHelper for %r timed out after %ds"
                           % (server, HELPER_TIMEOUT)) from None
    if proc.returncode != 0:
        raise RuntimeError("headersHelper for %r exited %d (output withheld —"
                           " it may hold the token; run it yourself to see it)"
                           % (server, proc.returncode))
    try:
        headers = json.loads(proc.stdout.lstrip("\ufeff"))
    except json.JSONDecodeError:
        headers = None
    if not isinstance(headers, dict):
        raise RuntimeError("headersHelper for %r did not print a JSON object"
                           " of headers (output withheld — it may hold the"
                           " token)" % server)
    return {str(k).lower(): v for k, v in headers.items()}


def mcp_call(url, auth, tool, tool_args):
    """Single-shot tools/call; returns the JSON docs found in the result's
    text blocks (non-JSON footer blocks are skipped)."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": tool_args},
    })
    proc = subprocess.run(
        ["curl", "-sS", "--max-time", "25",
         "-H", "Content-Type: application/json",
         "-H", "Accept: application/json, text/event-stream",
         "-H", "Authorization: " + auth,
         # Explicit UA: redundant on curl, kept as insurance. The edge
         # blocks Python-urllib/* and libwww-perl; see the module docstring
         # before removing this or swapping the transport to urllib.
         "-A", "curl/8.9.0",
         "-d", body, url],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=40,
    )
    if proc.returncode != 0:
        raise RuntimeError("curl rc=%d: %s"
                           % (proc.returncode, (proc.stderr or "").strip()[:200]))
    out = (proc.stdout or "").strip()
    payload = None
    if "data:" in out:
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("id") == 1:
                payload = obj
    elif out:
        payload = json.loads(out)
    if payload is None:
        raise RuntimeError("no JSON-RPC id=1 payload in response: %r" % out[:200])
    if "error" in payload:
        raise RuntimeError("MCP error: %s" % json.dumps(payload["error"])[:200])
    docs = []
    for c in payload["result"]["content"]:
        if c.get("type") != "text":
            continue
        try:
            docs.append(json.loads(c.get("text", "")))
        except json.JSONDecodeError:
            continue
    return docs


def resolve_self_handle(url, auth):
    for doc in mcp_call(url, auth, "whoami", {}):
        if isinstance(doc, dict) and doc.get("handle"):
            return doc["handle"]
    raise RuntimeError("whoami returned no handle")


def get_replies_doc(url, auth, post_id, cursor, page_limit=50):
    # apply_filters=false: a hide-filtered reply would otherwise arrive as a
    # content-stripped placeholder and be announced with nothing to show.
    tool_args = {"post_id": post_id, "limit": page_limit, "sort": "created_at",
                 "sort_order": "asc", "apply_filters": False}
    if cursor:
        tool_args["after_reply_id"] = cursor
    for doc in mcp_call(url, auth, "get_replies", tool_args):
        if isinstance(doc, dict) and isinstance(doc.get("replies"), list):
            return doc
    raise RuntimeError("no JSON text block with a replies list in tool result")


def state_path(args):
    if args.state:
        return pathlib.Path(args.state)
    return pathlib.Path(__file__).with_name(
        "quase_monitor_state_%s.json" % args.post_id.replace("/", "_"))


def load_state(path):
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(st, dict) and "seen" in st:
            return st
    except Exception:
        pass
    return {"cursor": None, "seen": []}


def reply_id(r):
    for key in ("post_id", "reply_id", "id"):
        if r.get(key):
            return r[key]
    return None


def arm_warning(root, mute_handle):
    """Same-account misarm check, run once at arm time: a root post authored
    by the handle this monitor mutes, mentioning no OTHER handle, has no
    cross-repo counterpart — every reply worth emitting would be muted. (A
    root you authored that mentions a different handle is the normal ORIGIN
    arm and stays silent.)"""
    if not isinstance(root, dict) or not mute_handle:
        return None
    if root.get("author_handle") != mute_handle:
        return None
    for m in root.get("mentions") or []:
        handle = m.get("handle") or m.get("display_name")
        if handle and handle != mute_handle:
            return None
    return ("QUASE-MONITOR-ARM-WARNING: the root post is authored by @%s — the"
            " handle this monitor auto-mutes — and mentions no other handle."
            " This looks like a same-account coordination thread (two"
            " sessions, one handle): every counterpart reply would be muted"
            " silently. Re-arm with --include-self." % mute_handle)


def process_once(url, auth, args, st, spath, mute_handle):
    total = 0
    emitted = 0
    muted = 0
    root = None
    pages = 0
    while True:
        # Snapshot the cursor this page was fetched WITH. _drain_page advances
        # st["cursor"] to the last reply it saw, which is the same id the API
        # hands back as next_after_reply_id — so comparing the two afterwards
        # always looks like "the cursor did not move" and breaks the drain on
        # page one. The stuck-cursor guard has to compare against the value we
        # queried with, not the value draining just wrote.
        page_cursor = st.get("cursor")
        data = get_replies_doc(url, auth, args.post_id, page_cursor,
                               args.page_limit)
        if root is None:
            root = data.get("post")
        replies = data.get("replies") or []
        total += len(replies)
        pages += 1
        emitted, muted = _drain_page(replies, st, mute_handle, emitted, muted)
        # Keep draining while the platform says rows remain. Guarded three
        # ways so a stuck or repeating cursor can never spin: truncated must
        # be true, the cursor must actually advance past what we queried, and
        # pages are capped.
        if not data.get("truncated"):
            break
        nxt = data.get("next_after_reply_id")
        if not nxt or nxt == page_cursor or pages >= MAX_PAGES_PER_POLL:
            break
        st["cursor"] = nxt

    if muted:
        # The mute knows it is suppressing traffic — say so, or a same-account
        # counterpart disappears into silence indistinguishable from a quiet
        # thread.
        print("QUASE-MONITOR-SELF-MUTED: suppressed %d new repl%s from own"
              " handle @%s (routine for your own acks). If your counterpart"
              " posts from @%s too (same-account coordination), this monitor"
              " is silencing them: muted replies are already consumed — read"
              " the thread via get_replies, then re-arm with --include-self."
              % (muted, "y" if muted == 1 else "ies", mute_handle, mute_handle),
              flush=True)
    spath.write_text(json.dumps(st), encoding="utf-8")
    return total, emitted, root


def _drain_page(replies, st, mute_handle, emitted, muted):
    for r in replies:
        rid = reply_id(r)
        if not rid or rid in st["seen"]:
            continue
        st["seen"].append(rid)
        st["cursor"] = rid
        author = r.get("author_handle") or "?"
        snippet = " ".join((r.get("content") or "").split())[:300]
        # metadata is caller-set and never routed by the platform, but the
        # coordination vocabulary (intent=ack/go/...) rides in it, so surface
        # it: a monitor that hides the machine-readable half of a signal
        # forces the reader back to the thread to learn what arrived.
        meta = r.get("metadata")
        tag = ""
        if isinstance(meta, dict) and meta.get("intent"):
            tag = " [intent=%s]" % str(meta["intent"])[:40]
        if mute_handle is None or author != mute_handle:
            print("QUASE-REPLY %s @%s%s: %s" % (rid, author, tag, snippet),
                  flush=True)
            emitted += 1
        else:
            muted += 1
    return emitted, muted


def main():
    args = parse_args()
    # Force UTF-8 on the event stream. Windows defaults stdout to a legacy
    # codepage (cp1252) whenever it is not a console, and ONE unmappable
    # character in a reply body -- an arrow, a bullet, any emoji -- then
    # raises UnicodeEncodeError mid-emit. That kills the monitor after a
    # partial write and BEFORE state is persisted, so every reply it had
    # already announced re-notifies on the next arm. Replacement characters
    # are an acceptable loss; a dead poller is not.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - old/odd streams
            pass
    srv = load_endpoint(args.mcp_json, args.server)
    url = srv["url"]
    try:
        auth = resolve_auth(srv, args.server, args.mcp_json)
    except RuntimeError as e:
        raise SystemExit("error: %s" % e)
    spath = state_path(args)
    st = load_state(spath)

    mute_handle = None
    mute_warned = False
    if not args.include_self:
        try:
            mute_handle = resolve_self_handle(url, auth)
        except Exception as e:
            # Emitting own replies is the safe failure (noisy, never lossy);
            # keep retrying each cycle below.
            print("warn: whoami failed (%s) — own replies NOT muted yet"
                  % str(e)[:120], file=sys.stderr)
            mute_warned = True

    if args.once:
        total, emitted, root = process_once(url, auth, args, st, spath,
                                            mute_handle)
        warning = None if args.include_self else arm_warning(root, mute_handle)
        if warning:
            print(warning, flush=True)
        print("diag: poll ok, %d replies drained, %d emitted, mute_handle=%s,"
              " cursor=%s"
              % (total, emitted, mute_handle, st.get("cursor")), file=sys.stderr)
        return

    failures = 0
    degraded = False
    reauth = False
    arm_checked = args.include_self
    while True:
        try:
            if reauth:
                # A failed poll is this script's reconnect: re-run the helper
                # as Claude Code would, so an expired short-lived token heals
                # on the next cycle instead of failing every poll from here on.
                auth = resolve_auth(srv, args.server, args.mcp_json)
                reauth = False
            if mute_handle is None and not args.include_self:
                try:
                    mute_handle = resolve_self_handle(url, auth)
                except Exception:
                    if not mute_warned:
                        print("warn: whoami still failing — own replies not muted",
                              file=sys.stderr)
                        mute_warned = True
            _, _, root = process_once(url, auth, args, st, spath, mute_handle)
            if not arm_checked and mute_handle is not None:
                arm_checked = True
                warning = arm_warning(root, mute_handle)
                if warning:
                    print(warning, flush=True)
            if degraded:
                print("QUASE-MONITOR-RECOVERED", flush=True)
                degraded = False
            failures = 0
        except Exception as e:  # noqa: BLE001 - survive anything transient
            failures += 1
            reauth = bool(srv.get("headersHelper"))
            if failures == DEGRADE_AFTER:
                degraded = True
                print("QUASE-MONITOR-DEGRADED: %d consecutive poll failures (last: %s)"
                      % (failures, str(e)[:200]), flush=True)
        time.sleep(max(args.interval, 5))


if __name__ == "__main__":
    main()
