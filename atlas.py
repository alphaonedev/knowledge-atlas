#!/usr/bin/env python3
# Knowledge Atlas — service lifecycle CLI (start | stop | restart | status | logs).
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
atlas.py — simple service-lifecycle CLI for the Knowledge Atlas Flask server.

Usage:
    python3 atlas.py start          # launch in background (writes PID + log)
    python3 atlas.py stop           # SIGTERM the running server, then SIGKILL if stuck
    python3 atlas.py restart        # stop, then start
    python3 atlas.py status         # PID + uptime + port + atlas content stats
    python3 atlas.py logs           # tail the server log (default 50 lines)
    python3 atlas.py logs -n 200    # tail more lines
    python3 atlas.py logs -f        # follow (Ctrl-C to exit)
    python3 atlas.py export                          # full atlas JSON to stdout
    python3 atlas.py export -o atlas.json            # full atlas JSON to a file
    python3 atlas.py export --source allin -o a.json # one source only
    python3 atlas.py export --rebuild -o atlas.json  # rebuild first, then dump

State files (gitignored):
    data/atlas.pid                  PID of the running Flask server
    data/atlas.log                  combined stdout/stderr of the Flask server

The MCP server is launched on demand by MCP clients (Claude Desktop, Claude
Code, etc.) and is NOT managed by this CLI — it lives/dies with its client.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PID_FILE = DATA_DIR / "atlas.pid"
LOG_FILE = DATA_DIR / "atlas.log"
APP_FILE = ROOT / "app.py"
URL = "http://127.0.0.1:5179"
PORT = 5179

# ANSI color codes (skipped if not a TTY)
def _ansi(code):
    return code if sys.stdout.isatty() else ""
GREEN  = _ansi("\033[32m")
RED    = _ansi("\033[31m")
YELLOW = _ansi("\033[33m")
BLUE   = _ansi("\033[34m")
DIM    = _ansi("\033[2m")
BOLD   = _ansi("\033[1m")
RESET  = _ansi("\033[0m")


# ---------------- helpers ----------------------------------------------------

def _read_pid():
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
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
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False
    finally:
        s.close()


def _wait_for_port(timeout=15):
    """Block until 127.0.0.1:PORT responds, or timeout. Returns bool."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open():
            return True
        time.sleep(0.25)
    return False


def _http_get_json(path, timeout=2):
    try:
        with urllib.request.urlopen(URL + path, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _human_duration(seconds):
    delta = timedelta(seconds=int(seconds))
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if mins: parts.append(f"{mins}m")
    if not parts: parts.append(f"{secs}s")
    return " ".join(parts)


def _terminate(pid, grace=5):
    """SIGTERM the process group, wait `grace` seconds, then SIGKILL."""
    if not _alive(pid):
        return True
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        pgid = pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return True
    deadline = time.time() + grace
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.2)
    # Hard kill
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    time.sleep(0.4)
    return not _alive(pid)


# ---------------- commands ---------------------------------------------------

def cmd_start():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"{YELLOW}Already running{RESET} (PID {pid}, {URL})")
        return 0
    if pid and not _alive(pid):
        PID_FILE.unlink(missing_ok=True)
        print(f"{DIM}stale PID file removed{RESET}")
    if _port_open():
        print(f"{RED}Port {PORT} is already in use by another process — "
              "cannot start.{RESET}")
        return 1
    if not APP_FILE.exists():
        print(f"{RED}{APP_FILE} not found.{RESET}")
        return 1

    print(f"Starting Atlas service…")
    # Open log file in append mode; rotate by truncation if it gets huge
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 10 * 1024 * 1024:
        rotated = LOG_FILE.with_suffix(".log.1")
        LOG_FILE.replace(rotated)
        print(f"{DIM}rotated log → {rotated.name} (>10MB){RESET}")
    log = open(LOG_FILE, "a", encoding="utf-8")
    log.write(f"\n=== atlas start at {datetime.now().isoformat(timespec='seconds')} ===\n")
    log.flush()

    proc = subprocess.Popen(
        [sys.executable, str(APP_FILE)],
        cwd=str(ROOT),
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,  # own process group; clean kill of subprocesses
        close_fds=True,
    )
    PID_FILE.write_text(str(proc.pid))

    print(f"  PID:  {proc.pid}")
    print(f"  log:  {LOG_FILE}")
    print(f"  URL:  {URL}")
    print(f"Waiting for server to bind port {PORT}…")
    if _wait_for_port(timeout=15):
        summary = _http_get_json("/api/source") or {}
        print(f"{GREEN}✓ Atlas is up.{RESET}")
        if summary.get("name"):
            print(f"  {summary['name']} · {summary.get('cards', '?')} cards · "
                  f"{summary.get('videos', '?')} videos · "
                  f"{summary.get('categories', '?')} topics")
        return 0
    else:
        print(f"{RED}✗ Server did not bind port {PORT} within 15s. "
              f"Check {LOG_FILE} for errors.{RESET}")
        return 2


def cmd_stop():
    pid = _read_pid()
    if not pid:
        if _port_open():
            print(f"{YELLOW}Port {PORT} is in use but no PID file found. "
                  f"Something else is bound to this port — not stopping it.{RESET}")
            return 1
        print("Atlas is not running.")
        return 0
    if not _alive(pid):
        PID_FILE.unlink(missing_ok=True)
        print(f"Atlas was not running (stale PID {pid} removed).")
        return 0
    print(f"Stopping Atlas (PID {pid})…")
    ok = _terminate(pid)
    PID_FILE.unlink(missing_ok=True)
    if ok:
        print(f"{GREEN}✓ Stopped.{RESET}")
        return 0
    print(f"{RED}✗ Could not stop process {pid} cleanly.{RESET}")
    return 1


def cmd_restart():
    cmd_stop()
    # Brief settle so the OS releases the port
    for _ in range(20):
        if not _port_open():
            break
        time.sleep(0.1)
    return cmd_start()


def cmd_status():
    pid = _read_pid()
    alive = _alive(pid)
    port = _port_open()
    if not pid:
        print(f"{DIM}● {RESET}Atlas is {RED}not running{RESET}.")
        if port:
            print(f"  {YELLOW}Note: port {PORT} is in use by an external process.{RESET}")
        return 1
    if not alive:
        print(f"{DIM}● {RESET}Atlas is {RED}not running{RESET} "
              f"(stale PID file: {pid}).")
        return 1

    # Uptime = age of the PID file (close enough)
    try:
        started = datetime.fromtimestamp(PID_FILE.stat().st_mtime)
        uptime = (datetime.now() - started).total_seconds()
    except OSError:
        started, uptime = None, None

    print(f"{GREEN}● {RESET}Atlas is {GREEN}running{RESET}.")
    print(f"  PID:    {pid}")
    if started:
        print(f"  Up:     {_human_duration(uptime)}  ({BOLD}since {started.strftime('%Y-%m-%d %H:%M:%S')}{RESET})")
    print(f"  URL:    {URL}")
    print(f"  Port:   {PORT} {'(reachable)' if port else f'{RED}(NOT reachable){RESET}'}")
    print(f"  Log:    {LOG_FILE}")

    # Atlas content summary
    summary = _http_get_json("/api/source") if port else None
    if summary:
        print(f"  Source: {summary.get('name', '?')}")
        print(f"  Atlas:  {summary.get('cards', '?')} cards · "
              f"{summary.get('videos', '?')} videos · "
              f"{summary.get('categories', '?')} topics")
    # MCP availability is informational — the MCP server is per-client, not managed here
    if (DATA_DIR / "knowledge.db").exists():
        print(f"  DB:     data/knowledge.db ({(DATA_DIR / 'knowledge.db').stat().st_size // 1024} KB)")
    return 0


def cmd_export(args):
    """Dump the unified atlas as JSON, either to stdout or a file.

    No service needs to be running — the export reads `data/export/knowledge_atlas.json`
    directly (rebuilt every time `build_knowledge.py` runs, which the ingest
    pipeline does after every refresh). Pass --rebuild to force a fresh build
    before dumping.
    """
    export_path = DATA_DIR / "export" / "knowledge_atlas.json"

    if args.rebuild or not export_path.exists():
        if not export_path.exists():
            print(f"{DIM}no export on disk yet — running build_knowledge.py…{RESET}",
                  file=sys.stderr)
        else:
            print(f"{DIM}--rebuild: regenerating export via build_knowledge.py…{RESET}",
                  file=sys.stderr)
        r = subprocess.run(
            [sys.executable, str(ROOT / "build_knowledge.py")],
            cwd=str(ROOT),
        )
        if r.returncode != 0:
            print(f"{RED}✗ build_knowledge.py exited {r.returncode}{RESET}",
                  file=sys.stderr)
            return r.returncode

    if not export_path.exists():
        print(f"{RED}✗ Export file not found at {export_path}{RESET}", file=sys.stderr)
        return 1

    raw = export_path.read_text(encoding="utf-8")

    # Per-source filter — parse, slice, re-serialize.
    if args.source:
        try:
            atlas = json.loads(raw)
        except Exception as e:
            print(f"{RED}✗ Could not parse export: {e}{RESET}", file=sys.stderr)
            return 1
        sids = sorted((atlas.get("cards_by_source") or {}).keys())
        if args.source not in sids:
            print(f"{RED}✗ Unknown source_id `{args.source}`{RESET}", file=sys.stderr)
            print(f"  Available: {sids}", file=sys.stderr)
            return 1
        cards = atlas["cards_by_source"][args.source]
        by_kind, by_cat = {}, {}
        for c in cards:
            by_kind.setdefault(c.get("kind") or "unknown", []).append(c)
            by_cat.setdefault(c.get("category") or "general", []).append(c)
        src_records = [s for s in atlas.get("sources", []) if s.get("id") == args.source]
        videos = [v for v in atlas.get("videos", []) if v.get("source_id") == args.source]
        subset = {
            "manifest": {
                **(atlas.get("manifest") or {}),
                "filtered_to_source": args.source,
                "totals": {
                    "sources": 1,
                    "videos": len(videos),
                    "cards": len(cards),
                    "categories": len(by_cat),
                    "kind_counts": {k: len(v) for k, v in by_kind.items()},
                    "category_counts": {k: len(v) for k, v in by_cat.items()},
                },
            },
            "sources": src_records,
            "videos": videos,
            "cards_by_kind": by_kind,
            "cards_by_category": by_cat,
            "cards_by_source": {args.source: cards},
            "source": src_records[0] if src_records else None,
        }
        raw = json.dumps(subset, indent=2, default=str)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(raw, encoding="utf-8")
        size_mb = len(raw) / 1024 / 1024
        scope = f"source `{args.source}`" if args.source else "full atlas"
        print(f"{GREEN}✓ Wrote {scope} as JSON ({size_mb:.2f} MB) to {out}{RESET}",
              file=sys.stderr)
    else:
        sys.stdout.write(raw)
        if not raw.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def cmd_logs(args):
    if not LOG_FILE.exists():
        print(f"{DIM}no log file yet at {LOG_FILE}{RESET}")
        return 0
    if args.follow:
        os.execvp("tail", ["tail", "-n", str(args.n), "-f", str(LOG_FILE)])
    else:
        # Plain tail
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-args.n:]:
            sys.stdout.write(line)
    return 0


# ---------------- entry ------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Service-lifecycle CLI for the Knowledge Atlas Flask server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1].split("State files:")[0],
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start",   help="Launch the Atlas service in the background.")
    sub.add_parser("stop",    help="Stop the running Atlas service.")
    sub.add_parser("restart", help="Stop then start.")
    sub.add_parser("status",  help="Show PID, uptime, port, and atlas content stats.")
    p_logs = sub.add_parser("logs", help="Tail the server log.")
    p_logs.add_argument("-n", type=int, default=50, help="number of lines (default 50)")
    p_logs.add_argument("-f", "--follow", action="store_true",
                        help="follow the log (like tail -f)")
    p_export = sub.add_parser("export",
        help="Export the atlas as JSON to stdout or a file.")
    p_export.add_argument("-o", "--output",
        help="Write to this file instead of stdout.")
    p_export.add_argument("--source",
        help="Filter to a single source_id (default: full corpus).")
    p_export.add_argument("--rebuild", action="store_true",
        help="Run build_knowledge.py first to regenerate the export file.")
    args = ap.parse_args()

    if args.cmd == "start":   return cmd_start()
    if args.cmd == "stop":    return cmd_stop()
    if args.cmd == "restart": return cmd_restart()
    if args.cmd == "status":  return cmd_status()
    if args.cmd == "logs":    return cmd_logs(args)
    if args.cmd == "export":  return cmd_export(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
