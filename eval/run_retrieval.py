"""Catalogue search alone, no model: how often is the right product in the top 1 / 3 / 5?

    uv run python -m eval.run_retrieval

Queries are the ground-truth lines exactly as customers wrote them (typos, German,
Dutch, shorthand), for the lines whose product we stock. Compares the four modes of
app/catalogue/index.py. Free to run: everything is local.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.catalogue.index import get_catalogue

ROOT = Path(__file__).resolve().parent
MODES = ("keyword_raw", "keyword", "vector", "hybrid")


def main() -> None:
    truth = json.loads((ROOT / "testset" / "ground_truth.json").read_text(encoding="utf-8"))["cases"]
    queries = [(c["id"], line["text"], line["sku"]) for c in truth for line in c["lines"] if line["sku"]]
    catalogue = get_catalogue()
    catalogue.warm()
    results = {}
    misses: dict[str, list[str]] = {}
    for mode in MODES:
        ranks = []
        for cid, text, sku in queries:
            hits = [h.product.sku for h in catalogue.search(text, k=5, mode=mode)]
            rank = hits.index(sku) + 1 if sku in hits else None
            ranks.append(rank)
            if mode == "hybrid" and (rank is None or rank > 3):
                misses.setdefault(mode, []).append(f"{cid} '{text}' -> {catalogue.get(sku).name} (rank {rank})")
        n = len(ranks)
        results[mode] = {k: sum(1 for r in ranks if r is not None and r <= k) / n for k in (1, 3, 5)}
        results[mode]["n"] = n
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    (out / "retrieval.json").write_text(json.dumps({"results": results, "hybrid_misses_top3": misses.get("hybrid", [])},
                                                   indent=1), encoding="utf-8")
    print(f"{len(queries)} queries (lines whose product is in the catalogue)\n")
    print(f"{'mode':12} {'top-1':>7} {'top-3':>7} {'top-5':>7}")
    for mode, r in results.items():
        print(f"{mode:12} {r[1]:7.1%} {r[3]:7.1%} {r[5]:7.1%}")
    for m in misses.get("hybrid", []):
        print("  hybrid miss:", m)


if __name__ == "__main__":
    main()
