# Installing Knowledge Atlas

End-to-end install for macOS, Linux, and Windows (WSL). Covers prerequisites, environment, your first indexed source, the localhost dashboard, and wiring Claude Desktop / Claude Code in through MCP.

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| **Python 3.10+** | runtime | `brew install python` / system package manager |
| **yt-dlp** | transcript download | `pip install yt-dlp` or `brew install yt-dlp` |
| **An LLM API key** | knowledge extraction | xAI ([console.x.ai](https://console.x.ai)) **or** Anthropic ([console.anthropic.com](https://console.anthropic.com)) |
| **(optional) `gh` CLI** | for cloning | `brew install gh` |
| **(optional) Claude Desktop or Claude Code** | for MCP integration | [claude.ai/download](https://claude.ai/download) |

## 1. Clone & install Python dependencies

```bash
git clone https://github.com/<your-account>/knowledge-atlas.git
cd knowledge-atlas
python3 -m venv .venv          # optional but recommended
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` pulls in Flask, yt-dlp, the `openai` SDK (used as xAI's OpenAI-compatible client), the `anthropic` SDK, the `mcp` SDK, and a few analytics helpers.

## 2. Configure your LLM provider

Knowledge Atlas can use either xAI Grok or Anthropic Claude for extraction. Pick one (or both — the system auto-detects whichever key is present).

Create a `.env` file in the project root:

```bash
# xAI (default; OpenAI-compatible endpoint at https://api.x.ai/v1)
XAI_API_KEY=sk-...

# or Anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

The `.env` file is gitignored. The Flask server also reads `~/.env` if you keep keys globally.

> **Cost note.** Extraction runs ~3K input / ~1K output tokens per video. At xAI Grok 4 pricing this is ~$0.02–$0.04/video; a 60-video channel runs ~$2. Use a faster non-reasoning model variant if you want speed over depth.

## 3. Initialize the sources registry

```bash
cp sources.json.example sources.json
```

`sources.json` is gitignored — what you index stays private.

## 4. Index your first expert

The one-shot CLI:

```bash
python3 add_source.py \
  --id my-expert \
  --name "Display Name" \
  --url "https://www.youtube.com/@channel" \
  --domain "one-line description of what they teach" \
  --expertise "what they're known for"
```

This:

1. Adds the source to `sources.json`.
2. Runs `fetch_channel.py` against the channel — downloads every video's transcript via yt-dlp into `sources/<id>/transcripts/`.

Now run the LLM extraction:

```bash
python3 extract_knowledge.py --source my-expert
```

This produces `data/knowledge/<video_id>.json` for every video — one file per video, each containing 4–12 paraphrased knowledge cards.

Build the unified atlas:

```bash
python3 build_knowledge.py
```

This aggregates every JSON into a single SQLite store at `data/knowledge.db` plus portable JSON snapshots in `data/export/`.

## 5. Launch the dashboard

Knowledge Atlas ships with a lifecycle CLI (`atlas.py`) that manages the Flask service as a background daemon. Use it instead of running `app.py` directly:

```bash
python3 atlas.py start       # launch in background, writes PID + log
python3 atlas.py status      # PID, uptime, port, atlas content stats
python3 atlas.py logs        # tail data/atlas.log (-f to follow)
python3 atlas.py restart     # stop then start (port settles between)
python3 atlas.py stop        # SIGTERM → grace period → SIGKILL fallback
```

After `start`, open **http://127.0.0.1:5179/**. You'll see the indexed expert, the full card library, browse-by-kind / browse-by-topic navigation, and ⌘K full-text search.

You can index more experts directly from the web UI — click **Index expert** in the header, paste a channel URL, watch the pipeline run live.

> **Manual mode (for development).** You can still run `python3 app.py` directly in the foreground if you prefer to see Flask's output inline — useful when iterating on the code. The CLI just wraps that with PID management + log capture + clean shutdown.

## 6. Wire in Claude Desktop (recommended for chat-style use)

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

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

Use the absolute path to `python3` — Claude Desktop launches subprocesses with a stripped PATH. Find it with `which python3` (or use your `.venv` interpreter).

Fully quit Claude Desktop (⌘Q on the menu bar) and relaunch. You'll see `knowledge-atlas` listed in **Local MCP servers** with 10 tools.

Try these prompts:

- *"List the sources in my knowledge atlas."*
- *"Teach me about [topic] using the knowledge-atlas tools."*
- *"What does my atlas say about [concept]? Cite the source videos."*
- *"Show me cross-source coverage and tell me which topics are deepest."*

## 7. Wire in Claude Code (terminal)

One command:

```bash
claude mcp add knowledge-atlas --scope user -- \
  /absolute/path/to/python3 /absolute/path/to/knowledge-atlas/mcp_server.py
```

Verify:

```bash
claude mcp list
# expect: knowledge-atlas: ... - ✓ Connected
```

See [HOW_TO_CONNECT.md](HOW_TO_CONNECT.md) for the full integration guide, including the honest answer about claude.ai (the web app) — which can't reach localhost directly.

## 8. Smoke test the install

```bash
# Server alive?
curl -s http://127.0.0.1:5179/api/source | python3 -m json.tool

# MCP server starts cleanly under a stripped env (mimics Claude Desktop)?
env -i HOME="$HOME" PATH="/usr/bin:/bin" python3 -c "
import asyncio
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

async def main():
    p = StdioServerParameters(command='python3', args=['mcp_server.py'])
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            t = await s.list_tools()
            print(f'OK · {len(t.tools)} tools')

asyncio.run(main())
"
# expect: OK · 10 tools
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Modal hangs at "Submitting…" | Flask isn't running | `python3 app.py` |
| `mcp_server.py` not visible in Claude Desktop | Path issue, or GUI re-serialized the config | Edit JSON file directly, use absolute paths, fully quit (⌘Q) and relaunch |
| `extract_knowledge.py` errors with "no API key found" | `.env` missing or wrong var name | Set `XAI_API_KEY` or `ANTHROPIC_API_KEY` |
| yt-dlp fails on a private/age-gated video | YouTube auth requirement | These will be skipped; everything else still indexes |
| Some videos have no transcript | Channel disabled captions | Skipped automatically; check terminal output |
| FTS search returns nothing | Build wasn't run after extraction | Re-run `python3 build_knowledge.py` |

## Updating

```bash
git pull
pip install -r requirements.txt --upgrade
python3 build_knowledge.py   # rebuild atlas with the new schema if changed
```

The atlas database is rebuilt holistically from `data/knowledge/*.json` each run, so schema changes propagate by re-running build, not by destructive migrations.

## Uninstall

```bash
deactivate                                  # if you used a venv
rm -rf data/ sources/ transcripts/ raw_srt/ # remove indexed content
rm sources.json .env                        # remove config
```

Then delete the project directory. To remove the MCP integration: edit `claude_desktop_config.json` to drop the `knowledge-atlas` entry, or `claude mcp remove knowledge-atlas` for Claude Code.
