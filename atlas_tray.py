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

Shows a colored icon in the menu bar (macOS) or system tray (Windows):
  · green = running
  · red   = stopped
  · amber = transitional (starting/stopping)

Right-click (or click on macOS) for a menu of actions:
  · Start / Restart / Stop
  · Open dashboard           → http://127.0.0.1:5179/ in browser
  · View logs                → opens data/atlas.log
  · Reveal in Finder/Explorer
  · Status (live atlas content stats)
  · Quit tray                (does NOT stop the atlas service)

The atlas service is launched by spawning `atlas.py start` as a subprocess,
so this app reuses the exact same lifecycle logic, PID file, and log file
as the CLI. You can mix-and-match: start from the tray, stop from the CLI.

Dependencies:
    pip install pystray pillow

Usage:
    python3 atlas_tray.py
"""

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
POLL_INTERVAL_SEC = 3
ICON_SIZE = 64  # rendered at high-res; OS scales down for menu bar

PLATFORM = platform.system()  # "Darwin" / "Windows" / "Linux"


# ---------- shared helpers (mirror atlas.py logic, no import to keep it light) -

import os
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

def main():
    if not ATLAS_CLI.exists():
        print(f"ERROR: {ATLAS_CLI} not found.", file=sys.stderr)
        sys.exit(1)

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


if __name__ == "__main__":
    main()
