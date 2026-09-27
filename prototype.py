"""Reusable project profile; physical stackup is not required for prototypes."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .connectivity import _parse_sexpression, _children
from .drc_profiles import drc_profile

DEFAULT_COPPER_LAYER_COUNT = 2
DEFAULT_CONTROLLED_IMPEDANCE_REQUIRED = False
DEFAULT_PCB_BORDERLESS = True


def project_settings(*, overrides: dict | None = None) -> dict:
    """Fresh defaults for factories, population and compiler fallbacks.

    Legacy stackup strings are accepted only when consistent with the canonical
    copper_layer_count. Explicit advanced choices are preserved, never reset.
    """
    settings = copy.deepcopy(overrides or {})
    drc = settings.get("drc", {})
    if not isinstance(drc, dict): raise ValueError("drc must be a dictionary")
    resolved_drc = drc_profile(command=drc)
    settings["drc"] = {k:v for k,v in resolved_drc.items() if k in {"mode", "reason", "solder_mask_checks", "silkscreen_mask_checks"}}
    borderless = settings.get("pcb_borderless", DEFAULT_PCB_BORDERLESS)
    if type(borderless) is not bool:
        raise ValueError("pcb_borderless must be a boolean")
    settings["pcb_borderless"] = borderless
    exception = settings.get("prototype_allow_pad_fabrication_omission", False)
    if type(exception) is not bool:
        raise ValueError("prototype_allow_pad_fabrication_omission must be a boolean")
    settings["prototype_allow_pad_fabrication_omission"] = exception
    legacy = settings.get("board_stackup")
    count = settings.get("copper_layer_count", DEFAULT_COPPER_LAYER_COUNT)
    if legacy is not None:
        if legacy not in {f"{n}-layer" for n in range(2, 33, 2)}:
            raise ValueError(f"Unsupported board_stackup: {legacy!r}")
        if "copper_layer_count" not in settings:
            count = int(legacy.split("-")[0])
        elif legacy != f"{count}-layer":
            raise ValueError("board_stackup conflicts with copper_layer_count")
    if type(count) is not int or count < 2 or count > 32 or count % 2:
        raise ValueError("copper_layer_count must be an even integer from 2 to 32")
    required = settings.get("controlled_impedance_required", DEFAULT_CONTROLLED_IMPEDANCE_REQUIRED)
    if type(required) is not bool:
        raise ValueError("controlled_impedance_required must be a boolean")
    layers = ["F.Cu", *[f"In{i}.Cu" for i in range(1, count-1)], "B.Cu"]
    if "copper_layers" in settings and settings["copper_layers"] != layers:
        raise ValueError("copper_layers conflicts with copper_layer_count")
    settings.update(copper_layer_count=count, copper_layers=layers,
                    board_stackup=f"{count}-layer", controlled_impedance_required=required)
    return settings


def board_profile(*, settings: dict | None = None) -> dict:
    resolved = project_settings(overrides=settings)
    return {key: resolved[key] for key in ("copper_layer_count", "copper_layers",
            "board_stackup", "controlled_impedance_required", "pcb_borderless",
            "prototype_allow_pad_fabrication_omission", "drc")}


def validate_copper_items(*, profile: dict, items: list[dict]) -> None:
    """Reject wrong-layer track, via and zone intent before backend mutation."""
    allowed = set(profile["copper_layers"])
    for item in items:
        layers = item.get("layers", [item.get("layer")])
        if not layers or not set(layers) <= allowed:
            raise ValueError(f"Copper item uses disabled layers: {layers!r}; enabled={sorted(allowed)}")


def compare_saved_copper(*, pcb: str, profile: dict) -> dict:
    root = _parse_sexpression(pcb)
    if not root or root[0] != "kicad_pcb":
        raise ValueError("Expected saved kicad_pcb")
    sections = _children(root, "layers")
    actual = [str(item[1]) for section in sections for item in section[1:]
              if isinstance(item, list) and len(item) > 1 and str(item[1]).endswith(".Cu")]
    errors = []
    if len(actual) != profile["copper_layer_count"] or set(actual) != set(profile["copper_layers"]):
        errors.append({"code": "saved_copper_layers_mismatch", "expected": profile["copper_layers"], "actual": actual})
    for kind in ("segment", "arc", "via", "zone"):
        for item in _children(root, kind):
            layers = [v for node in _children(item, "layer") + _children(item, "layers") for v in node[1:]]
            try:
                validate_copper_items(profile=profile, items=[{"layers": layers}])
            except ValueError as exc:
                errors.append({"code": "saved_copper_item_layer", "kind": kind, "message": str(exc)})
    return {"status": "invalid" if errors else "ready", "copper_layer_count": len(actual),
            "copper_layers": actual, "controlled_impedance_required": profile["controlled_impedance_required"],
            "diagnostics": errors}


def verify_saved_copper(*, bundle_root: Path, profile: dict) -> dict:
    try:
        result = compare_saved_copper(pcb=(bundle_root / "working.kicad_pcb").read_text(encoding="utf-8"), profile=profile)
    except Exception as exc:
        result = {"status": "invalid", "diagnostics": [{"code": "saved_copper_unavailable", "message": str(exc)}]}
    report = bundle_root / "reports/copper_verification.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result
