# line-chrome

Drive the **official LINE Chrome extension** from the command line — send messages,
read history, list rooms, watch for new messages — by injecting JavaScript through
AppleScript's `execute javascript` bridge.

It does **not** touch the LINE desktop app, harvest tokens, or reverse-engineer
LINE's private API. Every outbound message is produced by your own logged-in LINE
extension, exactly as if you typed it in the UI.

> macOS only. Relies on the AppleScript ↔ Chrome bridge.

## How it works

```
cli.py  ──osascript──▶  Google Chrome  ──execute javascript──▶  LINE extension DOM
```

The LINE extension renders a normal web page inside a Chrome tab. `cli.py` locates
that tab and runs small JS snippets in it (set the search box, click a chat row,
type into the editor, dispatch Enter, scrape message bubbles). UI selectors are
externalized in `selectors.json` so a LINE update can be repaired without code
changes.

## Prerequisites

1. **Install the LINE Chrome extension**
   <https://chromewebstore.google.com/detail/line/ophjlpahpchlmihnnnihgmmeilfjmjjc>
2. **Log in once** — click the extension icon and sign in via QR. Never automate
   this step; LINE's bot detection watches the auth flow.
3. **Detach the extension into its own window.** AppleScript can inject JS into
   Chrome *tabs*, not extension popups. Detached, it becomes a normal Chrome window
   with one tab (`…/index.html#/chats/…`).
4. **Enable AppleScript JS execution** — Chrome menu
   `View → Developer → Allow JavaScript from Apple Events`.
   `cli.py enable-applescript` can do this for you (see below).
5. **Grant Accessibility permission** — only needed for `enable-applescript`, which
   clicks the Chrome menu via System Events. Add your terminal (or the app running
   the CLI) under `System Settings → Privacy & Security → Accessibility`.

## Install

No dependencies beyond Python 3.9+ and `osascript` (preinstalled on macOS).

```sh
git clone https://github.com/ewhasan936/line-chrome.git
cd line-chrome
python3 cli.py status
```

Optionally put it on your `PATH`:

```sh
ln -s "$PWD/cli.py" /usr/local/bin/line-chrome
line-chrome status
```

## Usage

```sh
python3 cli.py status                    # Chrome attached? selectors loaded?
python3 cli.py enable-applescript        # turn on "Allow JavaScript from Apple Events"
python3 cli.py diagnose                  # check every selector against the live DOM

python3 cli.py list-rooms --limit 50
python3 cli.py list-contacts --limit 50

python3 cli.py send --to "홍길동" --text "안녕하세요"
python3 cli.py history --room "Family" --limit 50
python3 cli.py search --room "Family" --query "회의"
python3 cli.py watch --interval 5        # poll for new messages (Ctrl-C to stop)

python3 cli.py selectors show
python3 cli.py selectors set message_input "textarea-ex.text"

python3 cli.py cache-info                # locate the extension's LevelDB store
python3 cli.py cache-dump --out ~/line-cache-copy
```

All commands print JSON to stdout.

### `enable-applescript`

Probes whether AppleScript JS execution is on. If off, it brings Chrome to the
front and clicks `View → Developer → Allow JavaScript from Apple Events` via
System Events, then re-probes to confirm. It only clicks when the setting is
**off**, so it never accidentally toggles it back off.

```json
{ "ok": true, "already_on": false, "action": "toggled_on" }
```

Note: Chrome blocks turning this setting **off** via AppleScript — only the
on direction is automatable, which is all this command needs.

## Fixing broken selectors

When LINE ships an extension update, a selector may stop matching. No code change
is needed:

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

Selector priority: `--selector` flag  >  `~/.config/line-chrome/selectors.json`  >
repo `selectors.json`.

The 12 selector keys are listed in `selectors.json`. Nine are verified against a
live DOM; `search_input`, `send_button`, and `message_author` ship as generic
fallbacks. (`message_author` has no stable element — author names are read from a
`data-message-content-prefix` attribute instead, so `diagnose` reporting it as
unmatched is expected and harmless.)

## Message history & full-text search

`history` and `search` scrape the rendered DOM, so they only see messages that
are currently loaded in the chat view. For deep history, the extension's
IndexedDB (LevelDB) holds everything:

- `cache-info` — locate the store, show size and last-modified time.
- `cache-dump --out <dir>` — best-effort snapshot copy (`cp -R`) while Chrome holds
  the lock. For a clean read, quit Chrome first.

Decoding the LevelDB + V8-serialized entries is out of scope; point an external
tool (e.g. Node's `level` + `v8` deserialize) at the dumped copy.

## Caveats

- **macOS only.** Uses the AppleScript bridge to Chrome.
- **First login is manual.** Never script the QR auth flow.
- **Detach the extension window.** Popups can't be reached by AppleScript.
- **Selectors can drift.** LINE updates may rotate class hashes; see the fix
  procedure above.
- This is an unofficial tool and is not affiliated with or endorsed by LINE.

## License

[MIT](LICENSE)
