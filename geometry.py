"""Pure deterministic block, board, and placement geometry."""

from __future__ import annotations

import math
import re
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any
from .prototype import board_profile


SCHEMATIC_GRID_MM = Decimal("2.54")
STAGING_MARGIN_MM = 10.0
STAGING_GAP_MM = 3.0
UNKNOWN_BOUNDS = (-10.0, 10.0, -10.0, 10.0)
_NATURAL_RE = re.compile(r"(\d+)")


def canonical_number(value: int | float | Decimal) -> int | float:
    decimal = Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    if decimal == 0:
        return 0
    if decimal == decimal.to_integral():
        return int(decimal)
    return float(decimal)


def _natural(value: str) -> tuple[Any, ...]:
    return tuple(
        int(piece) if piece.isdigit() else piece.lower()
        for piece in _NATURAL_RE.split(value)
    )


class _Findings:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, code: str, message: str, *, severity: str = "error", **details: Any) -> str:
        finding_id = f"G{len(self.items) + 1:04d}"
        self.items.append(
            {
                "id": finding_id,
                "code": code,
                "severity": severity,
                "message": message,
                "details": details,
            }
        )
        return finding_id


def _block_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name")
    return value if isinstance(value, str) else None


def _axis_pair(
    item: dict[str, Any], x_key: str, y_key: str, *, absent_zero: bool
) -> tuple[float | int, float | int] | None:
    has_x = x_key in item
    has_y = y_key in item
    if not has_x and not has_y:
        return (0, 0) if absent_zero else None
    if has_x != has_y:
        return None
    return item[x_key], item[y_key]


def resolve_block_transforms(board: dict[str, Any]) -> dict[str, Any]:
    findings = _Findings()
    blocks = board.get("blocks", [])
    by_name: dict[str, dict[str, Any]] = {}
    records: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for block in blocks:
        name = block.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in by_name:
            finding = findings.add("duplicate_block_name", f"Block '{name}' is duplicated", block=name)
            records[name] = {
                "name": name,
                "resolution_status": "blocked",
                "finding_ids": [finding],
                "resolved": True,
            }
            duplicates.add(name)
        else:
            by_name[name] = block

    states: dict[str, str] = {}

    def resolve(name: str, trail: tuple[str, ...] = ()) -> dict[str, Any]:
        if name in records and records[name].get("resolved"):
            return records[name]
        if states.get(name) == "visiting":
            cycle = (*trail, name)
            finding = findings.add(
                "block_parent_cycle",
                "Block parent graph contains a cycle",
                cycle=list(cycle),
            )
            for member in set(cycle):
                records[member] = {
                    "name": member,
                    "resolution_status": "blocked",
                    "finding_ids": [finding],
                    "resolved": True,
                }
            return records[name]
        block = by_name.get(name)
        if block is None:
            finding = findings.add(
                "missing_block_parent", f"Block '{name}' does not exist", block=name
            )
            return {
                "name": name,
                "resolution_status": "blocked",
                "finding_ids": [finding],
                "resolved": True,
            }
        states[name] = "visiting"
        finding_ids: list[str] = []
        parent_name = _block_name(block.get("parent"))
        parent = None
        if parent_name is not None:
            if parent_name not in by_name:
                finding_ids.append(
                    findings.add(
                        "missing_block_parent",
                        f"Parent block '{parent_name}' for '{name}' does not exist",
                        block=name,
                        parent=parent_name,
                    )
                )
            else:
                parent = resolve(parent_name, (*trail, name))
                finding_ids.extend(parent.get("finding_ids", []))

        schematic = _axis_pair(block, "schematic_x", "schematic_y", absent_zero=True)
        pcb = _axis_pair(block, "pcb_x_mm", "pcb_y_mm", absent_zero=True)
        if schematic is None or any(not isinstance(value, int) or isinstance(value, bool) for value in schematic):
            finding_ids.append(
                findings.add(
                    "invalid_block_schematic_origin",
                    f"Block '{name}' schematic origin must be two integers or absent",
                    block=name,
                )
            )
        if pcb is None or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            for value in (pcb or ())
        ):
            finding_ids.append(
                findings.add(
                    "invalid_block_pcb_origin",
                    f"Block '{name}' PCB origin must be two finite numbers or absent",
                    block=name,
                )
            )
        if parent is not None and parent.get("resolution_status") != "ready":
            finding_ids.extend(parent.get("finding_ids", []))
        if finding_ids:
            record = {
                "name": name,
                "parent": parent_name,
                "resolution_status": "blocked",
                "finding_ids": list(dict.fromkeys(finding_ids)),
                "resolved": True,
            }
        else:
            parent_schematic = parent["schematic_grid"] if parent else [0, 0]
            parent_pcb = parent["pcb_local_mm"] if parent else [0, 0]
            record = {
                "name": name,
                "parent": parent_name,
                "schematic_grid": [
                    int(parent_schematic[0] + schematic[0]),
                    int(parent_schematic[1] + schematic[1]),
                ],
                "pcb_local_mm": [
                    canonical_number(parent_pcb[0] + pcb[0]),
                    canonical_number(parent_pcb[1] + pcb[1]),
                ],
                "resolution_status": "ready",
                "finding_ids": [],
                "resolved": True,
            }
        records[name] = record
        states[name] = "done"
        return record

    for name in by_name:
        if name in duplicates:
            continue
        if name not in records or not records[name].get("resolved"):
            resolve(name)
    public = []
    for block in blocks:
        name = block.get("name")
        record = dict(records.get(name, {"name": name, "resolution_status": "blocked", "finding_ids": []}))
        record.pop("resolved", None)
        public.append(record)
    return {"blocks": public, "by_name": records, "findings": findings.items}


def board_geometry(board: dict[str, Any]) -> dict[str, Any]:
    actions = [item for item in board.get("board_actions", []) if item.get("action") == "board_oobb"]
    if len(actions) != 1:
        return {
            "status": "blocked",
            "finding": {
                "code": "board_geometry_count",
                "message": "Exactly one board_oobb action is required",
                "details": {"count": len(actions)},
            },
        }
    action = actions[0]
    width_units = action.get("width_units")
    height_units = action.get("height_units")
    if (
        not isinstance(width_units, int)
        or isinstance(width_units, bool)
        or width_units < 2
        or not isinstance(height_units, int)
        or isinstance(height_units, bool)
        or height_units < 2
    ):
        return {
            "status": "blocked",
            "finding": {
                "code": "unsupported_oobb_size",
                "message": "OOBB width_units and height_units must both be integers of at least 2",
                "details": {"width_units": width_units, "height_units": height_units},
            },
        }
    width = 15 * width_units - 1
    height = 15 * height_units - 1
    holes = [[7, 7], [width - 7, 7], [width - 7, height - 7], [7, height - 7]]
    return {
        "status": "ready",
        "width_units": width_units,
        "height_units": height_units,
        "width_mm": width,
        "height_mm": height,
        "outline_vertices_mm": [[0, 0], [width, 0], [width, height], [0, height]],
        "hole_centers_mm": holes,
        "layer": "Edge.Cuts",
        "source_action_id": action.get("_creation_key"),
        **board_profile(settings=board.get("settings")),
    }


def _transformed_bounds(
    bounds: tuple[float, float, float, float], rotation: float, side: str
) -> tuple[float, float, float, float]:
    min_x, max_x, min_y, max_y = bounds
    corners = [(min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y)]
    radians = math.radians(rotation)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    transformed = []
    for x_value, y_value in corners:
        if side == "back":
            x_value = -x_value
        transformed.append(
            (
                x_value * cosine - y_value * sine,
                x_value * sine + y_value * cosine,
            )
        )
    return (
        canonical_number(min(point[0] for point in transformed)),
        canonical_number(max(point[0] for point in transformed)),
        canonical_number(min(point[1] for point in transformed)),
        canonical_number(max(point[1] for point in transformed)),
    )


def resolve_geometry(
    board: dict[str, Any],
    resolved_parts: list[dict[str, Any]],
    footprint_bounds: dict[str, tuple[float, float, float, float]] | None = None,
) -> dict[str, Any]:
    """Resolve final schematic/PCB placements, staging, blocks and board geometry."""

    bounds_by_source = footprint_bounds or {}
    block_result = resolve_block_transforms(board)
    board_result = board_geometry(board)
    findings = _Findings()
    findings.items.extend(block_result["findings"])
    if board_result["status"] != "ready":
        finding = board_result["finding"]
        findings.add(finding["code"], finding["message"], **finding["details"])
    source_parts = {part.get("_creation_key"): part for part in board.get("parts", [])}
    block_records = block_result["by_name"]
    placements: list[dict[str, Any]] = []
    staged: list[dict[str, Any]] = []
    for resolved in resolved_parts:
        source = source_parts.get(resolved.get("creation_key"), {})
        finding_ids = list(resolved.get("finding_ids", []))
        block_name = _block_name(source.get("block"))
        block = block_records.get(block_name) if block_name else None
        if block_name and (block is None or block.get("resolution_status") != "ready"):
            finding_ids.append(
                findings.add(
                    "part_block_unresolved",
                    "Part placement depends on an unresolved block",
                    instance_id=resolved.get("instance_id"),
                    block=block_name,
                )
            )
        schematic = _axis_pair(source, "schematic_x", "schematic_y", absent_zero=False)
        if board.get("settings", {}).get("schematic_layout", {}).get("mode") == "block_rows":
            schematic = (0, 0)  # Actual positions are packed from symbol envelopes by the compiler.
        schematic_record = None
        if not resolved.get("mechanical_only"):
            if schematic is None:
                code = (
                    "missing_schematic_placement"
                    if "schematic_x" not in source and "schematic_y" not in source
                    else "incomplete_schematic_placement"
                )
                finding_ids.append(
                    findings.add(
                        code,
                        "Electrical part needs both integer schematic coordinates",
                        instance_id=resolved.get("instance_id"),
                    )
                )
            elif any(not isinstance(value, int) or isinstance(value, bool) for value in schematic):
                finding_ids.append(
                    findings.add(
                        "invalid_schematic_coordinate",
                        "Schematic coordinates must be integers",
                        instance_id=resolved.get("instance_id"),
                    )
                )
            elif not isinstance(source.get("schematic_rotation", 0), (int, float)) or isinstance(
                source.get("schematic_rotation", 0), bool
            ) or not math.isfinite(source.get("schematic_rotation", 0)):
                finding_ids.append(
                    findings.add(
                        "invalid_schematic_rotation",
                        "Schematic rotation must be finite",
                        instance_id=resolved.get("instance_id"),
                    )
                )
            elif block_name is None or (block and block.get("resolution_status") == "ready"):
                origin = block["schematic_grid"] if block else [0, 0]
                grid = [int(origin[0] + schematic[0]), int(origin[1] + schematic[1])]
                schematic_record = {
                    "grid": grid,
                    "position_mm": [
                        canonical_number(Decimal(grid[0]) * SCHEMATIC_GRID_MM),
                        canonical_number(Decimal(grid[1]) * SCHEMATIC_GRID_MM),
                    ],
                    "rotation_degrees": canonical_number(source.get("schematic_rotation", 0)),
                }

        pcb = _axis_pair(source, "pcb_x_mm", "pcb_y_mm", absent_zero=False)
        pcb_record = None
        needs_staging = pcb is None and "pcb_x_mm" not in source and "pcb_y_mm" not in source
        if pcb is None and not needs_staging:
            finding_ids.append(
                findings.add(
                    "incomplete_pcb_placement",
                    "PCB placement must contain both coordinates or neither",
                    instance_id=resolved.get("instance_id"),
                )
            )
        elif pcb is not None:
            if any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in pcb
            ):
                finding_ids.append(
                    findings.add(
                        "invalid_pcb_coordinate",
                        "PCB coordinates must be finite numbers",
                        instance_id=resolved.get("instance_id"),
                    )
                )
            elif not isinstance(source.get("pcb_rotation", 0), (int, float)) or isinstance(
                source.get("pcb_rotation", 0), bool
            ) or not math.isfinite(source.get("pcb_rotation", 0)):
                finding_ids.append(
                    findings.add(
                        "invalid_pcb_rotation",
                        "PCB rotation must be finite",
                        instance_id=resolved.get("instance_id"),
                    )
                )
            elif source.get("pcb_side", "front") not in {"front", "back"}:
                finding_ids.append(
                    findings.add(
                        "invalid_pcb_side",
                        "PCB side must be front or back",
                        instance_id=resolved.get("instance_id"),
                    )
                )
            elif block_name is None or (block and block.get("resolution_status") == "ready"):
                origin = block["pcb_local_mm"] if block else [0, 0]
                pcb_record = {
                    "anchor_mm": [
                        canonical_number(origin[0] + pcb[0]),
                        canonical_number(origin[1] + pcb[1]),
                    ],
                    "rotation_degrees": canonical_number(source.get("pcb_rotation", 0)),
                    "side": source.get("pcb_side", "front"),
                    "placement_kind": "authored",
                }
        record = {
            "instance_id": resolved.get("instance_id"),
            "reference": resolved.get("assigned_reference"),
            "source_id": resolved.get("source_id"),
            "mechanical_only": resolved.get("mechanical_only", False),
            "block": block_name,
            "schematic": schematic_record,
            "pcb": pcb_record,
            "resolution_status": "ready" if resolved.get("resolution_status") == "ready" else "unresolved",
            "finding_ids": list(dict.fromkeys(finding_ids)),
        }
        if needs_staging and resolved.get("resolution_status") == "ready":
            staged.append(record)
        placements.append(record)

    if staged:
        if board_result["status"] != "ready":
            for record in staged:
                record["finding_ids"].append(
                    findings.add(
                        "staging_requires_board_geometry",
                        "Missing PCB positions cannot be staged without valid board geometry",
                        instance_id=record["instance_id"],
                    )
                )
        else:
            cursor_x = 0.0
            cursor_y = float(board_result["height_mm"] + STAGING_MARGIN_MM)
            row_height = 0.0
            wrap_width = float(max(board_result["width_mm"], 30))
            for record in sorted(staged, key=lambda item: _natural(str(item.get("reference") or ""))):
                source_id = str(record["source_id"])
                raw_bounds = bounds_by_source.get(source_id)
                if raw_bounds is None:
                    raw_bounds = UNKNOWN_BOUNDS
                    record["finding_ids"].append(
                        findings.add(
                            "unknown_staging_bounds",
                            "A 20x20 mm placeholder bound was used for staging",
                            severity="warning",
                            instance_id=record["instance_id"],
                            source_id=source_id,
                        )
                    )
                source = source_parts.get(
                    next(
                        (
                            part.get("creation_key")
                            for part in resolved_parts
                            if part.get("instance_id") == record["instance_id"]
                        ),
                        None,
                    ),
                    {},
                )
                rotation = source.get("pcb_rotation", 0)
                side = source.get("pcb_side", "front")
                transformed = _transformed_bounds(tuple(raw_bounds), rotation, side)
                width = float(transformed[1] - transformed[0])
                height = float(transformed[3] - transformed[2])
                if cursor_x > 0 and cursor_x + width > wrap_width:
                    cursor_x = 0.0
                    cursor_y += row_height + STAGING_GAP_MM
                    row_height = 0.0
                anchor = [
                    canonical_number(cursor_x - transformed[0]),
                    canonical_number(cursor_y - transformed[2]),
                ]
                record["pcb"] = {
                    "anchor_mm": anchor,
                    "rotation_degrees": canonical_number(rotation),
                    "side": side,
                    "placement_kind": "staged",
                    "transformed_bounds_mm": list(transformed),
                }
                cursor_x += width + STAGING_GAP_MM
                row_height = max(row_height, height)
    return {
        "status": (
            "partial"
            if any(item.get("severity") == "error" for item in findings.items)
            else "ready"
        ),
        "blocks": block_result["blocks"],
        "board": board_result,
        "placements": placements,
        "findings": findings.items,
    }
