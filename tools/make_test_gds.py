#!/usr/bin/env python3
"""Generate a small M1 (17/0) test pattern for retarget experiments."""
import sys
import klayout.db as db

def build(path: str) -> None:
    ly = db.Layout()
    ly.dbu = 0.001  # 1 nm
    top = ly.create_cell("TOP")
    m1 = top.shapes(ly.layer(17, 0))
    um = lambda v: int(round(v / ly.dbu))
    box = lambda x1, y1, x2, y2: m1.insert(db.Box(um(x1), um(y1), um(x2), um(y2)))

    # 1) dense lines: width 0.05, space 0.05
    for i in range(5):
        x = i * 0.10
        box(x, 0, x + 0.05, 2.0)
    # 2) semi-dense lines: width 0.05, space 0.08
    for i in range(3):
        x = 1.0 + i * 0.13
        box(x, 0, x + 0.05, 2.0)
    # 3) isolated wide line: width 0.10
    box(2.0, 0, 2.10, 2.0)
    # 4) line-end pairs: tip-to-tip 0.06 (tight) and 0.30 (open)
    box(3.0, 0, 3.05, 0.97); box(3.0, 1.03, 3.05, 2.0)
    box(3.5, 0, 3.55, 0.85);  box(3.5, 1.15, 3.55, 2.0)
    # 5) L-shape (concave corner)
    m1.insert(db.Polygon([db.Point(um(x), um(y)) for x, y in
              [(4.0, 0), (4.0, 1.0), (4.6, 1.0), (4.6, 0.9), (4.1, 0.9), (4.1, 0)]]))
    ly.write(path)

if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "test_m1.gds")
