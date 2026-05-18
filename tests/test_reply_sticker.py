#!/usr/bin/env python3
"""Test suite for the `reply` and `send-sticker` commands of line-chrome-cli.

Every test sends only into a single test room — set it with the LINE_TEST_ROOM
env var (use a personal/self group so test traffic bothers nobody):

    LINE_TEST_ROOM="나만의 그룹" python3 tests/test_reply_sticker.py

Prerequisites: Chrome running, the LINE extension detached into its own window
and logged in, AppleScript JS execution enabled (`cli.py enable-applescript`).

The cold-path tests reload the LINE tab to reach a "no room open" state — they
never open or send to any other room, so the whole run stays inside the test room.
"""
import json
import os
import subprocess
import sys
import time

CLI = os.path.join(os.path.dirname(__file__), "..", "cli.py")
ROOM = os.environ.get("LINE_TEST_ROOM")
LATENCY_BUDGET_MS = 1000

results = []  # (name, status, detail)


def cli(*args, timeout=40):
    """Run cli.py with args; return (returncode, parsed_json_or_None, raw_stdout)."""
    t0 = time.time()
    p = subprocess.run([sys.executable, CLI, *args],
                       capture_output=True, text=True, timeout=timeout)
    elapsed = int((time.time() - t0) * 1000)
    try:
        data = json.loads(p.stdout)
    except Exception:
        data = None
    return p.returncode, data, p.stdout, elapsed


def reload_tab():
    """Put the LINE tab into a 'no room open' state for cold-path tests by pointing it
    at the chat-list view — without opening or sending to any other room."""
    script = '''
    tell application "Google Chrome"
      repeat with w in windows
        repeat with t in tabs of w
          if URL of t starts with "chrome-extension://ophjlpahpchlmihnnnihgmmeilfjmjjc" and URL of t contains "index.html" then
            set URL of t to "chrome-extension://ophjlpahpchlmihnnnihgmmeilfjmjjc/index.html#/chats"
          end if
        end repeat
      end repeat
    end tell
    '''
    subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    time.sleep(6)  # let the SPA re-init on the chat list


def record(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    results.append((name, status, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def skip(name, detail):
    results.append((name, "SKIP", detail))
    print(f"  [SKIP] {name} — {detail}")


# ─────────────────────────────────────────────────────────────────────────────
# Reply tests
# ─────────────────────────────────────────────────────────────────────────────

def test_reply():
    print("\n# reply")
    stamp = str(int(time.time()))

    # R5: missing required args -> argparse error (exit code 2). No room touched.
    rc, _, _, _ = cli("reply", "--room", ROOM, "--to", "x")  # missing --text
    record("R5 missing --text rejected", rc == 2, f"exit={rc}")

    # Seed a unique target message in the test room.
    target = f"reply-target-{stamp}"
    rc, d, out, _ = cli("send", "--to", ROOM, "--text", target)
    if not (d and d.get("ok")):
        record("R0 seed target message", False, f"send failed: {out[:120]}")
        return
    record("R0 seed target message", True)
    time.sleep(0.5)

    # R1: hot reply (already on the room after the seed send) + latency.
    body = f"reply-hot-{stamp}"
    rc, d, out, elapsed = cli("reply", "--room", ROOM, "--to", target, "--text", body)
    ok = bool(d and d.get("ok") and d.get("verified_by") == "reply_bubble")
    record("R1 hot reply happy path", ok, f"path={d and d.get('path')} {d and d.get('duration_ms')}ms")
    record("R7 hot reply latency < 1s", ok and d.get("duration_ms", 9e9) < LATENCY_BUDGET_MS,
           f"{d and d.get('duration_ms')}ms (budget {LATENCY_BUDGET_MS})")

    # R8: verify via history that the reply quotes the target message.
    rc, d, out, _ = cli("history", "--room", ROOM, "--limit", "6")
    quoted = False
    if d and d.get("messages"):
        for m in d["messages"]:
            txt = m.get("text") or ""
            if body in txt and target in txt:  # reply bubble text = quote + body
                quoted = True
    record("R8 reply bubble quotes target (history)", quoted)

    # R3: reply to a message that does not exist -> clean failure, nothing sent.
    rc, d, out, _ = cli("reply", "--room", ROOM, "--to",
                        f"no-such-message-{stamp}", "--text", "should not send")
    record("R3 non-existent target fails cleanly",
           bool(d and not d.get("ok") and d.get("stage") == "find_message"),
           f"stage={d and d.get('stage')}")

    # R4: with two messages containing the same substring, reply targets the LATEST.
    dup = f"dup-{stamp}"
    cli("send", "--to", ROOM, "--text", f"{dup} first")
    time.sleep(0.4)
    cli("send", "--to", ROOM, "--text", f"{dup} second")
    time.sleep(0.4)
    r4body = f"reply-latest-{stamp}"
    rc, d, out, _ = cli("reply", "--room", ROOM, "--to", dup, "--text", r4body)
    matched_latest = bool(d and d.get("ok") and "second" in (d.get("matched") or ""))
    record("R4 targets most-recent matching message", matched_latest,
           f"matched={d and d.get('matched')}")

    # R6: reply body with Korean + punctuation + emoji-like chars.
    r6body = f"답장!@#({stamp})~"
    rc, d, out, _ = cli("reply", "--room", ROOM, "--to", target, "--text", r6body)
    record("R6 special/Korean chars in reply body", bool(d and d.get("ok")))

    # R2: cold reply — reload tab so no room is open, then reply (must navigate).
    reload_tab()
    r2body = f"reply-cold-{stamp}"
    rc, d, out, elapsed = cli("reply", "--room", ROOM, "--to", target, "--text", r2body)
    ok2 = bool(d and d.get("ok") and d.get("verified_by") == "reply_bubble")
    record("R2 cold reply happy path", ok2,
           f"path={d and d.get('path')} {d and d.get('duration_ms')}ms")
    record("R9 cold reply latency < 1s", ok2 and d.get("duration_ms", 9e9) < LATENCY_BUDGET_MS,
           f"{d and d.get('duration_ms')}ms (budget {LATENCY_BUDGET_MS})")


# ─────────────────────────────────────────────────────────────────────────────
# Sticker tests
# ─────────────────────────────────────────────────────────────────────────────

# Reasons that mean the sticker picker / packages are not reachable in this
# environment (the picker needs a trusted user-activation gesture the AppleScript
# bridge cannot produce). Happy-path sticker tests SKIP — not FAIL — on these.
PICKER_BLOCKED = {"picker_unavailable", "no_packages", "empty_package"}


def test_sticker():
    print("\n# send-sticker")

    # S5: missing args -> argparse error. Pure CLI logic — always runnable.
    rc, _, _, _ = cli("send-sticker")
    record("S5 missing args rejected", rc == 2, f"exit={rc}")

    # S1/S4/S6: happy path + latency + verification.
    rc, d, out, _ = cli("send-sticker", "--to", ROOM)
    blocked = bool(d and not d.get("ok") and d.get("reason") in PICKER_BLOCKED)
    if d and d.get("ok"):
        record("S1 send-sticker happy path", True, f"{d.get('duration_ms')}ms")
        record("S4 send-sticker latency < 1s",
               d.get("duration_ms", 9e9) < LATENCY_BUDGET_MS, f"{d.get('duration_ms')}ms")
        record("S6 sticker bubble verified", d.get("verified_by") == "sticker_bubble",
               f"verified_by={d.get('verified_by')}")
    elif blocked:
        why = f"environment-blocked ({d.get('reason')}) — sticker picker needs trusted input"
        skip("S1 send-sticker happy path", why)
        skip("S4 send-sticker latency < 1s", "blocked by S1")
        skip("S6 sticker bubble verified", "blocked by S1")
    else:
        record("S1 send-sticker happy path", False, f"{out[:160]}")

    # S2: address a sticker by package/index.
    rc, d, out, _ = cli("send-sticker", "--to", ROOM, "--package", "0", "--sticker", "0")
    if d and d.get("ok"):
        record("S2 send-sticker by index", True)
    elif d and not d.get("ok") and d.get("reason") in PICKER_BLOCKED:
        skip("S2 send-sticker by index", f"environment-blocked ({d.get('reason')})")
    else:
        record("S2 send-sticker by index", False, f"{out[:120]}")

    # S3: invalid index -> clean (ok:false) failure. The command must never crash or
    # falsely succeed on a bad index — verifiable regardless of picker availability.
    rc, d, out, _ = cli("send-sticker", "--to", ROOM, "--package", "999", "--sticker", "999")
    record("S3 invalid index fails cleanly, no crash",
           bool(d is not None and not d.get("ok")),
           f"reason={d and d.get('reason')}")


def main():
    if not ROOM:
        print("ERROR: set LINE_TEST_ROOM (e.g. LINE_TEST_ROOM='나만의 그룹')")
        sys.exit(2)
    print(f"Test room: {ROOM!r}   latency budget: {LATENCY_BUDGET_MS}ms")
    test_reply()
    test_sticker()

    print("\n" + "=" * 56)
    p = sum(1 for _, s, _ in results if s == "PASS")
    f = sum(1 for _, s, _ in results if s == "FAIL")
    sk = sum(1 for _, s, _ in results if s == "SKIP")
    print(f"  PASS {p}   FAIL {f}   SKIP {sk}   (total {len(results)})")
    if f:
        print("  failed:")
        for n, s, dt in results:
            if s == "FAIL":
                print(f"    - {n}: {dt}")
    sys.exit(1 if f else 0)


if __name__ == "__main__":
    main()
