"""Disk cache for model steps, so a scoring change never costs another day of free-tier quota.

The key is the step, the model and a hash of exactly what the step was given. A
provider error is never cached (and never scored as an agent decision): the next run
simply tries again. The time and call count of the first, real run are stored with
the answer, so cached re-runs still report what the step actually cost.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from pydantic import BaseModel

from app.agents import AgentRunner, Step, email_block
from app.reader import Document
from app.schema import CoverNote, ExtractionResult, InboundEmail, IntakeResult, MatchResult


def docs_key(docs: list[Document]) -> list:
    return [[d.name, d.kind, d.text, [hashlib.sha256(p.data).hexdigest() for p in d.pages]] for d in docs]


class StepCache:
    def __init__(self, folder: Path, model: str) -> None:
        self.folder = folder
        self.model = model
        self.folder.mkdir(parents=True, exist_ok=True)
        self.hits = self.misses = 0

    def get_or_run(self, step: str, payload: object, schema: type[BaseModel], run) -> Step:
        key = hashlib.sha256(json.dumps([step, self.model, payload], sort_keys=True, default=str).encode()).hexdigest()
        path = self.folder / f"{step}-{key[:24]}.json"
        if path.exists():
            self.hits += 1
            data = json.loads(path.read_text(encoding="utf-8"))
            return Step(schema.model_validate(data["value"]), data["calls"], data["seconds"],
                        data.get("tokens_in", 0), data.get("tokens_out", 0))
        self.misses += 1
        started = time.perf_counter()
        result: Step = run()
        path.write_text(json.dumps({"value": result.value.model_dump(), "calls": result.calls,
                                    "seconds": round(time.perf_counter() - started, 2),
                                    "tokens_in": result.tokens_in, "tokens_out": result.tokens_out}, indent=1),
                        encoding="utf-8")
        return result


class CachedRunner:
    def __init__(self, inner: AgentRunner, cache: StepCache) -> None:
        self.inner, self.cache = inner, cache
        self.name = inner.name

    def classify(self, email: InboundEmail) -> Step[IntakeResult]:
        return self.cache.get_or_run("intake", email_block(email), IntakeResult, lambda: self.inner.classify(email))

    def extract(self, email: InboundEmail, docs: list[Document]) -> Step[ExtractionResult]:
        return self.cache.get_or_run("extract", [email_block(email), docs_key(docs)], ExtractionResult,
                                     lambda: self.inner.extract(email, docs))

    def match(self, lines: list[dict], tools: list) -> Step[MatchResult]:
        return self.cache.get_or_run("match", lines, MatchResult, lambda: self.inner.match(lines, tools))

    def cover(self, context: dict) -> Step[CoverNote]:
        return self.cache.get_or_run("cover", context, CoverNote, lambda: self.inner.cover(context))
