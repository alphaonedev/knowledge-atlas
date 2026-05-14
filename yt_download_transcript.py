#!/usr/bin/env python3
# Knowledge Atlas — Single-video transcript helper (yt-dlp; legacy utility).
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
YouTube Transcript Downloader (yt-dlp only; no youtube-transcript-api needed)

- Prompts user for a YouTube URL
- Tries manual subs first, then auto-subs
- Produces:
    <video_id>_transcript.srt
    <video_id>_transcript.txt
"""

import os
import re
import sys
import glob
import shutil
import subprocess
from urllib.parse import urlparse, parse_qs

PREF_LANGS = ["en", "en-US", "en-GB"]

def extract_video_id(url: str) -> str:
    p = urlparse(url)
    host = p.netloc.lower()
    if "youtu.be" in host:
        return p.path.lstrip("/").split("/")[0]
    if "youtube.com" in host:
        if p.path == "/watch":
            return parse_qs(p.query).get("v", [""])[0]
        if p.path.startswith("/shorts/") or p.path.startswith("/embed/"):
            parts = p.path.split("/")
            if len(parts) >= 3 and parts[2]:
                return parts[2]
    cand = p.path.strip("/").split("/")[-1]
    if len(cand) >= 11:
        return cand
    raise ValueError("Could not extract a valid YouTube video ID.")

def have_yt_dlp():
    try:
        subprocess.run(["yt-dlp", "--version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        return False

def run_yt_dlp(url: str, video_id: str) -> str:
    """
    Use yt-dlp to download subtitles in preferred langs as SRT.
    Returns the chosen .srt path.
    """
    # Output template will be just the video id, so files like: <id>.<lang>.srt
    outtmpl = f"{video_id}.%(ext)s"

    base_cmd = [
        "yt-dlp", url,
        "--skip-download",
        "--sub-format", "srt",
        "--sub-lang", ",".join(PREF_LANGS),
        "--convert-subs", "srt",
        "-o", outtmpl,
    ]

    # 1) Try manual subtitles
    try:
        subprocess.run(base_cmd + ["--write-subs"], check=True)
    except subprocess.CalledProcessError:
        pass

    # 2) If none produced, try auto-subs
    srt_candidates = glob.glob(f"{video_id}*.srt")
    if not srt_candidates:
        try:
            subprocess.run(base_cmd + ["--write-auto-subs"], check=True)
        except subprocess.CalledProcessError:
            pass
        srt_candidates = glob.glob(f"{video_id}*.srt")

    if not srt_candidates:
        raise RuntimeError("No subtitles found (manual or auto).")

    # Prefer language order
    def lang_score(path: str) -> int:
        # Expect filenames like "<id>.<lang>.srt"
        for i, lang in enumerate(PREF_LANGS):
            if f".{lang}." in f".{path}." or path.endswith(f".{lang}.srt"):
                return i
        return len(PREF_LANGS) + 1  # worst

    srt_candidates.sort(key=lang_score)
    chosen = srt_candidates[0]
    # Normalize output filename
    final_srt = f"{video_id}_transcript.srt"
    if os.path.abspath(chosen) != os.path.abspath(final_srt):
        shutil.copyfile(chosen, final_srt)
    return final_srt

_time_re = re.compile(r"(\d+):(\d+):(\d+),(\d+)")

def parse_start_seconds(line: str) -> float:
    # SRT timing line: "HH:MM:SS,mmm --> HH:MM:SS,mmm"
    m = _time_re.search(line)
    if not m:
        return 0.0
    h, m_, s, ms = map(int, m.groups())
    return h*3600 + m_*60 + s + ms/1000.0

def srt_to_txt(srt_path: str, txt_path: str):
    with open(srt_path, "r", encoding="utf-8", errors="replace") as f:
        lines = [ln.rstrip("\n") for ln in f]

    out = []
    i = 0
    while i < len(lines):
        # Skip index line if present
        if lines[i].strip().isdigit() and i+1 < len(lines) and "-->" in lines[i+1]:
            i += 1  # move to timing
        # Timing line
        if i < len(lines) and "-->" in lines[i]:
            start_sec = parse_start_seconds(lines[i])
            i += 1
            # Collect text lines until blank
            block = []
            while i < len(lines) and lines[i].strip() != "":
                block.append(lines[i])
                i += 1
            i += 1  # skip blank
            text = " ".join(block).strip()
            mm, ss = divmod(int(start_sec), 60)
            out.append(f"[{mm:02d}:{ss:02d}] {text}")
        else:
            i += 1

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

def main():
    print("🟢 YouTube Transcript Downloader")
    url = input("Enter YouTube URL: ").strip()

    try:
        vid = extract_video_id(url)
    except Exception as e:
        print(f"❌ URL error: {e}")
        sys.exit(1)

    if not have_yt_dlp():
        print("❌ yt-dlp is not installed. Install with: pip install -U yt-dlp")
        sys.exit(2)

    try:
        srt_path = run_yt_dlp(url, vid)
        txt_path = f"{vid}_transcript.txt"
        srt_to_txt(srt_path, txt_path)
        print(f"✅ Saved:\n  • {srt_path}\n  • {txt_path}")
    except Exception as e:
        print(f"❌ Error retrieving subtitles: {e}")
        sys.exit(3)

if __name__ == "__main__":
    main()
