"""Download pinned local speech models, with resume and hash verification."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lubo.quality_models import MODELS, download_model

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=["asr", "aligner"])
    args = parser.parse_args()
    for key in args.models:
        download_model(key, args.root)
