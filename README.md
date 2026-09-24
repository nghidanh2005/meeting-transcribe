# Meeting Transcribe

A Kiro Crew app that turns a local meeting recording into a timestamped,
speaker-labelled transcript using Amazon Transcribe — and **refuses to spend
money on a recording that has no audio in it.**

## Why the gate exists

The app was built after a real failure. A 77m46s screen recording (3.3 GB) was
extracted, uploaded, and transcribed successfully. The pipeline worked, the job
completed in 77 seconds, and it cost $0.47. The transcript was **132 words**.

The recording turned out to be 98.7% digital silence — the recorder had lost its
audio device about nine minutes in and kept writing zero samples for the next
68 minutes. Amplifying the dead section by 40 dB left it at the noise floor,
which proves the samples were true zeros with nothing to recover.

A ten-second measurement would have caught that for free. So that measurement is
now step two, and steps three onward refuse to run without it.

## Pipeline

| Step | Verb | Billable |
|------|------|----------|
| 1 | `probe` — duration, streams, credential check | no |
| 2 | `preflight` — silence map, cost estimate, **verdict** | no |
| 3 | `extract` — 16 kHz mono WAV (3.3 GB video becomes ~142 MB) | no |
| 4 | `submit` — bucket, upload, start batch job | **yes** |
| 5 | `poll` — status, download transcript JSON | no |
| 6 | `render` — markdown with timestamps and confidence | no |

`preflight` exits 3 and records `refused` when silence exceeds the gate
(default 80%). `extract` and `submit` both refuse a refused job. `--force`
exists but the shipped skill instructs the agent not to use it unasked.

## What you get

- `<job>-transcript.md` — speaker turns with timestamps and per-turn confidence,
  plus automatic warnings when content is thin or confidence is low
- Minutes and action items, written by the agent **only when the transcript
  actually contains a meeting**
- A dashboard page showing the queue, the per-5-minute volume map for refused
  files, and how much billing the gate has prevented

## Requirements

- `ffmpeg` and `ffprobe` on `PATH`
- AWS CLI v2 with credentials that can use S3 and Transcribe
- Python 3.10+

Deliberately **not** declared in `dependencies.commands`: that check runs through
`which`, and a failure there is noisier than a clear runtime message. The app
tells you precisely which tool is missing when you run it.

## Configuration

`~/.kiro/crew/workspace/meeting-transcribe/config.json`:

| Key | Default | Notes |
|-----|---------|-------|
| `aws_cli` | `""` | full path; blank auto-discovers. A bare `aws` on PATH can be an older build missing subcommands |
| `profile` | `""` | blank uses the default credential chain |
| `region` | `ap-southeast-1` | always passed explicitly |
| `bucket` | `""` | blank derives `kiro-transcribe-<account>-<region>` |
| `language` | `vi-VN` | any Transcribe language code |
| `max_speakers` | `10` | diarization is included in the base price |
| `vocabulary_name` | `""` | custom vocabulary, the only code-switching lever `vi-VN` has |
| `silence_gate_pct` | `80` | above this, refuse |
| `price_per_minute_usd` | `0.006` | published us-east-1 batch tier 1 |
| `keep_wav` | `false` | keep the extracted WAV after rendering |

## Vietnamese and code-switching

`vi-VN` supports batch, custom vocabulary and speaker diarization. It does
**not** support custom language models, PII redaction, Call Analytics, or
automatic language identification. Custom vocabulary is therefore the only
quality lever available — if it is not enough, the answer is a different
service, not a different setting.

## Known-unverified

Stated as unknown rather than guessed:

- Transcribe batch hard limits (max duration / file size). A 78-minute file works.
- `ap-southeast-1` per-minute pricing. The estimate uses the published
  us-east-1 batch tier-1 rate.
- There is no published Vietnamese word-error rate for Transcribe, and none for
  code-switched speech. Quality on mixed-language meetings is unmeasured.

## Install

```bash
kirocrew app install /path/to/meeting-transcribe
```

Then enable it in the dashboard. Third-party apps are off by default —
**Settings → Security** has the toggle. The app self-heals its config on the
first maintenance cron run (within 5 minutes); the page shows an initializing
notice until then.

## License

MIT
