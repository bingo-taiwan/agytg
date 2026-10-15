# -*- coding: utf-8 -*-
"""
agytg_daemon.py — Universal Telegram Bot <-> Desktop agy CLI Bidirectional Bridge
- Outbound: Tails transcript.jsonl in real time and pushes desktop inputs, tool actions, and AI replies to Telegram.
- Inbound: Long-polls Telegram getUpdates (message + callback_query) and injects text/files/skills directly into agy.exe TUI via Win32 CONIN$ WriteConsoleInputW.
- Skills Integration: Auto-discovers installed skills in ~/.gemini/config/skills & ~/.agents/skills, registers Telegram native `/` menu (setMyCommands), and provides a paginated `/skills` Interactive Inline Keyboard panel.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
import re
import sys
import tempfile
import time
import traceback
import urllib.parse
import urllib.request

# ─── Win32 Console Structures for WriteConsoleInputW ─────────────────────────
KEY_EVENT = 0x0001
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

HOME_DIR = os.path.expanduser("~")
SKILLS_DIRS = [
    os.path.join(HOME_DIR, ".gemini", "config", "skills"),
    os.path.join(HOME_DIR, ".agents", "skills"),
]

# Native agy TUI slash commands that should keep leading `/`
NATIVE_AGY_SLASH = {
    "compact",
    "plan",
    "goal",
    "boost",
    "learn",
    "clear",
    "model",
    "help",
    "schedule",
    "browser",
}


class CHAR_UNION(ctypes.Union):
    _fields_ = [
        ("UnicodeChar", wintypes.WCHAR),
        ("AsciiChar", ctypes.c_char),
    ]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", CHAR_UNION),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class EVENT_UNION(ctypes.Union):
    _fields_ = [
        ("KeyEvent", KEY_EVENT_RECORD),
        ("_padding", ctypes.c_byte * 16),
    ]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventType", wintypes.WORD),
        ("Event", EVENT_UNION),
    ]


def make_key_record(down, vk, scan, ch, ctrl=0):
    rec = INPUT_RECORD()
    rec.EventType = KEY_EVENT
    rec.Event.KeyEvent.bKeyDown = 1 if down else 0
    rec.Event.KeyEvent.wRepeatCount = 1
    rec.Event.KeyEvent.wVirtualKeyCode = vk
    rec.Event.KeyEvent.wVirtualScanCode = scan
    rec.Event.KeyEvent.uChar.UnicodeChar = ch
    rec.Event.KeyEvent.dwControlKeyState = ctrl
    return rec


def inject_console_text(
    agy_pid: int, text: str, press_enter: bool = True, double_enter_for_slash: bool = False
) -> tuple[bool, str]:
    """Attach to agy_pid console and inject text + Enter into CONIN$."""
    k32 = ctypes.windll.kernel32
    k32.FreeConsole()
    if not k32.AttachConsole(wintypes.DWORD(agy_pid)):
        err = ctypes.GetLastError()
        return False, f"AttachConsole({agy_pid}) failed (err={err})"

    h_conin = k32.CreateFileW(
        "CONIN$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if h_conin in (-1, 0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        err = ctypes.GetLastError()
        k32.FreeConsole()
        return False, f"CreateFileW(CONIN$) failed (err={err})"

    try:
        clean_text = text.replace("\r\n", " ").replace("\n", " ").strip()
        records = []
        for ch in clean_text:
            code = ord(ch)
            if code > 0xFFFF:
                code -= 0x10000
                high = chr(0xD800 + (code >> 10))
                low = chr(0xDC00 + (code & 0x3FF))
                records.append(make_key_record(True, 0, 0, high))
                records.append(make_key_record(False, 0, 0, high))
                records.append(make_key_record(True, 0, 0, low))
                records.append(make_key_record(False, 0, 0, low))
            else:
                vk = 0
                scan = 0
                if "a" <= ch <= "z":
                    vk = ord(ch.upper())
                elif "A" <= ch <= "Z":
                    vk = ord(ch)
                elif "0" <= ch <= "9":
                    vk = ord(ch)
                elif ch == " ":
                    vk = 0x20
                records.append(make_key_record(True, vk, scan, ch))
                records.append(make_key_record(False, vk, scan, ch))

        written = wintypes.DWORD(0)
        chunk_size = 128
        for i in range(0, len(records), chunk_size):
            batch = records[i : i + chunk_size]
            arr = (INPUT_RECORD * len(batch))(*batch)
            ok = k32.WriteConsoleInputW(h_conin, ctypes.byref(arr), len(batch), ctypes.byref(written))
            if not ok:
                err = ctypes.GetLastError()
                return False, f"WriteConsoleInputW text failed (err={err})"
            time.sleep(0.015)

        if press_enter:
            time.sleep(0.08)
            enter_recs = (INPUT_RECORD * 2)(
                make_key_record(True, 0x0D, 0x1C, "\r"),
                make_key_record(False, 0x0D, 0x1C, "\r"),
            )
            ok = k32.WriteConsoleInputW(h_conin, ctypes.byref(enter_recs), 2, ctypes.byref(written))
            if not ok:
                err = ctypes.GetLastError()
                return False, f"WriteConsoleInputW Enter failed (err={err})"
            if double_enter_for_slash:
                time.sleep(0.12)
                k32.WriteConsoleInputW(h_conin, ctypes.byref(enter_recs), 2, ctypes.byref(written))

        return True, "OK"
    finally:
        k32.CloseHandle(h_conin)
        k32.FreeConsole()


def inject_console_vk(agy_pid: int, vk: int, scan: int, ch: str) -> tuple[bool, str]:
    k32 = ctypes.windll.kernel32
    k32.FreeConsole()
    if not k32.AttachConsole(wintypes.DWORD(agy_pid)):
        return False, f"AttachConsole({agy_pid}) failed"
    h_conin = k32.CreateFileW(
        "CONIN$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if h_conin in (-1, 0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        k32.FreeConsole()
        return False, "CreateFileW(CONIN$) failed"
    try:
        written = wintypes.DWORD(0)
        recs = (INPUT_RECORD * 2)(
            make_key_record(True, vk, scan, ch),
            make_key_record(False, vk, scan, ch),
        )
        k32.WriteConsoleInputW(h_conin, ctypes.byref(recs), 2, ctypes.byref(written))
        return True, "OK"
    finally:
        k32.CloseHandle(h_conin)
        k32.FreeConsole()


# ─── Skills Discovery & Keyboard Builder ─────────────────────────────────────
def discover_local_skills() -> list[dict]:
    """Scan local skills directories and return list of {name, path, priority}."""
    found = {}
    for idx, base_dir in enumerate(SKILLS_DIRS):
        if not os.path.isdir(base_dir):
            continue
        for entry in sorted(os.listdir(base_dir)):
            full = os.path.join(base_dir, entry)
            if not os.path.isdir(full):
                continue
            skill_md = os.path.join(full, "SKILL.md")
            if not os.path.exists(skill_md):
                skill_md = os.path.join(full, "skill.md")
            if not os.path.exists(skill_md):
                continue
            if entry not in found:
                found[entry] = {
                    "name": entry,
                    "path": skill_md,
                    "priority": 0 if idx == 0 else 1,
                }
    items = list(found.values())
    items.sort(key=lambda x: (x["priority"], x["name"]))
    return items


def build_skills_keyboard(page: int = 0) -> tuple[str, dict]:
    """Build Telegram Inline Keyboard for invoking native slash commands & local skills."""
    all_skills = discover_local_skills()
    per_page = 10
    total_pages = max(1, (len(all_skills) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))

    rows = []
    if page == 0:
        # Native agy built-in quick buttons at top of Page 0
        rows.append(
            [
                {"text": "🗜 /compact", "callback_data": "sk_slash:compact"},
                {"text": "🗺 /plan", "callback_data": "sk_slash:plan"},
                {"text": "🚀 /boost", "callback_data": "sk_slash:boost"},
            ]
        )

    start = page * per_page
    subset = all_skills[start : start + per_page]
    for i in range(0, len(subset), 2):
        row = []
        for item in subset[i : i + 2]:
            sname = item["name"]
            cb = f"sk_pick:{sname[:48]}"
            icon = "⭐" if item["priority"] == 0 else "📦"
            row.append({"text": f"{icon} {sname}", "callback_data": cb})
        rows.append(row)

    nav = []
    if page > 0:
        nav.append({"text": "⬅️ Prev", "callback_data": f"sk_page:{page - 1}"})
    if page < total_pages - 1:
        nav.append({"text": "Next ➡️", "callback_data": f"sk_page:{page + 1}"})
    if nav:
        rows.append(nav)

    title = (
        f"🧰 <b>Local agy Skills Panel (Page {page + 1}/{total_pages}, Total: {len(all_skills)})</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• Tap a native command (<code>/compact</code>, <code>/plan</code>, <code>/boost</code>) to execute immediately.\n"
        f"• Tap any local skill to invoke it or attach instructions.\n"
        f"• Or type <code>/&lt;skill_name&gt; &lt;instructions&gt;</code> directly."
    )
    return title, {"inline_keyboard": rows}


# ─── Config & Telegram Helpers ───────────────────────────────────────────────
def load_env(env_path: str) -> dict:
    cfg = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def tg_api(token: str, method: str, payload: dict = None, timeout: int = 25) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tg_send(
    token: str,
    chat_id: int,
    text: str,
    reply_to: int = None,
    reply_markup: dict = None,
    parse_mode: str = None,
):
    if not text:
        return
    max_len = 3800
    chunks = [text[i : i + max_len] for i in range(0, len(text), max_len)]
    for idx, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if reply_to and idx == 0:
            payload["reply_to_message_id"] = reply_to
        if reply_markup and idx == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            tg_api(token, "sendMessage", payload, timeout=15)
        except Exception as e:
            print(f"[agytg] sendMessage error: {e}", flush=True)


def register_bot_commands(token: str):
    """Register native `/` command menu in Telegram from built-in controls + local skills."""
    commands = [
        {"command": "skills", "description": "🧰 Open interactive local skills panel"},
        {"command": "compact", "description": "🗜 [/compact] Compact session context"},
        {"command": "plan", "description": "🗺 [/plan] Enter step-by-step planning mode"},
        {"command": "boost", "description": "🚀 [/boost] Deep multi-perspective reasoning"},
        {"command": "status", "description": "📊 Show desktop agy session & bridge status"},
        {"command": "esc", "description": "⏹ Send ESC key to interrupt desktop generation"},
        {"command": "off", "description": "🔴 Disconnect Telegram remote control"},
    ]
    # Add up to 15 local skills with valid Telegram command names (^[a-z0-9_]{1,32}$)
    seen = {c["command"] for c in commands}
    for item in discover_local_skills():
        cmd_name = item["name"].lower().replace("-", "_")
        if re.match(r"^[a-z0-9_]{1,32}$", cmd_name) and cmd_name not in seen:
            seen.add(cmd_name)
            commands.append({"command": cmd_name, "description": f"⭐ Invoke {item['name']} skill"})
            if len(commands) >= 22:
                break
    try:
        tg_api(token, "setMyCommands", {"commands": commands}, timeout=10)
    except Exception as e:
        print(f"[agytg] setMyCommands warning: {e}", flush=True)


def tg_download_file(token: str, file_id: str, dest_dir: str, fallback_name: str) -> str:
    info = tg_api(token, "getFile", {"file_id": file_id}, timeout=15)
    file_path = info["result"]["file_path"]
    ext = os.path.splitext(file_path)[1] or os.path.splitext(fallback_name)[1] or ".bin"
    base = os.path.splitext(fallback_name)[0]
    local_name = f"{base}_{int(time.time())}{ext}"
    local_path = os.path.join(dest_dir, local_name)
    dl_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    urllib.request.urlretrieve(dl_url, local_path)
    return local_path


def is_pid_alive(pid: int) -> bool:
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    code = wintypes.DWORD(0)
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok and code.value == 259)


def resolve_slash_or_skill(text: str, all_skill_names: set[str]) -> tuple[str, bool]:
    """
    Transform a Telegram `/cmd [args]` into the optimal prompt for desktop agy CLI.
    Returns: (prompt_to_inject, is_native_slash)
    """
    if not text.startswith("/"):
        return text, False

    parts = text[1:].split(None, 1)
    if not parts:
        return text, False

    raw_cmd = parts[0].split("@")[0]
    cmd_lower = raw_cmd.lower()
    extra = parts[1].strip() if len(parts) > 1 else ""

    if cmd_lower in NATIVE_AGY_SLASH:
        full = f"/{cmd_lower}" + (f" {extra}" if extra else "")
        return full, True

    candidate_hyphen = cmd_lower.replace("_", "-")
    for candidate in (raw_cmd, cmd_lower, candidate_hyphen):
        if candidate in all_skill_names:
            if extra:
                return f"Please use the {candidate} skill: {extra}", False
            return f"Please run the {candidate} skill.", False

    if extra:
        return f"Please use {raw_cmd}: {extra}", False
    return f"Please run {raw_cmd}", False


# ─── Main Bridge Loop ────────────────────────────────────────────────────────
def run_daemon(cid: str, agy_pid: int, transcript_path: str, state_path: str, env_path: str):
    cfg = load_env(env_path)
    token = cfg.get("TELEGRAM_BOT_TOKEN", "")
    allowed_raw = cfg.get("ALLOWED_USERS", "")
    allowed_users = {int(x.strip()) for x in allowed_raw.split(",") if x.strip().isdigit()}
    if not token or not allowed_users:
        raise RuntimeError(f"Missing TELEGRAM_BOT_TOKEN or ALLOWED_USERS in {env_path}")

    primary_chat_id = next(iter(allowed_users))
    runtime_dir = os.path.dirname(os.path.abspath(state_path))
    dl_dir = os.path.join(runtime_dir, "downloads")
    os.makedirs(dl_dir, exist_ok=True)

    register_bot_commands(token)

    offset = 0
    try:
        init_up = tg_api(token, "getUpdates", {"offset": -1, "timeout": 1}, timeout=10)
        if init_up.get("ok") and init_up.get("result"):
            offset = init_up["result"][-1]["update_id"] + 1
    except Exception as e:
        print(f"[agytg] init getUpdates warning: {e}", flush=True)

    file_pos = 0
    seen_steps = set()
    if os.path.exists(transcript_path):
        file_pos = os.path.getsize(transcript_path)
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if "step_index" in obj:
                            seen_steps.add((obj.get("step_index"), obj.get("type"), obj.get("status")))
                    except Exception:
                        pass
        except Exception:
            pass

    recent_tg_inputs = {}
    pending_skill_by_chat = {}
    started_at = time.time()

    state_data = {
        "daemon_pid": os.getpid(),
        "agy_pid": agy_pid,
        "cid": cid,
        "transcript_path": transcript_path,
        "started_at": started_at,
        "bot": cfg.get("BOT_HANDLE", "Telegram Bot"),
    }
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state_data, f, ensure_ascii=False, indent=2)

    _, kb_markup = build_skills_keyboard(page=0)
    tg_send(
        token,
        primary_chat_id,
        f"🟢 <b>Connected to Desktop agy CLI Session ({cid[:8]})</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• Type <code>/</code> for native skill & command menu\n"
        f"• Type <code>/skills</code> for the interactive button panel\n"
        f"• Send text or photos/files ➔ Injected directly into desktop agy TUI",
        reply_markup=kb_markup,
        parse_mode="HTML",
    )

    partial_line = ""

    while True:
        if not is_pid_alive(agy_pid):
            tg_send(token, primary_chat_id, f"⚪ Desktop agy.exe (PID {agy_pid}) closed. Stopping /agytg bridge.")
            break

        if not os.path.exists(state_path):
            tg_send(token, primary_chat_id, f"🔴 /agytg bridge stopped from desktop (Session {cid[:8]}).")
            break

        if os.path.exists(transcript_path):
            try:
                cur_size = os.path.getsize(transcript_path)
                if cur_size < file_pos:
                    file_pos = 0
                if cur_size > file_pos:
                    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(file_pos)
                        chunk = f.read()
                        file_pos = f.tell()
                    buf = partial_line + chunk
                    lines = buf.split("\n")
                    partial_line = lines.pop()
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            step = json.loads(line)
                        except Exception:
                            continue
                        s_idx = step.get("step_index")
                        s_type = step.get("type", "")
                        s_status = step.get("status", "")
                        key = (s_idx, s_type, s_status)
                        if key in seen_steps:
                            continue
                        seen_steps.add(key)

                        if s_type == "USER_INPUT" and s_status == "DONE":
                            content = (step.get("content") or "").strip()
                            if "<USER_REQUEST>" in content and "</USER_REQUEST>" in content:
                                content = content.split("<USER_REQUEST>", 1)[1].split("</USER_REQUEST>", 1)[0].strip()
                            if not content:
                                continue
                            now = time.time()
                            matched_tg = False
                            for k_txt, ts in list(recent_tg_inputs.items()):
                                if now - ts > 30:
                                    del recent_tg_inputs[k_txt]
                                elif k_txt == content or k_txt in content or content in k_txt:
                                    matched_tg = True
                                    break
                            if not matched_tg:
                                tg_send(token, primary_chat_id, f"🖥 [Desktop Input]\n{content}")

                        elif s_type == "PLANNER_RESPONSE" and s_status == "DONE":
                            content = (step.get("content") or "").strip()
                            tool_calls = step.get("tool_calls") or []
                            if tool_calls:
                                summaries = []
                                for tc in tool_calls:
                                    t_name = tc.get("name") or tc.get("tool_name") or "tool"
                                    args = tc.get("arguments") or tc.get("args") or {}
                                    if isinstance(args, str):
                                        try:
                                            args = json.loads(args)
                                        except Exception:
                                            args = {}
                                    action = args.get("toolAction") or args.get("toolSummary") or t_name
                                    summaries.append(f"• {action} ({t_name})")
                                tool_msg = "🔧 [Tool Execution]\n" + "\n".join(summaries)
                                if content:
                                    tg_send(token, primary_chat_id, f"🤖 {content}\n\n{tool_msg}")
                                else:
                                    tg_send(token, primary_chat_id, tool_msg)
                            elif content:
                                tg_send(token, primary_chat_id, f"🤖 {content}")
            except Exception as e:
                print(f"[agytg] transcript tail error: {e}", flush=True)

        try:
            res = tg_api(
                token,
                "getUpdates",
                {"offset": offset, "timeout": 2, "allowed_updates": ["message", "callback_query"]},
                timeout=8,
            )
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1

                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    cq_id = cq.get("id")
                    cq_data = cq.get("data") or ""
                    cq_msg = cq.get("message") or {}
                    chat_id = (cq_msg.get("chat") or {}).get("id")
                    msg_id = cq_msg.get("message_id")
                    if chat_id not in allowed_users:
                        continue

                    try:
                        tg_api(token, "answerCallbackQuery", {"callback_query_id": cq_id}, timeout=5)
                    except Exception:
                        pass

                    if cq_data.startswith("sk_page:"):
                        page_num = int(cq_data.split(":", 1)[1])
                        new_title, new_kb = build_skills_keyboard(page=page_num)
                        try:
                            tg_api(
                                token,
                                "editMessageText",
                                {
                                    "chat_id": chat_id,
                                    "message_id": msg_id,
                                    "text": new_title,
                                    "parse_mode": "HTML",
                                    "reply_markup": new_kb,
                                },
                                timeout=10,
                            )
                        except Exception:
                            tg_send(token, chat_id, new_title, reply_markup=new_kb, parse_mode="HTML")
                        continue

                    if cq_data.startswith("sk_slash:"):
                        slash_cmd = "/" + cq_data.split(":", 1)[1]
                        recent_tg_inputs[slash_cmd] = time.time()
                        ok, info = inject_console_text(
                            agy_pid, slash_cmd, press_enter=True, double_enter_for_slash=True
                        )
                        tg_send(token, chat_id, f"⚡ Injected slash command: {slash_cmd}" if ok else f"❌ Failed: {info}")
                        continue

                    if cq_data.startswith("sk_pick:"):
                        sname = cq_data.split(":", 1)[1]
                        pending_skill_by_chat[chat_id] = sname
                        sub_kb = {
                            "inline_keyboard": [
                                [
                                    {"text": f"▶️ Run {sname} immediately", "callback_data": f"sk_execnow:{sname[:44]}"},
                                    {"text": "❌ Cancel", "callback_data": "sk_cancel"},
                                ]
                            ]
                        }
                        tg_send(
                            token,
                            chat_id,
                            f"🧰 Selected <b>{sname}</b> skill!\n"
                            f"Reply with your instructions (or send a photo/file), or tap below to run without extra arguments:",
                            reply_markup=sub_kb,
                            parse_mode="HTML",
                        )
                        continue

                    if cq_data.startswith("sk_execnow:"):
                        sname = cq_data.split(":", 1)[1]
                        pending_skill_by_chat.pop(chat_id, None)
                        prompt_txt = f"Please run the {sname} skill."
                        recent_tg_inputs[prompt_txt] = time.time()
                        ok, info = inject_console_text(agy_pid, prompt_txt, press_enter=True)
                        tg_send(token, chat_id, f"⚡ Injected into desktop:\n{prompt_txt}" if ok else f"❌ Failed: {info}")
                        continue

                    if cq_data == "sk_cancel":
                        pending_skill_by_chat.pop(chat_id, None)
                        tg_send(token, chat_id, "👌 Skill selection cancelled.")
                        continue

                msg = upd.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                msg_id = msg.get("message_id")
                if chat_id not in allowed_users:
                    continue

                text = (msg.get("text") or msg.get("caption") or "").strip()

                if text in ("/off", "/stop", "/exit"):
                    tg_send(token, chat_id, f"🔴 Disconnected from desktop agy Session ({cid[:8]}).", reply_to=msg_id)
                    try:
                        if os.path.exists(state_path):
                            os.remove(state_path)
                    except Exception:
                        pass
                    return

                if text in ("/skills", "/skill", "/s", "/menu"):
                    title_html, kb_markup = build_skills_keyboard(page=0)
                    tg_send(token, chat_id, title_html, reply_to=msg_id, reply_markup=kb_markup, parse_mode="HTML")
                    continue

                if text in ("/status", "/start"):
                    uptime_m = int((time.time() - started_at) // 60)
                    uptime_s = int((time.time() - started_at) % 60)
                    alive_str = "🟢 Running" if is_pid_alive(agy_pid) else "🔴 Closed"
                    tg_send(
                        token,
                        chat_id,
                        f"📊 /agytg Remote Control Status\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"• Desktop agy: {alive_str} (PID {agy_pid})\n"
                        f"• Session ID: {cid}\n"
                        f"• Daemon PID: {os.getpid()}\n"
                        f"• Uptime: {uptime_m}m {uptime_s}s\n"
                        f"• Synced Steps: {len(seen_steps)}",
                        reply_to=msg_id,
                    )
                    continue

                if text == "/esc":
                    ok, info = inject_console_vk(agy_pid, 0x1B, 0x01, "\x1b")
                    tg_send(
                        token,
                        chat_id,
                        "⏹ Sent ESC key to desktop agy window." if ok else f"⚠️ Failed to send ESC: {info}",
                        reply_to=msg_id,
                    )
                    continue

                injected_prefix = ""
                if "photo" in msg and msg["photo"]:
                    best_photo = msg["photo"][-1]
                    local_file = tg_download_file(token, best_photo["file_id"], dl_dir, "photo.jpg")
                    injected_prefix = f"@{local_file} "
                elif "document" in msg and msg["document"]:
                    doc = msg["document"]
                    fname = doc.get("file_name") or "document.bin"
                    local_file = tg_download_file(token, doc["file_id"], dl_dir, fname)
                    injected_prefix = f"@{local_file} "

                waiting_skill = pending_skill_by_chat.pop(chat_id, None)
                is_native_slash = False

                if waiting_skill and not text.startswith("/"):
                    final_prompt = f"Please use the {waiting_skill} skill: {injected_prefix}{text}".strip()
                elif text.startswith("/"):
                    all_skill_names = {x["name"] for x in discover_local_skills()}
                    resolved, is_native_slash = resolve_slash_or_skill(text, all_skill_names)
                    final_prompt = (injected_prefix + resolved).strip()
                else:
                    final_prompt = (injected_prefix + text).strip()

                if not final_prompt:
                    continue

                recent_tg_inputs[final_prompt] = time.time()
                ok, info = inject_console_text(
                    agy_pid,
                    final_prompt,
                    press_enter=True,
                    double_enter_for_slash=is_native_slash,
                )
                if ok:
                    tg_send(token, chat_id, f"⚡ Injected into desktop:\n{final_prompt}", reply_to=msg_id)
                else:
                    tg_send(token, chat_id, f"❌ Injection failed: {info}", reply_to=msg_id)

        except Exception:
            time.sleep(0.5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cid", required=True)
    parser.add_argument("--agy-pid", type=int, required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--state", default=os.path.join(tempfile.gettempdir(), "agytg", "state.json"))
    parser.add_argument("--env", default=os.path.join(HOME_DIR, ".gemini", "agytg.env"))
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.state), exist_ok=True)
    log_path = os.path.join(os.path.dirname(args.state), "daemon.log")
    with open(log_path, "a", encoding="utf-8") as log_f:
        sys.stdout = log_f
        sys.stderr = log_f
        print(f"\n--- [agytg] Starting daemon at {time.strftime('%Y-%m-%d %H:%M:%S')} ---", flush=True)
        try:
            run_daemon(args.cid, args.agy_pid, args.transcript, args.state, args.env)
        except Exception:
            traceback.print_exc()
            sys.stdout.flush()
