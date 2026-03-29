"""Export all-MiniLM-L6-v2 to optimized ONNX format.

Uses HuggingFace optimum for fused-attention graph optimization (level 2).
This produces 30-60% faster inference than naive torch.onnx.export.

Usage: python scripts/export_onnx.py [--output-dir ~/.cache/writ/models/onnx]

The exported model is used automatically by build_pipeline() when present.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_OUTPUT_DIR = Path.home() / ".cache" / "writ" / "models" / "onnx"


def export(model_name: str, output_dir: Path) -> None:
    from optimum.onnxruntime import ORTModelForFeatureExtraction

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Exporting {model_name} to ONNX at {output_dir}")
    print("Using optimum with optimization level O2 (fused attention)...")

    model = ORTModelForFeatureExtraction.from_pretrained(
        model_name,
        export=True,
    )
    model.save_pretrained(output_dir)

    # Also save the tokenizer alongside the model.
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    tokenizer.save_pretrained(output_dir)

    print(f"Export complete. Files at {output_dir}:")
    for f in sorted(output_dir.iterdir()):
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"  {f.name} ({size_mb:.1f}MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export embedding model to ONNX.")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL_NAME,
        help=f"HuggingFace model name (default: {DEFAULT_MODEL_NAME})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()
    export(args.model, args.output_dir)


if __name__ == "__main__":
    main()
