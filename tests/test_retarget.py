import sys
from pathlib import Path

import klayout.db as db
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from opc_retarget import (BiasTable, LineEndRule, Retargeter, RetargetRule,
                          load_rules, run_file)

DBU = 0.001
eng = Retargeter(DBU)


def nm(v):
    return int(round(v / DBU))


def region(*boxes):
    r = db.Region()
    for b in boxes:
        r.insert(db.Box(*[nm(v) for v in b]))
    return r


def bbox_of(out: db.Region, probe):
    """Bounding box (in um) of the output polygon touching probe box."""
    hit = out.interacting(region(probe))
    assert hit.count() == 1
    b = hit.bbox()
    return tuple(round(v * DBU, 4) for v in (b.left, b.bottom, b.right, b.top))


def rule(**kw):
    return RetargetRule("t", (17, 0), (117, 0), **kw)


def test_space_dependent_bias_only_moves_facing_edge_portion():
    # A and B face each other with space 0.05 over y=0.2..0.8 only.
    src = region((0, 0, 0.05, 1.0), (0.10, 0.2, 0.15, 0.8))
    t = BiasTable([], [0.06], [[-0.005, 0.0]])
    out = eng.run(src, rule(table=t)).output
    # A's right edge pulled in only where it faces B
    a = out.interacting(region((0, 0, 0.001, 0.001)))
    assert a.area() == nm(0.05) * nm(1.0) - nm(0.005) * nm(0.6)
    # B's left edge pulled in over its full length
    assert bbox_of(out, (0.12, 0.3, 0.13, 0.4)) == (0.105, 0.2, 0.15, 0.8)


def test_isolated_bias_and_width_bins():
    # narrow (0.05) and wide (0.10) isolated lines
    src = region((0, 0, 0.05, 1.0), (1.0, 0, 1.10, 1.0))
    t = BiasTable([0.06], [], [[0.002], [0.004]])
    out = eng.run(src, rule(table=t)).output
    # Long sides: width 0.05 -> narrow bin (+2nm). The short end edges face
    # each other across the line length (1.0), so their projection width falls
    # in the wide bin (+4nm) -- same as SVRF INT PROJ. Use line_end to override.
    assert bbox_of(out, (0.01, 0.5, 0.02, 0.6)) == (-0.002, -0.004, 0.052, 1.004)
    assert bbox_of(out, (1.05, 0.5, 1.06, 0.6)) == (0.996, -0.004, 1.104, 1.004)


def test_line_end_bias_by_tip_to_tip_space():
    src = region((0, 0, 0.05, 0.97), (0, 1.03, 0.05, 2.0),   # tight tip-to-tip 0.06
                 (1, 0, 1.05, 0.85))                           # open line end
    le = LineEndRule(0.08, [0.08], [0.0, 0.01])
    out = eng.run(src, rule(line_end=le)).output
    # top end faces the other tip at 0.06 -> unchanged; bottom end is open -> +10nm
    assert bbox_of(out, (0, 0.1, 0.01, 0.2)) == (0, -0.01, 0.05, 0.97)
    assert bbox_of(out, (1, 0.1, 1.01, 0.2)) == (1, -0.01, 1.05, 0.86)  # both ends +10nm


def test_line_end_excluded_from_body_table():
    src = region((0, 0, 0.05, 1.0))
    t = BiasTable([], [], [[0.003]])
    le = LineEndRule(0.08, [], [0.010])
    out = eng.run(src, rule(table=t, line_end=le)).output
    assert bbox_of(out, (0.01, 0.5, 0.02, 0.6)) == (-0.003, -0.01, 0.053, 1.01)


def test_mrc_flags_space_closed_by_retarget():
    src = region((0, 0, 0.05, 1.0), (0.10, 0, 0.15, 1.0))
    t = BiasTable([], [], [[0.006]])  # space 0.05 -> 0.038
    from opc_retarget import MrcRule
    res = eng.run(src, rule(table=t, mrc=MrcRule(min_space=0.04)))
    assert res.mrc_space.count() == 1


def test_bias_table_shape_validation():
    with pytest.raises(ValueError):
        BiasTable.from_dict({"width_bins": [0.06], "space_bins": [0.1], "bias": [[0, 0]]})
    with pytest.raises(ValueError):
        BiasTable.from_dict({"space_bins": [0.2, 0.1], "bias": [0, 0, 0]})


def test_end_to_end_gds(tmp_path):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "tools"))
    from make_test_gds import build
    gin, gout = tmp_path / "in.gds", tmp_path / "out.gds"
    build(str(gin))
    results = run_file(str(gin), load_rules(root / "rules" / "example_m1.json"), str(gout))
    ly = db.Layout(); ly.read(str(gout))
    out = db.Region(ly.top_cell().begin_shapes_rec(ly.find_layer(117, 0)))
    assert out.count() == 14
    assert results[0].mrc_width.count() == 0
    assert results[0].mrc_space.count() == 0


def test_line_end_follows_biased_width_no_jog():
    # side edges shrink by 4nm, line end extends by 10nm: the tip must span
    # the new width (0.004..0.046) -> output is a single clean rectangle.
    src = region((0, 0, 0.05, 1.0))
    t = BiasTable([], [], [[-0.004]])
    le = LineEndRule(0.08, [], [0.010])
    out = eng.run(src, rule(table=t, line_end=le)).output
    poly = next(out.each())
    assert poly.is_box()
    assert bbox_of(out, (0.02, 0.5, 0.03, 0.6)) == (0.004, -0.01, 0.046, 1.01)
