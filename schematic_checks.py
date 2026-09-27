"""Independent saved KiCad XML/schematic readback; never edits electrical content."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET

import yaml

from .connectivity import _parse_sexpression, _children, _property
from .debug import span


def _endpoint(*, item: dict) -> str:
    return f"{item['reference']}.{item['pin_number']}"


def compare_saved_block_titles(*, schematic: str, titles: list[dict]) -> list[dict]:
    """Read back ordinary top-level text, never net labels or symbol fields."""
    annotations = _children(_parse_sexpression(schematic), "text")
    errors = []
    for title in titles:
        matches = [item for item in annotations if item[1] == title["text"]]
        valid = []
        for item in matches:
            at = _children(item, "at")
            effects = _children(item, "effects")
            fonts = _children(effects[0], "font") if effects else []
            sizes = _children(fonts[0], "size") if fonts else []
            bold_setting = _children(fonts[0], "bold") if fonts else []
            is_bold = bool(fonts and ("bold" in fonts[0] or bold_setting == [["bold", "yes"]]))
            justification = _children(effects[0], "justify") if effects else []
            valid.append(bool(at and sizes and fonts and justification
                and all(abs(float(a)-float(b)) <= 1e-6 for a,b in zip(at[0][1:4], (title["x"], title["y"], title["rotation"])))
                and len(at[0]) >= 4
                and all(abs(float(a)-title["size"]) <= 1e-6 for a in sizes[0][1:3])
                and len(sizes[0]) >= 3
                and is_bold == title["bold"]
                and set(justification[0][1:]) == set(title["justify"].split())))
        if len(matches) != 1 or valid != [True]:
            errors.append({"code": "block_title_mismatch", "block": title["block"],
                           "expected": title, "matching_text_count": len(matches), "valid_matches": valid})
    return errors


def compare_saved_schematic(*, parts: list, connectivity: dict, placements: list, schematic: str, netlist: str) -> dict:
    errors = []

    def check(*, code, expected, actual):
        if expected != actual:
            errors.append({"code": code, "expected": expected, "actual": actual})

    xml = ET.fromstring(netlist)
    if xml.tag != "export":
        raise ValueError("Expected a KiCad XML netlist export")
    components = xml.findall("components/comp")
    expected_parts = {p["reference"]: p for p in parts}
    refs = [p.attrib["ref"] for p in components]
    check(code="component_references", expected=sorted(expected_parts), actual=sorted(refs))
    for component in components:
        ref = component.attrib["ref"]
        expected = expected_parts.get(ref)
        if expected:
            for key, tag in (("value", "value"), ("footprint_id", "footprint")):
                check(code=f"{ref}.{key}", expected=expected[key], actual=component.findtext(tag, ""))
    expected_nets = {n["name"]: sorted({_endpoint(item=p) for p in n["endpoints"]}) for n in connectivity["nets"]}
    expected_nc = sorted({_endpoint(item=n["endpoint"]) for n in connectivity["no_connects"]})
    actual_nets = {}
    for net in xml.findall("nets/net"):
        name = net.attrib["name"].removeprefix("/")
        endpoints = sorted({f"{n.attrib['ref']}.{n.attrib['pin']}" for n in net.findall("node") if not n.attrib["ref"].startswith("#")})
        if not endpoints:
            continue
        if name.startswith("unconnected-") and len(endpoints) == 1 and endpoints[0] in expected_nc:
            continue
        actual_nets.setdefault(name, []).extend(endpoints)
    for name in sorted(set(expected_nets) | set(actual_nets)):
        check(code=f"net.{name}", expected=expected_nets.get(name, []), actual=sorted(set(actual_nets.get(name, []))))

    saved = _parse_sexpression(schematic)
    libraries = {item[1]: item for lib in _children(saved, "lib_symbols") for item in _children(lib, "symbol")}
    placed = [item for item in _children(saved, "symbol") if not str(_property(item, "Reference")).startswith("#")]
    check(code="saved_symbol_references", expected=sorted(expected_parts), actual=sorted(_property(item, "Reference") for item in placed))
    expected_placement = {p["reference"]: p["schematic"] for p in placements if not p.get("mechanical_only")}
    pins_at = {}
    for symbol in placed:
        ref = _property(symbol, "Reference")
        at = _children(symbol, "at")[0]
        x, y, rotation = map(float, at[1:4])
        if _children(symbol, "mirror"):
            errors.append({"code": "unsupported_mirrored_symbol", "reference": ref})
        expected = expected_placement.get(ref)
        if expected:
            for label, actual, intended in (("x", x, expected["position_mm"][0]), ("y", y, expected["position_mm"][1]), ("rotation", rotation % 360, expected["rotation_degrees"] % 360)):
                if abs(actual-intended) > 0.000001:
                    errors.append({"code": f"placement.{ref}.{label}", "expected": intended, "actual": actual})
        lib_id = _children(symbol, "lib_id")[0][1]
        if ref in expected_parts:
            check(code=f"symbol_id.{ref}", expected=expected_parts[ref]["symbol_id"], actual=lib_id)
        lib = libraries[lib_id]
        unit = int(_children(symbol, "unit")[0][1])
        angle = math.radians(rotation)
        for body in _children(lib, "symbol"):
            body_unit = int(body[1].rsplit("_", 2)[1])
            if body_unit not in (0, unit):
                continue
            for pin in _children(body, "pin"):
                number = _children(pin, "number")[0][1]
                px, py = map(float, _children(pin, "at")[0][1:3])
                position = (round(x+px*math.cos(angle)+py*math.sin(angle), 6), round(y-px*math.sin(angle)-py*math.cos(angle), 6))
                pins_at.setdefault(position, set()).add(f"{ref}.{number}")
    actual_nc = set()
    markers = []
    for marker in _children(saved, "no_connect"):
        position = tuple(round(float(v), 6) for v in _children(marker, "at")[0][1:3])
        markers.append(position)
        endpoints = pins_at.get(position, set())
        if not endpoints:
            errors.append({"code": "orphan_no_connect", "position": position})
        actual_nc.update(endpoints)
    check(code="explicit_no_connect_endpoints", expected=expected_nc, actual=sorted(actual_nc))
    if any(count > 1 for count in Counter(markers).values()):
        errors.append({"code": "duplicate_no_connect_marker"})
    return {"status": "ready" if not errors else "invalid", "component_count": len(refs),
            "named_net_count": len(expected_nets), "no_connect_count": len(expected_nc), "diagnostics": errors}


def apply_project_metadata(*, bundle_root: Path, worksheet: str, section: str = "schematic") -> dict:
    """CQ-006/CQ-009: ONLY the selected drawing-sheet metadata key may change."""
    if section not in {"schematic", "pcbnew"}:
        raise ValueError("Only schematic/PCB drawing-sheet metadata is authorized")
    root = bundle_root.resolve()
    if worksheet != "libraries/blank.kicad_wks":
        raise ValueError("Only the approved packaged blank worksheet is supported")
    path = (root / worksheet).resolve()
    path.relative_to(root)
    if not path.is_file():
        raise ValueError("Packaged worksheet missing")
    owner = json.loads((root / ".execution-owner.json").read_text())
    if owner.get("owner") != "oomlout_kicad_automation_version_5":
        raise ValueError("Refusing metadata update on an unowned project")
    with span(scope="executor.project_metadata", setting=f"{section}.page_layout_descr_file", value=worksheet):
        project = root / "working.kicad_pro"
        data = json.loads(project.read_text(encoding="utf-8"))
        previous = data.setdefault(section, {}).get("page_layout_descr_file")
        data[section]["page_layout_descr_file"] = worksheet
        temporary = project.with_suffix(".kicad_pro.metadata.tmp")
        if temporary.exists():
            raise ValueError("Refusing existing metadata temporary file")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
        temporary.replace(project)
    return {"setting": f"{section}.page_layout_descr_file", "previous": previous, "value": worksheet}


def verify_pcb_worksheet(*, bundle_root: Path, profile: dict) -> dict:
    """Read-only saved-setting gate, not a substitute for native/visual proof."""
    result = {"status": "ready", "pcb_borderless": profile.get("pcb_borderless", False), "diagnostics": []}
    if result["pcb_borderless"]:
        try:
            project = json.loads((bundle_root / "working.kicad_pro").read_text(encoding="utf-8"))
            value = project.get("pcbnew", {}).get("page_layout_descr_file")
            result["worksheet"] = value
            if value != "libraries/blank.kicad_wks" or not (bundle_root / value).is_file():
                raise ValueError("Saved PCB worksheet does not match packaged blank worksheet")
        except Exception as exc:
            result.update(status="invalid", diagnostics=[{"code": "pcb_worksheet_mismatch", "message": str(exc)}])
    report = bundle_root / "reports/pcb_worksheet_verification.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result


def verify_saved_schematic(*, bundle_root: Path) -> dict:
    """Use a freshly generated native KiCad XML export; persist full mismatches."""
    root = bundle_root
    report = root / "reports" / "schematic_verification.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    try:
        def read(relative):
            return yaml.safe_load((root / relative).read_text(encoding="utf-8"))
        result = compare_saved_schematic(parts=read("schematic/parts.yaml")["parts"],
            connectivity=read("schematic/nets.yaml"), placements=read("schematic/placement.yaml")["placements"],
            schematic=(root / "working.kicad_sch").read_text(encoding="utf-8"),
            netlist=(root / "reports/schematic_readback.xml").read_text(encoding="utf-8"))
        layout = read("schematic/layout.yaml")
        titles = layout.get("titles", [])
        result["diagnostics"].extend(compare_saved_block_titles(
            schematic=(root / "working.kicad_sch").read_text(encoding="utf-8"), titles=titles))
        result["block_title_count"] = len(titles)
        if result["diagnostics"]:
            result["status"] = "invalid"
        if layout.get("worksheet"):
            project = json.loads((root / "working.kicad_pro").read_text(encoding="utf-8"))
            if project.get("schematic", {}).get("page_layout_descr_file") != layout["worksheet"]:
                result["diagnostics"].append({"code": "worksheet_setting_mismatch"})
                result["status"] = "invalid"
        result["saved_sha256"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                  for name in ("working.kicad_sch", "working.kicad_pro", "reports/schematic_readback.xml")}
    except Exception as exc:
        result = {"status": "invalid", "diagnostics": [{"code": "readback_unavailable", "message": str(exc)}]}
    report.write_text(json.dumps(result, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    return result
