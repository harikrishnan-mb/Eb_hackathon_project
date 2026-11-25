"""Stage 2 dense retrieval: bge-large-en-v1.5 (ONNX) + FAISS HNSW.

Settings from Gate 3 and the design:
  chunk size 512 tokens, overlap 64 tokens
  encoder: bge-large-en-v1.5, CLS pooling, L2 normalization (1_Pooling + 2_Normalize)
  index: FAISS HNSW (M = 32), efSearch = 64, inner product on normalized vectors (cosine)
  top_k = 50

Query instruction: bge-v1.5 recommends this prefix for short queries. It is used
here. It is a documented choice, not a Blueprint value.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

CHUNK_TOKENS = 512
CHUNK_OVERLAP = 64
EF_SEARCH = 64
HNSW_M = 32
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    text: str


def chunk_document(doc_id: str, text: str, tokenizer: Tokenizer) -> list[Chunk]:
    """Split by tokens into windows of CHUNK_TOKENS with CHUNK_OVERLAP overlap.

    Each window is decoded back to text. Short documents become one chunk.
    """
    ids = tokenizer.encode(text, add_special_tokens=False).ids
    if len(ids) <= CHUNK_TOKENS:
        return [Chunk(chunk_id=f"{doc_id}-0", doc_id=doc_id, text=text.strip())]
    step = CHUNK_TOKENS - CHUNK_OVERLAP
    chunks = []
    for n, start in enumerate(range(0, len(ids), step)):
        window = ids[start:start + CHUNK_TOKENS]
        chunks.append(Chunk(
            chunk_id=f"{doc_id}-{n}",
            doc_id=doc_id,
            text=tokenizer.decode(window),
        ))
        if start + CHUNK_TOKENS >= len(ids):
            break
    return chunks


class BGEEncoder:
    def __init__(self, model_dir: str | Path, max_length: int = CHUNK_TOKENS) -> None:
        model_dir = Path(model_dir)
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=max_length)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        self.session = ort.InferenceSession(
            str(model_dir / "onnx" / "model.onnx"),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self._input_names = {i.name for i in self.session.get_inputs()}

    def encode(self, texts: list[str], batch_size: int = 16) -> np.ndarray:
        out: list[np.ndarray] = []
        for i in range(0, len(texts), batch_size):
            enc = self.tokenizer.encode_batch(texts[i:i + batch_size])
            feeds = {
                "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
            }
            if "token_type_ids" in self._input_names:
                feeds["token_type_ids"] = np.zeros_like(feeds["input_ids"])
            hidden = self.session.run(None, feeds)[0]  # (batch, seq, dim)
            cls = hidden[:, 0, :]                        # CLS pooling
            norm = np.linalg.norm(cls, axis=1, keepdims=True)
            out.append(cls / np.maximum(norm, 1e-12))
        return np.vstack(out).astype(np.float32) if out else np.zeros((0, 1024), np.float32)


@dataclass(frozen=True)
class DenseHit:
    chunk_id: str
    doc_id: str
    cosine: float
    rank: int


class DenseIndex:
    def __init__(self, encoder: BGEEncoder, chunks: list[Chunk]) -> None:
        self.encoder = encoder
        self.chunks = chunks
        dim = encoder.encode(["probe"]).shape[1]
        self.index = faiss.IndexHNSWFlat(dim, HNSW_M, faiss.METRIC_INNER_PRODUCT)
        self.index.hnsw.efSearch = EF_SEARCH
        vectors = encoder.encode([c.text for c in chunks])
        self.index.add(vectors)

    def search(self, query: str, top_k: int = 50) -> list[DenseHit]:
        q = self.encoder.encode([QUERY_INSTRUCTION + query])
        k = min(top_k, len(self.chunks))
        scores, ids = self.index.search(q, k)
        hits = []
        for rank, (score, idx) in enumerate(zip(scores[0], ids[0]), start=1):
            if idx < 0:
                continue
            c = self.chunks[int(idx)]
            hits.append(DenseHit(chunk_id=c.chunk_id, doc_id=c.doc_id, cosine=float(score), rank=rank))
        return hits
