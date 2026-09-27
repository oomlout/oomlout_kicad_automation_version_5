"""Pure expansion from normalized high-level actions to backend-neutral operations."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable
from .debug import traced


SUPPORTED_OPERATIONS = {
    "copper_pour",
    "route_path",
    "drc_profile_configure",
    "design_rules_configure",
    "project_create",
    "schematic_configure",
    "schematic_block_title",
    "symbol_add",
    "symbol_place",
    "net_render",
    "no_connect_add",
    "schematic_verify",
    "erc_run",
    "pcb_sync",
    "outline_create",
    "mechanical_add",
    "footprint_place",
    "pcb_verify",
    "clearance_run",
    "drc_run",
    "project_save_verify",
}


def operation_id(phase: str, action: str, identity: str) -> str:
    payload = f"{phase}\0{action}\0{identity}".encode("utf-8")
    return f"op_{phase}_{action}_{hashlib.sha256(payload).hexdigest()[:12]}"


def _operation(
    action: str,
    phase: str,
    identity: str,
    inputs: dict[str, Any],
    source_id: str,
    *,
    depends_on: Iterable[str] = (),
    after_attempt: Iterable[str] = (),
    resolution_status: str = "ready",
    finding_ids: Iterable[str] = (),
) -> dict[str, Any]:
    item = {
        "id": operation_id(phase, action, identity),
        "action": action,
        "inputs": copy.deepcopy(inputs),
        "depends_on": list(depends_on),
        "source": {"high_level_action_ids": [source_id]},
        "phase": phase,
        "resolution_status": resolution_status,
        "finding_ids": list(finding_ids),
        "_semantic_key": [phase, action, identity],
    }
    if after_attempt:
        item["after_attempt"] = list(after_attempt)
    return item


@traced(scope="compiler.expand_high_level_action")
def expand_high_level_action(*, action: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Expand one action without mutating it or any declaration state."""

    original = copy.deepcopy(action)
    action_name = action.get("action")
    action_id = str(action.get("id"))
    inputs = action.get("inputs", {})
    status = action.get("resolution_status", "ready")
    finding_ids = action.get("finding_ids", [])
    operations: list[dict[str, Any]] = []
    config_id = operation_id("schematic", "schematic_configure", "project")
    project_id = operation_id("schematic", "project_create", "project")
    if action_name == "copper_pour":
        from .copper_pours import validate_pour
        pour=validate_pour(command=inputs)
        operations.append(_operation('copper_pour','pcb',pour['name'],pour,action_id,
                                    depends_on=[operation_id('pcb','pcb_verify','project')]))
    elif action_name == "route_path":
        from .routing import validate_route
        route = validate_route(command=inputs)
        operations.append(_operation('route_path','pcb',route['name'],route,action_id,
                                    depends_on=[operation_id('pcb','pcb_verify','project')]))
    elif action_name == "set_drc_mode":
        # A single orchestration operation also covers implicit factory defaults.
        from .drc_profiles import drc_profile
        drc_profile(command=inputs)
    elif action_name == "set_design_rules":
        from .design_rules import validate_rules
        operations.append(_operation("design_rules_configure", "schematic", "project",
            validate_rules(command=inputs), action_id, depends_on=[project_id],
            resolution_status=status, finding_ids=finding_ids))
    elif action_name == "add_part":
        instance_id = str(inputs.get("instance_id"))
        if inputs.get("mechanical_only"):
            operations.append(
                _operation(
                    "mechanical_add",
                    "pcb",
                    instance_id,
                    {
                        key: inputs.get(key)
                        for key in ("instance_id", "reference", "footprint_id")
                    },
                    action_id,
                    depends_on=[operation_id("pcb", "pcb_sync", "project")],
                    resolution_status=status,
                    finding_ids=finding_ids,
                )
            )
        else:
            operations.append(
                _operation(
                    "symbol_add",
                    "schematic",
                    instance_id,
                    {
                        key: inputs.get(key)
                        for key in (
                            "instance_id",
                            "reference",
                            "symbol_id",
                            "value",
                            "footprint_id",
                        )
                    },
                    action_id,
                    depends_on=[config_id],
                    resolution_status=status,
                    finding_ids=finding_ids,
                )
            )
    elif action_name == "place_schematic_part":
        instance_id = str(inputs.get("instance_id"))
        operations.append(
            _operation(
                "symbol_place",
                "schematic",
                instance_id,
                copy.deepcopy(inputs),
                action_id,
                depends_on=[operation_id("schematic", "symbol_add", instance_id)],
                resolution_status=status,
                finding_ids=finding_ids,
            )
        )
    elif action_name == "place_pcb_part":
        instance_id = str(inputs.get("instance_id"))
        prerequisite = (
            operation_id("pcb", "mechanical_add", instance_id)
            if inputs.get("mechanical_only")
            else operation_id("pcb", "pcb_sync", "project")
        )
        operations.append(
            _operation(
                "footprint_place",
                "pcb",
                instance_id,
                copy.deepcopy(inputs),
                action_id,
                depends_on=[prerequisite],
                resolution_status=status,
                finding_ids=finding_ids,
            )
        )
    elif action_name == "connect":
        net = inputs.get("net", {})
        name = str(net.get("name") or inputs.get("net_name") or action_id)
        operations.append(
            _operation(
                "net_render",
                "schematic",
                name,
                copy.deepcopy(net),
                action_id,
                depends_on=[
                    operation_id("schematic", "symbol_place", endpoint["instance_id"])
                    for endpoint in net.get("endpoints", [])
                ],
                resolution_status=status,
                finding_ids=finding_ids,
            )
        )
    elif action_name == "no_connect":
        endpoint = inputs.get("endpoint", {})
        identity = f"{endpoint.get('instance_id')}:{endpoint.get('pin_number')}"
        operations.append(
            _operation(
                "no_connect_add",
                "schematic",
                identity,
                copy.deepcopy(endpoint),
                action_id,
                depends_on=[operation_id("schematic", "symbol_place", str(endpoint.get("instance_id")))],
                resolution_status=status,
                finding_ids=finding_ids,
            )
        )
    elif action_name == "board_oobb":
        operations.append(
            _operation(
                "outline_create",
                "pcb",
                "board",
                copy.deepcopy(inputs),
                action_id,
                depends_on=[operation_id("pcb", "pcb_sync", "project")],
                resolution_status=status,
                finding_ids=finding_ids,
            )
        )
    elif action_name == "add_block":
        operations = []
    else:
        operations.append(
            _operation(
                str(action_name),
                "schematic",
                action_id,
                copy.deepcopy(inputs),
                action_id,
                depends_on=[project_id],
                resolution_status="blocked",
                finding_ids=finding_ids,
            )
        )
    if action != original:
        raise RuntimeError("high-level action was mutated during expansion")
    return {
        "high_level_action_id": action_id,
        "action": action_name,
        "resolution_status": status,
        "operation_ids": [item["id"] for item in operations],
        "operations": operations,
    }


def _comparable(operation: dict[str, Any]) -> str:
    data = {
        key: value
        for key, value in operation.items()
        if key not in {"id", "source", "_semantic_key"}
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def deduplicate_operations(operations: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records: dict[tuple[str, ...], dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []
    for source in operations:
        operation = copy.deepcopy(source)
        key = tuple(operation.get("_semantic_key", ()))
        if not key:
            key = (str(operation.get("phase")), str(operation.get("action")), str(operation.get("id")))
        previous = records.get(key)
        if previous is None:
            records[key] = operation
            continue
        if _comparable(previous) != _comparable(operation):
            diagnostics.append(
                {
                    "code": "semantic_operation_conflict",
                    "message": "Duplicate semantic operation has different static inputs",
                    "details": {"semantic_key": list(key), "operation_ids": [previous["id"], operation["id"]]},
                }
            )
            previous["resolution_status"] = "blocked"
            continue
        previous["source"]["high_level_action_ids"] = sorted(
            set(previous["source"]["high_level_action_ids"])
            | set(operation["source"]["high_level_action_ids"])
        )
    result = list(records.values())
    result.sort(key=lambda item: (item["phase"], item["action"], item["id"]))
    for item in result:
        item.pop("_semantic_key", None)
    return {"operations": result, "diagnostics": diagnostics}


def validate_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for operation in operations:
        operation_id_value = operation.get("id")
        if operation_id_value in by_id:
            diagnostics.append(
                {
                    "code": "duplicate_operation_id",
                    "message": f"Operation ID '{operation_id_value}' is duplicated",
                    "details": {"operation_id": operation_id_value},
                }
            )
        else:
            by_id[str(operation_id_value)] = operation
        if operation.get("action") not in SUPPORTED_OPERATIONS:
            diagnostics.append(
                {
                    "code": "unsupported_operation",
                    "message": f"Unsupported normalized operation '{operation.get('action')}'",
                    "details": {"operation_id": operation_id_value},
                }
            )
    for operation in operations:
        for dependency in [*operation.get("depends_on", []), *operation.get("after_attempt", [])]:
            if dependency not in by_id:
                diagnostics.append(
                    {
                        "code": "unknown_operation_dependency",
                        "message": f"Operation '{operation.get('id')}' depends on missing '{dependency}'",
                        "details": {"operation_id": operation.get("id"), "dependency": dependency},
                    }
                )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(operation_id_value: str, trail: tuple[str, ...]) -> None:
        if operation_id_value in visiting:
            diagnostics.append(
                {
                    "code": "operation_dependency_cycle",
                    "message": "Operation dependency graph contains a cycle",
                    "details": {"cycle": [*trail, operation_id_value]},
                }
            )
            return
        if operation_id_value in visited or operation_id_value not in by_id:
            return
        visiting.add(operation_id_value)
        operation = by_id[operation_id_value]
        for dependency in operation.get("depends_on", []):
            visit(dependency, (*trail, operation_id_value))
        visiting.remove(operation_id_value)
        visited.add(operation_id_value)

    for operation_id_value in by_id:
        visit(operation_id_value, ())
    return diagnostics
