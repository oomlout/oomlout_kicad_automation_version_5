"""Pure conservative one-row-per-block packing; no backend calls or file writes."""
from __future__ import annotations

import math
from pathlib import Path

from .connectivity import _parse_sexpression, _children

PAGE_MM = {"A0": (1189, 841), "A1": (841, 594), "A2": (594, 420), "A3": (420, 297), "A4": (297, 210)}


def _snap(*, value: float) -> float:
    return round(math.ceil(value / 2.54) * 2.54, 6)


def symbol_envelope(*, path: Path, value: str, net_names: list[str], stub_mm: float,
                    connected_pins: set[str], stagger_mm: float) -> dict:
    """Conservative envelope includes body graphics, pins, visible fields and labels.

    Text uses 1.5 font widths per character; all graphic control points and
    circle extents are included. Vertical pin labels get separate stub lanes.
    """
    root = _parse_sexpression(path.read_text(encoding="utf-8"))
    xs, ys = [-5.08, 5.08], [-5.08, 5.08]
    text_height = 0.0

    def visit(node):
        nonlocal text_height
        if not isinstance(node, list) or not node:
            return
        tag = node[0]
        if tag == "property":
            # Visible reference/value fields are deliberately placed horizontally
            # above the complete symbol+stub region through a Konnect call.
            return
        if tag in {"at", "start", "end", "mid", "center", "xy"} and len(node) >= 3:
            xs.append(float(node[1])); ys.append(float(node[2]))
        if tag in {"text", "property"}:
            label = value if tag == "property" and node[1] == "Value" else str(node[2] if tag == "property" else node[1])
            effects = _children(node, "effects")
            font = _children(effects[0], "font") if effects else []
            size = _children(font[0], "size") if font else []
            width, height = (map(float, size[0][1:3]) if size else (1.27, 1.27))
            extent = len(label) * width * 1.5
            at = _children(node, "at")
            x, y = (map(float, at[0][1:3]) if at else (0.0, 0.0))
            justify = _children(effects[0], "justify") if effects else []
            alignment = justify[0][1:] if justify else []
            if "left" in alignment:
                xs.extend([x, x+extent])
            elif "right" in alignment:
                xs.extend([x-extent, x])
            else:
                xs.extend([x-extent/2, x+extent/2])
            ys.extend([y-height*2, y+height*2])
            if at and len(at[0]) > 3 and float(at[0][3]) != 0:
                ys.extend([y-extent, y+extent])
            text_height = max(text_height, height * 2)
        if tag == "circle":
            center, radius = _children(node, "center"), _children(node, "radius")
            if center and radius:
                x, y, r = float(center[0][1]), float(center[0][2]), float(radius[0][1])
                xs.extend([x-r, x+r]); ys.extend([y-r, y+r])
        for child in node[1:]:
            if isinstance(child, list):
                visit(child)

    visit(root)
    symbol = _children(root, "symbol")[0]
    vertical_pins = {90: {}, 270: {}}
    pin_geometry = {}
    for body in _children(symbol, "symbol"):
        for pin in _children(body, "pin"):
            number = str(_children(pin, "number")[0][1])
            at = _children(pin, "at")[0]
            direction = int(float(at[3])) % 360
            pin_geometry[number] = [float(at[1]), float(at[2]), direction]
            if direction in vertical_pins and number in connected_pins:
                vertical_pins[direction].setdefault(float(at[1]), []).append(number)
    stub_lengths = {}
    extra = {}
    for direction, columns in vertical_pins.items():
        for index, x in enumerate(sorted(columns)):
            length = stub_mm + (len(columns)-index-1)*stagger_mm
            for number in columns[x]:
                stub_lengths[number] = round(length, 6)
        extra[direction] = max(0, len(columns)-1)*stagger_mm
    label_width = max((len(name) * 1.27 * 1.5 for name in net_names), default=0)
    horizontal = label_width + stub_mm + 5.08
    vertical = stub_mm + max(text_height, 5.08) + 2.54
    left, right = min(xs)-horizontal, max(xs)+horizontal
    top, bottom = -max(ys)-vertical-extra[270], -min(ys)+vertical+extra[90]
    field_y = top-5.08
    for prop in _children(symbol, "property"):
        if prop[1] not in {"Reference", "Value"}:
            continue
        extent = (len(value) if prop[1] == "Value" else 5)*1.27*1.5
        effects = _children(prop, "effects")
        justify = _children(effects[0], "justify") if effects else []
        align = justify[0][1:] if justify else []
        a, b = (0, extent) if "left" in align else (-extent, 0) if "right" in align else (-extent/2, extent/2)
        left, right = min(left, a-2.54), max(right, b+2.54)
    return {"bounds": [left, right, field_y-7.62, bottom],
            "horizontal_power_stub_mm": _snap(value=stub_mm+label_width),
            "stub_lengths_mm": stub_lengths,
            "pin_geometry": pin_geometry,
            "fields": {"Reference": [0, field_y-5.08], "Value": [0, field_y]}}


def block_titles(*, rows: list[dict], settings: dict) -> list[dict]:
    """Reserve non-electrical headings in the whitespace above each block row.

    Bounds use two font heights and 1.5 font widths per character, including
    bold stroke allowance. Fail closed when edited spacing cannot fit a title.
    The component margin stays unchanged; headings use its upper whitespace.
    """
    size = float(settings.get("title_size_mm", 2.54))
    clearance = float(settings.get("title_clearance_mm", 5.08))
    if not all(math.isfinite(x) and x > 0 for x in (size, clearance)):
        raise ValueError("Block title size and clearance must be positive finite millimetres")
    names = settings.get("titles", {})
    titles = []
    previous_bottom = 0.0
    for row in rows:
        text = names.get(row["block"], row["block"].replace("_", " ").upper())
        if not isinstance(text, str) or not text.strip() or "\n" in text or "\r" in text:
            raise ValueError(f"Block title must be nonempty single-line text: {row['block']}")
        x, _, top, bottom = row["bounds_mm"]
        y = round(top-clearance, 6)
        bounds = [x, round(x+len(text)*size*1.5, 6), round(y-2*size, 6), y]
        if bounds[2] < previous_bottom+clearance-1e-6:
            raise ValueError(f"Insufficient whitespace above block title: {row['block']}; increase margin_mm or row_gap_mm")
        titles.append({"block": row["block"], "text": text, "x": x, "y": y,
                       "size": size, "rotation": 0, "bold": True,
                       "justify": "left bottom", "bounds_mm": bounds})
        previous_bottom = bottom
    return titles


def apply_block_rows(*, board: dict, connectivity: dict, geometry: dict, inspections: dict) -> dict:
    """Stable declaration order, one shared Y per block, explicit envelope gaps."""
    settings = board["settings"]["schematic_layout"]
    margin = float(settings["margin_mm"])
    gap = float(settings["part_gap_mm"])
    row_gap = float(settings["row_gap_mm"])
    stagger = float(settings["vertical_pin_stagger_mm"])
    if not all(math.isfinite(x) and x > 0 for x in (margin, gap, row_gap, stagger)):
        raise ValueError("Row layout margins must be positive finite millimetres")
    parts = {part["assigned_reference"]: part for part in connectivity["parts"]}
    nets = {ref: [] for ref in parts}
    connected_pins = {ref: set() for ref in parts}
    for net in connectivity["nets"]:
        for endpoint in net["endpoints"]:
            nets[endpoint["reference"]].append(net["name"])
            connected_pins[endpoint["reference"]].add(str(endpoint["pin_number"]))
    records = [p for p in geometry["placements"] if not p["mechanical_only"]]
    block_names = [b["name"] for b in board["blocks"]]
    if any(p["block"] not in block_names for p in records):
        raise ValueError("Every row-layout part must belong to a declared block")
    envelopes = {}
    for record in records:
        part = parts[record["reference"]]
        if record["schematic"] and record["schematic"]["rotation_degrees"] != 0:
            raise ValueError("Block-row layout currently requires unrotated symbols")
        inspection = inspections[part["source_id"]]
        envelopes[record["reference"]] = symbol_envelope(
            path=Path(inspection["valid_assets"]["symbol"]["_path"]),
            value=str(inspection["cache"].get("value") or inspection["working"].get("value") or ""),
            net_names=nets[record["reference"]], stub_mm=float(board["settings"].get("schematic_stub_length_mm", 5.08)),
            connected_pins=connected_pins[record["reference"]], stagger_mm=stagger,
        )
    cursor_y = margin
    rows = []
    for name in block_names:
        row = [record for record in records if record["block"] == name]
        if not row:
            continue
        top = min(envelopes[p["reference"]]["bounds"][2] for p in row)
        bottom = max(envelopes[p["reference"]]["bounds"][3] for p in row)
        y = _snap(value=cursor_y-top)
        cursor_x = margin
        for column, record in enumerate(row):
            box = envelopes[record["reference"]]
            left, right, lo, hi = box["bounds"]
            x = _snap(value=cursor_x-left)
            record["schematic"] = {"grid": [round(x/2.54), round(y/2.54)], "position_mm": [x, y],
                                   "rotation_degrees": 0, "layout_kind": "block_rows", "column": column,
                                   "envelope_mm": [round(x+left,6), round(x+right,6), round(y+lo,6), round(y+hi,6)]}
            record["schematic"]["field_placements"] = {name: {"x": round(x+offset[0],6), "y": round(y+offset[1],6), "rotation": 0, "hide": False} for name, offset in box["fields"].items()}
            record["schematic"]["pin_stub_lengths_mm"] = box["stub_lengths_mm"]
            record["schematic"]["pin_geometry"] = box["pin_geometry"]
            record["schematic"]["horizontal_power_stub_mm"] = box["horizontal_power_stub_mm"]
            cursor_x = x+right+gap
        rows.append({"block": name, "references": [p["reference"] for p in row], "y_mm": y,
                     "bounds_mm": [margin, cursor_x-gap, y+top, y+bottom]})
        cursor_y = y+bottom+row_gap
    titles = block_titles(rows=rows, settings=settings)
    width = max(item["bounds_mm"][1] for item in rows+titles)+margin
    height = rows[-1]["bounds_mm"][3]+margin
    # No clipped output: use a supported sheet at least as large as the packing.
    page = next((p for p in ("A4", "A3", "A2", "A1", "A0") if PAGE_MM[p][0] >= width and PAGE_MM[p][1] >= height), None)
    if page is None:
        raise ValueError(f"Row layout {width:.1f} x {height:.1f} mm exceeds A0; adjust explicit spacing")
    return {"mode": "block_rows", "page": page, "rows": rows, "titles": titles, "settings": settings}
