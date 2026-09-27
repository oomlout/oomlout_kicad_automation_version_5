"""CQ-013 reviewable DRC severity profiles; never change geometry or dimensions."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

DEFAULT_DRC_MODE = "light"
NORMAL = json.loads(Path(__file__).with_name("drc_normal_kicad_10.json").read_text(encoding="utf-8"))
LIGHT_ENABLED = frozenset({
    "shorting_items", "tracks_crossing", "zones_intersect", "unconnected_items",
    "net_conflict", "isolated_copper", "starved_thermal", "track_dangling",
    "via_dangling", "invalid_outline", "items_not_allowed",
})
MASK_SWITCHES = {"solder_mask_checks": "solder_mask_bridge", "silkscreen_mask_checks": "silk_over_copper"}


def drc_profile(*, command: dict | None = None) -> dict:
    command = dict(command or {})
    unknown = set(command) - {"mode", "reason", *MASK_SWITCHES}
    if unknown: raise ValueError(f"Unknown DRC profile keys: {sorted(unknown)}")
    mode = command.get("mode", DEFAULT_DRC_MODE)
    if mode not in ("light", "normal", "tight"): raise ValueError("DRC mode must be light, normal or tight")
    reason = command.get("reason", "Reusable prototype default; reduced-scope DRC")
    if not isinstance(reason, str) or not reason.strip(): raise ValueError("DRC mode requires a nonempty reason")
    severities = {key: (NORMAL[key] if mode == "normal" else
                       "error" if mode == "tight" or key in LIGHT_ENABLED else "ignore") for key in sorted(NORMAL)}
    overrides = {}
    for key, rule in MASK_SWITCHES.items():
        if key in command:
            if type(command[key]) is not bool: raise ValueError(f"{key} must be a boolean")
            overrides[key] = command[key]
            severities[rule] = ("error" if mode == "tight" else NORMAL[rule]) if command[key] else "ignore"
    return {"mode": mode, "reason": reason, **overrides, "severities": severities,
            "disabled_checks": [k for k,v in severities.items() if v == "ignore"],
            "reduced_scope": mode == "light" or any(v is False for v in overrides.values()),
            "catalog": "KiCad 10.0.6 recorded native severity map"}


def validate_frozen_profile(*, profile: dict) -> dict:
    resolved = drc_profile(command={k:v for k,v in profile.items() if k in {"mode", "reason", *MASK_SWITCHES}})
    if resolved != profile: raise ValueError("Frozen DRC profile differs from reviewed mode/catalog")
    return resolved


def verify_saved_drc_profile(*, bundle_root: Path, profile: dict) -> dict:
    expected = validate_frozen_profile(profile=profile)
    diagnostics, actual, digest = [], {}, None
    try:
        raw = (bundle_root/"working.kicad_pro").read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        actual = json.loads(raw)["board"]["design_settings"]["rule_severities"]
        if actual != expected["severities"]:
            diagnostics.append({"code": "saved_drc_profile_mismatch", "differences": [
                {"rule": key, "expected": expected["severities"].get(key), "actual": actual.get(key)}
                for key in sorted(set(actual) | set(expected["severities"]))
                if actual.get(key) != expected["severities"].get(key)]})
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        diagnostics.append({"code": "saved_drc_profile_unavailable", "message": str(exc)})
    result = {"status": "failed" if diagnostics else "ready", "mode": expected["mode"],
              "disabled_checks": expected["disabled_checks"], "reduced_scope": expected["reduced_scope"],
              "actual_severities": actual, "project_sha256": digest, "diagnostics": diagnostics}
    path = bundle_root/"reports/drc_profile_verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result
