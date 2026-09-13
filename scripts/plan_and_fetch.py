"""Runs once per job, as generate.yml's `plan` job.

Reads the repository_dispatch payload straight from $GITHUB_EVENT_PATH
(rather than piping user-controlled script_text through workflow YAML
inputs/shell args -- avoids quoting/escaping issues with long, multi-line,
Bengali/English narration text), fetches that channel's reference voice
profile from the private yt-core repo, decides how many shards to use, and
writes:

  manifest.json         -- job_id, channel_id, total_shards, per-shard text
  profile/reference.wav -- always a clean, real WAV, regardless of what
                            format the channel's profile was uploaded in
  profile/reference.json

...which the `shard` and `collect` jobs then pick up as artifacts. Also
writes two GITHUB_OUTPUT values (job_id, shards) so generate.yml's matrix
strategy can fan out.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

from common import raw_fetch, split_balanced

# Student Pack concurrency ceiling is 40 (see repo README) -- a single job
# is capped well under that so it never ALONE exhausts the whole account's
# pool. A second job arriving mid-run still gets native GitHub Actions
# queuing (see README "Concurrency" section) instead of starving.
MAX_SHARDS_PER_JOB = int(os.environ.get("MAX_SHARDS_PER_JOB", "20"))
TARGET_SECONDS_PER_SHARD = float(os.environ.get("TARGET_SECONDS_PER_SHARD", "45"))
# Rough narration rate, only used to size shards -- doesn't need to be
# exact, just consistent enough that shards come out similarly sized.
WORDS_PER_SECOND = 2.5


def estimate_seconds(text: str) -> float:
    words = len(text.split())
    return words / WORDS_PER_SECOND if words else 1.0


def fetch_and_normalize_reference(yt_core_repo: str, yt_core_token: str, channel_id: str, profile_dir: Path) -> dict:
    """reference.json is fetched FIRST because it names the actual audio
    file (see "audio_file" below) -- phone recordings are almost never
    real .wav (usually .m4a/.aac/.3gp from the Dashboard's upload page),
    so this never assumes a fixed filename or format. Whatever comes back
    is re-encoded through ffmpeg into a clean, known-good reference.wav --
    that sidesteps ever needing to know whether F5-TTS's own audio loader
    can read the original phone format directly.
    """
    ref_json = raw_fetch(yt_core_repo, f"data/voice_profiles/{channel_id}/reference.json", token=yt_core_token)
    if ref_json is None:
        print(f"::error::No voice profile found for channel '{channel_id}' in {yt_core_repo} "
              f"(expected data/voice_profiles/{channel_id}/reference.json).")
        sys.exit(1)
    reference_meta = json.loads(ref_json.decode("utf-8"))
    # "audio_file" is set by the Dashboard's upload route; default here
    # only covers a profile placed by hand before that field existed.
    audio_file = reference_meta.get("audio_file", "reference.wav")

    raw_audio = raw_fetch(yt_core_repo, f"data/voice_profiles/{channel_id}/{audio_file}", token=yt_core_token)
    if raw_audio is None:
        print(f"::error::reference.json for '{channel_id}' points at '{audio_file}' but that file is missing.")
        sys.exit(1)

    profile_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(audio_file).suffix or ".bin"
    original_path = profile_dir / f"original{suffix}"
    original_path.write_bytes(raw_audio)
    (profile_dir / "reference.json").write_bytes(ref_json)

    ref_wav = profile_dir / "reference.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(original_path), "-ar", "24000", "-ac", "1", str(ref_wav)],
        check=True, capture_output=True, text=True,
    )
    original_path.unlink()
    return reference_meta


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    payload = event["client_payload"]
    job_id = str(payload["job_id"])
    channel_id = str(payload["channel_id"])
    script_text = str(payload["script_text"])

    yt_core_repo = os.environ["YT_CORE_REPO"]
    yt_core_token = os.environ["YT_CORE_READONLY_PAT"]

    profile_dir = Path("profile")
    reference_meta = fetch_and_normalize_reference(yt_core_repo, yt_core_token, channel_id, profile_dir)

    seconds = estimate_seconds(script_text)
    shard_count = max(1, min(MAX_SHARDS_PER_JOB, round(seconds / TARGET_SECONDS_PER_SHARD) or 1))
    texts = split_balanced(script_text, shard_count)

    manifest = {
        "job_id": job_id,
        "channel_id": channel_id,
        "total_shards": shard_count,
        "reference_text": reference_meta.get("transcript", ""),
        "shards": [{"index": i, "text": t} for i, t in enumerate(texts)],
        "dispatched_at": payload.get("dispatched_at"),
    }
    Path("manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
        fh.write(f"job_id={job_id}\n")
        fh.write(f"shards={json.dumps(list(range(shard_count)))}\n")

    print(f"Planned {shard_count} shard(s) for job {job_id} (channel={channel_id}, ~{seconds:.0f}s estimated).")


if __name__ == "__main__":
    main()
