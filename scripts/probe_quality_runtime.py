"""One-shot native-runtime capability probe; run as a packaged entrypoint."""
import json
import os
from pathlib import Path
import sys
import traceback

destination = Path(os.environ["LUBO_QUALITY_PROBE_RESULT"])
sys.stdout = sys.stderr = open(destination.with_suffix(".log"), "w", encoding="utf-8", buffering=1)
try:
    import torch
    import transformers
    from transformers import Qwen3ASRForConditionalGeneration, Qwen3ASRForTokenClassification, Qwen3ASRProcessor

    report = {"ok": True, "torch": torch.__version__, "transformers": transformers.__version__,
              "asr": Qwen3ASRForConditionalGeneration.__name__,
              "aligner": Qwen3ASRForTokenClassification.__name__,
              "processor": Qwen3ASRProcessor.__name__, "cuda": torch.cuda.is_available()}
except BaseException:
    report = {"ok": False, "error": traceback.format_exc()}
destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
os._exit(0 if report["ok"] else 1)
