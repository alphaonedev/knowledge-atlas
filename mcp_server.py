#!/usr/bin/env python3
# Knowledge Atlas — Model Context Protocol server — exposes the atlas as tools to MCP clients.
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
mcp_server.py — Model Context Protocol server for the Knowledge Atlas.

This is the bridge that lets Claude Desktop, Claude Code, and any other
MCP-aware AI client query your local knowledge atlas as if it had native
tools. It speaks MCP over stdio — the client spawns this script as a
subprocess.

Why MCP and not HTTP:
  - localhost HTTP can't be reached by Claude.ai (web), Claude Desktop, or
    most cloud-hosted assistants — browsers and sandboxes block it.
  - MCP runs the server as a local subprocess of the client. That's the
    standard contract for hooking AIs into local resources.

What's exposed (queries SQLite directly — Flask doesn't need to be running):
  - list_sources         every indexed domain expert
  - list_categories      every topic in the atlas
  - search_knowledge     full-text search across cards
  - teach_about          ordered teaching packet for a question
  - learn_category       deep study packet for one topic
  - cross_concept        every source's take on one concept (multi-expert)
  - cross_compendium     every source's cards on one topic (multi-expert)
  - cross_coverage       matrix: which experts cover which topics
  - get_card             fetch one card by id

See HOW_TO_CONNECT.md for client config.
"""

import asyncio
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "knowledge.db"


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def rows(sql, params=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def one(sql, params=()):
    with conn() as c:
        r = c.execute(sql, params).fetchone()
        return dict(r) if r else None


def fts_clean(q):
    return " ".join(t for t in re.split(r"[^A-Za-z0-9]+", q or "") if t)


def hydrate_card(c):
    c["framework_steps"] = [
        r["step_content"] for r in rows(
            "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
            (c["id"],))
    ]
    return c


# ---- formatting helpers ------------------------------------------------------

def fmt_card(c, with_source=True):
    """Format one card as compact markdown the LLM can present directly."""
    out = []
    out.append(f"### {c['title']}")
    out.append(f"*{c['kind'].replace('_', ' ')} · {c.get('category', '—')}*")
    out.append("")
    out.append(c["content"])
    if c.get("framework_steps"):
        out.append("")
        for i, step in enumerate(c["framework_steps"], 1):
            out.append(f"{i}. {step}")
    if c.get("reasoning"):
        out.append("")
        out.append(f"*Why:* {c['reasoning']}")
    if c.get("source_quote"):
        out.append("")
        out.append(f"> {c['source_quote']}")
    if with_source and c.get("video_url"):
        out.append("")
        out.append(f"— *Source: [{c.get('video_title', 'video')}]({c['video_url']})*")
    return "\n".join(out)


def fmt_card_list(cards, max_cards=20):
    if not cards:
        return "_No cards matched._"
    blocks = [fmt_card(c) for c in cards[:max_cards]]
    return "\n\n---\n\n".join(blocks)


# ---- MCP server --------------------------------------------------------------

server = Server("knowledge-atlas")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_sources",
            description=(
                "List every indexed domain expert (source) in the knowledge "
                "atlas, with size of contribution. Call this first to know who's available."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_categories",
            description=(
                "List every topical category in the atlas with card counts. "
                "Useful for orienting before deep-study queries."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="search_knowledge",
            description=(
                "Full-text search across all knowledge cards (title, content, "
                "reasoning, quotes). Returns paraphrased standalone cards with "
                "citations. Use this for grounding answers to user questions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "natural-language query"},
                    "kind": {"type": "string",
                             "description": "optional filter: principle, tactic, warning, framework, mental_model, phrase, quote"},
                    "limit": {"type": "integer", "default": 15},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="teach_about",
            description=(
                "Given a user question, return a teaching packet of knowledge "
                "cards pre-ordered for explanation: principles → mental models "
                "→ frameworks → tactics → phrases → warnings. Use this when the "
                "user asks 'teach me X' or 'how do I Y'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["question"],
            },
        ),
        Tool(
            name="learn_category",
            description=(
                "Deep study packet for one topical category. Returns every "
                "card on that topic from every indexed source, ordered for teaching."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                },
                "required": ["category"],
            },
        ),
        Tool(
            name="cross_concept",
            description=(
                "MULTI-SOURCE: show every indexed expert's take on one concept, "
                "grouped by source. Use this when the user asks 'what do "
                "different experts say about X' or for comparing perspectives."
            ),
            inputSchema={
                "type": "object",
                "properties": {"term": {"type": "string"}},
                "required": ["term"],
            },
        ),
        Tool(
            name="cross_compendium",
            description=(
                "MULTI-SOURCE: every expert's cards on one category, grouped "
                "by source. Best for building a side-by-side topic briefing."
            ),
            inputSchema={
                "type": "object",
                "properties": {"category": {"type": "string"}},
                "required": ["category"],
            },
        ),
        Tool(
            name="cross_coverage",
            description=(
                "MULTI-SOURCE: matrix showing which experts cover which topics, "
                "and how deeply. Useful for picking which expert(s) to consult "
                "for a topic."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_card",
            description="Fetch a single knowledge card by its numeric id.",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="list_videos",
            description=(
                "List every video in the atlas with its one-line thesis and "
                "card count. Useful for browsing the corpus."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string", "description": "optional filter"}
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "list_sources":
        return _list_sources()
    if name == "list_categories":
        return _list_categories()
    if name == "search_knowledge":
        return _search_knowledge(arguments)
    if name == "teach_about":
        return _teach_about(arguments)
    if name == "learn_category":
        return _learn_category(arguments)
    if name == "cross_concept":
        return _cross_concept(arguments)
    if name == "cross_compendium":
        return _cross_compendium(arguments)
    if name == "cross_coverage":
        return _cross_coverage()
    if name == "get_card":
        return _get_card(arguments)
    if name == "list_videos":
        return _list_videos(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


def _list_sources():
    src = rows("SELECT * FROM sources")
    out = [f"## Indexed sources ({len(src)})", ""]
    for s in src:
        videos = one("SELECT COUNT(*) AS n FROM videos WHERE source_id=?", (s["id"],))["n"]
        cards = one("SELECT COUNT(*) AS n FROM cards WHERE source_id=?", (s["id"],))["n"]
        cats = one("SELECT COUNT(DISTINCT category) AS n FROM cards WHERE source_id=?", (s["id"],))["n"]
        out.append(f"### {s['name']} (`{s['id']}`)")
        if s.get("expertise"):
            out.append(f"*{s['expertise']}*")
        out.append("")
        out.append(f"- **Domain:** {s.get('domain', '—')}")
        out.append(f"- **Size:** {videos} videos · {cards} cards · {cats} topics")
        if s.get("url"):
            out.append(f"- **Source:** {s['url']}")
        out.append("")
    return [TextContent(type="text", text="\n".join(out))]


def _list_categories():
    cats = rows("SELECT category, COUNT(*) AS cards FROM cards GROUP BY category ORDER BY cards DESC")
    out = [f"## Categories in the atlas ({len(cats)} topics)", ""]
    for c in cats:
        out.append(f"- **{c['category']}** — {c['cards']} cards")
    return [TextContent(type="text", text="\n".join(out))]


def _search_knowledge(args):
    q = args.get("query", "").strip()
    kind = args.get("kind")
    limit = min(int(args.get("limit", 15)), 100)
    fq = fts_clean(q)
    if not fq:
        return [TextContent(type="text", text="Empty query.")]
    toks = [t for t in fq.split() if len(t) > 2]
    if not toks:
        return [TextContent(type="text", text="Query too short.")]
    fq_match = " OR ".join(t + "*" for t in toks)
    sql = """
        SELECT c.id, c.kind, c.category, c.title, c.content, c.reasoning,
               c.source_quote, c.video_id, v.title AS video_title, v.url AS video_url
        FROM cards_fts f
        JOIN cards c ON c.id = f.card_id
        JOIN videos v ON v.id = c.video_id
        WHERE cards_fts MATCH ?
    """
    params = [fq_match]
    if kind:
        sql += " AND c.kind = ?"; params.append(kind)
    sql += " ORDER BY bm25(cards_fts) LIMIT ?"; params.append(limit)
    hits = [hydrate_card(c) for c in rows(sql, tuple(params))]
    text = f"## Search · `{q}` · {len(hits)} cards\n\n" + fmt_card_list(hits, max_cards=limit)
    return [TextContent(type="text", text=text)]


def _teach_about(args):
    q = args.get("question", "").strip()
    limit = min(int(args.get("limit", 20)), 60)
    fq = fts_clean(q)
    if not fq:
        return [TextContent(type="text", text="Empty question.")]
    toks = [t for t in fq.split() if len(t) > 2]
    fq_match = " OR ".join(t + "*" for t in toks) if toks else fq
    hits = [hydrate_card(c) for c in rows("""
        SELECT c.id, c.kind, c.category, c.title, c.content, c.reasoning,
               c.source_quote, c.video_id, v.title AS video_title, v.url AS video_url
        FROM cards_fts f
        JOIN cards c ON c.id = f.card_id
        JOIN videos v ON v.id = c.video_id
        WHERE cards_fts MATCH ?
        ORDER BY
          CASE c.kind WHEN 'principle' THEN 1 WHEN 'mental_model' THEN 2
                      WHEN 'framework' THEN 3 WHEN 'tactic' THEN 4
                      WHEN 'phrase' THEN 5 WHEN 'warning' THEN 6 ELSE 7 END,
          bm25(cards_fts)
        LIMIT ?
    """, (fq_match, limit))]
    cats = sorted({c["category"] for c in hits})
    out = [
        f"# Teaching packet — {q}",
        "",
        f"*{len(hits)} cards across {len(cats)} topics. "
        f"Ordered for teaching: principles → models → frameworks → tactics → phrases → warnings.*",
        "",
    ]
    out.append(fmt_card_list(hits, max_cards=limit))
    return [TextContent(type="text", text="\n".join(out))]


def _learn_category(args):
    category = args.get("category", "").strip()
    if not category:
        return [TextContent(type="text", text="Category required.")]
    hits = [hydrate_card(c) for c in rows("""
        SELECT c.id, c.kind, c.category, c.title, c.content, c.reasoning,
               c.source_quote, c.video_id, v.title AS video_title, v.url AS video_url
        FROM cards c JOIN videos v ON v.id = c.video_id
        WHERE c.category = ?
        ORDER BY
          CASE c.kind WHEN 'principle' THEN 1 WHEN 'mental_model' THEN 2
                      WHEN 'framework' THEN 3 WHEN 'tactic' THEN 4
                      WHEN 'phrase' THEN 5 WHEN 'warning' THEN 6 ELSE 7 END
    """, (category,))]
    if not hits:
        return [TextContent(type="text", text=f"No cards in category `{category}`. Try `list_categories`.")]
    out = [
        f"# {category} — study packet",
        "",
        f"*{len(hits)} cards. Read top to bottom: principles → models → frameworks → tactics → phrases → warnings.*",
        "",
    ]
    out.append(fmt_card_list(hits, max_cards=len(hits)))
    return [TextContent(type="text", text="\n".join(out))]


def _cross_concept(args):
    term = args.get("term", "").strip()
    fq = fts_clean(term)
    if not fq:
        return [TextContent(type="text", text="Empty term.")]
    toks = [t for t in fq.split() if len(t) > 2]
    fq_match = " OR ".join(t + "*" for t in toks) if toks else fq
    hits = [hydrate_card(c) for c in rows("""
        SELECT c.id, c.source_id, c.kind, c.category, c.title, c.content,
               c.reasoning, c.source_quote, c.video_id,
               v.title AS video_title, v.url AS video_url,
               (SELECT name FROM sources WHERE id = c.source_id) AS source_name
        FROM cards_fts f
        JOIN cards c ON c.id = f.card_id
        JOIN videos v ON v.id = c.video_id
        WHERE cards_fts MATCH ?
        ORDER BY c.source_id, bm25(cards_fts)
        LIMIT 60
    """, (fq_match,))]
    by_source = {}
    for h in hits:
        by_source.setdefault(h["source_id"], {
            "name": h["source_name"], "cards": []
        })["cards"].append(h)
    out = [
        f"# Cross-source · concept `{term}`",
        "",
        f"*{len(hits)} cards across {len(by_source)} expert"
        f"{'s' if len(by_source) != 1 else ''}.*",
        "",
    ]
    if len(by_source) == 1:
        out.append("> Only one source indexed. Add more via `add_source.py` to unlock cross-expert correlation.")
        out.append("")
    for sid, payload in by_source.items():
        out.append(f"## {payload['name']} (`{sid}`) — {len(payload['cards'])} cards")
        out.append("")
        out.append(fmt_card_list(payload["cards"], max_cards=10))
        out.append("")
    return [TextContent(type="text", text="\n".join(out))]


def _cross_compendium(args):
    category = args.get("category", "").strip()
    if not category:
        return [TextContent(type="text", text="Category required.")]
    hits = [hydrate_card(c) for c in rows("""
        SELECT c.id, c.source_id, c.kind, c.category, c.title, c.content,
               c.reasoning, c.source_quote, c.video_id,
               v.title AS video_title, v.url AS video_url,
               (SELECT name FROM sources WHERE id = c.source_id) AS source_name
        FROM cards c JOIN videos v ON v.id = c.video_id
        WHERE c.category = ?
        ORDER BY c.source_id, c.kind
    """, (category,))]
    by_source = {}
    for h in hits:
        by_source.setdefault(h["source_id"], {
            "name": h["source_name"], "cards": []
        })["cards"].append(h)
    out = [f"# Cross-source compendium · {category}", ""]
    for sid, payload in by_source.items():
        out.append(f"## {payload['name']} (`{sid}`)")
        out.append("")
        out.append(fmt_card_list(payload["cards"], max_cards=20))
        out.append("")
    return [TextContent(type="text", text="\n".join(out))]


def _cross_coverage():
    grid = rows("""
        SELECT category, source_id, COUNT(*) AS cards
        FROM cards GROUP BY category, source_id
        ORDER BY category
    """)
    sids = sorted({r["source_id"] for r in grid})
    by_cat = {}
    for r in grid:
        by_cat.setdefault(r["category"], {})[r["source_id"]] = r["cards"]
    out = [f"# Cross-source coverage — {len(by_cat)} topics × {len(sids)} sources", ""]
    out.append("| topic | " + " | ".join(sids) + " | total |")
    out.append("|---|" + "|".join("---:" for _ in sids) + "|---:|")
    for cat in sorted(by_cat, key=lambda c: -sum(by_cat[c].values())):
        row = by_cat[cat]
        total = sum(row.values())
        out.append(
            f"| {cat} | "
            + " | ".join(str(row.get(s, "—")) for s in sids)
            + f" | {total} |"
        )
    return [TextContent(type="text", text="\n".join(out))]


def _get_card(args):
    cid = int(args.get("id", 0))
    c = one("""
        SELECT c.*, v.title AS video_title, v.url AS video_url
        FROM cards c JOIN videos v ON v.id = c.video_id WHERE c.id = ?
    """, (cid,))
    if not c:
        return [TextContent(type="text", text=f"No card with id {cid}.")]
    hydrate_card(c)
    return [TextContent(type="text", text=fmt_card(c))]


def _list_videos(args):
    sid = args.get("source_id")
    sql = """SELECT v.id, v.title, v.url, v.one_line, v.source_id,
                    (SELECT COUNT(*) FROM cards WHERE video_id=v.id) AS card_count
             FROM videos v"""
    params = ()
    if sid:
        sql += " WHERE v.source_id = ?"; params = (sid,)
    sql += " ORDER BY card_count DESC, v.title"
    vids = rows(sql, params)
    out = [f"## Videos ({len(vids)})", ""]
    for v in vids:
        out.append(f"- **{v['title']}** — {v['card_count']} cards")
        if v.get("one_line"):
            out.append(f"  *{v['one_line']}*")
        out.append(f"  {v['url']}")
    return [TextContent(type="text", text="\n".join(out))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
