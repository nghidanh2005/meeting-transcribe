---
name: transcribe-pipeline
description: Turn a local video or audio recording into a transcript and meeting minutes using Amazon Transcribe. Load when the user hands over a path to a meeting recording, asks to transcribe a video, asks for minutes or action items from a recording, or asks why a transcript came back nearly empty.
---

# Transcribe a recording

Six verbs, run in order, all through one script. Never hand-roll the ffmpeg or
AWS commands — the script carries the exact forms that are known to work,
including the Windows quoting workarounds.

```
SCRIPT = <crew>/apps/meeting-transcribe/scripts/mt.py
```

`<crew>` is `~/.kiro/crew` (or `$KIROCREW_HOME`). Use the platform's python:
`python3` on macOS/Linux, and on Windows the full interpreter path if `python`
resolves to a Store stub.

| Step | Command | Costs money |
|------|---------|-------------|
| 1 | `python $SCRIPT probe <video>` | no |
| 2 | `python $SCRIPT preflight <video> --job-id <id>` | **no — this is the gate** |
| 3 | `python $SCRIPT extract <video> --job-id <id>` | no |
| 4 | `python $SCRIPT submit <id>` | **YES** |
| 5 | `python $SCRIPT poll <id>` | no |
| 6 | `python $SCRIPT render <id>` | no |

## The gate is not optional

`preflight` exits **3** and writes `preflight_verdict: "refused"` when the file
is more silent than `silence_gate_pct` (default 80%). `extract` and `submit`
both refuse to run on a refused job.

**When preflight refuses, stop and report. Do not pass `--force`** unless the
user explicitly tells you to after seeing the numbers. The refusal exists
because a silent file still bills for its full duration: a 78-minute recording
that was 98.7% digital silence cost $0.47 and returned 132 words.

Report the refusal with the measured evidence, in this shape:

- duration vs. how much of it is silence, as a percentage
- the per-5-minute mean volume table, so the dropout is visible as a *position*
- the `+40 dB` result on the quietest window — if max volume stays below -80 dB
  after amplification the samples are true zeros and nothing is recoverable
- the conclusion: this is a **capture-side** failure, not a transcription one

Then tell the user to check the recorder's audio source and do a two-minute test
capture before the next long meeting. Do not offer to re-run on the same file.

## After a good transcript

`render` writes `<crew>/workspace/meeting-transcribe/<id>-transcript.md` with
timestamps, speaker turns and per-turn confidence, plus automatic warnings when
content is thin or confidence is low.

**Only then** write minutes, to `<id>-minutes.md` beside it: decisions taken,
open questions, and action items with an owner where one is actually named.

Do **not** write minutes when there is no meeting in the file. A 63-second
phone-call opening is not a meeting — say so instead of producing a hollow
document with invented action items.

## Code-switching (Vietnamese + English in one sentence)

`vi-VN` on Transcribe supports batch, custom vocabulary and speaker diarization.
It does **not** support custom language models, PII redaction, Call Analytics, or
automatic language identification. Custom vocabulary is therefore the *only*
remaining quality lever — if it is not enough, the fix is a different service,
not a different setting.

When English terms come back phonetically mangled, collect them and add them to
a Transcribe custom vocabulary, then set `vocabulary_name` in
`<crew>/workspace/meeting-transcribe/config.json`. Low-confidence words
(`< 0.50`) in an otherwise clean transcript are the best place to look.

## Reading state

Everything is in `<crew>/workspace/meeting-transcribe/state.json`, newest job
first. `stage` moves: `preflight-ok` / `refused` → `extracted` → `running` →
`transcribed` → `rendered`, or `failed`.

## Known-unverified

State these as unknown rather than guessing:

- Transcribe batch hard limits (max duration / file size) — not confirmed. A
  78-minute file works.
- `ap-southeast-1` per-minute price. `$0.006/min` is the published us-east-1
  batch tier-1 rate and is what the cost estimate uses.
- There is no published Vietnamese word-error rate for Transcribe, and none at
  all for code-switched speech. Quality on mixed-language meetings is unmeasured.
