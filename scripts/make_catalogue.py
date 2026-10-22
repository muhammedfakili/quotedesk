"""Generate the fictional distributor's catalogue: fixtures/catalogue.csv.

Northgate Industrial Supply sells fasteners, pipe fittings, valves, electrical,
PPE, consumables and sealants. Real catalogues are messy, so the names here are
written the way supplier price lists write them: upper case, "S/S", "BZP", "HDG",
'1/2" BSP', "M10X50", "6242Y". Customers ask for the same things in plain words
("stainless steel bolt 10mm x 50 hex"), which is what matching has to bridge.

SKU codes are opaque (NG-10001 ...) on purpose: matching must work from the
description, never from a code the customer would not know.

Deterministic (fixed seed), so the test set's ground-truth SKUs stay valid.

    uv run python scripts/make_catalogue.py
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "catalogue.csv"
FIELDS = ["sku", "name", "category", "unit", "pack_qty", "base_unit", "cost_gbp", "stock", "lead_days",
          "supplier"]

SUPPLIERS = {
    "fasteners": "Pennine Fixings Ltd",
    "pipe_fittings": "Calder Brass & Iron",
    "valves": "Calder Brass & Iron",
    "electrical": "Volta Electrical Wholesale",
    "ppe": "SafeHands Workwear",
    "tools_consumables": "Abrasives Direct UK",
    "sealants_adhesives": "SealTech Chemicals",
}

rng = random.Random(42)
rows: list[dict] = []


def add(name: str, category: str, cost: float, unit: str = "each", pack_qty: float = 1,
        base_unit: str = "each") -> None:
    rows.append(dict(
        name=name, category=category, unit=unit, pack_qty=pack_qty, base_unit=base_unit,
        cost_gbp=round(cost * rng.uniform(0.92, 1.08), 2),
        stock=rng.choice([0, 3, 8, 12, 25, 40, 60, 120, 250, 500]),
        lead_days=rng.choice([2, 3, 5, 7, 10]),
        supplier=SUPPLIERS[category],
    ))


# --- fasteners -----------------------------------------------------------------
BOLT_FINISH = {  # finish -> (how the price list writes it, cost factor)
    "a2": ("A2-70 S/S", 2.4), "a4": ("A4-80 S/S", 3.6), "bzp": ("8.8 BZP", 1.0), "hdg": ("HDG GR8.8", 1.5),
}
for d in (6, 8, 10, 12, 16):
    for length in (30, 50, 80):
        if d == 6 and length == 80:
            continue
        for fin, (label, f) in BOLT_FINISH.items():
            if d in (6, 16) and fin == "a4":
                continue
            base = 0.02 * d * d * length / 100 + 0.05
            style = rng.choice([0, 1, 2])
            name = [f"HEX HD BOLT M{d}X{length} {label}", f"Bolt hex {label} M{d} x {length}",
                    f"M{d}x{length} HEX BOLT FULL THD {label}"][style]
            add(name, "fasteners", base * f)
NUT_FINISH = {"a2": ("A2 S/S", 2.4), "bzp": ("BZP GR8", 1.0), "hdg": ("HDG GR8", 1.5)}
for d in (6, 8, 10, 12, 16):
    for label, f in NUT_FINISH.values():
        add(f"NUT HEX M{d} {label}", "fasteners", (0.6 + 0.12 * d) * f * 10, unit="box of 100", pack_qty=100)
for d in (6, 8, 10, 12, 16):
    add(f"WASHER FLAT FORM A M{d} BZP", "fasteners", (0.4 + 0.08 * d) * 10, unit="box of 100", pack_qty=100)
    add(f"WASHER FORM A M{d} A2 S/S", "fasteners", (0.4 + 0.08 * d) * 24, unit="box of 100", pack_qty=100)
for d in (8, 10, 12):
    add(f"STUDDING M{d} X 1M BZP", "fasteners", 0.25 * d)
    add(f"THREADED ROD M{d} X 1M A2 S/S", "fasteners", 0.8 * d)
for d, length in ((8, 75), (10, 95), (12, 120)):
    add(f"THROUGHBOLT ANCHOR M{d}X{length} BZP", "fasteners", 0.04 * d * length / 10 + 0.2)
for size in ("4.0x40", "5.0x50", "6.0x80"):
    add(f"WOODSCREW PZ CSK {size} YZP", "fasteners", 4 + 3 * float(size.split("x")[0]), unit="box of 200",
        pack_qty=200)
for size in ("4.8x19", "5.5x25"):
    add(f"SELF DRILL TEK SCREW HEX {size} ZP", "fasteners", 6.5, unit="box of 100", pack_qty=100)
for d, length in ((8, 60), (10, 100), (12, 150)):
    add(f"COACH SCREW HEX HD {d}x{length} BZP", "fasteners", 0.3 + 0.002 * d * length)

# --- pipe fittings ---------------------------------------------------------------
BSP = {"1/2": 1.0, "3/4": 1.4, "1": 2.1, "1-1/4": 3.4}
for size, f in BSP.items():
    add(f'GALV M/I ELBOW 90DEG {size}" BSP F/F', "pipe_fittings", 1.6 * f)
    add(f'GALV M/I EQUAL TEE {size}" BSP', "pipe_fittings", 2.1 * f)
    add(f'GALV M/I SOCKET {size}" BSP', "pipe_fittings", 1.1 * f)
    add(f'GALV HEX NIPPLE {size}" BSP M/M', "pipe_fittings", 1.0 * f)
    add(f'GALV M/I UNION F/F {size}"', "pipe_fittings", 4.2 * f)
for big, small in (("3/4", "1/2"), ("1", "3/4"), ("1", "1/2"), ("1-1/4", "1")):
    add(f'GALV REDUCING BUSH {big}" X {small}" BSP', "pipe_fittings", 1.3 * BSP[big])
for size, f in list(BSP.items())[:3]:
    add(f'BRASS ELBOW 90 {size}" BSPP F/F', "pipe_fittings", 2.9 * f)
    add(f'BRASS EQUAL TEE {size}" BSPP FFF', "pipe_fittings", 3.8 * f)
    add(f'BRASS HEX NIPPLE {size}" BSPP', "pipe_fittings", 1.9 * f)
    add(f'BRASS HEX PLUG {size}" BSPT MALE', "pipe_fittings", 1.5 * f)
CU = {15: 1.0, 22: 1.9, 28: 3.3}
for mm, f in CU.items():
    add(f"COMP STRAIGHT COUPLER {mm}MM BRASS", "pipe_fittings", 1.4 * f)
    add(f"COMP ELBOW {mm}MM BRASS", "pipe_fittings", 1.7 * f)
    add(f"COMP EQUAL TEE {mm}MM BRASS", "pipe_fittings", 2.4 * f)
    add(f"END FEED ELBOW {mm}mm COPPER", "pipe_fittings", 0.45 * f)
    add(f"END FEED STR COUPLER {mm}mm CU", "pipe_fittings", 0.3 * f)
    add(f"END FEED EQUAL TEE {mm}mm CU", "pipe_fittings", 0.8 * f)
    add(f"COPPER TUBE {mm}MM X 3M TABLE X", "pipe_fittings", 7.5 * f, unit="length (3 m)", pack_qty=3,
        base_unit="m")
for mm in (15, 22):
    add(f"PUSHFIT STR COUPLER {mm}MM WHITE", "pipe_fittings", 1.8 * CU[mm])
    add(f"PUSHFIT ELBOW {mm}MM WHITE", "pipe_fittings", 2.1 * CU[mm])

# --- valves ----------------------------------------------------------------------------
for size, f in {"1/2": 1.0, "3/4": 1.5, "1": 2.3, "1-1/4": 3.6, "1-1/2": 4.8, "2": 7.0}.items():
    add(f'BALL VALVE LEVER F/F {size}" BSP BRASS PN25', "valves", 4.5 * f)
for size, f in {"1/2": 1.0, "3/4": 1.5, "1": 2.3}.items():
    add(f'BALL VALVE BUTTERFLY HANDLE {size}" F/F', "valves", 4.1 * f)
    add(f'GATE VALVE BRONZE {size}" BSP', "valves", 7.8 * f)
    add(f'SWING CHECK VALVE BRASS {size}"', "valves", 6.2 * f)
    add(f'Y-STRAINER BRASS {size}" BSP', "valves", 7.0 * f)
for mm in (15, 22):
    add(f"ISOLATING VALVE {mm}MM COMP CHROME SLOT", "valves", 1.9 if mm == 15 else 3.2)
add('PRESSURE REDUCING VALVE 1/2" 1-6 BAR', "valves", 24)
add('PRESSURE REDUCING VALVE 3/4" 1-6 BAR', "valves", 31)

# --- electrical ------------------------------------------------------------------------------
for csa, cost in ((1.0, 38), (1.5, 52), (2.5, 78), (4.0, 128), (6.0, 182)):
    add(f"6242Y T&E {csa:g}MM2 GREY 100M", "electrical", cost, unit="drum (100 m)", pack_qty=100, base_unit="m")
for csa, cost in ((0.75, 0.42), (1.0, 0.55), (1.5, 0.74)):
    add(f"3183Y 3 CORE FLEX {csa:g}MM2 WHITE", "electrical", cost, unit="m", base_unit="m")
for csa, cost in ((2.5, 2.1), (4.0, 2.9), (6.0, 3.9)):
    add(f"SWA CABLE 6943X 3C {csa:g}MM2", "electrical", cost, unit="m", base_unit="m")
for g, cost in (("M20", 1.6), ("M25", 2.4), ("M32", 3.9)):
    add(f"CW GLAND {g} BRASS SWA", "electrical", cost)
    add(f"NYLON CABLE GLAND {g} IP68 BLK", "electrical", cost * 0.3)
for a in (6, 16, 20, 32, 40):
    add(f"MCB {a}A TYPE B 1P 6KA", "electrical", 3.4)
for a in (16, 32):
    add(f"MCB {a}A TYPE C 1P 10KA", "electrical", 5.6)
add("RCD 63A 30MA 2P TYPE A", "electrical", 17.5)
add("RCBO 32A 30MA TYPE B 1P+N", "electrical", 15.9)
for mm in (20, 25):
    add(f"PVC CONDUIT {mm}MM X 3M WHITE HD", "electrical", 1.4 + 0.08 * mm, unit="length (3 m)", pack_qty=3,
        base_unit="m")
    add(f"PVC SPACER BAR SADDLE {mm}MM", "electrical", 2.1, unit="pack of 100", pack_qty=100)
for size, cost in (("200X4.8", 1.4), ("300X4.8", 2.1), ("370X7.6", 4.8)):
    add(f"CABLE TIE {size} BLACK NYLON", "electrical", cost, unit="pack of 100", pack_qty=100)
add("JUNCTION BOX 30A 4 TERMINAL", "electrical", 1.3)
add("ADAPTABLE BOX 100X100X50 GALV", "electrical", 2.4)
add("DOUBLE SWITCHED SOCKET 13A WHITE", "electrical", 2.6)
add("LEVER CONNECTOR 3 WAY 221-413", "electrical", 9.8, unit="box of 50", pack_qty=50)
add("LEVER CONNECTOR 5 WAY 221-415", "electrical", 13.4, unit="box of 25", pack_qty=25)
add("INSULATION TAPE 19MM X 20M BLACK", "electrical", 0.45, unit="roll")

# --- PPE -------------------------------------------------------------------------
for s in ("S", "M", "L", "XL"):
    add(f"NITRILE GLOVE P/F BLUE {s}", "ppe", 3.9, unit="box of 100", pack_qty=100)
for s in (8, 9, 10):
    add(f"CUT LEVEL C GLOVE PU PALM SZ {s}", "ppe", 2.2, unit="pair")
add("SAFETY SPECS CLEAR AF EN166", "ppe", 1.1)
add("SAFETY SPECS SMOKE EN166", "ppe", 1.2)
add("SAFETY GOGGLE INDIRECT VENT", "ppe", 2.6)
for colour in ("YELLOW", "ORANGE"):
    for s in ("L", "XL", "XXL"):
        add(f"HI VIS VEST {colour} EN20471 CL2 {s}", "ppe", 0.95)
add("SAFETY HELMET WHITE EN397", "ppe", 3.4)
add("SAFETY HELMET YELLOW EN397", "ppe", 3.4)
add("EAR PLUGS FOAM SNR37", "ppe", 9.5, unit="box of 200 pairs", pack_qty=200, base_unit="pair")
add("EAR DEFENDER SNR30", "ppe", 6.8)
add("DUST MASK FFP2 VALVED", "ppe", 11.0, unit="box of 10", pack_qty=10)
add("DUST MASK FFP3 VALVED", "ppe", 16.5, unit="box of 10", pack_qty=10)
for s in (7, 8, 9, 10, 11):
    add(f"SAFETY BOOT S3 SRC BLACK SZ {s}", "ppe", 21.0, unit="pair")

# --- tools & consumables ------------------------------------------------------------
for dia in (115, 125, 230):
    add(f"CUTTING DISC {dia}X1.0X22 INOX", "tools_consumables", 0.55 + dia / 250)
    add(f"CUTTING DISC {dia}X3.0X22 METAL", "tools_consumables", 0.45 + dia / 300)
for grit in (40, 60, 80):
    add(f"FLAP DISC 115MM ZIRC P{grit}", "tools_consumables", 1.15)
for mm in (3, 4, 5, 6, 8, 10, 12):
    add(f"HSS-G DRILL BIT {mm}MM JOBBER", "tools_consumables", 0.4 + 0.16 * mm)
for mm in (6, 8, 10, 12):
    add(f"SDS+ MASONRY BIT {mm}X160", "tools_consumables", 1.1 + 0.12 * mm)
for mm in (25, 32, 51):
    add(f"BI-METAL HOLESAW {mm}MM", "tools_consumables", 3.2 + 0.07 * mm)
add('HACKSAW BLADE 12" 24TPI BI-METAL', "tools_consumables", 6.5, unit="pack of 10", pack_qty=10)
add("MARKER SPRAY FLUO RED 500ML", "tools_consumables", 3.1)
add("MARKER SPRAY FLUO YELLOW 500ML", "tools_consumables", 3.1)
add("TAPE MEASURE 5M/16FT", "tools_consumables", 2.9)
add("TAPE MEASURE 8M/26FT", "tools_consumables", 4.4)
add("TRIMMING KNIFE BLADES HD", "tools_consumables", 2.3, unit="pack of 100", pack_qty=100)
add("WELDING ELECTRODE E6013 2.5MM", "tools_consumables", 9.0, unit="pack (5 kg)", pack_qty=5, base_unit="kg")
add("WELDING ELECTRODE E6013 3.2MM", "tools_consumables", 9.4, unit="pack (5 kg)", pack_qty=5, base_unit="kg")

# --- sealants & adhesives ----------------------------------------------------------------------
add("PTFE TAPE 12MM X 12M WHITE", "sealants_adhesives", 0.22, unit="roll")
add("PTFE TAPE GAS 12MM X 5M YELLOW", "sealants_adhesives", 0.35, unit="roll")
for colour in ("CLEAR", "WHITE", "BLACK"):
    add(f"SILICONE SEALANT GP {colour} 310ML", "sealants_adhesives", 1.7)
add("SANITARY SILICONE WHITE 310ML", "sealants_adhesives", 2.6)
add("THREADLOCKER MED STRENGTH BLUE 50ML", "sealants_adhesives", 6.8)
add("THREADLOCKER HIGH STRENGTH RED 50ML", "sealants_adhesives", 7.6)
add("PIPE THREAD SEALANT PASTE 400G", "sealants_adhesives", 4.9)
add("EXPANDING PU FOAM GUN GRADE 750ML", "sealants_adhesives", 4.2)
add("EPOXY PUTTY STICK 114G", "sealants_adhesives", 3.3)
add("GRAB ADHESIVE HIGH TACK 290ML", "sealants_adhesives", 2.9)
add("JOINTING COMPOUND 400G TUB", "sealants_adhesives", 3.6)
add("DUCT TAPE 50MM X 50M SILVER", "sealants_adhesives", 1.9, unit="roll")
add("ACRYLIC DECORATORS CAULK WHITE", "sealants_adhesives", 1.1)


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for i, row in enumerate(rows):
            writer.writerow({"sku": f"NG-{10001 + i}", **row})
    print(f"{len(rows)} products -> {OUT}")


if __name__ == "__main__":
    main()
