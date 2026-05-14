# The Knowledge Atlas Pipeline — A Repeatable Standard

A standardized, multi-source pipeline that turns any domain expert's video
corpus into a queryable knowledge base for both humans and AI agents.

## Why this exists

The internet has thousands of subject-matter experts giving away decades of
hard-won knowledge across YouTube, podcasts, and long-form interviews. The
content is locked inside hours of speech a single human cannot reasonably
consume. This pipeline:

1. **Ingests** the raw spoken content (transcripts).
2. **Distills** it through AI into clean, paraphrased *knowledge cards* — each
   a standalone, learnable unit with provenance back to the source.
3. **Indexes** every card into a unified store keyed by source.
4. **Exposes** the result through a stable JSON API that both a human UI and
   any autonomous AI agent can read from.

When two or more sources are indexed, the cross-source surface
(`/ai/cross/*`) lets an AI compare, consense, and correlate knowledge across
experts — accelerating learning by orders of magnitude.

## Architecture

```
   sources.json  (registry of indexed experts)
        ▼
  fetch_channel.py  ──► transcripts/  (per source)
        ▼
  AI extraction  ────► data/knowledge/<video_id>.json
   (any LLM following data/knowledge/SCHEMA.md)
        ▼
  build_knowledge.py ─► data/knowledge.db (SQLite, source of truth)
                       data/export/*.json (portable JSON snapshots)
        ▼
  app.py  ────────────► localhost HTTP
        │
        ├── /api/*      human Knowledge Atlas UI
        ├── /ai/*       single-source agent surface
        └── /ai/cross/* multi-source correlation surface
```

## The five files that define the standard

| File | Role |
|------|------|
| `sources.json` | The registry of indexed experts. One entry per source. |
| `data/knowledge/SCHEMA.md` | The card contract. Every extraction must produce JSON conforming to this. |
| `build_knowledge.py` | Aggregator — rebuilds `knowledge.db` from `data/knowledge/*.json`. Idempotent. |
| `app.py` | The HTTP surface. Both UI and AI agents read from here. |
| `PIPELINE.md` (this file) | The standard, so anyone — human or AI — can extend the system. |

## Adding a new expert

```
python3 add_source.py \
    --id alexhormozi \
    --name "Alex Hormozi" \
    --url "https://www.youtube.com/@AlexHormozi" \
    --domain "business, scaling, sales, offers" \
    --expertise "founder of Acquisition.com; $100M Offers; lead gen"
```

That command:

1. Adds the source to `sources.json`.
2. Runs `fetch_channel.py` → downloads every video's transcript into
   `sources/alexhormozi/transcripts/`.
3. Prints next-step instructions for the AI extraction.

Then have your AI of choice (Claude / GPT / local model) read each transcript
with the schema and produce one JSON file per video under
`data/knowledge/`. The Claude Agent SDK pattern: spawn N parallel agents,
each given:

  - the schema file path,
  - a list of `<video_id>` files in `sources/<source_id>/transcripts/`,
  - instructions to write `data/knowledge/<video_id>.json` per the schema.

Once the JSON files exist:

```
python3 build_knowledge.py
```

Rebuilds the unified atlas. The HTTP surface auto-includes the new source
the moment you restart `app.py`.

## The card contract (summary — full schema in data/knowledge/SCHEMA.md)

```json
{
  "video_id": "...",
  "video_title": "...",
  "video_url": "https://www.youtube.com/watch?v=...",
  "one_line": "core thesis in one sentence",
  "best_for": "watch this if you...",
  "categories": ["topical tags"],
  "cards": [
    {
      "kind": "principle | tactic | warning | framework | mental_model | phrase | quote",
      "category": "topical tag",
      "title": "5-10 word title",
      "content": "1-3 clean sentences — the knowledge itself, paraphrased",
      "reasoning": "why it works (optional)",
      "source_quote": "verbatim anchor quote (optional)",
      "framework_steps": ["step 1", "step 2", "..."]
    }
  ]
}
```

Each card is **standalone, paraphrased, attributable**. No transcript noise.
No statistics. The card *is* the knowledge.

## The AI surface

### Single-source endpoints

  - `GET /ai/manifest` — discovery doc (capabilities, schema, recommended usage)
  - `GET /ai/sources` — every indexed source, with size of contribution
  - `GET /ai/categories` — topic taxonomy
  - `GET /ai/cards?kind=&category=&video=&limit=` — filterable card stream
  - `GET /ai/search?q=&limit=` — FTS across card title/content/reasoning/quote
  - `GET /ai/learn/<category>` — study packet for one topic, ordered for teaching
  - `GET /ai/teach?q=<question>` — synthesized teaching packet for a question

### Cross-source endpoints (the value multiplier)

  - `GET /ai/cross/coverage` — matrix: which sources cover which topics
  - `GET /ai/cross/concept/<term>` — every source's take on one concept, grouped by source
  - `GET /ai/cross/compendium/<category>` — every source's cards on one topic, in teaching order
  - `GET /ai/cross/consensus` — pairs of cards across sources that look like the same idea (basis for "X experts agree that...")

These let an AI agent stitch knowledge across experts. With two sources
indexed, an AI can answer "what do business strategists agree about lead
generation, and where do they conflict?" by stitching `/ai/cross/concept`
results into a unified briefing.

## The repeatability claim

The pipeline is repeatable because:

1. **Schema is stable.** Every card looks the same regardless of source.
2. **Source is a first-class dimension.** Every card carries `source_id`;
   every endpoint can filter, group, or correlate by it.
3. **Idempotent rebuild.** `build_knowledge.py` always rebuilds from
   `data/knowledge/*.json`. Re-running is safe and cheap.
4. **One contract for AI extractors.** As long as the AI produces JSON
   matching `SCHEMA.md`, the rest of the pipeline doesn't care which model
   or vendor extracted it. Swap Claude → GPT → local Llama at will.

## The exponential-learning argument

With one source: a human gets one expert's distilled corpus — already 10–100×
faster than watching the videos.

With ten sources in the same domain: cross-source correlation surfaces the
underlying laws (what every expert agrees on) and the open questions (where
they conflict). A human can absorb the consensus in minutes and then
strategically watch only the videos where genuine disagreement signals the
edge of knowledge in that domain.

With one hundred sources spanning multiple domains: an AI agent acting on
behalf of the human can answer "teach me what to know about X" by stitching
together cards from every relevant expert, attributed and verifiable. The
human's effective tutor pool scales linearly with sources indexed.

## File layout reference

```
YT/
├── sources.json                  registry of indexed experts
├── fetch_channel.py              downloads transcripts (--url --source)
├── add_source.py                 one-shot CLI: register + fetch
├── build_knowledge.py            aggregator → SQLite + JSON exports
├── app.py                        Flask localhost server (UI + AI surface)
├── ai_client_demo.py             worked example of an AI agent client
├── PIPELINE.md                   this document
├── data/
│   ├── knowledge/
│   │   ├── SCHEMA.md             ← the extraction contract
│   │   └── <video_id>.json       ← AI-produced card files
│   ├── knowledge.db              ← SQLite store (rebuilt by build_knowledge.py)
│   └── export/                   ← portable JSON snapshots
├── sources/<source_id>/          per-source storage (one tree per indexed expert)
│   ├── transcripts/
│   ├── raw_srt/
│   └── channel_metadata.json
├── templates/index.html          UI
└── static/                       (static assets if needed)
```

## How an AI agent uses this

```
1. GET /ai/manifest                           # discover capabilities
2. GET /ai/sources                            # what experts are indexed
3. GET /ai/teach?q=<user_question>            # one-shot answer with citations
   — or —
   GET /ai/cross/concept/<concept>            # multi-expert grounding
   GET /ai/cross/consensus                    # find agreed-upon principles
4. Always include video_url in citations to the human.
5. Never present raw transcripts. The cards ARE the knowledge.
```

That's the standard. Once any number of sources is plumbed in through this
pipeline, the human's effective access to domain expertise scales without
bound.
