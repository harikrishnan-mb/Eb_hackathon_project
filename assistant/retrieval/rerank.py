"""Stage 3 cross-encoder: bge-reranker-v2-m3 (Gate 3).

Settings: batch 32, max_len 512, score = sigmoid(logit), abstention threshold 0.42.
Runs in PyTorch on CPU. The design specifies an ONNX FP16 export. That export is
not built yet, so this is the PyTorch baseline. Its latency is not a KPI-09 result.
"""

from __future__ import annotations

from pathlib import Path

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MAX_LEN = 512
BATCH = 32
ABSTAIN_THRESHOLD = 0.42


class OnnxCrossEncoder:
    """ONNX Runtime backend (FP16 graph from scripts/export_reranker_onnx.py).

    Same interface as CrossEncoder. Gate 3 specifies ONNX FP16, so this is the
    deployment path; the PyTorch class is the reference.
    """

    def __init__(self, model_dir: str | Path, onnx_file: str = "onnx/model_fp16.onnx") -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = Path(model_dir)
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=MAX_LEN)
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")  # XLM-R pad id
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        self.session = ort.InferenceSession(
            str(model_dir / onnx_file), sess_options=opts, providers=["CPUExecutionProvider"],
        )

    def score(self, query: str, passages: list[str]) -> list[float]:
        import numpy as np

        out: list[float] = []
        for i in range(0, len(passages), BATCH):
            batch = passages[i:i + BATCH]
            enc = self.tokenizer.encode_batch([(query, p) for p in batch])
            logits = self.session.run(None, {
                "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
            })[0].reshape(-1).astype(np.float64)
            out.extend((1.0 / (1.0 + np.exp(-logits))).tolist())
        return out


class CrossEncoder:
    def __init__(self, model_dir: str | Path) -> None:
        model_dir = str(model_dir)
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_dir)
        self.model.eval()
        torch.set_num_threads(4)

    @torch.inference_mode()
    def score(self, query: str, passages: list[str]) -> list[float]:
        out: list[float] = []
        for i in range(0, len(passages), BATCH):
            batch = passages[i:i + BATCH]
            enc = self.tokenizer(
                [query] * len(batch), batch,
                padding=True, truncation=True, max_length=MAX_LEN, return_tensors="pt",
            )
            logits = self.model(**enc).logits.view(-1)
            out.extend(torch.sigmoid(logits).tolist())
        return out
