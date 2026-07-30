from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_MODEL_ID = "Helsinki-NLP/opus-mt-en-jap"


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Download and verify the optional local Marian translation model.")
    value.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    value.add_argument("--cache-dir")
    value.add_argument("--local-files-only", action="store_true")
    value.add_argument("--device-check", action="store_true")
    return value


def run(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    except Exception:
        print("translation dependencies are missing; install with python -m pip install -r requirements-translation.txt", file=sys.stderr)
        return 2
    if args.device_check:
        print(f"torch={torch.__version__} cuda_available={torch.cuda.is_available()}")
    kwargs = {"local_files_only": args.local_files_only}
    if args.cache_dir:
        kwargs["cache_dir"] = args.cache_dir
    try:
        tokenizer = AutoTokenizer.from_pretrained(args.model_id, **kwargs)
        model = AutoModelForSeq2SeqLM.from_pretrained(args.model_id, **kwargs)
        model.eval()
        verify_kwargs = {"local_files_only": True}
        if args.cache_dir:
            verify_kwargs["cache_dir"] = args.cache_dir
        AutoTokenizer.from_pretrained(args.model_id, **verify_kwargs)
        verified_model = AutoModelForSeq2SeqLM.from_pretrained(args.model_id, **verify_kwargs)
        verified_model.eval()
    except Exception as exc:
        print(f"model download or verification failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    revision = getattr(model.config, "_commit_hash", None) or getattr(tokenizer, "init_kwargs", {}).get("revision", "unknown")
    cache_path = args.cache_dir or os.getenv("HF_HOME") or os.getenv("TRANSFORMERS_CACHE") or "Hugging Face default cache"
    print(f"model_id={args.model_id}")
    print(f"revision={revision}")
    print(f"cache_path={cache_path}")
    print(f"model_type={getattr(model.config, 'model_type', 'unknown')}")
    print("local_files_only_verification=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
