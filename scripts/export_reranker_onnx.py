"""Export bge-reranker-v2-m3 to ONNX FP16 (Gate 3, ADR-02) and check it against PyTorch.

Steps:
  1. Export the PyTorch model to ONNX (FP32), dynamic batch and sequence axes.
  2. Convert the graph to FP16 (weights and activations).
  3. Compare FP16 ONNX scores with PyTorch scores on sample (query, passage) pairs.
  4. Time both on the same pairs.

Writes reports/retrieval/reranker_onnx_check.json. The FP32 intermediate is deleted
afterwards to save disk space (about 2.3 GB).

These are measurements on this machine (CPU). They are not KPI-09 sign-off.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import torch
from onnxconverter_common import float16
from transformers import AutoModelForSequenceClassification, AutoTokenizer

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = Path(os.environ.get("MODELS_DIR", ROOT / "models")) / "bge-reranker-v2-m3"
ONNX_DIR = MODEL_DIR / "onnx"
FP32_PATH = ONNX_DIR / "model_fp32.onnx"
FP16_PATH = ONNX_DIR / "model_fp16.onnx"
REPORT = ROOT / "reports" / "retrieval" / "reranker_onnx_check.json"
MAX_LEN = 512


def export_fp32(tokenizer, model) -> None:
    ONNX_DIR.mkdir(parents=True, exist_ok=True)
    enc = tokenizer(["what is the return window?"], ["Returns are accepted within 30 days."],
                    return_tensors="pt", padding="max_length", max_length=64, truncation=True)
    torch.onnx.export(
        model,
        (enc["input_ids"], enc["attention_mask"]),
        str(FP32_PATH),
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "seq"},
            "attention_mask": {0: "batch", 1: "seq"},
            "logits": {0: "batch"},
        },
        opset_version=17,
        dynamo=False,
    )


def convert_fp16() -> None:
    # The graph is larger than 2 GB, so shape inference cannot serialize it in memory.
    # Skip it, and save the result with external weights.
    model = onnx.load(str(FP32_PATH))
    model16 = float16.convert_float_to_float16(model, keep_io_types=True, disable_shape_infer=True)
    onnx.save(
        model16, str(FP16_PATH),
        save_as_external_data=True, all_tensors_to_one_file=True,
        location=FP16_PATH.name + "_data", size_threshold=1024,
    )


def ort_scores(path: Path, enc: dict) -> np.ndarray:
    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    out = sess.run(None, {
        "input_ids": enc["input_ids"].numpy().astype(np.int64),
        "attention_mask": enc["attention_mask"].numpy().astype(np.int64),
    })[0]
    return 1.0 / (1.0 + np.exp(-out.reshape(-1).astype(np.float64)))


def main() -> None:
    torch.set_num_threads(4)
    tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    model = AutoModelForSequenceClassification.from_pretrained(str(MODEL_DIR), torch_dtype=torch.float32)
    model.eval()

    # Remove files left by an earlier failed export (this folder is created by this script).
    if ONNX_DIR.exists():
        shutil.rmtree(ONNX_DIR)
    export_fp32(tokenizer, model)
    fp32_mb = sum(f.stat().st_size for f in ONNX_DIR.iterdir()) / 1e6
    convert_fp16()
    # Free disk space: delete every FP32 export file. Keep only the FP16 graph and its data.
    keep = {FP16_PATH.name, FP16_PATH.name + "_data"}
    for f in ONNX_DIR.iterdir():
        if f.name not in keep:
            f.unlink()
    fp16_mb = sum(f.stat().st_size for f in ONNX_DIR.iterdir()) / 1e6

    queries = [
        "What is the return window for SKU-001?",
        "What is the storage temperature range?",
        "Which lots were recalled?",
    ]
    passages = [
        "Returns are accepted within 30 days for SKU-001 Ceramic Mug.",
        "Carrier SLA covers freight delivery windows for pallets.",
        "LOT-00044 was recalled after a seal failure.",
        "Quarantined lots cannot be shipped until a supervisor record is filed.",
    ]
    pairs_q = [q for q in queries for _ in passages]
    pairs_p = [p for _ in queries for p in passages]
    enc = tokenizer(pairs_q, pairs_p, return_tensors="pt", padding=True,
                    truncation=True, max_length=MAX_LEN)

    with torch.inference_mode():
        t = time.perf_counter()
        torch_logits = model(**enc).logits.view(-1)
        torch_ms = (time.perf_counter() - t) * 1e3
    torch_scores = torch.sigmoid(torch_logits).numpy().astype(np.float64)

    t = time.perf_counter()
    fp16_scores = ort_scores(FP16_PATH, enc)
    fp16_ms = (time.perf_counter() - t) * 1e3

    diff = np.abs(fp16_scores - torch_scores)
    rank_torch = np.argsort(-torch_scores)
    rank_fp16 = np.argsort(-fp16_scores)

    report = {
        "model": "BAAI/bge-reranker-v2-m3",
        "export": "torch.onnx (opset 17, legacy exporter), then FP16 weights; I/O types kept FP32",
        "fp16_model_mb": round(fp16_mb, 1),
        "fp32_model_mb_before_conversion": round(fp32_mb, 1),
        "pairs_checked": int(len(torch_scores)),
        "max_abs_score_diff": round(float(diff.max()), 6),
        "mean_abs_score_diff": round(float(diff.mean()), 6),
        "top1_order_agrees": bool(rank_torch[0] == rank_fp16[0]),
        "threshold_0_42_decisions_agree": bool(((torch_scores >= 0.42) == (fp16_scores >= 0.42)).all()),
        "pairs_ms_pytorch_cpu": round(torch_ms, 1),
        "pairs_ms_onnx_fp16_cpu": round(fp16_ms, 1),
        "note": "CPU, shared machine, one batch of 12 pairs. Not KPI-09 sign-off.",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
