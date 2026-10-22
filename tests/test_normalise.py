import pytest

from app.catalogue.normalise import normalise, spec_conflicts, spec_tokens


@pytest.mark.parametrize(("customer", "catalogue_name"), [
    ("stainless steel bolt 10mm x 50 hex", "HEX HD BOLT M10X50 A2-70 S/S"),
    ("SS hex bolt M10x50", "HEX HD BOLT M10X50 A2-70 S/S"),
    ("galv elbow 3/4 inch", 'GALV M/I ELBOW 90DEG 3/4" BSP F/F'),
    ("2.5 sq mm twin and earth", "6242Y T&E 2.5MM2 GREY 100M"),
    ("SWA 3 core 4mm", "SWA CABLE 6943X 3C 4MM2"),
    ("MCB C32", "MCB 32A TYPE C 1P 10KA"),
    ("M10 washers st/st", "WASHER FORM A M10 A2 S/S"),
    ("DN15 ball valve", 'BALL VALVE LEVER F/F 1/2" BSP BRASS PN25'),
])
def test_both_sides_share_the_key_tokens(customer, catalogue_name):
    set(normalise(customer).split())
    product = set(normalise(catalogue_name).split())
    specs = {t for values in spec_tokens(normalise(customer)).values() for t in values}
    assert specs <= product, (normalise(customer), normalise(catalogue_name))


def test_inch_fractions():
    assert "1.25in" in normalise('1-1/4" socket').split()
    assert "1.25in" in normalise("1 1/4 inch socket").split()
    assert "0.5in" in normalise("½ in tee").split()
    assert "0.75in" in normalise("DN20 valve").split()


def test_pipe_mm_is_not_a_thread():
    assert "15mm" in normalise("15mm compression elbow").split()
    assert "m15" not in normalise("15mm compression elbow").split()


def test_spec_conflicts():
    assert spec_conflicts(normalise("M10x50 bolt"), normalise("M12X50 HEX BOLT"))
    assert not spec_conflicts(normalise("M10x50 bolt"), normalise("HEX HD BOLT M10X50 8.8 BZP"))
    # A detail the catalogue name does not state is not a conflict.
    assert not spec_conflicts(normalise("PTFE tape 12mm"), normalise("PTFE TAPE GAS 12MM X 5M YELLOW"))
    # A reducing bush has two inch sizes; asking for both is fine.
    assert not spec_conflicts(normalise('reducing bush 1" x 3/4"'), normalise('GALV REDUCING BUSH 1" X 3/4" BSP'))
    assert spec_conflicts(normalise('galv elbow 3"'), normalise('GALV M/I ELBOW 90DEG 1-1/4" BSP F/F'))
    assert not spec_conflicts(normalise("Adaptable box 100x100"), normalise("ADAPTABLE BOX 100X100X50 GALV"))
    assert spec_conflicts(normalise("cable tie 200x4.8"), normalise("CABLE TIE 300X4.8 BLACK NYLON"))
    assert not spec_conflicts(normalise("Woodscrew 5x50 pozi"), normalise("WOODSCREW PZ CSK 5.0x50 YZP"))
    assert not spec_conflicts(normalise("Flexibele kabel 3x1.5mm2 wit"), normalise("3183Y 3 CORE FLEX 1.5MM2 WHITE"))
