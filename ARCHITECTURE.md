# Knowledge Atlas — Reference Architecture

A complete map of how the system is designed: data flow, components, APIs, MCP integration, schema, extensibility points, and security model.

---

## Design principles

1. **SQLite is the single source of truth.** Everything else is a view or a service.
2. **Source is a first-class dimension.** Every artifact carries a `source_id`. The whole API can filter, group, or correlate by it.
3. **Schema is the contract.** Any LLM (or human) producing knowledge cards must conform to `data/knowledge/SCHEMA.md`. The pipeline doesn't care which model produced the JSON.
4. **Two surfaces, one backend.** Human dashboard and AI agents read from the same SQLite via parallel HTTP routes (`/api/*` for the UI, `/ai/*` for agents). MCP gives local AIs direct access without going through HTTP.
5. **Idempotent rebuild.** `build_knowledge.py` always rebuilds from the on-disk JSON. Re-running is safe and cheap.
6. **Localhost-only by default.** No cloud, no telemetry, no remote auth model. Trust boundary is the machine.
7. **No metrics.** The deliverable is paraphrased knowledge a human can absorb in seconds, not statistics about the corpus.

---

## System overview

```
                       ┌─────────────────────────────────────────────────────┐
                       │                  KNOWLEDGE ATLAS                    │
                       └─────────────────────────────────────────────────────┘

   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
   │   SOURCES    │ →  │   FETCHER    │ →  │  EXTRACTOR   │ →  │  AGGREGATOR  │
   │ sources.json │    │ yt-dlp       │    │  LLM (xAI /  │    │ build_       │
   │   registry   │    │ transcripts  │    │  Anthropic / │    │ knowledge.py │
   │              │    │              │    │  OpenAI)     │    │              │
   └──────────────┘    └──────────────┘    └──────────────┘    └──────┬───────┘
                                                                       │
                                                                       ▼
                                                          ┌────────────────────────┐
                                                          │     SQLite atlas       │
                                                          │   (knowledge.db)       │
                                                          │  + FTS5 full-text idx  │
                                                          └────────┬───────────────┘
                                                                   │
                            ┌──────────────────────┬───────────────┼─────────────────┐
                            ▼                      ▼               ▼                 ▼
                   ┌────────────────┐    ┌────────────────┐  ┌──────────────┐  ┌───────────────┐
                   │  Flask /api/*  │    │  Flask /ai/*   │  │ /ai/cross/*  │  │  MCP server   │
                   │  human UI      │    │ agent surface  │  │ multi-source │  │  (stdio)      │
                   └───────┬────────┘    └───────┬────────┘  └──────┬───────┘  └───────┬───────┘
                           ▼                     ▼                  ▼                  ▼
                   ┌────────────────┐    ┌────────────────────────────────┐  ┌───────────────┐
                   │  Browser SPA   │    │   Autonomous AI agents         │  │ Claude Desktop│
                   │  templates/    │    │   (Claude, GPT, local models   │  │ Claude Code   │
                   │  index.html    │    │    via HTTP+JSON)              │  │ Cursor, etc.  │
                   └────────────────┘    └────────────────────────────────┘  └───────────────┘
```

---

## The five files that define the standard

| File | Role |
|---|---|
| `sources.json` | Registry of indexed experts. One entry per source. The first-class identity dimension. |
| `data/knowledge/SCHEMA.md` | The extraction contract. Every LLM call must produce JSON conforming to this. |
| `build_knowledge.py` | Aggregator — rebuilds `knowledge.db` from `data/knowledge/*.json`. Idempotent. |
| `app.py` | HTTP service tier. Both UI and AI agents read from here. |
| `ARCHITECTURE.md` | This document — the standard so anyone (human or AI) can extend the system. |

---

## Pipeline stages

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │ STAGE 1: REGISTER                                                   │
   │ add_source.py  →  upsert entry in sources.json                      │
   └─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ STAGE 2: FETCH                                                      │
   │ fetch_channel.py --source <id> --url <channel-url>                  │
   │   → yt-dlp lists every video on the channel                         │
   │   → downloads SRT subtitles (manual first, auto-captions fallback)  │
   │   → de-duplicates rolling caption overlap                           │
   │   → writes sources/<id>/transcripts/<vid>.txt (plain)               │
   │   → writes sources/<id>/transcripts/<vid>.timed.txt ([mm:ss] cues)  │
   │   → writes sources/<id>/channel_metadata.json                       │
   └─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ STAGE 3: EXTRACT  (the only step that needs an LLM)                 │
   │ extract_knowledge.py --source <id>                                  │
   │   → for each transcript without a corresponding JSON file:          │
   │       → calls xAI Grok / Anthropic Claude / OpenAI with:             │
   │           system: SCHEMA.md (cached when supported)                 │
   │           user:   transcript + video metadata                       │
   │       → expects strict JSON matching the card schema                │
   │       → writes data/knowledge/<vid>.json (one file per video)       │
   └─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ STAGE 4: AGGREGATE                                                  │
   │ build_knowledge.py                                                  │
   │   → loads sources.json + every data/knowledge/<vid>.json            │
   │   → rebuilds knowledge.db from scratch                              │
   │   → creates FTS5 virtual index over title/content/reasoning/quote   │
   │   → exports portable JSON snapshots to data/export/                 │
   └─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ STAGE 5: SERVE                                                      │
   │ app.py             →  HTTP at 127.0.0.1:5179                        │
   │ mcp_server.py      →  spawned by MCP clients (Claude Desktop, etc.) │
   └─────────────────────────────────────────────────────────────────────┘
```

---

## The card schema (the contract)

Every JSON file in `data/knowledge/` must conform to this:

```json
{
  "video_id": "string",
  "video_title": "string",
  "video_url": "https://www.youtube.com/watch?v=...",
  "one_line": "single-sentence core thesis (≤ 25 words)",
  "best_for": "watch this if you... (one sentence)",
  "categories": ["topical tags"],
  "cards": [
    {
      "kind": "principle | tactic | warning | framework | mental_model | phrase | quote",
      "category": "topical category",
      "title": "5–10 word title",
      "content": "1–3 clean sentences — the knowledge itself, paraphrased",
      "reasoning": "optional: why it works",
      "source_quote": "optional: ≤30-word anchor quote",
      "framework_steps": ["only if kind=framework"]
    }
  ]
}
```

The full contract — including extraction rules — is in [data/knowledge/SCHEMA.md](data/knowledge/SCHEMA.md).

---

## SQLite data model

```
┌─────────────────┐         ┌──────────────────────┐         ┌──────────────────┐
│    sources      │←────┐   │       videos         │←─────┐  │     cards        │
│─────────────────│     │   │──────────────────────│      │  │──────────────────│
│ id (PK)         │     └───┤ source_id (FK)       │      └──┤ video_id (FK)    │
│ name            │         │ id (PK)              │         │ source_id        │
│ kind            │         │ title                │         │ id (PK)          │
│ url             │         │ url                  │         │ kind             │
│ domain          │         │ one_line             │         │ category         │
│ expertise       │         │ best_for             │         │ title            │
│ language        │         └──────────────────────┘         │ content          │
│ first_indexed   │              │                           │ reasoning        │
│ license         │              │                           │ source_quote     │
│ trust_notes     │              ▼                           └────────┬─────────┘
└─────────────────┘    ┌────────────────────┐                         │
                       │ video_categories   │                         ▼
                       │────────────────────│              ┌──────────────────────┐
                       │ video_id (FK)      │              │  framework_steps     │
                       │ category           │              │──────────────────────│
                       └────────────────────┘              │ card_id (FK)         │
                                                          │ step_number          │
                                                          │ step_content         │
                                                          └──────────────────────┘

                                       ┌──────────────────────┐
                                       │   cards_fts (FTS5)   │
                                       │──────────────────────│
                                       │ card_id (UNINDEXED)  │
                                       │ title                │
                                       │ content              │
                                       │ reasoning            │
                                       │ source_quote         │
                                       └──────────────────────┘
```

Indices: `idx_cards_video`, `idx_cards_kind`, `idx_cards_cat`, `idx_cards_source`, `idx_vc_cat`.

---

## HTTP API surface

Two parallel route families read from the same SQLite store.

### `/api/*` — the human dashboard's API

| Endpoint | Purpose |
|---|---|
| `GET /api/source` | the (first/active) source's overview |
| `GET /api/kinds` | card-kind counts |
| `GET /api/categories` | topical categories with counts |
| `GET /api/cards?kind=&category=&video=&limit=` | filterable card stream |
| `GET /api/card/<id>` | one card by id |
| `GET /api/videos` | every video with card counts and one-line theses |
| `GET /api/video/<vid>` | one video's full set of cards |
| `GET /api/search?q=&limit=` | FTS5 full-text search across cards |
| `POST /api/ingest` | self-serve onboarding (kicks off the pipeline as a background job) |
| `GET /api/ingest/status/<job_id>` | live job status + streaming subprocess log |
| `GET /api/ingest/has_key` | which LLM providers have a key configured |

### `/ai/*` — the autonomous agent surface

Designed for LLMs to discover and consume. Every card returned carries the `video_url` for citation.

| Endpoint | Purpose |
|---|---|
| `GET /ai/manifest` | discovery doc: schema, capabilities, recommended usage |
| `GET /.well-known/ai-knowledge.json` | well-known alias of the manifest |
| `GET /ai/sources` | every indexed source with size of contribution |
| `GET /ai/categories` | topic taxonomy |
| `GET /ai/cards?kind=&category=&video=&limit=` | filterable card stream |
| `GET /ai/search?q=&include=insights,segments,videos` | FTS across all layers |
| `GET /ai/atlas` | the full atlas grouped by category and kind |
| `GET /ai/learn/<category>` | study packet for one topic, ordered for teaching |
| `GET /ai/teach?q=<question>` | teaching packet synthesized for a user question |

### `/ai/cross/*` — multi-source correlation

The value multiplier when 2+ sources are indexed. With one source these return flat data; with many they unlock cross-expert reasoning.

| Endpoint | Purpose |
|---|---|
| `GET /ai/cross/coverage` | matrix: which sources cover which categories |
| `GET /ai/cross/concept/<term>` | every source's take on one concept, grouped by source |
| `GET /ai/cross/compendium/<category>` | every source's cards on one topic, in teaching order |
| `GET /ai/cross/consensus` | cross-source card pairs that look like the same idea |

---

## MCP integration

The Model Context Protocol server (`mcp_server.py`) is the bridge for AI clients that can't reach localhost HTTP (Claude Desktop's sandbox, claude.ai web, etc.).

```
   ┌──────────────────────┐                 ┌────────────────────────┐
   │   Claude Desktop /   │                 │     mcp_server.py      │
   │   Claude Code /      │  ◄────stdio───► │ (subprocess of client) │
   │   Cursor / Cline /   │  JSON-RPC       │                        │
   │   any MCP client     │                 │  Reads SQLite directly │
   └──────────────────────┘                 │  (NOT via Flask HTTP)  │
                                            └───────────┬────────────┘
                                                        │
                                                        ▼
                                              ┌────────────────────┐
                                              │  data/knowledge.db │
                                              └────────────────────┘
```

### Why direct SQLite (not HTTP)?

The MCP server bypasses Flask entirely. SQLite is in-process. This means:

- the MCP server works even if the Flask dashboard isn't running,
- there's no port to bind (Claude Desktop spawns it on demand),
- and there's no risk of Flask permissions causing tool failures.

### Tools exposed

| Tool | Purpose |
|---|---|
| `list_sources` | every indexed expert with size of contribution |
| `list_categories` | all topical categories |
| `list_videos` | all videos (filterable by source) |
| `search_knowledge` | full-text search across cards |
| `teach_about` | question → teaching packet, kind-ordered |
| `learn_category` | deep study packet for one topic |
| `cross_concept` | multi-source view of one concept |
| `cross_compendium` | multi-source cards on one topic |
| `cross_coverage` | matrix: which experts cover which topics |
| `get_card` | fetch one card by id |

Every tool returns Markdown that LLM clients can render directly. Every card includes the source `video_url` for verification.

### Client config (Claude Desktop, macOS)

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "knowledge-atlas": {
      "command": "/absolute/path/to/python3",
      "args": ["/absolute/path/to/knowledge-atlas/mcp_server.py"]
    }
  }
}
```

### Client config (Claude Code)

```bash
claude mcp add knowledge-atlas --scope user -- \
  /absolute/path/to/python3 /absolute/path/to/knowledge-atlas/mcp_server.py
```

See [HOW_TO_CONNECT.md](HOW_TO_CONNECT.md) for the full integration guide.

---

## Self-serve ingestion (the web onboarding loop)

```
  Browser                  Flask /api/ingest                Background thread
  ───────                  ─────────────────                ─────────────────
     │                            │                                │
     │ POST { url, source_id,     │                                │
     │        provider, api_key,  │                                │
     │        window, workers }   │                                │
     ├───────────────────────────►│                                │
     │                            │ spawn thread, return job_id    │
     │◄───────────────────────────┤                                │
     │                            │                                │
     │ GET /api/ingest/status/    │                                │
     │     <job_id>  (poll @1Hz)  │  registers source in JSON      │
     ├───────────────────────────►│  → spawns fetch_channel.py     │
     │  { phases: {               │     (subprocess, streaming)    │
     │     list:   {done},        │     → emits @@PROGRESS@@ JSON  │
     │     fetch:  {running,      │       events on stdout         │
     │       step:22, total:64,   │  → spawns extract_knowledge.py │
     │       eta_sec:83,          │     → @@PROGRESS@@ events      │
     │       current_item:{...}}, │  → spawns build_knowledge.py   │
     │     extract: {pending},    │     → @@PROGRESS@@ events      │
     │     aggregate:{pending}}}  │                                │
     │◄───────────────────────────┤                                │
     │   render phase dashboard   │                                │
     │                            │                                │
     │  ...continues polling...   │                                │
     │                            │                                │
     │  { phases: {... all done}, │                                │
     │    status: done }          │                                │
     │◄───────────────────────────┤                                │
     │   reload atlas             │                                │
```

The web modal accepts the user's choice of provider (xAI Grok, Anthropic Claude, or OpenAI), time window (1d/1w/1m/3m/6m/1y/all/custom), and worker count (1–8). API keys are saved to a local `.env` (chmod 600) and never logged.

### Structured progress protocol

Subprocesses (`fetch_channel.py`, `extract_knowledge.py`, `build_knowledge.py`) emit single-line structured events alongside their regular log output:

```
@@PROGRESS@@ {"event":"phase_start","phase":"fetch","total":64,"workers":4}
@@PROGRESS@@ {"event":"item_done","phase":"fetch","step":22,"total":64,
              "id":"abc123","title":"...","status":"fetched","counts":{...}}
@@PROGRESS@@ {"event":"phase_done","phase":"fetch","elapsed_sec":40.9,
              "summary":{"total":64,"fetched":22,"cached":0,"no_subs":42,"failed":0}}
```

`app.py` parses these out of the stdout stream and maintains a typed state object per phase in `JOBS[job_id].phases`. Each phase records `step`, `total`, `current_item`, `started_at`, `elapsed_sec`, `eta_sec` (computed from rate over elapsed time), and a `summary` once the phase completes. This drives the live phase-by-phase dashboard in the web UI:

| Phase | Sub-pipeline | Per-item visibility |
|---|---|---|
| `list` | `yt-dlp --flat-playlist` to enumerate the channel | total video count |
| `fetch` | parallel yt-dlp transcript downloads | each video's id + title + status as it completes; cached / fetched / no_subs / failed counts; rate (videos/s); per-phase ETA |
| `extract` | LLM extraction (xAI / Anthropic / OpenAI) | each video's id + title at start; card count + duration at completion; per-phase ETA |
| `aggregate` | `build_knowledge.py` rebuilds SQLite + JSON exports + FTS5 | total cards / videos / categories on completion |

Raw subprocess stdout is still kept verbatim in a foldable "Streaming log" panel for debugging — the progress events are an additive structured channel, not a replacement.

---

## LLM provider abstraction

`extract_knowledge.py` is provider-agnostic by design.

```python
def make_client(provider, api_key):
    if provider == "xai":
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    if provider == "anthropic":
        from anthropic import Anthropic
        return Anthropic(api_key=api_key)
    raise ValueError(f"unknown provider: {provider}")
```

Auto-detection priority:

1. `--provider` CLI flag
2. `XAI_API_KEY` env var → use xAI
3. `ANTHROPIC_API_KEY` env var → use Anthropic
4. Fail

### Adding a new provider

1. Add the API SDK to `requirements.txt`.
2. Add a `call_<provider>()` function that takes `(client, model, schema, video, transcript)` and returns the model's text response.
3. Register the provider in `DEFAULTS` and `make_client()`.
4. Add the env var to `PROVIDER_ENV` in `app.py`.

The schema stays the same. The aggregator and serving layer don't change.

---

## Extensibility

| Want to… | How |
|---|---|
| Index a new YouTube channel | `python3 add_source.py --id ... --url ...` |
| Index a non-YouTube source (podcast RSS, Substack, etc.) | Write a custom fetcher that produces `sources/<id>/transcripts/<vid>.txt` files. Everything downstream is source-agnostic. |
| Use a different LLM | Add a `call_<provider>()` function — see above. |
| Add a new card kind | Extend `VALID_KINDS` in `build_knowledge.py`, add the kind to `SCHEMA.md`, add color tokens in `templates/index.html`. |
| Add a new endpoint | Drop a Flask route in `app.py`; for AI consumption, also add an MCP tool in `mcp_server.py`. |
| Expose to a non-localhost AI | Tunnel `127.0.0.1:5179` via `cloudflared` or `ngrok`. **Add auth before doing this** — the server has no built-in auth (localhost trust model). |

---

## Security & privacy model

| Concern | Mitigation |
|---|---|
| API keys | Stored only in local `.env` (chmod 600). Never logged. Never sent to the browser. The web modal accepts them in a `type="password"` field and writes them server-side. |
| Network exposure | Flask binds to `127.0.0.1` only by default. Nothing listens on a public interface. |
| Indexed content | All transcripts and derived knowledge cards stay on disk in directories that are gitignored. Nothing leaves the machine unless the user explicitly redistributes. |
| Auth | None — localhost trust model. If you tunnel out, add auth (basic-auth header, bearer token middleware) before exposing. |
| Subprocess hygiene | `subprocess.Popen` calls never use `shell=True`. Arguments are passed as lists. User-supplied URL goes only to yt-dlp via `--url`. |
| MCP exposure | The MCP server reads SQLite read-only. It cannot mutate data. It cannot reach the network. |

### What `.env` looks like

```
XAI_API_KEY=...
ANTHROPIC_API_KEY=...
```

This file is in `.gitignore`. The Flask server reads `~/.env` first, then `./.env`. The web modal can save keys here if the user opts in.

---

## File layout

```
knowledge-atlas/
├── README.md
├── INSTALL.md
├── ARCHITECTURE.md          (this file)
├── PIPELINE.md
├── HOW_TO_CONNECT.md
├── LICENSE
├── .gitignore
├── requirements.txt
├── sources.json.example     (template; real sources.json is gitignored)
├── fetch_channel.py         (yt-dlp wrapper)
├── extract_knowledge.py     (LLM extractor — provider-agnostic)
├── build_knowledge.py       (aggregator → SQLite + JSON)
├── add_source.py            (one-shot register + fetch CLI)
├── app.py                   (Flask server)
├── mcp_server.py            (MCP server)
├── ai_client_demo.py        (worked example AI client)
├── yt_download_transcript.py (legacy single-video helper)
├── templates/
│   └── index.html           (the dashboard SPA)
├── data/
│   └── knowledge/
│       └── SCHEMA.md        (the extraction contract)
└── docs/                    (GitHub Pages content)
    ├── index.html
    ├── install.html
    └── architecture.html
```

Runtime (gitignored) directories created as you index sources:

```
sources/<source_id>/transcripts/        per-source plain-text transcripts
sources/<source_id>/raw_srt/            per-source raw subtitle files
sources/<source_id>/channel_metadata.json
data/knowledge/<video_id>.json          one card file per video (LLM output)
data/knowledge.db                       unified atlas (rebuilt by build_knowledge.py)
data/export/                            portable JSON snapshots
.env                                    local API keys
```

---

## The repeatability claim

The pipeline is genuinely repeatable because:

1. **Schema is stable.** Every card looks the same regardless of source.
2. **Source is a first-class dimension.** Every card carries `source_id`. Every endpoint filters/groups/correlates by it.
3. **Idempotent rebuild.** `build_knowledge.py` is safe to re-run anytime.
4. **One contract for AI extractors.** As long as the LLM produces JSON matching `SCHEMA.md`, the rest doesn't care which model or vendor produced it.

Index one expert: one expert's distilled corpus. Index ten in a domain: cross-source endpoints surface what every expert agrees on (consensus = high confidence) and where they conflict (the edge of knowledge). Index a hundred across many domains: an AI agent acting for the human can stitch grounded, attributed answers from the right combination of experts on demand.
