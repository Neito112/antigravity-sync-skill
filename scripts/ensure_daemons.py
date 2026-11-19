#!/usr/bin/env python3
"""
Update-proof daemon watchdog for Antigravity customizations.
Checks if antigravity-sync (Syn topbar button) and antigravity-custom-theme
daemons are running; if not (e.g. after an Antigravity update/restart),
launches them detached in the background.
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SKILLS_DIR = Path.home() / ".gemini" / "config" / "skills"

DAEMONS = [
    {
        "name": "antigravity-sync",
        "script": SKILLS_DIR / "antigravity-sync" / "daemon" / "syn_topbar_daemon.js",
        "pid_file": SKILLS_DIR / "antigravity-sync" / "syn-daemon.pid",
    },
    {
        "name": "antigravity-custom-theme",
        "script": SKILLS_DIR / "antigravity-custom-theme" / "daemon" / "theme_daemon.js",
        "pid_file": SKILLS_DIR / "antigravity-custom-theme" / "daemon.pid",
    },
    {
        "name": "antigravity-gemini-extension",
        "script": SKILLS_DIR / "antigravity-gemini-extension" / "daemon" / "bridge_topbar_daemon.js",
        "pid_file": SKILLS_DIR / "antigravity-gemini-extension" / "bridge-topbar-daemon.pid",
    },
]


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        try:
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            return str(pid) in res.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def launch_detached(node_exe: str, script_path: Path, name: str = "daemon"):
    if platform.system() == "Windows":
        # Launching via explorer.exe -> .vbs escapes Antigravity/hook Windows Job Objects
        # so the daemon is NOT killed when the hook or Antigravity restarts/updates.
        import tempfile
        vbs_path = Path(tempfile.gettempdir()) / f"agy_watchdog_{name}.vbs"
        cwd_str = str(script_path.parent.parent).replace('"', '""')
        node_str = str(node_exe).replace('"', '""')
        script_str = str(script_path).replace('"', '""')
        vbs_content = (
            'Set WshShell = CreateObject("WScript.Shell")\r\n'
            f'WshShell.CurrentDirectory = "{cwd_str}"\r\n'
            f'WshShell.Run """{node_str}"" ""{script_str}""", 0, False\r\n'
        )
        vbs_path.write_text(vbs_content, encoding="ascii")
        subprocess.Popen(
            ["explorer.exe", str(vbs_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    subprocess.Popen(
        [node_exe, str(script_path)],
        cwd=str(script_path.parent.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main():
    node_exe = shutil.which("node")
    if not node_exe:
        return 0

    for d in DAEMONS:
        script = d["script"]
        pid_file = d["pid_file"]
        if not script.exists():
            continue
        alive = False
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                alive = is_pid_alive(pid)
            except Exception:
                alive = False
        if not alive:
            try:
                launch_detached(node_exe, script, d["name"])
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
