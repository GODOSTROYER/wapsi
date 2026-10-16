"""Upload this folder to the static Hugging Face Space.

Usage (from the repo root, token in the environment, never in a file):
    set -a; source ../.secrets/hf.env; set +a
    uv run --with huggingface_hub python deploy/hf_static/publish.py [--repo GODOSTROYER/wapsi]
"""

import argparse
import os
from pathlib import Path

from huggingface_hub import HfApi

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="GODOSTROYER/wapsi")
    args = ap.parse_args()
    if not (HERE / "assets" / "wapsi_demo.mp4").exists():
        raise SystemExit("assets/wapsi_demo.mp4 missing: build it with ffmpeg first (see README.md)")
    api = HfApi(token=os.environ["HF_TOKEN"])
    api.create_repo(args.repo, repo_type="space", space_sdk="static", exist_ok=True)
    info = api.upload_folder(
        repo_id=args.repo,
        repo_type="space",
        folder_path=str(HERE),
        ignore_patterns=["publish.py", ".gitignore", "__pycache__/*"],
        commit_message="Update Wapsi showcase",
    )
    print(info)
    print("https://" + args.repo.lower().replace("/", "-") + ".static.hf.space")
    print("https://huggingface.co/spaces/" + args.repo)


if __name__ == "__main__":
    main()
