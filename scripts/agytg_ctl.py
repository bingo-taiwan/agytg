# -*- coding: utf-8 -*-
"""
agytg_ctl.py — Controller for starting, checking status, and stopping the /agytg bridge daemon.
Zero external dependencies (Python 3.10+ standard library only).
"""
import argparse
import ctypes
from ctypes import wintypes
import glob
import json
import os
import subprocess
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HOME_DIR = os.path.expanduser("~")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(SCRIPT_DIR)
DAEMON_SCRIPT = os.path.join(SCRIPT_DIR, "agytg_daemon.py")
BRAIN_DIR = os.environ.get(
    "AGY_BRAIN_DIR",
    os.path.join(HOME_DIR, ".gemini", "antigravity-cli", "brain"),
)
RUNTIME_DIR = os.environ.get(
    "AGYTG_RUNTIME_DIR",
    os.path.join(tempfile.gettempdir(), "agytg"),
)
STATE_PATH = os.path.join(RUNTIME_DIR, "state.json")


def resolve_env_path(explicit_path: str = "") -> str:
    candidates = [
        explicit_path,
        os.environ.get("AGYTG_ENV_FILE", ""),
        os.path.join(HOME_DIR, ".gemini", "agytg.env"),
        os.path.join(SKILL_ROOT, ".env"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return os.path.abspath(path)
    return os.path.join(HOME_DIR, ".gemini", "agytg.env")


def is_pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return False
    code = wintypes.DWORD(0)
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok and code.value == 259)


def kill_pid(pid: int):
    if not is_pid_alive(pid):
        return
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x0001, False, int(pid))
    if h:
        k32.TerminateProcess(h, 0)
        k32.CloseHandle(h)


def find_agy_pid() -> int:
    """Walk parent process chain via PowerShell CIM to find the owning agy.exe PID."""
    ps_cmd = (
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {os.getpid()}'; "
        "while ($p) { "
        "  if ($p.Name -eq 'agy.exe') { Write-Output $p.ProcessId; exit 0 } "
        "  if (-not $p.ParentProcessId -or $p.ParentProcessId -eq $p.ProcessId) { break } "
        "  $p = Get-CimInstance Win32_Process -Filter \"ProcessId = $($p.ParentProcessId)\" "
        "} "
        "$any = Get-Process agy -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending | Select-Object -First 1; "
        "if ($any) { Write-Output $any.Id }"
    )
    shell = "pwsh"
    try:
        out = subprocess.check_output([shell, "-NoProfile", "-Command", ps_cmd], text=True).strip()
    except FileNotFoundError:
        out = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps_cmd], text=True).strip()
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    raise RuntimeError("Could not locate a running agy.exe process.")


def find_latest_cid() -> str:
    pattern = os.path.join(BRAIN_DIR, "*", ".system_generated", "logs", "transcript.jsonl")
    files = glob.glob(pattern)
    if not files:
        raise RuntimeError(f"No transcript.jsonl found under {BRAIN_DIR}")
    latest = max(files, key=os.path.getmtime)
    parts = os.path.normpath(latest).split(os.sep)
    idx = parts.index("brain")
    return parts[idx + 1]


def stop_existing():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
            old_pid = st.get("daemon_pid")
            try:
                os.remove(STATE_PATH)
            except Exception:
                pass
            if old_pid and is_pid_alive(old_pid):
                time.sleep(0.3)
                kill_pid(old_pid)
                print(f"[agytg] Terminated previous daemon PID {old_pid}")
        except Exception as e:
            print(f"[agytg] Warning cleaning old state: {e}")


def cmd_start(args):
    stop_existing()
    cid = args.cid or find_latest_cid()
    agy_pid = args.agy_pid or find_agy_pid()
    env_file = resolve_env_path(args.env)
    if not os.path.exists(env_file):
        raise RuntimeError(
            f"Config file not found: {env_file}\n"
            f"Please copy .env.example to {env_file} and set TELEGRAM_BOT_TOKEN & ALLOWED_USERS."
        )

    transcript = os.path.join(BRAIN_DIR, cid, ".system_generated", "logs", "transcript.jsonl")
    if not os.path.exists(transcript):
        raise RuntimeError(f"Transcript not found: {transcript}")

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    py_exe = sys.executable
    pyw_exe = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
    runner = pyw_exe if os.path.exists(pyw_exe) else py_exe

    cmd_line = (
        f'"{runner}" "{DAEMON_SCRIPT}" '
        f'--cid "{cid}" --agy-pid {agy_pid} '
        f'--transcript "{transcript}" --state "{STATE_PATH}" --env "{env_file}"'
    )
    ps_wmi = (
        f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='{cmd_line}'}}; "
        "Write-Output $r.ProcessId"
    )
    try:
        out = subprocess.check_output(["pwsh", "-NoProfile", "-Command", ps_wmi], text=True).strip()
    except FileNotFoundError:
        out = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps_wmi], text=True).strip()
    spawned_pid = int(out.splitlines()[-1].strip())

    for _ in range(15):
        time.sleep(0.2)
        if os.path.exists(STATE_PATH):
            break

    print("🟢 /agytg bidirectional bridge started!")
    print(f"  • Session ID: {cid}")
    print(f"  • Desktop agy.exe PID: {agy_pid}")
    print(f"  • Background Daemon PID: {spawned_pid} (Alive: {is_pid_alive(spawned_pid)})")


def cmd_status(_args):
    if not os.path.exists(STATE_PATH):
        print("⚪ /agytg is currently not running.")
        return
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        st = json.load(f)
    d_pid = st.get("daemon_pid")
    a_pid = st.get("agy_pid")
    print("📊 /agytg Status:")
    print(f"  • Daemon PID: {d_pid} ({'🟢 Running' if is_pid_alive(d_pid) else '🔴 Stopped'})")
    print(f"  • Desktop agy PID: {a_pid} ({'🟢 Running' if is_pid_alive(a_pid) else '🔴 Closed'})")
    print(f"  • Session ID: {st.get('cid')}")
    print(f"  • Bot: {st.get('bot', 'Telegram Bot')}")


def cmd_stop(_args):
    stop_existing()
    print("🔴 /agytg bidirectional bridge stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Control /agytg Telegram Remote Control daemon")
    sub = parser.add_subparsers(dest="action", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("--cid", default="", help="Conversation ID (auto-detected if omitted)")
    p_start.add_argument("--agy-pid", type=int, default=0, help="agy.exe PID (auto-detected if omitted)")
    p_start.add_argument("--env", default="", help="Path to .env file")

    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("off")

    parsed = parser.parse_args()
    if parsed.action == "start":
        cmd_start(parsed)
    elif parsed.action == "status":
        cmd_status(parsed)
    else:
        cmd_stop(parsed)
