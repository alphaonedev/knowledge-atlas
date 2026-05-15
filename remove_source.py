#!/usr/bin/env python3
# Knowledge Atlas — remove a previously indexed source (CLI + library).
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
remove_source.py — destructively remove a knowledge source from the atlas.

What it removes:
  1. The entry in sources.json
  2. Every per-video card JSON in data/knowledge/<vid>.json that belongs to
     this source (looked up from the source's channel_metadata.json so we
     never touch another source's cards)
  3. The entire sources/<source_id>/ tree (transcripts, raw_srt, metadata)
  4. Rebuilds the unified SQLite atlas afterwards via build_knowledge.py

Usage:
    python3 remove_source.py --id <source_id>                  # interactive
    python3 remove_source.py --id <source_id> --confirm        # no prompt
    python3 remove_source.py --id <source_id> --confirm --dry-run

This is irreversible. A dry-run (--dry-run) prints exactly what would be
deleted without touching anything.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES_PATH = ROOT / "sources.json"
KNOWLEDGE_DIR = ROOT / "data" / "knowledge"
SOURCES_DIR = ROOT / "sources"


def plan_removal(source_id):
    """Return a dict of everything that would be touched, without doing it."""
    plan = {
        "source_id": source_id,
        "in_registry": False,
        "source_dir": str(SOURCES_DIR / source_id),
        "source_dir_exists": (SOURCES_DIR / source_id).exists(),
        "video_ids": [],
        "card_files": [],
        "transcript_count": 0,
        "raw_srt_count": 0,
    }

    # Check registry
    if SOURCES_PATH.exists():
        doc = json.loads(SOURCES_PATH.read_text())
        for s in doc.get("sources", []):
            if s.get("id") == source_id:
                plan["in_registry"] = True
                break

    # Look up the videos belonging to this source from its channel_metadata
    meta_path = SOURCES_DIR / source_id / "channel_metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            plan["video_ids"] = [m["id"] for m in meta if m.get("id")]
        except Exception:
            pass

    # Find the corresponding card files in data/knowledge/
    for vid in plan["video_ids"]:
        card = KNOWLEDGE_DIR / f"{vid}.json"
        if card.exists():
            plan["card_files"].append(str(card))

    # Count files that will go with the directory removal
    tx_dir = SOURCES_DIR / source_id / "transcripts"
    if tx_dir.exists():
        plan["transcript_count"] = len([p for p in tx_dir.glob("*.txt")
                                        if not p.name.endswith(".timed.txt")])
    raw = SOURCES_DIR / source_id / "raw_srt"
    if raw.exists():
        plan["raw_srt_count"] = len(list(raw.glob("*.srt")))

    return plan


def remove(source_id, dry_run=False, rebuild=True, log=print):
    """Execute the removal plan. Returns the plan dict augmented with results.
    If dry_run is True, returns the plan without performing any deletion.
    Set rebuild=False to skip the build_knowledge.py rebuild step (caller can
    do it themselves, e.g. in batch operations)."""
    plan = plan_removal(source_id)

    if not plan["in_registry"] and not plan["source_dir_exists"] and not plan["card_files"]:
        plan["actions"] = []
        plan["status"] = "nothing-to-remove"
        log(f"  Source '{source_id}' not found in registry, source dir, or card files.")
        return plan

    actions = []
    log(f"Removal plan for '{source_id}':")
    log(f"  · In registry:        {'yes' if plan['in_registry'] else 'no'}")
    log(f"  · Source dir:         {plan['source_dir']} "
        f"({'exists' if plan['source_dir_exists'] else 'absent'})")
    log(f"  · Videos in source:   {len(plan['video_ids'])}")
    log(f"  · Card JSON files:    {len(plan['card_files'])}")
    log(f"  · Transcript .txt:    {plan['transcript_count']}")
    log(f"  · Raw .srt files:     {plan['raw_srt_count']}")
    if dry_run:
        plan["actions"] = []
        plan["status"] = "dry-run"
        log("Dry-run only — no files touched.")
        return plan

    # 1. Remove entry from sources.json
    if plan["in_registry"]:
        doc = json.loads(SOURCES_PATH.read_text())
        before = len(doc.get("sources", []))
        doc["sources"] = [s for s in doc.get("sources", []) if s.get("id") != source_id]
        after = len(doc["sources"])
        SOURCES_PATH.write_text(json.dumps(doc, indent=2))
        actions.append(f"sources.json: removed (was {before} entries, now {after})")
        log(f"  ✓ removed from sources.json")

    # 2. Remove each card JSON file
    deleted_cards = 0
    for c in plan["card_files"]:
        try:
            Path(c).unlink()
            deleted_cards += 1
        except OSError:
            pass
    if deleted_cards:
        actions.append(f"deleted {deleted_cards} card files in data/knowledge/")
        log(f"  ✓ deleted {deleted_cards} card JSON files")

    # 3. Remove the per-source directory tree
    src_dir = Path(plan["source_dir"])
    if src_dir.exists():
        try:
            shutil.rmtree(src_dir)
            actions.append(f"deleted source tree {src_dir}")
            log(f"  ✓ deleted source tree {src_dir}")
        except OSError as e:
            log(f"  ✗ could not remove {src_dir}: {e}")

    # 4. Rebuild the unified atlas
    if rebuild:
        log("Rebuilding unified atlas via build_knowledge.py…")
        try:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "build_knowledge.py")],
                cwd=str(ROOT), capture_output=True, text=True, check=False,
            )
            if proc.returncode == 0:
                actions.append("rebuilt knowledge.db")
                log("  ✓ atlas rebuilt")
            else:
                log(f"  ✗ build_knowledge.py exited {proc.returncode}")
                log(proc.stdout[-400:])
                log(proc.stderr[-400:])
        except Exception as e:
            log(f"  ✗ rebuild error: {e}")

    plan["actions"] = actions
    plan["status"] = "removed"
    return plan


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--id", required=True, help="source_id to remove")
    ap.add_argument("--confirm", action="store_true",
                    help="skip the interactive 'are you sure?' prompt")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan without deleting anything")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="skip the build_knowledge.py rebuild step")
    args = ap.parse_args()

    plan = plan_removal(args.id)
    if not plan["in_registry"] and not plan["source_dir_exists"] and not plan["card_files"]:
        print(f"Source '{args.id}' not found.", file=sys.stderr)
        sys.exit(1)

    print(f"About to remove source '{args.id}':")
    print(f"  Source dir:      {plan['source_dir']}")
    print(f"  In registry:     {'yes' if plan['in_registry'] else 'no'}")
    print(f"  Card files:      {len(plan['card_files'])}")
    print(f"  Transcripts:     {plan['transcript_count']}")
    print(f"  Raw .srt:        {plan['raw_srt_count']}")
    print()
    if args.dry_run:
        print("Dry-run only — no files touched.")
        return 0
    if not args.confirm:
        try:
            ans = input("Type the source id to confirm removal: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if ans != args.id:
            print("Confirmation did not match. Aborted.")
            return 1

    result = remove(args.id, dry_run=False, rebuild=not args.no_rebuild)
    if result.get("status") == "removed":
        print(f"\n✓ Source '{args.id}' removed.")
        return 0
    print(f"\n✗ Removal incomplete: {result.get('status')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
