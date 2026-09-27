"""Read-only footprint-bound extraction for deterministic PCB staging."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

from .connectivity import SymbolParseError, _children, _parse_sexpression
from .geometry import canonical_number


def _numbers(expression: list[Any], tag: str) -> list[float] | None:
    items = _children(expression, tag)
    if not items:
        return None
    try:
        return [float(value) for value in items[0][1:]]
    except (TypeError, ValueError):
        return None


def _layer(expression: list[Any]) -> str | None:
    items = _children(expression, "layer")
    return str(items[0][1]) if items and len(items[0]) >= 2 else None


def _rotated_rectangle(
    center_x: float, center_y: float, width: float, height: float, degrees: float
) -> list[tuple[float, float]]:
    radians = math.radians(degrees)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    points = []
    for x_value, y_value in (
        (-width / 2, -height / 2),
        (-width / 2, height / 2),
        (width / 2, -height / 2),
        (width / 2, height / 2),
    ):
        points.append(
            (
                center_x + x_value * cosine - y_value * sine,
                center_y + x_value * sine + y_value * cosine,
            )
        )
    return points


def _graphic_points(expression: list[Any]) -> list[tuple[float, float]]:
    tag = expression[0] if expression else None
    points: list[tuple[float, float]] = []
    if tag in {"fp_line", "fp_rect"}:
        for point_tag in ("start", "end"):
            values = _numbers(expression, point_tag)
            if values and len(values) >= 2:
                points.append((values[0], values[1]))
    elif tag == "fp_circle":
        center = _numbers(expression, "center")
        end = _numbers(expression, "end")
        if center and end and len(center) >= 2 and len(end) >= 2:
            radius = math.dist(center[:2], end[:2])
            points.extend(
                [
                    (center[0] - radius, center[1] - radius),
                    (center[0] + radius, center[1] + radius),
                ]
            )
    elif tag == "fp_poly":
        point_sets = _children(expression, "pts")
        if point_sets:
            for item in _children(point_sets[0], "xy"):
                if len(item) >= 3:
                    try:
                        points.append((float(item[1]), float(item[2])))
                    except ValueError:
                        pass
    elif tag == "fp_arc":
        # Start/mid/end are all retained.  Exact arbitrary-arc extrema are not
        # inferred; generated courtyards use lines and circles in supported v1 assets.
        for point_tag in ("start", "mid", "end"):
            values = _numbers(expression, point_tag)
            if values and len(values) >= 2:
                points.append((values[0], values[1]))
    return points


def footprint_bounds_text(text: str) -> tuple[int | float, int | float, int | float, int | float]:
    root = _parse_sexpression(text)
    if not root or root[0] not in {"footprint", "module"}:
        raise SymbolParseError("expected a KiCad footprint root")
    courtyard: list[tuple[float, float]] = []
    body: list[tuple[float, float]] = []
    pads: list[tuple[float, float]] = []
    for expression in root[1:]:
        if not isinstance(expression, list) or not expression:
            continue
        tag = expression[0]
        if tag in {"fp_line", "fp_rect", "fp_circle", "fp_poly", "fp_arc"}:
            points = _graphic_points(expression)
            layer = _layer(expression)
            if layer in {"F.CrtYd", "B.CrtYd"}:
                courtyard.extend(points)
            elif layer in {"F.Fab", "B.Fab", "F.SilkS", "B.SilkS"}:
                body.extend(points)
        elif tag == "pad":
            at = _numbers(expression, "at") or [0.0, 0.0, 0.0]
            size = _numbers(expression, "size")
            if size and len(size) >= 2:
                pads.extend(
                    _rotated_rectangle(
                        at[0],
                        at[1],
                        size[0],
                        size[1],
                        at[2] if len(at) >= 3 else 0.0,
                    )
                )
    points = courtyard or [*body, *pads]
    if not points:
        raise SymbolParseError("footprint has no supported geometry for bounds")
    return (
        canonical_number(min(point[0] for point in points)),
        canonical_number(max(point[0] for point in points)),
        canonical_number(min(point[1] for point in points)),
        canonical_number(max(point[1] for point in points)),
    )


def footprint_bounds_file(path: str | Path) -> tuple[int | float, int | float, int | float, int | float]:
    return footprint_bounds_text(Path(path).read_text(encoding="utf-8"))


def bounds_from_inspections(
    inspections: dict[str, dict[str, Any]],
) -> tuple[dict[str, tuple[int | float, int | float, int | float, int | float]], list[dict[str, Any]]]:
    result = {}
    diagnostics = []
    for source_id, inspection in inspections.items():
        kind = inspection.get("selected_footprint_kind")
        asset = inspection.get("valid_assets", {}).get(kind) if kind else None
        if asset is None:
            continue
        try:
            result[source_id] = footprint_bounds_file(asset["_path"])
        except (OSError, SymbolParseError) as exc:
            diagnostics.append(
                {
                    "code": "footprint_bounds_unresolved",
                    "message": f"Could not resolve footprint bounds for {source_id}: {exc}",
                    "details": {"source_id": source_id, "asset_kind": kind},
                }
            )
    return result, diagnostics
