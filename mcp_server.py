#!/usr/bin/env python3
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
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "knowledge.db"
FLASK_URL = "http://127.0.0.1:5179"
FLASK_PORT = 5179


# ---------- Flask service helpers (used by the add/remove/cancel tools) -----

def _port_open():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", FLASK_PORT))
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False
    finally:
        s.close()


def _ensure_flask(timeout=20):
    """If Flask isn't running, start it via atlas.py and wait for the port."""
    if _port_open():
        return True
    try:
        subprocess.Popen(
            [sys.executable, str(ROOT / "atlas.py"), "start"],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open():
            return True
        time.sleep(0.4)
    return False


def _http_json(method, path, body=None, timeout=10):
    url = FLASK_URL + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


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


# Generic, low-signal tokens that show up in nearly every card and drown
# distinctive terms when expanded as OR-prefix wildcards. Dropping them
# from the FTS rewriter dramatically reduces cross-source leakage on a
# corpus dominated by one large source. Keep this list short and obvious —
# only words that carry no domain signal of their own.
FTS_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "this", "that", "these", "those",
    "into", "onto", "your", "you", "are", "was", "were", "has", "have", "had",
    "but", "not", "any", "all", "out", "off", "can", "will", "just", "now",
    "new", "old", "more", "most", "much", "many", "less", "fewer",
    "top", "best", "worst", "good", "bad", "high", "low",
    "year", "years", "day", "days", "time", "times",
    "report", "reports", "data", "info", "thing", "things",
    "way", "ways", "use", "uses", "used", "using",
    "make", "makes", "made", "get", "gets", "got",
    "people", "person", "company", "companies",
    "tax", "taxes", "price", "prices", "market", "markets",
    "stock", "stocks", "money", "real", "estate",
})


def _toks_for_match(fq):
    """Return distinctive >2-char tokens from a cleaned FTS query, with
    common stopwords removed. Falls back to the full token list when the
    stopword filter would otherwise leave the query empty."""
    raw = [t for t in fq.split() if len(t) > 2]
    filtered = [t for t in raw if t.lower() not in FTS_STOPWORDS]
    return filtered or raw


def _fts_match_with_fallback(toks, *, min_hits=1):
    """Build an FTS5 MATCH expression. Strict-AND only for multi-token queries.

    Returning `tok1* OR tok2* OR ...` for every query produces a flood of
    false positives on multi-word names: "Marc Benioff" matched every card
    containing "March" or "Marketing" because each token was OR'd as an
    independent prefix wildcard.

    Multi-token policy:
      Tier A: `tok1 AND tok2 AND ...`     — exact tokens, all required
      Tier B: `tok1* AND tok2* AND ...`   — prefix wildcards, all required
                                            (typo / morphology tolerant)
      Otherwise: return None — honest empty rather than misleading OR.

    Single-token: just `tok*` (prefix). Returns None on empty token list.
    """
    if not toks:
        return None
    if len(toks) == 1:
        return toks[0] + "*"
    candidates = [
        " AND ".join(toks),
        " AND ".join(t + "*" for t in toks),
    ]
    for expr in candidates:
        try:
            n = one(
                "SELECT COUNT(*) AS n FROM cards_fts WHERE cards_fts MATCH ?",
                (expr,),
            )
        except Exception:
            continue
        if n and (n.get("n") or 0) >= min_hits:
            return expr
    return None


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
                "citations. Use this for grounding answers to user questions. "
                "Pass `source_id` to scope the search to a single expert and "
                "avoid leakage from larger sources in the corpus."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "natural-language query"},
                    "kind": {"type": "string",
                             "description": "optional filter: principle, tactic, warning, framework, mental_model, phrase, quote"},
                    "source_id": {"type": "string",
                                  "description": "optional filter: restrict results to a single source (see list_sources)"},
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
                "user asks 'teach me X' or 'how do I Y'. Pass `source_id` to "
                "scope the packet to a single expert."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "source_id": {"type": "string",
                                  "description": "optional filter: restrict results to a single source (see list_sources)"},
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
        # -------- Write/management tools (mutate the atlas) -----------------
        Tool(
            name="add_source",
            description=(
                "Index a NEW knowledge source (YouTube channel). Runs the full "
                "pipeline: register → fetch transcripts → LLM-extract knowledge "
                "cards → rebuild atlas. Returns a job_id; poll with "
                "`ingest_status`. Auto-starts the Flask service if not running. "
                "For refreshing a source you've ALREADY indexed, use "
                "`update_source` instead — it auto-skips existing videos and "
                "defaults to a sensible time window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url":       {"type": "string",
                                  "description": "YouTube channel URL (e.g. https://www.youtube.com/@channel)"},
                    "source_id": {"type": "string",
                                  "description": "stable slug for this source (auto-derived from URL if omitted)"},
                    "name":      {"type": "string", "description": "display name"},
                    "domain":    {"type": "string", "description": "one-line domain description"},
                    "expertise": {"type": "string", "description": "what the expert is known for"},
                    "window":    {"type": "string",
                                  "enum": ["1d", "1w", "1m", "3m", "6m", "1y", "all"],
                                  "description": "time window for fetched videos (default 'all')"},
                    "since":     {"type": "string",
                                  "description": "absolute YYYYMMDD lower bound (overrides window)"},
                    "until":     {"type": "string",
                                  "description": "absolute YYYYMMDD upper bound"},
                    "provider":  {"type": "string", "enum": ["xai", "anthropic", "openai"],
                                  "description": "LLM provider for the extraction step "
                                                 "(default: auto-detect from env keys)"},
                    "model":     {"type": "string",
                                  "description": "model id (default: provider's default)"},
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="update_source",
            description=(
                "Pull only net-new videos for an EXISTING source. Looks up the "
                "channel URL from the registry and runs the same pipeline as "
                "add_source, but with sensible defaults for refreshes: defaults "
                "to a 1-week window and auto-computes `since` from the latest "
                "upload_date already on disk so you never re-download what you "
                "already have. The pipeline is idempotent end-to-end — cached "
                "transcripts and existing card JSON are skipped automatically. "
                "Use this (not add_source) when refreshing a channel you've "
                "already indexed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string",
                                  "description": "existing source id (see list_sources)"},
                    "window":    {"type": "string",
                                  "enum": ["1d", "1w", "1m", "3m", "6m", "1y", "all"],
                                  "description": "time window for the refresh (default '1w'; "
                                                 "ignored if `since` is set)"},
                    "since":     {"type": "string",
                                  "description": "absolute YYYYMMDD lower bound (overrides window)"},
                    "until":     {"type": "string",
                                  "description": "absolute YYYYMMDD upper bound"},
                    "provider":  {"type": "string", "enum": ["xai", "anthropic", "openai"],
                                  "description": "LLM provider for the extraction step "
                                                 "(default: auto-detect from env keys)"},
                    "model":     {"type": "string",
                                  "description": "model id (default: provider's default)"},
                },
                "required": ["source_id"],
            },
        ),
        Tool(
            name="ingest_status",
            description=(
                "Check the status of an in-flight ingestion job. Returns "
                "step, percent, latest log lines, and terminal status "
                "(running | done | error | cancelled | awaiting_extraction)."
            ),
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        Tool(
            name="cancel_ingest",
            description=(
                "Cleanly stop a running ingestion job. Partial work is "
                "preserved on disk; the next add_source for the same source "
                "resumes idempotently from where this one stopped."
            ),
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        Tool(
            name="remove_source",
            description=(
                "Destructively remove a knowledge source: deletes its entry "
                "in sources.json, its transcripts, its card files, and "
                "rebuilds the atlas. IRREVERSIBLE. Call once with confirm=false "
                "to see a plan, then again with confirm=true to execute."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "confirm":   {"type": "boolean",
                                  "description": "must be true to actually delete (default false → dry-run)"},
                },
                "required": ["source_id"],
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
    if name == "add_source":
        return _add_source(arguments)
    if name == "update_source":
        return _update_source(arguments)
    if name == "ingest_status":
        return _ingest_status(arguments)
    if name == "cancel_ingest":
        return _cancel_ingest(arguments)
    if name == "remove_source":
        return _remove_source(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


def _safe_field(value, max_len=300):
    """Sanitize a string field before echoing it through MCP tool output.

    Why this exists: an MCP tool response is text that flows directly into an
    LLM's context window. If a malicious (or fat-fingered) actor managed to
    write prompt-injection text into a source's `url`, `name`, etc., it would
    arrive at the LLM as untrusted instructions wearing the costume of a
    legitimate data field. Claude Desktop caught one such case in the wild and
    refused to act, but defense-in-depth means we sanitize at the server too:
      - cap length (real YouTube URLs are < 150 chars)
      - collapse newlines/tabs (block-quote-style injections need newlines)
      - mark obvious non-URL contents in URL-typed fields
    """
    if value is None:
        return ""
    s = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = " ".join(s.split())  # collapse runs of whitespace
    if len(s) > max_len:
        s = s[:max_len] + f"… [truncated, {len(value) - max_len} more chars]"
    return s


def _safe_url(value):
    """Same as _safe_field but additionally flags URL-typed fields whose
    content doesn't look like a URL — so an LLM reading the output sees a
    clear `<malformed>` marker instead of injection text."""
    s = _safe_field(value, max_len=200)
    if not s:
        return ""
    low = s.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return f"<malformed URL value omitted>"
    return s


def _list_sources():
    src = rows("SELECT * FROM sources")
    out = [f"## Indexed sources ({len(src)})", ""]
    for s in src:
        videos = one("SELECT COUNT(*) AS n FROM videos WHERE source_id=?", (s["id"],))["n"]
        cards = one("SELECT COUNT(*) AS n FROM cards WHERE source_id=?", (s["id"],))["n"]
        cats = one("SELECT COUNT(DISTINCT category) AS n FROM cards WHERE source_id=?", (s["id"],))["n"]
        out.append(f"### {_safe_field(s['name'], 120)} (`{_safe_field(s['id'], 60)}`)")
        if s.get("expertise"):
            out.append(f"*{_safe_field(s['expertise'], 200)}*")
        out.append("")
        out.append(f"- **Domain:** {_safe_field(s.get('domain') or '—', 80)}")
        out.append(f"- **Size:** {videos} videos · {cards} cards · {cats} topics")
        if s.get("url"):
            out.append(f"- **Source:** {_safe_url(s['url'])}")
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
    source_id = (args.get("source_id") or "").strip() or None
    limit = min(int(args.get("limit", 15)), 100)
    fq = fts_clean(q)
    if not fq:
        return [TextContent(type="text", text="Empty query.")]
    toks = _toks_for_match(fq)
    if not toks:
        return [TextContent(type="text", text="Query too short.")]
    if source_id and not one("SELECT id FROM sources WHERE id=?", (source_id,)):
        return [TextContent(
            type="text",
            text=f"Unknown source_id `{source_id}`. Call `list_sources` to see valid ids.",
        )]
    fq_match = _fts_match_with_fallback(toks)
    if not fq_match:
        return [TextContent(
            type="text",
            text=f"## Search · `{q}` · 0 cards\n\n_No cards match all of these terms. "
                 f"Try fewer or different keywords._",
        )]
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
    if source_id:
        sql += " AND c.source_id = ?"; params.append(source_id)
    sql += " ORDER BY bm25(cards_fts) LIMIT ?"; params.append(limit)
    hits = [hydrate_card(c) for c in rows(sql, tuple(params))]
    scope = f" · source `{source_id}`" if source_id else ""
    text = (
        f"## Search · `{q}`{scope} · {len(hits)} cards\n\n"
        + fmt_card_list(hits, max_cards=limit)
    )
    return [TextContent(type="text", text=text)]


def _teach_about(args):
    q = args.get("question", "").strip()
    source_id = (args.get("source_id") or "").strip() or None
    limit = min(int(args.get("limit", 20)), 60)
    fq = fts_clean(q)
    if not fq:
        return [TextContent(type="text", text="Empty question.")]
    toks = _toks_for_match(fq)
    if source_id and not one("SELECT id FROM sources WHERE id=?", (source_id,)):
        return [TextContent(
            type="text",
            text=f"Unknown source_id `{source_id}`. Call `list_sources` to see valid ids.",
        )]
    fq_match = _fts_match_with_fallback(toks)
    if not fq_match:
        return [TextContent(
            type="text",
            text=f"# Teaching packet — {q}\n\n_No cards match all of these terms. "
                 f"Try fewer or different keywords._",
        )]
    sql = """
        SELECT c.id, c.kind, c.category, c.title, c.content, c.reasoning,
               c.source_quote, c.video_id, v.title AS video_title, v.url AS video_url
        FROM cards_fts f
        JOIN cards c ON c.id = f.card_id
        JOIN videos v ON v.id = c.video_id
        WHERE cards_fts MATCH ?
    """
    params = [fq_match]
    if source_id:
        sql += " AND c.source_id = ?"; params.append(source_id)
    sql += """
        ORDER BY
          CASE c.kind WHEN 'principle' THEN 1 WHEN 'mental_model' THEN 2
                      WHEN 'framework' THEN 3 WHEN 'tactic' THEN 4
                      WHEN 'phrase' THEN 5 WHEN 'warning' THEN 6 ELSE 7 END,
          bm25(cards_fts)
        LIMIT ?
    """
    params.append(limit)
    hits = [hydrate_card(c) for c in rows(sql, tuple(params))]
    cats = sorted({c["category"] for c in hits})
    scope = f" (source `{source_id}`)" if source_id else ""
    out = [
        f"# Teaching packet — {q}{scope}",
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
    toks = _toks_for_match(fq)
    fq_match = _fts_match_with_fallback(toks)
    if not fq_match:
        return [TextContent(
            type="text",
            text=f"# Cross-source · `{term}`\n\n_No cards match all of these terms._",
        )]
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


def _add_source(args):
    """Kick off an ingestion job by POSTing to Flask's /api/ingest. Returns
    job_id + presentation hint. Auto-starts Flask if it's not already up."""
    if not args.get("url"):
        return [TextContent(type="text", text="**url** is required.")]
    if not _ensure_flask():
        return [TextContent(type="text",
                text="✗ Could not reach or start the Flask service on port 5179. "
                     "Run `python3 atlas.py start` manually and try again.")]
    payload = {
        "url": args["url"],
        "source_id": args.get("source_id"),
        "name": args.get("name"),
        "domain": args.get("domain"),
        "expertise": args.get("expertise"),
        "window": args.get("window") or "all",
        "since": args.get("since"),
        "until": args.get("until"),
        "provider": args.get("provider"),
        "model": args.get("model"),
        "save_api_key": True,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    code, body = _http_json("POST", "/api/ingest", body=payload, timeout=15)
    if code != 200:
        return [TextContent(type="text",
                text=f"✗ ingest start failed (HTTP {code}): {body.get('error', body)}")]
    job_id = body.get("job_id")
    out = [
        f"# Ingestion started",
        f"",
        f"- **job_id**: `{job_id}`",
        f"- **url**: {args['url']}",
        f"- **window**: {args.get('window') or 'all'}",
        f"- **provider**: {args.get('provider') or 'auto-detect'}",
        f"",
        f"Poll progress with the `ingest_status` tool, e.g. ingest_status job_id='{job_id}'.",
        f"To stop early: cancel_ingest job_id='{job_id}' (partial work is preserved).",
    ]
    return [TextContent(type="text", text="\n".join(out))]


def _update_source(args):
    """Refresh an existing source: look up its URL from sources.json, default
    to a 1-week window, auto-compute `since` from the latest upload_date on
    disk so we never re-download what we already have. Hands off to the same
    /api/ingest endpoint as add_source — the pipeline is idempotent."""
    sid = (args.get("source_id") or "").strip()
    if not sid:
        return [TextContent(type="text", text="**source_id** is required.")]

    sources_path = ROOT / "sources.json"
    try:
        doc = json.loads(sources_path.read_text())
    except Exception as e:
        return [TextContent(type="text",
                text=f"✗ Could not read sources.json: {e}")]
    src = next((s for s in doc.get("sources", []) if s.get("id") == sid), None)
    if not src:
        return [TextContent(type="text",
                text=f"Unknown source `{sid}`. Call `list_sources` to see valid ids. "
                     f"To index a brand-new channel use `add_source` instead.")]
    url = src.get("url")
    if not url:
        return [TextContent(type="text",
                text=f"Source `{sid}` has no url recorded. Re-add with `add_source`.")]

    # Auto-compute `since` from the latest upload_date already on disk so we
    # only fetch genuinely new videos. The user can override with explicit
    # `since` or `window`.
    auto_since = None
    if not args.get("since") and not args.get("window"):
        meta_path = ROOT / "sources" / sid / "channel_metadata.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                dates = [m.get("upload_date") for m in meta
                         if isinstance(m.get("upload_date"), str)
                         and len(m["upload_date"]) == 8 and m["upload_date"].isdigit()]
                if dates:
                    auto_since = max(dates)  # YYYYMMDD; yt-dlp filter is inclusive
            except Exception:
                pass

    if not _ensure_flask():
        return [TextContent(type="text",
                text="✗ Could not reach or start the Flask service on port 5179. "
                     "Run `python3 atlas.py start` manually and try again.")]

    payload = {
        "url": url,
        "source_id": sid,
        "name": src.get("name") or sid,
        "domain": src.get("domain") or "",
        "expertise": src.get("expertise") or "",
        "window": args.get("window") or ("all" if auto_since else "1w"),
        "since": args.get("since") or auto_since,
        "until": args.get("until"),
        "provider": args.get("provider"),
        "model": args.get("model"),
        "save_api_key": True,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    code, body = _http_json("POST", "/api/ingest", body=payload, timeout=15)
    if code != 200:
        return [TextContent(type="text",
                text=f"✗ update start failed (HTTP {code}): {body.get('error', body)}")]
    job_id = body.get("job_id")
    out = [
        f"# 🔄 Updating source `{sid}`",
        "",
        f"- **job_id**: `{job_id}`",
        f"- **url**: {url}",
        f"- **window**: {payload['window']}",
        f"- **since**: {payload.get('since') or '—'}"
        + (" *(auto-computed from latest upload_date on disk)*" if auto_since and not args.get('since') else ""),
        f"- **until**: {payload.get('until') or '—'}",
        f"- **provider**: {args.get('provider') or 'auto-detect'}",
        "",
        "The pipeline is idempotent: cached transcripts and existing card JSON "
        "are skipped automatically. Only net-new videos hit the network and the LLM.",
        "",
        f"Poll progress with `ingest_status` job_id='{job_id}'.",
        f"To stop early: `cancel_ingest` job_id='{job_id}' (partial work is preserved).",
    ]
    return [TextContent(type="text", text="\n".join(out))]


def _ingest_status(args):
    job_id = args.get("job_id")
    if not job_id:
        return [TextContent(type="text", text="**job_id** is required.")]
    if not _ensure_flask():
        return [TextContent(type="text",
                text="✗ Flask not reachable; job state lives in its memory.")]
    code, body = _http_json("GET", f"/api/ingest/status/{job_id}", timeout=5)
    if code != 200:
        return [TextContent(type="text",
                text=f"✗ status fetch failed (HTTP {code}): {body.get('error', body)}")]
    out = [
        f"# Ingestion `{job_id}`",
        f"",
        f"- **status**:  `{body.get('status', '?')}`",
        f"- **step**:    {body.get('step', '?')}",
        f"- **percent**: {body.get('percent', 0)}%",
        f"- **message**: {body.get('message', '')}",
    ]
    if body.get("source_id"):
        out.append(f"- **source_id**: {body['source_id']}")
    out.append("")
    log = body.get("log") or []
    if log:
        out.append("## Latest log")
        out.append("```")
        for line in log[-20:]:
            out.append(line)
        out.append("```")
    return [TextContent(type="text", text="\n".join(out))]


def _cancel_ingest(args):
    job_id = args.get("job_id")
    if not job_id:
        return [TextContent(type="text", text="**job_id** is required.")]
    if not _ensure_flask():
        return [TextContent(type="text", text="✗ Flask not reachable.")]
    code, body = _http_json("POST", f"/api/ingest/cancel/{job_id}", timeout=10)
    if code != 200:
        return [TextContent(type="text",
                text=f"✗ cancel failed (HTTP {code}): {body.get('error', body)}")]
    note = body.get("note", "")
    status = body.get("status", "cancelling")
    msg = f"Job `{job_id}`: **{status}**"
    if note:
        msg += f"\n\n> {note}"
    msg += "\n\nPartial work is preserved on disk. The next add_source for the same source resumes idempotently."
    return [TextContent(type="text", text=msg)]


def _remove_source(args):
    sid = args.get("source_id")
    if not sid:
        return [TextContent(type="text", text="**source_id** is required.")]
    confirm = bool(args.get("confirm", False))
    if not _ensure_flask():
        return [TextContent(type="text", text="✗ Flask not reachable.")]
    # DELETE /api/source/<sid>?confirm=true|false
    suffix = "?confirm=true" if confirm else ""
    code, body = _http_json("DELETE", f"/api/source/{urllib.parse.quote(sid)}{suffix}",
                            timeout=120)
    if code == 404:
        return [TextContent(type="text",
                text=f"✗ Source `{sid}` not found.")]
    if code != 200:
        return [TextContent(type="text",
                text=f"✗ remove failed (HTTP {code}): {body.get('error', body)}")]
    if not confirm:
        plan = body.get("plan", {})
        out = [
            f"# Dry-run: would remove `{sid}`",
            f"",
            f"- in registry:      {'yes' if plan.get('in_registry') else 'no'}",
            f"- source directory: `{plan.get('source_dir')}` "
                f"({'exists' if plan.get('source_dir_exists') else 'absent'})",
            f"- videos in source: {len(plan.get('video_ids', []))}",
            f"- card JSON files:  {len(plan.get('card_files', []))}",
            f"- transcripts:      {plan.get('transcript_count', 0)}",
            f"- raw .srt files:   {plan.get('raw_srt_count', 0)}",
            f"",
            f"**This is irreversible.** Call again with `confirm: true` to actually delete.",
        ]
        return [TextContent(type="text", text="\n".join(out))]
    # Confirmed: report what was done
    actions = body.get("actions") or []
    out = [f"# ✓ Removed source `{sid}`", ""]
    for a in actions:
        out.append(f"- {a}")
    out.append("")
    out.append("Atlas rebuilt. Cross-source endpoints reflect the new state.")
    return [TextContent(type="text", text="\n".join(out))]


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
