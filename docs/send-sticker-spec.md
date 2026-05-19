# send-sticker spec and test matrix

## Current CLI spec

`python3 cli.py send-sticker --to ROOM [--package N] [--sticker N]`

`python3 cli.py send-sticker --to ROOM --meaning TAG`

- Sends the sticker at package index `--package` and sticker index `--sticker`;
  both default to `0`.
- Resolves `--meaning TAG` through `~/.config/line-chrome/stickers.json`.
  `--meaning` is mutually exclusive with explicit `--package`/`--sticker`.
- Manages manual tags with `sticker-tags set/show/remove`.
- Uses the existing hot/cold room behavior:
  - hot: if the current chat header matches `--to`, send without navigation.
  - cold: otherwise search for `--to`, open the room, then send.
- Uses macOS CoreGraphics session-event clicks for trusted OS-level input because
  LINE's sticker picker does not reliably open from JavaScript-injected clicks.
- Requires Chrome's AppleScript JavaScript bridge and Accessibility permission for
  the terminal/app running the CLI.
- Verifies success by detecting that the rendered sticker-bubble count increased.
- Returns JSON for every path.

## Success response

Required fields:

- `ok: true`
- `room`
- `path: "hot" | "cold"`
- `duration_ms`
- `verified_by: "sticker_bubble"`
- `package`
- `sticker`
- `trusted_input: "core_graphics"`
- `sticker_bubbles: [before, after]` where `after > before`
- Optional when `--meaning` is used: `meaning`, `resolved_tag`, `sticker_label`

The live integration target is under `1000ms` for both hot and cold paths.

## Failure contract

- Missing `--to`: argparse error, exit code `2`.
- Negative `--package` or `--sticker`: `ok: false`, `stage: "validate"`,
  `reason: "negative_index"`; must happen before Chrome/LINE access.
- `--meaning` combined with `--package` or `--sticker`: `ok: false`,
  `stage: "validate"`, `reason: "meaning_conflicts_with_index"`.
- Unmapped `--meaning`: `ok: false`, `stage: "validate"`,
  `reason: "meaning_not_mapped"`; must happen before Chrome/LINE access.
- No Chrome extension tab: `stage: "locate_tab"`.
- Sticker button missing or hidden: `stage: "open_picker"`.
- Accessibility/CoreGraphics click unavailable: `stage: "trusted_click"`,
  `reason: "trusted_input_unavailable"`.
- Picker did not open after trusted click: `stage: "open_picker"`,
  `reason: "picker_unavailable"`.
- Package index too large: `stage: "select_sticker"`,
  `reason: "package_out_of_range"`.
- Sticker index too large: `stage: "select_sticker"`,
  `reason: "sticker_out_of_range"`.
- Empty package: `stage: "select_sticker"`, `reason: "empty_package"`.
- Click happened but no new sticker bubble appeared: `stage: "not_confirmed"`.

## Test matrix

Always-runnable contract tests: `tests/test_send_sticker_contract.py`

- Parse trusted click coordinate success.
- Parse coordinate error responses.
- Reject malformed coordinate responses.
- Parse successful send only when sticker-bubble count increases.
- Treat unchanged sticker-bubble count as `not_confirmed`.
- Map no-tab abort to `locate_tab`.
- Map missing sticker button to `open_picker`.
- Map CoreGraphics permission/click errors to `trusted_input_unavailable`.
- Map picker timeout to `picker_unavailable`.
- Map package out-of-range to `package_out_of_range`.
- Map sticker out-of-range to `sticker_out_of_range`.
- Map verify timeout to `not_confirmed`.
- Map unknown workflow responses to `unknown`.
- Reject negative package index before Chrome access.
- Reject negative sticker index before Chrome access.
- Reject unmapped meanings before Chrome access.
- Reject `--meaning` combined with explicit indexes.
- Resolve mapped meanings to package/sticker indexes.
- Set, show, resolve, and remove manual sticker tags.

Live integration tests: `tests/test_reply_sticker.py` with `LINE_TEST_ROOM`.
Use `LINE_TEST_ONLY=sticker` to run only the sticker matrix against the configured
test room.

- Missing `--to` returns argparse exit code `2`.
- Negative package index fails cleanly.
- Negative sticker index fails cleanly.
- Hot happy path sends a sticker.
- Hot happy path finishes under `1000ms`.
- Hot happy path is verified by `sticker_bubble`.
- Hot path reports `path: "hot"`.
- Explicit `--package 0 --sticker 0` sends the first sticker.
- Cold happy path navigates to the room and sends.
- Cold happy path finishes under `1000ms`.
- Package index `999` fails cleanly with `package_out_of_range`.
- Sticker index `999` in package `0` fails cleanly with `sticker_out_of_range`.
