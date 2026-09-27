"""Plain-dictionary KiCad board authoring and declaration finalization."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import re
from typing import Any, Callable

from oomp_populate_helper import build_oomp_id
from .prototype import project_settings


MOUNTING_HOLE_OOMP_ID = "mechanical_mounting_hole_3_2_mm_round_unplated"
REFERENCE_PATTERN = re.compile(r"^[A-Za-z]+[1-9][0-9]*$")
PLACEMENT_KEYS = {
    "schematic_x",
    "schematic_y",
    "schematic_rotation",
    "pcb_x_mm",
    "pcb_y_mm",
    "pcb_rotation",
    "pcb_side",
}


def new_board(
    *,
    board_project_id: str,
    kicad_name: str,
    mode: str,
    settings: dict[str, Any] | None = None,
    source_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fresh board state; no state is shared between evaluations."""

    board: dict[str, Any] = {
        "board_project_id": board_project_id,
        "kicad_name": kicad_name,
        "mode": mode,
        "settings": project_settings(overrides=settings),
        "parts": [],
        "blocks": [],
        "connections": [],
        "no_connects": [],
        "board_actions": [],
        "warnings": [],
        "diagnostics": [],
        "source_dependencies": [],
        "kicad_details": {},
        "_next_creation_index": 1,
    }
    board["_owner_token"] = f"board:{id(board)}"
    if source_record is not None:
        board["source_record"] = source_record
    return board


def _add_diagnostic(
    board: dict[str, Any], code: str, message: str, **details: Any
) -> dict[str, Any]:
    item = {"code": code, "message": message, "details": details}
    board["diagnostics"].append(item)
    return item


def _invalid(action: str, diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "invalid", "action": action, "diagnostics": diagnostics}


def _creation_fields(board: dict[str, Any], kind: str) -> dict[str, Any]:
    index = board["_next_creation_index"]
    board["_next_creation_index"] += 1
    return {
        "_creation_index": index,
        "_creation_key": f"{kind}:{index:06d}",
        "_board_owner": board["_owner_token"],
    }


def _is_owned(board: dict[str, Any], item: Any, expected_kind: str) -> bool:
    return (
        isinstance(item, dict)
        and item.get("_board_owner") == board.get("_owner_token")
        and item.get("_kind") == expected_kind
    )


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _placement_diagnostics(kwargs: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    problems: list[tuple[str, str, dict[str, Any]]] = []
    for key in kwargs:
        if (key.startswith("schematic_") or key.startswith("pcb_")) and key not in PLACEMENT_KEYS:
            problems.append(
                (
                    "unknown_placement_key",
                    f"Unknown placement key '{key}'",
                    {"key": key},
                )
            )
    schematic_present = ("schematic_x" in kwargs, "schematic_y" in kwargs)
    if schematic_present[0] != schematic_present[1]:
        problems.append(
            (
                "incomplete_schematic_placement",
                "schematic_x and schematic_y must be supplied together",
                {},
            )
        )
    for key in ("schematic_x", "schematic_y"):
        if key in kwargs and (
            not isinstance(kwargs[key], int) or isinstance(kwargs[key], bool)
        ):
            problems.append(
                (
                    "invalid_schematic_coordinate",
                    f"{key} must be an integer grid coordinate",
                    {"key": key, "value": kwargs[key]},
                )
            )
    pcb_present = ("pcb_x_mm" in kwargs, "pcb_y_mm" in kwargs)
    if pcb_present[0] != pcb_present[1]:
        problems.append(
            (
                "incomplete_pcb_placement",
                "pcb_x_mm and pcb_y_mm must be supplied together",
                {},
            )
        )
    for key in ("pcb_x_mm", "pcb_y_mm", "schematic_rotation", "pcb_rotation"):
        if key in kwargs and not _finite_number(kwargs[key]):
            problems.append(
                (
                    "invalid_placement_number",
                    f"{key} must be finite",
                    {"key": key, "value": kwargs[key]},
                )
            )
    if "pcb_side" in kwargs and kwargs["pcb_side"] not in {"front", "back"}:
        problems.append(
            (
                "invalid_pcb_side",
                "pcb_side must be exactly 'front' or 'back'",
                {"value": kwargs["pcb_side"]},
            )
        )
    return problems


def add_part(board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    action = "add_part"
    diagnostics: list[dict[str, Any]] = []
    for key in ("oomp_part_id", "subsection", "description"):
        if not isinstance(kwargs.get(key), str) or not kwargs[key].strip():
            diagnostics.append(
                _add_diagnostic(
                    board,
                    "missing_part_identity",
                    f"{key} is required and must be a non-empty string",
                    key=key,
                )
            )
    index = kwargs.get("index")
    if index is not None and (
        not isinstance(index, int) or isinstance(index, bool) or index <= 0
    ):
        diagnostics.append(
            _add_diagnostic(
                board,
                "invalid_instance_index",
                "index must be a positive integer",
                index=index,
            )
        )
    preferred_id = kwargs.get("preferred_id")
    if preferred_id is not None and (
        not isinstance(preferred_id, str) or not REFERENCE_PATTERN.fullmatch(preferred_id)
    ):
        diagnostics.append(
            _add_diagnostic(
                board,
                "invalid_preferred_reference",
                "preferred_id must be letters followed by a positive decimal integer without leading zeroes",
                preferred_id=preferred_id,
            )
        )
    if "value" in kwargs:
        diagnostics.append(
            _add_diagnostic(
                board,
                "value_override_not_allowed",
                "Component values are resolved from OOMP source metadata",
            )
        )
    if "mechanical_only" in kwargs and not isinstance(kwargs["mechanical_only"], bool):
        diagnostics.append(
            _add_diagnostic(
                board,
                "invalid_mechanical_flag",
                "mechanical_only must be boolean",
                value=kwargs["mechanical_only"],
            )
        )
    block = kwargs.get("block")
    if isinstance(block, dict) and not _is_owned(board, block, "block"):
        diagnostics.append(
            _add_diagnostic(
                board,
                "foreign_block",
                "Part block dictionaries must belong to the current board evaluation",
            )
        )
    elif block is not None and not isinstance(block, (str, dict)):
        diagnostics.append(
            _add_diagnostic(
                board,
                "invalid_block_reference",
                "block must be a block dictionary or exact block name",
            )
        )
    for code, message, details in _placement_diagnostics(kwargs):
        diagnostics.append(_add_diagnostic(board, code, message, **details))
    if diagnostics:
        return _invalid(action, diagnostics)

    part = copy.deepcopy(kwargs)
    if isinstance(block, dict):
        part["block"] = block
    part["mechanical_only"] = kwargs.get("mechanical_only", False)
    part["schematic_rotation"] = kwargs.get("schematic_rotation", 0)
    part["pcb_rotation"] = kwargs.get("pcb_rotation", 0)
    part["pcb_side"] = kwargs.get("pcb_side", "front")
    part["_kind"] = "part"
    part.update(_creation_fields(board, "part"))
    board["parts"].append(part)
    return part


def add_block(board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    action = "add_block"
    diagnostics: list[dict[str, Any]] = []
    name = kwargs.get("name")
    if not isinstance(name, str) or not name.strip():
        diagnostics.append(
            _add_diagnostic(board, "invalid_block_name", "Block name must be non-empty")
        )
    elif any(block.get("name") == name for block in board["blocks"]):
        diagnostics.append(
            _add_diagnostic(
                board, "duplicate_block_name", f"Block name '{name}' is already declared", name=name
            )
        )
    parent = kwargs.get("parent")
    if isinstance(parent, dict) and not _is_owned(board, parent, "block"):
        diagnostics.append(
            _add_diagnostic(
                board,
                "foreign_block",
                "Parent block dictionaries must belong to the current board evaluation",
            )
        )
    elif parent is not None and not isinstance(parent, (str, dict)):
        diagnostics.append(
            _add_diagnostic(
                board, "invalid_block_parent", "parent must be a block dictionary or exact name"
            )
        )
    for code, message, details in _placement_diagnostics(kwargs):
        diagnostics.append(_add_diagnostic(board, code, message, **details))
    if diagnostics:
        return _invalid(action, diagnostics)
    block = copy.deepcopy(kwargs)
    if isinstance(parent, dict):
        block["parent"] = parent
    block["_kind"] = "block"
    block.update(_creation_fields(board, "block"))
    board["blocks"].append(block)
    return block


def connect(board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    if "pin_number" in kwargs:
        diagnostics.append(
            _add_diagnostic(
                board,
                "ambiguous_pin_number",
                "Use pin_number_a and/or pin_number_b for a two-ended connection",
            )
        )
    for side in ("a", "b"):
        part = kwargs.get(f"part_{side}")
        if not _is_owned(board, part, "part"):
            diagnostics.append(
                _add_diagnostic(
                    board,
                    "foreign_part",
                    f"part_{side} must be a part returned by this board evaluation",
                    endpoint=side,
                )
            )
        pin = kwargs.get(f"pin_{side}")
        if not isinstance(pin, str) or not pin:
            diagnostics.append(
                _add_diagnostic(
                    board,
                    "invalid_pin_name",
                    f"pin_{side} must be a non-empty string",
                    endpoint=side,
                )
            )
    if diagnostics:
        return _invalid("connect", diagnostics)
    connection = copy.copy(kwargs)
    connection["_kind"] = "connection"
    connection.update(_creation_fields(board, "connection"))
    board["connections"].append(connection)
    return connection


def no_connect(board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    if not _is_owned(board, kwargs.get("part"), "part"):
        diagnostics.append(
            _add_diagnostic(
                board,
                "foreign_part",
                "part must be returned by this board evaluation",
            )
        )
    if not isinstance(kwargs.get("pin"), str) or not kwargs["pin"]:
        diagnostics.append(
            _add_diagnostic(board, "invalid_pin_name", "pin must be a non-empty string")
        )
    if diagnostics:
        return _invalid("no_connect", diagnostics)
    item = copy.copy(kwargs)
    item["_kind"] = "no_connect"
    item.update(_creation_fields(board, "no_connect"))
    board["no_connects"].append(item)
    return item


def _record_override(
    board: dict[str, Any], part: dict[str, Any], action: str, changes: dict[str, Any]
) -> None:
    overridden = {
        key: {"previous": part[key], "replacement": value}
        for key, value in changes.items()
        if key in part and part[key] != value
    }
    if overridden:
        board["warnings"].append(
            {
                "code": "placement_overridden",
                "message": f"{action} replaced earlier placement values",
                "details": {"creation_key": part["_creation_key"], "fields": overridden},
            }
        )


def place_schematic_part(board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    part = kwargs.get("part")
    if not _is_owned(board, part, "part"):
        diagnostic = _add_diagnostic(
            board, "foreign_part", "part must belong to the current board evaluation"
        )
        return _invalid("place_schematic_part", [diagnostic])
    values = {key: value for key, value in kwargs.items() if key != "part"}
    problems = _placement_diagnostics(values)
    if "schematic_x" not in values or "schematic_y" not in values:
        problems.append(
            (
                "missing_schematic_placement",
                "place_schematic_part requires schematic_x and schematic_y",
                {},
            )
        )
    if problems:
        diagnostics = [_add_diagnostic(board, code, message, **details) for code, message, details in problems]
        return _invalid("place_schematic_part", diagnostics)
    changes = {
        "schematic_x": values["schematic_x"],
        "schematic_y": values["schematic_y"],
        "schematic_rotation": values.get("schematic_rotation", part.get("schematic_rotation", 0)),
    }
    _record_override(board, part, "place_schematic_part", changes)
    part.update(changes)
    return part


def place_pcb_part(board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    part = kwargs.get("part")
    if not _is_owned(board, part, "part"):
        diagnostic = _add_diagnostic(
            board, "foreign_part", "part must belong to the current board evaluation"
        )
        return _invalid("place_pcb_part", [diagnostic])
    values = {key: value for key, value in kwargs.items() if key != "part"}
    problems = _placement_diagnostics(values)
    if "pcb_x_mm" not in values or "pcb_y_mm" not in values:
        problems.append(
            (
                "missing_pcb_placement",
                "place_pcb_part requires pcb_x_mm and pcb_y_mm",
                {},
            )
        )
    if problems:
        diagnostics = [_add_diagnostic(board, code, message, **details) for code, message, details in problems]
        return _invalid("place_pcb_part", diagnostics)
    changes = {
        "pcb_x_mm": values["pcb_x_mm"],
        "pcb_y_mm": values["pcb_y_mm"],
        "pcb_rotation": values.get("pcb_rotation", part.get("pcb_rotation", 0)),
        "pcb_side": values.get("pcb_side", part.get("pcb_side", "front")),
    }
    _record_override(board, part, "place_pcb_part", changes)
    part.update(changes)
    return part


def copper_pour(*, board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    from .copper_pours import validate_pour
    pour=validate_pour(command=kwargs)
    if any(a.get('action')=='copper_pour' and a['pour']['name']==pour['name'] for a in board['board_actions']):
        raise ValueError('Duplicate copper pour name')
    action={'action':'copper_pour','_kind':'board_action','pour':pour,**_creation_fields(board,'board_action')}
    board['board_actions'].append(action)
    return action


def route_path(*, board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    from .routing import validate_route
    route = validate_route(command=kwargs)
    if any(a.get('action')=='route_path' and a['route']['name']==route['name'] for a in board['board_actions']):
        raise ValueError('Duplicate route name')
    action={'action':'route_path','_kind':'board_action','route':route,**_creation_fields(board,'board_action')}
    board['board_actions'].append(action)
    return action


def set_drc_mode(*, board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    from .drc_profiles import drc_profile
    if board.get("_drc_mode_declared"): raise ValueError("Only one set_drc_mode declaration is allowed")
    resolved = drc_profile(command=kwargs)
    board["settings"]["drc"] = {k:v for k,v in resolved.items() if k in {"mode", "reason", "solder_mask_checks", "silkscreen_mask_checks"}}
    board["_drc_mode_declared"] = True
    return {"action": "set_drc_mode", "status": "declared", "inputs": copy.deepcopy(board["settings"]["drc"])}


def set_design_rules(*, board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    from .design_rules import validate_rules
    if any(item.get("action") == "set_design_rules" for item in board["board_actions"]):
        raise ValueError("Only one set_design_rules declaration is allowed")
    action = {"action": "set_design_rules", "_kind": "board_action",
              "rules": validate_rules(command=kwargs), **_creation_fields(board, "board_action")}
    board["board_actions"].append(action)
    return action


def board_oobb(board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    settings = project_settings(overrides=board.get("settings"))
    if kwargs.get("layers", settings["copper_layer_count"]) != settings["copper_layer_count"]:
        diagnostics.append(_add_diagnostic(board, "conflicting_copper_layer_count",
            "Set advanced copper_layer_count in project settings; board_oobb.layers must agree"))
    if any(item.get("action") == "board_oobb" for item in board["board_actions"]):
        diagnostics.append(
            _add_diagnostic(
                board, "duplicate_board_oobb", "Only one board_oobb declaration is allowed"
            )
        )
    width_units = kwargs.get("width_units")
    height_units = kwargs.get("height_units")
    for key, value in (("width_units", width_units), ("height_units", height_units)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 2:
            diagnostics.append(
                _add_diagnostic(
                    board,
                    "invalid_oobb_units",
                    f"{key} must be an integer of at least 2",
                    key=key,
                    value=value,
                )
            )
    if diagnostics:
        return _invalid("board_oobb", diagnostics)

    width_mm = 15 * width_units - 1
    height_mm = 15 * height_units - 1
    action = copy.deepcopy(kwargs)
    action["layers"] = settings["copper_layer_count"]
    action.update(
        {
            "action": "board_oobb",
            "width_mm": width_mm,
            "height_mm": height_mm,
            "_kind": "board_action",
        }
    )
    action.update(_creation_fields(board, "board_action"))
    board["board_actions"].append(action)
    positions = [
        (7, 7),
        (width_mm - 7, 7),
        (width_mm - 7, height_mm - 7),
        (7, height_mm - 7),
    ]
    holes = []
    for index, (x_mm, y_mm) in enumerate(positions, start=1):
        hole = add_part(
            board,
            oomp_part_id=MOUNTING_HOLE_OOMP_ID,
            subsection="mechanical",
            description="mounting_hole",
            index=index,
            mechanical_only=True,
            pcb_x_mm=x_mm,
            pcb_y_mm=y_mm,
        )
        holes.append(hole)
    action["hole_creation_keys"] = [hole["_creation_key"] for hole in holes]
    return action


def high_level_action(action_name: str, **kwargs: Any) -> dict[str, Any]:
    return {"status": "declared", "action": action_name, "inputs": copy.deepcopy(kwargs)}


def pending_expansion(action_name: str, action: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return {
        "status": "not_implemented",
        "action": action_name,
        "operation": "expand_action",
        "diagnostic": f"{action_name}.expand_action is implemented in stage 05",
    }


def _normalize_segment(value: str) -> str:
    return str(value).strip().replace(" ", "_").replace(".", "_")


def _block_name(part: dict[str, Any]) -> str | None:
    block = part.get("block")
    if isinstance(block, dict):
        return block.get("name")
    return block


def finalize_declarations(board: dict[str, Any]) -> dict[str, Any]:
    """Allocate deterministic instance IDs after every declaration is known."""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for part in board["parts"]:
        raw_key = (part["subsection"], part["description"])
        normalized_key = tuple(_normalize_segment(value) for value in raw_key)
        part["_raw_identity_key"] = raw_key
        part["_normalized_identity_key"] = normalized_key
        part.pop("instance_id", None)
        part.pop("_assigned_index", None)
        groups.setdefault(normalized_key, []).append(part)

    for normalized_key, group in groups.items():
        raw_keys = {tuple(part["_raw_identity_key"]) for part in group}
        if len(raw_keys) > 1:
            _add_diagnostic(
                board,
                "normalized_identity_collision",
                "Different subsection/description values normalize to the same instance identity",
                normalized=list(normalized_key),
                raw=[list(item) for item in sorted(raw_keys)],
            )
            for part in group:
                part["identity_status"] = "blocked"
            continue

        reserved: dict[int, dict[str, Any]] = {}
        blocked: set[int] = set()
        for part in group:
            explicit = part.get("index")
            if explicit is None:
                continue
            if explicit in reserved:
                _add_diagnostic(
                    board,
                    "duplicate_explicit_index",
                    "Two declarations requested the same explicit instance index",
                    subsection=normalized_key[0],
                    description=normalized_key[1],
                    index=explicit,
                    first_creation_key=reserved[explicit]["_creation_key"],
                    duplicate_creation_key=part["_creation_key"],
                )
                part["identity_status"] = "blocked"
                blocked.add(part["_creation_index"])
            else:
                reserved[explicit] = part

        next_index = 1
        repeated = len(group) > 1
        for part in group:
            if part["_creation_index"] in blocked:
                continue
            assigned = part.get("index")
            if assigned is None and repeated:
                while next_index in reserved:
                    next_index += 1
                assigned = next_index
                reserved[assigned] = part
                next_index += 1
            part["_assigned_index"] = assigned
            taxonomy = {
                "taxonomy_1": "oomp",
                "taxonomy_2": "project",
                "taxonomy_3": "part",
                "taxonomy_4": normalized_key[0],
                "taxonomy_5": normalized_key[1],
            }
            if assigned is not None:
                taxonomy["taxonomy_6"] = str(assigned)
            part["instance_id"] = build_oomp_id(taxonomy)
            part["identity_status"] = "ready"

    seen_ids: dict[str, dict[str, Any]] = {}
    for part in board["parts"]:
        instance_id = part.get("instance_id")
        if instance_id is None:
            continue
        if instance_id in seen_ids:
            _add_diagnostic(
                board,
                "instance_id_collision",
                "Final instance ID collides with an earlier declaration",
                instance_id=instance_id,
                first_creation_key=seen_ids[instance_id]["_creation_key"],
                duplicate_creation_key=part["_creation_key"],
            )
            part.pop("instance_id", None)
            part["identity_status"] = "blocked"
        else:
            seen_ids[instance_id] = part

    membership = [
        [
            part.get("instance_id"),
            part.get("oomp_part_id"),
            part.get("mechanical_only", False),
            _block_name(part),
        ]
        for part in board["parts"]
    ]
    canonical = json.dumps(membership, ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    board["declaration_membership"] = membership
    board["declaration_fingerprint"] = fingerprint
    board["kicad_details"]["declaration_fingerprint"] = fingerprint
    dependencies: list[str] = []
    for part in board["parts"]:
        source_id = part.get("oomp_part_id")
        if source_id not in dependencies:
            dependencies.append(source_id)
    board["source_dependencies"] = dependencies
    board["declaration_status"] = (
        "blocked"
        if any(part.get("identity_status") == "blocked" for part in board["parts"])
        else "ready"
    )
    return board


def evaluate_board_definition(
    board_function: Callable[..., Any],
    *,
    board_project_id: str,
    kicad_name: str,
    mode: str,
    settings: dict[str, Any] | None = None,
    source_record: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    board = new_board(
        board_project_id=board_project_id,
        kicad_name=kicad_name,
        mode=mode,
        settings=settings,
        source_record=source_record,
    )
    signature = inspect.signature(board_function)
    invalid_parameters = [
        name
        for name in ("board", "mode")
        if signature.parameters.get(name) is None
        or signature.parameters[name].kind is not inspect.Parameter.KEYWORD_ONLY
    ]
    if invalid_parameters:
        _add_diagnostic(
            board,
            "invalid_board_function_signature",
            "Board definitions require keyword-only board and mode parameters",
            parameters=invalid_parameters,
        )
        return finalize_declarations(board)
    returned = board_function(board=board, mode=mode, **kwargs)
    if returned is not None and returned is not board:
        if not isinstance(returned, dict):
            _add_diagnostic(
                board,
                "board_function_invalid_result",
                "Board function must mutate/return the board dictionary or return None",
                result_type=type(returned).__name__,
            )
        else:
            board = returned
    return finalize_declarations(board)


def compare_declaration_fingerprint(
    populated_fingerprint: str | None, build_board: dict[str, Any]
) -> dict[str, Any]:
    actual = build_board.get("declaration_fingerprint")
    if not populated_fingerprint:
        return {
            "status": "not_available",
            "diagnostics": [
                {
                    "code": "population_fingerprint_missing",
                    "message": "Populated board record has no declaration fingerprint",
                    "details": {},
                }
            ],
        }
    if populated_fingerprint != actual:
        return {
            "status": "blocked",
            "diagnostics": [
                {
                    "code": "declaration_fingerprint_mismatch",
                    "message": "Populate/build membership differs; compilation is blocked",
                    "details": {"populated": populated_fingerprint, "build": actual},
                }
            ],
        }
    return {"status": "match", "diagnostics": []}
