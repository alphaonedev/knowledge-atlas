#!/usr/bin/env python3
# Knowledge Atlas — Worked example of an AI agent consuming the Knowledge Atlas API.
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
ai_client_demo.py — example of how an autonomous AI agent uses the
Knowledge Atlas to teach a human.

No metrics. No statistics. Just distilled, paraphrased knowledge cards
extracted by AI from a domain expert's video corpus.

Pattern:
  1. discover capabilities via /ai/manifest
  2. answer a user question via /ai/teach?q=...
     (returns cards pre-ordered: principles → mental models → frameworks →
      tactics → phrases → warnings)
  3. present cards to the human, citing each video URL

Run:
    python3 ai_client_demo.py "how do I survive cross-examination by a narcissist's lawyer"
"""
import json
import sys
import urllib.request
import urllib.parse

BASE = "http://127.0.0.1:5179"


def get(path, **params):
    if params:
        path = path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read())


def present_card(c, idx):
    pill = c["kind"].replace("_", " ").upper()
    print(f"\n  [{idx}] {pill} · {c['category']}")
    print(f"      {c['title']}")
    print()
    # word-wrap content
    import textwrap
    for line in textwrap.wrap(c["content"], 72):
        print(f"      {line}")
    if c.get("framework_steps"):
        print()
        for i, s in enumerate(c["framework_steps"], 1):
            for j, line in enumerate(textwrap.wrap(f"{i}. {s}", 70)):
                print(f"        {line}" if j else f"        {line}")
    if c.get("reasoning"):
        print()
        for line in textwrap.wrap(f"Why: {c['reasoning']}", 70):
            print(f"      › {line}")
    print(f"\n      Source: {c['video_title']}")
    print(f"              {c['video_url']}")


def main():
    user_question = " ".join(sys.argv[1:]) or "how do I survive cross-examination by a narcissist's lawyer"

    print("═" * 76)
    print("STEP 1 — discover the knowledge surface")
    print("═" * 76)
    manifest = get("/ai/manifest")
    print(f"  {manifest['name']}  v{manifest['version']}")
    print(f"  Purpose: {manifest['purpose']}")
    src = manifest["sources"][0] if manifest["sources"] else None
    if src:
        print(f"\n  Indexed source:")
        print(f"    · {src['name']}")
        print(f"      domain: {src['domain']}")
    print(f"\n  Card kinds available: {[k['kind'] for k in manifest['card_kinds']]}")

    print()
    print("═" * 76)
    print(f'STEP 2 — teach me: "{user_question}"')
    print("═" * 76)
    packet = get("/ai/teach", q=user_question)
    print(f"  Found {packet['card_count']} relevant cards across "
          f"{len(packet['categories_touched'])} topics: "
          f"{', '.join(packet['categories_touched'])}")
    print()
    print("─" * 76)
    print("AI: here's what I learned from this expert's corpus for your question.")
    print("    Presented in teaching order: principles → models → frameworks → tactics → phrases → warnings.")
    print("─" * 76)

    for i, c in enumerate(packet["cards"][:8], 1):
        present_card(c, i)

    print()
    print("═" * 76)
    print("STEP 3 — deep-study a topic")
    print("═" * 76)
    if packet["categories_touched"]:
        topic = packet["categories_touched"][0]
        print(f"  Top topic touched: {topic}")
        packet2 = get(f"/ai/learn/{urllib.parse.quote(topic)}")
        print(f"  /ai/learn/{topic} → {packet2['card_count']} cards available")
        print(f"  Recommended teaching order: {packet2['study_order']}")

    print()
    print("═" * 76)
    print("HOW AN AI AGENT USES THIS")
    print("═" * 76)
    print("""\
  • /ai/manifest tells you what's there.
  • /ai/teach?q=<question> gives you a teaching packet, pre-ordered.
  • /ai/learn/<category> gives you all cards on a topic in study order.
  • /ai/cards?kind=warning gives you just the things to AVOID.
  • Every card carries video_url → cite the source so the human can verify.
  • Never present raw transcripts. The cards ARE the knowledge.
""")


if __name__ == "__main__":
    main()
