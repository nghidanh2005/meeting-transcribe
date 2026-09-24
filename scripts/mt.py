#!/usr/bin/env python3
"""meeting-transcribe pipeline.

One script, six verbs. Every command in here ran successfully end to end on
2026-09-22 against a 77m46s / 3.3 GB MP4 before being packaged.

    python mt.py probe      <video>          measure the file and the credential
    python mt.py preflight  <video>          silence + cost gate  (SPEND NOTHING)
    python mt.py extract    <video>          16 kHz mono WAV
    python mt.py submit     <job-id>         bucket + upload + start job
    python mt.py poll       [job-id]         status; downloads JSON when done
    python mt.py render     <job-id>         transcript JSON -> markdown

The gate is the point. `preflight` is the only verb that decides whether money
gets spent, and `submit` refuses to run until preflight has recorded a verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

APP = "meeting-transcribe"

# ---------------------------------------------------------------- paths / state


def crew_home() -> Path:
    return Path(os.environ.get("KIROCREW_HOME") or (Path.home() / ".kiro" / "crew"))


def state_dir() -> Path:
    d = crew_home() / "workspace" / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def work_dir() -> Path:
    """Where WAVs and raw JSON live. Big files, safe to lose."""
    base = os.environ.get("KIROCREW_SCRATCH") or os.environ.get("TMPDIR") or os.environ.get("TEMP")
    d = Path(base or Path.home()) / "meeting-transcribe-work"
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULT_CONFIG = {
    "aws_cli": "",                      # blank = auto-discover
    "profile": "",                      # blank = default credential chain
    "region": "ap-southeast-1",
    "bucket": "",                       # blank = derive from account id
    "language": "vi-VN",
    "max_speakers": 10,
    "vocabulary_name": "",              # the only code-switching lever vi-VN has
    "silence_gate_pct": 80.0,
    "price_per_minute_usd": 0.006,      # us-east-1 batch tier 1, published
    "keep_wav": False,
}


def load_config() -> dict:
    p = state_dir() / "config.json"
    cfg = dict(DEFAULT_CONFIG)
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"  warn: config.json unreadable ({exc}); using defaults")
    return cfg


def save_config(cfg: dict) -> None:
    (state_dir() / "config.json").write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_state() -> dict:
    p = state_dir() / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"version": 1, "updated_at": None, "jobs": []}


def save_state(st: dict) -> None:
    st["updated_at"] = now_iso()
    tmp = state_dir() / "state.json.tmp"
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state_dir() / "state.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_job(st: dict, job_id: str) -> dict | None:
    return next((j for j in st["jobs"] if j["id"] == job_id), None)


def upsert_job(st: dict, job: dict) -> None:
    for i, j in enumerate(st["jobs"]):
        if j["id"] == job["id"]:
            st["jobs"][i] = job
            return
    st["jobs"].insert(0, job)


# ---------------------------------------------------------------- tool discovery

WIN_AWS = r"C:\Users\%s\AppData\Local\Programs\Amazon\AWSCLIV2\aws.exe"


def find_aws(cfg: dict) -> str:
    """The bare `aws` on PATH can be an older build missing subcommands, so an
    explicit full path wins when one is configured or discoverable."""
    if cfg.get("aws_cli") and Path(cfg["aws_cli"]).exists():
        return cfg["aws_cli"]
    guess = Path(WIN_AWS % os.environ.get("USERNAME", ""))
    if guess.exists():
        return str(guess)
    found = shutil.which("aws")
    if found:
        return found
    die("AWS CLI not found. Install it, or set aws_cli in config.json to the full path.")


def need(tool: str) -> str:
    found = shutil.which(tool)
    if not found:
        die(f"`{tool}` is not on PATH. ffmpeg and ffprobe are both required.")
    return found


def die(msg: str) -> None:
    print(f"ERROR: {msg}")
    sys.exit(1)


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


def aws_args(cfg: dict) -> list[str]:
    """Region is always explicit: a profile with no configured region otherwise
    fails with a confusing 'you must specify a region'."""
    a = ["--region", cfg["region"]]
    if cfg.get("profile"):
        a += ["--profile", cfg["profile"]]
    return a


# ---------------------------------------------------------------- ffprobe helpers


def probe_media(path: Path) -> dict:
    need("ffprobe")
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size",
             "-show_entries", "stream=codec_type,codec_name,sample_rate,channels",
             "-of", "json", "--", str(path)])
    if r.returncode != 0:
        die(f"ffprobe failed: {r.stderr.strip()}")
    data = json.loads(r.stdout)
    streams = data.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    return {
        "duration_sec": float(data.get("format", {}).get("duration") or 0.0),
        "size_bytes": int(data.get("format", {}).get("size") or 0),
        "audio_streams": audio,
    }


def mean_volume(path: Path, start: float | None = None,
                dur: float | None = None, boost_db: int = 0) -> dict:
    """mean/max volume over the whole file or one window.

    boost_db amplifies before measuring. That is the test that separates 'very
    quiet' from 'true digital zeros': amplifying zeros yields zeros, so a window
    that stays at the floor after +40 dB has no signal to recover.
    """
    need("ffmpeg")
    args = ["ffmpeg", "-hide_banner"]
    if start is not None:
        args += ["-ss", str(start)]
    if dur is not None:
        args += ["-t", str(dur)]
    af = "volumedetect" if not boost_db else f"volume={boost_db}dB,volumedetect"
    args += ["-i", str(path), "-af", af, "-f", "null", "-"]
    r = run(args)
    out = {"mean_db": None, "max_db": None}
    for line in r.stderr.splitlines():
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", line)
        if m:
            out["mean_db"] = float(m.group(1))
        m = re.search(r"max_volume:\s*(-?[\d.]+) dB", line)
        if m:
            out["max_db"] = float(m.group(1))
    return out


def silence_total(path: Path, noise_db: int = -45, min_dur: int = 20) -> tuple[float, int]:
    need("ffmpeg")
    r = run(["ffmpeg", "-hide_banner", "-i", str(path), "-af",
             f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"])
    total, runs = 0.0, 0
    for line in r.stderr.splitlines():
        m = re.search(r"silence_duration:\s*([\d.]+)", line)
        if m:
            total += float(m.group(1))
            runs += 1
    return total, runs


def fmt_hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:d}h{(sec % 3600) // 60:02d}m{sec % 60:02d}s" if sec >= 3600 \
        else f"{sec // 60:d}m{sec % 60:02d}s"


# ---------------------------------------------------------------- verbs


def cmd_probe(args) -> None:
    cfg = load_config()
    src = Path(args.video)
    if not src.exists():
        die(f"file not found: {src}")

    info = probe_media(src)
    print("=== FILE ===")
    print(f"  path     : {src}")
    print(f"  size     : {info['size_bytes'] / 1048576:,.1f} MB")
    print(f"  duration : {info['duration_sec']:,.1f}s  ({fmt_hms(info['duration_sec'])})")
    print(f"  audio    : {len(info['audio_streams'])} stream(s)")
    for s in info["audio_streams"]:
        print(f"    - {s.get('codec_name')} {s.get('sample_rate')} Hz "
              f"{s.get('channels')} ch")
    if not info["audio_streams"]:
        die("no audio stream at all; nothing to transcribe")

    print()
    print("=== CREDENTIAL ===")
    aws = find_aws(cfg)
    r = run([aws, "sts", "get-caller-identity", *aws_args(cfg), "--output", "json"])
    if r.returncode != 0:
        print(f"  DEAD: {r.stderr.strip() or r.stdout.strip()}")
        print("  Run `aws configure` / `aws sso login`, then retry.")
        sys.exit(2)
    ident = json.loads(r.stdout)
    print(f"  cli      : {aws}")
    print(f"  account  : {ident['Account']}")
    print(f"  arn      : {ident['Arn']}")
    print(f"  region   : {cfg['region']}")
    bucket = cfg["bucket"] or f"kiro-transcribe-{ident['Account']}-{cfg['region']}"
    suffix = "" if cfg["bucket"] else "   (will be derived on submit)"
    print(f"  bucket   : {bucket}{suffix}")


def cmd_preflight(args) -> None:
    """Measure BEFORE paying. This is the verb that earns the app its keep."""
    cfg = load_config()
    src = Path(args.video)
    if not src.exists():
        die(f"file not found: {src}")

    info = probe_media(src)
    dur = info["duration_sec"]
    if dur <= 0:
        die("could not read a duration; refusing to guess")

    print(f"=== PREFLIGHT: {src.name} ===")
    print(f"  duration {dur:,.1f}s ({fmt_hms(dur)}), "
          f"{info['size_bytes'] / 1048576:,.1f} MB, "
          f"{len(info['audio_streams'])} audio stream(s)")

    whole = mean_volume(src)
    sil_sec, sil_runs = silence_total(src)
    sil_pct = (sil_sec / dur * 100.0) if dur else 0.0

    print()
    print("=== LOUDNESS / SILENCE ===")
    print(f"  mean volume      : {whole['mean_db']} dB")
    print(f"  max  volume      : {whole['max_db']} dB")
    print(f"  silent >=20s runs: {sil_runs}")
    print(f"  total silence    : {sil_sec:,.0f}s of {dur:,.0f}s  ({sil_pct:.1f}%)")

    # per-5-minute map, so a dropout is visible as a position not just a ratio
    print()
    print("=== MEAN VOLUME PER 5 MIN ===")
    blocks = []
    for b in range(int(dur // 300) + 1):
        ss = b * 300
        if ss >= dur:
            break
        mv = mean_volume(src, start=ss, dur=min(300, dur - ss))
        blocks.append({"from_min": b * 5, "mean_db": mv["mean_db"]})
        print(f"  {b * 5:>4}-{b * 5 + 5:<4} min : mean {mv['mean_db']} dB")

    est_cost = dur / 60.0 * float(cfg["price_per_minute_usd"])
    gate = float(cfg["silence_gate_pct"])
    verdict = "ok" if sil_pct < gate else "refused"

    zeros_proof = None
    if verdict == "refused" and blocks:
        # Distinguish 'quiet' from 'nothing'. Amplifying true zeros stays at the
        # floor, and that is what makes the refusal defensible rather than fussy.
        # Measure the QUIETEST window, not the whole file: a file with one good
        # minute in it has a loud max overall, which proves nothing about the
        # dead part.
        quietest = min((b for b in blocks if b["mean_db"] is not None),
                       key=lambda b: b["mean_db"], default=None)
        if quietest is not None:
            ss = quietest["from_min"] * 60
            boosted = mean_volume(src, start=ss, dur=min(300, dur - ss), boost_db=40)
            zeros_proof = dict(boosted, window_from_min=quietest["from_min"],
                               window_mean_db=quietest["mean_db"])
            print()
            print(f"=== IS ANYTHING BURIED IN THE QUIET PART? "
                  f"(+40 dB on {quietest['from_min']}-{quietest['from_min'] + 5} min) ===")
            print(f"  before boost: mean {quietest['mean_db']} dB")
            print(f"  after  boost: mean {boosted['mean_db']} dB, "
                  f"max {boosted['max_db']} dB")
            if boosted["max_db"] is not None and boosted["max_db"] < -80:
                print("  -> amplifying 40 dB changed nothing: the samples are true")
                print("     zeros. There is no signal to recover. This is a CAPTURE")
                print("     failure, not a transcription problem.")

    print()
    print("=== VERDICT ===")
    print(f"  estimated cost : ${est_cost:,.2f}  "
          f"({dur / 60:,.1f} min x ${cfg['price_per_minute_usd']}/min)")
    print(f"  silence gate   : {gate:.0f}%")
    if verdict == "ok":
        print(f"  VERDICT: OK to submit ({sil_pct:.1f}% silent)")
    else:
        print(f"  VERDICT: REFUSED - {sil_pct:.1f}% of this file is silent.")
        print("  Submitting would bill for the full duration and return almost")
        print("  nothing. Check the recorder's audio source and re-record.")
        print("  Override with --force if you know better.")

    job_id = args.job_id or default_job_id(src)
    st = load_state()
    job = get_job(st, job_id) or {"id": job_id, "created_at": now_iso()}
    job.update({
        "source": str(src),
        "source_size_mb": round(info["size_bytes"] / 1048576, 1),
        "duration_sec": round(dur, 1),
        "audio_stream_count": len(info["audio_streams"]),
        "mean_db": whole["mean_db"],
        "max_db": whole["max_db"],
        "silence_sec": round(sil_sec, 1),
        "silence_pct": round(sil_pct, 1),
        "silence_runs": sil_runs,
        "blocks": blocks,
        "zeros_proof": zeros_proof,
        "est_cost_usd": round(est_cost, 2),
        "preflight_verdict": verdict,
        "preflight_at": now_iso(),
        "stage": "preflight-ok" if verdict == "ok" else "refused",
        "updated_at": now_iso(),
    })
    upsert_job(st, job)
    save_state(st)
    print()
    print(f"  job id: {job_id}   (recorded in {state_dir() / 'state.json'})")
    if verdict != "ok" and not args.force:
        sys.exit(3)


def default_job_id(src: Path) -> str:
    stem = re.sub(r"[^a-zA-Z0-9]+", "-", src.stem).strip("-").lower()[:40]
    return f"{stem or 'job'}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def cmd_extract(args) -> None:
    cfg = load_config()
    src = Path(args.video)
    if not src.exists():
        die(f"file not found: {src}")
    st = load_state()
    job_id = args.job_id or default_job_id(src)
    job = get_job(st, job_id)
    if job is None:
        die(f"no job {job_id}; run preflight first so the gate is recorded")
    if job.get("preflight_verdict") != "ok" and not args.force:
        die(f"preflight verdict is '{job.get('preflight_verdict')}'; "
            "refusing to extract. Use --force to override.")

    need("ffmpeg")
    wav = work_dir() / f"{job_id}.wav"
    if wav.exists():
        wav.unlink()

    print(f"Extracting 16 kHz mono PCM -> {wav}")
    t0 = time.monotonic()
    # -vn drops the video: uploading audio only turned 3.3 GB into 142 MB.
    r = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
    took = time.monotonic() - t0
    if r.returncode != 0 or not wav.exists():
        die(f"ffmpeg failed: {r.stderr.strip()}")

    mb = wav.stat().st_size / 1048576
    print(f"DONE in {took:,.0f}s -> {mb:,.1f} MB")

    job.update({"wav": str(wav), "wav_mb": round(mb, 1),
                "stage": "extracted", "updated_at": now_iso()})
    upsert_job(st, job)
    save_state(st)


def cmd_submit(args) -> None:
    cfg = load_config()
    st = load_state()
    job = get_job(st, args.job_id)
    if job is None:
        die(f"no job {args.job_id} in state")
    if job.get("preflight_verdict") != "ok" and not args.force:
        die(f"preflight verdict is '{job.get('preflight_verdict')}'; refusing "
            "to spend money. Use --force to override.")
    wav = Path(job.get("wav", ""))
    if not wav.exists():
        die("extracted WAV is missing; run extract first")

    aws = find_aws(cfg)
    base = aws_args(cfg)

    bucket = cfg.get("bucket")
    if not bucket:
        r = run([aws, "sts", "get-caller-identity", *base, "--output", "json"])
        if r.returncode != 0:
            die(f"cannot read account id: {r.stderr.strip()}")
        acct = json.loads(r.stdout)["Account"]
        bucket = f"kiro-transcribe-{acct}-{cfg['region']}"
        cfg["bucket"] = bucket
        save_config(cfg)
        print(f"  derived bucket name: {bucket}")

    print("=== 1. BUCKET ===")
    r = run([aws, "s3api", "head-bucket", "--bucket", bucket, *base])
    if r.returncode == 0:
        print(f"  exists: {bucket}")
    else:
        print(f"  creating: {bucket}")
        create = [aws, "s3api", "create-bucket", "--bucket", bucket, *base]
        if cfg["region"] != "us-east-1":
            create += ["--create-bucket-configuration",
                       f"LocationConstraint={cfg['region']}"]
        r = run(create)
        if r.returncode != 0:
            die(f"create-bucket failed: {r.stderr.strip()}")
        print("  created")

    key = f"{args.job_id}.wav"
    print()
    print(f"=== 2. UPLOAD ({job.get('wav_mb')} MB) ===")
    t0 = time.monotonic()
    r = run([aws, "s3", "cp", str(wav), f"s3://{bucket}/{key}", *base,
             "--only-show-errors"])
    if r.returncode != 0:
        die(f"upload failed: {r.stderr.strip()}")
    print(f"  uploaded in {time.monotonic() - t0:,.0f}s")

    print()
    print("=== 3. START JOB ===")
    settings = {"ShowSpeakerLabels": True,
                "MaxSpeakerLabels": int(cfg["max_speakers"])}
    if cfg.get("vocabulary_name"):
        settings["VocabularyName"] = cfg["vocabulary_name"]
    # Passed as a file:// to dodge Windows shell quoting of the JSON braces.
    sfile = work_dir() / f"{args.job_id}-settings.json"
    sfile.write_text(json.dumps(settings), encoding="ascii")
    print(f"  settings: {json.dumps(settings)}")

    r = run([aws, "transcribe", "start-transcription-job",
             "--transcription-job-name", args.job_id,
             "--language-code", cfg["language"],
             "--media", f"MediaFileUri=s3://{bucket}/{key}",
             "--output-bucket-name", bucket,
             "--output-key", "transcripts/",
             "--settings", f"file://{sfile}",
             *base, "--output", "json"])
    if r.returncode != 0:
        die(f"start-transcription-job failed: {r.stderr.strip() or r.stdout.strip()}")

    job.update({"bucket": bucket, "s3_key": key,
                "s3_uri": f"s3://{bucket}/{key}",
                "language": cfg["language"],
                "submitted_at": now_iso(), "stage": "running",
                "updated_at": now_iso()})
    upsert_job(st, job)
    save_state(st)
    print(f"  submitted: {args.job_id}")
    print(f"  billed for {job['duration_sec'] / 60:,.1f} min "
          f"~ ${job['est_cost_usd']:,.2f}")


def cmd_poll(args) -> None:
    cfg = load_config()
    st = load_state()
    targets = [get_job(st, args.job_id)] if args.job_id else \
        [j for j in st["jobs"] if j.get("stage") == "running"]
    targets = [t for t in targets if t]
    if not targets:
        print("nothing in flight")
        return

    aws = find_aws(cfg)
    base = aws_args(cfg)
    for job in targets:
        r = run([aws, "transcribe", "get-transcription-job",
                 "--transcription-job-name", job["id"], *base, "--output", "json"])
        if r.returncode != 0:
            print(f"  {job['id']}: query failed: {r.stderr.strip()}")
            continue
        tj = json.loads(r.stdout)["TranscriptionJob"]
        status = tj["TranscriptionJobStatus"]
        print(f"  {job['id']}: {status}")
        job["aws_status"] = status
        job["updated_at"] = now_iso()

        if status == "COMPLETED":
            uri = tj["Transcript"]["TranscriptFileUri"]
            s3key = f"transcripts/{job['id']}.json"
            raw = work_dir() / f"{job['id']}-raw.json"
            r2 = run([aws, "s3", "cp", f"s3://{job['bucket']}/{s3key}",
                      str(raw), *base, "--only-show-errors"])
            if r2.returncode != 0:
                print(f"    download failed: {r2.stderr.strip()}")
                print(f"    transcript is still readable at: {uri}")
                job["stage"] = "completed-not-downloaded"
            else:
                job["raw_json"] = str(raw)
                job["stage"] = "transcribed"
                print(f"    downloaded -> {raw}")
            job["completed_at"] = str(tj.get("CompletionTime", ""))
        elif status == "FAILED":
            job["stage"] = "failed"
            job["failure_reason"] = tj.get("FailureReason", "")
            print(f"    FAILED: {job['failure_reason']}")

        upsert_job(st, job)
    save_state(st)


def cmd_render(args) -> None:
    st = load_state()
    job = get_job(st, args.job_id)
    if job is None:
        die(f"no job {args.job_id}")
    raw = Path(job.get("raw_json", ""))
    if not raw.exists():
        die("no downloaded transcript JSON; run poll first")

    data = json.loads(raw.read_text(encoding="utf-8"))
    res = data["results"]
    items = res["items"]
    words = [i for i in items if i["type"] == "pronunciation"]
    if not words:
        die("transcript contains zero words")

    # word start_time -> speaker, when diarization ran
    spk_of: dict[str, str] = {}
    n_speakers = 0
    if "speaker_labels" in res:
        n_speakers = res["speaker_labels"].get("speakers", 0)
        for seg in res["speaker_labels"]["segments"]:
            for it in seg["items"]:
                spk_of[it["start_time"]] = it["speaker_label"]

    turns, cur = [], None
    for it in items:
        if it["type"] == "punctuation":
            if cur:
                cur["words"][-1] += it["alternatives"][0]["content"]
            continue
        spk = spk_of.get(it["start_time"], "spk_0")
        conf = float(it["alternatives"][0]["confidence"])
        w = it["alternatives"][0]["content"]
        if cur is None or cur["spk"] != spk:
            if cur:
                turns.append(cur)
            cur = {"spk": spk, "start": it["start_time"], "end": it["end_time"],
                   "words": [w], "confs": [conf]}
        else:
            cur["words"].append(w)
            cur["confs"].append(conf)
            cur["end"] = it["end_time"]
    if cur:
        turns.append(cur)

    confs = [float(w["alternatives"][0]["confidence"]) for w in words]
    low = [c for c in confs if c < 0.50]
    low_pct = len(low) / len(confs) * 100.0

    def mmss(t) -> str:
        t = float(t)
        return f"{int(t) // 60:02d}:{t % 60:05.2f}"

    dur = float(job.get("duration_sec") or 0)
    speech_span = float(words[-1].get("end_time", 0)) - float(words[0].get("start_time", 0))

    L: list[str] = []
    L.append(f"# Transcript - {Path(job['source']).name}")
    L.append("")
    L.append(f"Amazon Transcribe batch | language {job.get('language')} | "
             f"job `{job['id']}`")
    L.append(f"Duration {fmt_hms(dur)} | {len(words)} words | {len(turns)} turns | "
             f"{n_speakers} machine-detected speakers")
    L.append("")

    # Honest header when the content is thin relative to the runtime.
    words_per_min = len(words) / (dur / 60.0) if dur else 0
    if words_per_min < 20 or job.get("silence_pct", 0) >= 50:
        L.append("## Warning: thin content for this runtime")
        L.append("")
        L.append(f"- File runs {fmt_hms(dur)} but speech spans only "
                 f"{speech_span:,.0f}s ({words_per_min:.1f} words/min).")
        L.append(f"- Preflight measured {job.get('silence_pct')}% of the file as "
                 f"silence (mean {job.get('mean_db')} dB, max {job.get('max_db')} dB).")
        zp = job.get("zeros_proof") or {}
        if zp.get("max_db") is not None and zp["max_db"] < -80:
            L.append(f"- Amplifying +40 dB left max volume at {zp['max_db']} dB, so "
                     "those samples are true zeros. Nothing is recoverable there.")
        L.append("- This points at the recorder losing its audio device, not at "
                 "the transcription.")
        L.append("")

    if low_pct > 10:
        L.append(f"**{len(low)} of {len(confs)} words ({low_pct:.1f}%) scored below "
                 "0.50 confidence.** Treat flagged turns as unreliable; in "
                 "code-switched speech the English terms are what degrade first.")
        L.append("")

    L.append("## Transcript")
    L.append("")
    for t in turns:
        avg = sum(t["confs"]) / len(t["confs"])
        flag = "  `LOW CONFIDENCE`" if avg < 0.60 else ""
        L.append(f"**[{mmss(t['start'])} - {mmss(t['end'])}] {t['spk']}** "
                 f"(conf {avg:.2f}){flag}")
        L.append("")
        L.append(" ".join(t["words"]))
        L.append("")

    if n_speakers and speech_span < 300:
        L.append("## On the speaker labels")
        L.append("")
        L.append(f"Diarization split {speech_span:,.0f}s of audio into {n_speakers} "
                 "speakers. On a span that short the labels are not trustworthy - "
                 "read the content, not the `spk_N` tags.")
        L.append("")

    out = state_dir() / f"{args.job_id}-transcript.md"
    out.write_text("\n".join(L), encoding="utf-8")

    job.update({"transcript_md": str(out), "words": len(words),
                "turns": len(turns), "speakers": n_speakers,
                "low_conf_pct": round(low_pct, 1),
                "speech_span_sec": round(speech_span, 1),
                "stage": "rendered", "updated_at": now_iso()})
    upsert_job(st, job)
    save_state(st)

    if not load_config().get("keep_wav"):
        w = Path(job.get("wav", ""))
        if w.exists():
            w.unlink()
            print(f"  removed local WAV ({job.get('wav_mb')} MB)")

    print(f"written: {out}")
    print(f"words {len(words)} | turns {len(turns)} | "
          f"low-confidence {low_pct:.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(prog="mt", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="measure the file and the credential")
    p.add_argument("video")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("preflight", help="silence + cost gate; spends nothing")
    p.add_argument("video")
    p.add_argument("--job-id")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("extract", help="16 kHz mono WAV")
    p.add_argument("video")
    p.add_argument("--job-id")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("submit", help="bucket + upload + start job (BILLABLE)")
    p.add_argument("job_id")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("poll", help="status; downloads JSON when done")
    p.add_argument("job_id", nargs="?")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("render", help="transcript JSON -> markdown")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_render)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
