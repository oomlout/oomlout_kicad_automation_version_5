"""Explicit project DRC intent and independent saved-project readback.

Only the Konnect adapter writes settings. This module never edits a KiCad file.
Omitted rules stay unchanged; no prototype relaxation is a framework default.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

RULE_KEYS = {
    "minimum_clearance_mm": ("min_clearance", "min_clearance"),
    "minimum_track_width_mm": ("min_trace_width", "min_track_width"),
    "minimum_through_hole_diameter_mm": ("min_via_drill", "min_through_hole_diameter"),
    "minimum_via_diameter_mm": ("min_via_size", "min_via_diameter"),
    "minimum_hole_to_hole_mm": ("min_hole_to_hole", "min_hole_to_hole"),
}


def validate_rules(*, command: dict) -> dict:
    allowed = {*RULE_KEYS, "default_netclass_clearance_mm", "reason"}
    if set(command) - allowed:
        raise ValueError(f"Unknown design-rule keys: {sorted(set(command) - allowed)}")
    if not isinstance(command.get("reason"), str) or not command["reason"].strip():
        raise ValueError("Design-rule changes require a nonempty reason")
    if not set(command) - {"reason"}:
        raise ValueError("At least one explicit design rule is required")
    for key, value in command.items():
        if key != "reason" and (isinstance(value, bool) or not isinstance(value, (int, float))
                                or not math.isfinite(value) or value <= 0):
            raise ValueError(f"{key} must be a finite positive millimetre value")
    return dict(command)


def expected_rules(*, plan: dict) -> dict | None:
    items = [op["inputs"] for op in plan["operations"] if op["action"] == "design_rules_configure"]
    if len(items) > 1:
        raise ValueError("Only one design-rule declaration is allowed")
    return validate_rules(command=items[0]) if items else None


def verify_saved_rules(*, bundle_root: Path, command: dict) -> dict:
    """Compare frozen intent with saved settings, including on PCB resume."""
    command = validate_rules(command=command)
    diagnostics, actual = [], {}
    path = bundle_root / "working.kicad_pro"
    digest = None
    try:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        project = json.loads(raw)
        rules = project.get("board", {}).get("design_settings", {}).get("rules", {})
        for key, (_, saved_key) in RULE_KEYS.items():
            if key in command:
                actual[key] = rules.get(saved_key)
        if "default_netclass_clearance_mm" in command:
            classes = [c for c in project.get("net_settings", {}).get("classes", []) if c.get("name") == "Default"]
            actual["default_netclass_clearance_mm"] = classes[0].get("clearance") if len(classes) == 1 else None
        for key, value in actual.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isclose(value, command[key], abs_tol=1e-9, rel_tol=0):
                diagnostics.append({"code": "saved_design_rule_mismatch", "key": key, "expected": command[key], "actual": value})
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        diagnostics.append({"code": "saved_design_rules_unavailable", "message": str(exc)})
    report = {"status": "failed" if diagnostics else "ready", "expected": command,
              "actual": actual, "project_sha256": digest, "diagnostics": diagnostics}
    output = bundle_root / "reports/design_rules_verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
