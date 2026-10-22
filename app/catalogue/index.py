"""The product catalogue and its search: keyword (BM25), semantic (embeddings), hybrid (RRF).

Same idea as DocuMind's retrieval, sized for a catalogue: a few hundred short product
names fit in memory, so BM25 is computed in Python and the embeddings are one numpy
matrix (cached in data/catalogue.db; rebuilt when the CSV or the model changes).
Postgres + pgvector would replace the matrix once the catalogue reaches ~100k SKUs.

Four search modes, compared in the evaluation (eval/run_retrieval.py):
  keyword_raw  BM25 on plain lower-case words (what a quick prototype does)
  keyword      BM25 on the trade-normalised tokens (app/catalogue/normalise.py)
  vector       cosine similarity of local embeddings (bge-small, ONNX, no GPU)
  hybrid       reciprocal rank fusion of keyword + vector
"""

from __future__ import annotations

import csv
import hashlib
import math
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from functools import cached_property
from pathlib import Path
from typing import Literal

import numpy as np

from app.catalogue.normalise import normalise, tokenize_raw
from app.config import FIXTURES, settings

CATEGORIES = {
    "fasteners": "Fasteners",
    "pipe_fittings": "Pipe & fittings",
    "valves": "Valves",
    "electrical": "Electrical",
    "ppe": "PPE & workwear",
    "tools_consumables": "Tools & consumables",
    "sealants_adhesives": "Sealants & adhesives",
}
SearchMode = Literal["keyword_raw", "keyword", "vector", "hybrid"]
RRF_K = 60


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: str
    unit: str  # how it is sold: "each", "box of 100", "m", "drum (100 m)", "pair"...
    pack_qty: float  # base units per sell unit (100 for a box of 100, 3 for a 3 m length)
    base_unit: str  # "each", "m", "pair" or "kg"
    cost_gbp: float  # per sell unit. Confidential: never leaves the building.
    stock: int  # in sell units
    lead_days: int
    supplier: str  # confidential too

    def public(self) -> dict:
        """What an agent or a customer may see: no cost, no supplier."""
        d = asdict(self)
        d.pop("cost_gbp")
        d.pop("supplier")
        return d


@dataclass
class Hit:
    product: Product
    score: float
    keyword_rank: int | None
    vector_rank: int | None
    matched_terms: list[str]

    def why(self) -> str:
        parts = []
        if self.keyword_rank is not None:
            parts.append(f"keyword #{self.keyword_rank}")
        if self.vector_rank is not None:
            parts.append(f"semantic #{self.vector_rank}")
        if self.matched_terms:
            parts.append("matched: " + " ".join(self.matched_terms))
        return "; ".join(parts) or "no overlap"

    def as_dict(self) -> dict:
        return {**self.product.public(), "score": round(self.score, 4), "why": self.why(),
                "keyword_rank": self.keyword_rank, "vector_rank": self.vector_rank}


def load_products(path: Path = FIXTURES / "catalogue.csv") -> list[Product]:
    with path.open(encoding="utf-8") as fh:
        return [
            Product(sku=r["sku"], name=r["name"], category=r["category"], unit=r["unit"],
                    pack_qty=float(r["pack_qty"]), base_unit=r["base_unit"], cost_gbp=float(r["cost_gbp"]),
                    stock=int(r["stock"]), lead_days=int(r["lead_days"]), supplier=r["supplier"])
            for r in csv.DictReader(fh)
        ]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75) -> None:
        self.docs = [Counter(d) for d in docs]
        self.lengths = [len(d) for d in docs]
        self.avg = sum(self.lengths) / max(len(docs), 1)
        df = Counter(t for d in self.docs for t in d)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query: list[str]) -> np.ndarray:
        out = np.zeros(len(self.docs))
        for i, (tf, length) in enumerate(zip(self.docs, self.lengths, strict=True)):
            s = 0.0
            for t in set(query):
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / self.avg))
            out[i] = s
        return out


def _embedding_text(name: str) -> str:
    # The raw words carry meaning for the embedding model; the normalised tokens make
    # "S/S" and "stainless steel" land on the same word.
    return f"{name} | {normalise(name)}"


class Embedder:
    """Local ONNX embeddings via fastembed. Loaded lazily (about 2 s), then ~200 texts/s on a laptop CPU."""

    def __init__(self, model: str | None = None) -> None:
        self.model_name = model or settings.embedding_model
        self._model = None

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(self.model_name, cache_dir=str(settings.models_dir))
        vectors = np.array(list(self._model.embed(texts)), dtype=np.float32)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


class Catalogue:
    def __init__(self, products: list[Product] | None = None, embedder: Embedder | None = None,
                 cache_db: Path | None = None) -> None:
        self.products = products if products is not None else load_products()
        self.by_sku = {p.sku: p for p in self.products}
        self.embedder = embedder or Embedder()
        self.cache_db = cache_db or settings.catalogue_db
        self._norm = [normalise(p.name) for p in self.products]
        self._bm25 = BM25([n.split() for n in self._norm])
        self._bm25_raw = BM25([tokenize_raw(p.name) for p in self.products])

    def get(self, sku: str) -> Product | None:
        return self.by_sku.get(sku)

    def normalised_name(self, sku: str) -> str:
        return self._norm[self.products.index(self.by_sku[sku])]

    @cached_property
    def vectors(self) -> np.ndarray:
        texts = [_embedding_text(p.name) for p in self.products]
        key = hashlib.sha256(("\n".join(texts) + self.embedder.model_name).encode()).hexdigest()
        self.cache_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.cache_db) as db:
            db.execute("CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, dim INT, data BLOB)")
            row = db.execute("SELECT dim, data FROM embeddings WHERE key = ?", (key,)).fetchone()
            if row:
                return np.frombuffer(row[1], dtype=np.float32).reshape(-1, row[0])
            vectors = self.embedder.embed(texts)
            db.execute("DELETE FROM embeddings")
            db.execute("INSERT INTO embeddings VALUES (?, ?, ?)", (key, vectors.shape[1], vectors.tobytes()))
            return vectors

    def warm(self) -> None:
        """Load the embedding model and the vectors now, so the first search is not the slow one."""
        _ = self.vectors
        self.embedder.embed(["warm up"])

    def search(self, query: str, k: int = 5, mode: SearchMode = "hybrid",
               category: str | None = None) -> list[Hit]:
        qnorm = normalise(query)
        qtokens = qnorm.split()
        mask = np.array([category is None or p.category == category for p in self.products])
        keyword_order: list[int] = []
        vector_order: list[int] = []
        if mode in ("keyword", "hybrid"):
            keyword_order = _ranked(self._bm25.scores(qtokens), mask)
        if mode == "keyword_raw":
            keyword_order = _ranked(self._bm25_raw.scores(tokenize_raw(query)), mask)
        if mode in ("vector", "hybrid"):
            qvec = self.embedder.embed([_embedding_text(query)])[0]
            vector_order = _ranked(self.vectors @ qvec, mask, keep_zero=True)

        if mode == "hybrid":
            fused: dict[int, float] = {}
            for order in (keyword_order[:50], vector_order[:50]):
                for rank, idx in enumerate(order):
                    fused[idx] = fused.get(idx, 0.0) + 1 / (RRF_K + rank + 1)
            order = sorted(fused, key=lambda i: -fused[i])
            scores = fused
        else:
            order = keyword_order or vector_order
            scores = {i: 1 / (RRF_K + r + 1) for r, i in enumerate(order)}

        kw_rank = {idx: r + 1 for r, idx in enumerate(keyword_order)}
        vec_rank = {idx: r + 1 for r, idx in enumerate(vector_order)}
        qset = set(qtokens)
        return [
            Hit(product=self.products[i], score=scores[i], keyword_rank=kw_rank.get(i), vector_rank=vec_rank.get(i),
                matched_terms=[t for t in self._norm[i].split() if t in qset])
            for i in order[:k]
        ]


def _ranked(scores: np.ndarray, mask: np.ndarray, keep_zero: bool = False) -> list[int]:
    idx = [int(i) for i in np.argsort(-scores, kind="stable") if mask[i] and (keep_zero or scores[i] > 0)]
    return idx


_catalogue: Catalogue | None = None


def get_catalogue() -> Catalogue:
    global _catalogue
    if _catalogue is None:
        _catalogue = Catalogue()
    return _catalogue
