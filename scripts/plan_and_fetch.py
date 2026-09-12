"""Runs once per job, as generate.yml's `plan` job.

Reads the repository_dispatch payload straight from $GITHUB_EVENT_PATH
(rather than piping user-controlled script_text through workflow YAML
inputs/shell args -- avoids quoting/escaping issues with long, multi-line,
Bengali/English narration text), fetches that channel's reference voice
profile from the private yt-core repo, decides how many shards to use, and
writes:

  manifest.json         -- job_id, channel_id, total_shards, per-shard text
  profile/reference.wav
  profile/reference.json

...which the `shard` and `collect` jobs then pick up as artifacts. Also
writes two GITHUB_OUTPUT values (job_id, shards) so generate.yml's matrix
strategy can fan out.
"""
from __future__ import annotations
import json
import os
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


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    payload = event["client_payload"]
    job_id = str(payload["job_id"])
    channel_id = str(payload["channel_id"])
    script_text = str(payload["script_text"])

    yt_core_repo = os.environ["YT_CORE_REPO"]
    yt_core_token = os.environ["YT_CORE_READONLY_PAT"]

    profile_dir = Path("profile")
    profile_dir.mkdir(parents=True, exist_ok=True)
    ref_wav = raw_fetch(yt_core_repo, f"data/voice_profiles/{channel_id}/reference.wav", token=yt_core_token)
    ref_json = raw_fetch(yt_core_repo, f"data/voice_profiles/{channel_id}/reference.json", token=yt_core_token)
    if ref_wav is None or ref_json is None:
        print(f"::error::No voice profile found for channel '{channel_id}' in {yt_core_repo} "
              f"(expected data/voice_profiles/{channel_id}/reference.wav + reference.json).")
        sys.exit(1)
    (profile_dir / "reference.wav").write_bytes(ref_wav)
    (profile_dir / "reference.json").write_bytes(ref_json)
    reference_meta = json.loads(ref_json.decode("utf-8"))

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
