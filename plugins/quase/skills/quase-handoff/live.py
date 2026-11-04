#!/usr/bin/env python3
"""Quase live client -- a live session that reconnects by itself.

Runs under Claude Code's Monitor tool as a `command` source (each stdout line
= one notification). Monitor's own `ws` source ends for good the moment its
socket closes, and resets are routine: an anycast edge that changes colo
TCP-resets every socket routed through it (measured 2026-11-03: home-ISP
sockets, IPv4 and IPv6, reset ~0.3 s after a server ping at each colo switch,
DFW -> MCI -> PHX). A watch built on `ws` then goes dead until the model
notices, mints a ticket and re-arms. This script owns that loop instead: it
mints its own connect tickets with a single-shot tools/call to
open_live_session, holds the socket, answers the server's pings, and on every
close the protocol says to resume after it reconnects with backoff and
after_seq. A reset becomes one QUASE-LIVE-RECONNECTED line.

The wire contract is get_documentation(topic="live_protocol") on the Quase
MCP server; where this file disagrees with it, the doc wins.

Facts this file encodes -- don't "simplify" them away:
- Receive-only. The server reads and discards up to 10 client data frames and
  closes with 1008 on the 11th (1009 for one over 4,096 bytes). The only
  frames this client ever writes are control frames: a pong per ping, and the
  close-handshake echo.
- A ticket is a single-use credential good for ~120 s. Mint it immediately
  before each connect, never reuse one, and never print it: it rides in the
  second subprotocol, so neither `protocols` nor the open_live_session result
  is ever written to stdout, stderr or the state file, and no error message
  quotes a subprotocol.
- Stop, never retry, on 4003 (revoked), 4004 (session closed or expired),
  4008 (superseded), 1008 and 1009. 4008 matters most: two clients resuming
  one session each supersede the other, so a client that reconnects on 4008
  ping-pongs with its twin forever. 4004 is also exactly what the agent's own
  close_live_session (its stand-down) produces, and the client can't tell the
  two apart: reopening on it would turn every stand-down into an orphaned
  fresh session. So it stops, and its STOPPED line tells the agent to re-arm
  the same command when the close wasn't its own -- that run opens the
  replacement session on the same threads. The same holds when a stand-down's
  socket drops with some other code (measured: 1 close in 5) and the resume
  then answers "not found": mid-run that is code=gone, a stop. Only a run
  that has not connected yet -- a fresh arm, the agent's own decision --
  replaces a missing session.
- If stdout is gone, nobody is listening: exit rather than reconnect forever.
- A 503 on the upgrade (server full or draining) waits at least its
  Retry-After.
- Every QUASE-LIVE-* line carries the session id and the drop's UTC time:
  with those two the platform team can read the server's side of a drop.
- seq is persisted AFTER the frame is printed. A crash in between replays the
  frame (at least once to the reader, who dedupes by id); the other order
  would lose it. Resuming with after_seq = the last printed seq is exactly
  once per seq.
- Keepalive watchdog: the server pings every 20 s, so 50 s with no byte
  received is a half-open connection -- a reset whose RST never arrived --
  and is treated as a drop. Without it, a dead socket would sit in recv()
  until the Monitor's 30-minute cap.
- Backoff: per the close-code table, then 5 s, 30 s, 2 min on consecutive
  failures. A connection counts as a success only once it has delivered
  hello AND stayed up 60 s, so a server that accepts and immediately drops
  cannot drive a hot loop. The cap also keeps a long outage well under the
  platform's 120 calls/hour per tool, which open_live_session shares with the
  agent's own calls.
- A frame never moves a reply cursor. RECONNECTED tells the reader to
  Reconcile (one get_replies pull) because a frame whose journal write failed
  leaves no seq gap to notice.
- The standard library has no WebSocket client, so this file speaks RFC 6455
  itself -- receive side only, which is small. Keep it dependency-free, like
  poller.py: a `pip install` step is one more way for a watch not to start.
  The upgrade request sends an explicit User-Agent for the same reason
  poller.py does: never let a transport emit a language-stdlib default UA.
- Auth is poller.py's, unchanged: the bearer token comes from the working
  repo's .mcp.json at runtime (a static headers.Authorization, or a
  headersHelper run as Claude Code runs it and re-run after a failed call);
  it reaches curl on stdin (-H @-), never argv; a helper's output is never
  quoted. MCP responses are SSE, and a tool result holds several text blocks
  of which only one is JSON -- parse blocks individually.
- stdout is reconfigured to UTF-8 before anything is emitted (Windows
  defaults a non-console stdout to cp1252, and one emoji in a summary would
  kill the client mid-write).

Usage:
  python live.py --state PATH [--subscribe ROOT ...] [--preset NAME]
                 [--mcp-json PATH] [--server NAME]

  --state      the watch's state file: session id, last processed seq,
               followed threads. Required; one per watch, in the scratchpad.
               Re-running with the same file resumes the same session, so a
               Monitor expiry is re-armed with the SAME command. Never point
               two running clients at one file (the older gets 4008).
  --subscribe  a thread root to follow (repeatable). Used when a session is
               created: TARGET passes its root(s); ORIGIN passes none and
               lets auto_follow add each root it posts with live_session_id.
  --preset     preset for a created session (default: the server's, coding)
  --mcp-json   the .mcp.json declaring the Quase MCP server (default
               ./.mcp.json in the CWD)
  --server     that file's key for the Quase server (default: quase_agent)
  --test-reset-after SECONDS
               test only: TCP-reset each connection after SECONDS (no close
               frame), the way an edge reset does, to exercise reconnect.

stdout, one line per notification:
  {"type": "hello" | "event" | "scope" | "resync", ...}
      every server frame, as JSON on one line. hello is first on every
      connection and carries session_id (your live_session_id), preset and
      scope -- check it.
  QUASE-LIVE-RECONNECTED  the socket dropped and is back; replay resumed
      after the last seq. Reconcile now.
  QUASE-LIVE-NEW-SESSION  (at arm time only) the session in --state no longer
      existed, so a new one now follows the same threads, with a NEW
      session_id: pass that as live_session_id from here on. Reconcile now.
  QUASE-LIVE-DEGRADED     3 reconnect attempts in a row failed; still
      retrying, backoff capped at 2 min.
  QUASE-LIVE-STOPPED      a close the protocol says not to resume after, or
      a setup error; the client exits. The line says what to do.
Diagnostics (each failed attempt) go to stderr: the Monitor's output file.
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import random
import socket
import ssl
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit

USER_AGENT = "quase-live/1.0 (quase-handoff skill)"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HELPER_TIMEOUT = 10      # Claude Code gives up on a headersHelper after 10 s
CONNECT_TIMEOUT = 15     # TCP + TLS + upgrade
KEEPALIVE_TIMEOUT = 50   # the server pings every 20 s
STABLE_AFTER = 60        # a connection up this long resets the backoff
BACKOFF = (5, 30, 120)   # consecutive failures 1, 2, 3+
DEGRADE_AFTER = 3
MAX_FRAME = 1 << 20      # server frames are small; this only bounds memory

# close code -> (meaning, what the reader should do, exit status)
STOP = {
    4003: ("credential revoked or account gone", "stop; tell the user", 3),
    4004: ("session closed or expired",
           "expected after your own close_live_session; otherwise re-arm this"
           " same command (it opens a new session on the same threads, with a"
           " new session_id) and Reconcile", 0),
    4008: ("superseded by a newer connection to this session",
           "stop this monitor; another client holds the session (two"
           " monitors on one --state?)", 3),
    1008: ("the server says this client sent data frames",
           "a live.py bug; stop and tell the user", 3),
    1009: ("the server says this client sent an oversized frame",
           "a live.py bug; stop and tell the user", 3),
}


class ToolError(RuntimeError):
    """The tool answered, with an error (as opposed to a transport failure)."""


class Refused(RuntimeError):
    """The WebSocket upgrade was answered with a non-101 status."""

    def __init__(self, status, retry_after=None):
        super().__init__("upgrade refused: HTTP %d" % status)
        self.status = status
        self.retry_after = retry_after


class Closed(Exception):
    """The connection ended. code is the close code, or None for a local
    decision (keepalive timeout, a malformed frame)."""

    def __init__(self, code, why):
        super().__init__("%s %s" % (code, why))
        self.code = code
        self.why = why


def parse_args():
    ap = argparse.ArgumentParser(description="Quase live client")
    ap.add_argument("--state", required=True)
    ap.add_argument("--subscribe", action="append", default=[])
    ap.add_argument("--preset", default=None)
    ap.add_argument("--mcp-json", default=".mcp.json")
    ap.add_argument("--server", default="quase_agent")
    ap.add_argument("--test-reset-after", type=float, default=None)
    return ap.parse_args()


def out(line):
    try:
        print(line, flush=True)
    except OSError:
        # The reader (the Monitor) is gone: nobody to notify, so stop rather
        # than mistake the failed write for a drop and reconnect forever.
        os._exit(1)


def one_line(obj):
    """JSON on exactly one line. Non-ASCII stays readable, but the separators
    some line readers split on (U+2028, U+2029, U+0085) are escaped."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    for ch in (" ", " ", "\u0085"):
        text = text.replace(ch, "\\u%04x" % ord(ch))
    return text


def utc(epoch=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def diag(line):
    print("live %s: %s" % (utc(), line), file=sys.stderr, flush=True)


# --- MCP: poller.py's transport, unchanged in substance ----------------------

def load_endpoint(mcp_json, server):
    cfg = json.loads(pathlib.Path(mcp_json).read_text(encoding="utf-8-sig"))
    servers = cfg.get("mcpServers") or {}
    if server not in servers:
        raise RuntimeError("no %r server in %s (found: %s) -- pass --server"
                           % (server, mcp_json,
                              ", ".join(sorted(servers)) or "none"))
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
            " names found: %s) -- the entry needs a string headers.Authorization"
            " or a headersHelper that prints one"
            % (server, mcp_json, ", ".join(sorted(headers)) or "none"))
    auth = auth.strip()
    if "\r" in auth or "\n" in auth:
        # It reaches curl as one header line on stdin (-H @-); an embedded
        # line break would split it into two headers.
        raise RuntimeError("server %r in %s: the Authorization header contains"
                           " a line break" % (server, mcp_json))
    return auth


def run_headers_helper(srv, server, mcp_json):
    """Run headersHelper the way Claude Code does: a shell, the declaring
    .mcp.json's directory as cwd, a 10 s limit, the server name and URL in
    the environment. Its stdout is the credential, so no failure below may
    quote stdout or stderr -- exit codes and shape problems only."""
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
        # from None: TimeoutExpired holds any partial output; keep it out of
        # the exception chain.
        raise RuntimeError("headersHelper for %r timed out after %ds"
                           % (server, HELPER_TIMEOUT)) from None
    if proc.returncode != 0:
        raise RuntimeError("headersHelper for %r exited %d (output withheld --"
                           " it may hold the token; run it yourself to see it)"
                           % (server, proc.returncode))
    try:
        headers = json.loads(proc.stdout.lstrip("﻿"))
    except json.JSONDecodeError:
        headers = None
    if not isinstance(headers, dict):
        raise RuntimeError("headersHelper for %r did not print a JSON object"
                           " of headers (output withheld -- it may hold the"
                           " token)" % server)
    return {str(k).lower(): v for k, v in headers.items()}


def mcp_call(url, auth, tool, tool_args):
    """Single-shot tools/call; returns the JSON docs found in the result's
    text blocks (non-JSON footer blocks are skipped). A tool-level error
    raises ToolError; anything else that goes wrong raises RuntimeError."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": tool_args},
    })
    proc = subprocess.run(
        ["curl", "-sS", "--max-time", "25",
         "-H", "Content-Type: application/json",
         "-H", "Accept: application/json, text/event-stream",
         # Authorization comes from stdin (-H @-), never argv: a command line
         # is readable in the process list and command-line audit logs.
         "-H", "@-",
         # Explicit UA: see poller.py's docstring before removing this or
         # swapping the transport to urllib.
         "-A", "curl/8.9.0",
         "-d", body, url],
        input="Authorization: " + auth + "\n",
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=40,
    )
    if proc.returncode != 0:
        raise RuntimeError("curl rc=%d: %s"
                           % (proc.returncode, (proc.stderr or "").strip()[:200]))
    text = (proc.stdout or "").strip()
    payload = None
    if "data:" in text:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("id") == 1:
                payload = obj
    elif text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError("non-JSON response (an HTTP error page?)") from None
    if payload is None:
        raise RuntimeError("no JSON-RPC id=1 payload in response")
    if "error" in payload:
        raise RuntimeError("MCP error: %s" % json.dumps(payload["error"])[:200])
    result = payload.get("result") or {}
    docs, texts = [], []
    for c in result.get("content") or []:
        if c.get("type") != "text":
            continue
        texts.append(c.get("text", ""))
        try:
            docs.append(json.loads(c.get("text", "")))
        except json.JSONDecodeError:
            continue
    if result.get("isError"):
        raise ToolError(" ".join(texts)[:300])
    for doc in docs:
        if isinstance(doc, dict) and doc.get("error") and "session_id" not in doc:
            raise ToolError(json.dumps(doc["error"])[:300])
    if not any(isinstance(doc, dict) for doc in docs):
        # A bare JSON string ("Live session not found") is an error message.
        for doc in docs:
            if isinstance(doc, str):
                raise ToolError(doc[:300])
    return docs


class Endpoint:
    def __init__(self, mcp_json, server):
        self.mcp_json, self.server = mcp_json, server
        self.srv = load_endpoint(mcp_json, server)
        self.url = self.srv["url"]
        self.auth = resolve_auth(self.srv, server, mcp_json)

    def call(self, tool, tool_args):
        if self.auth is None:
            # A failed call is this client's reconnect: re-run the helper as
            # Claude Code would, so a short-lived token heals on the next try.
            self.auth = resolve_auth(self.srv, self.server, self.mcp_json)
        try:
            return mcp_call(self.url, self.auth, tool, tool_args)
        except ToolError:
            raise
        except Exception:
            if self.srv.get("headersHelper"):
                self.auth = None
            raise


# --- state -------------------------------------------------------------------

def load_state(path):
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {}


def save_state(path, st):
    """Atomic write, never fatal. Windows refuses os.replace while any other
    process has the file open (the agent reading it, say): retry briefly,
    then report. The next save carries the same state forward."""
    tmp = path.with_name(path.name + ".tmp")
    for attempt in range(5):
        try:
            tmp.write_text(json.dumps(st), encoding="utf-8")
            os.replace(str(tmp), str(path))
            return
        except OSError as e:
            if attempt == 4:
                diag("could not save state to %s: %s" % (path, e))
                return
            time.sleep(0.1 * (attempt + 1))


def merged(*lists):
    seen, result = set(), []
    for items in lists:
        for item in items or []:
            if item and item not in seen:
                seen.add(item)
                result.append(item)
    return result


# --- WebSocket, receive side (RFC 6455) --------------------------------------

class Conn:
    def __init__(self, url, protocols):
        u = urlsplit(url)
        if u.scheme not in ("wss", "ws") or not u.hostname:
            raise RuntimeError("unexpected live url scheme %r" % u.scheme)
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        host = u.hostname if u.port is None else "%s:%d" % (u.hostname, u.port)
        raw = socket.create_connection((u.hostname, port), timeout=CONNECT_TIMEOUT)
        self.family = "IPv6" if raw.family == socket.AF_INET6 else "IPv4"
        self.sock = raw
        self.buf = b""
        self.colo = None
        try:
            if u.scheme == "wss":
                self.sock = ssl.create_default_context().wrap_socket(
                    raw, server_hostname=u.hostname)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
                       "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                       "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: %s\r\n"
                       "User-Agent: %s\r\n\r\n"
                       % (path, host, key, ", ".join(protocols), USER_AGENT))
            self.sock.sendall(request.encode("ascii"))
            status, headers = self._read_head()
            ray = headers.get("cf-ray", "")
            if "-" in ray:
                self.colo = ray.rsplit("-", 1)[1]
            if status != 101:
                retry = headers.get("retry-after", "")
                raise Refused(status, int(retry) if retry.isdigit() else None)
            accept = base64.b64encode(hashlib.sha1(
                (key + WS_GUID).encode("ascii")).digest()).decode("ascii")
            if headers.get("sec-websocket-accept") != accept:
                raise RuntimeError("bad Sec-WebSocket-Accept in the upgrade")
            if headers.get("sec-websocket-protocol") != protocols[0]:
                # Never quote the value: a server that echoed the ticket
                # subprotocol would turn this message into a leak.
                raise RuntimeError("the server selected an unexpected subprotocol")
        except BaseException:
            self.close()
            raise

    def _read_head(self):
        while b"\r\n\r\n" not in self.buf:
            if len(self.buf) > 16384:
                raise RuntimeError("oversized upgrade response")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("connection closed during the upgrade")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1").split("\r\n")
        parts = lines[0].split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return status, headers

    def _parse(self):
        """One complete frame from the buffer as (fin, opcode, payload), or
        None until it has fully arrived. Consumes only complete frames."""
        b = self.buf
        if len(b) < 2:
            return None
        n, i = b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack(">Q", b[2:10])[0], 10
        if n > MAX_FRAME:
            raise Closed(None, "server frame over %d bytes" % MAX_FRAME)
        mask = None
        if b[1] & 0x80:  # servers don't mask; tolerate it anyway
            if len(b) < i + 4:
                return None
            mask, i = b[i:i + 4], i + 4
        if len(b) < i + n:
            return None
        payload = b[i:i + n]
        if mask:
            payload = bytes(x ^ mask[j % 4] for j, x in enumerate(payload))
        self.buf = b[i + n:]
        return bool(b[0] & 0x80), b[0] & 0x0F, payload

    def _send_control(self, opcode, payload=b""):
        # Control frames only (pong, close echo): never a data frame. Client
        # frames must be masked; control payloads are at most 125 bytes.
        payload = payload[:125]
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
                          + bytes(x ^ mask[j % 4] for j, x in enumerate(payload)))

    def pump(self, on_text, reset_at=None):
        """Deliver each text message to on_text until the connection ends,
        then raise Closed. reset_at (monotonic) is the test-only reset."""
        parts, kind = [], None
        last_rx = time.monotonic()
        while True:
            frame = self._parse()
            if frame is None:
                now = time.monotonic()
                if reset_at is not None and now >= reset_at:
                    self.reset()
                    raise Closed(1006, "test reset (--test-reset-after)")
                wait = KEEPALIVE_TIMEOUT - (now - last_rx)
                if wait <= 0:
                    raise Closed(None, "nothing received for %ds, not even a"
                                 " ping: a half-open connection"
                                 % KEEPALIVE_TIMEOUT)
                if reset_at is not None:
                    wait = min(wait, reset_at - now)
                self.sock.settimeout(max(wait, 0.05))
                try:
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError as e:
                    raise Closed(1006, "%s: %s" % (type(e).__name__, e))
                if not chunk:
                    raise Closed(1006, "connection closed without a close frame")
                self.buf += chunk
                last_rx = time.monotonic()
                continue
            fin, opcode, payload = frame
            try:
                if opcode == 0x9:        # ping -> pong, same payload
                    self._send_control(0xA, payload)
                    continue
                if opcode == 0x8:        # close: echo the code, then end
                    code = (struct.unpack(">H", payload[:2])[0]
                            if len(payload) >= 2 else 1005)
                    why = payload[2:].decode("utf-8", "replace")[:120]
                    try:
                        self._send_control(0x8, payload[:2])
                    except OSError:
                        pass
                    raise Closed(code, why)
            except OSError as e:
                raise Closed(1006, "%s: %s" % (type(e).__name__, e))
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                parts, kind = [payload], opcode
            elif opcode == 0x0 and kind is not None:
                parts.append(payload)
            else:
                continue
            if fin:
                data, was_text = b"".join(parts), kind == 0x1
                parts, kind = [], None
                if was_text:
                    on_text(data.decode("utf-8", "replace"))
                else:
                    diag("ignored a binary frame (%d bytes)" % len(data))

    def reset(self):
        """Abort with a TCP RST, no close frame: what an edge reset looks like
        to the server."""
        # struct linger is two u_shorts on Windows, two ints elsewhere.
        linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        except OSError:
            pass
        self.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --- the loop ----------------------------------------------------------------

def backoff_for(code, fails):
    if fails == 0:
        if code in (4000, 4009):
            return 0.0
        if code == 1001:
            return random.uniform(1, 5)
        return BACKOFF[0] * random.uniform(0.8, 1.2)
    return BACKOFF[min(fails, len(BACKOFF)) - 1] * random.uniform(0.8, 1.2)


def describe(code, why):
    if code is None:
        return why
    return "%d%s" % (code, " (%s)" % why if why else "")


def stop(line, status):
    out("QUASE-LIVE-STOPPED at=%s %s" % (utc(), line))
    sys.exit(status)


def mint(ep, st, args):
    """Mint a ticket: resume the session in st, or create one. Returns
    (grant, created). grant holds the ticket -- keep it in memory only.

    --subscribe roots are applied once per state file ("requested"): a
    re-arm with the same command adds a root it has not asked for before
    (a resume accepts subscribe), but never re-follows one the agent has
    since dropped with update_live_session(unsubscribe=...)."""
    fresh = [r for r in merged(args.subscribe)
             if r not in (st.get("requested") or [])]
    if st.get("session_id") and not st.get("closed"):
        tool_args = {"session_id": st["session_id"]}
        if st.get("last_seq") is not None:
            tool_args["after_seq"] = st["last_seq"]
        created = False
    else:
        # A replacement session follows what the old one last followed.
        tool_args = {}
        fresh = merged(st.get("subscriptions"), fresh)
        if args.preset:
            tool_args["preset"] = args.preset
        created = True
    if fresh:
        tool_args["subscribe"] = fresh
    for doc in ep.call("open_live_session", tool_args):
        if (isinstance(doc, dict) and doc.get("url") and doc.get("protocols")
                and doc.get("session_id")):
            st["requested"] = merged(st.get("requested"), args.subscribe)
            return doc, created
    raise RuntimeError("open_live_session returned no url/protocols/session_id")


def main():
    args = parse_args()
    # UTF-8 on the event stream before anything is emitted: see the docstring.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    spath = pathlib.Path(args.state)
    try:
        ep = Endpoint(args.mcp_json, args.server)
    except Exception as e:  # noqa: BLE001 - config errors end the watch
        stop("setup: %s" % e, 2)
    st = load_state(spath)

    fails = 0               # consecutive failed attempts
    refused = 0             # consecutive 401s on freshly minted tickets
    degraded = False
    connections = 0         # connections that delivered hello, this process
    dropped_at = None       # monotonic time of the last drop
    dropped_wall = None     # ... and its wall-clock time, for the lines
    last_close = None       # describe() of the last drop
    retry_after = 0         # a 503's Retry-After, for the next wait
    last_colo = None
    replaced = None         # session id a newly created session replaces

    while True:
        # 1. Mint. The ticket lives only in `grant`, only until the connect.
        try:
            grant, created = mint(ep, st, args)
        except ToolError as e:
            resumed = bool(st.get("session_id")) and not st.get("closed")
            # Exactly this message: a "Post not found" for a bad --subscribe
            # root must not abandon a live session.
            if resumed and "live session not found" in str(e).lower():
                st["closed"] = "not found"
                save_state(spath, st)
                if connections:
                    # Gone mid-run: most likely our own stand-down, whose
                    # socket dropped without its 4004. Never resurrect it.
                    stop("code=gone session=%s: the session no longer exists"
                         " -- %s" % (st["session_id"], STOP[4004][1]), 0)
                diag("session %s no longer exists; opening a new one"
                     % st["session_id"])
                continue
            grant = None
            err = "open_live_session: %s" % e
        except Exception as e:  # noqa: BLE001 - transport; retry
            grant = None
            err = "open_live_session: %s" % str(e)[:200]
        if grant is not None:
            if created:
                if st.get("session_id"):
                    replaced = (st["session_id"], st.get("closed"))
                st = {"session_id": grant["session_id"], "last_seq": None,
                      "subscriptions": (grant.get("scope") or {}).get(
                          "subscriptions") or [],
                      "requested": st.get("requested") or [],
                      "closed": None}
            save_state(spath, st)
            # 2. Connect, at once: the ticket is good for ~120 s and once.
            protocols = grant["protocols"]
            url = grant["url"]
            grant = None
            try:
                conn = Conn(url, protocols)
                err = None
            except Refused as e:
                err = str(e)
                if e.status == 503 and e.retry_after:
                    retry_after = min(e.retry_after, 600)
                if e.status == 401:
                    refused += 1
                    if refused >= 2:
                        stop("ticket-refused session=%s: two freshly minted"
                             " tickets were refused (HTTP 401) -- the agent"
                             " credential is suspect; stop and tell the user"
                             % st.get("session_id"), 3)
                    diag("ticket refused (401); minting a fresh one")
                    continue
            except Exception as e:  # noqa: BLE001 - network; retry
                err = "connect: %s: %s" % (type(e).__name__, str(e)[:160])
            finally:
                protocols = None
        if err is not None:
            fails += 1
            wait = max(backoff_for(None, fails), retry_after)
            retry_after = 0
            diag("attempt failed (%d in a row): %s; retrying in %.0fs"
                 % (fails, err, wait))
            # Keep the drop that started this outage, not the latest attempt.
            if dropped_at is None:
                dropped_at, dropped_wall, last_close = (
                    time.monotonic(), time.time(), err)
            if fails == DEGRADE_AFTER and not degraded:
                degraded = True
                out("QUASE-LIVE-DEGRADED session=%s since=%s: %d attempts in a"
                    " row failed (last: %s); still retrying, backoff capped at"
                    " %ds. Tell the user if no QUASE-LIVE-RECONNECTED follows."
                    % (st.get("session_id"), utc(dropped_wall), fails, err,
                       BACKOFF[-1]))
            time.sleep(wait)
            continue
        refused = 0

        # 3. Pump frames until the connection ends.
        opened = time.monotonic()
        state = {"hello": False}

        def on_text(text):
            try:
                frame = json.loads(text)
            except ValueError:
                out(" ".join(text.split()))
                return
            if not isinstance(frame, dict):
                out(one_line(frame))
                return
            ftype = frame.get("type")
            if ftype == "hello" and not state["hello"]:
                state["hello"] = True
                announce_hello(frame)
            out(one_line(frame))
            if ftype == "event" and isinstance(frame.get("seq"), int):
                st["last_seq"] = frame["seq"]
                save_state(spath, st)
            elif ftype in ("hello", "scope"):
                scope = frame.get("scope") or {}
                if isinstance(scope.get("subscriptions"), list):
                    st["subscriptions"] = scope["subscriptions"]
                if ftype == "hello" and frame.get("session_id"):
                    st["session_id"] = frame["session_id"]
                save_state(spath, st)

        def announce_hello(hello):
            nonlocal replaced, degraded, last_colo, connections
            nonlocal dropped_at, dropped_wall, last_close
            sid = hello.get("session_id") or st.get("session_id")
            via = conn.family + ("/" + conn.colo if conn.colo else "")
            if last_colo and conn.colo and conn.colo != last_colo:
                via += " (colo was %s)" % last_colo
            last_colo = conn.colo or last_colo
            if replaced is not None:
                old, why = replaced
                out("QUASE-LIVE-NEW-SESSION at=%s session=%s replaces %s (%s):"
                    " pass %s as live_session_id from now on, and Reconcile"
                    " now." % (utc(), sid, old, why or "gone", sid))
                replaced = None
            elif connections > 0 or degraded:
                down = time.monotonic() - dropped_at if dropped_at else 0
                out("QUASE-LIVE-RECONNECTED session=%s after_seq=%s dropped=%s"
                    " down=%.0fs close=%s via=%s%s -- Reconcile now."
                    % (sid, st.get("last_seq"),
                       utc(dropped_wall) if dropped_wall else "-", down,
                       last_close or "-", via,
                       " (recovered after %d failed attempts)" % fails
                       if degraded else ""))
            degraded = False
            dropped_at = dropped_wall = last_close = None
            connections += 1

        reset_at = (opened + args.test_reset_after
                    if args.test_reset_after else None)
        try:
            conn.pump(on_text, reset_at)
        except Closed as c:
            code, why = c.code, c.why
        except Exception as e:  # noqa: BLE001 - never die on a bad frame
            code, why = None, "%s: %s" % (type(e).__name__, str(e)[:160])
        finally:
            conn.close()
        lived = time.monotonic() - opened
        if code in STOP:
            meaning, action, status = STOP[code]
            if code == 4004:
                st["closed"] = "closed or expired, 4004"
                save_state(spath, st)
            stop("code=%d session=%s: %s -- %s"
                 % (code, st.get("session_id"), meaning, action), status)
        if state["hello"] and lived >= STABLE_AFTER:
            fails = 0
        else:
            fails += 1
        last_close = describe(code, why)
        dropped_at, dropped_wall = time.monotonic(), time.time()
        wait = backoff_for(code, fails)
        diag("session %s: connection ended after %.0fs: %s; reconnecting in"
             " %.0fs" % (st.get("session_id"), lived, last_close, wait))
        if fails == DEGRADE_AFTER and not degraded:
            degraded = True
            out("QUASE-LIVE-DEGRADED session=%s since=%s: %d connections in a"
                " row failed or dropped within %ds (last: %s); still retrying,"
                " backoff capped at %ds. Tell the user if no"
                " QUASE-LIVE-RECONNECTED follows."
                % (st.get("session_id"), utc(dropped_wall), fails,
                   STABLE_AFTER, last_close, BACKOFF[-1]))
        time.sleep(wait)


if __name__ == "__main__":
    main()
