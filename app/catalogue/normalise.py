"""Turn trade shorthand into one canonical vocabulary.

A price list says 'HEX HD BOLT M10X50 A2-70 S/S'; a customer writes 'stainless steel
bolt 10mm x 50 hex'. Both should become the same tokens: `stainless hex bolt m10 len50`.
The same function runs on catalogue names and on request lines, so whatever it does,
it does to both sides.

Covered: inch fractions ('1/2"', '1/2 in', '½', '1-1/4"', 'DN15'), metric thread x
length ('M10X50', '10mm x 50'), cable cross-section ('2.5mm2', '2.5 sq mm'), finishes
(S/S, A2, BZP, HDG, galv.), and the common product synonyms of the trade.

`spec_tokens` pulls out the size-like tokens (m10, len50, 0.5in, 2.5mm2, 15mm, sz9...):
code uses them to catch a match whose size contradicts the request.
"""

from __future__ import annotations

import re

_UNICODE = {
    "½": " 1/2", "¾": " 3/4", "¼": " 1/4", "²": "2", "×": "x", "″": '"', "”": '"', "“": '"',
    "°": "deg", "–": "-", "—": "-", "’": "'", "‘": "'",
}

# Phrases first (longest first), then single tokens. Values are canonical tokens.
_PHRASES = {
    "stainless steel": "stainless", "s/s": "stainless", "st/st": "stainless", "st st": "stainless", "s.s.": "stainless", "inox": "stainless",
    "hot dip galvanised": "galvanised", "hot dip galvanized": "galvanised", "hot-dip galvanised": "galvanised",
    "bright zinc plated": "zinc", "zinc plated": "zinc", "yellow zinc": "zinc",
    "twin and earth": "twinearth", "twin & earth": "twinearth", "t&e": "twinearth", "t+e": "twinearth",
    "high visibility": "hivis", "hi-vis": "hivis", "hi vis": "hivis", "hi-viz": "hivis", "hi viz": "hivis",
    "threaded rod": "threadedrod", "thread rod": "threadedrod", "all thread": "threadedrod",
    "all-thread": "threadedrod", "allthread": "threadedrod", "studding": "threadedrod",
    "circuit breaker": "mcb", "miniature circuit breaker": "mcb",
    "lever connector": "leverconnector", "wago connector": "leverconnector", "lever connectors": "leverconnector",
    "cable tie": "cabletie", "cable ties": "cabletie", "zip tie": "cabletie", "zip ties": "cabletie",
    "tie wrap": "cabletie", "tie wraps": "cabletie",
    "safety glasses": "safetyglasses", "safety specs": "safetyglasses", "safety spectacles": "safetyglasses",
    "ear plugs": "earplug", "ear plug": "earplug", "earplugs": "earplug",
    "ear defenders": "eardefender", "ear defender": "eardefender", "ear muffs": "eardefender",
    "push fit": "pushfit", "push-fit": "pushfit",
    "end feed": "endfeed", "end-feed": "endfeed",
    "malleable iron": "malleable", "m/i": "malleable",
    "f/f": "ff", "female x female": "ff", "female/female": "ff", "fff": "ff",
    "m/m": "malemale", "male x male": "malemale",
    "through bolt": "throughbolt", "wedge anchor": "throughbolt", "throughbolt anchor": "throughbolt",
    "self drill": "selfdrill", "self-drill": "selfdrill", "self drilling": "selfdrill", "tek screw": "selfdrill",
    "hex head": "hex", "hex hd": "hex", "hexagon head": "hex", "hexagon": "hex",
    "sds plus": "sds", "sds+": "sds", "hss-g": "hss",
    "hole saw": "holesaw", "hole saws": "holesaw",
    "tape measure": "tapemeasure", "measuring tape": "tapemeasure",
    "cutting disc": "cuttingdisc", "cutting discs": "cuttingdisc", "cut off disc": "cuttingdisc",
    "cut-off disc": "cuttingdisc", "cut off wheel": "cuttingdisc", "cut-off wheel": "cuttingdisc",
    "cutting disk": "cuttingdisc", "slitting disc": "cuttingdisc",
    "flap disc": "flapdisc", "flap discs": "flapdisc",
    "drill bit": "drill", "drill bits": "drill",
    "dust mask": "mask", "dust masks": "mask", "respirator": "mask",
    "hard hat": "helmet", "hard hats": "helmet",
    "thread lock": "threadlocker", "thread locker": "threadlocker",
    "plumbers tape": "ptfe", "plumber's tape": "ptfe", "teflon tape": "ptfe",
    "powder free": "powderfree", "p/f": "powderfree",
    "grade 8.8": "8.8", "gr8.8": "8.8", "gr 8.8": "8.8", "gr8": "8.8", "grade 8": "8.8",
    "half inch": "0.5in", "half-inch": "0.5in", "three quarter inch": "0.75in", "three-quarter inch": "0.75in",
    "quarter inch": "0.25in", "one inch": "1in", "inch and a quarter": "1.25in",
    "copper pipe": "copper tube", "set screw": "bolt", "setscrew": "bolt",
    "isolation valve": "isolating valve", "non return valve": "check valve", "non-return valve": "check valve",
    "double socket": "double switched socket", "twin socket": "double switched socket",
    "armoured cable": "swa", "armored cable": "swa",
    "coach bolt": "coach screw", "lag screw": "coach screw", "lag bolt": "coach screw",
}
_TOKENS = {
    "ss": "stainless", "a2": "stainless a2", "a4": "stainless a4", "a2-70": "stainless a2", "a4-80": "stainless a4",
    "galv": "galvanised", "galvanized": "galvanised", "hdg": "galvanised", "galvanised": "galvanised",
    "bzp": "zinc", "zp": "zinc", "yzp": "zinc", "zinc": "zinc",
    "hexagonal": "hex", "bolts": "bolt", "comp": "compression", "compression": "compression",
    "coupling": "coupler", "couplings": "coupler", "couplers": "coupler", "str": "straight",
    "eq": "equal", "ell": "elbow", "elbows": "elbow", "degree": "deg", "degrees": "deg",
    "tees": "tee", "t-piece": "tee", "cu": "copper", "silicon": "silicone", "teflon": "ptfe",
    "loctite": "threadlocker", "wago": "leverconnector", "specs": "safetyglasses", "goggles": "goggle",
    "gloves": "glove", "boots": "boot", "helmets": "helmet", "gray": "grey", "6242y": "twinearth",
    "3183y": "flex", "flexible": "flex", "armoured": "swa", "6943x": "swa", "pozi": "pz", "pozidriv": "pz",
    "csk": "countersunk", "jobbers": "jobber", "electrodes": "electrode", "rods": "rod",
    "tek": "selfdrill", "holesaws": "holesaw", "nos": "", "pcs": "", "pc": "", "pieces": "", "piece": "",
    "x": "", "the": "", "and": "", "of": "", "for": "", "with": "", "please": "", "qty": "", "nr": "",
    "ea": "", "each": "", "off": "", "a": "", "to": "", "in": "", "type": "", "size": "", "sz": "sz",
    "mm": "", "full": "", "thd": "", "thread": "", "threaded": "",
    "small": "s", "medium": "m", "large": "l", "xlarge": "xl", "x-large": "xl", "xx-large": "xxl",
}
_CANONICAL = {word for value in (*_PHRASES.values(), *_TOKENS.values()) for word in value.split()}
_DN = {"dn15": "0.5in", "dn20": "0.75in", "dn25": "1in", "dn32": "1.25in", "dn40": "1.5in", "dn50": "2in"}
_CABLE_WORDS = re.compile(r"\b(twinearth|swa|flex|cable|core)\b")
_FASTENER_WORDS = re.compile(r"\b(bolt|nut|washer|screw|threadedrod|throughbolt|anchor|rod)\b")

_PHRASE_RE = re.compile(
    "|".join(re.escape(p) for p in sorted(_PHRASES, key=len, reverse=True)).join((r"(?<![\w/])(?:", r")(?![\w/])"))
)


def _fraction(whole: str | None, num: str, den: str) -> str:
    value = (int(whole) if whole else 0) + int(num) / int(den)
    return f"{value:g}in"


def _canonical_number(m: re.Match) -> str:
    return f"{float(m.group(0)):g}"


def normalise(text: str) -> str:
    s = text.lower()
    for char, repl in _UNICODE.items():
        s = s.replace(char, repl)
    # Inch sizes: "1-1/4"", "1 1/4 in", "1/2"", "1/2 bsp", "3/4in". A fraction in this trade is
    # always an inch size, so a bare "1/2" counts too.
    s = re.sub(r"\b(?:(\d)[- ])?(\d)/(\d{1,2})\s*(?:\"|''|inch(?:es)?\b|in\b)?",
               lambda m: " " + _fraction(m.group(1), m.group(2), m.group(3)) + " ", s)
    s = re.sub(r"\b(\d+(?:\.\d+)?)\s*(?:\"|''|inch(?:es)?\b)", lambda m: f" {float(m.group(1)):g}in ", s)
    s = re.sub(r"\bdn\s?(15|20|25|32|40|50)\b", lambda m: " " + _DN["dn" + m.group(1)] + " ", s)
    # Cable cross-section: 2.5mm2, 2.5 sq mm, 2.5mm sq
    s = re.sub(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:mm2|mm\^2|sq\.?\s*mm|sqmm|mm\s*sq)\b",
               lambda m: f" {float(m.group(1)):g}mm2 ", s)
    s = re.sub(r"\b(\d)\s*(?:c|core)?\s*x\s*(\d+(?:\.\d+)?mm2)", r" \1 core \2 ", s)  # "3x1.5mm2" = 3 core 1.5mm2
    # Metric thread x length: M10X50, M10 x 50, 10mm x 50(mm), 10 x 50mm. Not "15mm x 3m" (a tube).
    s = re.sub(r"\bm(\d{1,2})\s*x\s*(\d{2,3})(?:\s*mm)?\b(?!\s*m\b)", r" m\1 len\2 ", s)
    s = re.sub(r"\b(\d{1,2})\s*mm\s*x\s*(\d{2,3})(?:\s*mm)?\b(?!\s*m\b)(?:\s*(?:long|lg))?", r" m\1 len\2 ", s)
    s = re.sub(r"\b(\d{1,2})\s*x\s*(\d{2,3})\s*mm\b", r" m\1 len\2 ", s)
    s = re.sub(r"\b(\d{2,3})\s*mm\s*(?:long|lg)\b", r" len\1 ", s)
    s = re.sub(r"\blength\s*(\d{2,3})\s*mm\b", r" len\1 ", s)
    s = re.sub(r"\b(\d+(?:\.\d+)?)\s*(?:mm)?\s*x\s*(\d+(?:\.\d+)?)(?:\s*mm)?\b",  # 200 x 4.8mm, 5.0x50
               lambda m: f" {float(m.group(1)):g}x{float(m.group(2)):g} ", s)
    s = re.sub(r"\b(?:size|sz)\s*(\d{1,2})\b", r" sz\1 ", s)
    s = re.sub(r"\b(\d+(?:\.\d+)?)\s*(?:metres?|meters?|mtrs?|m)\b", r" \1m ", s)  # "3 m", "50 metres"
    # MCB curve written first: "C32", "B16" -> "32a c"
    s = re.sub(r"\b([bcd])\s?(6|10|16|20|25|32|40|50|63)\b", r" \2a \1 ", s)
    s = _PHRASE_RE.sub(lambda m: " " + _PHRASES[m.group(0)] + " ", s)
    # On cable, "2.5mm" or a bare "2.5" means the cross-section in mm2.
    if _CABLE_WORDS.search(s):
        s = re.sub(r"\b(\d{1,2}(?:\.\d+)?)\s*mm\b(?!2)", lambda m: f" {float(m.group(1)):g}mm2 ", s)
        s = re.sub(r"(?<![\w.])(0\.75|1\.5|2\.5|1|4|6|10|16)(?![\w.])", lambda m: f" {float(m.group(1)):g}mm2 ", s)
    # "10mm" next to a fastener word is the thread size M10 (but "15mm" on a pipe fitting is not).
    if _FASTENER_WORDS.search(s) or re.search(r"\bm\d{1,2}\b", s):
        s = re.sub(r"\b(\d{1,2})\s*mm\b(?!2)", r" m\1 ", s)
    s = re.sub(r"\b(\d+)\s+mm\b", r"\1mm", s)  # "15 mm" -> "15mm"
    s = re.sub(r"\b\d+\.0\b", _canonical_number, s)  # "4.0" -> "4"

    tokens: list[str] = []
    for tok in re.findall(r"[a-z0-9]+(?:[.+-][a-z0-9]+)*", s):
        if tok in _DN:
            tokens.append(_DN[tok])
            continue
        mapped = _TOKENS.get(tok)
        if (mapped is None and tok not in _CANONICAL and len(tok) > 3 and tok.isalpha() and tok.endswith("s")
                and not tok.endswith("ss")):
            tok = tok[:-1]  # plurals: discs -> disc, valves -> valve
            mapped = _TOKENS.get(tok)
        tokens.extend((mapped if mapped is not None else tok).split())
        if re.fullmatch(r"\d+x[\d.x]+", tok):  # "115x1x22" also answers to "115mm"
            tokens.append(tok.split("x")[0] + "mm")
    return " ".join(t for t in tokens if t)


_SPEC_KINDS = (
    ("thread", re.compile(r"^m\d{1,2}$")), ("length", re.compile(r"^len\d+$")),
    ("inch", re.compile(r"^\d+(?:\.\d+)?in$")), ("csa", re.compile(r"^\d+(?:\.\d+)?mm2$")),
    ("mm", re.compile(r"^\d+(?:\.\d+)?mm$")), ("amps", re.compile(r"^\d+a$")),
    ("ml", re.compile(r"^\d+ml$")), ("size", re.compile(r"^sz\d+$")), ("grit", re.compile(r"^p\d{2,3}$")),
    ("dims", re.compile(r"^\d+(?:\.\d+)?x[\d.x]+$")),
)


def spec_tokens(normalised: str) -> dict[str, set[str]]:
    """Size-like tokens of a normalised string, by kind: thread, length, inch size, mm, amps..."""
    found: dict[str, set[str]] = {}
    for tok in normalised.split():
        for kind, pattern in _SPEC_KINDS:
            if pattern.match(tok):
                found.setdefault(kind, set()).add(tok)
                break
    return found


def spec_conflicts(request: str, candidate: str) -> list[str]:
    """Sizes the request states that the candidate states differently ("m10" vs "m12").

    Both arguments are normalised strings. A kind the candidate does not mention is
    not a conflict (catalogue names leave things out); a different value is.
    Inch sizes are compared as a set because a reducing bush has two of them.
    """
    req, cand = spec_tokens(request), spec_tokens(candidate)
    conflicts = []
    for kind, values in req.items():
        if kind == "dims" and kind in cand and all(any(c == v or c.startswith(v + "x") for c in cand[kind])
                                                   for v in values):
            continue  # "100x100" asked, "100x100x50" offered: a detail left out, not a different size
        if kind in cand and not values <= cand[kind]:
            conflicts.append(f"{kind}: asked {', '.join(sorted(values))}, item has {', '.join(sorted(cand[kind]))}")
    return conflicts


def tokenize_raw(text: str) -> list[str]:
    """Plain lower-case word split, no domain knowledge: the keyword baseline."""
    return re.findall(r"[a-z0-9]+", text.lower())
