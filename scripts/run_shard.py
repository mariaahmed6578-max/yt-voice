"""Runs once per matrix entry (one shard). Synthesizes this shard's slice of
text against the shared reference voice, with its own retry/backoff -- a
transient hiccup on one shard shouldn't force recomputing the other 39.
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def synthesize(ref_audio: Path, ref_text: str, gen_text: str, out_dir: Path, out_name: str) -> None:
    """Shells out to the documented `f5-tts_infer-cli` entry point rather
    than importing f5_tts's internal Python classes directly -- the
    internal API (the F5TTS class's constructor signature, module layout)
    has changed across versions, while these CLI flags
    (-r/-s/-t/-f/-o/-w, confirmed against the upstream infer_cli.py source)
    have stayed the stable, documented surface.

    NOTE: re-verify with `f5-tts_infer-cli --help` after any version bump
    pinned in requirements.txt -- this was written and confirmed against
    the plain flag layout in SWivid/F5-TTS's infer_cli.py, but this
    workflow can't be end-to-end tested without a GPU/model-weights
    environment, so treat the FIRST canary run as the real test of this
    exact command line.
    """
    gen_text_file = out_dir / f"{out_name}.gen.txt"
    gen_text_file.write_text(gen_text, encoding="utf-8")
    cmd = [
        "f5-tts_infer-cli",
        "-r", str(ref_audio),
        "-s", ref_text,
        "-f", str(gen_text_file),
        "-o", str(out_dir),
        "-w", f"{out_name}.wav",
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=900)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    shard = next(s for s in manifest["shards"] if s["index"] == args.shard_index)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not shard["text"].strip():
        # split_balanced() can hand back an empty pad chunk if the script
        # had fewer sentences than requested shards -- nothing to
        # synthesize; write a marker so collect_and_stitch.py can skip it
        # without treating it as a failure.
        (out_dir / "EMPTY").write_text("", encoding="utf-8")
        return

    ref_audio = Path(args.profile_dir) / "reference.wav"
    ref_meta = json.loads((Path(args.profile_dir) / "reference.json").read_text(encoding="utf-8"))
    ref_text = ref_meta.get("transcript", "")
    out_name = f"shard_{args.shard_index:03d}"

    last_exc: Exception | None = None
    for attempt in range(1, args.max_attempts + 1):
        try:
            synthesize(ref_audio, ref_text, shard["text"], out_dir, out_name)
            if not (out_dir / f"{out_name}.wav").exists():
                raise RuntimeError("f5-tts_infer-cli exited 0 but produced no output file")
            print(f"Shard {args.shard_index}: done on attempt {attempt}.")
            return
        except Exception as exc:  # noqa: BLE001 -- any failure here should retry, not just specific types
            last_exc = exc
            print(f"Shard {args.shard_index} attempt {attempt}/{args.max_attempts} failed: {exc}")
            if attempt < args.max_attempts:
                time.sleep(min(60, 5 * 2 ** attempt))
    print(f"::error::Shard {args.shard_index} failed after {args.max_attempts} attempts: {last_exc}")
    sys.exit(1)


if __name__ == "__main__":
    main()
