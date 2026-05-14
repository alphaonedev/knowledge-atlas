# How to point AI at the Knowledge Atlas

You have **three layers** of access. Use whichever one matches the AI client
you're sitting in front of.

```
                    ┌──────────────────────────────┐
   Claude Desktop ──┤  MCP server (mcp_server.py)  ├── SQLite knowledge.db
   Claude Code     ─┤  10 tools, stdio JSON-RPC    │
   Other MCP AIs  ──┘                              │
                                                   │
   Claude Code, curl, your scripts ──── HTTP /ai/* │── same SQLite
                                                   │
   Claude.ai (web/chat) ─── cannot reach localhost │
   Workarounds: tunnel + connector, or copy/paste  │
                    └──────────────────────────────┘
```

---

## 1. Claude Desktop (recommended for chat-style use)

This is the cleanest fit. Claude Desktop launches MCP servers as local
subprocesses on your machine — exactly what `mcp_server.py` is built for.

**Step 1.** Edit Claude Desktop's config file:

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

Add (or merge) the `knowledge-atlas` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "knowledge-atlas": {
      "command": "python3",
      "args": ["/absolute/path/to/knowledge-atlas/mcp_server.py"]
    }
  }
}
```

**Step 2.** Quit and relaunch Claude Desktop.

**Step 3.** In a chat, you'll see a 🔌 indicator showing `knowledge-atlas`
is connected with 10 tools. Now you can ask things like:

  - "List the sources in my knowledge atlas."
  - "Teach me about cross-examination in custody court."
  - "What does my atlas say about negotiating with a narcissist?"
  - "Use cross_compendium on the topic 'mindset' and summarize."
  - "Search my knowledge atlas for 'deposition' and give me the top tactics."

Claude will pick the right tool, call it, and present the cards inline
with proper citations back to the YouTube videos.

---

## 2. Claude Code (terminal — what you're using right now)

Two ways:

### a) MCP, one-time setup

```
claude mcp add knowledge-atlas python3 /absolute/path/to/knowledge-atlas/mcp_server.py
```

Verify:

```
claude mcp list
```

After this, in any Claude Code session, Claude can call the atlas tools
directly. Persists across sessions.

### b) Just ask — no setup

Claude Code already has Bash + WebFetch. With Flask running (`python3 app.py`),
you can say in chat:

> "Curl `http://127.0.0.1:5179/ai/teach?q=how%20to%20cross%20examine%20a%20narcissist`
> and present the cards to me."

That's it. No MCP config needed for the terminal use case.

---

## 3. Claude.ai (the web chat at claude.ai) — the honest answer

**You cannot directly point claude.ai at `http://127.0.0.1:5179/`.** That URL
only exists inside your laptop. Claude.ai is hosted on Anthropic's servers
and runs in a browser sandbox; it has no way to reach a localhost address
on your machine.

There are three workarounds, in order of practicality:

### Workaround A · Use Claude Desktop instead (best)

Same Claude model, same chat UI, but it runs on your machine and can spawn
the MCP server. This is the path 99% of people should take. See section 1.

### Workaround B · Tunnel localhost to a public URL, then a connector

Expose your Flask server via `cloudflared` or `ngrok`:

```
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:5179
# prints a public https URL
```

You now have something like `https://random-words.trycloudflare.com/ai/teach`.
You could then use claude.ai's Connectors / custom tools feature to point
at that URL — but: this exposes the data to the public internet for the
lifetime of the tunnel, and the connector setup is fiddly. **Only do this
if you understand the exposure risk.**

If you choose to tunnel, also add basic-auth or a token check in `app.py` so
the public URL isn't open to anyone who guesses it.

### Workaround C · Copy-paste

Run the API call yourself and paste the result:

```
curl -s 'http://127.0.0.1:5179/ai/teach?q=your+question' | jq
```

Paste into claude.ai. Crude but always works.

---

## 4. Any other MCP-aware AI

Anyone speaking MCP (Cline, Cursor, Continue.dev, your own scripts using
the `mcp` Python SDK) can hook in the same way as Claude Desktop. Same
config snippet, same 10 tools.

---

## What tools are exposed (the menu)

| Tool | Purpose |
|---|---|
| `list_sources` | Every indexed expert and the size of their contribution |
| `list_categories` | All topical categories in the atlas |
| `list_videos` | All videos (optionally filtered by source) |
| `search_knowledge` | Full-text search across cards (kind filter optional) |
| `teach_about` | Question → teaching packet ordered for explanation |
| `learn_category` | Deep study packet for one topic |
| `cross_concept` | Multi-source view of one concept |
| `cross_compendium` | Multi-source cards on one topic |
| `cross_coverage` | Matrix: which experts cover which topics |
| `get_card` | Fetch one card by id |

Every tool returns markdown that's directly presentable to a human. Every
card includes its source video URL for verification.

---

## Sanity check

```
python3 -c "
import asyncio
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

async def main():
    p = StdioServerParameters(command='python3',
                              args=['/absolute/path/to/knowledge-atlas/mcp_server.py'])
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print(f'OK · {len(tools.tools)} tools exposed')

asyncio.run(main())
"
```

Expected output: `OK · 10 tools exposed`.

If that works, Claude Desktop will work too after you add the config.
