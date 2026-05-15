#!/usr/bin/env python3
"""
Pulls every video's metadata + transcript for a YouTube channel.

Outputs:
  raw_srt/<id>.<lang>.srt         (original subtitle files)
  transcripts/<id>.txt            (cleaned plain text)
  transcripts/<id>.timed.txt      ([mm:ss] text per cue)
  channel_metadata.json           (one record per video)
"""

import os
import re
import sys
import json
import glob
import time
import shutil
import subprocess
from pathlib import Path

import argparse
import datetime
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent
PREF_LANGS = ["en", "en-US", "en-GB"]

WINDOW_PRESETS = {
    "1d":  datetime.timedelta(days=1),
    "1w":  datetime.timedelta(weeks=1),
    "1m":  datetime.timedelta(days=30),
    "3m":  datetime.timedelta(days=90),
    "6m":  datetime.timedelta(days=180),
    "1y":  datetime.timedelta(days=365),
    "all": None,
}


def resolve_window(window=None, since=None, until=None):
    """Returns (since_yyyymmdd, until_yyyymmdd). Either may be None.

    Priority: explicit --since/--until override --window. 'all' means no filter.
    """
    if since or until:
        return since, until
    if window and window in WINDOW_PRESETS:
        delta = WINDOW_PRESETS[window]
        if delta is None:
            return None, None
        return (datetime.date.today() - delta).strftime("%Y%m%d"), None
    return None, None

# A transcript with fewer than this many characters is considered partial/corrupt
# (a real transcript is thousands of chars). On resume, such files are re-fetched.
MIN_TRANSCRIPT_BYTES = 200


def _atomic_write_text(path, content):
    """Write text to `path` atomically: write to a sibling tempfile, then rename.
    Prevents a kill mid-write from leaving a truncated file that passes the
    existence check on resume."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _is_valid_transcript(path):
    """A previously downloaded transcript is 'valid' if it exists and is
    larger than MIN_TRANSCRIPT_BYTES. Empty / tiny files are treated as
    cancelled-mid-write and will be re-fetched on the next run."""
    try:
        return path.exists() and path.stat().st_size >= MIN_TRANSCRIPT_BYTES
    except OSError:
        return False


def paths_for(source_id):
    """Per-source storage layout. All sources live under sources/<source_id>/."""
    base = ROOT / "sources" / source_id
    return {
        "raw":  base / "raw_srt",
        "txt":  base / "transcripts",
        "meta": base / "channel_metadata.json",
    }

_time_re = re.compile(r"(\d+):(\d+):(\d+)[,\.](\d+)")
_tag_re = re.compile(r"<[^>]+>")
_dup_re = re.compile(r"\s+")


def list_videos(channel_url):
    """Return list of dicts with id, title, upload_date, duration, view_count, etc."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--ignore-errors",
        channel_url,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    videos = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue
        videos.append({
            "id": v.get("id"),
            "title": v.get("title"),
            "url": v.get("url") or f"https://www.youtube.com/watch?v={v.get('id')}",
            "duration": v.get("duration"),
            "view_count": v.get("view_count"),
            "upload_date": v.get("upload_date"),
            "channel": v.get("channel") or v.get("uploader"),
        })
    return videos


def enrich_metadata(video_id):
    """Get richer metadata for a single video (upload_date, view_count, etc.)."""
    cmd = ["yt-dlp", "--skip-download", "--dump-json", f"https://www.youtube.com/watch?v={video_id}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        return json.loads(out.stdout)
    except Exception:
        return {}


def download_subs(video_id, raw_dir, since=None, until=None):
    """Download subtitles into raw_dir/<id>.<lang>.srt. Returns chosen srt path or None.
    `since` / `until` are YYYYMMDD strings; if set, yt-dlp will skip out-of-window videos."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    outtmpl = str(raw_dir / f"{video_id}.%(ext)s")

    base = [
        "yt-dlp", url,
        "--skip-download",
        "--sub-format", "srt",
        "--sub-lang", ",".join(PREF_LANGS),
        "--convert-subs", "srt",
        "-o", outtmpl,
    ]
    if since:
        base += ["--dateafter", since]
    if until:
        base += ["--datebefore", until]

    # Try manual subs first, then auto
    for flag in ("--write-subs", "--write-auto-subs"):
        subprocess.run(base + [flag], capture_output=True, text=True, check=False)
        cands = glob.glob(str(raw_dir / f"{video_id}*.srt"))
        if cands:
            break

    cands = glob.glob(str(raw_dir / f"{video_id}*.srt"))
    if not cands:
        return None

    def score(p):
        for i, lang in enumerate(PREF_LANGS):
            if p.endswith(f".{lang}.srt"):
                return i
        return len(PREF_LANGS) + 1

    cands.sort(key=score)
    return cands[0]


def srt_to_text(srt_path):
    """Convert SRT to (plain_text, timed_text). De-duplicates rolling auto-caption lines."""
    with open(srt_path, "r", encoding="utf-8", errors="replace") as f:
        lines = [ln.rstrip("\n") for ln in f]

    timed_blocks = []
    i = 0
    while i < len(lines):
        if lines[i].strip().isdigit() and i + 1 < len(lines) and "-->" in lines[i + 1]:
            i += 1
        if i < len(lines) and "-->" in lines[i]:
            m = _time_re.search(lines[i])
            start = 0.0
            if m:
                h, mm, s, ms = map(int, m.groups())
                start = h * 3600 + mm * 60 + s + ms / 1000.0
            i += 1
            block = []
            while i < len(lines) and lines[i].strip() != "":
                block.append(lines[i])
                i += 1
            i += 1
            text = " ".join(block)
            text = _tag_re.sub("", text)
            text = _dup_re.sub(" ", text).strip()
            if text:
                timed_blocks.append((start, text))
        else:
            i += 1

    # De-duplicate rolling auto-caption blocks (each block often repeats previous tail)
    deduped = []
    last_words = []
    for start, text in timed_blocks:
        words = text.split()
        # find longest suffix of last_words that is prefix of words
        overlap = 0
        max_check = min(len(last_words), len(words))
        for k in range(max_check, 0, -1):
            if last_words[-k:] == words[:k]:
                overlap = k
                break
        new = words[overlap:]
        if new:
            deduped.append((start, " ".join(new)))
            last_words = words

    plain = " ".join(t for _, t in deduped)
    plain = _dup_re.sub(" ", plain).strip()
    timed_lines = []
    for start, text in deduped:
        mm, ss = divmod(int(start), 60)
        timed_lines.append(f"[{mm:02d}:{ss:02d}] {text}")
    return plain, "\n".join(timed_lines)


_print_lock = threading.Lock()


def _process_one_video(v, raw_dir, txt_dir, existing, source_id, since, until,
                       max_retries=3):
    """Download one video's transcript with retry + exponential backoff.

    Returns (record_dict, status_str) where status_str is one of:
      'cached'   already on disk and valid; no work done
      'fetched'  downloaded successfully
      'no_subs'  video has no captions available (or filtered by date window)
      'failed'   after all retries
    """
    vid = v["id"]
    if not vid:
        return None, "skipped"

    txt_path = txt_dir / f"{vid}.txt"
    timed_path = txt_dir / f"{vid}.timed.txt"
    rec = dict(existing.get(vid, {}))
    rec.update(v)
    rec["source_id"] = source_id

    # Already-present and valid? Skip the network round-trip entirely.
    if txt_path.exists() and _is_valid_transcript(txt_path):
        plain = txt_path.read_text(encoding="utf-8")
        rec["has_transcript"] = True
        rec["word_count"] = len(plain.split())
        return rec, "cached"

    last_err = None
    for attempt in range(max_retries):
        if attempt > 0:
            # Exponential backoff with jitter — 1s, then 2-3s, then 4-5s.
            time.sleep((2 ** (attempt - 1)) + random.uniform(0, 1.0))
        try:
            srt_path = download_subs(vid, raw_dir, since=since, until=until)
            if not srt_path:
                # No subs available (or window-filtered). Don't retry.
                if since or until:
                    return None, "no_subs"  # window-filtered → drop record
                rec["has_transcript"] = False
                rec["word_count"] = 0
                return rec, "no_subs"

            plain, timed = srt_to_text(srt_path)
            _atomic_write_text(txt_path, plain)
            _atomic_write_text(timed_path, timed)
            rec["has_transcript"] = True
            rec["word_count"] = len(plain.split())
            return rec, "fetched"
        except Exception as e:
            last_err = e

    # All retries exhausted
    with _print_lock:
        print(f"      ! {vid} failed after {max_retries} attempts: {last_err}")
    rec["has_transcript"] = False
    rec["word_count"] = 0
    return rec, "failed"


def main():
    ap = argparse.ArgumentParser(
        description="Fetch all transcripts from a YouTube channel for a source.")
    ap.add_argument("--url", required=True,
                    help="Channel /videos URL (e.g. https://www.youtube.com/@channel/videos)")
    ap.add_argument("--source", required=True,
                    help="Source id from sources.json (used to scope output paths)")
    ap.add_argument("--window", default="all", choices=list(WINDOW_PRESETS.keys()),
                    help="Time window for fetched videos: 1d/1w/1m/3m/6m/1y/all (default: all)")
    ap.add_argument("--since", default=None,
                    help="Absolute lower date bound YYYYMMDD (overrides --window)")
    ap.add_argument("--until", default=None,
                    help="Absolute upper date bound YYYYMMDD")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel transcript downloads (default 4; max 8 recommended "
                         "to avoid YouTube rate limits)")
    ap.add_argument("--retries", type=int, default=3,
                    help="Retry attempts per video on transient failures (default 3)")
    args = ap.parse_args()
    args.workers = max(1, min(args.workers, 8))

    since, until = resolve_window(args.window, args.since, args.until)
    if since or until:
        print(f"      Time-window filter: since={since or '-'} until={until or '-'}")

    paths = paths_for(args.source)
    raw_dir = paths["raw"]; txt_dir = paths["txt"]; meta_path = paths["meta"]
    raw_dir.mkdir(parents=True, exist_ok=True)
    txt_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/3] Listing videos from {args.url} (source: {args.source})")
    videos = list_videos(args.url)
    print(f"      Found {len(videos)} videos.")

    # Resume scan: how many of these are already on disk and valid?
    valid_ids = {p.stem for p in txt_dir.glob("*.txt")
                 if not p.name.endswith(".timed.txt") and _is_valid_transcript(p)}
    invalid_ids = {p.stem for p in txt_dir.glob("*.txt")
                   if not p.name.endswith(".timed.txt") and not _is_valid_transcript(p)}
    listed_ids = {v["id"] for v in videos if v.get("id")}
    if valid_ids or invalid_ids:
        already = len(valid_ids & listed_ids)
        corrupt = len(invalid_ids & listed_ids)
        print(f"      Resume detected: {already} already downloaded and valid"
              + (f", {corrupt} partial/corrupt (will re-fetch)" if corrupt else "")
              + f", {len(listed_ids - valid_ids)} pending.")
        for vid in invalid_ids & listed_ids:
            (txt_dir / f"{vid}.txt").unlink(missing_ok=True)
            (txt_dir / f"{vid}.timed.txt").unlink(missing_ok=True)

    existing = {}
    if meta_path.exists():
        try:
            for rec in json.load(open(meta_path)):
                existing[rec["id"]] = rec
        except Exception:
            pass

    print(f"[2/3] Downloading transcripts with {args.workers} parallel worker(s)…")
    records = []
    counts = {"cached": 0, "fetched": 0, "no_subs": 0, "failed": 0, "skipped": 0}
    done = 0
    total = len(videos)
    started = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {}
        for v in videos:
            if not v.get("id"):
                continue
            futures[ex.submit(
                _process_one_video, v, raw_dir, txt_dir, existing,
                args.source, since, until, args.retries,
            )] = v
        for future in as_completed(futures):
            v = futures[future]
            rec, status = future.result()
            counts[status] = counts.get(status, 0) + 1
            done += 1
            if rec is not None:
                records.append(rec)
            with _print_lock:
                tag = {"cached": "·", "fetched": "✓", "no_subs": "—",
                       "failed": "✗", "skipped": "·"}.get(status, "?")
                rate = done / max(0.1, time.time() - started)
                print(f"      [{done:>3}/{total}] {tag} {v['id']}  "
                      f"({status:<7}) {rate:.1f}/s  {v['title'][:55]}")

    # Enrich missing metadata for fetched videos (serial — small batch, only
    # for the few that need it). Most records already have full metadata from
    # the flat-playlist listing.
    needs_enrich = [r for r in records if r.get("has_transcript")
                    and (not r.get("upload_date") or not r.get("view_count"))]
    if needs_enrich:
        print(f"      Enriching metadata for {len(needs_enrich)} videos…")
        for rec in needs_enrich:
            full = enrich_metadata(rec["id"])
            for k in ("upload_date", "view_count", "duration", "like_count",
                      "comment_count", "description", "tags", "channel"):
                if full.get(k) is not None and not rec.get(k):
                    rec[k] = full.get(k)

    elapsed = time.time() - started
    print(f"      Stage 2 done in {elapsed:.1f}s  ·  "
          f"cached={counts.get('cached',0)} fetched={counts.get('fetched',0)} "
          f"no_subs={counts.get('no_subs',0)} failed={counts.get('failed',0)}")

    print(f"[3/3] Writing metadata to {meta_path}")
    _atomic_write_text(meta_path, json.dumps(records, indent=2, default=str))

    have = sum(1 for r in records if r.get("has_transcript"))
    total_words = sum(r.get("word_count", 0) for r in records)
    print(f"\nDone. {have}/{len(records)} videos with transcripts. {total_words:,} total words.")


if __name__ == "__main__":
    main()
