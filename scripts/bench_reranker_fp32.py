"""Benchmark the bge-reranker-v2-m3 FP32 ONNX graph against PyTorch and the FP16 graph.

The FP32 graph is exported to a temporary folder, timed, and deleted afterwards
(about 2.3 GB; the root disk has little free space). The FP16 graph is kept.

Timing: one query against all knowledge_vault notes (about 38 passages, close to
the 40-candidate pool in the eval), median of 5 runs after 1 warm-up run.
Accuracy: FP32 ONNX vs PyTorch scores, max absolute difference.

Writes reports/retrieval/reranker_fp32_bench.json. CPU, shared machine. Not KPI-09 sign-off.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = Path(os.environ.get("MODELS_DIR", ROOT / "models")) / "bge-reranker-v2-m3"
TMP = MODEL_DIR / "onnx_fp32_tmp"
FP16 = MODEL_DIR / "onnx" / "model_fp16.onnx"
REPORT = ROOT / "reports" / "retrieval" / "reranker_fp32_bench.json"
QUERY = "What is the return window for SKU-001?"


def passages() -> list[str]:
    out = []
    for p in sorted((ROOT / "knowledge_vault").glob("*.md")):
        out.append(p.read_text(encoding="utf-8").split("---", 2)[-1].strip()[:2000])
    return out


def timed(fn, runs: int = 5) -> tuple[float, object]:
    fn()  # warm-up
    times, result = [], None
    for _ in range(runs):
        t = time.perf_counter()
        result = fn()
        times.append((time.perf_counter() - t) * 1e3)
    return round(statistics.median(times), 1), result


def main() -> None:
    torch.set_num_threads(4)
    tok = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    model = AutoModelForSequenceClassification.from_pretrained(str(MODEL_DIR), torch_dtype=torch.float32).eval()
    docs = passages()
    enc = tok([QUERY] * len(docs), docs, return_tensors="pt", padding=True, truncation=True, max_length=512)

    @torch.inference_mode()
    def run_torch():
        return torch.sigmoid(model(**enc).logits.view(-1)).numpy().astype(np.float64)

    torch_ms, torch_scores = timed(run_torch)

    # FP32 export to a temporary folder
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    fp32_path = TMP / "model_fp32.onnx"
    dummy = tok([QUERY], [docs[0]], return_tensors="pt", padding="max_length", max_length=64, truncation=True)
    torch.onnx.export(
        model, (dummy["input_ids"], dummy["attention_mask"]), str(fp32_path),
        input_names=["input_ids", "attention_mask"], output_names=["logits"],
        dynamic_axes={"input_ids": {0: "batch", 1: "seq"}, "attention_mask": {0: "batch", 1: "seq"},
                      "logits": {0: "batch"}},
        opset_version=17, dynamo=False,
    )

    def ort_run(path: Path):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        sess = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        ids = enc["input_ids"].numpy().astype(np.int64)
        mask = enc["attention_mask"].numpy().astype(np.int64)

        def run():
            logits = sess.run(None, {"input_ids": ids, "attention_mask": mask})[0]
            return (1.0 / (1.0 + np.exp(-logits.reshape(-1).astype(np.float64))))
        return run

    fp32_ms, fp32_scores = timed(ort_run(fp32_path))
    fp16_ms, fp16_scores = timed(ort_run(FP16))

    fp32_diff = float(np.abs(fp32_scores - torch_scores).max())
    fp16_diff = float(np.abs(fp16_scores - torch_scores).max())
    shutil.rmtree(TMP)  # free disk space

    report = {
        "passages": len(docs),
        "timing_median_ms_per_batch": {
            "pytorch_fp32": torch_ms,
            "onnx_fp32": fp32_ms,
            "onnx_fp16": fp16_ms,
        },
        "max_abs_score_diff_vs_pytorch": {
            "onnx_fp32": round(fp32_diff, 7),
            "onnx_fp16": round(fp16_diff, 7),
        },
        "method": "CPU, 4 threads, shared machine, median of 5 runs after 1 warm-up. The FP32 "
                  "graph was exported to a temp folder and deleted afterwards.",
        "note": "Not KPI-09 sign-off. KPI-09 is a p95 latency per query on the target hardware.",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
