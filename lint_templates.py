#!/usr/bin/env python3
# Knowledge Atlas — pre-commit JS-in-templates linter.
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
lint_templates.py — validate JavaScript embedded in Flask templates.

WHY THIS EXISTS
  We shipped a bug where a duplicate `const phases` declaration in the same
  function scope caused a strict-mode SyntaxError on page load — the boot
  IIFE never ran, the dashboard hung on "Loading…". `node --check` on the
  rendered HTML would have caught it instantly; the pre-commit hook wasn't
  validating template JS at all. This script closes that gap in Python.

WHAT IT DOES
  For every `templates/**.html` (or whatever paths you pass), extract each
  inline <script> block and check it for syntax errors using two strategies
  in order of strength:
    1. node --check     (most reliable; needs node on PATH)
    2. Pure-Python      (fallback: scope-aware duplicate-`const`/`let`/`var`
                         detection that catches the exact class of bug we
                         just hit, with no external dependency)

  Either strategy returns line/column pointing into the *full HTML file*,
  not the extracted script — so a developer can jump directly to the source.

USAGE
    python3 lint_templates.py                    # lint every templates/*.html
    python3 lint_templates.py templates/foo.html # lint specific files
    python3 lint_templates.py --staged           # only files staged for commit
                                                  (intended for pre-commit hook)

EXIT CODE
    0 = clean
    1 = at least one finding
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "templates"

# Capture the contents and start offset of each inline <script>...</script>.
# Skips <script src="..."> tags (those reference external JS, nothing to lint).
SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)[^>]*>(?P<body>.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)


# ============================================================================
# Strategy 1 — node --check
# ============================================================================

def _node_check(js_src):
    """Run `node --check` and return (ok: bool, error_msg: str)."""
    if not shutil.which("node"):
        return None, "node not available"
    try:
        proc = subprocess.run(
            ["node", "--check", "-"],
            input=js_src, capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, "node --check timed out"
    if proc.returncode == 0:
        return True, ""
    return False, proc.stderr.strip()


# ============================================================================
# Strategy 2 — pure-Python scope-aware duplicate-declaration check
#
# Walks the source character by character, maintaining:
#   - brace depth (the start of `{` we're inside)
#   - string state (single/double/backtick + template-expr depth via ${})
#   - comment state (// line, /* block */)
# For each `const X = ...`, `let X = ...`, `var X = ...` at the top level
# of its enclosing scope, record (scope_id, name). Duplicates within the
# same scope are flagged with line+column.
# ============================================================================

def _strip_strings_and_comments(js):
    """Yield (line_no, col_no, char) tuples for chars that are NOT inside a
    string literal, template literal, or comment. Backtick template literals
    are special: `${...}` expressions are *code* and yielded normally."""
    i = 0
    n = len(js)
    line = 1
    col = 1
    # State machine
    in_sq = False     # single-quote string
    in_dq = False     # double-quote string
    in_bt = 0         # backtick depth (0 = not in template)
    bt_expr = 0       # nesting depth of ${...} inside template literal
    in_line_c = False
    in_block_c = False
    while i < n:
        c = js[i]
        nxt = js[i + 1] if i + 1 < n else ""

        # End of line
        if c == "\n":
            line += 1; col = 0
            if in_line_c: in_line_c = False

        if in_line_c:
            i += 1; col += 1; continue
        if in_block_c:
            if c == "*" and nxt == "/":
                in_block_c = False; i += 2; col += 2; continue
            i += 1; col += 1; continue
        if in_sq:
            if c == "\\" and nxt:
                i += 2; col += 2; continue
            if c == "'":
                in_sq = False
            i += 1; col += 1; continue
        if in_dq:
            if c == "\\" and nxt:
                i += 2; col += 2; continue
            if c == '"':
                in_dq = False
            i += 1; col += 1; continue
        if in_bt:
            # template literal — characters are mostly string EXCEPT inside ${...}
            if bt_expr > 0:
                # Inside ${...} — these are real code; yield them
                if c == "{":
                    bt_expr += 1
                elif c == "}":
                    bt_expr -= 1
                    if bt_expr == 0:
                        i += 1; col += 1; continue
                yield (line, col, c)
                i += 1; col += 1; continue
            if c == "\\" and nxt:
                i += 2; col += 2; continue
            if c == "$" and nxt == "{":
                bt_expr = 1
                i += 2; col += 2; continue
            if c == "`":
                in_bt -= 1
            i += 1; col += 1; continue

        # Not in any string/comment
        if c == "/" and nxt == "/":
            in_line_c = True; i += 2; col += 2; continue
        if c == "/" and nxt == "*":
            in_block_c = True; i += 2; col += 2; continue
        if c == "'":
            in_sq = True; i += 1; col += 1; continue
        if c == '"':
            in_dq = True; i += 1; col += 1; continue
        if c == "`":
            in_bt += 1; i += 1; col += 1; continue

        yield (line, col, c)
        i += 1; col += 1


def _python_lint_duplicates(js):
    """Scope-aware duplicate-`const`/`let`/`var` detector.
    Returns a list of (line, col, message) findings."""
    # Build a string-stripped representation along with original positions.
    cleaned = []
    positions = []
    for line, col, ch in _strip_strings_and_comments(js):
        cleaned.append(ch)
        positions.append((line, col))
    text = "".join(cleaned)

    findings = []
    decls_per_scope = [{}]   # stack of dicts: scope -> name -> (line, col)
    scope_id = [0]
    next_id = [1]

    i = 0
    n = len(text)
    DECL_KEYWORDS = ("const", "let", "var")

    while i < n:
        c = text[i]
        if c == "{":
            scope_id.append(next_id[0])
            next_id[0] += 1
            decls_per_scope.append({})
            i += 1
            continue
        if c == "}":
            if len(decls_per_scope) > 1:
                decls_per_scope.pop()
                scope_id.pop()
            i += 1
            continue

        # Detect `const|let|var <ident>` at a word boundary
        if c.isalpha() or c == "_":
            # Read full identifier/keyword
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            word = text[i:j]
            if word in DECL_KEYWORDS:
                # Skip whitespace
                k = j
                while k < n and text[k] in " \t\n\r":
                    k += 1
                # Skip destructuring patterns for now (`const {a,b} =`); only
                # check the simple `const NAME = ...` case
                if k < n and (text[k].isalpha() or text[k] == "_" or text[k] == "$"):
                    m = k
                    while m < n and (text[m].isalnum() or text[m] in "_$"):
                        m += 1
                    name = text[k:m]
                    current_scope = decls_per_scope[-1]
                    if name in current_scope:
                        prev_line, prev_col = current_scope[name]
                        ln, co = positions[k]
                        findings.append((
                            ln, co,
                            f"redeclaration of '{name}' in same scope "
                            f"(first declared at line {prev_line}:{prev_col})",
                        ))
                    else:
                        current_scope[name] = positions[k]
                    i = m
                    continue
            i = j
            continue

        i += 1

    return findings


# ============================================================================
# Driver
# ============================================================================

def lint_one(path):
    """Returns a list of (script_line_start, lint_line, lint_col, msg)."""
    html = path.read_text(encoding="utf-8", errors="replace")
    out = []
    for m in SCRIPT_RE.finditer(html):
        body = m.group("body")
        if not body.strip():
            continue
        script_start = html[: m.start("body")].count("\n") + 1

        ok, err = _node_check(body)
        if ok is True:
            continue
        if ok is False:
            # node found a syntax error
            # node's error message includes "line:col" in its output; surface it raw
            out.append((script_start, None, None, err))
            continue

        # node unavailable → fallback to Python scope-aware check
        dups = _python_lint_duplicates(body)
        for ln, co, msg in dups:
            out.append((script_start, ln, co, msg))
    return out


def files_to_lint(args):
    if args.staged:
        try:
            res = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRT"],
                text=True, stderr=subprocess.DEVNULL,
            )
        except Exception:
            return []
        return [Path(p) for p in res.splitlines()
                if p.strip() and p.lower().endswith((".html", ".htm"))]
    if args.paths:
        return [Path(p) for p in args.paths
                if p.lower().endswith((".html", ".htm"))]
    return sorted(TEMPLATE_DIR.glob("*.html"))


def main():
    ap = argparse.ArgumentParser(
        description="Validate JavaScript embedded in HTML templates.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--staged", action="store_true",
                    help="only scan files staged for the next commit")
    ap.add_argument("paths", nargs="*", help="specific files to scan")
    args = ap.parse_args()

    files = files_to_lint(args)
    if not files:
        print("lint_templates: nothing to scan.")
        return 0

    strategy = "node --check" if shutil.which("node") else "pure-Python scope check"
    print(f"lint_templates: scanning {len(files)} file(s) via {strategy}")

    total = 0
    for f in files:
        findings = lint_one(f)
        if not findings:
            print(f"  ✓ {f}")
            continue
        for (script_start, ln, co, msg) in findings:
            total += 1
            if ln is not None:
                # in-file line number = script_start + lint_line - 1
                abs_line = script_start + ln - 1
                print(f"  ✗ {f}:{abs_line}:{co}  {msg}")
            else:
                print(f"  ✗ {f}  (script begins at line {script_start})")
                for line in msg.splitlines()[:8]:
                    print(f"        {line}")

    if total == 0:
        print("\n✓ template JS clean")
        return 0
    print(f"\nlint_templates BLOCKED — {total} issue(s) above. Fix and re-stage.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
