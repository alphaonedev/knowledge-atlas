#!/usr/bin/env python3
# Knowledge Atlas — Flask service tier — human dashboard and AI agent surfaces.
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
app.py — localhost Flask server for the Knowledge Atlas.

No metrics. No charts. No scores. Just the distilled knowledge.

Run:
    python3 app.py
    # then open http://127.0.0.1:5179/
"""

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, render_template, send_from_directory, abort

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "knowledge.db"
SOURCES_PATH = ROOT / "sources.json"
ENV_FILE = ROOT / ".env"

# ---------- background-job state for self-serve ingestion --------------------
# JOBS[job_id] holds the live state of one ingest job.
#   status: queued | running | awaiting_extraction | done | error | cancelled
#   step:   register | fetch | extract | aggregate | done | cancelled
#   percent, message, log[], error, source_id
#   cancelled (bool): user requested stop; checked between stages and during subprocess streaming
#   proc:   the currently-running subprocess.Popen (so cancel can terminate it)
JOBS = {}
JOBS_LOCK = threading.Lock()


def _job_set(job_id, **fields):
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {"log": []}).update(fields)


def _job_log(job_id, line):
    with JOBS_LOCK:
        j = JOBS.setdefault(job_id, {"log": []})
        j.setdefault("log", []).append(line)
        if len(j["log"]) > 200:
            j["log"] = j["log"][-200:]


def _job_is_cancelled(job_id):
    with JOBS_LOCK:
        return bool(JOBS.get(job_id, {}).get("cancelled"))


def _job_set_proc(job_id, proc):
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {"log": []})["proc"] = proc


def _terminate_proc(proc, grace_sec=3):
    """Kill a subprocess and its entire process group cleanly.
    Sends SIGTERM, waits grace_sec, then SIGKILL if still alive."""
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=grace_sec)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


PROVIDER_ENV = {"xai": "XAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
PROVIDER_DEFAULT_MODEL = {
    "xai":       "grok-4.20-0309-reasoning",
    "anthropic": "claude-sonnet-4-5",
}


def _load_env():
    """Read ~/.env then project .env; existing env vars win."""
    for p in (Path.home() / ".env", ENV_FILE):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def _save_api_key(var_name, key):
    """Persist a provider API key to project .env (gitignored). Merges with existing keys."""
    existing = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            existing[k.strip()] = v.strip()
    existing[var_name] = key
    ENV_FILE.write_text(
        "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(ENV_FILE, 0o600)
    except Exception:
        pass


def _auto_provider():
    if os.environ.get("XAI_API_KEY"):
        return "xai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def _derive_id_from_url(url):
    """Best-effort: extract a stable short id from a YouTube channel URL."""
    p = urlparse(url)
    path = p.path.strip("/")
    if path.startswith("@"):
        path = path[1:]
    # take first path segment, drop /videos suffix etc.
    seg = (path.split("/") or [""])[0]
    return re.sub(r"[^A-Za-z0-9_-]+", "", seg).lower() or "source"

app = Flask(__name__, template_folder=str(ROOT / "templates"),
            static_folder=str(ROOT / "static"))


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


def hydrate_card(card):
    card["framework_steps"] = [
        r["step_content"] for r in rows(
            "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
            (card["id"],))
    ]
    if card.get("video_id"):
        v = one("SELECT title, url FROM videos WHERE id=?", (card["video_id"],))
        if v:
            card["video_title"] = v["title"]
            card["video_url"] = v["url"]
    return card


# ============================================================================
# HTML
# ============================================================================

@app.route("/")
def index():
    return render_template("index.html")


# ============================================================================
# Human-facing API — what the UI calls
# ============================================================================

@app.route("/api/source")
def api_source():
    """Single source overview (extensible to multi-source later)."""
    s = one("SELECT * FROM sources LIMIT 1")
    if not s:
        return jsonify({"name": "—"})
    s["videos"] = one("SELECT COUNT(*) AS n FROM videos")["n"]
    s["cards"] = one("SELECT COUNT(*) AS n FROM cards")["n"]
    s["categories"] = one("SELECT COUNT(DISTINCT category) AS n FROM cards")["n"]
    return jsonify(s)


@app.route("/api/categories")
def api_categories():
    """All categories with card counts, ordered by size."""
    return jsonify(rows("""
        SELECT category, COUNT(*) AS cards
        FROM cards GROUP BY category ORDER BY cards DESC, category
    """))


@app.route("/api/kinds")
def api_kinds():
    return jsonify(rows("""
        SELECT kind, COUNT(*) AS cards FROM cards
        GROUP BY kind ORDER BY cards DESC
    """))


@app.route("/api/cards")
def api_cards():
    """Filterable card list. ?kind=&category=&video=&limit="""
    kind = request.args.get("kind")
    category = request.args.get("category")
    video = request.args.get("video")
    limit = min(int(request.args.get("limit", 500)), 2000)
    sql = """
      SELECT c.id, c.kind, c.category, c.title, c.content, c.reasoning,
             c.source_quote, c.video_id, v.title AS video_title, v.url AS video_url
      FROM cards c JOIN videos v ON v.id = c.video_id
      WHERE 1=1
    """
    params = []
    if kind:
        sql += " AND c.kind = ?"; params.append(kind)
    if category:
        sql += " AND c.category = ?"; params.append(category)
    if video:
        sql += " AND c.video_id = ?"; params.append(video)
    sql += " ORDER BY c.category, c.kind, c.title LIMIT ?"
    params.append(limit)
    out = rows(sql, tuple(params))
    for c in out:
        c["framework_steps"] = [
            r["step_content"] for r in rows(
                "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
                (c["id"],))
        ]
    return jsonify(out)


@app.route("/api/card/<int:cid>")
def api_card(cid):
    c = one("""
        SELECT c.*, v.title AS video_title, v.url AS video_url, v.one_line AS video_one_line
        FROM cards c JOIN videos v ON v.id = c.video_id
        WHERE c.id = ?
    """, (cid,))
    if not c:
        abort(404)
    c["framework_steps"] = [
        r["step_content"] for r in rows(
            "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
            (cid,))
    ]
    return jsonify(c)


@app.route("/api/videos")
def api_videos():
    return jsonify(rows("""
        SELECT v.id, v.title, v.url, v.one_line, v.best_for,
               (SELECT COUNT(*) FROM cards WHERE video_id = v.id) AS card_count
        FROM videos v
        ORDER BY card_count DESC, v.title
    """))


@app.route("/api/video/<vid>")
def api_video(vid):
    v = one("SELECT * FROM videos WHERE id=?", (vid,))
    if not v:
        abort(404)
    v["categories"] = [r["category"] for r in rows(
        "SELECT category FROM video_categories WHERE video_id=?", (vid,))]
    v["cards"] = rows("""
        SELECT id, kind, category, title, content, reasoning, source_quote
        FROM cards WHERE video_id = ? ORDER BY kind, title
    """, (vid,))
    for c in v["cards"]:
        c["framework_steps"] = [
            r["step_content"] for r in rows(
                "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
                (c["id"],))
        ]
    return jsonify(v)


def _fts_clean(q):
    # Replace anything that isn't a letter/digit with a space so FTS5
    # never sees `-`, `'`, quotes, etc. as syntax.
    import re as _re
    return " ".join(t for t in _re.split(r"[^A-Za-z0-9]+", q or "") if t)


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 50)), 500)
    if not q:
        return jsonify([])
    fq = _fts_clean(q)
    if not fq:
        return jsonify([])
    toks = [t for t in fq.split() if len(t) > 2]
    if not toks:
        return jsonify([])
    fq_match = " OR ".join(t + "*" for t in toks)
    hits = rows("""
        SELECT f.card_id AS id, c.kind, c.category, c.title, c.content,
               c.reasoning, c.source_quote, c.video_id,
               v.title AS video_title, v.url AS video_url,
               f.snippet, f.score
        FROM (
          SELECT card_id,
                 snippet(cards_fts, -1, '<mark>', '</mark>', ' … ', 16) AS snippet,
                 bm25(cards_fts) AS score
          FROM cards_fts WHERE cards_fts MATCH ?
          ORDER BY score LIMIT ?
        ) f
        JOIN cards c ON c.id = f.card_id
        JOIN videos v ON v.id = c.video_id
        ORDER BY f.score
    """, (fq_match, limit))
    for h in hits:
        h["framework_steps"] = [
            r["step_content"] for r in rows(
                "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
                (h["id"],))
        ]
    return jsonify(hits)


# ============================================================================
# AI-facing surface — for autonomous agents pulling distilled knowledge.
# Each card carries provenance (video + URL) so the AI can cite back to humans.
# ============================================================================

@app.route("/ai/manifest")
def ai_manifest():
    src = one("SELECT * FROM sources LIMIT 1")
    return jsonify({
        "name": "Distilled Knowledge Atlas",
        "version": "2.0",
        "purpose": (
            "AI-extracted knowledge cards from one or more domain experts' long-form "
            "video transcripts. Each card is paraphrased into clean, standalone, "
            "human-learnable form. Use these for retrieval-augmented learning."
        ),
        "philosophy": (
            "We do not return metrics, statistics, or raw transcripts. We return the "
            "valuable knowledge being communicated, distilled by AI, ready for a "
            "human to absorb in seconds."
        ),
        "sources": [src] if src else [],
        "card_kinds": [
            {"kind": "principle",    "meaning": "a general rule or law the expert teaches"},
            {"kind": "tactic",       "meaning": "a specific action to take in a specific situation"},
            {"kind": "warning",      "meaning": "something to avoid + why"},
            {"kind": "framework",    "meaning": "a named, multi-step approach (carries framework_steps)"},
            {"kind": "mental_model", "meaning": "a reframe / way of thinking"},
            {"kind": "phrase",       "meaning": "exact language to use"},
            {"kind": "quote",        "meaning": "a memorable line worth preserving"},
        ],
        "endpoints": [
            {"path": "/ai/manifest",                 "description": "this discovery doc"},
            {"path": "/ai/atlas",                    "description": "full atlas: all cards grouped by category"},
            {"path": "/ai/categories",               "description": "list of categories with card counts"},
            {"path": "/ai/cards",
             "query": "kind=&category=&video=&limit=",
             "description": "filterable card stream"},
            {"path": "/ai/search",
             "query": "q=&limit=",
             "description": "OR-matched FTS across card titles, content, reasoning, quotes"},
            {"path": "/ai/learn/<category>",
             "description": "study packet: all cards for a category, grouped by kind, ready for a human"},
            {"path": "/ai/teach",
             "query": "q=",
             "description": "given a question, returns a synthesized study packet drawn from matching cards"},
        ],
        "card_schema": {
            "id": "int",
            "kind": "principle|tactic|warning|framework|mental_model|phrase|quote",
            "category": "topical category (str)",
            "title": "5-10 word title (str)",
            "content": "1-3 sentences of clean knowledge (str)",
            "reasoning": "optional: why it works (str|null)",
            "source_quote": "optional: speaker's anchor quote (str|null)",
            "framework_steps": "optional: ordered steps (list[str])",
            "video_id": "provenance: source video id (str)",
            "video_title": "provenance: video title (str)",
            "video_url": "provenance: clickable YouTube URL (str)",
        },
        "recommended_usage": [
            "1. GET /ai/manifest once per session to see what's available.",
            "2. To teach a topic: GET /ai/learn/<category> for a ready-to-present study packet.",
            "3. To answer a question: GET /ai/teach?q=<question>.",
            "4. Always include video_url in the response to the human so they can verify.",
            "5. Never present raw transcripts — the cards are the deliverable.",
        ],
    })


@app.route("/.well-known/ai-knowledge.json")
def well_known():
    return ai_manifest()


@app.route("/ai/categories")
def ai_categories():
    return jsonify(rows("""
        SELECT category, COUNT(*) AS cards FROM cards
        GROUP BY category ORDER BY cards DESC
    """))


@app.route("/ai/cards")
def ai_cards():
    kind = request.args.get("kind")
    category = request.args.get("category")
    video = request.args.get("video")
    limit = min(int(request.args.get("limit", 200)), 2000)
    sql = """
      SELECT c.id, c.kind, c.category, c.title, c.content, c.reasoning,
             c.source_quote, c.video_id, v.title AS video_title, v.url AS video_url
      FROM cards c JOIN videos v ON v.id = c.video_id
      WHERE 1=1
    """
    params = []
    if kind: sql += " AND c.kind = ?"; params.append(kind)
    if category: sql += " AND c.category = ?"; params.append(category)
    if video: sql += " AND c.video_id = ?"; params.append(video)
    sql += " LIMIT ?"; params.append(limit)
    out = rows(sql, tuple(params))
    for c in out:
        c["framework_steps"] = [
            r["step_content"] for r in rows(
                "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
                (c["id"],))
        ]
    return jsonify(out)


@app.route("/ai/atlas")
def ai_atlas():
    """Full atlas: cards grouped by category, then by kind."""
    cats = rows("""
        SELECT category, COUNT(*) AS n FROM cards
        GROUP BY category ORDER BY n DESC
    """)
    atlas = []
    for c in cats:
        cards = rows("""
            SELECT id, kind, title, content, reasoning, source_quote,
                   video_id,
                   (SELECT title FROM videos WHERE id = cards.video_id) AS video_title,
                   (SELECT url FROM videos WHERE id = cards.video_id) AS video_url
            FROM cards WHERE category = ? ORDER BY kind, title
        """, (c["category"],))
        for card in cards:
            card["framework_steps"] = [
                r["step_content"] for r in rows(
                    "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
                    (card["id"],))
            ]
        by_kind = {}
        for card in cards:
            by_kind.setdefault(card["kind"], []).append(card)
        atlas.append({
            "category": c["category"],
            "card_count": c["n"],
            "cards_by_kind": by_kind,
        })
    return jsonify(atlas)


@app.route("/ai/search")
def ai_search():
    return api_search()


@app.route("/ai/learn/<category>")
def ai_learn(category):
    """Study packet for a category — designed for an AI to read to a human."""
    cards = rows("""
        SELECT c.id, c.kind, c.title, c.content, c.reasoning, c.source_quote,
               c.video_id, v.title AS video_title, v.url AS video_url
        FROM cards c JOIN videos v ON v.id = c.video_id
        WHERE c.category = ? ORDER BY
          CASE c.kind
            WHEN 'principle' THEN 1
            WHEN 'mental_model' THEN 2
            WHEN 'framework' THEN 3
            WHEN 'tactic' THEN 4
            WHEN 'phrase' THEN 5
            WHEN 'warning' THEN 6
            WHEN 'quote' THEN 7
            ELSE 8
          END, c.title
    """, (category,))
    for c in cards:
        c["framework_steps"] = [
            r["step_content"] for r in rows(
                "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
                (c["id"],))
        ]
    by_kind = {}
    for c in cards:
        by_kind.setdefault(c["kind"], []).append(c)
    return jsonify({
        "category": category,
        "card_count": len(cards),
        "study_order": ["principle", "mental_model", "framework", "tactic", "phrase", "warning", "quote"],
        "cards_by_kind": by_kind,
        "presentation_hint": (
            "Present principles first to frame the mental model, then frameworks "
            "(name + steps), then specific tactics + phrases. End with warnings."
        ),
    })


@app.route("/ai/teach")
def ai_teach():
    """Given a question, return a synthesized study packet from matching cards."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"question": q, "cards": [], "presentation_hint": ""})
    fq = _fts_clean(q)
    if not fq:
        return jsonify({"question": q, "cards": [], "presentation_hint": ""})
    toks = [t for t in fq.split() if len(t) > 2]
    fq_match = " OR ".join(t + "*" for t in toks) if toks else fq
    hits = rows("""
        SELECT f.card_id AS id, c.kind, c.category, c.title, c.content,
               c.reasoning, c.source_quote, c.video_id,
               v.title AS video_title, v.url AS video_url
        FROM (
          SELECT card_id, bm25(cards_fts) AS score
          FROM cards_fts WHERE cards_fts MATCH ?
          ORDER BY score LIMIT 30
        ) f
        JOIN cards c ON c.id = f.card_id
        JOIN videos v ON v.id = c.video_id
        ORDER BY
          CASE c.kind
            WHEN 'principle' THEN 1 WHEN 'mental_model' THEN 2
            WHEN 'framework' THEN 3 WHEN 'tactic' THEN 4
            WHEN 'phrase' THEN 5 WHEN 'warning' THEN 6 ELSE 7
          END
    """, (fq_match,))
    for c in hits:
        c["framework_steps"] = [
            r["step_content"] for r in rows(
                "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
                (c["id"],))
        ]
    cats = sorted({c["category"] for c in hits})
    return jsonify({
        "question": q,
        "card_count": len(hits),
        "categories_touched": cats,
        "cards": hits,
        "presentation_hint": (
            "These cards are pre-ordered for teaching: principles → mental models → "
            "frameworks → tactics → phrases → warnings. Present them in that order. "
            "Always include video_url so the human can verify."
        ),
    })


# ============================================================================
# Cross-source correlation — the value multiplier when 2+ sources are indexed.
# Each endpoint works meaningfully with one source too, but unlocks fully as
# you add more domain experts.
# ============================================================================

@app.route("/ai/sources")
def ai_sources():
    """Every indexed source with the size of its contribution."""
    out = []
    for s in rows("SELECT * FROM sources"):
        sid = s["id"]
        s["videos"] = one("SELECT COUNT(*) AS n FROM videos WHERE source_id=?", (sid,))["n"]
        s["cards"] = one("SELECT COUNT(*) AS n FROM cards WHERE source_id=?", (sid,))["n"]
        s["categories"] = one(
            "SELECT COUNT(DISTINCT category) AS n FROM cards WHERE source_id=?", (sid,))["n"]
        out.append(s)
    return jsonify(out)


@app.route("/ai/cross/coverage")
def ai_cross_coverage():
    """Matrix: which sources cover which categories, with depth (card count).
    A human or AI can scan this to see which experts speak to which topics."""
    grid = rows("""
        SELECT category, source_id, COUNT(*) AS cards
        FROM cards GROUP BY category, source_id
        ORDER BY category, cards DESC
    """)
    out = {}
    for r in grid:
        out.setdefault(r["category"], []).append({
            "source_id": r["source_id"], "cards": r["cards"]
        })
    return jsonify([
        {"category": k, "sources": v,
         "total_cards": sum(s["cards"] for s in v),
         "source_count": len(v)}
        for k, v in sorted(out.items(),
                           key=lambda kv: (-len(kv[1]), -sum(s["cards"] for s in kv[1])))
    ])


@app.route("/ai/cross/concept/<term>")
def ai_cross_concept(term):
    """Show how each indexed source covers a concept.
    Groups matching cards by source so an AI can compare perspectives."""
    fq = _fts_clean(term)
    if not fq:
        return jsonify({"term": term, "by_source": []})
    toks = [t for t in fq.split() if len(t) > 2]
    fq_match = " OR ".join(t + "*" for t in toks) if toks else fq
    hits = rows("""
        SELECT c.id, c.source_id, c.kind, c.category, c.title, c.content,
               c.reasoning, c.video_id,
               v.title AS video_title, v.url AS video_url,
               (SELECT name FROM sources WHERE id = c.source_id) AS source_name
        FROM cards_fts f
        JOIN cards c ON c.id = f.card_id
        JOIN videos v ON v.id = c.video_id
        WHERE cards_fts MATCH ?
        ORDER BY bm25(cards_fts)
        LIMIT 80
    """, (fq_match,))
    for h in hits:
        h["framework_steps"] = [
            r["step_content"] for r in rows(
                "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
                (h["id"],))
        ]
    by_source = {}
    for h in hits:
        sid = h["source_id"]
        by_source.setdefault(sid, {
            "source_id": sid,
            "source_name": h["source_name"],
            "cards": [],
        })["cards"].append(h)
    return jsonify({
        "term": term,
        "source_count": len(by_source),
        "total_cards": len(hits),
        "by_source": list(by_source.values()),
        "presentation_hint": (
            "Each source represents one domain expert. Present what each expert "
            "says about this concept side-by-side so the human can compare. "
            "Note agreements (consensus) and any conflicts."
        ),
    })


@app.route("/ai/cross/consensus")
def ai_cross_consensus():
    """Identify cards from DIFFERENT sources that overlap heavily in language —
    proxies for cross-expert consensus.

    Heuristic: title token overlap >= 2 between cards from different sources,
    same kind. Cheap, deterministic, no embedding model required."""
    cards = rows("""
        SELECT id, source_id, kind, category, title, content, video_id
        FROM cards
    """)
    # tokenize titles
    def toks(s):
        return set(w for w in (s or "").lower().split() if len(w) > 3)

    groups = {}
    for c in cards:
        c["_toks"] = toks(c["title"])

    seen_pairs = set()
    clusters = []
    for i, a in enumerate(cards):
        for b in cards[i + 1:]:
            if a["source_id"] == b["source_id"]:
                continue
            if a["kind"] != b["kind"]:
                continue
            overlap = a["_toks"] & b["_toks"]
            if len(overlap) >= 2:
                key = tuple(sorted([a["id"], b["id"]]))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                clusters.append({
                    "shared_tokens": sorted(overlap),
                    "kind": a["kind"],
                    "cards": [
                        {k: v for k, v in a.items() if not k.startswith("_")},
                        {k: v for k, v in b.items() if not k.startswith("_")},
                    ],
                })
    return jsonify({
        "cluster_count": len(clusters),
        "note": ("Heuristic title-token overlap across different sources. With "
                 "only one source indexed, this list will be empty — add another "
                 "domain expert via add_source.py to unlock cross-source consensus."),
        "clusters": clusters[:200],
    })


@app.route("/ai/cross/compendium/<category>")
def ai_cross_compendium(category):
    """Everything every indexed expert says on a topic, ordered for teaching.
    Drop-in replacement for /ai/learn/<category> but grouped by source first."""
    cards = rows("""
      SELECT c.id, c.source_id, c.kind, c.title, c.content, c.reasoning,
             c.source_quote, c.video_id, v.title AS video_title, v.url AS video_url,
             (SELECT name FROM sources WHERE id = c.source_id) AS source_name
      FROM cards c JOIN videos v ON v.id = c.video_id
      WHERE c.category = ?
      ORDER BY c.source_id,
        CASE c.kind WHEN 'principle' THEN 1 WHEN 'mental_model' THEN 2
                    WHEN 'framework' THEN 3 WHEN 'tactic' THEN 4
                    WHEN 'phrase' THEN 5 WHEN 'warning' THEN 6 ELSE 7 END
    """, (category,))
    for c in cards:
        c["framework_steps"] = [
            r["step_content"] for r in rows(
                "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
                (c["id"],))
        ]
    by_source = {}
    for c in cards:
        by_source.setdefault(c["source_id"], {
            "source_id": c["source_id"], "source_name": c["source_name"],
            "cards": []
        })["cards"].append(c)
    return jsonify({
        "category": category,
        "total_cards": len(cards),
        "source_count": len(by_source),
        "by_source": list(by_source.values()),
        "presentation_hint": (
            "Present each expert's view sequentially. If two experts say similar "
            "things, point that out (cross-source consensus = high confidence). "
            "If they conflict, present both with their reasoning."
        ),
    })


@app.route("/export/<path:filename>")
def export(filename):
    return send_from_directory(str(ROOT / "data" / "export"), filename)


# ============================================================================
# Self-serve ingestion — drop a channel URL, the pipeline runs end-to-end.
# ============================================================================

class _Cancelled(Exception):
    """Raised internally when the user has requested job cancellation."""


def _stream_subprocess(args, job_id, step_label):
    """Run a child process, capture stdout/stderr into the job log.
    The child runs in its own process group so a cancel request can terminate
    the whole tree (yt-dlp, the LLM extractor's HTTP client, etc.)."""
    if _job_is_cancelled(job_id):
        raise _Cancelled()

    _job_log(job_id, f"$ {' '.join(args)}")
    proc = subprocess.Popen(
        args, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        start_new_session=True,  # new process group → killable as a unit
    )
    _job_set_proc(job_id, proc)
    try:
        for line in proc.stdout:
            _job_log(job_id, line.rstrip())
            if _job_is_cancelled(job_id):
                _terminate_proc(proc)
                raise _Cancelled()
        rc = proc.wait()
    finally:
        _job_set_proc(job_id, None)

    if _job_is_cancelled(job_id):
        raise _Cancelled()
    if rc != 0:
        # SIGTERM exits with negative return code (-15) — treat as cancel
        if rc < 0:
            raise _Cancelled()
        raise RuntimeError(f"{step_label} exited {rc}")


def _ingest_pipeline(job_id, payload):
    try:
        url       = payload["url"].strip()
        sid       = payload.get("source_id") or _derive_id_from_url(url)
        name      = payload.get("name") or sid
        domain    = payload.get("domain") or ""
        expertise = payload.get("expertise") or ""
        api_key   = (payload.get("api_key") or "").strip()
        save_key  = payload.get("save_api_key", True)
        provider  = (payload.get("provider") or _auto_provider() or "xai").lower()
        model     = (payload.get("model") or "").strip() or PROVIDER_DEFAULT_MODEL.get(provider)
        # Time-window for the fetch step. UI presets: 1d/1w/1m/3m/6m/1y/all
        # or explicit YYYYMMDD bounds via since/until.
        window    = (payload.get("window") or "all").lower()
        since     = (payload.get("since") or "").strip() or None
        until     = (payload.get("until") or "").strip() or None
        if window not in {"1d", "1w", "1m", "3m", "6m", "1y", "all"}:
            window = "all"

        # Normalize URL — yt-dlp wants /videos for channels
        if "/videos" not in url and "/watch" not in url and "/playlist" not in url:
            channel_url = url.rstrip("/") + "/videos"
        else:
            channel_url = url

        _job_set(job_id, status="running", step="register", percent=2,
                 message="Registering source", source_id=sid)
        # 1. upsert source in sources.json
        doc = json.loads(SOURCES_PATH.read_text())
        sources = doc.setdefault("sources", [])
        existing = next((s for s in sources if s["id"] == sid), None)
        record = {
            "id": sid, "name": name, "kind": "youtube_channel",
            "url": url, "domain": domain, "expertise": expertise,
            "language": "en",
            "first_indexed": (existing or {}).get("first_indexed") or str(date.today()),
            "license": "transcripts derived from public YouTube auto-captions",
            "trust_notes": "",
        }
        if existing:
            existing.update(record)
        else:
            sources.append(record)
        SOURCES_PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        _job_log(job_id, f"source '{sid}' registered")

        # 2. fetch transcripts
        _job_set(job_id, step="fetch", percent=8,
                 message=f"Fetching transcripts from {channel_url}")
        fetch_cmd = [sys.executable, str(ROOT / "fetch_channel.py"),
                     "--url", channel_url, "--source", sid,
                     "--window", window]
        if since:
            fetch_cmd += ["--since", since]
        if until:
            fetch_cmd += ["--until", until]
        _job_log(job_id, f"window={window} since={since or '-'} until={until or '-'}")
        _stream_subprocess(fetch_cmd, job_id, "fetch_channel.py")
        _job_set(job_id, percent=55)

        # 3. AI extraction (requires API key for the chosen provider)
        env_var = PROVIDER_ENV.get(provider, "XAI_API_KEY")
        if api_key:
            if save_key:
                _save_api_key(env_var, api_key)
                _job_log(job_id, f"saved {env_var} to .env (chmod 600)")
            os.environ[env_var] = api_key

        if os.environ.get(env_var):
            _job_set(job_id, step="extract", percent=60,
                     message=f"Extracting knowledge via {provider} / {model}")
            _stream_subprocess(
                [sys.executable, str(ROOT / "extract_knowledge.py"),
                 "--source", sid, "--provider", provider, "--model", model],
                job_id, "extract_knowledge.py")
            _job_set(job_id, percent=90)
        else:
            _job_log(job_id, f"no {env_var} — skipping extraction.")
            _job_log(job_id, "→ to finish: paste this in Claude Desktop:")
            _job_log(job_id,
                f'   "Run extract_knowledge.py --source {sid} --provider {provider} and rebuild the atlas."')
            _job_set(job_id, step="awaiting_extraction", percent=70,
                     message=("Transcripts ready. Extraction skipped (no API key). "
                              "Provide one and re-run, or use Claude Desktop."))
            _job_set(job_id, status="awaiting_extraction")
            return

        # Cancel checkpoint before aggregate (the last expensive step)
        if _job_is_cancelled(job_id):
            raise _Cancelled()

        # 4. rebuild unified atlas
        _job_set(job_id, step="aggregate", percent=92,
                 message="Rebuilding unified atlas")
        _stream_subprocess(
            [sys.executable, str(ROOT / "build_knowledge.py")],
            job_id, "build_knowledge.py")
        _job_set(job_id, status="done", step="done", percent=100,
                 message=f"Source '{sid}' is live in the atlas.")
    except _Cancelled:
        _job_log(job_id, "STOPPED by user. Partial work is preserved — re-running")
        _job_log(job_id, "the ingestion is idempotent and resumes from where it stopped.")
        _job_set(job_id, status="cancelled", step="cancelled",
                 message="Stopped. Partial work preserved; re-run to resume.")
    except Exception as e:
        _job_log(job_id, f"ERROR: {e}")
        _job_set(job_id, status="error", error=str(e), percent=0,
                 message=f"Failed: {e}")


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    payload = request.get_json(silent=True) or {}
    if not payload.get("url"):
        return jsonify({"error": "url required"}), 400
    job_id = uuid.uuid4().hex[:10]
    _job_set(job_id, status="queued", step="queued", percent=0,
             message="Queued", log=[])
    t = threading.Thread(target=_ingest_pipeline, args=(job_id, payload), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/ingest/status/<job_id>")
def api_ingest_status(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"error": "unknown job"}), 404
        # Strip non-JSON-serializable fields (proc) and trim log
        out = {k: v for k, v in j.items() if k != "proc"}
        out["log"] = (out.get("log") or [])[-40:]
    return jsonify(out)


@app.route("/api/ingest/cancel/<job_id>", methods=["POST"])
def api_ingest_cancel(job_id):
    """Request a clean stop. Sets the cancel flag and SIGTERMs the running subprocess
    group. The pipeline thread sees the flag between stages and exits with status=cancelled.
    Partial transcripts / partial card files are kept on disk; the next run is
    idempotent and resumes from where this one stopped."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"error": "unknown job"}), 404
        if j.get("status") in ("done", "error", "cancelled"):
            return jsonify({"job_id": job_id, "status": j.get("status"),
                            "note": "already finished — nothing to stop"}), 200
        j["cancelled"] = True
        proc = j.get("proc")
    # Kill outside the lock so we don't hold it during wait()
    if proc is not None:
        _terminate_proc(proc)
    _job_log(job_id, "received STOP request")
    return jsonify({"job_id": job_id, "status": "cancelling"})


@app.route("/api/ingest/has_key")
def api_ingest_has_key():
    return jsonify({
        "providers": {
            p: {
                "has_key": bool(os.environ.get(env)),
                "default_model": PROVIDER_DEFAULT_MODEL[p],
            }
            for p, env in PROVIDER_ENV.items()
        },
        "auto_provider": _auto_provider(),
    })


if __name__ == "__main__":
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found at {DB_PATH} — run build_knowledge.py first.")
    print("Knowledge Atlas serving at http://127.0.0.1:5179/")
    app.run(host="127.0.0.1", port=5179, debug=False)
