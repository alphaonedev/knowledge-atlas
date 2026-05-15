#!/usr/bin/env python3
"""
build_knowledge.py — aggregate per-video knowledge-card JSON into SQLite.

Reads:  data/knowledge/<video_id>.json (one per video, produced by extraction agents)
Writes: a fresh `knowledge.db` plus consolidated `data/export/knowledge_atlas.json`.

The schema deliberately drops all metrics (sentiment, readability, word counts,
view counts, n-grams, topics, correlations). What remains is the knowledge itself:
sources, videos as references, and the cards.
"""

import json
import sqlite3
from pathlib import Path
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parent
KNOWLEDGE_DIR = ROOT / "data" / "knowledge"
DATA = ROOT / "data"
EXPORT = DATA / "export"
DB_PATH = DATA / "knowledge.db"
SOURCES_PATH = ROOT / "sources.json"

EXPORT.mkdir(parents=True, exist_ok=True)

VALID_KINDS = {"principle", "tactic", "warning", "framework", "mental_model", "phrase", "quote"}


def _emit_progress(event, **fields):
    """Emit a single-line structured progress event for app.py to parse.
    Format: @@PROGRESS@@ {"event":"...","phase":"...",...}"""
    payload = {"event": event, **fields}
    print(f"@@PROGRESS@@ {json.dumps(payload, default=str)}", flush=True)


def main():
    import time as _time
    _started = _time.time()
    _emit_progress("phase_start", phase="aggregate",
                   message="Aggregating into SQLite + JSON + FTS")

    with open(SOURCES_PATH) as f:
        sources = json.load(f)["sources"]

    files = sorted(KNOWLEDGE_DIR.glob("*.json"))
    files = [f for f in files if f.name != "SCHEMA.md" and not f.name.startswith("_")]

    cards_by_video = {}
    summaries = {}
    for fp in files:
        try:
            doc = json.loads(fp.read_text())
        except Exception as e:
            print(f"  ! skipped {fp.name}: {e}")
            continue
        vid = doc.get("video_id") or fp.stem
        summaries[vid] = {
            "video_id": vid,
            "video_title": doc.get("video_title", ""),
            "video_url": doc.get("video_url", f"https://www.youtube.com/watch?v={vid}"),
            "one_line": doc.get("one_line", ""),
            "best_for": doc.get("best_for", ""),
            "categories": doc.get("categories", []),
        }
        cards_by_video[vid] = doc.get("cards", [])

    # Drop & rebuild DB (metrics tier is gone for good)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE sources (
      id TEXT PRIMARY KEY,
      name TEXT, kind TEXT, url TEXT, domain TEXT, expertise TEXT,
      language TEXT, first_indexed TEXT, license TEXT, trust_notes TEXT
    );
    CREATE TABLE videos (
      id TEXT PRIMARY KEY,
      source_id TEXT,
      title TEXT,
      url TEXT,
      one_line TEXT,
      best_for TEXT
    );
    CREATE TABLE video_categories (
      video_id TEXT,
      category TEXT
    );
    CREATE TABLE cards (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source_id TEXT,
      video_id TEXT,
      kind TEXT,
      category TEXT,
      title TEXT,
      content TEXT,
      reasoning TEXT,
      source_quote TEXT
    );
    CREATE TABLE framework_steps (
      card_id INTEGER,
      step_number INTEGER,
      step_content TEXT
    );
    CREATE INDEX idx_cards_video ON cards(video_id);
    CREATE INDEX idx_cards_kind ON cards(kind);
    CREATE INDEX idx_cards_cat ON cards(category);
    CREATE INDEX idx_cards_source ON cards(source_id);
    CREATE INDEX idx_vc_cat ON video_categories(category);

    CREATE VIRTUAL TABLE cards_fts USING fts5(
      card_id UNINDEXED, title, content, reasoning, source_quote,
      tokenize='porter unicode61'
    );
    """)

    # sources
    for s in sources:
        cur.execute("""INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)""", (
            s.get("id"), s.get("name"), s.get("kind"), s.get("url"),
            s.get("domain"), s.get("expertise"), s.get("language"),
            s.get("first_indexed"), s.get("license"), s.get("trust_notes"),
        ))

    default_source = sources[0]["id"] if sources else "unknown"

    # videos + categories + cards
    total_cards = 0
    kind_counts = Counter()
    category_counts = Counter()
    for vid, summary in summaries.items():
        cur.execute("""INSERT INTO videos VALUES (?,?,?,?,?,?)""", (
            vid, default_source,
            summary.get("video_title"),
            summary.get("video_url"),
            summary.get("one_line"),
            summary.get("best_for"),
        ))
        for cat in summary.get("categories", []):
            cur.execute("INSERT INTO video_categories VALUES (?,?)", (vid, cat))

        for c in cards_by_video.get(vid, []):
            kind = c.get("kind", "").strip().lower().replace(" ", "_").replace("-", "_")
            if kind not in VALID_KINDS:
                # normalize common variants
                kind = {"principles": "principle", "tactics": "tactic",
                        "warnings": "warning", "frameworks": "framework",
                        "mental-model": "mental_model"}.get(kind, "principle")
            category = (c.get("category") or "general").strip().lower()
            cur.execute("""
                INSERT INTO cards(source_id, video_id, kind, category, title,
                                  content, reasoning, source_quote)
                VALUES (?,?,?,?,?,?,?,?)
            """, (default_source, vid, kind, category,
                  c.get("title", ""), c.get("content", ""),
                  c.get("reasoning", "") or None,
                  c.get("source_quote", "") or None))
            card_id = cur.lastrowid

            steps = c.get("framework_steps") or []
            for i, step in enumerate(steps, 1):
                cur.execute("INSERT INTO framework_steps VALUES (?,?,?)",
                            (card_id, i, step))

            cur.execute("""
                INSERT INTO cards_fts(card_id, title, content, reasoning, source_quote)
                VALUES (?,?,?,?,?)
            """, (card_id, c.get("title", ""), c.get("content", ""),
                  c.get("reasoning") or "", c.get("source_quote") or ""))

            total_cards += 1
            kind_counts[kind] += 1
            category_counts[category] += 1

    con.commit()

    # JSON exports
    atlas = {
        "source": sources[0] if sources else None,
        "videos": list(summaries.values()),
        "cards_by_category": defaultdict(list),
        "cards_by_kind": defaultdict(list),
        "totals": {
            "videos": len(summaries),
            "cards": total_cards,
            "kind_counts": dict(kind_counts),
            "category_counts": dict(category_counts),
        },
    }
    all_cards = [dict(r) for r in cur.execute("""
        SELECT c.id, c.kind, c.category, c.title, c.content, c.reasoning,
               c.source_quote, c.video_id, v.title AS video_title, v.url AS video_url
        FROM cards c JOIN videos v ON v.id = c.video_id
    """).fetchall()]
    cur2 = con.cursor()
    for d in all_cards:
        d["framework_steps"] = [
            r[0] for r in cur2.execute(
                "SELECT step_content FROM framework_steps WHERE card_id=? ORDER BY step_number",
                (d["id"],)
            ).fetchall()
        ]
        atlas["cards_by_category"][d["category"]].append(d)
        atlas["cards_by_kind"][d["kind"]].append(d)

    atlas["cards_by_category"] = dict(atlas["cards_by_category"])
    atlas["cards_by_kind"] = dict(atlas["cards_by_kind"])

    (EXPORT / "knowledge_atlas.json").write_text(
        json.dumps(atlas, indent=2, default=str), encoding="utf-8")

    con.close()

    print(f"Aggregated {total_cards} cards across {len(summaries)} videos.")
    print(f"Categories: {len(category_counts)}")
    print("By kind:")
    for k, n in kind_counts.most_common():
        print(f"  {k:18s}  {n}")
    print(f"Wrote {DB_PATH} and {EXPORT/'knowledge_atlas.json'}")
    _emit_progress("phase_done", phase="aggregate",
                   elapsed_sec=round(_time.time() - _started, 1),
                   summary={
                       "videos": len(summaries),
                       "cards": total_cards,
                       "categories": len(category_counts),
                   })


if __name__ == "__main__":
    main()
