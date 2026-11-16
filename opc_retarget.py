#!/usr/bin/env python3
"""Rule-based conditional retarget for GDS layouts (KLayout Python API).

SVRF-style retarget expressed as a JSON rule deck:

  * Every polygon edge is fragmented by the *facing* width / space
    (projection metric, the same way Calibre measures INT/EXT with PROJ),
    classified into width x space bins and moved by the bin's bias.
  * Line-end edges (short edges with convex corners on both ends) are
    handled by a separate bias table keyed on end-to-end space.
  * Optional MRC (min width / min space) on the retargeted result.

All lengths in the rule file are in microns (like SVRF); they are converted
to database units with the layout's dbu.

Usage:
    python opc_retarget.py input.gds rules.json -o output.gds [--report report.txt]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import klayout.db as db


# --------------------------------------------------------------------------
# Rule definitions
# --------------------------------------------------------------------------

def _parse_layer(spec: str) -> tuple[int, int]:
    """'17/0' -> (17, 0)."""
    layer, _, datatype = str(spec).partition("/")
    return int(layer), int(datatype or 0)


@dataclass
class BiasTable:
    """2D bias table: rows = width bins, columns = space bins.

    ``width_bins`` / ``space_bins`` are ascending upper bounds (exclusive).
    With N bounds there are N+1 bins; the last one is ">= last bound".
    ``bias`` has shape (len(width_bins)+1) x (len(space_bins)+1).
    Positive bias grows the edge outward, negative pulls it inward.
    """

    width_bins: list[float]
    space_bins: list[float]
    bias: list[list[float]]

    @classmethod
    def from_dict(cls, d: dict) -> "BiasTable":
        width_bins = list(d.get("width_bins", []))
        space_bins = list(d.get("space_bins", []))
        bias = d["bias"]
        # A 1D list is a space-only table.
        if bias and not isinstance(bias[0], list):
            bias = [bias]
        table = cls(width_bins, space_bins, [list(map(float, row)) for row in bias])
        table.validate()
        return table

    def validate(self) -> None:
        for name, bins in (("width_bins", self.width_bins), ("space_bins", self.space_bins)):
            if any(b <= 0 for b in bins) or bins != sorted(set(bins)):
                raise ValueError(f"{name} must be strictly ascending positive values: {bins}")
        rows, cols = len(self.width_bins) + 1, len(self.space_bins) + 1
        if len(self.bias) != rows or any(len(r) != cols for r in self.bias):
            raise ValueError(
                f"bias table must be {rows} x {cols} "
                f"(width_bins+1 x space_bins+1), got "
                f"{len(self.bias)} x {[len(r) for r in self.bias]}"
            )


@dataclass
class LineEndRule:
    """Line-end = edge shorter than or equal to ``max_width`` whose both
    endpoints are convex corners. Biased by end-to-end space bins."""

    max_width: float
    space_bins: list[float]
    bias: list[float]

    @classmethod
    def from_dict(cls, d: dict) -> "LineEndRule":
        space_bins = list(d.get("space_bins", []))
        bias = d["bias"]
        bias = [float(bias)] if not isinstance(bias, list) else list(map(float, bias))
        if len(bias) != len(space_bins) + 1:
            raise ValueError(f"line_end.bias needs {len(space_bins) + 1} values, got {len(bias)}")
        if space_bins != sorted(set(space_bins)):
            raise ValueError(f"line_end.space_bins must be strictly ascending: {space_bins}")
        return cls(float(d["max_width"]), space_bins, bias)


@dataclass
class MrcRule:
    min_width: float | None = None
    min_space: float | None = None
    marker_layer: tuple[int, int] | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "MrcRule":
        marker = d.get("marker_layer")
        return cls(d.get("min_width"), d.get("min_space"), _parse_layer(marker) if marker else None)


@dataclass
class RetargetRule:
    name: str
    input_layer: tuple[int, int]
    output_layer: tuple[int, int]
    table: BiasTable | None = None
    line_end: LineEndRule | None = None
    mrc: MrcRule | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "RetargetRule":
        return cls(
            name=d["name"],
            input_layer=_parse_layer(d["input"]),
            output_layer=_parse_layer(d["output"]),
            table=BiasTable.from_dict(d["bias_table"]) if "bias_table" in d else None,
            line_end=LineEndRule.from_dict(d["line_end"]) if "line_end" in d else None,
            mrc=MrcRule.from_dict(d["mrc"]) if "mrc" in d else None,
        )


def load_rules(path: str | Path) -> list[RetargetRule]:
    data = json.loads(Path(path).read_text())
    return [RetargetRule.from_dict(r) for r in data["rules"]]


# --------------------------------------------------------------------------
# Geometry engine
# --------------------------------------------------------------------------

@dataclass
class RuleResult:
    name: str
    output: db.Region
    edge_stats: dict[str, int] = field(default_factory=dict)
    mrc_width: db.EdgePairs | None = None
    mrc_space: db.EdgePairs | None = None


class Retargeter:
    def __init__(self, dbu: float):
        self.dbu = dbu

    def to_dbu(self, um: float) -> int:
        return int(round(um / self.dbu))

    # --- edge classification ----------------------------------------------

    def _space_lt(self, region: db.Region, um: float) -> db.Edges:
        """Edge portions whose facing space (projection) is < um."""
        pairs = region.space_check(self.to_dbu(um), False, db.Metrics.Projection)
        return pairs.edges()

    def _width_lt(self, region: db.Region, um: float) -> db.Edges:
        """Edge portions whose facing width (projection) is < um."""
        pairs = region.width_check(self.to_dbu(um), False, db.Metrics.Projection)
        return pairs.edges()

    @staticmethod
    def _bins(all_edges: db.Edges, nested: list[db.Edges]) -> list[db.Edges]:
        """Turn nested '< bound' edge sets into disjoint bins.

        nested[k] = edges with value < bound[k]  (nested[k] subset of nested[k+1])
        bin 0 = nested[0], bin k = nested[k] - nested[k-1], last = all - nested[-1]
        """
        bins: list[db.Edges] = []
        prev = db.Edges()
        for lt in nested:
            lt = lt & all_edges
            bins.append(lt - prev)
            prev = lt
        bins.append(all_edges - prev)
        return bins

    def line_end_edges(self, region: db.Region, max_width_um: float) -> db.Edges:
        edges = region.edges()
        short = edges.with_length(0, self.to_dbu(max_width_um) + 1, False)
        # KLayout hulls are clockwise -> convex corners report -90 degrees.
        convex_dots = region.corners(-91, -89).sized(1)
        return short.interacting(convex_dots, 2)

    # --- bias application ---------------------------------------------------

    def _bias_shapes(self, edges: db.Edges, bias_um: float) -> tuple[db.Region, db.Region]:
        """Return (grow, shrink) regions for moving edges by bias_um."""
        d = self.to_dbu(abs(bias_um))
        if d == 0 or edges.is_empty():
            return db.Region(), db.Region()
        if bias_um > 0:
            return edges.extended(0, 0, d, 0, True), db.Region()
        return db.Region(), edges.extended(0, 0, 0, d, True)

    # --- main entry -----------------------------------------------------------

    @staticmethod
    def _apply(region: db.Region, grow: db.Region, shrink: db.Region) -> db.Region:
        return ((region + grow).merged() - shrink).merged()

    def run(self, region: db.Region, rule: RetargetRule) -> RuleResult:
        """Two-stage retarget.

        Stage 1: body edges (everything except line ends) are classified on
                 the ORIGINAL shapes and biased by the width x space table.
                 Line-end edges stay where they are.
        Stage 2: line ends are re-found on the stage-1 shapes (so the tip
                 spans the already-biased line width -> no jogs) and moved
                 by the line-end table. Tip-to-tip space is unchanged by
                 stage 1, so measuring it on stage-1 shapes is equivalent.
        """
        region = region.merged()
        stats: dict[str, int] = {}

        orig_line_ends = db.Edges()
        if rule.line_end:
            orig_line_ends = self.line_end_edges(region, rule.line_end.max_width)

        # ---- stage 1: body table ---------------------------------------
        stage1 = region
        if rule.table:
            t = rule.table
            body = region.edges() - orig_line_ends
            w_bins = self._bins(body, [self._width_lt(region, w) for w in t.width_bins])
            s_nested = [self._space_lt(region, s) for s in t.space_bins]
            grow, shrink = db.Region(), db.Region()
            for i, w_edges in enumerate(w_bins):
                for j, edges in enumerate(self._bins(w_edges, s_nested)):
                    b = t.bias[i][j]
                    stats[f"body[w{i},s{j}] bias={b:+g}"] = edges.count()
                    g, s = self._bias_shapes(edges, b)
                    grow += g
                    shrink += s
            stage1 = self._apply(region, grow, shrink)

        # ---- stage 2: line ends -----------------------------------------
        out = stage1
        if rule.line_end:
            le = rule.line_end
            # Same tips as found on the original, now spanning the new width.
            tips = stage1.edges().interacting(orig_line_ends)
            tips = tips.with_length(0, self.to_dbu(le.max_width) * 2, False)
            nested = [self._space_lt(stage1, s) for s in le.space_bins]
            grow, shrink = db.Region(), db.Region()
            for k, edges in enumerate(self._bins(tips, nested)):
                stats[f"line_end[s{k}] bias={le.bias[k]:+g}"] = edges.count()
                g, s = self._bias_shapes(edges, le.bias[k])
                grow += g
                shrink += s
            out = self._apply(stage1, grow, shrink)

        result = RuleResult(rule.name, out, stats)

        if rule.mrc:
            if rule.mrc.min_width:
                result.mrc_width = out.width_check(self.to_dbu(rule.mrc.min_width))
            if rule.mrc.min_space:
                result.mrc_space = out.space_check(self.to_dbu(rule.mrc.min_space))
        return result


# --------------------------------------------------------------------------
# GDS I/O
# --------------------------------------------------------------------------

def run_file(input_gds: str, rules: list[RetargetRule], output_gds: str,
             top_cell: str | None = None) -> list[RuleResult]:
    layout = db.Layout()
    layout.read(input_gds)
    top = layout.cell(top_cell) if top_cell else layout.top_cell()
    if top is None:
        raise SystemExit(f"top cell not found: {top_cell or '(single top cell expected)'}")

    engine = Retargeter(layout.dbu)
    results = []
    for rule in rules:
        li = layout.find_layer(*rule.input_layer)
        src = db.Region(top.begin_shapes_rec(li)) if li is not None else db.Region()
        res = engine.run(src, rule)
        results.append(res)

        out_li = layout.layer(*rule.output_layer)
        top.shapes(out_li).insert(res.output)
        if rule.mrc and rule.mrc.marker_layer:
            mk_li = layout.layer(*rule.mrc.marker_layer)
            for pairs in (res.mrc_width, res.mrc_space):
                if pairs is not None:
                    top.shapes(mk_li).insert(pairs.polygons(1))

    layout.write(output_gds)
    return results


def format_report(results: list[RuleResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"[{r.name}] output polygons: {r.output.count()}")
        for key, n in r.edge_stats.items():
            lines.append(f"  {key:<32} edges: {n}")
        if r.mrc_width is not None:
            lines.append(f"  MRC min_width violations: {r.mrc_width.count()}")
        if r.mrc_space is not None:
            lines.append(f"  MRC min_space violations: {r.mrc_space.count()}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("input", help="input GDS")
    ap.add_argument("rules", help="rule deck (JSON)")
    ap.add_argument("-o", "--output", required=True, help="output GDS")
    ap.add_argument("--top", help="top cell name (default: the single top cell)")
    ap.add_argument("--report", help="write text report to this file")
    args = ap.parse_args(argv)

    results = run_file(args.input, load_rules(args.rules), args.output, args.top)
    report = format_report(results)
    print(report)
    if args.report:
        Path(args.report).write_text(report + "\n")
    violations = sum(
        p.count() for r in results for p in (r.mrc_width, r.mrc_space) if p is not None
    )
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
