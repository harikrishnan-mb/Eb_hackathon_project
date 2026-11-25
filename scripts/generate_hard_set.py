"""Generate a HARDER synthetic retrieval set (synthetic, labeled, deterministic).

Why: the base golden set names exact SKUs and uses template wording, so BM25
nearly saturates. This set removes that shortcut.

What it adds, in data/synthetic/hard/ (the base corpus is not changed):
  corpus/*.md           base notes + 60 "Mini" lookalike product notes whose names
                        share words with real products (distractors)
  golden_hard.jsonl     200 questions written as paraphrases that name the product,
                        not the SKU. Half target base products, half target the
                        lookalike Mini products, so the right answer depends on
                        the exact name. All gold labels are exact.

Deterministic for a fixed seed. No LLM is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_CORPUS = ROOT / "knowledge_vault"
OUT = ROOT / "data" / "synthetic" / "hard"

# Reuse the base generator's product catalog so names and values match exactly.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("gen", ROOT / "scripts" / "generate_synthetic_dataset.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _lookalike_note(rng: random.Random, i: int, base_name: str) -> tuple[str, dict]:
    sku = f"SKU-{i:03d}"
    fact = {
        "sku": sku,
        "name": f"{base_name} Mini",
        "return_days": rng.choice([7, 10, 21]),
        "storage_c": rng.choice(["5 to 15", "0 to 4", "20 to 30"]),
        "warranty_months": rng.choice([3, 9]),
        "weight_g": rng.randrange(40, 900, 5),
    }
    note_id = f"note_{sku.lower().replace('-', '_')}"
    body = (
        f"# {sku} {fact['name']}\n\n"
        f"- Returns are accepted within {fact['return_days']} days of delivery.\n"
        f"- Storage temperature: {fact['storage_c']} degrees Celsius.\n"
        f"- Warranty: {fact['warranty_months']} months.\n"
        f"- Net weight: {fact['weight_g']} grams.\n"
    )
    return note_id, {"body": body, "fact": fact}


PARA_RETURN = [
    "How long after delivery can a customer send back the {name}?",
    "What is the return period for the {name}?",
    "Within how many days of receiving the {name} can I return it?",
]
PARA_STORAGE = [
    "What temperature should I keep the {name} stored at?",
    "Which storage temperature range applies to the {name}?",
]
PARA_WARRANTY = [
    "How long does the warranty on the {name} last?",
    "What warranty period comes with the {name}?",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20261119)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    corpus_dir = OUT / "corpus"
    if corpus_dir.exists():
        shutil.rmtree(corpus_dir)
    corpus_dir.mkdir(parents=True)
    for p in BASE_CORPUS.glob("*.md"):
        shutil.copy(p, corpus_dir / p.name)

    base_products = list(gen.PRODUCT_NAMES[:gen.CATALOG_SIZE])
    # Lookalike notes start at SKU-031 so they never collide with base SKUs.
    lookalikes = []
    for n in range(60):
        base_name = base_products[n % len(base_products)]
        sku_num = 31 + n
        note_id, data = _lookalike_note(rng, sku_num, base_name)
        (corpus_dir / f"{note_id}.md").write_text(
            "---\n"
            f'id: "{note_id}"\ndomain: "catalog"\nrisk_tier: "TIER_1"\nversion: "1.0"\nsynthetic: true\n'
            "---\n\n" + data["body"], encoding="utf-8")
        lookalikes.append((note_id, data["fact"]))

    # Base product facts, regenerated with the same seed logic as the base generator.
    base_rng = random.Random(20261118)
    base_facts = {}
    for i in range(1, gen.CATALOG_SIZE + 1):
        f = gen._product_facts(base_rng, i)
        base_facts[f"note_{f['sku'].lower().replace('-', '_')}"] = f

    rows: list[dict] = []
    # Base-name paraphrases (target base notes).
    for i in range(100):
        note_id = list(base_facts)[i % len(base_facts)]
        f = base_facts[note_id]
        kind = i % 3
        # "standard" disambiguates from the Mini lookalike sharing the same name.
        std = f"standard {f['name']}"
        if kind == 0:
            q, ans = rng.choice(PARA_RETURN).format(name=std), f"{f['return_days']} days"
        elif kind == 1:
            q, ans = rng.choice(PARA_STORAGE).format(name=std), f"{f['storage_c']} degrees Celsius"
        else:
            q, ans = rng.choice(PARA_WARRANTY).format(name=std), f"{f['warranty_months']} months"
        rows.append({"id": f"hard-base-{i + 1:03d}", "type": "paraphrase_base", "question": q,
                     "gold_note_ids": [note_id], "gold_answer": ans, "answerable": True,
                     "synthetic": True})
    # Lookalike paraphrases (target Mini notes; the name ends in "Mini").
    for i in range(100):
        note_id, f = lookalikes[i % len(lookalikes)]
        kind = i % 3
        mini = f["name"]
        if kind == 0:
            q, ans = rng.choice(PARA_RETURN).format(name=mini), f"{f['return_days']} days"
        elif kind == 1:
            q, ans = rng.choice(PARA_STORAGE).format(name=mini), f"{f['storage_c']} degrees Celsius"
        else:
            q, ans = rng.choice(PARA_WARRANTY).format(name=mini), f"{f['warranty_months']} months"
        rows.append({"id": f"hard-mini-{i + 1:03d}", "type": "paraphrase_lookalike", "question": q,
                     "gold_note_ids": [note_id], "gold_answer": ans, "answerable": True,
                     "synthetic": True})
    rng.shuffle(rows)
    assert len(rows) == 200

    OUT.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n"
    (OUT / "golden_hard.jsonl").write_text(text, encoding="utf-8")
    manifest = {
        "generator": "generate_hard_set.py", "seed": args.seed, "synthetic": True,
        "corpus_notes": len(list(corpus_dir.glob("*.md"))),
        "lookalike_notes": len(lookalikes),
        "questions": len(rows),
        "sha256_golden_hard": hashlib.sha256(text.encode()).hexdigest(),
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
