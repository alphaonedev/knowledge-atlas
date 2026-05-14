# Knowledge Card Extraction Schema

Each video produces one JSON file named `<video_id>.json` in this directory.

## File format

```json
{
  "video_id": "tRABRy-awzM",
  "video_title": "...",
  "video_url": "https://www.youtube.com/watch?v=...",
  "one_line": "A single sentence (≤ 25 words) capturing the core thesis of the video.",
  "best_for": "Watch this if you... (one sentence, situational guidance).",
  "categories": ["short topical tags, e.g. cross-examination, narcissist-tactics, mediation, custody-strategy"],
  "cards": [
    {
      "kind": "principle | tactic | warning | framework | mental_model | phrase | quote",
      "category": "specific topic (e.g. cross-examination, dealing-with-narcissist, witness-selection)",
      "title": "5–10 word title that names the knowledge",
      "content": "1–3 clean sentences in modern paraphrased English. The knowledge itself, in standalone form.",
      "reasoning": "Optional: why this works / why it matters (1 sentence). Omit if obvious.",
      "source_quote": "Optional: short verbatim quote from the speaker that anchors the claim (≤ 30 words).",
      "framework_steps": ["step 1", "step 2", "..."]
    }
  ]
}
```

## Card kinds

- **principle** — a general rule or law the expert teaches ("Never argue with a narcissist in front of the judge")
- **tactic** — a specific action to take in a specific situation ("Ask narrow yes/no questions during cross-examination")
- **warning** — a thing to avoid + why ("Don't bring rambling witnesses; the judge tunes out")
- **framework** — a named, multi-step approach (use `framework_steps`)
- **mental_model** — a reframe / way of thinking ("Treat the courtroom like a chess match, not a fistfight")
- **phrase** — exact language to use ("Your honor, I'd like to offer this as exhibit A")
- **quote** — a memorable, punchy line worth preserving (rare — only if genuinely memorable)

## Rules

1. **Paraphrase aggressively.** YouTube auto-captions are full of disfluencies ("you know", "uh", "I mean", repeated phrases). Strip all of it and rewrite into clean modern English.
2. **Standalone value.** Each card must be useful even without watching the video. No "as I mentioned" or "like he said".
3. **No fluff.** If a video has 4 valuable nuggets, return 4 — not 12 padded ones. Aim for 4–12 cards per video; fewer if the video is conversational.
4. **Skip the dross.** Discard: intros, "smash that subscribe button", rhetorical questions, emotional bonding, recap of previous videos, anecdotes that don't carry a lesson.
5. **Concrete over abstract.** "Document every text message with date and screenshot" beats "be organized".
6. **Quote sparingly.** Only include `source_quote` when the speaker's exact words are punchier than your paraphrase.
7. **Output strict JSON only** — no markdown fences, no commentary.

## When extraction is impossible

If a video is purely conversational with no extractable knowledge, still emit a valid JSON with empty `cards: []` and explain in `one_line`.
