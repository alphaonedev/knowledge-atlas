# Knowledge Atlas

**Turn any domain expert's video corpus into a queryable, AI-consumable knowledge base.**

Knowledge Atlas is a self-hosted pipeline that downloads transcripts from a YouTube channel (or any source you wire in), uses an LLM to distill them into clean, paraphrased *knowledge cards* with full provenance, indexes everything into SQLite, and exposes the result through three surfaces:

- a **human dashboard** (localhost web UI)
- a **REST/JSON API** for autonomous agents
- a **Model Context Protocol (MCP) server** that plugs directly into Claude Desktop, Claude Code, and any MCP-aware client

Index 1 expert and you have one expert's distilled corpus. Index 10 in the same domain and the cross-source endpoints surface what every expert agrees on and where they conflict — orders-of-magnitude faster learning than watching the videos.

> **Quick links:** [INSTALL](INSTALL.md) · [ARCHITECTURE](ARCHITECTURE.md) · [PIPELINE](PIPELINE.md) · [HOW_TO_CONNECT](HOW_TO_CONNECT.md) · [SCHEMA](data/knowledge/SCHEMA.md)

---

## Why this exists

Subject-matter experts give away decades of hard-won knowledge across YouTube and podcasts. The content is locked inside hours of speech a single human cannot reasonably consume. This pipeline distills that knowledge into a form humans and AI agents can actually use — paraphrased, categorized, attributable, searchable, cross-correlatable.

## What it produces

For every video in an indexed source, an LLM produces 4–12 **knowledge cards**, each classified as one of seven kinds:

| Kind | Meaning |
|---|---|
| `principle` | a general rule or law the expert teaches |
| `tactic` | a specific action to take in a specific situation |
| `warning` | something to avoid + why |
| `framework` | a named multi-step approach (carries ordered steps) |
| `mental_model` | a reframe / way of thinking |
| `phrase` | exact language to use |
| `quote` | a memorable line worth preserving |

Each card carries clean paraphrased content, optional reasoning, optional anchoring quote, and provenance back to the source video URL. See [SCHEMA.md](data/knowledge/SCHEMA.md) for the contract.

## Three ways to consume

### 1. Web dashboard
A polished single-page interface at `http://127.0.0.1:5179/` for browsing by topic, kind, or source. Cmd+K to search.

### 2. HTTP/JSON API
Stable endpoints for autonomous agents (`/ai/*`) and the cross-source correlation surface (`/ai/cross/*`). Discovery via `/ai/manifest`.

### 3. MCP server
Drop-in tool surface for Claude Desktop, Claude Code, and any MCP client. 10 tools: `list_sources`, `search_knowledge`, `teach_about`, `learn_category`, `cross_concept`, `cross_compendium`, `cross_coverage`, `get_card`, `list_videos`, `list_categories`.

## Quickstart

```bash
git clone https://github.com/<your-account>/knowledge-atlas.git
cd knowledge-atlas
pip install -r requirements.txt

# Configure at least one LLM provider in a local .env file
echo 'XAI_API_KEY=...' >> .env       # xAI Grok (OpenAI-compatible)
# — or —
echo 'ANTHROPIC_API_KEY=...' >> .env # Claude

# Initialize sources registry
cp sources.json.example sources.json

# Index your first expert (one-shot CLI)
python3 add_source.py \
  --id my-expert \
  --name "My Expert" \
  --url "https://www.youtube.com/@channel" \
  --domain "what they teach" \
  --expertise "their unique angle"

# Run the LLM extraction (uses XAI_API_KEY if set, else ANTHROPIC_API_KEY)
python3 extract_knowledge.py --source my-expert

# Build the unified atlas
python3 build_knowledge.py

# Launch the dashboard
python3 app.py
# open http://127.0.0.1:5179/
```

See [INSTALL.md](INSTALL.md) for full setup, [ARCHITECTURE.md](ARCHITECTURE.md) for the system design, and [HOW_TO_CONNECT.md](HOW_TO_CONNECT.md) to wire Claude Desktop / Claude Code into the atlas via MCP.

## Architecture (one paragraph)

SQLite is the single source of truth. Flask exposes parallel surfaces — `/api/*` for the human dashboard, `/ai/*` for autonomous agents, `/ai/cross/*` for multi-source correlation, plus `/api/ingest` for self-serve onboarding. An MCP server provides the same data over stdio JSON-RPC to local AI clients without going through HTTP. Every card carries provenance back to a specific YouTube video so consumers can always cite the source. The extraction step is provider-agnostic (xAI Grok or Anthropic Claude); swap in another provider by adding one function. Read [ARCHITECTURE.md](ARCHITECTURE.md) for the full diagram and component breakdown.

## Self-serve ingestion via the web UI

A built-in **Index expert** modal lets a non-technical user drop a YouTube channel URL, paste an API key (saved locally to `.env`), and watch the pipeline run end-to-end with a live streaming subprocess log. No CLI required after the initial install.

## Ethics & copyright

This software is a tool. It ships no third-party content. When you run it against a YouTube channel:

- **Personal/research use on localhost:** strong fair-use posture. The output is transformative (paraphrased into clean prose), non-substitutive (always links back to the source video), and never shipped publicly by default.
- **Do not redistribute raw transcripts.** They're gitignored for a reason. Keep them local.
- **Source quotes are short by contract** (≤ 30 words). They function as anchors, not substitutes for the original.
- **For commercial / public deployment:** seek written permission from each indexed expert. Many creators welcome attributed derivative use; most do not welcome silent commercialization.

See the `license` and `trust_notes` fields in `sources.json` — record what you know about each expert's stance on derivative use.

## License

**Apache License 2.0.** Copyright © 2026 **AlphaOne LLC**. All rights reserved.

See [LICENSE](LICENSE) for the full license text and [NOTICE](NOTICE) for attribution and third-party trademark notices.

### Disclaimers

- This software is provided **AS IS** without warranty of any kind. AlphaOne LLC and contributors disclaim all liability for damages arising from its use.
- The knowledge cards produced by this software are AI-generated paraphrases of third-party content. They reflect the original speakers' opinions, not those of AlphaOne LLC.
- Cards are **not professional advice** (legal, financial, medical, or otherwise). Verify against the cited source and consult qualified professionals.
- Users are solely responsible for compliance with the terms of service of any third-party source they index.

YouTube® is a trademark of Google LLC; Claude® is a trademark of Anthropic PBC; Grok and xAI are trademarks of X.AI Corp. Names are used here for nominative reference only.
