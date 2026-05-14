#!/usr/bin/env python3
"""
extract_knowledge.py — extract knowledge cards from un-processed transcripts.

Supports two LLM providers (auto-detected from env, overridable):
  - xai        → uses XAI_API_KEY; default model: grok-4.20-0309-reasoning
                 (OpenAI-compatible API at https://api.x.ai/v1)
  - anthropic  → uses ANTHROPIC_API_KEY; default model: claude-sonnet-4-5

Reads:
  data/knowledge/SCHEMA.md            (extraction contract)
  transcripts/<vid>.txt               (legacy single-source layout)
  sources/<source_id>/transcripts/<vid>.txt
  channel_metadata.json / sources/<sid>/channel_metadata.json

Writes:
  data/knowledge/<vid>.json           (one per video, idempotent)

API-key resolution (in order):
  1. --api-key flag
  2. provider-specific env var (XAI_API_KEY / ANTHROPIC_API_KEY)
  3. ~/.env file        (your global key store)
  4. ./.env file        (project-local key store)
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA = ROOT / "data" / "knowledge" / "SCHEMA.md"
OUT_DIR = ROOT / "data" / "knowledge"
SOURCES_JSON = ROOT / "sources.json"

DEFAULTS = {
    "xai":       {"model": "grok-4.20-0309-reasoning", "base_url": "https://api.x.ai/v1",
                  "env": "XAI_API_KEY"},
    "anthropic": {"model": "claude-sonnet-4-5",        "base_url": None,
                  "env": "ANTHROPIC_API_KEY"},
}
MAX_TOKENS = 4096


def load_env():
    """Read ~/.env then ./.env into os.environ (existing vars win)."""
    for p in (Path.home() / ".env", ROOT / ".env"):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def resolve(provider, cli_key):
    load_env()
    env_var = DEFAULTS[provider]["env"]
    return cli_key or os.environ.get(env_var)


def auto_provider():
    load_env()
    if os.environ.get("XAI_API_KEY"):
        return "xai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def paths_for(source_id):
    """Per-source storage layout. All sources live under sources/<source_id>/."""
    base = ROOT / "sources" / source_id
    return {
        "txt":  base / "transcripts",
        "meta": base / "channel_metadata.json",
    }


def _is_valid_card_file(path):
    """A previously written card file is valid only if it parses as JSON
    and has the required shape. Killed-mid-write or otherwise corrupt files
    are deleted so they get re-extracted on resume."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(doc, dict) and "video_id" in doc and "cards" in doc
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False


def _atomic_write_text(path, content):
    """Atomic file write: write to a sibling tempfile, then rename.
    Prevents kill-mid-write from leaving a half-written .json on disk."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def list_pending(source_id):
    """Return videos that still need extraction. Repairs the on-disk state
    on the way: existing .json files that don't parse as valid card docs
    are deleted so they'll be re-extracted."""
    paths = paths_for(source_id)
    meta_path = paths["meta"]
    if not meta_path.exists():
        return []
    meta = json.loads(meta_path.read_text())
    txt_dir = paths["txt"]
    pending = []
    valid_count = 0
    repaired = []
    for m in meta:
        vid = m.get("id")
        if not vid:
            continue
        tp = txt_dir / f"{vid}.txt"
        if not tp.exists():
            continue
        out_path = OUT_DIR / f"{vid}.json"
        if out_path.exists():
            if _is_valid_card_file(out_path):
                valid_count += 1
                continue
            else:
                # Corrupt / half-written file — delete and re-extract
                out_path.unlink(missing_ok=True)
                repaired.append(vid)
        pending.append({
            "id": vid,
            "title": m.get("title", ""),
            "url": m.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "transcript_path": tp,
        })
    if valid_count or repaired:
        print(f"      Resume scan: {valid_count} already extracted and valid"
              + (f", {len(repaired)} corrupt (will re-extract: {', '.join(repaired)})" if repaired else ""))
    return pending


SYSTEM_TEMPLATE = """You extract structured knowledge from YouTube transcripts of subject-matter experts and produce STRICT JSON conforming to the schema below. Output the JSON object only — no prose, no markdown fences, no commentary.

The transcripts are YouTube auto-captions: lots of disfluencies ("you know", "uh", repetitions, fragmented phrasing). Paraphrase aggressively into clean modern English. Each card must be a STANDALONE piece of knowledge usable without watching the video.

Skip: intros, sign-offs, calls to subscribe, rhetorical questions, anecdotes without lessons. Keep only nuggets a person can USE.

SCHEMA:

{schema}
"""

USER_TEMPLATE = """Video metadata:
  video_id: {vid}
  video_title: {title}
  video_url: {url}

Extract 4–12 knowledge cards from the transcript below (fewer if the video is conversational or sparse). Output ONLY the JSON object.

TRANSCRIPT:
---
{transcript}
---"""


def _strip_fences(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text


def _extract_json(text):
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def call_xai(client, model, schema, video, transcript):
    sys_prompt = SYSTEM_TEMPLATE.format(schema=schema)
    usr = USER_TEMPLATE.format(
        vid=video["id"], title=video["title"], url=video["url"],
        transcript=transcript,
    )
    # xAI is OpenAI-compatible; ask for json_object response format when supported
    kwargs = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        temperature=0.2,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user",   "content": usr},
        ],
    )
    try:
        resp = client.chat.completions.create(
            response_format={"type": "json_object"}, **kwargs)
    except Exception:
        # response_format isn't supported on every Grok model; fall back without it
        resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content


def call_anthropic(client, model, schema, video, transcript):
    sys_prompt = SYSTEM_TEMPLATE.format(schema=schema)
    usr = USER_TEMPLATE.format(
        vid=video["id"], title=video["title"], url=video["url"],
        transcript=transcript,
    )
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[
            {"type": "text", "text": sys_prompt,
             "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": usr}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def make_client(provider, api_key):
    if provider == "xai":
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=DEFAULTS["xai"]["base_url"])
    if provider == "anthropic":
        from anthropic import Anthropic
        return Anthropic(api_key=api_key)
    raise ValueError(f"unknown provider: {provider}")


def extract_one(client, provider, model, schema, video):
    transcript = video["transcript_path"].read_text(encoding="utf-8")
    if provider == "xai":
        raw = call_xai(client, model, schema, video, transcript)
    else:
        raw = call_anthropic(client, model, schema, video, transcript)
    doc = _extract_json(raw)
    doc.setdefault("video_id", video["id"])
    doc.setdefault("video_title", video["title"])
    doc.setdefault("video_url", video["url"])
    doc.setdefault("cards", [])
    out_path = OUT_DIR / f"{video['id']}.json"
    # Atomic write — if the process is killed mid-write, no half-baked JSON
    # is left on disk that would confuse the aggregator or fail validation.
    _atomic_write_text(out_path, json.dumps(doc, indent=2))
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True,
                    help="source_id (registered in sources.json)")
    ap.add_argument("--provider", choices=["xai", "anthropic"], default=None,
                    help="LLM provider (default: auto — xai if XAI_API_KEY else anthropic)")
    ap.add_argument("--model", default=None,
                    help=f"model id (default per provider: {DEFAULTS['xai']['model']} / {DEFAULTS['anthropic']['model']})")
    ap.add_argument("--api-key", default=None,
                    help="API key for the chosen provider; falls back to env / .env")
    ap.add_argument("--limit", type=int, default=0,
                    help="process only the first N pending videos (0 = all)")
    args = ap.parse_args()

    provider = args.provider or auto_provider()
    if not provider:
        print("ERROR: no API key found. Set XAI_API_KEY or ANTHROPIC_API_KEY "
              "(env var or ~/.env), or pass --provider + --api-key.", file=sys.stderr)
        sys.exit(2)

    api_key = resolve(provider, args.api_key)
    if not api_key:
        print(f"ERROR: no key for provider '{provider}' "
              f"(looked for {DEFAULTS[provider]['env']}).", file=sys.stderr)
        sys.exit(2)

    model = args.model or DEFAULTS[provider]["model"]
    client = make_client(provider, api_key)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA.read_text()

    pending = list_pending(args.source)
    if args.limit:
        pending = pending[: args.limit]

    print(f"Source: {args.source}")
    print(f"Provider: {provider}  ·  Model: {model}")
    # Count of videos with transcripts on disk (the universe extraction could touch).
    paths = paths_for(args.source)
    total_with_transcripts = sum(1 for p in paths["txt"].glob("*.txt")
                                 if not p.name.endswith(".timed.txt"))
    print(f"Transcripts on disk: {total_with_transcripts}  ·  Pending extraction: {len(pending)}")
    if total_with_transcripts and not pending:
        print("Nothing to extract — every transcript already has a valid card file.")
        return
    if not pending:
        print("Nothing to extract.")
        return

    total_cards = 0
    started = time.time()
    for i, v in enumerate(pending, 1):
        t0 = time.time()
        try:
            doc = extract_one(client, provider, model, schema, v)
            n = len(doc.get("cards", []))
            total_cards += n
            print(f"  [{i}/{len(pending)}] {v['id']}  {n} cards  ({time.time()-t0:.1f}s)  {v['title'][:60]}")
        except Exception as e:
            print(f"  [{i}/{len(pending)}] {v['id']}  FAILED: {e}", file=sys.stderr)

    print(f"\nDone. Extracted {total_cards} cards across {len(pending)} videos "
          f"in {time.time()-started:.1f}s.")


if __name__ == "__main__":
    main()
