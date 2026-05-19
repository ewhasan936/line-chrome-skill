#!/usr/bin/env python3
"""line-chrome — drive LINE Chrome extension via AppleScript-injected JS.

Selector mapping is externalized for resilience to UI changes. Override priority:
  CLI --selector  >  ~/.config/line-chrome/selectors.json  >  selectors.json (repo default)
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class SkillError(Exception):
    """Raised for user-visible failures inside the CLI."""


def emit_json(data: Any) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


SKILL_DIR = Path(__file__).resolve().parent
DEFAULT_SEL_PATH = SKILL_DIR / "selectors.json"
USER_SEL_PATH = Path.home() / ".config" / "line-chrome" / "selectors.json"
USER_STICKER_TAGS_PATH = Path.home() / ".config" / "line-chrome" / "stickers.json"
EXTENSION_ID = "ophjlpahpchlmihnnnihgmmeilfjmjjc"
EXTENSION_PREFIX = f"chrome-extension://{EXTENSION_ID}/"


# ── Selector resolution ──────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise SkillError(f"selectors file at {path} has invalid JSON: {e}")


def load_selectors(cli_overrides: list[str]) -> tuple[dict, dict]:
    """Returns (resolved_selectors, sources_info)."""
    default_doc = _load_json(DEFAULT_SEL_PATH)
    user_doc = _load_json(USER_SEL_PATH)

    selectors: dict = {}
    selectors.update(default_doc.get("selectors", {}))
    selectors.update(user_doc.get("selectors", {}))

    cli_parsed: dict[str, str] = {}
    for ov in cli_overrides or []:
        if "=" not in ov:
            raise SkillError(f"--selector expects key=value, got '{ov}'")
        k, v = ov.split("=", 1)
        k = k.strip()
        if not k:
            raise SkillError(f"--selector key empty in '{ov}'")
        cli_parsed[k] = v
    selectors.update(cli_parsed)

    sources = {
        "default": str(DEFAULT_SEL_PATH),
        "user_override": str(USER_SEL_PATH) if USER_SEL_PATH.exists() else None,
        "cli_overrides": len(cli_parsed),
        "cli_keys": list(cli_parsed.keys()),
    }
    return selectors, sources


# ── AppleScript + Chrome bridge ─────────────────────────────────────────────

def _osascript(applescript: str, timeout: int = 30) -> str:
    try:
        out = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        raise SkillError("osascript not found — this skill is macOS-only")
    except subprocess.TimeoutExpired:
        raise SkillError(f"osascript timed out after {timeout}s")
    if out.returncode != 0:
        raise SkillError(f"osascript failed: {out.stderr.strip()}")
    return out.stdout.strip()


def find_extension_tab() -> dict:
    """Locate the LINE extension's Chrome window+tab. Prefers index.html (the main app)
    over popup.html. Returns {window_id, tab_id, url} where IDs are stable Chrome internal
    identifiers (NOT positional indices, which shift when windows open/close)."""
    script = f'''
on run
  if application "Google Chrome" is not running then return ""
  tell application "Google Chrome"
    set fallback to ""
    repeat with w in windows
      repeat with t in tabs of w
        set u to URL of t
        if u starts with "{EXTENSION_PREFIX}" then
          set candidate to (id of w as string) & "|" & (id of t as string) & "|" & u
          if u contains "/index.html" then
            -- Prefer the real detached LINE app window. Directly opening the extension
            -- URL can leave a Chrome error document whose URL still looks correct.
            set ttl to title of t
            if ttl is "LINE" then return candidate
            if fallback is "" then
              set fallback to candidate
            else if u contains "#/chats/" and fallback does not contain "#/chats/" then
              set fallback to candidate
            end if
          else if fallback is "" then
            set fallback to candidate
          end if
        end if
      end repeat
    end repeat
    return fallback
  end tell
  return ""
end run
'''
    raw = _osascript(script)
    if not raw:
        return {}
    win, tab, url = raw.split("|", 2)
    return {"window_id": int(win), "tab_id": int(tab), "url": url}


def chrome_running() -> bool:
    raw = _osascript('tell application "System Events" to (name of processes) contains "Google Chrome"')
    return raw.lower() == "true"


def exec_js(js: str, tab_loc: dict, timeout: int = 30,
            fallback: bool = True, focus: bool = False) -> str:
    """Execute JS in the located LINE extension tab. Looks up by stable window/tab id so
    background popups opening/closing between calls don't shift our target."""
    win_id = tab_loc.get("window_id")
    tab_id = tab_loc.get("tab_id")
    if win_id is None or tab_id is None:
        # Backward compat for old positional locator
        win_id = tab_loc.get("window_index")
        tab_id = tab_loc.get("tab_index")
    wrapped = (
        "(function(){try{var r=(function(){"
        + js
        + "})();return (typeof r==='string')?r:JSON.stringify(r);}"
        "catch(e){return JSON.stringify({error:String(e&&e.message||e)});}})()"
    )
    js_escaped = wrapped.replace("\\", "\\\\").replace('"', '\\"')
    # Locate the tab by id at execution time (handles windows/tabs that have shifted).
    # Popup windows are short-lived; callers can disable fallback so a stale popup id
    # never silently executes against the main LINE tab.
    fallback_block = ""
    if fallback:
        fallback_block = (
            f'  if targetTab is missing value then\n'
            f'    -- fallback: any tab matching the LINE extension URL prefix\n'
            f'    repeat with w in windows\n'
            f'      set tabIndex to 1\n'
            f'      repeat with t in tabs of w\n'
            f'        if URL of t starts with "{EXTENSION_PREFIX}" and URL of t contains "/index.html" then\n'
            f'          set targetTab to t\n'
            f'          set targetWindow to w\n'
            f'          set targetTabIndex to tabIndex\n'
            f'          exit repeat\n'
            f'        end if\n'
            f'        set tabIndex to tabIndex + 1\n'
            f'      end repeat\n'
            f'      if targetTab is not missing value then exit repeat\n'
            f'    end repeat\n'
            f'  end if\n'
        )
    focus_block = ""
    if focus:
        focus_block = (
            f'  if targetWindow is not missing value then\n'
            f'    if targetTabIndex is not 0 then set active tab index of targetWindow to targetTabIndex\n'
            f'    set visible of targetWindow to true\n'
            f'    set index of targetWindow to 1\n'
            f'    activate\n'
            f'  end if\n'
        )
    script = (
        f'tell application "Google Chrome"\n'
        f'  set targetTab to missing value\n'
        f'  set targetWindow to missing value\n'
        f'  set targetTabIndex to 0\n'
        f'  repeat with w in windows\n'
        f'    if (id of w as integer) is {win_id} then\n'
        f'      set tabIndex to 1\n'
        f'      repeat with t in tabs of w\n'
        f'        if (id of t as integer) is {tab_id} then\n'
        f'          set targetTab to t\n'
        f'          set targetWindow to w\n'
        f'          set targetTabIndex to tabIndex\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'        set tabIndex to tabIndex + 1\n'
        f'      end repeat\n'
        f'    end if\n'
        f'    if targetTab is not missing value then exit repeat\n'
        f'  end repeat\n'
        f'{fallback_block}'
        f'  if targetTab is missing value then return ""\n'
        f'{focus_block}'
        f'  return execute targetTab javascript "{js_escaped}"\n'
        f'end tell'
    )
    return _osascript(script, timeout=timeout)


# ── JS payloads ──────────────────────────────────────────────────────────────

def js_diagnose(selectors: dict) -> str:
    return f"""
const map = {json.dumps(selectors, ensure_ascii=False)};
const out = [];
for (const k of Object.keys(map)) {{
  const css = map[k];
  let n = 0;
  try {{ n = document.querySelectorAll(css).length; }} catch (_) {{ n = -1; }}
  out.push({{ key: k, selector: css, match_count: n, ok: n > 0 }});
}}
return JSON.stringify({{
  matched: out.filter(r => r.ok).length,
  broken: out.filter(r => !r.ok).length,
  results: out,
}});
"""


def js_list_rooms(selectors: dict, limit: int) -> str:
    return f"""
const sel = {json.dumps(selectors, ensure_ascii=False)};
const items = document.querySelectorAll(sel.chat_list_item);
const out = [];
for (let i = 0; i < items.length && i < {limit}; i++) {{
  const it = items[i];
  const nameEl = it.querySelector(sel.chat_list_item_name) || it;
  out.push({{ index: i, name: (nameEl.textContent || '').trim() }});
}}
return JSON.stringify({{ count: out.length, rooms: out }});
"""


def js_set_search(selectors: dict, query: str) -> str:
    """Sync: clear then type query into the chat-list search input."""
    return f"""
const sel = {json.dumps(selectors, ensure_ascii=False)};
const Q = {json.dumps(query, ensure_ascii=False)};
function go() {{
  const sb = document.querySelector(sel.search_input);
  if (!sb) return {{ ok: false, error: 'search_input not found' }};
  sb.focus();
  const vs = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  // Always clear first so React picks up the change cleanly
  vs.call(sb, '');
  sb.dispatchEvent(new Event('input', {{ bubbles: true }}));
  if (Q) {{
    vs.call(sb, Q);
    sb.dispatchEvent(new Event('input', {{ bubbles: true }}));
  }}
  return {{ ok: true, value: sb.value }};
}}
return go();
"""


def js_click_room_in_list(selectors: dict, room_name: str) -> str:
    """Sync: among currently visible chat_list_item elements, find one matching room_name and click."""
    return f"""
const sel = {json.dumps(selectors, ensure_ascii=False)};
const ROOM = {json.dumps(room_name, ensure_ascii=False)};
function go() {{
  const items = document.querySelectorAll(sel.chat_list_item);
  if (!items.length) return {{ ok: false, step: 'chat_list_item', error: 'no chat list items visible' }};
  let target = null;
  for (const it of items) {{
    const nm = (it.querySelector(sel.chat_list_item_name)?.textContent || '').trim();
    if (nm === ROOM) {{ target = it; break; }}
  }}
  if (!target) {{
    for (const it of items) {{
      const nm = (it.querySelector(sel.chat_list_item_name)?.textContent || '').trim();
      if (nm.includes(ROOM)) {{ target = it; break; }}
    }}
  }}
  if (!target) {{
    return {{ ok: false, step: 'find_room', error: 'room not found: ' + ROOM,
             visible: Array.from(items).map(it => (it.querySelector(sel.chat_list_item_name)?.textContent || '').trim()) }};
  }}
  // Pick the chat-row's main button. The DOM has both a profile-image button and the
  // navigation button; clicking the profile one opens a modal instead of opening the chat.
  const btn = target.querySelector('button[class*="button_chatlist_item"]')
            || target.querySelector('button:not([class*="button_profile"])')
            || target.querySelector('button');
  if (btn) btn.click();
  return {{ ok: true, matched: (target.querySelector(sel.chat_list_item_name)?.textContent || '').trim() }};
}}
return go();
"""


def _navigate_to_room(sel: dict, loc: dict, room_name: str) -> dict:
    """Set search → poll for filtered list → click → poll for header change.
    All in ONE osascript invocation with polling instead of fixed sleeps."""
    win_id = loc.get("window_id")
    tab_id = loc.get("tab_id")
    set_js = js_set_search(sel, room_name)
    click_js = js_click_room_in_list(sel, room_name)
    # Combined check: returns header AND chat_list_item count + names matching room_name
    cli_sel = json.dumps(sel.get("chat_list_item"), ensure_ascii=False)
    name_sel = json.dumps(sel.get("chat_list_item_name"), ensure_ascii=False)
    hdr_sel = json.dumps(sel.get("chat_room_header"), ensure_ascii=False)
    room_lit = json.dumps(room_name, ensure_ascii=False)
    state_js = (
        "(function(){"
        f"var items=document.querySelectorAll({cli_sel});"
        f"var ROOM={room_lit};"
        "var matchCount=0;"
        "for(var i=0;i<items.length;i++){"
        f"var n=(items[i].querySelector({name_sel})?.textContent||'').trim();"
        "if(n===ROOM||n.includes(ROOM)||ROOM.includes(n))matchCount++;"
        "}"
        f"var h=document.querySelector({hdr_sel});"
        "var header=h?(h.textContent||'').trim():'';"
        "return JSON.stringify({itemCount:items.length,matchCount:matchCount,header:header});"
        "})()"
    )

    def wrap(js: str) -> str:
        wrapped = (
            "(function(){try{var r=(function(){"
            + js
            + "})();return (typeof r==='string')?r:JSON.stringify(r);}"
            "catch(e){return JSON.stringify({error:String(e&&e.message||e)});}})()"
        )
        return wrapped.replace("\\", "\\\\").replace('"', '\\"')

    def esc(js):
        return js.replace("\\", "\\\\").replace('"', '\\"')

    set_esc = wrap(set_js)
    click_esc = wrap(click_js)
    state_esc = esc(state_js)

    # Reset folder tab to "All" only if not already there. LINE auto-switches folder when
    # you navigate into a group/official account; skipping this when already on ALL saves
    # ~150ms (one osascript + 100ms settle).
    reset_folder_js = (
        "var btns=document.querySelectorAll('[class*=\"folderTab-module__tab_item__\"]');"
        "var current=Array.from(btns).find(function(b){return (b.getAttribute('aria-selected')==='true'||b.classList.value.includes('active'));});"
        "var currentText=current?(current.textContent||'').trim():'';"
        "if(currentText==='전체'||currentText==='All'||currentText==='すべて'){return JSON.stringify({skipped:true,current:currentText});}"
        "var all=Array.from(btns).find(function(b){var t=(b.textContent||'').trim();return t==='전체'||t==='All'||t==='すべて';});"
        "if(all)all.click();return JSON.stringify({clicked:!!all,was:currentText});"
    )
    try:
        result = exec_js(reset_folder_js, loc, timeout=5)
        if "skipped" not in result:
            time.sleep(0.1)
    except Exception:
        pass

    last_result = {"ok": False, "step": "init"}
    # If the room name has special chars LINE search may not match (parens, slashes),
    # also try shorter variants as the search query while still matching the full name on click
    import re as _re
    safe_q = _re.sub(r"[()\[\]{}/\\]", "", room_name).strip()
    first_word = _re.split(r"[\s()\[\]{}/\\]", room_name, 1)[0].strip()
    candidate_queries = [room_name]
    if safe_q and safe_q != room_name and safe_q not in candidate_queries:
        candidate_queries.append(safe_q)
    if first_word and first_word not in candidate_queries:
        candidate_queries.append(first_word)
    # Last-resort: prefix of first 3 chars
    if len(room_name) >= 3 and room_name[:3] not in candidate_queries:
        candidate_queries.append(room_name[:3])
    for q in candidate_queries:
        set_esc_q = wrap(js_set_search(sel, q))
        # Single osascript with polling: set search → poll filter → click → poll header.
        # Total max ≈ 30 * 50ms (filter) + 30 * 50ms (header) = 3s; typical 200-600ms.
        script = (
            f'set jsSet to "{set_esc_q}"\n'
            f'set jsClick to "{click_esc}"\n'
            f'set jsState to "{state_esc}"\n'
            f'tell application "Google Chrome"\n'
            f'  set targetTab to missing value\n'
            f'  repeat with w in windows\n'
            f'    if (id of w as integer) is {win_id} then\n'
            f'      repeat with t in tabs of w\n'
            f'        if (id of t as integer) is {tab_id} then\n'
            f'          set targetTab to t\n'
            f'          exit repeat\n'
            f'        end if\n'
            f'      end repeat\n'
            f'    end if\n'
            f'    if targetTab is not missing value then exit repeat\n'
            f'  end repeat\n'
            f'  if targetTab is missing value then\n'
            f'    repeat with w in windows\n'
            f'      repeat with t in tabs of w\n'
            f'        if URL of t starts with "{EXTENSION_PREFIX}" and URL of t contains "/index.html" then\n'
            f'          set targetTab to t\n'
            f'          exit repeat\n'
            f'        end if\n'
            f'      end repeat\n'
            f'      if targetTab is not missing value then exit repeat\n'
            f'    end repeat\n'
            f'  end if\n'
            f'  if targetTab is missing value then return ""\n'
            f'  execute targetTab javascript jsSet\n'
            f'  -- Poll: wait until filtered list contains a match (max ~1.5s)\n'
            f'  set filterReady to false\n'
            f'  repeat 30 times\n'
            f'    delay 0.05\n'
            f'    set s to execute targetTab javascript jsState\n'
            f'    if s contains "\\"matchCount\\":0" then\n'
            f'      -- not yet\n'
            f'    else\n'
            f'      set filterReady to true\n'
            f'      exit repeat\n'
            f'    end if\n'
            f'  end repeat\n'
            f'  if not filterReady then return "{{\\"ok\\":false,\\"step\\":\\"filter_timeout\\"}}@@@{{}}"\n'
            f'  set clickResult to execute targetTab javascript jsClick\n'
            f'  -- Brief settle for React to apply navigation. The subsequent fast-send\n'
            f'  -- call validates the chatroom header itself, so we skip a separate poll here.\n'
            f'  delay 0.15\n'
            f'  set finalState to execute targetTab javascript jsState\n'
            f'  return clickResult & "@@@" & finalState\n'
            f'end tell'
        )
        try:
            raw = _osascript(script, timeout=10)
        except SkillError as e:
            last_result = {"ok": False, "step": "osascript", "error": str(e)}
            continue
        if not raw or "@@@" not in raw:
            last_result = {"ok": False, "step": "osascript", "raw": raw or ""}
            continue
        click_raw, state_raw = raw.split("@@@", 1)
        try:
            click_result = json.loads(click_raw)
        except json.JSONDecodeError:
            click_result = {"ok": False, "raw": click_raw}
        try:
            state_result = json.loads(state_raw) if state_raw else {}
        except json.JSONDecodeError:
            state_result = {}
        if not click_result.get("ok"):
            last_result = click_result
            continue
        # Click succeeded → return ok regardless of header (the caller's fast_send
        # will validate the actual chatroom header). React often hasn't updated the
        # header yet at the moment we return, but the chat IS opening.
        return {"ok": True, "matched": click_result.get("matched"),
                "header": state_result.get("header", ""), "query": q}
    return last_result


def _open_visible_room_fast(sel: dict, loc: dict, room_name: str) -> dict:
    """Fast cold path for rooms already visible in the chat list.

    Avoids search input churn; used before the deeper search-based navigator.
    """
    win_id = loc.get("window_id")
    tab_id = loc.get("tab_id")
    click_js = js_click_room_in_list(sel, room_name)

    def wrap(js: str) -> str:
        wrapped = (
            "(function(){try{var r=(function(){"
            + js
            + "})();return (typeof r==='string')?r:JSON.stringify(r);}"
            "catch(e){return JSON.stringify({error:String(e&&e.message||e)});}})()"
        )
        return wrapped.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'set jsClick to "{wrap(click_js)}"\n'
        f'tell application "Google Chrome"\n'
        f'  set targetTab to missing value\n'
        f'  repeat with w in windows\n'
        f'    if (id of w as integer) is {win_id} then\n'
        f'      repeat with t in tabs of w\n'
        f'        if (id of t as integer) is {tab_id} then\n'
        f'          set targetTab to t\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'      end repeat\n'
        f'    end if\n'
        f'    if targetTab is not missing value then exit repeat\n'
        f'  end repeat\n'
        f'  if targetTab is missing value then return "{{\\"ok\\":false,\\"step\\":\\"no_tab\\"}}@@@"\n'
        f'  set clickResult to execute targetTab javascript jsClick\n'
        f'  if clickResult does not contain "\\"ok\\":true" then return clickResult & "@@@"\n'
        f'  return clickResult & "@@@"\n'
        f'end tell'
    )
    try:
        raw = _osascript(script, timeout=5)
    except SkillError as e:
        return {"ok": False, "step": "osascript", "error": str(e)}
    if "@@@" not in raw:
        return {"ok": False, "step": "bad_response", "raw": raw[:200]}
    click_raw, header = raw.split("@@@", 1)
    try:
        click_result = json.loads(click_raw)
    except json.JSONDecodeError:
        click_result = {"ok": False, "raw": click_raw}
    if not click_result.get("ok"):
        return click_result
    return {"ok": True, "matched": click_result.get("matched"), "header": header}


def js_type_and_send(selectors: dict, text: str) -> str:
    """Sync: type into message input and dispatch Enter to send. Assumes the chat is already open.
    Handles LINE's <textarea-ex> Web Component by drilling into its shadow <textarea>."""
    return f"""
const sel = {json.dumps(selectors, ensure_ascii=False)};
const TEXT = {json.dumps(text, ensure_ascii=False)};
function setNativeValue(el, val) {{
  const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, val);
}}
function go() {{
  let mi = document.querySelector(sel.message_input);
  if (!mi) return {{ ok: false, step: 'message_input', error: 'not found' }};
  // If it's a textarea-ex Web Component, drill into shadow root for the real textarea
  let target = mi;
  if (mi.shadowRoot) {{
    const inner = mi.shadowRoot.querySelector('textarea, input, [contenteditable]');
    if (inner) target = inner;
  }}
  target.focus();
  const tag = target.tagName.toLowerCase();
  if (tag === 'textarea' || tag === 'input') {{
    setNativeValue(target, TEXT);
  }} else if (target.getAttribute && target.getAttribute('contenteditable') !== null) {{
    target.textContent = TEXT;
  }} else {{
    target.textContent = TEXT;
  }}
  // Fire input events on inner element (React listens here)
  target.dispatchEvent(new Event('input', {{ bubbles: true, composed: true }}));
  target.dispatchEvent(new InputEvent('input', {{ bubbles: true, composed: true, inputType: 'insertText', data: TEXT }}));
  // Also signal the host element (textarea-ex listens for its inner state)
  if (mi !== target) {{
    mi.removeAttribute('data-is-empty');
    mi.dispatchEvent(new Event('input', {{ bubbles: true, composed: true }}));
  }}
  // Send via Enter — LINE's <textarea-ex> binds the send handler on the HOST element,
  // not the shadow inner textarea. Fire on host first, then on inner as backup.
  const enterInit = {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true, composed: true }};
  mi.dispatchEvent(new KeyboardEvent('keydown', enterInit));
  mi.dispatchEvent(new KeyboardEvent('keypress', enterInit));
  mi.dispatchEvent(new KeyboardEvent('keyup', enterInit));
  if (mi !== target) {{
    target.dispatchEvent(new KeyboardEvent('keydown', enterInit));
    target.dispatchEvent(new KeyboardEvent('keypress', enterInit));
    target.dispatchEvent(new KeyboardEvent('keyup', enterInit));
  }}
  // Fallback: explicit send button if present (LINE doesn't expose one normally)
  const sBtn = document.querySelector(sel.send_button);
  if (sBtn && !sBtn.disabled) {{ try {{ sBtn.click(); }} catch(e) {{}} }}
  return {{ ok: true, text: TEXT, host_tag: mi.tagName.toLowerCase(), inner_tag: tag, used_shadow: mi !== target }};
}}
return go();
"""


def js_history(selectors: dict, limit: int) -> str:
    return f"""
const sel = {json.dumps(selectors, ensure_ascii=False)};
const bubblesAll = document.querySelectorAll(sel.message_bubble);
// LINE renders newest-first in DOM. Take the first `limit` (= newest) and emit
// in chronological order (oldest → newest) for natural reading.
const newestFirst = Array.from(bubblesAll).slice(0, {limit});
const bubbles = newestFirst.reverse();
const out = [];
for (let i = 0; i < bubbles.length; i++) {{
  const b = bubbles[i];
  const author = b.querySelector(sel.message_author);
  const text = b.querySelector(sel.message_text);
  const ts = b.querySelector(sel.message_timestamp);
  const dmc = b.getAttribute('data-message-content');
  const dmcPrefix = b.getAttribute('data-message-content-prefix') || '';
  const direction = b.getAttribute('data-direction') || '';
  // Prefer data-message-content (received). For sent messages it's null — fall back to bubble
  // textContent minus the trailing time/read-marker noise.
  let textValue = dmc;
  if (!textValue) {{
    let raw = (text ? text.textContent : (b.textContent || '')).trim();
    // Strip trailing "오후 2:43" / "AM 11:23" / "12:34" + invisible markers
    raw = raw.replace(/\\s*(오전|오후|AM|PM)?\\s*\\d{{1,2}}:\\d{{2}}\\s*[\\u200b-\\u200f]*\\s*$/u, '').trim();
    textValue = raw;
  }}
  // Author: explicit selector → prefix "HH:MM Name " → null
  let authorVal = null;
  if (author) authorVal = author.textContent.trim();
  else if (dmcPrefix) {{
    const m = dmcPrefix.match(/\\d\\d?:\\d\\d\\s+(.+?)\\s*$/);
    if (m) authorVal = m[1];
  }}
  out.push({{
    index: i,
    author: authorVal,
    direction: direction === 'reverse' ? 'sent' : 'received',
    text: textValue,
    timestamp: ts ? (ts.getAttribute('datetime') || ts.textContent.trim()) : (b.getAttribute('data-timestamp') || null),
  }});
}}
return JSON.stringify({{ count: out.length, messages: out }});
"""


def _js_type_only(selectors: dict, text: str) -> str:
    """Type text into LINE input; do NOT send. Lets React reconcile before Enter dispatch."""
    mi = json.dumps(selectors.get("message_input"), ensure_ascii=False)
    return (
        "(function(){"
        f"var TEXT={json.dumps(text, ensure_ascii=False)};"
        f"var tx=document.querySelector({mi});"
        "if(!tx)return JSON.stringify({ok:false,reason:'no_input'});"
        "var target=tx;"
        "if(tx.shadowRoot){var inner=tx.shadowRoot.querySelector('textarea, input, [contenteditable]');if(inner)target=inner;}"
        "target.focus();"
        "var tag=target.tagName.toLowerCase();"
        "if(tag==='textarea'||tag==='input'){"
        "var proto=tag==='textarea'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
        "Object.getOwnPropertyDescriptor(proto,'value').set.call(target,TEXT);"
        "}else{target.textContent=TEXT;}"
        "target.dispatchEvent(new Event('input',{bubbles:true,composed:true}));"
        "target.dispatchEvent(new InputEvent('input',{bubbles:true,composed:true,inputType:'insertText',data:TEXT}));"
        "if(tx!==target){tx.removeAttribute('data-is-empty');tx.dispatchEvent(new Event('input',{bubbles:true,composed:true}));}"
        "return JSON.stringify({ok:true,host_tag:tx.tagName.toLowerCase(),inner_tag:tag,used_shadow:tx!==target});"
        "})()"
    )


def _js_enter_only(selectors: dict) -> str:
    """Dispatch Enter to send. Fires ONLY on host element to avoid duplicate sends —
    LINE binds the send handler such that both host and inner dispatching can each
    trigger a separate send."""
    mi = json.dumps(selectors.get("message_input"), ensure_ascii=False)
    return (
        "(function(){"
        f"var tx=document.querySelector({mi});"
        "if(!tx)return JSON.stringify({ok:false,reason:'no_input'});"
        "var target=tx;"
        "if(tx.shadowRoot){var inner=tx.shadowRoot.querySelector('textarea, input, [contenteditable]');if(inner)target=inner;}"
        "target.focus();"
        "var init={key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true,composed:true};"
        "tx.dispatchEvent(new KeyboardEvent('keydown',init));"
        "tx.dispatchEvent(new KeyboardEvent('keypress',init));"
        "tx.dispatchEvent(new KeyboardEvent('keyup',init));"
        "return JSON.stringify({ok:true});"
        "})()"
    )


def _js_send_payload(selectors: dict, text: str) -> str:
    """Combined type+Enter for legacy callers (kept for compatibility)."""
    mi = json.dumps(selectors.get("message_input"), ensure_ascii=False)
    return (
        "(function(){"
        f"var TEXT={json.dumps(text, ensure_ascii=False)};"
        f"var tx=document.querySelector({mi});"
        "if(!tx)return JSON.stringify({ok:false,reason:'no_input'});"
        "var target=tx;"
        "if(tx.shadowRoot){var inner=tx.shadowRoot.querySelector('textarea, input, [contenteditable]');if(inner)target=inner;}"
        "target.focus();"
        "var tag=target.tagName.toLowerCase();"
        "if(tag==='textarea'||tag==='input'){"
        "var proto=tag==='textarea'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
        "Object.getOwnPropertyDescriptor(proto,'value').set.call(target,TEXT);"
        "}else{target.textContent=TEXT;}"
        "target.dispatchEvent(new Event('input',{bubbles:true,composed:true}));"
        "target.dispatchEvent(new InputEvent('input',{bubbles:true,composed:true,inputType:'insertText',data:TEXT}));"
        "if(tx!==target){tx.removeAttribute('data-is-empty');tx.dispatchEvent(new Event('input',{bubbles:true,composed:true}));}"
        "var init={key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true,composed:true};"
        "tx.dispatchEvent(new KeyboardEvent('keydown',init));"
        "tx.dispatchEvent(new KeyboardEvent('keypress',init));"
        "tx.dispatchEvent(new KeyboardEvent('keyup',init));"
        "if(tx!==target){"
        "target.dispatchEvent(new KeyboardEvent('keydown',init));"
        "target.dispatchEvent(new KeyboardEvent('keypress',init));"
        "target.dispatchEvent(new KeyboardEvent('keyup',init));"
        "}"
        "return JSON.stringify({ok:true,host_tag:tx.tagName.toLowerCase(),inner_tag:tag,used_shadow:tx!==target});"
        "})()"
    )


def _js_check_state(selectors: dict, text: str) -> str:
    """Single-expression JS: returns header + input value + bubble count.
    `cleared` means input is empty AND bubble count is the latest snapshot."""
    hdr = json.dumps(selectors.get("chat_room_header"), ensure_ascii=False)
    mi = json.dumps(selectors.get("message_input"), ensure_ascii=False)
    bubble = json.dumps(selectors.get("message_bubble"), ensure_ascii=False)
    return (
        "(function(){"
        f"var TEXT={json.dumps(text, ensure_ascii=False)};"
        f"var h=document.querySelector({hdr});"
        "var header=h?(h.textContent||'').trim():'';"
        f"var tx=document.querySelector({mi});"
        "var val='';"
        "if(tx){"
        "if(tx.shadowRoot){var inner=tx.shadowRoot.querySelector('textarea, input, [contenteditable]');if(inner)val=inner.value!==undefined?inner.value:(inner.textContent||'');}"
        "if(!val)val=tx.textContent||'';"
        "}"
        f"var bc=document.querySelectorAll({bubble}).length;"
        "return JSON.stringify({header:header,input:val,cleared:val==='',hasInput:!!tx,bubbleCount:bc});"
        "})()"
    )


def _try_combined_send(sel: dict, loc: dict, room_name: str, text: str, q: str) -> dict:
    """COLD path optimized: set search → click → type → enter → verify, all in one osascript.
    Saves a subprocess vs separate _navigate_to_room + _try_fast_send."""
    win_id = loc.get("window_id")
    tab_id = loc.get("tab_id")

    def esc(js):
        return js.replace("\\", "\\\\").replace('"', '\\"')

    def wrap(js):
        wrapped = (
            "(function(){try{var r=(function(){"
            + js
            + "})();return (typeof r==='string')?r:JSON.stringify(r);}"
            "catch(e){return JSON.stringify({error:String(e&&e.message||e)});}})()"
        )
        return wrapped.replace("\\", "\\\\").replace('"', '\\"')

    cli_sel = json.dumps(sel.get("chat_list_item"), ensure_ascii=False)
    name_sel = json.dumps(sel.get("chat_list_item_name"), ensure_ascii=False)
    state_js = (
        "(function(){"
        f"var items=document.querySelectorAll({cli_sel});"
        f"var ROOM={json.dumps(room_name, ensure_ascii=False)};"
        "var matchCount=0;"
        "for(var i=0;i<items.length;i++){"
        f"var n=(items[i].querySelector({name_sel})?.textContent||'').trim();"
        "if(n===ROOM||n.includes(ROOM)||ROOM.includes(n))matchCount++;"
        "}"
        "return JSON.stringify({matchCount:matchCount,itemCount:items.length});"
        "})()"
    )

    set_esc = wrap(js_set_search(sel, q))
    click_esc = wrap(js_click_room_in_list(sel, room_name))
    filter_esc = esc(state_js)
    header_esc = esc(_js_check_state(sel, text))
    type_esc = esc(_js_type_only(sel, text))
    enter_esc = esc(_js_enter_only(sel))
    state_esc = esc(_js_check_state(sel, text))

    script = (
        f'set jsSet to "{set_esc}"\n'
        f'set jsClick to "{click_esc}"\n'
        f'set jsFilter to "{filter_esc}"\n'
        f'set jsHeader to "{header_esc}"\n'
        f'set jsType to "{type_esc}"\n'
        f'set jsEnter to "{enter_esc}"\n'
        f'set jsState to "{state_esc}"\n'
        f'tell application "Google Chrome"\n'
        f'  set targetTab to missing value\n'
        f'  repeat with w in windows\n'
        f'    if (id of w as integer) is {win_id} then\n'
        f'      repeat with t in tabs of w\n'
        f'        if (id of t as integer) is {tab_id} then\n'
        f'          set targetTab to t\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'      end repeat\n'
        f'    end if\n'
        f'    if targetTab is not missing value then exit repeat\n'
        f'  end repeat\n'
        f'  if targetTab is missing value then\n'
        f'    repeat with w in windows\n'
        f'      repeat with t in tabs of w\n'
        f'        if URL of t starts with "{EXTENSION_PREFIX}" and URL of t contains "/index.html" then\n'
        f'          set targetTab to t\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'      end repeat\n'
        f'      if targetTab is not missing value then exit repeat\n'
        f'    end repeat\n'
        f'  end if\n'
        f'  if targetTab is missing value then return ""\n'
        f'  execute targetTab javascript jsSet\n'
        f'  set filterReady to false\n'
        f'  repeat 30 times\n'
        f'    delay 0.05\n'
        f'    set fs to execute targetTab javascript jsFilter\n'
        f'    if fs does not contain "\\"matchCount\\":0" then\n'
        f'      set filterReady to true\n'
        f'      exit repeat\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  if not filterReady then return "FILTER_TIMEOUT"\n'
        f'  set clickResult to execute targetTab javascript jsClick\n'
        f'  -- Poll: wait for chatroom header AND fresh empty editor before typing,\n'
        f'  -- so we don\'t type into the previous chat\'s editor mid-swap.\n'
        f'  set headerJson to ""\n'
        f'  set headerOk to false\n'
        f'  repeat 40 times\n'
        f'    delay 0.05\n'
        f'    set headerJson to execute targetTab javascript jsHeader\n'
        f'    if headerJson contains "{room_name.replace(chr(92), chr(92)+chr(92)).replace(chr(34), chr(92)+chr(34))}" then\n'
        f'      if headerJson contains "\\"input\\":\\"\\"" then\n'
        f'        set headerOk to true\n'
        f'        exit repeat\n'
        f'      end if\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  if not headerOk then return clickResult & "@@C@@" & headerJson & "@@H@@{{}}@@T@@{{}}@@S@@{{}}"\n'
        f'  -- Additional safety settle for editor mount to fully complete\n'
        f'  delay 0.1\n'
        f'  set typeJson to execute targetTab javascript jsType\n'
        f'  delay 0.05\n'
        f'  set sendJson to execute targetTab javascript jsEnter\n'
        f'  set finalState to ""\n'
        f'  set sentOk to false\n'
        f'  repeat 20 times\n'
        f'    delay 0.05\n'
        f'    set s to execute targetTab javascript jsState\n'
        f'    set finalState to s\n'
        f'    if s contains "\\"cleared\\":true" then\n'
        f'      set sentOk to true\n'
        f'      exit repeat\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  if not sentOk then\n'
        f'    execute targetTab javascript jsEnter\n'
        f'    repeat 16 times\n'
        f'      delay 0.05\n'
        f'      set s to execute targetTab javascript jsState\n'
        f'      set finalState to s\n'
        f'      if s contains "\\"cleared\\":true" then exit repeat\n'
        f'    end repeat\n'
        f'  end if\n'
        f'  return clickResult & "@@C@@" & headerJson & "@@H@@" & typeJson & "@@T@@" & sendJson & "@@S@@" & finalState\n'
        f'end tell'
    )
    try:
        raw = _osascript(script, timeout=15)
    except SkillError as e:
        return {"ok": False, "reason": "osascript_error", "error": str(e)}
    if raw == "FILTER_TIMEOUT":
        return {"ok": False, "reason": "filter_timeout"}
    if not raw or "@@C@@" not in raw:
        return {"ok": False, "reason": "bad_response", "raw": (raw or "")[:200]}
    click_part, rest = raw.split("@@C@@", 1)
    header_part, rest2 = rest.split("@@H@@", 1)
    type_part, rest3 = rest2.split("@@T@@", 1)
    send_part, state_part = rest3.split("@@S@@", 1)
    try:
        click_info = json.loads(click_part)
    except Exception:
        click_info = {"ok": False}
    if not click_info.get("ok"):
        return {"ok": False, "reason": "click_failed", "detail": click_info}
    try:
        header_info = json.loads(header_part) if header_part else {}
    except Exception:
        header_info = {}
    actual_header = header_info.get("header", "")
    # Header may not have updated yet — accept if it matches OR is empty (chat still loading)
    if actual_header and not (
        actual_header == room_name or room_name in actual_header or actual_header in room_name
    ):
        return {"ok": False, "reason": "wrong_room", "header": actual_header,
                "matched": click_info.get("matched")}
    try:
        type_info = json.loads(type_part) if type_part else {}
    except Exception:
        type_info = {}
    try:
        send_info = json.loads(send_part) if send_part else {}
    except Exception:
        send_info = {}
    try:
        state_info = json.loads(state_part) if state_part else {}
    except Exception:
        state_info = {}
    initial_bubbles = header_info.get("bubbleCount", 0)
    final_bubbles = state_info.get("bubbleCount", 0)
    sent = state_info.get("cleared") or (final_bubbles > initial_bubbles)
    if sent:
        return {"ok": True, "text": text, "header": actual_header or state_info.get("header", ""),
                "host_tag": type_info.get("host_tag"), "inner_tag": type_info.get("inner_tag"),
                "used_shadow": type_info.get("used_shadow"),
                "verified_by": "input_clear" if state_info.get("cleared") else "bubble_added",
                "matched": click_info.get("matched")}
    return {"ok": False, "reason": "not_confirmed", "input": state_info.get("input"),
            "header": actual_header or state_info.get("header", ""),
            "matched": click_info.get("matched")}


def _try_fast_send(sel: dict, loc: dict, room_name: str, text: str) -> dict:
    """Hot path: header check + send + poll for input clear, all in one osascript.
    Returns {ok:True, ...} only if header matched AND send was confirmed (input cleared).
    Otherwise {ok:False, reason:...} so caller falls back to cold path."""
    win_id = loc.get("window_id")
    tab_id = loc.get("tab_id")
    if win_id is None or tab_id is None:
        return {"ok": False, "reason": "no_loc"}

    def esc(js):
        return js.replace("\\", "\\\\").replace('"', '\\"')

    header_check = esc(_js_check_state(sel, text))
    type_js = esc(_js_type_only(sel, text))
    enter_js = esc(_js_enter_only(sel))
    state_check = esc(_js_check_state(sel, text))

    # Single osascript: header → type → settle delay → Enter → poll for clear / bubble.
    # Splitting type and Enter (with a 100ms settle) lets React reconcile state so
    # rapid consecutive sends don't get dropped.
    script = (
        f'set jsCheck to "{header_check}"\n'
        f'set jsType to "{type_js}"\n'
        f'set jsEnter to "{enter_js}"\n'
        f'set jsState to "{state_check}"\n'
        f'tell application "Google Chrome"\n'
        f'  set targetTab to missing value\n'
        f'  repeat with w in windows\n'
        f'    if (id of w as integer) is {win_id} then\n'
        f'      repeat with t in tabs of w\n'
        f'        if (id of t as integer) is {tab_id} then\n'
        f'          set targetTab to t\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'      end repeat\n'
        f'    end if\n'
        f'    if targetTab is not missing value then exit repeat\n'
        f'  end repeat\n'
        f'  if targetTab is missing value then\n'
        f'    repeat with w in windows\n'
        f'      repeat with t in tabs of w\n'
        f'        if URL of t starts with "{EXTENSION_PREFIX}" and URL of t contains "/index.html" then\n'
        f'          set targetTab to t\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'      end repeat\n'
        f'      if targetTab is not missing value then exit repeat\n'
        f'    end repeat\n'
        f'  end if\n'
        f'  if targetTab is missing value then return ""\n'
        f'  set headerJson to execute targetTab javascript jsCheck\n'
        f'  -- CRITICAL: validate header IN APPLESCRIPT before typing+sending,\n'
        f'  -- otherwise we send to whatever chat is currently active (the bug we hit before).\n'
        f'  if headerJson does not contain "{room_name.replace(chr(92), chr(92)+chr(92)).replace(chr(34), chr(92)+chr(34))}" then\n'
        f'    return headerJson & "@@H@@WRONG_ROOM@@T@@{{}}@@S@@{{}}"\n'
        f'  end if\n'
        f'  set typeJson to execute targetTab javascript jsType\n'
        f'  delay 0.1\n'
        f'  set sendJson to execute targetTab javascript jsEnter\n'
        f'  set finalState to ""\n'
        f'  set sentOk to false\n'
        f'  repeat 20 times\n'
        f'    delay 0.05\n'
        f'    set s to execute targetTab javascript jsState\n'
        f'    set finalState to s\n'
        f'    if s contains "\\"cleared\\":true" then\n'
        f'      set sentOk to true\n'
        f'      exit repeat\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  -- If still not cleared, retry Enter once more\n'
        f'  if not sentOk then\n'
        f'    execute targetTab javascript jsEnter\n'
        f'    repeat 16 times\n'
        f'      delay 0.05\n'
        f'      set s to execute targetTab javascript jsState\n'
        f'      set finalState to s\n'
        f'      if s contains "\\"cleared\\":true" then exit repeat\n'
        f'    end repeat\n'
        f'  end if\n'
        f'  return headerJson & "@@H@@" & typeJson & "@@T@@" & sendJson & "@@S@@" & finalState\n'
        f'end tell'
    )
    try:
        raw = _osascript(script, timeout=10)
    except SkillError as e:
        return {"ok": False, "reason": "osascript_error", "error": str(e)}
    if not raw or "@@H@@" not in raw or "@@T@@" not in raw or "@@S@@" not in raw:
        return {"ok": False, "reason": "bad_response", "raw": (raw or "")[:200]}
    header_part, rest = raw.split("@@H@@", 1)
    type_part, rest2 = rest.split("@@T@@", 1)
    send_part, state_part = rest2.split("@@S@@", 1)
    # Pre-type header guard fired: skip type+send entirely.
    if type_part == "WRONG_ROOM":
        try:
            header_info = json.loads(header_part)
        except Exception:
            header_info = {}
        return {"ok": False, "reason": "wrong_room",
                "header": header_info.get("header", "")}
    try:
        header_info = json.loads(header_part)
    except Exception:
        return {"ok": False, "reason": "header_parse"}
    actual_header = header_info.get("header", "")
    if not actual_header:
        return {"ok": False, "reason": "no_header"}
    if not (actual_header == room_name or room_name in actual_header or actual_header in room_name):
        return {"ok": False, "reason": "wrong_room", "header": actual_header}
    try:
        type_info = json.loads(type_part) if type_part else {}
    except Exception:
        type_info = {}
    if not type_info.get("ok"):
        return {"ok": False, "reason": "type_failed", "detail": type_info, "header": actual_header}
    try:
        send_info = json.loads(send_part) if send_part else {}
    except Exception:
        send_info = {}
    if not send_info.get("ok"):
        return {"ok": False, "reason": "send_failed", "detail": send_info, "header": actual_header}
    try:
        state_info = json.loads(state_part) if state_part else {}
    except Exception:
        state_info = {}
    initial_bubbles = header_info.get("bubbleCount", 0)
    final_bubbles = state_info.get("bubbleCount", 0)
    sent = state_info.get("cleared") or (final_bubbles > initial_bubbles)
    if sent:
        return {"ok": True, "text": text, "header": actual_header,
                "host_tag": type_info.get("host_tag"), "inner_tag": type_info.get("inner_tag"),
                "used_shadow": type_info.get("used_shadow"),
                "verified_by": "input_clear" if state_info.get("cleared") else "bubble_added"}
    return {"ok": False, "reason": "not_confirmed", "input": state_info.get("input"),
            "header": actual_header, "bubbles": [initial_bubbles, final_bubbles]}


def _send_with_verify(sel: dict, loc: dict, text: str) -> dict:
    """Cold-path send (chat is already open via _navigate_to_room). Polls for input clear
    after Enter, with retry. Returns {ok, text, ...} or {ok:False, reason}."""
    win_id = loc.get("window_id")
    tab_id = loc.get("tab_id")

    def esc(js):
        return js.replace("\\", "\\\\").replace('"', '\\"')

    type_js = esc(_js_type_only(sel, text))
    enter_js = esc(_js_enter_only(sel))
    state_check = esc(_js_check_state(sel, text))

    # Wait for editor mount, then type → 100ms settle → Enter → poll for clear/bubble
    script = (
        f'set jsType to "{type_js}"\n'
        f'set jsEnter to "{enter_js}"\n'
        f'set jsState to "{state_check}"\n'
        f'tell application "Google Chrome"\n'
        f'  set targetTab to missing value\n'
        f'  repeat with w in windows\n'
        f'    if (id of w as integer) is {win_id} then\n'
        f'      repeat with t in tabs of w\n'
        f'        if (id of t as integer) is {tab_id} then\n'
        f'          set targetTab to t\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'      end repeat\n'
        f'    end if\n'
        f'    if targetTab is not missing value then exit repeat\n'
        f'  end repeat\n'
        f'  if targetTab is missing value then\n'
        f'    repeat with w in windows\n'
        f'      repeat with t in tabs of w\n'
        f'        if URL of t starts with "{EXTENSION_PREFIX}" and URL of t contains "/index.html" then\n'
        f'          set targetTab to t\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'      end repeat\n'
        f'      if targetTab is not missing value then exit repeat\n'
        f'    end repeat\n'
        f'  end if\n'
        f'  if targetTab is missing value then return ""\n'
        f'  repeat 20 times\n'
        f'    set sCheck to execute targetTab javascript jsState\n'
        f'    if sCheck contains "\\"hasInput\\":true" then exit repeat\n'
        f'    delay 0.05\n'
        f'  end repeat\n'
        f'  set preSendState to execute targetTab javascript jsState\n'
        f'  set typeJson to execute targetTab javascript jsType\n'
        f'  delay 0.1\n'
        f'  set sendJson to execute targetTab javascript jsEnter\n'
        f'  set finalState to ""\n'
        f'  set sentOk to false\n'
        f'  repeat 20 times\n'
        f'    delay 0.05\n'
        f'    set s to execute targetTab javascript jsState\n'
        f'    set finalState to s\n'
        f'    if s contains "\\"cleared\\":true" then\n'
        f'      set sentOk to true\n'
        f'      exit repeat\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  if not sentOk then\n'
        f'    execute targetTab javascript jsEnter\n'
        f'    repeat 16 times\n'
        f'      delay 0.05\n'
        f'      set s to execute targetTab javascript jsState\n'
        f'      set finalState to s\n'
        f'      if s contains "\\"cleared\\":true" then exit repeat\n'
        f'    end repeat\n'
        f'  end if\n'
        f'  return preSendState & "@@P@@" & typeJson & "@@T@@" & sendJson & "@@S@@" & finalState\n'
        f'end tell'
    )
    try:
        raw = _osascript(script, timeout=15)
    except SkillError as e:
        return {"ok": False, "reason": "osascript_error", "error": str(e)}
    if not raw or "@@P@@" not in raw or "@@T@@" not in raw or "@@S@@" not in raw:
        return {"ok": False, "reason": "bad_response", "raw": (raw or "")[:200]}
    pre_part, rest = raw.split("@@P@@", 1)
    type_part, rest2 = rest.split("@@T@@", 1)
    send_part, state_part = rest2.split("@@S@@", 1)
    try:
        pre_info = json.loads(pre_part) if pre_part else {}
    except Exception:
        pre_info = {}
    try:
        type_info = json.loads(type_part) if type_part else {}
    except Exception:
        type_info = {}
    try:
        send_info = json.loads(send_part) if send_part else {}
    except Exception:
        send_info = {"ok": False}
    try:
        state_info = json.loads(state_part) if state_part else {}
    except Exception:
        state_info = {}
    if not type_info.get("ok"):
        return {"ok": False, "reason": "type_failed", "detail": type_info}
    if not send_info.get("ok"):
        return {"ok": False, "reason": "send_failed", "detail": send_info}
    initial_bubbles = pre_info.get("bubbleCount", 0)
    final_bubbles = state_info.get("bubbleCount", 0)
    sent = state_info.get("cleared") or (final_bubbles > initial_bubbles)
    if sent:
        return {"ok": True, "text": text, "host_tag": type_info.get("host_tag"),
                "inner_tag": type_info.get("inner_tag"), "used_shadow": type_info.get("used_shadow"),
                "verified_by": "input_clear" if state_info.get("cleared") else "bubble_added"}
    return {"ok": False, "reason": "not_confirmed", "input": state_info.get("input"),
            "text": text, "bubbles": [initial_bubbles, final_bubbles]}


# ── Subcommands ──────────────────────────────────────────────────────────────

def _probe_applescript_js() -> dict:
    """Run a trivial JS via Chrome AppleScript. Returns one of:
      {state: 'on'}              — JS executed successfully
      {state: 'off'}             — Chrome refused with the AppleScript-JS-disabled error
      {state: 'no_chrome'}       — Chrome not running or no window/tab
      {state: 'error', msg: ...} — other AppleScript error
    """
    script = (
        'tell application "Google Chrome"\n'
        '  if (count of windows) is 0 then return "NO_WIN"\n'
        '  if (count of tabs of window 1) is 0 then return "NO_TAB"\n'
        '  try\n'
        '    return execute (tab 1 of window 1) javascript "42"\n'
        '  on error errMsg\n'
        '    return "ERR:" & errMsg\n'
        '  end try\n'
        'end tell'
    )
    try:
        out = _osascript(script, timeout=5)
    except SkillError as e:
        return {"state": "error", "msg": str(e)}
    if out in ("NO_WIN", "NO_TAB"):
        return {"state": "no_chrome", "msg": out}
    if out == "42":
        return {"state": "on"}
    if out.startswith("ERR:"):
        msg = out[4:]
        if "turned off" in msg or "AppleScript" in msg or "Apple Events" in msg:
            return {"state": "off", "msg": msg}
        return {"state": "error", "msg": msg}
    return {"state": "error", "msg": f"unexpected: {out}"}


def cmd_enable_applescript(args, sel, sources):
    """Ensure Chrome's 'Allow JavaScript from Apple Events' is ON.
    Probes first; only clicks the menu if currently OFF, so it never accidentally toggles OFF."""
    pre = _probe_applescript_js()
    if pre["state"] == "on":
        return {"ok": True, "already_on": True, "action": "none"}
    if pre["state"] == "no_chrome":
        return {"ok": False, "stage": "probe", "reason": pre["msg"],
                "hint": "Chrome을 먼저 실행해주세요."}
    if pre["state"] == "error":
        return {"ok": False, "stage": "probe", "reason": pre["msg"]}

    # state == "off": walk the menu tree click-by-click with delays.
    # The single-line `click menu item X of menu 1 of menu item Y of ...` form
    # only opens the parent menus without actually firing the leaf click — we have
    # to click each level individually with a short settle delay in between.
    # Click is safe only because we already confirmed it is OFF (toggle semantics).
    click_script = (
        'tell application "Google Chrome" to activate\n'
        'delay 0.3\n'
        'tell application "System Events"\n'
        '  if not (exists process "Google Chrome") then return "NO_PROC"\n'
        '  tell process "Google Chrome"\n'
        '    set localePairs to {{"보기", "개발자 정보", "Apple Events의 자바스크립트 허용"}, {"View", "Developer", "Allow JavaScript from Apple Events"}}\n'
        '    set didClick to false\n'
        '    repeat with triple in localePairs\n'
        '      set vn to item 1 of triple\n'
        '      set dn to item 2 of triple\n'
        '      set tn to item 3 of triple\n'
        '      try\n'
        '        click menu bar item vn of menu bar 1\n'
        '        delay 0.15\n'
        '        click menu item dn of menu 1 of menu bar item vn of menu bar 1\n'
        '        delay 0.15\n'
        '        click menu item tn of menu 1 of menu item dn of menu 1 of menu bar item vn of menu bar 1\n'
        '        set didClick to true\n'
        '        exit repeat\n'
        '      on error\n'
        '        -- Close any half-opened menu so the next locale attempt is clean\n'
        '        try\n'
        '          key code 53\n'
        '        end try\n'
        '      end try\n'
        '    end repeat\n'
        '    if didClick then return "CLICKED"\n'
        '    return "MENU_NOT_FOUND"\n'
        '  end tell\n'
        'end tell'
    )
    try:
        out = _osascript(click_script, timeout=10)
    except SkillError as e:
        emsg = str(e)
        hint = ("System Events 권한이 없을 수 있습니다. 시스템 설정 > 개인정보 보호 > "
                "손쉬운 사용에서 터미널(또는 호출하는 앱)을 활성화해주세요.")
        return {"ok": False, "stage": "click", "reason": emsg, "hint": hint}
    if out == "NO_PROC":
        return {"ok": False, "stage": "click", "reason": "Google Chrome process not found"}
    if out == "MENU_NOT_FOUND":
        return {"ok": False, "stage": "click", "reason": "menu item not found",
                "hint": "Chrome 메뉴 언어가 한국어/영어가 아닐 수 있습니다."}

    # Verify
    post = _probe_applescript_js()
    if post["state"] == "on":
        return {"ok": True, "already_on": False, "action": "toggled_on"}
    return {"ok": False, "stage": "verify", "reason": post.get("msg"),
            "post_state": post["state"]}


def cmd_leave_group(args, sel, sources):
    """Leave a LINE group. Destructive and irreversible — requires --confirm.
    Navigates to the room, verifies the chatroom header matches the requested
    name, then drives the header menu → Leave → confirmation modal."""
    if not args.room:
        raise SkillError("--room is required")
    if not args.confirm:
        return {
            "ok": False,
            "reason": "confirmation_required",
            "room": args.room,
            "warning": (
                "Leaving a group is permanent. Once you leave you cannot rejoin on "
                "your own — a current member must invite you back."
            ),
            "next_step": (
                "Show this warning to the user and get an explicit yes "
                "(e.g. \"그룹을 나가면 다시 참여할 수 없습니다. "
                "진행하시겠습니까?\"), then re-run with --confirm."
            ),
        }

    loc = _require_tab()
    nav = _navigate_to_room(sel, loc, args.room)
    if not nav.get("ok"):
        return {"ok": False, "stage": "navigate", "reason": nav}

    win_id = loc.get("window_id")
    tab_id = loc.get("tab_id")

    def esc(js):
        return js.replace("\\", "\\\\").replace('"', '\\"')

    hdr_sel = json.dumps(sel.get("chat_room_header"), ensure_ascii=False)
    js_header = (
        f"(function(){{var h=document.querySelector({hdr_sel});"
        "return h?(h.textContent||'').trim():'';})()"
    )
    # Open the header "more" menu. Idempotent: clicking the button toggles the
    # popover, so we only click when it is not already open — never closing one.
    # The popover renders asynchronously; the caller polls js_pop_check afterwards.
    js_more = (
        "(function(){"
        "var h=document.querySelector('[class*=\"chatroomHeader-module\"]');"
        "if(!h)return JSON.stringify({ok:false,reason:'no_header'});"
        "var b=h.querySelector('button[class*=\"button_more\"]');"
        "if(!b)return JSON.stringify({ok:false,reason:'no_more_button'});"
        "var pop=document.querySelector('[class*=\"actionPopoverLayout-module__popover_wrap__\"]');"
        "if(pop)return JSON.stringify({ok:true,already_open:true});"
        "b.click();return JSON.stringify({ok:true,clicked:true});"
        "})()"
    )
    js_pop_check = (
        "(function(){"
        "var p=document.querySelector('[class*=\"actionPopoverLayout-module__popover_wrap__\"]');"
        "return JSON.stringify({present:!!p});"
        "})()"
    )
    # Click the "Leave" item inside the popover. Match by keyword substring so it
    # works across LINE UI languages: "Leave" / "그룹 나가기" / "グループを退会".
    js_leave = (
        "(function(){"
        "var pop=document.querySelector('[class*=\"actionPopoverLayout-module__popover_wrap__\"]');"
        "if(!pop)return JSON.stringify({ok:false,reason:'no_popover'});"
        "var btns=pop.querySelectorAll('button[class*=\"button_action\"]');"
        "var kws=['Leave','나가기','退会','退出'];"
        "var lb=null;"
        "for(var i=0;i<btns.length;i++){var bt=(btns[i].textContent||'').trim();"
        "for(var k=0;k<kws.length;k++){if(bt.indexOf(kws[k])>=0){lb=btns[i];break;}}"
        "if(lb)break;}"
        "if(!lb)return JSON.stringify({ok:false,reason:'no_leave_item',"
        "items:Array.from(btns).map(function(b){return (b.textContent||'').trim();})});"
        "lb.click();return JSON.stringify({ok:true});"
        "})()"
    )
    js_modal = (
        "(function(){"
        "var m=document.querySelector('[class*=\"alertModal-module__modal__\"]');"
        "if(!m)return JSON.stringify({present:false});"
        "return JSON.stringify({present:true,text:(m.textContent||'').trim()});"
        "})()"
    )
    js_confirm = (
        "(function(){"
        "var m=document.querySelector('[class*=\"alertModal-module__modal__\"]');"
        "if(!m)return JSON.stringify({ok:false,reason:'no_modal'});"
        "var b=m.querySelector('button[class*=\"alertModal-module__button_confirm\"]');"
        "if(!b)return JSON.stringify({ok:false,reason:'no_confirm_button'});"
        "b.click();return JSON.stringify({ok:true});"
        "})()"
    )
    js_final = (
        f"(function(){{var m=document.querySelector('[class*=\"alertModal-module__modal__\"]');"
        f"var h=document.querySelector({hdr_sel});"
        "return JSON.stringify({modalPresent:!!m,header:h?(h.textContent||'').trim():''});})()"
    )

    room_lit = args.room.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'set jsHeader to "{esc(js_header)}"\n'
        f'set jsMore to "{esc(js_more)}"\n'
        f'set jsPopCheck to "{esc(js_pop_check)}"\n'
        f'set jsLeave to "{esc(js_leave)}"\n'
        f'set jsModal to "{esc(js_modal)}"\n'
        f'set jsConfirm to "{esc(js_confirm)}"\n'
        f'set jsFinal to "{esc(js_final)}"\n'
        f'tell application "Google Chrome"\n'
        f'  set targetTab to missing value\n'
        f'  repeat with w in windows\n'
        f'    if (id of w as integer) is {win_id} then\n'
        f'      repeat with t in tabs of w\n'
        f'        if (id of t as integer) is {tab_id} then\n'
        f'          set targetTab to t\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'      end repeat\n'
        f'    end if\n'
        f'    if targetTab is not missing value then exit repeat\n'
        f'  end repeat\n'
        f'  if targetTab is missing value then return "ABORT_NO_TAB"\n'
        f'  -- Poll for the chatroom header to settle on the target group.\n'
        f'  set headerText to ""\n'
        f'  set matched to false\n'
        f'  repeat 30 times\n'
        f'    delay 0.1\n'
        f'    set headerText to execute targetTab javascript jsHeader\n'
        f'    if headerText is not "" then\n'
        f'      if (headerText contains "{room_lit}") or ("{room_lit}" contains headerText) then\n'
        f'        set matched to true\n'
        f'        exit repeat\n'
        f'      end if\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  if not matched then return "ABORT_WRONG_ROOM:" & headerText\n'
        f'  set moreJson to execute targetTab javascript jsMore\n'
        f'  -- The popover renders asynchronously; poll for it, re-clicking once if needed.\n'
        f'  set popReady to false\n'
        f'  repeat 20 times\n'
        f'    delay 0.15\n'
        f'    if (execute targetTab javascript jsPopCheck) contains "\\"present\\":true" then\n'
        f'      set popReady to true\n'
        f'      exit repeat\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  if not popReady then\n'
        f'    execute targetTab javascript jsMore\n'
        f'    repeat 20 times\n'
        f'      delay 0.15\n'
        f'      if (execute targetTab javascript jsPopCheck) contains "\\"present\\":true" then\n'
        f'        set popReady to true\n'
        f'        exit repeat\n'
        f'      end if\n'
        f'    end repeat\n'
        f'  end if\n'
        f'  if not popReady then return "ABORT_NO_POPOVER:" & moreJson\n'
        f'  set leaveJson to execute targetTab javascript jsLeave\n'
        f'  delay 0.6\n'
        f'  set modalJson to execute targetTab javascript jsModal\n'
        f'  -- Only confirm if a leave-confirmation modal is actually showing.\n'
        f'  if modalJson does not contain "\\"present\\":true" then return "ABORT_NO_MODAL:" & modalJson\n'
        f'  if (modalJson does not contain "eave") and (modalJson does not contain "退") and (modalJson does not contain "나") then return "ABORT_MODAL_MISMATCH:" & modalJson\n'
        f'  set confirmJson to execute targetTab javascript jsConfirm\n'
        f'  delay 1.0\n'
        f'  set finalJson to execute targetTab javascript jsFinal\n'
        f'  return headerText & "@@1@@" & moreJson & "@@2@@" & leaveJson & "@@3@@" & modalJson & "@@4@@" & confirmJson & "@@5@@" & finalJson\n'
        f'end tell'
    )
    try:
        raw = _osascript(script, timeout=20)
    except SkillError as e:
        return {"ok": False, "stage": "osascript", "reason": str(e)}

    if raw == "ABORT_NO_TAB":
        return {"ok": False, "stage": "locate_tab", "reason": "extension tab not found"}
    if raw.startswith("ABORT_WRONG_ROOM:"):
        return {"ok": False, "stage": "verify_room", "reason": "chatroom header did not match",
                "requested": args.room, "header": raw[len("ABORT_WRONG_ROOM:"):]}
    if raw.startswith("ABORT_NO_POPOVER:"):
        return {"ok": False, "stage": "open_menu", "reason": "header menu popover did not appear",
                "detail": raw[len("ABORT_NO_POPOVER:"):]}
    if raw.startswith("ABORT_NO_MODAL:"):
        return {"ok": False, "stage": "confirm_modal", "reason": "leave confirmation modal did not appear",
                "detail": raw[len("ABORT_NO_MODAL:"):]}
    if raw.startswith("ABORT_MODAL_MISMATCH:"):
        return {"ok": False, "stage": "confirm_modal", "reason": "modal text was not a group-leave confirmation",
                "detail": raw[len("ABORT_MODAL_MISMATCH:"):]}
    if "@@1@@" not in raw:
        return {"ok": False, "stage": "unknown", "raw": raw[:300]}

    header_part, rest = raw.split("@@1@@", 1)
    more_part, rest = rest.split("@@2@@", 1)
    leave_part, rest = rest.split("@@3@@", 1)
    modal_part, rest = rest.split("@@4@@", 1)
    confirm_part, final_part = rest.split("@@5@@", 1)

    def _j(s):
        try:
            return json.loads(s)
        except Exception:
            return {}

    more_info, leave_info = _j(more_part), _j(leave_part)
    confirm_info, final_info = _j(confirm_part), _j(final_part)
    if not more_info.get("ok"):
        return {"ok": False, "stage": "open_menu", "detail": more_info, "header": header_part}
    if not leave_info.get("ok"):
        return {"ok": False, "stage": "click_leave", "detail": leave_info, "header": header_part}
    if not confirm_info.get("ok"):
        return {"ok": False, "stage": "confirm", "detail": confirm_info, "header": header_part}

    left = not final_info.get("modalPresent") and final_info.get("header", "") != header_part
    return {"ok": bool(left), "room": args.room, "header_before": header_part,
            "header_after": final_info.get("header", ""),
            "verified": left,
            "note": None if left else "confirm was clicked but the chat still shows the group; verify manually"}


def cmd_status(args, sel, sources):
    info = {
        "chrome_running": chrome_running(),
        "extension_window_found": False,
        "extension_window_url": None,
        "selectors_loaded": len(sel),
        "selector_sources": sources,
    }
    if info["chrome_running"]:
        loc = find_extension_tab()
        if loc:
            info["extension_window_found"] = True
            info["extension_window_url"] = loc["url"]
    return info


def cmd_diagnose(args, sel, sources):
    loc = _require_tab()
    raw = exec_js(js_diagnose(sel), loc)
    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise SkillError(f"diagnose returned non-JSON: {raw[:200]}")
    if result.get("broken", 0) > 0:
        result["hint"] = (
            "Open LINE extension > DevTools > inspect the broken element > override "
            "via --selector key=value or ~/.config/line-chrome/selectors.json"
        )
    return result


def cmd_list_rooms(args, sel, sources):
    loc = _require_tab()
    raw = exec_js(js_list_rooms(sel, args.limit), loc)
    return json.loads(raw)


def cmd_list_contacts(args, sel, sources):
    # LINE doesn't separate "contacts" from rooms in the UI cleanly; alias to list-rooms
    return cmd_list_rooms(args, sel, sources)


def cmd_send(args, sel, sources):
    if not args.to or not args.text:
        raise SkillError("--to and --text are required")
    loc = _require_tab()
    t0 = time.time()

    # FAST PATH: read header + send + poll for clear, all in one osascript.
    # If we're already on the target chat, this completes in 150-500ms.
    fast = _try_fast_send(sel, loc, args.to, args.text)
    if fast.get("ok"):
        fast["room"] = args.to
        fast["duration_ms"] = int((time.time() - t0) * 1000)
        fast["path"] = "hot"
        return fast

    # COLD PATH: combined nav+send in one osascript (fastest), with fallback chain.
    # Try the room name as search query first, then shorter variants for tricky names.
    import re as _re
    safe_q = _re.sub(r"[()\[\]{}/\\]", "", args.to).strip()
    first_word = _re.split(r"[\s()\[\]{}/\\]", args.to, 1)[0].strip()
    candidate_queries = [args.to]
    for cand in (safe_q, first_word, args.to[:3]):
        if cand and cand not in candidate_queries:
            candidate_queries.append(cand)

    last_failure = None
    for q in candidate_queries:
        combined = _try_combined_send(sel, loc, args.to, args.text, q)
        if combined.get("ok"):
            combined["room"] = combined.get("matched") or args.to
            combined["duration_ms"] = int((time.time() - t0) * 1000)
            combined["path"] = "cold"
            try:
                exec_js(js_set_search(sel, ""), loc, timeout=5)
            except Exception:
                pass
            return combined
        last_failure = combined
        # Don't try more queries if click succeeded but send didn't (room is right)
        if combined.get("reason") in ("not_confirmed", "wrong_room"):
            break

    # Combined fast path failed. Fall back to deep nav + verify-with-mount-poll.
    nav = _navigate_to_room(sel, loc, args.to)
    if not nav.get("ok"):
        return {"ok": False, "stage": "navigate", "reason": nav,
                "duration_ms": int((time.time() - t0) * 1000),
                "combined_failure": last_failure}
    send_result = _send_with_verify(sel, loc, args.text)
    send_result["room"] = nav.get("matched")
    send_result["duration_ms"] = int((time.time() - t0) * 1000)
    send_result["path"] = "cold-deep"
    try:
        exec_js(js_set_search(sel, ""), loc, timeout=5)
    except Exception:
        pass
    return send_result


def _js_find_message_contextmenu(target: str) -> str:
    """Find the most recent message bubble whose text contains `target` and open its
    context menu (right-click). Returns ok + the matched text, or a not-found reason."""
    return (
        "(function(){"
        f"var TARGET={json.dumps(target, ensure_ascii=False)};"
        "var msgs=document.querySelectorAll('[class*=\"messageLayout-module__message__\"]');"
        "var hit=null,hitText='',bestTs=-1;"
        "for(var i=0;i<msgs.length;i++){"
        "var tn=msgs[i].querySelector('[class*=\"textMessageContent-module__text__\"]');"
        "if(tn&&(tn.textContent||'').indexOf(TARGET)>=0){"
        "var ts=parseInt(msgs[i].getAttribute('data-timestamp')||'0',10)||0;"
        "if(ts>=bestTs){bestTs=ts;hit=msgs[i];hitText=tn.textContent;}"
        "}"
        "}"
        "if(!hit)return JSON.stringify({ok:false,reason:'message_not_found'});"
        "var cw=hit.querySelector('[class*=\"textMessageContent-module__content_wrap__\"]')"
        "||hit.querySelector('[class*=\"textMessageContent-module__text__\"]');"
        "if(!cw)return JSON.stringify({ok:false,reason:'no_content_wrap'});"
        "var r=cw.getBoundingClientRect();"
        "var o={bubbles:true,cancelable:true,composed:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2,button:2};"
        "cw.dispatchEvent(new MouseEvent('mouseover',o));"
        "cw.dispatchEvent(new MouseEvent('mousemove',o));"
        "cw.dispatchEvent(new MouseEvent('contextmenu',o));"
        "return JSON.stringify({ok:true,matched:hitText.slice(0,80)});"
        "})()"
    )


def _js_context_menu_present() -> str:
    """Report whether the message context menu is currently rendered."""
    return (
        "(function(){return JSON.stringify({present:!!("
        "document.querySelector('[class*=\"actionPopoverList-module__action_list__\"]')"
        "||document.querySelector('[class*=\"actionPopoverLayout-module__popover_wrap__\"]'))});})()"
    )


def _js_click_reply_item() -> str:
    """Inside the open context menu, click the Reply item (EN/KO/JA labels)."""
    return (
        "(function(){"
        "var pop=document.querySelector('[class*=\"actionPopoverList-module__action_list__\"]')"
        "||document.querySelector('[class*=\"actionPopoverLayout-module__popover_wrap__\"]');"
        "if(!pop)return JSON.stringify({ok:false,reason:'no_context_menu'});"
        "var btns=pop.querySelectorAll('button[class*=\"actionPopoverListItem-module__button_action__\"]');"
        "var kws=['Reply','답장','返信'];"
        "var rb=null;"
        "for(var i=0;i<btns.length;i++){var t=(btns[i].textContent||'').trim();"
        "for(var k=0;k<kws.length;k++){if(t.indexOf(kws[k])>=0){rb=btns[i];break;}}}"
        "if(!rb)return JSON.stringify({ok:false,reason:'no_reply_item',"
        "items:Array.from(btns).map(function(b){return (b.textContent||'').trim();})});"
        "rb.click();return JSON.stringify({ok:true});"
        "})()"
    )


def _js_reply_state(text: str) -> str:
    """Report reply-compose state: whether the quote banner is present, and whether a
    sent reply bubble containing `text` exists. Used for both pre-type and post-send checks."""
    return (
        "(function(){"
        f"var TEXT={json.dumps(text, ensure_ascii=False)};"
        "var banner=document.querySelector('[class*=\"replyTargetMessage-module__reply_target_message__\"]');"
        "var msgs=document.querySelectorAll('[class*=\"messageLayout-module__message__\"]');"
        "var sent=false;"
        "for(var i=0;i<msgs.length;i++){"
        "var rc=msgs[i].querySelector('[class*=\"replyMessageContent-module__reply_content__\"]');"
        "var c=msgs[i].querySelector('[class*=\"messageLayout-module__content__\"]');"
        "if(rc&&c&&(c.textContent||'').indexOf(TEXT)>=0)sent=true;"
        "}"
        "return JSON.stringify({bannerPresent:!!banner,replySent:sent});"
        "})()"
    )


def _do_reply(sel: dict, loc: dict, target: str, text: str,
              nav_room: str = None, nav_query: str = None) -> dict:
    """Run the full reply sequence in one osascript: (optional navigate) -> contextmenu
    -> Reply -> verify quote banner -> type -> Enter -> poll for the sent reply bubble.
    When nav_room is given, navigation is folded into the same osascript (cold path)."""
    win_id = loc.get("window_id")
    tab_id = loc.get("tab_id")

    def esc(js):
        return js.replace("\\", "\\\\").replace('"', '\\"')

    def wrap(js):
        wrapped = (
            "(function(){try{var r=(function(){" + js
            + "})();return (typeof r==='string')?r:JSON.stringify(r);}"
            "catch(e){return JSON.stringify({error:String(e&&e.message||e)});}})()"
        )
        return wrapped.replace("\\", "\\\\").replace('"', '\\"')

    hdr_sel = json.dumps(sel.get("chat_room_header"), ensure_ascii=False)
    header_js = f"(function(){{var h=document.querySelector({hdr_sel});return h?(h.textContent||'').trim():'';}})()"

    find_esc = esc(_js_find_message_contextmenu(target))
    menu_esc = esc(_js_context_menu_present())
    reply_esc = esc(_js_click_reply_item())
    mode_esc = esc(_js_reply_state(text))
    type_esc = esc(_js_type_only(sel, text))
    enter_esc = esc(_js_enter_only(sel))
    state_esc = esc(_js_reply_state(text))
    header_esc = esc(header_js)

    # Cold path: fold search -> click -> header-settle into the same osascript.
    nav_block = ""
    nav_setup = ""
    if nav_room:
        cli_sel = json.dumps(sel.get("chat_list_item"), ensure_ascii=False)
        name_sel = json.dumps(sel.get("chat_list_item_name"), ensure_ascii=False)
        filter_js = (
            "(function(){"
            f"var items=document.querySelectorAll({cli_sel});"
            f"var ROOM={json.dumps(nav_room, ensure_ascii=False)};var mc=0;"
            "for(var i=0;i<items.length;i++){"
            f"var n=(items[i].querySelector({name_sel})?.textContent||'').trim();"
            "if(n===ROOM||n.indexOf(ROOM)>=0||ROOM.indexOf(n)>=0)mc++;}"
            "return JSON.stringify({matchCount:mc});})()"
        )
        room_lit = nav_room.replace("\\", "\\\\").replace('"', '\\"')
        nav_setup = (
            f'set jsSet to "{wrap(js_set_search(sel, nav_query or nav_room))}"\n'
            f'set jsClick to "{wrap(js_click_room_in_list(sel, nav_room))}"\n'
            f'set jsFilter to "{esc(filter_js)}"\n'
        )
        nav_block = (
            f'  execute targetTab javascript jsSet\n'
            f'  set filterReady to false\n'
            f'  repeat 50 times\n'
            f'    delay 0.03\n'
            f'    if (execute targetTab javascript jsFilter) does not contain "\\"matchCount\\":0" then\n'
            f'      set filterReady to true\n'
            f'      exit repeat\n'
            f'    end if\n'
            f'  end repeat\n'
            f'  if not filterReady then return "ABORT_NAV@@filter_timeout"\n'
            f'  execute targetTab javascript jsClick\n'
            f'  set navOk to false\n'
            f'  repeat 50 times\n'
            f'    delay 0.03\n'
            f'    set hh to execute targetTab javascript jsHeader\n'
            f'    if hh is not "" then\n'
            f'      if (hh contains "{room_lit}") or ("{room_lit}" contains hh) then\n'
            f'        set navOk to true\n'
            f'        exit repeat\n'
            f'      end if\n'
            f'    end if\n'
            f'  end repeat\n'
            f'  if not navOk then return "ABORT_NAV@@header_timeout"\n'
            f'  delay 0.03\n'
        )

    script = (
        f'set jsHeader to "{header_esc}"\n'
        f'set jsFind to "{find_esc}"\n'
        f'set jsMenu to "{menu_esc}"\n'
        f'set jsReply to "{reply_esc}"\n'
        f'set jsMode to "{mode_esc}"\n'
        f'set jsType to "{type_esc}"\n'
        f'set jsEnter to "{enter_esc}"\n'
        f'set jsState to "{state_esc}"\n'
        f'{nav_setup}'
        f'tell application "Google Chrome"\n'
        f'  set targetTab to missing value\n'
        f'  repeat with w in windows\n'
        f'    if (id of w as integer) is {win_id} then\n'
        f'      repeat with t in tabs of w\n'
        f'        if (id of t as integer) is {tab_id} then\n'
        f'          set targetTab to t\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'      end repeat\n'
        f'    end if\n'
        f'    if targetTab is not missing value then exit repeat\n'
        f'  end repeat\n'
        f'  if targetTab is missing value then return "ABORT_NO_TAB"\n'
        f'{nav_block}'
        f'  set findJson to execute targetTab javascript jsFind\n'
        f'  if findJson does not contain "\\"ok\\":true" then return "ABORT_FIND@@" & findJson\n'
        f'  set menuReady to false\n'
        f'  repeat 24 times\n'
        f'    delay 0.03\n'
        f'    if (execute targetTab javascript jsMenu) contains "\\"present\\":true" then\n'
        f'      set menuReady to true\n'
        f'      exit repeat\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  if not menuReady then return "ABORT_REPLY@@{{\\"reason\\":\\"context_menu_did_not_appear\\"}}"\n'
        f'  set replyJson to execute targetTab javascript jsReply\n'
        f'  if replyJson does not contain "\\"ok\\":true" then return "ABORT_REPLY@@" & replyJson\n'
        f'  set modeOk to false\n'
        f'  set modeJson to ""\n'
        f'  repeat 25 times\n'
        f'    delay 0.04\n'
        f'    set modeJson to execute targetTab javascript jsMode\n'
        f'    if modeJson contains "\\"bannerPresent\\":true" then\n'
        f'      set modeOk to true\n'
        f'      exit repeat\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  if not modeOk then return "ABORT_MODE@@" & modeJson\n'
        f'  set typeJson to execute targetTab javascript jsType\n'
        f'  delay 0.03\n'
        f'  set enterJson to execute targetTab javascript jsEnter\n'
        f'  set finalJson to ""\n'
        f'  set sentOk to false\n'
        f'  repeat 25 times\n'
        f'    delay 0.04\n'
        f'    set finalJson to execute targetTab javascript jsState\n'
        f'    if finalJson contains "\\"replySent\\":true" then\n'
        f'      set sentOk to true\n'
        f'      exit repeat\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  return findJson & "@@F@@" & replyJson & "@@R@@" & typeJson & "@@T@@" & enterJson & "@@E@@" & finalJson\n'
        f'end tell'
    )
    try:
        raw = _osascript(script, timeout=20)
    except SkillError as e:
        return {"ok": False, "stage": "osascript", "reason": str(e)}
    if raw == "ABORT_NO_TAB":
        return {"ok": False, "stage": "locate_tab", "reason": "extension tab not found"}
    if raw.startswith("ABORT_NAV@@"):
        return {"ok": False, "stage": "navigate", "reason": raw[len("ABORT_NAV@@"):],
                "room": nav_room}
    if raw.startswith("ABORT_FIND@@"):
        return {"ok": False, "stage": "find_message", "reason": "target message not found",
                "detail": raw[len("ABORT_FIND@@"):], "target": target}
    if raw.startswith("ABORT_REPLY@@"):
        return {"ok": False, "stage": "click_reply", "reason": "Reply menu item not found",
                "detail": raw[len("ABORT_REPLY@@"):]}
    if raw.startswith("ABORT_MODE@@"):
        return {"ok": False, "stage": "reply_mode", "reason": "reply quote banner did not appear",
                "detail": raw[len("ABORT_MODE@@"):]}
    if "@@F@@" not in raw:
        return {"ok": False, "stage": "unknown", "raw": raw[:300]}
    find_part, rest = raw.split("@@F@@", 1)
    reply_part, rest = rest.split("@@R@@", 1)
    type_part, rest = rest.split("@@T@@", 1)
    enter_part, final_part = rest.split("@@E@@", 1)

    def _j(s):
        try:
            return json.loads(s)
        except Exception:
            return {}

    find_info, final_info = _j(find_part), _j(final_part)
    if not _j(type_part).get("ok"):
        return {"ok": False, "stage": "type", "detail": _j(type_part)}
    if final_info.get("replySent"):
        return {"ok": True, "matched": find_info.get("matched"), "text": text,
                "verified_by": "reply_bubble"}
    return {"ok": False, "stage": "not_confirmed", "detail": final_info,
            "matched": find_info.get("matched")}


def cmd_reply(args, sel, sources):
    """Reply to a specific message in a room. --to is a substring of the message being
    replied to; --text is the reply body. Hot path skips navigation when already in the room."""
    if not args.room or not args.to or not args.text:
        raise SkillError("--room, --to and --text are required")
    loc = _require_tab()
    t0 = time.time()

    hdr_sel = json.dumps(sel.get("chat_room_header"), ensure_ascii=False)
    try:
        header = exec_js(f"return (document.querySelector({hdr_sel})||{{}}).textContent||'';",
                         loc, timeout=5).strip()
    except Exception:
        header = ""
    on_room = bool(header) and (header == args.room or args.room in header or header in args.room)

    if on_room:
        # HOT: already in the room — reply directly, no navigation.
        result = _do_reply(sel, loc, args.to, args.text)
        result["path"] = "hot"
    else:
        # COLD: fold navigation into the same osascript as the reply.
        result = _do_reply(sel, loc, args.to, args.text,
                           nav_room=args.room, nav_query=args.room)
        result["path"] = "cold"
        if result.get("ok"):
            try:
                exec_js(js_set_search(sel, ""), loc, timeout=5)
            except Exception:
                pass
    result["room"] = args.room
    result["duration_ms"] = int((time.time() - t0) * 1000)
    return result


# ── send-sticker ─────────────────────────────────────────────────────────────

def _parse_pipe_point(raw: str) -> tuple[bool, dict]:
    """Parse the small pipe protocol returned by sticker coordinate JS.

    Shape:
      OK|x|y
      ERR|reason|detail

    Kept intentionally tiny so AppleScript can split it without needing JSON
    parsing before it performs a trusted OS click.
    """
    parts = (raw or "").split("|", 2)
    if len(parts) >= 3 and parts[0] == "OK":
        try:
            return True, {"x": int(float(parts[1])), "y": int(float(parts[2]))}
        except ValueError:
            return False, {"reason": "bad_coordinates", "raw": raw}
    if len(parts) >= 2 and parts[0] == "ERR":
        return False, {"reason": parts[1], "detail": parts[2] if len(parts) > 2 else ""}
    return False, {"reason": "bad_point_response", "raw": raw}


def _parse_sticker_count(raw: str) -> int:
    try:
        return int(json.loads(raw).get("stickerBubbles", 0))
    except Exception:
        return 0


def _focus_chrome_tab(loc: dict) -> None:
    """Bring the target Chrome window/tab to the front using Chrome AppleScript only."""
    win_id = loc.get("window_id")
    tab_id = loc.get("tab_id")
    if win_id is None or tab_id is None:
        return
    script = (
        f'tell application "Google Chrome"\n'
        f'  repeat with w in windows\n'
        f'    if (id of w as integer) is {int(win_id)} then\n'
        f'      set tabIndex to 1\n'
        f'      repeat with t in tabs of w\n'
        f'        if (id of t as integer) is {int(tab_id)} then\n'
        f'          set active tab index of w to tabIndex\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'        set tabIndex to tabIndex + 1\n'
        f'      end repeat\n'
        f'      set visible of w to true\n'
        f'      set index of w to 1\n'
        f'      activate\n'
        f'      return "ok"\n'
        f'    end if\n'
        f'  end repeat\n'
        f'end tell'
    )
    _osascript(script, timeout=3)


def _find_sticker_popup_tab(timeout: float = 0.7) -> dict:
    """Locate LINE's transient sticker popup tab exactly (no fallback)."""
    deadline = time.time() + timeout
    script = (
        f'tell application "Google Chrome"\n'
        f'  repeat with w in windows\n'
        f'    repeat with t in tabs of w\n'
        f'      try\n'
        f'        set u to URL of t\n'
        f'        if u is "{EXTENSION_PREFIX}popup.html" then\n'
        f'          return (id of w as string) & "|" & (id of t as string) & "|" & u\n'
        f'        end if\n'
        f'      end try\n'
        f'    end repeat\n'
        f'  end repeat\n'
        f'  return ""\n'
        f'end tell'
    )
    while True:
        raw = _osascript(script, timeout=3)
        if raw:
            win, tab, url = raw.split("|", 2)
            return {"window_id": int(win), "tab_id": int(tab), "url": url}
        if time.time() >= deadline:
            return {}
        time.sleep(0.04)


def _exec_sticker_popup_js(js: str, timeout: float = 0.6) -> str:
    """Find LINE's transient sticker popup and execute JS in one AppleScript call."""
    wrapped = (
        "(function(){try{var r=(function(){"
        + js
        + "})();return (typeof r==='string')?r:JSON.stringify(r);}"
        "catch(e){return JSON.stringify({error:String(e&&e.message||e)});}})()"
    )
    js_escaped = wrapped.replace("\\", "\\\\").replace('"', '\\"')
    loops = max(1, int(timeout / 0.04))
    script = (
        f'tell application "Google Chrome"\n'
        f'  set targetTab to missing value\n'
        f'  set targetWindow to missing value\n'
        f'  repeat {loops} times\n'
        f'    repeat with w in windows\n'
        f'      repeat with t in tabs of w\n'
        f'        try\n'
        f'          if URL of t is "{EXTENSION_PREFIX}popup.html" then\n'
        f'            set targetTab to t\n'
        f'            set targetWindow to w\n'
        f'            exit repeat\n'
        f'          end if\n'
        f'        end try\n'
        f'      end repeat\n'
        f'      if targetTab is not missing value then exit repeat\n'
        f'    end repeat\n'
        f'    if targetTab is not missing value then exit repeat\n'
        f'    delay 0.04\n'
        f'  end repeat\n'
        f'  if targetTab is missing value then return ""\n'
        f'  if targetWindow is not missing value then\n'
        f'    set visible of targetWindow to true\n'
        f'    set index of targetWindow to 1\n'
        f'    activate\n'
        f'  end if\n'
        f'  return execute targetTab javascript "{js_escaped}"\n'
        f'end tell'
    )
    return _osascript(script, timeout=max(3, int(timeout) + 2))


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _trusted_click(x: int, y: int) -> dict:
    """Post a real left-click at screen coordinates using CoreGraphics.

    This avoids `tell application "System Events" to click at ...`, which can
    block indefinitely in some Chrome/Accessibility states.
    """
    app_path = ctypes.util.find_library("ApplicationServices")
    if not app_path:
        return {"ok": False, "reason": "application_services_not_found"}
    cf_path = ctypes.util.find_library("CoreFoundation")
    app = ctypes.CDLL(app_path)
    cf = ctypes.CDLL(cf_path) if cf_path else None

    app.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    app.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32
    ]
    app.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    if cf:
        cf.CFRelease.argtypes = [ctypes.c_void_p]

    point = _CGPoint(float(x), float(y))
    # 5 = mouse moved, 1 = left down, 2 = left up, 0 = left button.
    # kCGSessionEventTap (1) reaches Chrome extension windows in normal user sessions;
    # kCGHIDEventTap can move the cursor without delivering mouse down/up to Chrome.
    event_tap = 1

    def post(kind: int) -> bool:
        event = app.CGEventCreateMouseEvent(None, kind, point, 0)
        if not event:
            return False
        app.CGEventPost(event_tap, event)
        if cf:
            cf.CFRelease(event)
        return True

    if not post(5):
        return {"ok": False, "reason": "mouse_move_event_failed", "x": x, "y": y}
    time.sleep(0.015)
    if not post(1):
        return {"ok": False, "reason": "mouse_down_event_failed", "x": x, "y": y}
    time.sleep(0.025)
    if not post(2):
        return {"ok": False, "reason": "mouse_up_event_failed", "x": x, "y": y}
    return {"ok": True, "x": x, "y": y, "tap": "session"}


def _json_get(raw: str) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _normalize_sticker_tag(tag: str) -> str:
    return (tag or "").strip().casefold()


def _load_sticker_tags_doc(path: Path | None = None) -> dict:
    path = path or USER_STICKER_TAGS_PATH
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return {"version": 1, "tags": {}}
    except json.JSONDecodeError as e:
        raise SkillError(f"sticker tags file at {path} has invalid JSON: {e}")
    if not isinstance(doc, dict):
        raise SkillError(f"sticker tags file at {path} must contain a JSON object")
    tags = doc.setdefault("tags", {})
    if not isinstance(tags, dict):
        raise SkillError(f"sticker tags file at {path} must contain an object at `tags`")
    doc.setdefault("version", 1)
    return doc


def _write_sticker_tags_doc(doc: dict, path: Path | None = None) -> None:
    path = path or USER_STICKER_TAGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _coerce_sticker_tag_entry(tag: str, entry: Any) -> dict:
    # Future-proof for list-valued tags; current CLI writes a single object.
    if isinstance(entry, list):
        entry = entry[0] if entry else {}
    if not isinstance(entry, dict):
        raise SkillError(f"sticker tag '{tag}' must map to an object")
    try:
        package_idx = int(entry["package"])
        sticker_idx = int(entry["sticker"])
    except (KeyError, TypeError, ValueError):
        raise SkillError(f"sticker tag '{tag}' requires integer package and sticker values")
    if package_idx < 0 or sticker_idx < 0:
        raise SkillError(f"sticker tag '{tag}' has a negative package/sticker index")
    resolved = {"tag": tag, "package": package_idx, "sticker": sticker_idx}
    if entry.get("label"):
        resolved["label"] = entry.get("label")
    return resolved


def _resolve_sticker_meaning(meaning: str, path: Path | None = None) -> dict | None:
    doc = _load_sticker_tags_doc(path)
    tags = doc.get("tags", {})
    key = (meaning or "").strip()
    if not key:
        return None
    if key in tags:
        return _coerce_sticker_tag_entry(key, tags[key])
    norm = _normalize_sticker_tag(key)
    for tag, entry in tags.items():
        if _normalize_sticker_tag(tag) == norm:
            return _coerce_sticker_tag_entry(tag, entry)
    return None


def _parse_send_sticker_result(raw: str, package_idx: int, sticker_idx: int) -> dict:
    """Map the AppleScript sticker workflow result into the public CLI contract."""
    if raw == "ABORT_NO_TAB":
        return {"ok": False, "stage": "locate_tab", "reason": "extension tab not found"}
    if raw.startswith("ABORT_OPEN@@"):
        ok, detail = _parse_pipe_point(raw[len("ABORT_OPEN@@"):])
        return {"ok": False, "stage": "open_picker",
                "reason": detail.get("reason", "sticker button not found"),
                "detail": detail}
    if raw.startswith("ABORT_TRUSTED_CLICK@@"):
        return {
            "ok": False,
            "stage": "trusted_click",
            "reason": "trusted_input_unavailable",
            "detail": raw[len("ABORT_TRUSTED_CLICK@@"):],
            "hint": (
                "Grant Accessibility permission to the terminal or app running this CLI "
                "so CoreGraphics can perform a trusted click in Chrome."
            ),
        }
    if raw.startswith("ABORT_PICKER@@"):
        return {
            "ok": False,
            "stage": "open_picker",
            "reason": "picker_unavailable",
            "detail": (
                "The sticker picker did not open after a trusted click. Verify the "
                "LINE extension window is visible and the editor is enabled."
            ),
        }
    if raw.startswith("ABORT_STICKER@@"):
        ok, detail = _parse_pipe_point(raw[len("ABORT_STICKER@@"):])
        return {"ok": False, "stage": "select_sticker",
                "reason": detail.get("reason", "sticker_not_selected"), "detail": detail}
    if raw.startswith("ABORT_VERIFY@@"):
        return {"ok": False, "stage": "not_confirmed",
                "reason": "no new sticker bubble appeared",
                "detail": raw[len("ABORT_VERIFY@@"):]}
    if "@@C@@" not in raw or "@@K@@" not in raw:
        return {"ok": False, "stage": "unknown", "raw": (raw or "")[:300]}

    before_part, rest = raw.split("@@C@@", 1)
    click_part, after_part = rest.split("@@K@@", 1)
    before_n = _parse_sticker_count(before_part)
    after_n = _parse_sticker_count(after_part)
    if after_n > before_n:
        return {
            "ok": True,
            "verified_by": "sticker_bubble",
            "package": package_idx,
            "sticker": sticker_idx,
            "trusted_input": "core_graphics",
            "sticker_bubbles": [before_n, after_n],
        }
    return {
        "ok": False,
        "stage": "not_confirmed",
        "reason": "no new sticker bubble appeared",
        "sticker_bubbles": [before_n, after_n],
        "click": click_part,
    }


def _do_send_sticker_osascript_legacy(sel: dict, loc: dict, package_idx: int, sticker_idx: int) -> dict:
    """Open the sticker picker, click the target sticker (sends immediately), and
    verify a sticker bubble appeared. Assumes the target room is already open.

    The sticker picker is opened by a trusted OS-level click through System Events.
    LINE requires user activation here; JS-injected `.click()` is not enough."""
    win_id = loc.get("window_id")
    tab_id = loc.get("tab_id")

    def esc(js):
        return js.replace("\\", "\\\\").replace('"', '\\"')

    js_count = (
        "(function(){var m=document.querySelectorAll('[class*=\"messageLayout-module__message__\"]');"
        "var n=0;for(var i=0;i<m.length;i++){if(m[i].querySelector('[class*=\"stickerMessageContent-module__\"]'))n++;}"
        "return JSON.stringify({stickerBubbles:n});})()"
    )
    js_open_point = (
        "(function(){var ed=document.querySelector('[class*=\"chatroomEditor-module\"]');"
        "if(!ed)return 'ERR|no_editor|';"
        "var bs=ed.querySelectorAll('button');var b=null;"
        "for(var i=0;i<bs.length;i++){var label=((bs[i].getAttribute('aria-label')||'')+' '+(bs[i].getAttribute('title')||'')+' '+(bs[i].textContent||''));"
        "if(/sticker|스티커|スタンプ|貼圖|贴图/i.test(label)){b=bs[i];break;}}"
        "if(!b)return 'ERR|no_sticker_button|';"
        "var r=b.getBoundingClientRect();"
        "if(!r.width||!r.height)return 'ERR|sticker_button_not_visible|';"
        "var x=Math.round(window.screenX+(window.outerWidth-window.innerWidth)/2+r.left+r.width/2);"
        "var y=Math.round(window.screenY+(window.outerHeight-window.innerHeight)+r.top+r.height/2);"
        "return 'OK|'+x+'|'+y;})()"
    )
    js_picker = (
        "(function(){return JSON.stringify({present:"
        "!!document.querySelector('[class*=\"stickerPopup-module__popup__\"]')});})()"
    )
    js_sticker_point = (
        "(function(){"
        f"var PKG={int(package_idx)},STK={int(sticker_idx)};"
        "var pop=document.querySelector('[class*=\"stickerPopup-module__popup__\"]');"
        "if(!pop)return 'ERR|no_popup|';"
        "var pkgs=pop.querySelectorAll('[class*=\"packageItemList-module__package_item_list__\"]');"
        "if(!pkgs.length)return 'ERR|no_packages|';"
        "if(PKG>=pkgs.length)return 'ERR|package_out_of_range|packages='+pkgs.length;"
        "var stks=pkgs[PKG].querySelectorAll('button[class*=\"packageItemList-module__button_item__\"]');"
        "if(!stks.length)return 'ERR|empty_package|';"
        "if(STK>=stks.length)return 'ERR|sticker_out_of_range|stickers='+stks.length;"
        "var b=stks[STK];var r=b.getBoundingClientRect();"
        "if(!r.width||!r.height)return 'ERR|sticker_not_visible|';"
        "var x=Math.round(window.screenX+(window.outerWidth-window.innerWidth)/2+r.left+r.width/2);"
        "var y=Math.round(window.screenY+(window.outerHeight-window.innerHeight)+r.top+r.height/2);"
        "return 'OK|'+x+'|'+y;})()"
    )

    script = (
        f'set jsCount to "{esc(js_count)}"\n'
        f'set jsOpenPoint to "{esc(js_open_point)}"\n'
        f'set jsPicker to "{esc(js_picker)}"\n'
        f'set jsStickerPoint to "{esc(js_sticker_point)}"\n'
        f'tell application "Google Chrome"\n'
        f'  set targetTab to missing value\n'
        f'  repeat with w in windows\n'
        f'    if (id of w as integer) is {win_id} then\n'
        f'      repeat with t in tabs of w\n'
        f'        if (id of t as integer) is {tab_id} then\n'
        f'          set targetTab to t\n'
        f'          exit repeat\n'
        f'        end if\n'
        f'      end repeat\n'
        f'    end if\n'
        f'    if targetTab is not missing value then exit repeat\n'
        f'  end repeat\n'
        f'  if targetTab is missing value then return "ABORT_NO_TAB"\n'
        f'  set countBefore to execute targetTab javascript jsCount\n'
        f'  set pickerReady to false\n'
        f'  set openPoint to ""\n'
        f'  if (execute targetTab javascript jsPicker) contains "\\"present\\":true" then\n'
        f'    set pickerReady to true\n'
        f'  else\n'
        f'    set openPoint to execute targetTab javascript jsOpenPoint\n'
        f'    if openPoint does not start with "OK|" then return "ABORT_OPEN@@" & openPoint\n'
        f'    set oldDelims to AppleScript\'s text item delimiters\n'
        f'    set AppleScript\'s text item delimiters to "|"\n'
        f'    set openParts to text items of openPoint\n'
        f'    set AppleScript\'s text item delimiters to oldDelims\n'
        f'    set openX to item 2 of openParts as integer\n'
        f'    set openY to item 3 of openParts as integer\n'
        f'    tell application "Google Chrome" to activate\n'
        f'    delay 0.03\n'
        f'    try\n'
        f'      tell application "System Events" to click at {{openX, openY}}\n'
        f'    on error errMsg number errNum\n'
        f'      return "ABORT_TRUSTED_CLICK@@" & errMsg & " (" & errNum & ")"\n'
        f'    end try\n'
        f'    repeat 22 times\n'
        f'      delay 0.03\n'
        f'      if (execute targetTab javascript jsPicker) contains "\\"present\\":true" then\n'
        f'        set pickerReady to true\n'
        f'        exit repeat\n'
        f'      end if\n'
        f'    end repeat\n'
        f'  end if\n'
        f'  if not pickerReady then return "ABORT_PICKER@@" & openPoint\n'
        f'  set stickerPoint to ""\n'
        f'  set stickerReady to false\n'
        f'  repeat 18 times\n'
        f'    delay 0.025\n'
        f'    set stickerPoint to execute targetTab javascript jsStickerPoint\n'
        f'    if stickerPoint starts with "OK|" then\n'
        f'      set stickerReady to true\n'
        f'      exit repeat\n'
        f'    end if\n'
        f'    if stickerPoint contains "package_out_of_range" or stickerPoint contains "sticker_out_of_range" or stickerPoint contains "empty_package" then exit repeat\n'
        f'  end repeat\n'
        f'  if not stickerReady then return "ABORT_STICKER@@" & stickerPoint\n'
        f'  set oldDelims to AppleScript\'s text item delimiters\n'
        f'  set AppleScript\'s text item delimiters to "|"\n'
        f'  set stickerParts to text items of stickerPoint\n'
        f'  set AppleScript\'s text item delimiters to oldDelims\n'
        f'  set stickerX to item 2 of stickerParts as integer\n'
        f'  set stickerY to item 3 of stickerParts as integer\n'
        f'  try\n'
        f'    tell application "System Events" to click at {{stickerX, stickerY}}\n'
        f'  on error errMsg number errNum\n'
        f'    return "ABORT_TRUSTED_CLICK@@" & errMsg & " (" & errNum & ")"\n'
        f'  end try\n'
        f'  set countAfter to ""\n'
        f'  set sentOk to false\n'
        f'  repeat 18 times\n'
        f'    delay 0.03\n'
        f'    set countAfter to execute targetTab javascript jsCount\n'
        f'    if countAfter is not countBefore then\n'
        f'      set sentOk to true\n'
        f'      exit repeat\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  if not sentOk then return "ABORT_VERIFY@@" & countBefore & "@@K@@" & countAfter\n'
        f'  return countBefore & "@@C@@" & stickerPoint & "@@K@@" & countAfter\n'
        f'end tell'
    )
    try:
        raw = _osascript(script, timeout=20)
    except SkillError as e:
        return {"ok": False, "stage": "osascript", "reason": str(e)}
    return _parse_send_sticker_result(raw, package_idx, sticker_idx)


def _do_send_sticker(sel: dict, loc: dict, package_idx: int, sticker_idx: int) -> dict:
    """Send a sticker through LINE's real popup using CoreGraphics trusted clicks.

    AppleScript-injected JS runs in an isolated world on Chrome extension pages, so
    synthetic DOM clicks do not reach LINE's React handlers. The reliable path is:
    read coordinates with JS, deliver trusted OS clicks, and verify the main chat.
    """

    js_count_and_open_point = (
        "var n=document.querySelectorAll('[class*=\"stickerMessageContent-module__content_wrap__\"]').length;"
        "var ed=document.querySelector('[class*=\"chatroomEditor-module\"]');"
        "if(!ed)return JSON.stringify({ok:false,reason:'no_editor',stickerBubbles:n});"
        "var bs=ed.querySelectorAll('button');var b=null;"
        "for(var j=0;j<bs.length;j++){"
        "var label=((bs[j].getAttribute('aria-label')||'')+' '+"
        "(bs[j].getAttribute('title')||'')+' '+(bs[j].textContent||''));"
        "if(bs[j].getAttribute('data-type')==='sticker'||"
        "/sticker|스티커|スタンプ|貼圖|贴图/i.test(label)){b=bs[j];break;}"
        "}"
        "if(!b)return JSON.stringify({ok:false,reason:'no_sticker_button',stickerBubbles:n});"
        "var r=b.getBoundingClientRect();"
        "if(!r.width||!r.height)return JSON.stringify({ok:false,reason:'sticker_button_not_visible',stickerBubbles:n});"
        "return JSON.stringify({ok:true,stickerBubbles:n,focused:document.hasFocus(),"
        "x:Math.round(window.screenX+(window.outerWidth-window.innerWidth)/2+r.left+r.width/2),"
        "y:Math.round(window.screenY+(window.outerHeight-window.innerHeight)+r.top+r.height/2)});"
    )
    js_count = (
        "var n=document.querySelectorAll('[class*=\"stickerMessageContent-module__content_wrap__\"]').length;"
        "return JSON.stringify({stickerBubbles:n});"
    )
    js_popup_target = (
        f"var PKG={int(package_idx)},STK={int(sticker_idx)};"
        "function pt(b){var r=b.getBoundingClientRect();return {"
        "x:Math.round(window.screenX+(window.outerWidth-window.innerWidth)/2+r.left+r.width/2),"
        "y:Math.round(window.screenY+(window.outerHeight-window.innerHeight)+r.top+r.height/2)};}"
        "var pkgs=Array.from(document.querySelectorAll('button[class*=\"packageListItem-module__button_package__\"]'));"
        "if(!pkgs.length)return JSON.stringify({ok:false,reason:'no_packages'});"
        "if(PKG>=pkgs.length)return JSON.stringify({ok:false,reason:'package_out_of_range',packages:pkgs.length});"
        "if(pkgs[PKG].getAttribute('aria-selected')!=='true'){"
        "var pp=pt(pkgs[PKG]);return JSON.stringify({ok:true,action:'click_package',packages:pkgs.length,x:pp.x,y:pp.y});"
        "}"
        "var items=Array.from(document.querySelectorAll('button[class*=\"packageItemList-module__button_item__\"]'));"
        "if(!items.length)return JSON.stringify({ok:false,reason:'empty_package',packages:pkgs.length});"
        "if(STK>=items.length)return JSON.stringify({ok:false,reason:'sticker_out_of_range',packages:pkgs.length,stickers:items.length});"
        "var sp=pt(items[STK]);return JSON.stringify({ok:true,action:'click_sticker',packages:pkgs.length,stickers:items.length,x:sp.x,y:sp.y});"
    )

    try:
        before_info = {}
        for i in range(6):
            if i:
                time.sleep(0.04)
            before_info = _json_get(exec_js(js_count_and_open_point, loc, timeout=5, focus=True))
            if before_info.get("ok") or before_info.get("reason") not in {
                "no_editor", "no_sticker_button", "sticker_button_not_visible"
            }:
                break
        before_n = int(before_info.get("stickerBubbles") or 0)
        if not before_info.get("ok"):
            return {"ok": False, "stage": "open_picker",
                    "reason": before_info.get("reason", "sticker button not found"),
                    "detail": before_info}

        if not before_info.get("focused"):
            try:
                _focus_chrome_tab(loc)
            except SkillError:
                pass
        click = _trusted_click(int(before_info["x"]), int(before_info["y"]))
        if not click.get("ok"):
            return {"ok": False, "stage": "trusted_click",
                    "reason": "trusted_input_unavailable", "detail": click}

        popup_loc = _find_sticker_popup_tab(timeout=0.6)
        if not popup_loc:
            try:
                _focus_chrome_tab(loc)
            except SkillError:
                pass
            retry_click = _trusted_click(int(before_info["x"]), int(before_info["y"]))
            if not retry_click.get("ok"):
                return {"ok": False, "stage": "trusted_click",
                        "reason": "trusted_input_unavailable", "detail": retry_click}
            popup_loc = _find_sticker_popup_tab(timeout=0.5)
            if not popup_loc:
                return {"ok": False, "stage": "open_picker",
                        "reason": "picker_unavailable",
                        "detail": {"first": click, "retry": retry_click}}

        target = {}
        for _ in range(6):
            target = _json_get(exec_js(js_popup_target, popup_loc, timeout=5, fallback=False))
            if target.get("ok") or target.get("reason") in {
                "package_out_of_range", "sticker_out_of_range", "empty_package", "no_packages"
            }:
                break
            time.sleep(0.04)
        if not target:
            return {"ok": False, "stage": "open_picker",
                    "reason": "picker_unavailable", "detail": "popup closed before selection"}
        if not target.get("ok"):
            return {"ok": False, "stage": "select_sticker",
                    "reason": target.get("reason", "sticker_not_selected"),
                    "detail": target}

        if target.get("action") == "click_package":
            click_pkg = _trusted_click(int(target["x"]), int(target["y"]))
            if not click_pkg.get("ok"):
                return {"ok": False, "stage": "trusted_click",
                        "reason": "trusted_input_unavailable", "detail": click_pkg}
            target = {}
            for _ in range(6):
                time.sleep(0.05)
                target = _json_get(exec_js(js_popup_target, popup_loc, timeout=5, fallback=False))
                if target.get("action") == "click_sticker" or target.get("reason") in {
                    "sticker_out_of_range", "empty_package", "no_packages"
                }:
                    break
            if not target.get("ok"):
                return {"ok": False, "stage": "select_sticker",
                        "reason": target.get("reason", "sticker_not_selected"),
                        "detail": target}

        click_sticker = _trusted_click(int(target["x"]), int(target["y"]))
        if not click_sticker.get("ok"):
            return {"ok": False, "stage": "trusted_click",
                    "reason": "trusted_input_unavailable", "detail": click_sticker}

        after_raw = ""
        after_n = before_n
        for i in range(6):
            if i:
                time.sleep(0.05)
            after_raw = exec_js(js_count, loc, timeout=5)
            after_n = _parse_sticker_count(after_raw)
            if after_n > before_n:
                break
        if after_n > before_n:
            return {
                "ok": True,
                "verified_by": "sticker_bubble",
                "package": package_idx,
                "sticker": sticker_idx,
                "trusted_input": "core_graphics",
                "sticker_bubbles": [before_n, after_n],
            }
        return {
            "ok": False,
            "stage": "not_confirmed",
            "reason": "no new sticker bubble appeared",
            "sticker_bubbles": [before_n, after_n],
            "detail": after_raw,
        }
    except SkillError as e:
        return {"ok": False, "stage": "osascript", "reason": str(e)}


def cmd_send_sticker(args, sel, sources):
    """Send a sticker to a room. --meaning resolves a tagged sticker; otherwise
    --package / --sticker pick by position (default first sticker of first package).
    Hot path skips navigation."""
    if not args.to:
        raise SkillError("--to is required")
    package_arg = getattr(args, "package", None)
    sticker_arg = getattr(args, "sticker", None)
    meaning = (getattr(args, "meaning", None) or "").strip()
    resolved_meaning = None
    if meaning:
        if package_arg is not None or sticker_arg is not None:
            return {"ok": False, "stage": "validate",
                    "reason": "meaning_conflicts_with_index",
                    "meaning": meaning}
        resolved_meaning = _resolve_sticker_meaning(meaning)
        if not resolved_meaning:
            return {"ok": False, "stage": "validate", "reason": "meaning_not_mapped",
                    "meaning": meaning, "config": str(USER_STICKER_TAGS_PATH)}
        package_idx = resolved_meaning["package"]
        sticker_idx = resolved_meaning["sticker"]
    else:
        package_idx = 0 if package_arg is None else package_arg
        sticker_idx = 0 if sticker_arg is None else sticker_arg

    if package_idx < 0 or sticker_idx < 0:
        return {"ok": False, "stage": "validate", "reason": "negative_index",
                "package": package_idx, "sticker": sticker_idx}
    loc = _require_tab()
    t0 = time.time()

    hdr_sel = json.dumps(sel.get("chat_room_header"), ensure_ascii=False)
    header = ""
    # If the extension is on the chat-list route, no room is open; skip one
    # AppleScript round trip on the cold path.
    if "#/chats/" in (loc.get("url") or ""):
        try:
            header = exec_js(f"return (document.querySelector({hdr_sel})||{{}}).textContent||'';",
                             loc, timeout=5).strip()
        except Exception:
            header = ""
    on_room = bool(header) and (header == args.to or args.to in header or header in args.to)
    path = "hot"
    if not on_room:
        path = "cold"
        nav = _open_visible_room_fast(sel, loc, args.to)
        if not nav.get("ok"):
            nav = _navigate_to_room(sel, loc, args.to)
        if not nav.get("ok"):
            return {"ok": False, "stage": "navigate", "reason": nav,
                    "duration_ms": int((time.time() - t0) * 1000)}

    result = _do_send_sticker(sel, loc, package_idx, sticker_idx)
    result["room"] = args.to
    result["path"] = path
    if resolved_meaning:
        result["meaning"] = meaning
        result["resolved_tag"] = resolved_meaning.get("tag")
        if resolved_meaning.get("label"):
            result["sticker_label"] = resolved_meaning.get("label")
    result["duration_ms"] = int((time.time() - t0) * 1000)
    return result


def cmd_history(args, sel, sources):
    if not args.room:
        raise SkillError("--room is required")
    loc = _require_tab()
    nav = _navigate_to_room(sel, loc, args.room)
    if not nav.get("ok"):
        return {"opened": False, "reason": nav}
    # Poll for messages to lazy-load (max ~1s)
    bubble_sel = json.dumps(sel.get("message_bubble"), ensure_ascii=False)
    poll_js = f"return document.querySelectorAll({bubble_sel}).length;"
    bubble_count = 0
    for _ in range(20):
        try:
            n = int(exec_js(poll_js, loc, timeout=5))
            if n > bubble_count:
                bubble_count = n
                # Saw bubbles — short additional wait for any tail to load, then read
                time.sleep(0.15)
                break
        except Exception:
            pass
        time.sleep(0.05)
    raw = exec_js(js_history(sel, args.limit), loc)
    out = {"room": args.room, "matched": nav.get("matched"), **json.loads(raw)}
    # Clear search so chat list is back to normal
    try:
        exec_js(js_set_search(sel, ""), loc, timeout=5)
    except Exception:
        pass
    return out


def cmd_search(args, sel, sources):
    """Substring search across current message list. Doesn't scroll to load older — use Path 2 for full history."""
    if not args.query or not args.room:
        raise SkillError("--query and --room are required")
    args.limit = max(args.limit, 200)
    h = cmd_history(args, sel, sources)
    matched = [m for m in h.get("messages", []) if args.query in (m.get("text") or "")]
    return {"room": args.room, "query": args.query, "match_count": len(matched), "matches": matched}


def cmd_watch(args, sel, sources):
    loc = _require_tab()
    seen = set()
    sys.stderr.write(f"watching (interval={args.interval}s). Ctrl-C to stop.\n")
    sys.stderr.flush()
    while True:
        try:
            raw = exec_js(js_history(sel, 30), loc, timeout=15)
            data = json.loads(raw)
            for m in data.get("messages", []):
                key = (m.get("author"), m.get("text"), m.get("timestamp"))
                if key in seen or m.get("text") is None:
                    continue
                seen.add(key)
                # Skip the initial batch
                if len(seen) <= 30 and not args.include_initial:
                    continue
                emit_json(m)
                sys.stdout.flush()
        except SkillError as e:
            sys.stderr.write(f"transient error: {e}\n")
            sys.stderr.flush()
        time.sleep(args.interval)


def cmd_selectors(args, sel, sources):
    if args.action == "show":
        return {"sources": sources, "resolved": sel}
    if args.action == "set":
        if not args.key or args.value is None:
            raise SkillError("`selectors set` requires KEY and VALUE positional args")
        USER_SEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        doc = _load_json(USER_SEL_PATH)
        doc.setdefault("selectors", {})[args.key] = args.value
        USER_SEL_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
        return {"updated": str(USER_SEL_PATH), "key": args.key, "value": args.value}
    raise SkillError(f"unknown selectors action '{args.action}'")


def cmd_sticker_tags(args, sel, sources):
    doc = _load_sticker_tags_doc()
    tags = doc.setdefault("tags", {})
    if args.action == "show":
        return {"path": str(USER_STICKER_TAGS_PATH), "tags": tags}

    tag = (args.tag or "").strip()
    if not tag:
        raise SkillError("`sticker-tags set/remove` requires TAG")

    if args.action == "set":
        if args.package is None or args.sticker is None:
            raise SkillError("`sticker-tags set` requires --package and --sticker")
        if args.package < 0 or args.sticker < 0:
            raise SkillError("sticker package/sticker indexes must be non-negative")
        entry = {"package": args.package, "sticker": args.sticker}
        if args.label:
            entry["label"] = args.label
        tags[tag] = entry
        _write_sticker_tags_doc(doc)
        return {"ok": True, "updated": str(USER_STICKER_TAGS_PATH),
                "tag": tag, **entry}

    if args.action == "remove":
        key = tag if tag in tags else None
        if key is None:
            norm = _normalize_sticker_tag(tag)
            for candidate in tags:
                if _normalize_sticker_tag(candidate) == norm:
                    key = candidate
                    break
        if key is None:
            return {"ok": False, "reason": "tag_not_found",
                    "tag": tag, "path": str(USER_STICKER_TAGS_PATH)}
        removed = tags.pop(key)
        _write_sticker_tags_doc(doc)
        return {"ok": True, "updated": str(USER_STICKER_TAGS_PATH),
                "removed_tag": key, "removed": removed}

    raise SkillError(f"unknown sticker-tags action '{args.action}'")


def cmd_cache_info(args, sel, sources):
    profile = args.profile or "Default"
    template = _load_json(DEFAULT_SEL_PATH).get("leveldb_path_template", "")
    path = Path(os.path.expanduser(template.replace("{profile}", profile)))
    info = {"profile": profile, "path": str(path), "exists": path.exists()}
    if path.exists():
        size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        info["size_bytes"] = size
        info["file_count"] = sum(1 for _ in path.rglob("*"))
    return info


def cmd_cache_dump(args, sel, sources):
    info = cmd_cache_info(args, sel, sources)
    if not info["exists"]:
        raise SkillError(f"leveldb dir does not exist at {info['path']}")
    dest = Path(os.path.expanduser(args.out))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise SkillError(f"output path {dest} already exists; remove first")
    shutil.copytree(info["path"], dest)
    return {
        "copied_from": info["path"],
        "copied_to": str(dest),
        "size_bytes": sum(p.stat().st_size for p in dest.rglob("*") if p.is_file()),
        "next_step": (
            "Use external tools to decode: Node's `level` package can read LevelDB, "
            "then use V8 deserializer for the values. This skill does not include parsing."
        ),
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _require_tab() -> dict:
    if not chrome_running():
        raise SkillError("Google Chrome is not running. Start it and open the LINE extension.")
    loc = find_extension_tab()
    if not loc:
        raise SkillError(
            "No Chrome tab with the LINE extension was found. "
            f"Open the LINE extension (must show {EXTENSION_PREFIX}...) and re-run. "
            "Tip: detach the popup into its own window so AppleScript can reach it."
        )
    return loc


# ── argparse ────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(prog="line-chrome", description="Drive LINE Chrome extension.")
    p.add_argument("--selector", action="append", default=[],
                   help="Selector override, e.g. --selector message_input='div.new-class'. Repeatable.")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    sub.add_parser("status")
    sub.add_parser("diagnose")
    sub.add_parser("enable-applescript")

    lr = sub.add_parser("list-rooms"); lr.add_argument("--limit", type=int, default=50)
    lc = sub.add_parser("list-contacts"); lc.add_argument("--limit", type=int, default=50)

    sd = sub.add_parser("send")
    sd.add_argument("--to", required=True)
    sd.add_argument("--text", required=True)

    lg = sub.add_parser("leave-group")
    lg.add_argument("--room", required=True)
    lg.add_argument("--confirm", action="store_true",
                    help="Required. Leaving a group is irreversible.")

    rp = sub.add_parser("reply")
    rp.add_argument("--room", required=True)
    rp.add_argument("--to", required=True,
                    help="Substring of the message being replied to.")
    rp.add_argument("--text", required=True, help="The reply body.")

    ss = sub.add_parser("send-sticker")
    ss.add_argument("--to", required=True, help="Room to send the sticker to.")
    ss.add_argument("--meaning", "--tag", dest="meaning",
                    help="Sticker meaning/tag from ~/.config/line-chrome/stickers.json.")
    ss.add_argument("--package", type=int, default=None,
                    help="Sticker package index (default 0).")
    ss.add_argument("--sticker", type=int, default=None,
                    help="Sticker index within the package (default 0).")

    h = sub.add_parser("history")
    h.add_argument("--room", required=True)
    h.add_argument("--limit", type=int, default=30)

    sr = sub.add_parser("search")
    sr.add_argument("--room", required=True)
    sr.add_argument("--query", required=True)
    sr.add_argument("--limit", type=int, default=200)

    w = sub.add_parser("watch")
    w.add_argument("--interval", type=int, default=5)
    w.add_argument("--include-initial", action="store_true")

    sl = sub.add_parser("selectors")
    sl.add_argument("action", choices=["show", "set"])
    sl.add_argument("key", nargs="?")
    sl.add_argument("value", nargs="?")

    st = sub.add_parser("sticker-tags", aliases=["sticker-tag"])
    st.set_defaults(cmd="sticker-tags")
    st.add_argument("action", choices=["show", "set", "remove"])
    st.add_argument("tag", nargs="?")
    st.add_argument("--package", type=int)
    st.add_argument("--sticker", type=int)
    st.add_argument("--label")

    ci = sub.add_parser("cache-info"); ci.add_argument("--profile")
    cd = sub.add_parser("cache-dump")
    cd.add_argument("--out", required=True); cd.add_argument("--profile")

    args = p.parse_args()
    selectors, sources = load_selectors(args.selector)

    handlers = {
        "status": cmd_status, "diagnose": cmd_diagnose,
        "enable-applescript": cmd_enable_applescript,
        "list-rooms": cmd_list_rooms, "list-contacts": cmd_list_contacts,
        "send": cmd_send, "history": cmd_history, "search": cmd_search,
        "leave-group": cmd_leave_group, "reply": cmd_reply,
        "send-sticker": cmd_send_sticker,
        "watch": cmd_watch,
        "selectors": cmd_selectors,
        "sticker-tags": cmd_sticker_tags,
        "cache-info": cmd_cache_info, "cache-dump": cmd_cache_dump,
    }
    try:
        result = handlers[args.cmd](args, selectors, sources)
        if result is not None:
            emit_json(result)
    except SkillError as e:
        sys.stderr.write(f"error: {e}\n")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
