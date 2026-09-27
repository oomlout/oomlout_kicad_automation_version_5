"""Read-only semantic source/placed-footprint comparison for KiCad 10.

Placement coordinates in saved children are footprint-local; side mirrors Y.
Pad angles include the instance angle. Text can be kept upright (180 degrees).
Models remain definition-local under native FOOTPRINT::Flip/SetOrientation.
Identifiers, instance nets and explicit Reference/Value values are not library
fidelity differences. Unknown source children are compared, never discarded.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import yaml
from pathlib import Path

from .connectivity import _children, _parse_sexpression

NUMERIC_TOLERANCE_MM = 0.000001  # one KiCad internal nanometre
PROTOTYPE_PAD_PROPERTIES = {"pad_prop_heatsink", "pad_prop_mechanical"}
_INSTANCE_ROOT = {"at", "layer", "uuid", "path", "sheetname", "sheetfile", "version",
                  "generator", "generator_version", "embedded_fonts", "locked"}


def _canonical(node):
    if not isinstance(node, list):
        try: return float(node)
        except (ValueError, TypeError): return node
    tag = node[0]
    atoms, children = [], []
    for value in node[1:]:
        if isinstance(value, list):
            if value[0] in {"uuid", "tstamp", "net"}: continue
            if value in (["unlocked", "no"], ["line_spacing", "0"],
                         ["hide", "no"], ["remove_unused_layers", "no"]): continue
            children.append(_canonical(value))
        else: atoms.append(_canonical(value))
    if tag in {"at"} and len(atoms) == 2: atoms.append(0.0)
    if tag == "fill" and atoms == ["none"]: atoms = ["no"]
    if tag == "stroke":
        # KiCad 10.0.6 EDA_SHAPE::GetLineStyle resolves DEFAULT to SOLID;
        # Serialize uses that getter. This does not depend on preferences.
        # Only this effective style is equivalent: widths/dashes still compare.
        for child in children:
            if child == ["type", "default"]: child[1] = "solid"
    if tag == "fp_poly":
        # A closed polygon's repeated final vertex contributes only a zero-length
        # edge. IPC's closed polyline omits it; keep every other ordered vertex.
        for child in children:
            if child[0] == "pts" and len(child) > 4 and _equal(child[1], child[-1]):
                child.pop()
    # Polygon vertex order is geometry, not serialization order.
    if tag != "pts": children.sort(key=repr)
    if tag in {"layers", "attr", "justify"}: atoms.sort(key=repr)
    return [tag, *atoms, *children]


def _equal(left, right):
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a,b) for a,b in zip(left,right))
    if isinstance(left, (float,int)) and isinstance(right, (float,int)):
        return math.isclose(left,right,rel_tol=0,abs_tol=NUMERIC_TOLERANCE_MM+1e-12)
    return left == right


def _expected_child(*, node, back, rotation):
    node = copy.deepcopy(node)
    kind = node[0]
    # Models are native footprint-local definitions: no manual flip is applied.
    if kind == "model": return node
    def visit(item):
        if not isinstance(item,list): return
        if back and item[0] in {"layer", "layers"}:
            for i,value in enumerate(item[1:],1):
                if isinstance(value,str) and value.startswith(("F.","B.")):
                    item[i] = ("B." if value.startswith("F.") else "F.")+value[2:]
        if item[0] in {"at", "start", "end", "mid", "center", "xy"} and len(item)>=3:
            if back: item[2] = str(-float(item[2]))
            if item[0] == "at" and kind in {"pad", "property", "fp_text"}:
                angle = float(item[3]) if len(item)>3 else 0.0
                is_text = kind in {"property", "fp_text"}
                angle = rotation + ((180 if is_text else 0)-angle if back else angle)
                angle %= _angle_period(node=node)
                if len(item)>3: item[3] = str(angle)
                else: item.append(str(angle))
        for child in item[1:]: visit(child)
    visit(node)
    if back and kind in {"property", "fp_text"}:
        effects = _children(node,"effects")
        if effects:
            justify = _children(effects[0],"justify")
            if not justify: effects[0].append(["justify","mirror"])
            elif "mirror" in justify[0]: justify[0].remove("mirror")
            else: justify[0].append("mirror")
    return node


def _angle_period(*, node):
    # Missing/explicit `unlocked no` means keep upright in KiCad footprints.
    # `unlocked yes` is not an object lock and must not be ignored.
    return 180 if node[0] in {"property", "fp_text"} and ["unlocked", "yes"] not in node else 360


def compare_footprint(*, source: str, placed: list, reference: str, value: str,
                      prototype_allow_pad_fabrication_omission: bool = False,
                      accepted_exceptions: list | None = None) -> list[dict]:
    original = _parse_sexpression(source)
    rotation_nodes = _children(placed,"at")
    rotation = float(rotation_nodes[0][3]) if rotation_nodes and len(rotation_nodes[0])>3 else 0.0
    back = _children(placed,"layer")[0][1] == "B.Cu"
    expected, actual = {}, {}
    for is_source, root, target in ((True,original,expected),(False,placed,actual)):
        for raw in root[2:]:
            if not isinstance(raw,list) or raw[0] in _INSTANCE_ROOT: continue
            node = _expected_child(node=raw,back=back,rotation=rotation) if is_source else copy.deepcopy(raw)
            key = node[0]
            if node == ["duplicate_pad_numbers_are_jumpers", "no"]:
                continue  # KiCad's explicit serialization of the false default
            if key == "property":
                key += ":" + node[1]
                if is_source and node[1] in {"Reference","Value"}:
                    node[2] = reference if node[1]=="Reference" else value
            if not is_source and node[0] in {"property","fp_text","pad"}:
                for at in _children(node,"at"):
                    if len(at)>3: at[3] = str(float(at[3]) % _angle_period(node=node))
            target.setdefault(key,[]).append(_canonical(node))
    errors=[]
    for key in sorted(set(expected)|set(actual)):
        # KiCad supplies empty mandatory fields absent from a library. They
        # carry no authored source content; nonempty or custom additions fail.
        if key in {"property:Datasheet","property:Description"} and key not in expected:
            if all(n[2] == "" for n in actual[key]): continue
        left,right = sorted(expected.get(key,[]),key=repr), sorted(actual.get(key,[]),key=repr)
        if key == "pad" and prototype_allow_pad_fabrication_omission:
            remaining = list(right)
            omissions = []
            matched = len(left) == len(right)
            for pad in left:
                exact = next((i for i,p in enumerate(remaining) if _equal(pad,p)), None)
                if exact is not None:
                    remaining.pop(exact)
                    continue
                props = _children(pad, "property")
                if len(props) != 1 or len(props[0]) != 2 or props[0][1] not in PROTOTYPE_PAD_PROPERTIES:
                    matched = False
                    break
                reduced = [item for item in pad if item != props[0]]
                match = next((i for i,p in enumerate(remaining) if _equal(reduced,p)), None)
                if match is None:
                    matched = False
                    break
                remaining.pop(match)
                omissions.append({"rule": "CQ-010", "reference": reference,
                                  "pad_number": pad[1], "source_property": props[0][1],
                                  "position": _children(pad, "at"),
                                  "status": "accepted_prototype_metadata_loss"})
            if matched and not remaining:
                if accepted_exceptions is not None: accepted_exceptions.extend(omissions)
                continue
        if not _equal(left,right): errors.append({"reference":reference,"domain":key,"expected":left,"actual":right})
    return errors


def verify_saved_footprints(*, bundle_root: Path) -> dict:
    path = bundle_root/"working.kicad_pcb"
    payload = path.read_bytes()
    root = _parse_sexpression(payload.decode("utf-8"))
    errors, references, exceptions = [], [], []
    board_profile = yaml.safe_load((bundle_root/"pcb/board.yaml").read_text(encoding="utf-8"))
    allow = board_profile.get("prototype_allow_pad_fabrication_omission", False)
    for placed in _children(root,"footprint"):
        props = {p[1]:p[2] for p in _children(placed,"property")}
        reference,value = props["Reference"],props["Value"]
        references.append(reference)
        library,entry = placed[1].split(":",1)
        asset = (bundle_root/"libraries"/(library+".pretty")/(entry+".kicad_mod")).resolve()
        if not asset.is_relative_to((bundle_root/"libraries").resolve()): raise ValueError("library path escape")
        errors.extend(compare_footprint(source=asset.read_text(encoding="utf-8"), placed=placed,
                                        reference=reference,value=value,
                                        prototype_allow_pad_fabrication_omission=allow,
                                        accepted_exceptions=exceptions))
    if not references: errors.append({"domain":"no_footprints"})
    result = {"status":"invalid" if errors else "ready","references":sorted(references),
              "pcb_sha256":hashlib.sha256(payload).hexdigest(),"diagnostics":errors,
              "accepted_exceptions":exceptions,
              "fidelity_status": "unexplained_mismatches" if errors else (
                  "accepted_prototype_metadata_loss" if exceptions else "lossless_semantic_match"),
              "numeric_tolerance_mm":NUMERIC_TOLERANCE_MM}
    (bundle_root/"reports/footprint_fidelity.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    return result
