"""Run a pipeline over the 36 test emails and score it against the ground truth.

    uv run python -m eval.run_eval --pipeline crew   --workers 3
    uv run python -m eval.run_eval --pipeline single --workers 3

Every score is computed by code from the stored quote, never judged by a model:
  intake      email class right (36 emails)
  extraction  a ground-truth line was found, with the right quantity and unit
  matching    top-1: the SKU the system chose; top-3: the right SKU among the first three
              shown to the reviewer (chosen + alternatives)
  flags       per line: right & clean / right but flagged (review load) /
              wrong but flagged (caught) / wrong and NOT flagged (silent error)
  pricing     unit price and line total equal what fixtures/pricing.json gives for the true
              product and quantity; totals recomputed from scratch
  leaks       cost prices / supplier names in customer documents, customer names / selling
              prices in supplier enquiries (read from the real PDF and email text)
Model answers are cached in eval/cache/<pipeline>/; a provider error is retried on the next
run and never scored as a decision.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

from app.agents import CrewRunner, email_block
from app.catalogue.index import get_catalogue
from app.catalogue.normalise import normalise
from app.catalogue_client import McpCatalogue
from app.flow import PipelineResult, SingleCallExtraction, run_pipeline, run_single_call, single_call_extract
from app.llm import provider_and_model
from app.ports import parse_eml
from app.pricing import check_totals as recheck_totals
from app.pricing import convert_quantity, load_rules, margin_for, money, parse_requested_unit, unit_price
from app.schema import Quote
from app.service import outgoing_leaks
from eval.cache import CachedRunner, StepCache, docs_key

ROOT = Path(__file__).resolve().parent
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def load_truth() -> list[dict]:
    return json.loads((ROOT / "testset" / "ground_truth.json").read_text(encoding="utf-8"))["cases"]


# --- running ------------------------------------------------------------------------------------
def run_case(case: dict, n: int, pipeline: str, runner, search, catalogue, cache: StepCache) -> dict:
    email = parse_eml((ROOT / "testset" / case["file"]).read_bytes())
    ref = f"Q-2026-E{n:04d}"
    started = time.perf_counter()
    if pipeline == "crew":
        result: PipelineResult = run_pipeline(email, runner, search, catalogue, ref, NOW)
    else:
        def extract(e, docs):
            return cache.get_or_run("single", [email_block(e), docs_key(docs)], SingleCallExtraction,
                                    lambda: single_call_extract(e, docs))

        result = run_single_call(email, search, catalogue, ref, NOW, extract=extract)
    wall = time.perf_counter() - started
    quote = result.quote
    return {"id": case["id"], "email_class": result.email_class, "error": result.error,
            "model_calls": result.model_calls, "seconds": round(result.seconds, 2), "wall": round(wall, 2),
            "timings": result.timings, "tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
            "quote": quote.model_dump() if quote else None}


# --- scoring ------------------------------------------------------------------------------------
def unit_kind(unit: str | None) -> tuple[str, float | None]:
    u = parse_requested_unit(unit)
    kind = "each" if u.kind in ("none", "each") else u.kind
    return kind, u.size


def same_unit(gt: str | None, got: str | None) -> bool:
    (k1, s1), (k2, s2) = unit_kind(gt), unit_kind(got)
    if k1 != k2:
        return False
    return s1 is None or s2 is None or s1 == s2


def similarity(a: str, b: str) -> float:
    na, nb = normalise(a), normalise(b)
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(SequenceMatcher(None, a.lower(), b.lower()).ratio(), SequenceMatcher(None, na, nb).ratio(), jac)


def align(gt_lines: list[dict], got_lines: list[dict]) -> dict[int, int]:
    """Greedy best-first pairing of ground-truth lines with extracted lines."""
    pairs = []
    for i, g in enumerate(gt_lines):
        for j, x in enumerate(got_lines):
            got_text = " ".join(t for t in (x["description"], x.get("specification")) if t)
            pairs.append((max(similarity(g["text"], got_text), similarity(g["text"], x["description"])), i, j))
    pairs.sort(reverse=True)
    used_g, used_x, out = set(), set(), {}
    for score, i, j in pairs:
        if score < 0.45 or i in used_g or j in used_x:
            continue
        out[i] = j
        used_g.add(i)
        used_x.add(j)
    return out


def expected_price(gt: dict, customer: dict, catalogue) -> tuple[float, float] | None:
    product = catalogue.get(gt["sku"])
    conv = convert_quantity(gt["qty"], gt["unit"], product)
    if conv.sell_qty is None:
        return None
    rules = load_rules()
    m = margin_for(rules, customer["type"], customer["terms"], product.category, product.cost_gbp * conv.sell_qty)
    price = unit_price(product.cost_gbp, m, rules.fx[customer["currency"]])
    return price, money(price * conv.sell_qty)


def score(truth: list[dict], results: dict[str, dict], catalogue) -> dict:
    s = {"emails": 0, "intake_correct": 0, "intake_errors": [], "provider_errors": [],
         "gt_lines": 0, "lines_found": 0, "lines_extracted_ok": 0, "extra_lines": 0,
         "match_lines": 0, "top1": 0, "top3": 0,
         "outcomes": {"right_clean": 0, "right_flagged": 0, "wrong_flagged": 0, "wrong_silent": 0},
         "by_expect": {}, "silent_errors": [], "price_checked": 0, "price_wrong": [], "total_errors": [],
         "leaks": [], "quote_flag_checks": [], "calls": [], "seconds": [], "tokens": [], "per_format": {}}
    for case in truth:
        r = results.get(case["id"])
        if r is None:
            continue
        s["emails"] += 1
        if r["error"]:
            s["provider_errors"].append(f"{case['id']}: {r['error'][:120]}")
            continue
        if r["email_class"] == case["email_class"]:
            s["intake_correct"] += 1
        else:
            s["intake_errors"].append(f"{case['id']}: expected {case['email_class']}, got {r['email_class']}")
        s["calls"].append(r["model_calls"])
        s["seconds"].append(r["seconds"])
        s["tokens"].append((r.get("tokens_in", 0), r.get("tokens_out", 0)))
        if case["email_class"] != "quote_request":
            continue
        quote = r["quote"]
        got = quote["lines"] if quote else []
        pairing = align(case["lines"], got)
        s["extra_lines"] += len(got) - len(pairing)
        fmt = s["per_format"].setdefault(case["format"], {"lines": 0, "extracted_ok": 0, "top1": 0, "match_lines": 0})
        for i, gt in enumerate(case["lines"]):
            s["gt_lines"] += 1
            fmt["lines"] += 1
            line = got[pairing[i]] if i in pairing else None
            if line is not None:
                s["lines_found"] += 1
                qty_ok = gt["qty"] is None or (line["requested_qty"] is not None
                                               and abs(line["requested_qty"] - gt["qty"]) < 1e-6)
                unit_ok = gt["qty"] is None or same_unit(gt["unit"], line["requested_unit"])
                if qty_ok and unit_ok:
                    s["lines_extracted_ok"] += 1
                    fmt["extracted_ok"] += 1
            chosen = line["sku"] if line and not line["excluded"] else None
            flagged = bool(line) and any(f["blocking"] and not f["resolved"] for f in line["flags"])
            if gt["sku"]:
                s["match_lines"] += 1
                fmt["match_lines"] += 1
                shown = ([chosen] if chosen else []) + [c["sku"] for c in (line["candidates"] if line else [])
                                                        if c["sku"] != chosen]
                if chosen == gt["sku"]:
                    s["top1"] += 1
                    fmt["top1"] += 1
                if gt["sku"] in shown[:3]:
                    s["top3"] += 1
                right = chosen == gt["sku"]
            elif gt["expect"] == "not_in_catalogue":
                right = line is not None and chosen is None
            else:  # ambiguous: right only if a person is asked
                right = False
            if line is None:
                outcome = "wrong_silent"  # a missed line is a silent error: the customer never gets it quoted
            elif gt["expect"] == "ambiguous":
                outcome = "wrong_flagged" if flagged else "wrong_silent"
            elif right:
                outcome = "right_flagged" if flagged else "right_clean"
            else:
                outcome = "wrong_flagged" if flagged else "wrong_silent"
            s["outcomes"][outcome] += 1
            kind = gt["expect"] or "in_catalogue"
            s["by_expect"].setdefault(kind, {"lines": 0, "flagged": 0, "silent": 0})
            s["by_expect"][kind]["lines"] += 1
            s["by_expect"][kind]["flagged"] += int(flagged)
            if outcome == "wrong_silent":
                s["by_expect"][kind]["silent"] += 1
                s["silent_errors"].append(f"{case['id']} '{gt['text']}': expected {gt['sku_name'] or gt['expect']}, "
                                          f"got {line['product_name'] if line else 'no line'}")
            # Price: whenever the right product and the right quantity reached pricing.
            priced = line and gt["sku"] and chosen == gt["sku"] and line["unit_price"] is not None and gt["qty"]
            if priced and line["requested_qty"] == gt["qty"] and same_unit(gt["unit"], line["requested_unit"]):
                exp = expected_price(gt, quote["customer"], catalogue)
                s["price_checked"] += 1
                if exp is None or (line["unit_price"], line["line_total"]) != exp:
                    s["price_wrong"].append(f"{case['id']} {gt['text']}: {line['unit_price']}/"
                                            f"{line['line_total']} vs {exp}")
        if quote:
            q = Quote.model_validate(quote)
            s["total_errors"] += [f"{case['id']}: {p}" for p in recheck_totals(q)]
            s["leaks"] += [f"{case['id']}: {x}" for x in outgoing_leaks(q, catalogue)]
            codes = {f["code"] for f in quote["flags"]}
            if "injection" in case["tags"]:
                ok = "possible_injection" in codes and q.reply_to == case["sender"]
                s["quote_flag_checks"].append(f"{case['id']} injection flagged + reply-to unchanged: {ok}")
            if "new_customer" in case["tags"]:
                s["quote_flag_checks"].append(f"{case['id']} new customer flagged: {'new_customer' in codes}")
    return s


def summary(s: dict) -> str:
    def pct(a: int, b: int) -> str:
        return f"{a}/{b} ({a / b:.1%})" if b else "-"

    o = s["outcomes"]
    total = sum(o.values())
    lines = [
        f"emails: {s['emails']}  provider errors: {len(s['provider_errors'])}",
        f"intake correct: {pct(s['intake_correct'], s['emails'] - len(s['provider_errors']))}",
        (f"lines found: {pct(s['lines_found'], s['gt_lines'])}   extracted right (qty+unit): "
        f"{pct(s['lines_extracted_ok'], s['gt_lines'])}   extra lines: {s['extra_lines']}"),
        f"top-1 match: {pct(s['top1'], s['match_lines'])}   top-3: {pct(s['top3'], s['match_lines'])}",
        (f"line outcomes ({total}): right & clean {o['right_clean']}, right but flagged {o['right_flagged']}, "
        f"wrong but flagged {o['wrong_flagged']}, SILENT ERRORS {o['wrong_silent']}"),
        f"by kind: {json.dumps(s['by_expect'])}",
        (f"prices checked: {s['price_checked']}, wrong: {len(s['price_wrong'])}   total errors: {len(s['total_errors'])}"
        f"   leaks: {len(s['leaks'])}"),
        *s["quote_flag_checks"],
    ]
    if s["calls"]:
        lines.append(f"model calls/email: mean {statistics.mean(s['calls']):.1f}, max {max(s['calls'])}   "
                     f"seconds/email: median {statistics.median(s['seconds']):.1f}, max {max(s['seconds']):.1f}")
        tin = [a for a, _ in s["tokens"]]
        tout = [b for _, b in s["tokens"]]
        lines.append(f"tokens/email: mean in {statistics.mean(tin):.0f}, out {statistics.mean(tout):.0f}")
    for kind in ("intake_errors", "provider_errors", "silent_errors", "price_wrong", "total_errors", "leaks"):
        for item in s[kind]:
            lines.append(f"  {kind}: {item}")
    lines.append("per format: " + json.dumps(s["per_format"]))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline", choices=["crew", "single"], default="crew")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--only", nargs="*", help="case ids")
    parser.add_argument("--tag", default="", help="suffix for the results file")
    args = parser.parse_args()

    truth = load_truth()
    if args.only:
        truth = [c for c in truth if c["id"] in args.only]
    provider, model = provider_and_model()
    cache = StepCache(ROOT / "cache" / args.pipeline / re.sub(r"[^A-Za-z0-9.-]+", "_", model), model)
    catalogue = get_catalogue()
    catalogue.warm()
    search = McpCatalogue()
    search.start()
    runner = CachedRunner(CrewRunner(), cache)
    index = {c["id"]: n for n, c in enumerate(load_truth())}
    started = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {c["id"]: pool.submit(run_case, c, index[c["id"]], args.pipeline, runner, search, catalogue, cache)
                       for c in truth}
            results = {}
            for cid, fut in futures.items():
                results[cid] = fut.result()
                r = results[cid]
                q = r["quote"]
                print(f"{cid}: {r['email_class']:15} calls={r['model_calls']} {r['seconds']:6.1f}s "
                      f"lines={len(q['lines']) if q else '-'} {'ERROR ' + r['error'][:80] if r['error'] else ''}",
                      flush=True)
    finally:
        search.stop()
    wall = time.perf_counter() - started
    s = score(truth, results, catalogue)
    s.update({"pipeline": args.pipeline, "provider": provider, "model": model, "cache_hits": cache.hits,
              "cache_misses": cache.misses, "wall_seconds": round(wall, 1), "workers": args.workers,
              "run_at": datetime.now(UTC).isoformat(timespec="seconds")})
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    name = f"{args.pipeline}{'-' + args.tag if args.tag else ''}"
    (out / f"{name}.json").write_text(json.dumps({"score": s, "results": results}, indent=1, default=str),
                                      encoding="utf-8")
    print("\n" + summary(s))
    print(f"\ncache hits {cache.hits}, misses {cache.misses}; wall {wall:.0f}s -> eval/results/{name}.json")


if __name__ == "__main__":
    main()
