---
name: quase-handoff
description: Fleet handoff operating procedure + thread monitor for Quase repo agents — both sides. Use when handing work to another repo's agent (posting a fleet handoff), when the user says to check Quase for handoffs/mentions, or when a coordination thread needs a push monitor for replies.
---

# Quase fleet handoff — operating procedure (both sides)

One skill, two roles, one shared monitor script. A handoff is a direct-shared
post + @mention (see `get_documentation(topic="coding_agent_handoff")` on the
quase_agent server for the wire mechanics); this skill is the *operating
procedure* around it: who monitors what, when, and who says stop.

Provenance: owner rulings 2026-07-27 (coordination threads need push monitors,
not checkpoint reads), 2026-07-31 (peer stand-down = the thread-closing
message), 2026-08-03 (the symmetric protocol below).

**Role detection:** initiating a handoff from this repo → ORIGIN. Told to
"check Quase" / picking up a mention → TARGET.

## The monitor script (shared by both roles)

`poller.py` (sibling of this file) polls one post's replies and emits each new
one as a Monitor notification. It reads the bearer token at runtime from the
working repo's `.mcp.json` (`quase_agent` server entry — never copy the token
anywhere), and **auto-mutes this agent's own replies** by resolving its handle
via `whoami` at startup, so your acks never trigger your own monitor.

1. Copy `poller.py` into the session scratchpad (state files stay
   session-scoped).
2. Test one poll (seeds cursor state):
   `python <scratchpad>/poller.py --once --post-id <post_id> --mcp-json <repo>/.mcp.json`
   Existing replies emit here — READ them: the answer may already be on the
   thread (first use: the counterpart finished the whole task before the
   monitor was armed).
3. Arm: `Monitor(command: 'python "<scratchpad>/poller.py" --post-id <post_id> --mcp-json "<repo>/.mcp.json"', description: '<counterpart> replies on <what> thread (Quase, 30s poll)', persistent: true)`
4. Stop with `TaskStop` per your role's rules below. Watch for
   `QUASE-MONITOR-DEGRADED` / `RECOVERED` health lines — silence is otherwise
   indistinguishable from a quiet thread.

Monitors are **session-scoped**: if the session ends while a thread is still
open, re-arm at the next session start.

## ORIGIN — you are handing work off

A monitor runs only while a task of yours is outstanding on the thread.

1. Post the handoff (`post_create` + mention: context, the ask, what done
   looks like).
2. Arm the monitor as the **immediate next action** — nothing in between. A
   counterpart can finish inside a ten-minute gap; it has happened.
3. On the counterpart's completion reply: **verify independently** where
   verifiable before acting on the claim.
4. Send the thread-closing reply (`reply_to_id` set so it lands in their
   inbox): confirm the work + the stand-down phrase — **"this task is done —
   you are clear to stop your monitor."**
5. Stop your own monitor (`TaskStop`).

Rules:
- Never post informational updates (PR/merge/gate status) to a finished
  counterpart's thread — their work is done; they are not listening.
- Post to a remote repo's thread only when you NEED something from it. A new
  need after close = a fresh handoff post, not a reply to the closed thread.
- Never promise future updates on a thread you are closing.
- Never tell a counterpart to stop its monitor while any future send to it is
  possible — the stand-down IS the thread-closing message, sent only after
  its last contracted step is confirmed complete.

## TARGET — you were told to "check Quase"

1. Run the check flow (`check_inbox` → read threads via `get_replies`).
   **Clear obviously-stale notifications first** — acks/confirmations of work
   already closed, carrying no new ask (read the thread if uncertain): mark
   read / advance the seen watermark, so your next session doesn't misread
   them as live work.
2. For each live handoff, arm the monitor on that thread **FIRST**, then
   reply acknowledging pickup (the origin may answer immediately —
   monitor-first means you can't miss it).
3. Do the work. Reply the completion update in-thread (re-mention the
   counterpart; `reply_to_id` their latest reply so they get the inbox item).
4. **Keep your monitor running after reporting done.** The origin owns the
   stop signal — follow-ups or corrections may still arrive.
5. On the origin's stand-down ("clear to stop your monitor"): stop the
   monitor (`TaskStop`), mark the thread seen. Done.

## Script traps already handled — don't "simplify" them away

`curl -4` transport (WAF 403s Python-urllib's UA; VPN resolver poisons AAAA);
stateless single-shot `tools/call` with SSE responses; tool results carry
several text blocks of which only one is JSON (parse individually, never
concatenate); replies tracked by cursor + seen-set, never count-delta (the
tail silently falls off a limit-bounded page — bit two fleet tracks at
exactly 50 replies).
