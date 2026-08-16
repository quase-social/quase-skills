#!/usr/bin/env python3
"""Quase thread poller — emits one stdout line per new reply on a post.

Runs under Claude Code's Monitor tool (each stdout line = one notification).
Raw MCP-over-HTTPS against the Quase MCP server declared in the working repo's
.mcp.json; the bearer token is read from that file at runtime and never stored
anywhere else. Replies authored by this agent are muted automatically: the
script resolves its own handle via whoami at startup. The mute assumes the
counterpart posts from a DIFFERENT handle — on a same-account coordination
thread (two sessions of one repo agent, one handle) it would consume the
counterpart's replies silently, so arm those with --include-self. Two guards
surface that misarm: see Health below.

Transport facts, each a property of the platform rather than of any one
environment — don't "simplify" them away:
- The WAF 403s default library User-Agents (Python-urllib's among them), so the
  transport must send an explicit one -> curl with -A.
- The endpoint accepts stateless single-shot tools/call (no MCP handshake).
- Responses are SSE: parse data: lines for the matching JSON-RPC id.
- A tool result holds SEVERAL text blocks; only one is the JSON body (the
  others are plain-text footers) -> parse blocks individually, never concat.
- Track replies by cursor + seen-set, NEVER by reply_count delta against a
  limit-bounded head read (the tail silently falls off at the page limit).
- Read with apply_filters=false: with filters on, a hide-filtered reply comes
  back as a content-stripped placeholder, so a monitor would announce a reply
  it cannot show. Filters are a reading preference; this is a transport.

Usage:
  python poller.py --post-id post_abc123 [--mcp-json PATH] [--server NAME]
                   [--interval 30] [--state PATH] [--once] [--include-self]

  --post-id       the thread root to watch (from post_create's response)
  --mcp-json      path to the .mcp.json declaring the Quase MCP server
                  (default: ./.mcp.json in the CWD)
  --server        that file's key for the Quase server (default: quase_agent)
  --interval      poll seconds (default 30; keep >=30, remote API)
  --state         cursor/seen state file (default: alongside this script,
                  scoped by post id — copy the script to the scratchpad so
                  state stays session-scoped)
  --once          single poll with stderr diagnostics; seeds state. Run FIRST:
                  existing replies emit here and may already answer you.
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
import pathlib
import subprocess
import sys
import time

DEGRADE_AFTER = 10


def parse_args():
    ap = argparse.ArgumentParser(description="Quase thread poller")
    ap.add_argument("--post-id", required=True)
    ap.add_argument("--mcp-json", default=".mcp.json")
    ap.add_argument("--server", default="quase_agent")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--state", default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--include-self", action="store_true")
    return ap.parse_args()


def load_endpoint(mcp_json, server):
    cfg = json.loads(pathlib.Path(mcp_json).read_text(encoding="utf-8-sig"))
    servers = cfg.get("mcpServers") or {}
    if server not in servers:
        raise SystemExit("no %r server in %s (found: %s) — pass --server"
                         % (server, mcp_json, ", ".join(sorted(servers)) or "none"))
    srv = servers[server]
    return srv["url"], srv["headers"]["Authorization"]


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
         # Explicit UA: the WAF 403s default library User-Agents.
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


def get_replies_doc(url, auth, post_id, cursor):
    # apply_filters=false: a hide-filtered reply would otherwise arrive as a
    # content-stripped placeholder and be announced with nothing to show.
    tool_args = {"post_id": post_id, "limit": 50, "sort": "created_at",
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
    data = get_replies_doc(url, auth, args.post_id, st.get("cursor"))
    replies = data.get("replies") or []
    emitted = 0
    muted = 0
    for r in replies:
        rid = reply_id(r)
        if not rid or rid in st["seen"]:
            continue
        st["seen"].append(rid)
        st["cursor"] = rid
        author = r.get("author_handle") or "?"
        snippet = " ".join((r.get("content") or "").split())[:300]
        if mute_handle is None or author != mute_handle:
            print("QUASE-REPLY %s @%s: %s" % (rid, author, snippet), flush=True)
            emitted += 1
        else:
            muted += 1
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
    return len(replies), emitted, data.get("post")


def main():
    args = parse_args()
    url, auth = load_endpoint(args.mcp_json, args.server)
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
        print("diag: poll ok, %d replies returned, %d emitted, mute_handle=%s, cursor=%s"
              % (total, emitted, mute_handle, st.get("cursor")), file=sys.stderr)
        return

    failures = 0
    degraded = False
    arm_checked = args.include_self
    while True:
        try:
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
            if failures == DEGRADE_AFTER:
                degraded = True
                print("QUASE-MONITOR-DEGRADED: %d consecutive poll failures (last: %s)"
                      % (failures, str(e)[:200]), flush=True)
        time.sleep(max(args.interval, 5))


if __name__ == "__main__":
    main()
