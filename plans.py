"""Focused-data, high-level-plan, and normalized-operation bundle compiler."""

from __future__ import annotations
from .drc_profiles import drc_profile

import copy
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

import yaml

from .connectivity import compile_connectivity
from .expansion import (
    deduplicate_operations,
    expand_high_level_action,
    operation_id,
    validate_operations,
)
from .geometry import resolve_geometry
from .footprints import bounds_from_inspections
from .schematic_layout import apply_block_rows
from .prototype import board_profile


BUNDLE_OWNER_FILE = ".compiler-owner.json"
CANONICAL_FILES = (
    "sources/manifest.yaml",
    "schematic/parts.yaml",
    "schematic/nets.yaml",
    "schematic/blocks.yaml",
    "schematic/placement.yaml",
    "schematic/libraries.yaml",
    "schematic/validation.yaml",
    "schematic/manifest.yaml",
    "schematic/layout.yaml",
    "pcb/board.yaml",
    "pcb/placement.yaml",
    "pcb/validation.yaml",
    "high_level_plan.yaml",
    "mcp_plan.yaml",
)


class PlanError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details

    def diagnostic(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}


def canonical_yaml(data: Any) -> bytes:
    return yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    ).encode("utf-8")


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_details_from_inspections(
    inspections: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for source_id, inspection in inspections.items():
        symbol = inspection.get("valid_assets", {}).get("symbol")
        footprint_kind = inspection.get("selected_footprint_kind")
        footprint = inspection.get("valid_assets", {}).get(footprint_kind) if footprint_kind else None
        cache = inspection.get("cache", {})
        details[source_id] = {
            "source_id": source_id,
            "source_hash": inspection.get("source_hash"),
            "status": inspection.get("status"),
            "symbol_id": f"OOMP:{source_id}" if symbol else None,
            "footprint_id": (
                f"OOMP_{'MachineSolder' if footprint_kind == 'machine_solder' else 'HandSolder'}:{source_id}"
                if footprint
                else None
            ),
            "value": cache.get("value") or cache.get("name_proper") or cache.get("name") or source_id,
            "selected_footprint_kind": footprint_kind,
            "assets": [
                {
                    "kind": asset.get("kind"),
                    "file": asset.get("file"),
                    "sha256": asset.get("sha256"),
                    "valid": asset.get("valid"),
                }
                for asset in inspection.get("assets", [])
            ],
        }
    return details


def _source_manifest_from_details(details: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "format_version": 1,
        "sources": [
            {
                "oomp_id": source_id,
                "source_hash": item.get("source_hash"),
                "status": item.get("status"),
                "selected_footprint_kind": item.get("selected_footprint_kind"),
                "assets": item.get("assets", []),
            }
            for source_id, item in sorted(details.items())
        ],
    }


def _part_high_action(
    part: dict[str, Any], source: dict[str, Any] | None
) -> dict[str, Any]:
    source = source or {}
    ready = part.get("resolution_status") == "ready"
    required_asset = source.get("footprint_id") and (
        part.get("mechanical_only") or source.get("symbol_id")
    )
    status = "ready" if ready and required_asset else "blocked"
    return {
        "id": f"hl_add_part_{part.get('creation_key')}",
        "action": "add_part",
        "instance_id": part.get("instance_id"),
        "reference": part.get("assigned_reference"),
        "resolution_status": status,
        "finding_ids": list(part.get("finding_ids", [])),
        "inputs": {
            "instance_id": part.get("instance_id"),
            "reference": part.get("assigned_reference"),
            "source_id": part.get("source_id"),
            "symbol_id": source.get("symbol_id"),
            "value": source.get("value"),
            "footprint_id": source.get("footprint_id"),
            "mechanical_only": part.get("mechanical_only", False),
        },
    }


def build_high_level_actions(
    board: dict[str, Any],
    connectivity: dict[str, Any],
    geometry: dict[str, Any],
    source_details: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    placement_by_instance = {
        item.get("instance_id"): item for item in geometry.get("placements", [])
    }
    net_by_source = {
        source_id: net
        for net in connectivity.get("nets", [])
        for source_id in net.get("source_action_ids", [])
    }
    nc_by_source = {
        source_id: nc
        for nc in connectivity.get("no_connects", [])
        for source_id in nc.get("source_action_ids", [])
    }
    for part in connectivity.get("parts", []):
        source = source_details.get(str(part.get("source_id")))
        actions.append(_part_high_action(part, source))
        placement = placement_by_instance.get(part.get("instance_id"), {})
        if not part.get("mechanical_only"):
            schematic = placement.get("schematic")
            actions.append(
                {
                    "id": f"hl_place_schematic_{part.get('creation_key')}",
                    "action": "place_schematic_part",
                    "instance_id": part.get("instance_id"),
                    "reference": part.get("assigned_reference"),
                    "resolution_status": (
                        "ready"
                        if part.get("resolution_status") == "ready" and schematic is not None
                        else "blocked"
                    ),
                    "finding_ids": list(placement.get("finding_ids", [])),
                    "inputs": {
                        "instance_id": part.get("instance_id"),
                        "reference": part.get("assigned_reference"),
                        "position_mm": schematic.get("position_mm") if schematic else None,
                        "rotation_degrees": schematic.get("rotation_degrees") if schematic else None,
                        "field_placements": schematic.get("field_placements") if schematic else None,
                    },
                }
            )
        pcb = placement.get("pcb")
        actions.append(
            {
                "id": f"hl_place_pcb_{part.get('creation_key')}",
                "action": "place_pcb_part",
                "instance_id": part.get("instance_id"),
                "reference": part.get("assigned_reference"),
                "resolution_status": (
                    "ready"
                    if part.get("resolution_status") == "ready" and pcb is not None
                    else "blocked"
                ),
                "finding_ids": list(placement.get("finding_ids", [])),
                "inputs": {
                    "instance_id": part.get("instance_id"),
                    "reference": part.get("assigned_reference"),
                    "anchor_mm": pcb.get("anchor_mm") if pcb else None,
                    "rotation_degrees": pcb.get("rotation_degrees") if pcb else None,
                    "side": pcb.get("side") if pcb else None,
                    "placement_kind": pcb.get("placement_kind") if pcb else None,
                    "mechanical_only": part.get("mechanical_only", False),
                },
            }
        )
    for block in geometry.get("blocks", []):
        actions.append(
            {
                "id": f"hl_add_block_{block.get('name')}",
                "action": "add_block",
                "resolution_status": block.get("resolution_status", "blocked"),
                "finding_ids": list(block.get("finding_ids", [])),
                "inputs": copy.deepcopy(block),
            }
        )
    for history in connectivity.get("connection_history", []):
        source_id = history.get("source_action_id")
        net = net_by_source.get(source_id)
        rendered_net = copy.deepcopy(net) if net else {}
        stub_length = board.get("settings", {}).get("schematic_stub_length_mm")
        if stub_length is not None:
            rendered_net["stub_length_mm"] = stub_length
        by_reference = {p["reference"]: p.get("schematic") or {} for p in geometry["placements"]}
        seen_attachments = {}
        for endpoint in rendered_net.get("endpoints", []):
            length = by_reference[endpoint["reference"]].get("pin_stub_lengths_mm", {}).get(str(endpoint["pin_number"]))
            if length is not None:
                endpoint["stub_length_mm"] = length
            pin_geometry = by_reference[endpoint["reference"]].get("pin_geometry", {}).get(str(endpoint["pin_number"]))
            if pin_geometry is not None:
                # Stacked physical pins still all exist in the endpoint oracle,
                # but their shared physical attachment needs one wire/label only.
                key = (endpoint["reference"], *pin_geometry)
                if key in seen_attachments:
                    endpoint["render_attachment"] = False
                    endpoint["shared_attachment_pin"] = seen_attachments[key]
                else:
                    seen_attachments[key] = str(endpoint["pin_number"])
                if rendered_net.get("name") in {"GND", "+3V3", "+5V"} and pin_geometry[2] in (0, 180):
                    # Keep conventional upright power symbols; place them beyond
                    # neighboring horizontal labels instead of rotating text
                    # into tightly spaced header pins.
                    endpoint["stub_length_mm"] = by_reference[endpoint["reference"]]["horizontal_power_stub_mm"]
        actions.append(
            {
                "id": f"hl_{source_id}",
                "action": "connect",
                "resolution_status": "ready" if net else history.get("resolution_status", "blocked"),
                "finding_ids": list(history.get("finding_ids", [])),
                "inputs": {"requested": history.get("requested"), "net": rendered_net},
            }
        )
    for history in connectivity.get("no_connect_history", []):
        source_id = history.get("source_action_id")
        nc = nc_by_source.get(source_id)
        actions.append(
            {
                "id": f"hl_{source_id}",
                "action": "no_connect",
                "resolution_status": "ready" if nc else history.get("resolution_status", "blocked"),
                "finding_ids": list(history.get("finding_ids", [])),
                "inputs": {"endpoint": copy.deepcopy(nc.get("endpoint", {})) if nc else {}},
            }
        )
    board_data = geometry.get("board", {})
    for board_action in board.get("board_actions", []):
        if board_action.get('action')=='copper_pour':
            from .copper_pours import validate_pour
            pour=validate_pour(command=board_action['pour'])
            if pour['net'] not in {n['name'] for n in connectivity['nets']}:
                raise PlanError('pour_unknown_net','Pour must belong to an authored net',pour=pour['name'])
            if any(not 0 < p[axis] < board_data[dimension] for p in pour['points_mm']
                   for axis,dimension in (('x','width_mm'),('y','height_mm'))):
                raise PlanError('pour_outside_outline','Pour must stay inside board',pour=pour['name'])
            actions.append({'id':f"hl_{board_action['_creation_key']}",'action':'copper_pour',
                            'resolution_status':'ready','finding_ids':[],'inputs':pour})
        if board_action.get('action') == 'route_path':
            route = copy.deepcopy(board_action['route'])
            net = next((n for n in connectivity['nets'] if n['name']==route['net']),None)
            if not net or any({k:e[k] for k in ('reference','pin_number')} not in [{k:p[k] for k in ('reference','pin_number')} for p in net['endpoints']] for e in (route['start'],route['end'])):
                raise PlanError('route_endpoint_net_mismatch','Route endpoints must belong to the authored net',route=route['name'])
            if any(not route['width_mm']/2 <= p[axis] <= board_data[dimension]-route['width_mm']/2
                   for p in route['points_mm'] for axis,dimension in (('x','width_mm'),('y','height_mm'))):
                raise PlanError('route_outside_outline','Route must remain inside the authored board',route=route['name'])
            if any(not via['diameter_mm']/2 <= via[axis] <= board_data[dimension]-via['diameter_mm']/2
                   for via in route.get('vias',[]) for axis,dimension in (('x','width_mm'),('y','height_mm'))):
                raise PlanError('via_outside_outline','Via copper must remain inside the authored board',route=route['name'])
            actions.append({'id':f"hl_{board_action['_creation_key']}",'action':'route_path',
                            'resolution_status':'ready','finding_ids':[],'inputs':route})
        if board_action.get("action") == "set_design_rules":
            actions.append({"id": f"hl_{board_action.get('_creation_key')}",
                            "action": "set_design_rules", "resolution_status": "ready",
                            "finding_ids": [], "inputs": copy.deepcopy(board_action["rules"])})
        if board_action.get("action") == "board_oobb":
            actions.append(
                {
                    "id": f"hl_{board_action.get('_creation_key')}",
                    "action": "board_oobb",
                    "resolution_status": "ready" if board_data.get("status") == "ready" else "blocked",
                    "finding_ids": [],
                    "inputs": {
                        key: board_data.get(key)
                        for key in (
                            "width_units",
                            "height_units",
                            "width_mm",
                            "height_mm",
                            "outline_vertices_mm",
                            "hole_centers_mm",
                            "layer",
                        )
                    },
                }
            )
    return actions


def _orchestration_operations(
    board_project_id: str,
    high_level_plan_hash: str,
    expanded_operations: list[dict[str, Any]],
    *, layout: dict[str, Any], profile: dict[str, Any],
) -> list[dict[str, Any]]:
    project = operation_id("schematic", "project_create", "project")
    drc_config = operation_id("schematic", "drc_profile_configure", "project")
    configure = operation_id("schematic", "schematic_configure", "project")
    schematic_verify = operation_id("schematic", "schematic_verify", "project")
    erc = operation_id("schematic", "erc_run", "project")
    schematic_save = operation_id("schematic", "project_save_verify", "schematic")
    sync = operation_id("pcb", "pcb_sync", "project")
    pcb_verify = operation_id("pcb", "pcb_verify", "project")
    clearance = operation_id("pcb", "clearance_run", "project")
    drc = operation_id("pcb", "drc_run", "project")
    pcb_save = operation_id("pcb", "project_save_verify", "pcb")

    def operation(
        operation_id_value: str,
        action: str,
        phase: str,
        inputs: dict[str, Any],
        depends_on: list[str],
        after_attempt: list[str] | None = None,
    ) -> dict[str, Any]:
        result = {
            "id": operation_id_value,
            "action": action,
            "inputs": inputs,
            "depends_on": depends_on,
            "source": {"compiler_phase": action},
            "phase": phase,
            "resolution_status": "ready",
            "finding_ids": [],
        }
        if after_attempt:
            result["after_attempt"] = after_attempt
        return result

    title_operations = [
        operation(
            operation_id("schematic", "schematic_block_title", title["block"]),
            "schematic_block_title", "schematic", title, [configure],
        )
        for title in layout.get("titles", [])
    ]
    schematic_mutations = [
        item["id"]
        for item in expanded_operations
        if item["phase"] == "schematic"
        and item["action"] in {"symbol_add", "symbol_place", "net_render", "no_connect_add"}
    ]
    pcb_mutations = [
        item["id"]
        for item in expanded_operations
        if item["phase"] == "pcb"
        and item["action"] in {"outline_create", "mechanical_add", "footprint_place"}
    ]
    references = sorted(
        {
            item.get("inputs", {}).get("reference")
            for item in expanded_operations
            if item.get("action") == "footprint_place"
            and item.get("inputs", {}).get("reference")
        }
    )
    reference_pairs = [
        [left, right]
        for index, left in enumerate(references)
        for right in references[index + 1 :]
    ]
    routing_operations = [item['id'] for kind in ('copper_pour','route_path') for item in expanded_operations if item['action']==kind]
    # Ground/plane intent first, then signals in stable declaration order.
    # Final native refill/save remains mandatory after all signal mutations.
    for previous,current in zip(routing_operations,routing_operations[1:]):
        next(item for item in expanded_operations if item['id']==current)['depends_on'].append(previous)
    return [
        operation(
            project,
            "project_create",
            "schematic",
            {
                "board_project_id": board_project_id,
                "project_path": "working.kicad_pro",
                "schematic_path": "working.kicad_sch",
                "pcb_path": "working.kicad_pcb",
                "profile": profile,
            },
            [],
        ),
        operation(drc_config, "drc_profile_configure", "schematic",
                  drc_profile(command=profile.get("drc")), [project]),
        operation(
            configure,
            "schematic_configure",
            "schematic",
            {
                "sym_lib_table": "sym-lib-table",
                "fp_lib_table": "fp-lib-table",
                "page": layout["page"],
                "worksheet": layout.get("worksheet"),
            },
            [project, drc_config, *[op["id"] for op in expanded_operations if op["action"] == "design_rules_configure"]],
        ),
        *title_operations,
        operation(
            schematic_verify,
            "schematic_verify",
            "schematic",
            {"high_level_plan_sha256": high_level_plan_hash},
            [configure, *[item["id"] for item in title_operations]],
            sorted(schematic_mutations),
        ),
        operation(erc, "erc_run", "schematic", {"schematic_path": "working.kicad_sch"}, [schematic_verify]),
        operation(
            schematic_save,
            "project_save_verify",
            "schematic",
            {"required_paths": ["working.kicad_pro", "working.kicad_sch"]},
            [schematic_verify],
            [erc],
        ),
        operation(
            sync,
            "pcb_sync",
            "pcb",
            {"schematic_plan_sha256": high_level_plan_hash, "workflow": ["dry_run", "apply_exact_revision"], "profile": profile},
            [schematic_verify],
            [erc],
        ),
        operation(
            pcb_verify,
            "pcb_verify",
            "pcb",
            {"high_level_plan_sha256": high_level_plan_hash, "references": references},
            [sync],
            sorted(pcb_mutations),
        ),
        operation(
            clearance,
            "clearance_run",
            "pcb",
            {"pcb_path": "working.kicad_pcb", "reference_pairs": reference_pairs},
            [pcb_verify],
            routing_operations,
        ),
        operation(drc, "drc_run", "pcb", {"pcb_path": "working.kicad_pcb",
                  **({'refill_zones':True} if any(o['action']=='copper_pour' for o in expanded_operations) else {})}, [pcb_verify], routing_operations),
        operation(
            pcb_save,
            "project_save_verify",
            "pcb",
            {"required_paths": ["working.kicad_pro", "working.kicad_sch", "working.kicad_pcb"], "profile": profile},
            [pcb_verify],
            [clearance, drc],
        ),
    ]


def _write_yaml(root: Path, relative: str, data: Any) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_yaml(data))


def _copy_libraries(source: Path, destination: Path) -> None:
    for name in ("libraries", "sym-lib-table", "fp-lib-table"):
        source_path = source / name
        if not source_path.exists():
            continue
        destination_path = destination / name
        if destination_path.is_dir():
            shutil.rmtree(destination_path)
        elif destination_path.exists():
            destination_path.unlink()
        if source_path.is_dir():
            shutil.copytree(source_path, destination_path)
        else:
            shutil.copy2(source_path, destination_path)


def _publish(
    staging: Path,
    output: Path,
    board_project_id: str,
    before_promote: Callable[[Path], None] | None,
) -> None:
    if before_promote:
        before_promote(staging)
    backup = output.parent / f".{output.name}.previous"
    if backup.exists():
        raise PlanError("stale_bundle_backup", f"Stale compiler backup exists: {backup}")
    had_previous = output.exists()
    try:
        if had_previous:
            output.replace(backup)
        staging.replace(output)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if not output.exists() and backup.exists():
            backup.replace(output)
        raise


def compile_frozen_bundle(
    board: dict[str, Any],
    symbols: dict[str, dict[str, Any]],
    output_root: str | Path,
    *,
    source_details: dict[str, dict[str, Any]] | None = None,
    source_manifest: dict[str, Any] | None = None,
    inspections: dict[str, dict[str, Any]] | None = None,
    footprint_bounds: dict[str, tuple[float, float, float, float]] | None = None,
    libraries_source: str | Path | None = None,
    before_promote: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """Compile and atomically publish one complete deterministic plan bundle."""

    details = copy.deepcopy(source_details or {})
    bounds = copy.deepcopy(footprint_bounds or {})
    bounds_diagnostics: list[dict[str, Any]] = []
    if inspections is not None:
        details = _source_details_from_inspections(inspections)
        if footprint_bounds is None:
            bounds, bounds_diagnostics = bounds_from_inspections(inspections)
    manifest = copy.deepcopy(source_manifest or _source_manifest_from_details(details))
    profile = board_profile(settings=board.get("settings"))
    if profile["copper_layer_count"] != 2 or profile["controlled_impedance_required"]:
        raise PlanError("unsupported_advanced_profile",
                        "Explicit advanced profile preserved, but this executor currently supports only two-layer prototype replay",
                        profile=profile)
    connectivity = compile_connectivity(board, symbols)
    geometry = resolve_geometry(board, connectivity["parts"], bounds)
    geometry["board"].update(profile)
    layout = {"mode": "authored", "page": board.get("settings", {}).get("schematic_page", "A4")}
    if board.get("settings", {}).get("schematic_layout", {}).get("mode") == "block_rows":
        layout = apply_block_rows(board=board, connectivity=connectivity, geometry=geometry, inspections=inspections or {})
    if board.get("settings", {}).get("schematic_borderless", False):
        layout["worksheet"] = "libraries/blank.kicad_wks"
    high_actions = build_high_level_actions(board, connectivity, geometry, details)
    expanded = [expand_high_level_action(action=action) for action in high_actions]
    deduplicated = deduplicate_operations(
        operation
        for expansion in expanded
        for operation in expansion["operations"]
    )
    compilation_findings = [
        *connectivity["findings"],
        *geometry["findings"],
        *bounds_diagnostics,
        *deduplicated["diagnostics"],
    ]
    for action in high_actions:
        if action["action"] == "add_part" and action["resolution_status"] != "ready":
            inputs = action["inputs"]
            if not inputs.get("footprint_id") or (not inputs.get("mechanical_only") and not inputs.get("symbol_id")):
                compilation_findings.append({"code": "part_asset_unresolved", "severity": "error",
                    "message": "Required part symbol/footprint is unresolved",
                    "details": {key: inputs.get(key) for key in ("source_id", "reference", "mechanical_only")}})

    has_errors = any(item.get("severity", "error") != "warning" for item in compilation_findings)
    focused = {
        "schematic/layout.yaml": layout,
        "sources/manifest.yaml": manifest,
        "schematic/parts.yaml": {
            "parts": [
                {
                    "instance_id": part.get("instance_id"),
                    "reference": part.get("assigned_reference"),
                    "source_id": part.get("source_id"),
                    "value": details.get(str(part.get("source_id")), {}).get("value"),
                    "symbol_id": details.get(str(part.get("source_id")), {}).get("symbol_id"),
                    "footprint_id": details.get(str(part.get("source_id")), {}).get("footprint_id"),
                    "pins": part.get("pins", []),
                    "resolution_status": part.get("resolution_status"),
                    "finding_ids": part.get("finding_ids", []),
                }
                for part in connectivity["parts"]
                if not part.get("mechanical_only")
            ]
        },
        "schematic/nets.yaml": {
            "nets": connectivity["nets"],
            "no_connects": connectivity["no_connects"],
            "connection_history": connectivity["connection_history"],
            "no_connect_history": connectivity["no_connect_history"],
        },
        "schematic/blocks.yaml": {"blocks": geometry["blocks"]},
        "schematic/placement.yaml": {
            "placements": [
                item for item in geometry["placements"] if not item.get("mechanical_only")
            ]
        },
        "schematic/libraries.yaml": {
            "symbol_table": "sym-lib-table",
            "footprint_table": "fp-lib-table",
            "sources": [details[source_id] for source_id in sorted(details)],
        },
        "schematic/validation.yaml": {
            "status": "partial" if has_errors else "ready",
            "declared_part_count": len(board.get("parts", [])),
            "emitted_symbol_count": sum(
                1
                for action in high_actions
                if action["action"] == "add_part"
                and not action["inputs"].get("mechanical_only")
                and action["resolution_status"] == "ready"
            ),
            "findings": compilation_findings,
        },
        "pcb/board.yaml": geometry["board"],
        "pcb/placement.yaml": {"placements": geometry["placements"]},
        "pcb/validation.yaml": {
            "status": geometry["status"],
            "findings": geometry["findings"],
        },
    }

    output = Path(output_root).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        owner_path = output / BUNDLE_OWNER_FILE
        if not owner_path.is_file():
            raise PlanError("unowned_bundle_target", f"Refusing unowned bundle target: {output}")
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        if owner.get("board_project_id") != board.get("board_project_id"):
            raise PlanError("bundle_owner_mismatch", "Bundle belongs to a different board")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        if output.exists():
            shutil.copytree(output, staging, dirs_exist_ok=True)
        for relative in CANONICAL_FILES:
            path = staging / relative
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        if libraries_source is not None:
            _copy_libraries(Path(libraries_source), staging)
        if layout.get("worksheet") or profile["pcb_borderless"]:
            worksheet = staging / "libraries/blank.kicad_wks"
            worksheet.parent.mkdir(parents=True, exist_ok=True)
            worksheet.write_text('(kicad_wks (version 20231118) (generator "oomlout")\n'
                                 ' (setup (textsize 1.5 1.5) (linewidth 0.15) (textlinewidth 0.15)\n'
                                 ' (left_margin 0) (right_margin 0) (top_margin 0) (bottom_margin 0))\n'
                                 ' (tbtext "" (name "blank") (pos 0 0) (font (size 1.5 1.5))))\n', encoding="utf-8")
        for relative, data in focused.items():
            _write_yaml(staging, relative, data)
        focused_manifest_paths = [
            relative
            for relative in focused
            if relative.startswith("schematic/") and relative != "schematic/manifest.yaml"
        ]
        library_paths = []
        for base in ("libraries",):
            root = staging / base
            if root.is_dir():
                library_paths.extend(
                    path.relative_to(staging).as_posix()
                    for path in sorted(root.rglob("*"))
                    if path.is_file()
                )
        library_paths.extend(
            name for name in ("sym-lib-table", "fp-lib-table") if (staging / name).is_file()
        )
        schematic_manifest = {
            "format_version": 1,
            "files": {
                relative: _sha_file(staging / relative)
                for relative in sorted([*focused_manifest_paths, *library_paths])
            },
        }
        _write_yaml(staging, "schematic/manifest.yaml", schematic_manifest)
        source_manifest_hash = _sha_file(staging / "sources/manifest.yaml")
        input_paths = [
            "schematic/manifest.yaml",
            "pcb/board.yaml",
            "pcb/placement.yaml",
            "pcb/validation.yaml",
        ]
        high_plan = {
            "board_project_id": board.get("board_project_id"),
            "kicad_name": board.get("kicad_name"),
            "mcp_backend": "konnect",
            "source_manifest_sha256": source_manifest_hash,
            "input_sha256": {
                relative: _sha_file(staging / relative) for relative in input_paths
            },
            "actions": high_actions,
        }
        _write_yaml(staging, "high_level_plan.yaml", high_plan)
        high_hash = _sha_file(staging / "high_level_plan.yaml")
        orchestration = _orchestration_operations(
            str(board.get("board_project_id")), high_hash, deduplicated["operations"], layout=layout, profile=profile
        )
        all_operations = [*deduplicated["operations"], *orchestration]
        all_operations.sort(key=lambda item: (item["phase"], item["action"], item["id"]))
        operation_diagnostics = validate_operations(all_operations)
        if operation_diagnostics:
            raise PlanError(
                "invalid_operation_graph",
                "Normalized operation graph failed pre-publication validation",
                diagnostics=operation_diagnostics,
            )
        mcp_plan = {
            "board_project_id": board.get("board_project_id"),
            "mcp_backend": "konnect",
            "high_level_plan_sha256": high_hash,
            "operations": all_operations,
        }
        _write_yaml(staging, "mcp_plan.yaml", mcp_plan)
        (staging / BUNDLE_OWNER_FILE).write_text(
            json.dumps(
                {"owner": "oomlout_kicad_automation_version_5", "board_project_id": board.get("board_project_id")},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _publish(staging, output, str(board.get("board_project_id")), before_promote)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "status": "partial" if has_errors else "ready",
        "output_root": str(output),
        "connectivity": connectivity,
        "geometry": geometry,
        "high_level_plan": high_plan,
        "mcp_plan": mcp_plan,
        "expansion": expanded,
        "findings": compilation_findings,
    }


def verify_frozen_bundle(output_root: str | Path) -> dict[str, Any]:
    root = Path(output_root)
    diagnostics: list[dict[str, Any]] = []
    required = [*CANONICAL_FILES, BUNDLE_OWNER_FILE]
    for relative in required:
        if not (root / relative).is_file():
            diagnostics.append(
                {"code": "bundle_file_missing", "message": f"Missing frozen file: {relative}", "details": {"path": relative}}
            )
    if diagnostics:
        return {"status": "invalid", "diagnostics": diagnostics}
    schematic_manifest = yaml.safe_load((root / "schematic/manifest.yaml").read_text(encoding="utf-8"))
    for relative, expected in schematic_manifest.get("files", {}).items():
        path = root / relative
        actual = _sha_file(path) if path.is_file() else None
        if actual != expected:
            diagnostics.append(
                {
                    "code": "focused_input_hash_mismatch",
                    "message": f"Frozen input hash changed: {relative}",
                    "details": {"path": relative, "expected": expected, "actual": actual},
                }
            )
    high_plan = yaml.safe_load((root / "high_level_plan.yaml").read_text(encoding="utf-8"))
    if _sha_file(root / "sources/manifest.yaml") != high_plan.get("source_manifest_sha256"):
        diagnostics.append(
            {"code": "source_manifest_hash_mismatch", "message": "Source manifest hash changed", "details": {}}
        )
    for relative, expected in high_plan.get("input_sha256", {}).items():
        actual = _sha_file(root / relative) if (root / relative).is_file() else None
        if actual != expected:
            diagnostics.append(
                {"code": "plan_input_hash_mismatch", "message": f"Plan input hash changed: {relative}", "details": {"path": relative, "expected": expected, "actual": actual}}
            )
    mcp_plan = yaml.safe_load((root / "mcp_plan.yaml").read_text(encoding="utf-8"))
    actual_high = _sha_file(root / "high_level_plan.yaml")
    if actual_high != mcp_plan.get("high_level_plan_sha256"):
        diagnostics.append(
            {"code": "high_level_plan_hash_mismatch", "message": "High-level plan hash changed", "details": {"expected": mcp_plan.get("high_level_plan_sha256"), "actual": actual_high}}
        )
    diagnostics.extend(validate_operations(mcp_plan.get("operations", [])))
    return {"status": "ready" if not diagnostics else "invalid", "diagnostics": diagnostics}
