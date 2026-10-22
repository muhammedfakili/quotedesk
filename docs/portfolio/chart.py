"""Portfolio image 05: measured results, read from eval/results/{crew,single,retrieval}.json.

    uv run python docs/portfolio/chart.py
"""

import json
from pathlib import Path

import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[2]
RES = ROOT / "eval" / "results"
OUT = ROOT / "docs" / "portfolio" / "05-measured-results.png"


def score(name: str) -> dict:
    return json.loads((RES / f"{name}.json").read_text(encoding="utf-8"))["score"]


crew, single = score("crew"), score("single")
retrieval = json.loads((RES / "retrieval.json").read_text(encoding="utf-8"))["results"]


def metrics(s: dict) -> list[float]:
    ok = s["emails"] - len(s["provider_errors"])
    o = s["outcomes"]
    safe = o["right_clean"] + o["right_flagged"] + o["wrong_flagged"]
    return [s["intake_correct"] / ok * 100, s["lines_extracted_ok"] / s["gt_lines"] * 100,
            s["top1"] / s["match_lines"] * 100, safe / sum(o.values()) * 100]


BG, FG, MUTED = "#0b0d10", "#f2f6fa", "#8fa0b4"
BLUE, GREY = "#2f7fd4", "#5d6b7c"
fig, (ax, ax2) = plt.subplots(1, 2, figsize=(15.68, 6.98), dpi=100, gridspec_kw={"width_ratios": [2.3, 1]})
fig.patch.set_facecolor(BG)
labels = ["Email class\nright", "Lines extracted\n(qty + unit)", "Right product\nchosen (top-1)",
          "Lines right or\nflagged for a person"]
x = range(len(labels))
w = 0.38
for offset, s, colour, name in ((-w / 2, single, GREY, "One call + top search hit"),
                                (w / 2, crew, BLUE, "QuoteDesk crew (CrewAI + MCP)")):
    vals = metrics(s)
    bars = ax.bar([i + offset for i in x], vals, w, color=colour, label=name)
    for bar, v in zip(bars, vals, strict=True):
        ax.text(bar.get_x() + bar.get_width() / 2, v + 1, f"{v:.0f}%", ha="center", va="bottom", color=FG,
                fontsize=13, fontweight="bold")
ax.set_xticks(list(x), labels, color=FG, fontsize=12)
ax.set_ylim(0, 115)
ax.set_yticks([0, 50, 100], ["0%", "50%", "100%"], color=MUTED, fontsize=11)
ax.legend(loc="upper left", bbox_to_anchor=(0, 1.13), ncol=2, frameon=False, labelcolor=FG, fontsize=12)
ax.text(3.0, 8, f"silent errors:\n{single['outcomes']['wrong_silent']} vs {crew['outcomes']['wrong_silent']}",
        ha="center", color=BG, fontsize=13, fontweight="bold")

modes = ["keyword_raw", "keyword", "vector", "hybrid"]
names = ["Plain\nkeyword", "Keyword +\ntrade words", "Embeddings", "Hybrid\n(RRF)"]
vals = [retrieval[m]["3"] * 100 for m in modes]
bars = ax2.bar(range(4), vals, 0.6, color=[GREY, GREY, GREY, BLUE])
for bar, v in zip(bars, vals, strict=True):
    ax2.text(bar.get_x() + bar.get_width() / 2, v + 1, f"{v:.0f}%", ha="center", va="bottom", color=FG,
             fontsize=13, fontweight="bold")
ax2.set_xticks(range(4), names, color=FG, fontsize=11)
ax2.set_ylim(0, 115)
ax2.set_yticks([0, 50, 100], ["0%", "50%", "100%"], color=MUTED, fontsize=11)
ax2.set_title("Catalogue search alone: right product in top 3", color=FG, fontsize=13, loc="left")
for a in (ax, ax2):
    a.set_facecolor(BG)
    for side in ("top", "right"):
        a.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        a.spines[side].set_color(MUTED)
fig.suptitle("Measured on 36 test emails (30 quote requests, 169 lines)", color=FG, fontsize=21,
             fontweight="bold", x=0.05, ha="left", y=0.97)
fig.text(0.05, 0.885, "PDFs, Excel, Word, scans, phone photos, typos, German/Dutch units, unstocked and vague "
         "items, one injection attempt · Gemini 3.5 Flash-Lite (free tier) in both pipelines",
         color=MUTED, fontsize=11.5, ha="left")
fig.subplots_adjust(top=0.76, bottom=0.14, left=0.05, right=0.98, wspace=0.18)
fig.savefig(OUT, facecolor=BG)
print("saved", OUT)
