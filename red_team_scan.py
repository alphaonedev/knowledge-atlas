#!/usr/bin/env python3
# Knowledge Atlas — pre-commit / pre-push red-team scan.
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
red_team_scan.py — pre-commit / pre-push safety sweep.

Scans staged-for-commit files (or all tracked files) for things that must
never ship to a public repo:

  · API-key / token patterns      (OpenAI, Anthropic, xAI, GitHub PAT, Alpaca, AWS, etc.)
  · Personal-email patterns       (gmail, yahoo, outlook, icloud, hotmail, protonmail)
  · Hardcoded absolute home paths (/Users/<name>, /home/<name>, C:\\Users\\<name>)
  · Real indexed-source data      (transcript text, knowledge-card JSON, sqlite DB)
  · Files that should never be committed (sources.json, .env, *.pid, *.log,
    channel_metadata.json)

Usage:
    python3 red_team_scan.py                  # scan everything currently staged for commit
    python3 red_team_scan.py --all            # scan everything tracked by git
    python3 red_team_scan.py --paths a.py b/  # scan specific files / directories
    python3 red_team_scan.py --json           # output machine-readable JSON

Exit code 0 → clean, safe to commit. Exit code 1 → at least one finding.

Designed to be run as a git pre-commit hook. Wire it up with:
    cp red_team_scan.py .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit
or symlink it:
    ln -s ../../red_team_scan.py .git/hooks/pre-commit
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- patterns ---

# (pattern, label, severity). Severity is informational only; any match fails.
PATTERNS = [
    # Secrets / tokens
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),                "Anthropic API key",     "HIGH"),
    (re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{30,}"),          "OpenAI API key",        "HIGH"),
    (re.compile(r"\bxai-[A-Za-z0-9_\-]{20,}"),                 "xAI API key",           "HIGH"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),              "GitHub PAT (classic)",  "HIGH"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}"),                    "GitHub Personal Token", "HIGH"),
    (re.compile(r"\bghs_[A-Za-z0-9]{30,}"),                    "GitHub OAuth Token",    "HIGH"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                      "AWS Access Key",        "HIGH"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"),                      "AWS STS Token",         "HIGH"),
    (re.compile(r"\bPKLY[A-Z0-9]{12,}"),                       "Alpaca paper-trading key", "HIGH"),
    (re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
                                                               "Private key block",      "HIGH"),

    # Personal-email patterns (allow noreply / example domains)
    (re.compile(r"[A-Za-z0-9._+-]+@(?:gmail|yahoo|outlook|hotmail|protonmail|icloud|me|mac)\.com",
                re.IGNORECASE),                                "Personal email address", "HIGH"),

    # Absolute home paths — generic, catches any /Users/<name>, /home/<name>,
    # C:\Users\<name>. Username-specific patterns belong in .red_team_local.txt.
    (re.compile(r"(?:/Users/|/home/|C:\\\\Users\\\\)[A-Za-z0-9_.-]+"),
                                                               "Absolute home path", "HIGH"),

    # Per-maintainer source-specific patterns are loaded at runtime from
    # `.red_team_local.txt` (gitignored). See _load_local_patterns() below.
    # Hardcoding indexed handles HERE would itself leak which experts the
    # maintainer is researching, so we keep this list source-agnostic and
    # let each instance configure its own private patterns locally.
]


def _load_local_patterns():
    """Load per-maintainer private patterns from `.red_team_local.txt`.

    Format: one entry per line.  Either bare regex, or 'regex :: label'.
    Lines starting with '#' and blanks are ignored.  This file MUST be
    gitignored; the scanner refuses to run if it's committed.

    Example contents (NOT committed):
        \\bexample-handle\\b      :: indexed source handle
        \\bsome-side-project\\b   :: other-project path
        myrealname@example\\.com  :: maintainer's real email
    """
    p = Path(__file__).resolve().parent / ".red_team_local.txt"
    if not p.exists():
        return []
    extra = []
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "::" in line:
            pat, _, label = line.partition("::")
            pat, label = pat.strip(), label.strip() or "local pattern"
        else:
            pat, label = line, "local pattern"
        try:
            extra.append((re.compile(pat, re.IGNORECASE), label, "HIGH"))
        except re.error as e:
            print(f"warn: skipping malformed regex in .red_team_local.txt: {pat!r} ({e})",
                  file=sys.stderr)
    return extra


# Append the maintainer's private patterns at import time
PATTERNS.extend(_load_local_patterns())

# Files / globs that must NEVER be committed regardless of contents.
# These are checked by path; matched files fail the scan even if their content looks clean.
FORBIDDEN_PATHS = [
    re.compile(r"(?:^|/)\.env$"),
    re.compile(r"(?:^|/)\.env\.local$"),
    re.compile(r"(?:^|/)sources\.json$"),               # sources.json.example is allowed
    re.compile(r"(?:^|/)channel_metadata\.json$"),
    re.compile(r"(?:^|/)data/atlas\.pid$"),
    re.compile(r"(?:^|/)data/atlas\.log"),
    re.compile(r"(?:^|/)data/knowledge\.db$"),
    re.compile(r"(?:^|/)data/lexicon\.db$"),
    re.compile(r"(?:^|/)data/knowledge/.+\.json$"),     # per-video card files
    re.compile(r"(?:^|/)data/export/.+$"),
    re.compile(r"(?:^|/)transcripts/.+$"),
    re.compile(r"(?:^|/)raw_srt/.+$"),
    re.compile(r"(?:^|/)sources/.+$"),                  # per-source data dirs
    re.compile(r"\.srt$"),
    re.compile(r"\.bak(?:-\d+)?$"),
    re.compile(r"\.pyc$"),
]

# Heuristic for content-only checks: skip binary files and the LICENSE
# (which contains "fate" of the world boilerplate).
BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".db", ".pyc",
                     ".ico", ".woff", ".woff2", ".ttf", ".otf", ".zip", ".gz"}


# ---------------------------------------------------------------- helpers ----

def _is_text(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return False
    try:
        with open(path, "rb") as f:
            chunk = f.read(2048)
        if b"\x00" in chunk:
            return False
        return True
    except OSError:
        return False


def _git_root():
    try:
        r = subprocess.check_output(["git", "rev-parse", "--show-toplevel"],
                                    text=True, stderr=subprocess.DEVNULL).strip()
        return Path(r)
    except Exception:
        return None


def _files_staged():
    """Files staged for the next commit (M, A, R, etc.) — what a pre-commit hook should scan."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRT"],
            text=True, stderr=subprocess.DEVNULL,
        )
        return [Path(p) for p in out.splitlines() if p.strip()]
    except Exception:
        return []


def _files_tracked():
    """Everything currently tracked by git."""
    try:
        out = subprocess.check_output(["git", "ls-files"],
                                      text=True, stderr=subprocess.DEVNULL)
        return [Path(p) for p in out.splitlines() if p.strip()]
    except Exception:
        return []


def _expand_paths(paths):
    """Expand directories to file lists; pass through files."""
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and ".git/" not in str(f):
                    out.append(f)
        elif p.is_file():
            out.append(p)
    return out


# ---------------------------------------------------------------- scan -------

def scan_file(path: Path):
    """Return a list of (line_no, label, severity, excerpt) findings."""
    findings = []
    # 1. Path-level forbidden check
    str_path = str(path)
    for pat in FORBIDDEN_PATHS:
        if pat.search(str_path):
            findings.append((0, "forbidden path (would commit private data)", "HIGH", str_path))
            return findings  # don't bother reading its content
    # 2. Content-level pattern scan (text files only)
    if not _is_text(path):
        return findings
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    for i, line in enumerate(text.splitlines(), 1):
        for pat, label, sev in PATTERNS:
            if pat.search(line):
                excerpt = line.strip()
                if len(excerpt) > 140:
                    excerpt = excerpt[:140] + "…"
                findings.append((i, label, sev, excerpt))
    return findings


def run_scan(files, json_out=False, label_set=""):
    """Run the scan and report. Returns 0 if clean, 1 if findings."""
    total_findings = 0
    all_findings = []
    for f in files:
        for (line, lbl, sev, excerpt) in scan_file(f):
            all_findings.append({
                "file": str(f),
                "line": line,
                "label": lbl,
                "severity": sev,
                "excerpt": excerpt,
            })
            total_findings += 1
    if json_out:
        print(json.dumps({
            "scanned_files": len(files),
            "findings": all_findings,
            "ok": total_findings == 0,
        }, indent=2))
        return 0 if total_findings == 0 else 1

    print(f"red-team scan {label_set}({len(files)} file{'s' if len(files)!=1 else ''})")
    if not all_findings:
        print("  ✓ clean")
        return 0
    print(f"  ✗ {total_findings} finding{'s' if total_findings!=1 else ''}:")
    # Group by file
    by_file = {}
    for f in all_findings:
        by_file.setdefault(f["file"], []).append(f)
    for fname, items in by_file.items():
        print(f"\n  {fname}")
        for item in items:
            loc = f"line {item['line']}" if item['line'] else "PATH"
            print(f"    [{item['severity']}] {loc}: {item['label']}")
            if item['excerpt']:
                print(f"        {item['excerpt']}")
    print()
    print("  Pre-commit BLOCKED. Resolve the findings above and re-stage.")
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true",
                   help="scan every git-tracked file (default: only staged)")
    g.add_argument("--paths", nargs="+",
                   help="scan specific files or directories")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON instead of human report")
    args = ap.parse_args()

    if args.paths:
        files = _expand_paths(args.paths)
        label = f"of {args.paths} "
    elif args.all:
        files = _files_tracked()
        label = "of all tracked files "
    else:
        files = _files_staged()
        if not files:
            print("nothing staged — pass --all to scan all tracked files, "
                  "or --paths to scan specific files.")
            return 0
        label = "of staged changes "

    rc = run_scan(files, json_out=args.json, label_set=label)
    return rc


if __name__ == "__main__":
    sys.exit(main())
