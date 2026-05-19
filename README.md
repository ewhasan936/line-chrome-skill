# line-chrome-cli

**English** | [한국어](README.ko.md) | [日本語](README.ja.md)

Drive the **official LINE Chrome extension** from the command line — or let an AI
assistant do it for you. Send messages, catch up on conversations, summarize, search,
and watch for replies, all by injecting JavaScript through AppleScript's
`execute javascript` bridge.

It does **not** touch the LINE desktop app, harvest tokens, or reverse-engineer LINE's
private API. Every outbound message is produced by your own logged-in LINE extension,
exactly as if you typed it in the UI.

> macOS only. Relies on the AppleScript ↔ Chrome bridge.

<!-- Optional: drop a demo screenshot or GIF here, e.g. ![demo](docs/demo.gif) -->

## What you can do

Install it as a [Claude Code](https://claude.com/claude-code) skill (or wire it into
any agent or shell script), and then just ask in plain language:

| You say | What happens |
| --- | --- |
| "Tell Alex I'll be 5 minutes late" | Finds the chat, types, sends — verified delivered |
| "What did I miss in the team room?" | Reads recent messages and reports back |
| "Summarize today's conversation in the Family group" | Pulls history and summarizes it |
| "Find the address Alex sent me yesterday" | Searches a room's messages for a keyword |
| "Tell me when someone replies in the project room" | Watches for new incoming messages |

### Meaningful scenarios

- **Morning catch-up** — summarize overnight messages across your busy rooms in one go.
- **Reply drafting** — the assistant reads the thread, drafts a context-aware reply,
  and you approve before it sends.
- **Inbox triage** — "did anyone ask me a direct question I haven't answered yet?"
- **Scheduled reminders** — combine with `cron`: every weekday 9am, post a standup
  reminder to a room.
- **Archiving** — dump a room's history to a file for your own records.

Every one of these maps to a plain CLI subcommand (`send`, `history`, `search`,
`watch`, …), so it works just as well inside shell scripts with no agent involved.

## How it works

```
cli.py  ──osascript──▶  Google Chrome  ──execute javascript──▶  LINE extension DOM
```

The LINE extension renders a normal web page inside a Chrome tab. `cli.py` locates
that tab and runs small JS snippets in it (set the search box, click a chat row,
type into the editor, dispatch Enter, scrape message bubbles). UI selectors are
externalized in `selectors.json` so a LINE update can be repaired without code changes.

## Prerequisites

![The LINE Chrome extension detached into its own window](docs/line-window.png)

*The LINE extension detached into its own Chrome window — the state this tool drives.*

1. **Install the LINE Chrome extension**
   <https://chromewebstore.google.com/detail/line/ophjlpahpchlmihnnnihgmmeilfjmjjc>
2. **Log in once** — click the extension icon and sign in via QR. Never automate this
   step; LINE's bot detection watches the auth flow.
3. **Detach the extension into its own window.** AppleScript can inject JS into Chrome
   *tabs*, not extension popups. Detached, it becomes a normal Chrome window with one
   tab (`…/index.html#/chats/…`).
4. **Enable AppleScript JS execution** — Chrome menu
   `View → Developer → Allow JavaScript from Apple Events`.
   `python3 cli.py enable-applescript` can do this for you.
5. **Grant Accessibility permission** — only needed for `enable-applescript`, which
   clicks the Chrome menu via System Events. Add your terminal under
   `System Settings → Privacy & Security → Accessibility`.

## Install

No dependencies beyond Python 3.9+ and `osascript` (preinstalled on macOS).

```sh
git clone https://github.com/ewhasan936/line-chrome-cli.git
cd line-chrome-cli
python3 cli.py status
```

Optionally put it on your `PATH`:

```sh
ln -s "$PWD/cli.py" /usr/local/bin/line-chrome
line-chrome status
```

To use it as a Claude Code skill, place this directory where Claude Code looks for
skills — the bundled `SKILL.md` makes it discoverable.

## Usage

```sh
python3 cli.py status                    # Chrome attached? selectors loaded?
python3 cli.py enable-applescript         # turn on "Allow JavaScript from Apple Events"
python3 cli.py diagnose                   # check every selector against the live DOM

python3 cli.py list-rooms --limit 50
python3 cli.py list-contacts --limit 50

python3 cli.py send --to "Alex" --text "Hello"
python3 cli.py history --room "Family" --limit 50
python3 cli.py search --room "Family" --query "meeting"
python3 cli.py reply --room "Family" --to "see you at 6" --text "got it"
python3 cli.py send-sticker --to "Family"   # see "Stickers" below
python3 cli.py send-sticker --to "Family" --meaning thanks
python3 cli.py sticker-tags set thanks --package 0 --sticker 3 --label "thank you"
python3 cli.py brief --room "Family" --room "Team"
python3 cli.py needs-reply --room "Team"
python3 cli.py tone-profiles set polite --prefix "Hi, " --suffix " Thanks."
python3 cli.py tone-profiles assign "Team" --profile polite
python3 cli.py follow-ups add --room "Team" --text "check for a reply" --in 2h
python3 cli.py schedule add --to "Team" --text "Standup time" --at "2030-01-02 09:00"
python3 cli.py schedule run --dry-run
python3 cli.py allowed-rooms add "나만의 그룹"
python3 cli.py allowed-rooms enable
python3 cli.py leave-group --room "Old Group" --confirm   # irreversible — see below
python3 cli.py watch --interval 5         # poll for new messages (Ctrl-C to stop)

python3 cli.py selectors show
python3 cli.py selectors set message_input "textarea-ex.text"

python3 cli.py cache-info                 # locate the extension's LevelDB store
python3 cli.py cache-dump --out ~/line-cache-copy
```

All commands print JSON to stdout.

## Tests

```sh
python3 -m unittest tests/test_send_sticker_contract.py
LINE_TEST_ONLY=sticker LINE_TEST_ROOM="나만의 그룹" python3 tests/test_reply_sticker.py
```

The first command checks the `send-sticker` JSON/validation contract without
touching Chrome or LINE. The second runs the live sticker matrix only in the
configured test room, including hot/cold latency checks under 1s.

### `enable-applescript`

Probes whether AppleScript JS execution is on. If off, it brings Chrome to the front
and clicks `View → Developer → Allow JavaScript from Apple Events` via System Events,
then re-probes to confirm. It only clicks when the setting is **off**, so it never
accidentally toggles it back off.

Note: Chrome blocks turning this setting **off** via AppleScript — only the on
direction is automatable, which is all this command needs.

## Daily automation

`brief` scans recent messages and returns structured JSON: message counts, recent
previews, question/request counts, likely reply-needed items, and a conversation
summary in `summary.text`. With no room provided it uses a fast path that reads
the currently open room once. Multiple rooms are supported; the default
`--max-runtime-ms 900` budget marks remaining rooms as `deadline_exceeded`
instead of hanging.

```sh
python3 cli.py brief --room "Family" --room "Team" --limit 50
python3 cli.py daily-brief --rooms "Family,Team" --preview 3 --max-runtime-ms 1500
```

`needs-reply` finds received question/request-like messages after your latest sent
message in each room. With no room provided it reads the currently open room, and
`--max-runtime-ms` controls the scan budget for multiple rooms.

```sh
python3 cli.py needs-reply --room "Team"
python3 cli.py inbox --rooms "Family,Team" --include-before-last-sent
```

`tone-profiles` stores manual room tone profiles. The current implementation does
not rewrite with an LLM; it applies user-defined prefixes and suffixes.

```sh
python3 cli.py tone-profiles set polite --prefix "Hi, " --suffix " Thanks."
python3 cli.py tone-profiles assign "Team" --profile polite
python3 cli.py send --to "Team" --text "confirmed"   # profile auto-applies
python3 cli.py send --to "Team" --text "confirmed" --no-profile
```

`follow-ups` is a local reminder list. `send` and `reply` can also create a
follow-up after successful delivery.

```sh
python3 cli.py follow-ups add --room "Team" --text "check for a reply" --in 2h
python3 cli.py follow-ups due
python3 cli.py send --to "Team" --text "please confirm" --follow-up-in 1d
```

`schedule` is a scheduled-send queue. It does not run a background daemon; execute
`schedule run` from `cron` or `launchd` to send due items.

```sh
python3 cli.py schedule add --to "Team" --text "Standup time" --at "2030-01-02 09:00"
python3 cli.py schedule add --to "Team" --text "Share today's blockers" --in 10m
python3 cli.py schedule run
```

`allowed-rooms` is the outbound safety rail. When enabled, `send`, `reply`,
`send-sticker`, `leave-group`, and `schedule add/run` fail before touching Chrome
if the target room is not explicitly allowed.

```sh
python3 cli.py allowed-rooms add "나만의 그룹"
python3 cli.py allowed-rooms enable
python3 cli.py allowed-rooms show
```

### `reply`

`reply --room R --to "<substring>" --text "<body>"` replies to a specific earlier
message (a quoted reply). `--to` is a substring identifying the message being
replied to; if several match, the most recent one is used. Completes in roughly
0.4s when already in the room, ~0.8s when it has to navigate first.

### `send-sticker`

`send-sticker --to R [--package N] [--sticker N]` sends a sticker, addressed by
package/sticker index (default `0 0` — the first sticker of the first package).
`send-sticker --to R --meaning TAG` sends a tagged sticker from
`~/.config/line-chrome/stickers.json`.

Create or update tags with:

```sh
python3 cli.py sticker-tags set thanks --package 0 --sticker 3 --label "thank you"
python3 cli.py sticker-tags set sorry --package 0 --sticker 7 --label "sorry"
python3 cli.py sticker-tags show
python3 cli.py sticker-tags remove thanks
```

Tags are exact labels you choose, so Korean tags such as `고마워`, `감사`, or `미안`
work the same way. If `--meaning` is not mapped, the command returns
`{"ok": false, "reason": "meaning_not_mapped"}` before touching Chrome or LINE.
For LINE character stickers, first pick the package/sticker indexes from your own
picker order, then tag them, for example `미안해`, `고마워`, or `축하`.

Opening LINE's sticker picker requires a *trusted* user-activation gesture, so
`send-sticker` uses macOS CoreGraphics session-event clicks in Chrome. Grant
Accessibility permission to the terminal or app running the CLI. When trusted input
is unavailable, the command returns `{"ok": false, "reason": "trusted_input_unavailable"}`
instead of failing opaquely.

Successful sends are verified by detecting a new sticker message bubble. Negative
indexes are rejected before touching LINE, and out-of-range package/sticker indexes
return clean `ok: false` JSON responses.

### `leave-group`

Permanently removes you from a group. **This is irreversible** — once you leave you
cannot rejoin on your own; a current member has to invite you back.

Because of that, `leave-group` uses a two-step confirmation:

1. Run it first **without** `--confirm`. Nothing destructive happens — it returns
   `reason: "confirmation_required"` plus a `warning`:
   ```sh
   python3 cli.py leave-group --room "Old Group"
   ```
2. Read the warning, make sure you really want to leave, then run again **with**
   `--confirm`:
   ```sh
   python3 cli.py leave-group --room "Old Group" --confirm
   ```

The command verifies the open chatroom header matches `--room` before doing
anything, and aborts before the destructive step if the menu or the confirmation
modal does not appear as expected. If an AI assistant runs this for you, it should
show you the warning and get an explicit yes before passing `--confirm`.

## Fixing broken selectors

When LINE ships an extension update, a selector may stop matching. No code change is
needed:

1. `python3 cli.py diagnose` — reports which selector key fails.
2. Open DevTools inside the LINE extension window and inspect the element.
3. Pick a stable selector (prefer `data-*` / `aria-*` / `role` > class > tag).
4. Override it, by priority:
   - **One-off:** `--selector message_input='…'` on any command (repeatable).
   - **Persistent:** `~/.config/line-chrome/selectors.json`
     ```json
     { "selectors": { "message_input": "textarea-ex.text" } }
     ```
   - **Repo default:** edit `selectors.json` in this directory.
5. Re-run `diagnose` to confirm.

Selector priority: `--selector` flag > `~/.config/line-chrome/selectors.json` > repo
`selectors.json`. Nine of the 12 keys are verified against a live DOM; `search_input`,
`send_button`, and `message_author` ship as generic fallbacks. (`message_author` has
no stable element — author names are read from a `data-message-content-prefix`
attribute instead, so `diagnose` reporting it as unmatched is expected and harmless.)

## Message history & full-text search

`history` and `search` scrape the rendered DOM, so they only see messages currently
loaded in the chat view. For deep history, the extension's IndexedDB (LevelDB) holds
everything:

- `cache-info` — locate the store, show size and last-modified time.
- `cache-dump --out <dir>` — best-effort snapshot copy (`cp -R`) while Chrome holds the
  lock. For a clean read, quit Chrome first.

Decoding the LevelDB + V8-serialized entries is out of scope; point an external tool
(e.g. Node's `level` + `v8` deserialize) at the dumped copy.

## Caveats

- **macOS only.** Uses the AppleScript bridge to Chrome.
- **First login is manual.** Never script the QR auth flow.
- **Detach the extension window.** Popups can't be reached by AppleScript.
- **Selectors can drift.** LINE updates may rotate class hashes; see the fix procedure
  above.
- This is an unofficial tool and is not affiliated with or endorsed by LINE.

## License

[MIT](LICENSE)
