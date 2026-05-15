#!/usr/bin/env python3
# Knowledge Atlas — macOS menu-bar / Windows system-tray controller.
# Copyright (c) 2026 AlphaOne LLC. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is included with this distribution (LICENSE) and at:
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# SPDX-License-Identifier: Apache-2.0

"""
atlas_tray.py — cross-platform menu-bar / system-tray app for the
Knowledge Atlas service.

USAGE

  python3 atlas_tray.py             Run in foreground (Ctrl-C to quit).
                                    Icon disappears when the terminal closes.

  python3 atlas_tray.py --install   PERSISTENT: install a LaunchAgent (macOS)
                                    or Startup shortcut (Windows). Icon
                                    appears on every login and is restarted
                                    if it ever dies. Command returns
                                    immediately.

  python3 atlas_tray.py --uninstall Remove the persistent install.

  python3 atlas_tray.py --status    Show whether the persistent service is
                                    installed and currently running.

  python3 atlas_tray.py --restart   Reload the persistent service.

ICON
  Shows a colored 'A' icon in the menu bar (macOS) or system tray (Windows):
    · green = running
    · red   = stopped
    · amber = transitional (starting/stopping)

MENU
  Right-click (or click on macOS) for:
    · Start / Restart / Stop
    · Open dashboard           → http://127.0.0.1:5179/ in browser
    · View logs                → opens data/atlas.log
    · Reveal in Finder/Explorer
    · Copy status
    · Quit tray                (does NOT stop the atlas service)

DEPENDENCIES
    pip install pystray pillow
"""

import argparse
import os
import platform
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

try:
    from pystray import Icon, Menu, MenuItem
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Missing dependencies. Install with:\n  pip install pystray pillow",
          file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parent
ATLAS_CLI = ROOT / "atlas.py"
LOG_FILE = ROOT / "data" / "atlas.log"
DASHBOARD_URL = "http://127.0.0.1:5179/"
POLL_INTERVAL_SEC = 15  # status indicator refresh; 3s was excessive log noise
ICON_SIZE = 64  # rendered at high-res; OS scales down for menu bar

PLATFORM = platform.system()  # "Darwin" / "Windows" / "Linux"


# ---------- shared helpers (mirror atlas.py logic, no import to keep it light) -

import socket


def _read_pid():
    p = ROOT / "data" / "atlas.pid"
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def _alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _port_open():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", 5179))
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False
    finally:
        s.close()


def _atlas_running():
    pid = _read_pid()
    return bool(pid and _alive(pid)) or _port_open()


def _http_get_json(path, timeout=1.5):
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(DASHBOARD_URL.rstrip("/") + path,
                                    timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _run_cli(verb):
    """Spawn `python3 atlas.py <verb>` and return (rc, output)."""
    try:
        proc = subprocess.run(
            [sys.executable, str(ATLAS_CLI), verb],
            cwd=str(ROOT),
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode, (proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired:
        return 1, "timed out"
    except Exception as e:
        return 1, str(e)


# ---------- icon rendering ---------------------------------------------------

def _make_icon(color):
    """Render a square icon with a rounded background + 'A' letter.
    `color` is the background hex (status indicator)."""
    size = ICON_SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # rounded square background in the status color
    radius = int(size * 0.22)
    d.rounded_rectangle(
        (4, 4, size - 4, size - 4),
        radius=radius, fill=color,
    )

    # white 'A' letter in the middle
    try:
        # macOS / Windows have these system fonts
        font_paths = [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "C:\\Windows\\Fonts\\arialbd.ttf",
            "C:\\Windows\\Fonts\\arial.ttf",
        ]
        font = None
        for fp in font_paths:
            if Path(fp).exists():
                font = ImageFont.truetype(fp, int(size * 0.55))
                break
        if font is None:
            font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    text = "A"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1] - 1
    d.text((tx, ty), text, fill="white", font=font)
    return img


ICON_GREEN  = None
ICON_RED    = None
ICON_AMBER  = None

def _ensure_icons():
    global ICON_GREEN, ICON_RED, ICON_AMBER
    if ICON_GREEN is None:
        ICON_GREEN  = _make_icon("#0d8a3c")   # tactic green
        ICON_RED    = _make_icon("#b51c1c")   # warning red
        ICON_AMBER  = _make_icon("#a26800")   # framework amber


# ---------- platform-specific helpers ----------------------------------------

def _reveal_in_file_manager(path):
    path = Path(path)
    if not path.exists():
        # Use the directory if file doesn't exist yet
        path = path.parent
    if PLATFORM == "Darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif PLATFORM == "Windows":
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def _open_file(path):
    path = Path(path)
    if not path.exists():
        return
    if PLATFORM == "Darwin":
        subprocess.Popen(["open", str(path)])
    elif PLATFORM == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _notify(title, message):
    """Best-effort native notification (macOS only; silent fail elsewhere)."""
    if PLATFORM == "Darwin":
        try:
            subprocess.run([
                "osascript", "-e",
                f'display notification "{message}" with title "{title}"',
            ], check=False)
        except Exception:
            pass


# ---------- menu actions -----------------------------------------------------

# Use a mutable holder so menu callbacks can update the icon
_state = {
    "icon": None,
    "running": False,
    "summary": None,   # last /api/source response
    "busy": False,
}


def _refresh_icon():
    icon = _state["icon"]
    if icon is None:
        return
    running = _atlas_running()
    if _state["busy"]:
        icon.icon = ICON_AMBER
    elif running:
        icon.icon = ICON_GREEN
    else:
        icon.icon = ICON_RED
    _state["running"] = running
    if running:
        s = _http_get_json("/api/source")
        if s:
            _state["summary"] = s
    else:
        _state["summary"] = None
    # Rebuild menu to update labels/state
    icon.menu = _build_menu()
    try:
        icon.update_menu()
    except Exception:
        pass


def _polling_loop():
    while True:
        try:
            _refresh_icon()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_SEC)


def on_start(icon, item):
    _state["busy"] = True
    _refresh_icon()
    rc, _ = _run_cli("start")
    _state["busy"] = False
    _refresh_icon()
    _notify("Knowledge Atlas",
            "Started — dashboard ready" if rc == 0 else "Start failed; check logs")


def on_restart(icon, item):
    _state["busy"] = True
    _refresh_icon()
    rc, _ = _run_cli("restart")
    _state["busy"] = False
    _refresh_icon()
    _notify("Knowledge Atlas",
            "Restarted" if rc == 0 else "Restart failed; check logs")


def on_stop(icon, item):
    _state["busy"] = True
    _refresh_icon()
    rc, _ = _run_cli("stop")
    _state["busy"] = False
    _refresh_icon()
    _notify("Knowledge Atlas",
            "Stopped" if rc == 0 else "Stop failed; check logs")


def on_open_dashboard(icon, item):
    if not _atlas_running():
        _notify("Knowledge Atlas",
                "Atlas is not running — Start it first.")
        return
    webbrowser.open(DASHBOARD_URL)


def on_view_logs(icon, item):
    if not LOG_FILE.exists():
        _notify("Knowledge Atlas", "No log file yet.")
        return
    _open_file(LOG_FILE)


def on_reveal_data(icon, item):
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    _reveal_in_file_manager(data_dir)


def on_copy_status(icon, item):
    """Copy a one-line status string to the clipboard."""
    running = _atlas_running()
    s = _state.get("summary") or {}
    text = (
        f"Atlas: {'running' if running else 'stopped'}"
        + (f" · {s.get('cards', '?')} cards · {s.get('videos', '?')} videos · "
           f"{s.get('categories', '?')} topics"
           if s else "")
    )
    try:
        if PLATFORM == "Darwin":
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            proc.communicate(text.encode("utf-8"))
        elif PLATFORM == "Windows":
            proc = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
            proc.communicate(text.encode("utf-8"))
    except Exception:
        pass
    _notify("Knowledge Atlas", "Status copied to clipboard")


def on_quit(icon, item):
    icon.stop()


# ---------- menu construction ------------------------------------------------

def _build_menu():
    running = _state.get("running", False)
    busy = _state.get("busy", False)
    summary = _state.get("summary") or {}

    if busy:
        header_text = "● Working…"
    elif running:
        header_text = f"● Running on {DASHBOARD_URL}"
    else:
        header_text = "● Stopped"

    items = [
        MenuItem(header_text, None, enabled=False),
    ]

    # Atlas stats line (only when running and we have data)
    if running and summary.get("name"):
        items.append(MenuItem(
            f"   {summary.get('cards', '?')} cards · "
            f"{summary.get('videos', '?')} videos · "
            f"{summary.get('categories', '?')} topics",
            None, enabled=False))
        items.append(MenuItem(
            f"   {summary['name']}", None, enabled=False))

    items += [
        Menu.SEPARATOR,
        MenuItem("Start",   on_start,   enabled=not running and not busy),
        MenuItem("Restart", on_restart, enabled=running and not busy),
        MenuItem("Stop",    on_stop,    enabled=running and not busy),
        Menu.SEPARATOR,
        MenuItem("Open dashboard…",     on_open_dashboard, enabled=running),
        MenuItem("View logs…",          on_view_logs),
        MenuItem("Reveal data folder…", on_reveal_data),
        MenuItem("Copy status",         on_copy_status),
        Menu.SEPARATOR,
        MenuItem("Quit tray", on_quit),
    ]
    return Menu(*items)


# ---------- main -------------------------------------------------------------

# ============================================================================
# Persistent install — macOS LaunchAgent / Windows Startup folder
# ============================================================================

LAUNCH_AGENT_LABEL = "com.alphaone.knowledge-atlas-tray"
LAUNCH_AGENT_PATH = Path.home() / "Library/LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
WINDOWS_STARTUP_FILENAME = "KnowledgeAtlasTray.bat"


def _launch_agent_plist():
    """Generate the LaunchAgent plist XML for this Python interpreter + script."""
    python_path = sys.executable
    script_path = str(Path(__file__).resolve())
    cwd = str(Path(__file__).resolve().parent)
    log_out = str(Path(cwd) / "data" / "atlas-tray.log")
    log_err = str(Path(cwd) / "data" / "atlas-tray.err.log")
    # Ensure log directory exists
    (Path(cwd) / "data").mkdir(exist_ok=True)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
    </array>
    <key>WorkingDirectory</key> <string>{cwd}</string>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>        <true/>
    <key>ProcessType</key>      <string>Interactive</string>
    <key>StandardOutPath</key>  <string>{log_out}</string>
    <key>StandardErrorPath</key><string>{log_err}</string>
</dict>
</plist>
"""


def _launchctl(*args):
    """Run launchctl and return (rc, output)."""
    try:
        proc = subprocess.run(["launchctl", *args],
                              capture_output=True, text=True, timeout=10)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _agent_loaded():
    """Returns True if launchd knows about our agent."""
    rc, out = _launchctl("list", LAUNCH_AGENT_LABEL)
    return rc == 0


def cmd_install():
    """Install + start the persistent service for the current platform."""
    if PLATFORM == "Darwin":
        return _install_macos()
    if PLATFORM == "Windows":
        return _install_windows()
    print(f"--install is supported only on macOS and Windows (got {PLATFORM}).",
          file=sys.stderr)
    return 1


def _install_macos():
    # Kill any currently-running foreground tray instance first
    foreground_pids = _find_foreground_tray_pids()
    if foreground_pids:
        print(f"Stopping {len(foreground_pids)} foreground tray instance(s)…")
        for p in foreground_pids:
            try:
                os.kill(p, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_PATH.write_text(_launch_agent_plist())
    print(f"Wrote LaunchAgent: {LAUNCH_AGENT_PATH}")

    # Unload first if it's already loaded (idempotent re-install)
    if _agent_loaded():
        _launchctl("unload", "-w", str(LAUNCH_AGENT_PATH))
        time.sleep(0.5)

    rc, out = _launchctl("load", "-w", str(LAUNCH_AGENT_PATH))
    if rc != 0:
        print(f"✗ launchctl load failed: {out}", file=sys.stderr)
        return 1

    # Wait a beat for launchd to spawn it
    time.sleep(1.5)
    if _agent_loaded():
        print(f"✓ Knowledge Atlas tray installed and started.")
        print(f"  · runs as user LaunchAgent: {LAUNCH_AGENT_LABEL}")
        print(f"  · auto-starts on login")
        print(f"  · auto-restarts if it crashes (KeepAlive)")
        print(f"  · logs:  data/atlas-tray.log  +  data/atlas-tray.err.log")
        print(f"")
        print(f"To remove: python3 atlas_tray.py --uninstall")
        return 0
    print(f"⚠ Installed plist but agent didn't appear in launchctl list.")
    return 1


def _install_windows():
    """Drop a .bat shortcut into the user's Startup folder."""
    startup = Path(os.environ.get("APPDATA", "")) / \
              "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    if not startup.exists():
        print(f"✗ Startup folder not found at {startup}", file=sys.stderr)
        return 1
    target = startup / WINDOWS_STARTUP_FILENAME
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not Path(pythonw).exists():
        pythonw = sys.executable  # fall back; will flash a console window
    script_path = str(Path(__file__).resolve())
    target.write_text(
        f'@echo off\r\nstart "" "{pythonw}" "{script_path}"\r\n'
    )
    print(f"✓ Knowledge Atlas tray installed:")
    print(f"  · {target}")
    print(f"  · runs at every login (Startup folder)")
    # Launch it now
    subprocess.Popen([pythonw, script_path],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
    print(f"  · launched in background now")
    return 0


def cmd_uninstall():
    if PLATFORM == "Darwin":
        return _uninstall_macos()
    if PLATFORM == "Windows":
        return _uninstall_windows()
    print(f"--uninstall is supported only on macOS and Windows.", file=sys.stderr)
    return 1


def _uninstall_macos():
    if not LAUNCH_AGENT_PATH.exists() and not _agent_loaded():
        print("Not installed.")
        return 0
    if _agent_loaded():
        _launchctl("unload", "-w", str(LAUNCH_AGENT_PATH))
    LAUNCH_AGENT_PATH.unlink(missing_ok=True)
    print(f"✓ Uninstalled LaunchAgent {LAUNCH_AGENT_LABEL}.")
    return 0


def _uninstall_windows():
    startup = Path(os.environ.get("APPDATA", "")) / \
              "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    target = startup / WINDOWS_STARTUP_FILENAME
    if target.exists():
        target.unlink()
        print(f"✓ Removed {target}")
        return 0
    print("Not installed.")
    return 0


def cmd_status():
    if PLATFORM == "Darwin":
        installed = LAUNCH_AGENT_PATH.exists()
        loaded = _agent_loaded()
        print(f"Platform:     macOS")
        print(f"Plist file:   {'✓ ' + str(LAUNCH_AGENT_PATH) if installed else '✗ not installed'}")
        print(f"launchctl:    {'✓ loaded as ' + LAUNCH_AGENT_LABEL if loaded else '✗ not loaded'}")
        # Also report PIDs
        pids = _find_tray_pids()
        if pids:
            print(f"Running PIDs: {', '.join(map(str, pids))}")
        else:
            print(f"Running PIDs: (none)")
        return 0 if loaded else 1
    if PLATFORM == "Windows":
        startup = Path(os.environ.get("APPDATA", "")) / \
                  "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        target = startup / WINDOWS_STARTUP_FILENAME
        installed = target.exists()
        print(f"Platform:     Windows")
        print(f"Startup file: {'✓ ' + str(target) if installed else '✗ not installed'}")
        return 0 if installed else 1
    print(f"Status check not supported on {PLATFORM}.", file=sys.stderr)
    return 1


def cmd_restart_agent():
    if PLATFORM != "Darwin":
        print("--restart is currently macOS-only.", file=sys.stderr)
        return 1
    if not LAUNCH_AGENT_PATH.exists():
        print("Not installed. Run --install first.")
        return 1
    _launchctl("unload", str(LAUNCH_AGENT_PATH))
    time.sleep(0.5)
    rc, out = _launchctl("load", "-w", str(LAUNCH_AGENT_PATH))
    if rc == 0:
        print("✓ Tray service restarted.")
        return 0
    print(f"✗ Reload failed: {out}", file=sys.stderr)
    return 1


def _find_tray_pids():
    """Find all PIDs running this script (any mode)."""
    try:
        proc = subprocess.run(["pgrep", "-f", "atlas_tray.py"],
                              capture_output=True, text=True, timeout=3)
        if proc.returncode != 0:
            return []
        my_pid = os.getpid()
        return [int(p) for p in proc.stdout.split()
                if p.isdigit() and int(p) != my_pid]
    except Exception:
        return []


def _find_foreground_tray_pids():
    """Best-effort: PIDs of foreground tray instances (not the LaunchAgent one).
    We can't reliably distinguish, so we return all current PIDs minus ours; the
    LaunchAgent (if loaded) will simply be restarted by launchd."""
    return _find_tray_pids()


# ============================================================================
# Foreground run
# ============================================================================

import signal  # already used by _install_macos via os.kill — keep for that path


def run_foreground():
    if not ATLAS_CLI.exists():
        print(f"ERROR: {ATLAS_CLI} not found.", file=sys.stderr)
        sys.exit(1)

    print("Knowledge Atlas tray — running in foreground.")
    print("  · icon appears in your menu bar (macOS) / system tray (Windows)")
    print("  · Ctrl-C here will close it; closing this terminal will too")
    print("")
    print("For a persistent install that survives terminal close + reboots:")
    print("    python3 atlas_tray.py --install")
    print("")

    _ensure_icons()
    _state["running"] = _atlas_running()

    icon = Icon(
        "knowledge-atlas",
        ICON_GREEN if _state["running"] else ICON_RED,
        "Knowledge Atlas",
        menu=_build_menu(),
    )
    _state["icon"] = icon

    # Background polling thread keeps the icon + menu fresh
    t = threading.Thread(target=_polling_loop, daemon=True)
    t.start()

    # icon.run() blocks until on_quit() calls icon.stop()
    icon.run()


def main():
    ap = argparse.ArgumentParser(
        description="Cross-platform menu-bar / system-tray controller for "
                    "the Knowledge Atlas service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("USAGE")[1].split("ICON")[0].rstrip(),
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--install",   action="store_true",
                   help="Install persistently (macOS LaunchAgent / Windows Startup).")
    g.add_argument("--uninstall", action="store_true",
                   help="Remove the persistent install.")
    g.add_argument("--status",    action="store_true",
                   help="Show whether the persistent service is installed and running.")
    g.add_argument("--restart",   action="store_true",
                   help="Reload the LaunchAgent (macOS only).")
    args = ap.parse_args()

    if args.install:   return cmd_install()
    if args.uninstall: return cmd_uninstall()
    if args.status:    return cmd_status()
    if args.restart:   return cmd_restart_agent()

    # No flag → foreground run
    run_foreground()
    return 0


if __name__ == "__main__":
    sys.exit(main())
