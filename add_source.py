#!/usr/bin/env python3
# Knowledge Atlas — One-shot CLI to register a new source and run the fetch pipeline.
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
add_source.py — one-shot ingestion of a new domain expert (any YouTube channel).

This is the repeatable pipeline entry point. Run it once per new podcast /
channel / expert; everything else (correlation, search, AI surface) updates
automatically because the schema is fixed.

Usage:
    python3 add_source.py \
        --id alexhormozi \
        --name "Alex Hormozi" \
        --url "https://www.youtube.com/@AlexHormozi" \
        --domain "business, sales, marketing, scaling" \
        --expertise "$100M offers, lead generation, business acquisition"

What it does:
  1. Adds an entry to sources.json (idempotent).
  2. Runs fetch_channel.py against the channel — writes transcripts into
     sources/<id>/transcripts/.
  3. Reminds you to run the knowledge-card extraction step. Extraction is
     the only step that genuinely needs an LLM — either:
       a) point your Claude/agent loop at the new transcripts using
          data/knowledge/SCHEMA.md as the contract, OR
       b) run extract_knowledge.py if you've wired in an Anthropic API key.
  4. Once <id>.json files exist in data/knowledge/, re-running
     build_knowledge.py rebuilds the unified atlas.

The atlas (knowledge.db) is rebuilt holistically each run — there is no
per-source DB. Every card has a `source_id`; every endpoint can filter,
group, or correlate by it.
"""

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES_PATH = ROOT / "sources.json"


def upsert_source(args):
    doc = json.loads(SOURCES_PATH.read_text())
    sources = doc.setdefault("sources", [])
    for s in sources:
        if s["id"] == args.id:
            print(f"  source '{args.id}' already in sources.json — updating fields")
            s.update({
                "name": args.name or s.get("name"),
                "kind": args.kind or s.get("kind", "youtube_channel"),
                "url": args.url or s.get("url"),
                "domain": args.domain or s.get("domain"),
                "expertise": args.expertise or s.get("expertise"),
                "language": args.language or s.get("language", "en"),
                "license": args.license or s.get("license"),
                "trust_notes": args.trust_notes or s.get("trust_notes"),
            })
            break
    else:
        sources.append({
            "id": args.id,
            "name": args.name,
            "kind": args.kind,
            "url": args.url,
            "domain": args.domain,
            "expertise": args.expertise,
            "language": args.language,
            "first_indexed": str(date.today()),
            "license": args.license,
            "trust_notes": args.trust_notes,
        })
        print(f"  added new source '{args.id}' to sources.json")
    SOURCES_PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True, help="short stable id (used in URLs and filenames)")
    ap.add_argument("--url", required=True, help="channel URL (will fetch from /videos)")
    ap.add_argument("--name", required=True, help="human-readable name")
    ap.add_argument("--domain", required=True, help="one-line domain description")
    ap.add_argument("--expertise", required=True, help="what the expert is known for")
    ap.add_argument("--kind", default="youtube_channel")
    ap.add_argument("--language", default="en")
    ap.add_argument("--license", default="transcripts derived from public YouTube auto-captions")
    ap.add_argument("--trust-notes", default="")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="register source only; don't run fetch_channel.py yet")
    ap.add_argument("--window", default="all",
                    choices=["1d", "1w", "1m", "3m", "6m", "1y", "all"],
                    help="Time window for fetched videos (default: all)")
    ap.add_argument("--since", default=None,
                    help="Absolute lower date bound YYYYMMDD (overrides --window)")
    ap.add_argument("--until", default=None,
                    help="Absolute upper date bound YYYYMMDD")
    args = ap.parse_args()

    print(f"[1/3] Register source in sources.json")
    upsert_source(args)

    if args.skip_fetch:
        print("  --skip-fetch set; not fetching transcripts.")
    else:
        print(f"[2/3] Fetching transcripts via fetch_channel.py")
        videos_url = args.url
        if "/videos" not in videos_url:
            videos_url = videos_url.rstrip("/") + "/videos"
        fetch_args = [
            sys.executable, str(ROOT / "fetch_channel.py"),
            "--url", videos_url, "--source", args.id,
            "--window", args.window,
        ]
        if args.since:
            fetch_args += ["--since", args.since]
        if args.until:
            fetch_args += ["--until", args.until]
        rc = subprocess.call(fetch_args)
        if rc != 0:
            print(f"  fetch_channel.py exited {rc}", file=sys.stderr)
            sys.exit(rc)

    print()
    print(f"[3/3] Knowledge-card extraction is the only step that needs an LLM.")
    print( "      The schema is the contract:  data/knowledge/SCHEMA.md")
    print( "      Hand the schema + each transcript to your AI of choice and have")
    print( "      it write data/knowledge/<video_id>.json files. Or run:")
    print(f"          python3 extract_knowledge.py --source {args.id}")
    print( "      (auto-detects xAI / Anthropic / OpenAI API keys from env or .env)")
    print()
    print( "      Once cards exist, rebuild the unified atlas:")
    print(f"          python3 build_knowledge.py")
    print()
    print(f"Source '{args.id}' registered. Transcripts will be at:")
    print(f"  sources/{args.id}/transcripts/")


if __name__ == "__main__":
    main()
