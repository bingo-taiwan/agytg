---
name: agytg
description: Bidirectional Telegram Remote Control for Google Antigravity CLI (agy) sessions (similar to Claude Code /rc). Takes over a live desktop agy TUI session from a Telegram Bot on your phone, streaming desktop outputs to Telegram and injecting mobile text/photos/skills directly into the desktop agy console. Trigger on /agytg, Telegram remote control, or mobile takeover requests.
---

# `/agytg` — Bidirectional Telegram Remote Control for `agy` CLI

Turn any Telegram Bot into a real-time, bidirectional remote control for a live desktop **Google Antigravity (`agy`) CLI** session:
- **Outbound Stream (Desktop ➔ Phone Telegram)**: Tails the current session's `transcript.jsonl` in real time, forwarding desktop keyboard prompts (`🖥`), tool executions (`🔧`), and AI responses (`🤖`) to Telegram.
- **Inbound Injection (Phone Telegram ➔ Desktop TUI)**: Uses Win32 `CONIN$` (`AttachConsole` + `WriteConsoleInputW`) to type messages, uploaded photos/files (`@path`), and local skills directly into the live `agy` TUI window and press Enter — works even when the terminal window is minimized or the PC screen is locked.

## Commands

### 1. Start Takeover (`/agytg` or `/agytg start`)
```powershell
python "$HOME\.gemini\config\skills\agytg\scripts\agytg_ctl.py" start
```
*(Optional: pass `--cid "<CONVERSATION_ID>"` to bind a specific session ID.)*

### 2. Check Status (`/agytg status`)
```powershell
python "$HOME\.gemini\config\skills\agytg\scripts\agytg_ctl.py" status
```

### 3. Stop Takeover (`/agytg off` or `/agytg stop`)
```powershell
python "$HOME\.gemini\config\skills\agytg\scripts\agytg_ctl.py" stop
```

## Mobile Telegram Commands
- **Send any text** ➔ Injected directly into the desktop `agy` prompt and submitted.
- **Send photo / file** ➔ Downloaded locally and injected as `@<local_path> <caption>`.
- **`/skills` or `/s`** ➔ Opens an interactive Inline Keyboard menu of all locally installed `agy` skills (`~/.gemini/config/skills` & `~/.agents/skills`) for 1-tap invocation.
- **`/compact`, `/plan`, `/goal`, `/boost`** ➔ Triggers native `agy` slash commands.
- **`/status`** ➔ Shows live connection & session status.
- **`/esc`** ➔ Sends `VK_ESCAPE` to interrupt an active generation on desktop.
- **`/off`** ➔ Disconnects the bridge from your phone.
