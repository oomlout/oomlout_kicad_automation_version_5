"""Pure translation from normalized operations to captured Konnect tool calls."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any


BACKEND_NAME = "konnect"
PCB_ORIGIN_MM = (100.0, 100.0)
REQUIRED_TOOLS = {
    "create_project",
    "set_schematic_page",
    "add_schematic_text",
    "add_schematic_component",
    "move_schematic_component",
    "edit_schematic_component",
    "generate_netlist",
    "rotate_schematic_component",
    "connect_to_net",
    "add_wire",
    "add_power_symbol",
    "get_schematic_pin_locations",
    "add_no_connect",
    "list_schematic_components",
    "list_schematic_nets",
    "export_netlist_summary",
    "run_erc",
    "update_pcb_from_schematic",
    "add_board_outline",
    "place_component",
    "move_component",
    "rotate_component",
    "flip_component",
    "get_component_list",
    "get_component_pads",
    "check_clearance",
    "run_drc",
    "save_project",
    "get_project_info",
}
POWER_NETS = {"GND", "+3V3", "+5V"}


def validate_tool_schemas(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = {item.get("name") for item in tools}
    return [
        {
            "code": "required_konnect_tool_missing",
            "message": f"Captured Konnect schema lacks required tool '{name}'",
            "details": {"tool": name},
        }
        for name in sorted(REQUIRED_TOOLS - names)
    ]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _path(root: Path, relative: str) -> str:
    candidate = (root / relative).resolve()
    if not _inside(candidate, root):
        raise ValueError(f"normalized path escapes managed board root: {relative}")
    return str(candidate)


def _call(tool: str, arguments: dict[str, Any], *, mutation: bool = False, bind: str | None = None) -> dict[str, Any]:
    result = {"tool": tool, "arguments": arguments, "mutation": mutation}
    if bind:
        result["bind"] = bind
    return result


def translate_operation(operation: dict[str, Any], board_root: str | Path) -> dict[str, Any]:
    root = Path(board_root).resolve()
    inputs = copy.deepcopy(operation.get("inputs", {}))
    action = operation.get("action")
    schematic = _path(root, "working.kicad_sch")
    board = _path(root, "working.kicad_pcb")
    calls: list[dict[str, Any]] = []
    if action == "project_create":
        calls = [_call("create_project", {"path": str(root), "name": "working"}, mutation=True)]
    elif action == "drc_profile_configure":
        from ...drc_profiles import validate_frozen_profile
        policy = validate_frozen_profile(profile=inputs)
        calls = [_call("set_drc_severities", {"board": board, "severities": policy["severities"]}, mutation=True),
                 _call("get_drc_severities", {"board": board})]
    elif action == "design_rules_configure":
        from ...design_rules import RULE_KEYS, validate_rules
        inputs = validate_rules(command=inputs)
        constraints = {tool_key: inputs[key] for key, (tool_key, _) in RULE_KEYS.items() if key in inputs}
        if constraints:
            calls.append(_call("set_design_rules", {"board": board, **constraints}, mutation=True))
        if "default_netclass_clearance_mm" in inputs:
            calls.append(_call("create_netclass", {"board": board, "name": "Default",
                "clearance": inputs["default_netclass_clearance_mm"]}, mutation=True))
        calls.extend([_call("get_design_rules", {"board": board}), _call("get_netclasses", {"board": board})])
    elif action == "schematic_configure":
        calls = [_call("set_schematic_page", {"schematic": schematic, "size": inputs.get("page", "A4")}, mutation=True)]
    elif action == "schematic_block_title":
        calls = [_call("add_schematic_text", {
            "schematic": schematic,
            **{key: inputs[key] for key in ("text", "x", "y", "size", "rotation", "bold", "justify")},
        }, mutation=True)]
    elif action == "symbol_add":
        calls = [
            _call(
                "add_schematic_component",
                {
                    "schematic": schematic,
                    "lib_id": inputs.get("symbol_id"),
                    "x": 0,
                    "y": 0,
                    "reference": inputs.get("reference"),
                    "value": inputs.get("value"),
                    "footprint": inputs.get("footprint_id"),
                },
                mutation=True,
            )
        ]
    elif action == "symbol_place":
        position = inputs.get("position_mm") or [0, 0]
        reference = inputs.get("reference")
        calls = [_call("move_schematic_component", {"schematic": schematic, "reference": reference, "x": position[0], "y": position[1]}, mutation=True)]
        if inputs.get("rotation_degrees", 0):
            calls.append(_call("rotate_schematic_component", {"schematic": schematic, "reference": reference, "rotation": inputs["rotation_degrees"]}, mutation=True))
        if inputs.get("field_placements"):
            calls.append(_call("edit_schematic_component", {"schematic": schematic, "reference": reference, "field_placements": inputs["field_placements"]}, mutation=True))
    elif action == "net_render":
        for endpoint in inputs.get("endpoints", []):
            if endpoint.get("render_attachment") is False:
                continue
            stub_length_mm = endpoint.get("stub_length_mm", inputs.get("stub_length_mm", 5.08))
            if inputs.get("name") in POWER_NETS:
                calls.extend(
                    [
                        _call(
                            "get_schematic_pin_locations",
                            {"schematic": schematic, "reference": endpoint.get("reference")},
                            bind="pin_location",
                        ),
                        _call(
                            "add_wire",
                            {
                                "schematic": schematic,
                                "x1": {
                                    "$bind": "pin_location.x",
                                    "pin_number": endpoint.get("pin_number"),
                                },
                                "y1": {
                                    "$bind": "pin_location.y",
                                    "pin_number": endpoint.get("pin_number"),
                                },
                                "x2": {
                                    "$bind": "pin_location.offset_x",
                                    "pin_number": endpoint.get("pin_number"),
                                    "distance_mm": stub_length_mm,
                                },
                                "y2": {
                                    "$bind": "pin_location.offset_y",
                                    "pin_number": endpoint.get("pin_number"),
                                    "distance_mm": stub_length_mm,
                                },
                            },
                            mutation=True,
                        ),
                        _call(
                            "add_power_symbol",
                            {
                                "schematic": schematic,
                                "power_net": inputs.get("name"),
                                "rotation": endpoint.get("power_rotation_degrees", 0),
                                "x": {
                                    "$bind": "pin_location.offset_x",
                                    "pin_number": endpoint.get("pin_number"),
                                    "distance_mm": stub_length_mm,
                                },
                                "y": {
                                    "$bind": "pin_location.offset_y",
                                    "pin_number": endpoint.get("pin_number"),
                                    "distance_mm": stub_length_mm,
                                },
                            },
                            mutation=True,
                        ),
                    ]
                )
            else:
                calls.append(
                    _call(
                        "connect_to_net",
                        {
                            "schematic": schematic,
                            "net": inputs.get("name"),
                            "reference": endpoint.get("reference"),
                            "pin_number": endpoint.get("pin_number"),
                            "stub_length": stub_length_mm,
                            "label_type": "net_label",
                        },
                        mutation=True,
                    )
                )
    elif action == "no_connect_add":
        calls = [
            _call("get_schematic_pin_locations", {"schematic": schematic, "reference": inputs.get("reference")}, bind="pin_location"),
            _call("add_no_connect", {"schematic": schematic, "x": {"$bind": "pin_location.x", "pin_number": inputs.get("pin_number")}, "y": {"$bind": "pin_location.y", "pin_number": inputs.get("pin_number")}}, mutation=True),
        ]
    elif action == "schematic_verify":
        calls = [
            _call("generate_netlist", {"schematic": schematic, "format": "kicadxml", "output": _path(root, "reports/schematic_readback.xml")}),
            _call("list_schematic_components", {"schematic": schematic}),
            _call("list_schematic_nets", {"schematic": schematic}),
            _call("export_netlist_summary", {"schematic": schematic}),
        ]
    elif action == "erc_run":
        calls = [_call("run_erc", {"schematic": schematic, "output": _path(root, "reports/erc.json")})]
    elif action == "pcb_sync":
        prototype_policy = ({"prototype_allow_pad_fabrication_omission": True}
                            if inputs.get("profile", {}).get("prototype_allow_pad_fabrication_omission", False) else {})
        calls = [
            _call("generate_netlist", {"schematic": schematic, "format": "kicadxml", "output": _path(root, "reports/schematic_readback.xml")}, bind="saved_schematic_gate"),
            _call("update_pcb_from_schematic", {"schematic": schematic, "board": board, "dry_run": True, **prototype_policy}, bind="sync_revision"),
            _call("update_pcb_from_schematic", {"schematic": schematic, "board": board, "dry_run": False, "expected_plan_revision": {"$bind": "sync_revision"}, **prototype_policy}, mutation=True),
        ]
    elif action == "outline_create":
        vertices = inputs.get("outline_vertices_mm", [])
        xs = [point[0] for point in vertices]
        ys = [point[1] for point in vertices]
        calls = [_call("add_board_outline", {"board": board, "x1": min(xs) + PCB_ORIGIN_MM[0], "y1": min(ys) + PCB_ORIGIN_MM[1], "x2": max(xs) + PCB_ORIGIN_MM[0], "y2": max(ys) + PCB_ORIGIN_MM[1]}, mutation=True)]
    elif action == "mechanical_add":
        calls = [_call("place_component", {"board": board, "footprint": inputs.get("footprint_id"), "reference": inputs.get("reference"), "x": PCB_ORIGIN_MM[0], "y": PCB_ORIGIN_MM[1], "layer": "F.Cu", "rotation": 0}, mutation=True)]
    elif action == "footprint_place":
        anchor = inputs.get("anchor_mm") or [0, 0]
        reference = inputs.get("reference")
        calls = [_call("move_component", {"board": board, "reference": reference, "x": anchor[0] + PCB_ORIGIN_MM[0], "y": anchor[1] + PCB_ORIGIN_MM[1]}, mutation=True)]
        if inputs.get("side") == "back":
            calls.append(_call("flip_component", {"board": board, "reference": reference, "layer": "B.Cu"}, mutation=True))
        if inputs.get("rotation_degrees", 0):
            calls.append(_call("rotate_component", {"board": board, "reference": reference, "rotation": inputs["rotation_degrees"]}, mutation=True))
    elif action == "pcb_verify":
        calls = [_call("get_component_list", {"board": board})]
        for reference in inputs.get("references", []):
            calls.append(_call("get_component_pads", {"board": board, "reference": reference}))
    elif action == "clearance_run":
        calls = [_call("check_clearance", {"board": board, "ref1": pair[0], "ref2": pair[1], "mode": "courtyard"}) for pair in inputs.get("reference_pairs", [])]
    elif action == "route_path":
        from ...routing import backend_net, validate_route
        route = validate_route(command=inputs)
        calls = [_call('get_board_info', {'board':board}, bind='routing_identity'),
                 _call('get_component_pads', {'board':board,'reference':route['start']['reference']}, bind='route_start'),
                 _call('get_component_pads', {'board':board,'reference':route['end']['reference']}, bind='route_end')]
        layers=route.get('segment_layers',[route['layer']]*(len(route['points_mm'])-1))
        for a,b,layer in zip(route['points_mm'],route['points_mm'][1:],layers):
            calls.append(_call('route_trace', {'board':board,'net_name':backend_net(name=route['net']),
                'layer':layer,'width':route['width_mm'],
                'x1':a['x']+PCB_ORIGIN_MM[0],'y1':a['y']+PCB_ORIGIN_MM[1],
                'x2':b['x']+PCB_ORIGIN_MM[0],'y2':b['y']+PCB_ORIGIN_MM[1]}, mutation=True))
        for via in route.get('vias',[]):
            calls.append(_call('add_via', {'board':board,'net_name':backend_net(name=route['net']),
                'x':via['x']+PCB_ORIGIN_MM[0],'y':via['y']+PCB_ORIGIN_MM[1],
                'pad_size':via['diameter_mm'],'drill':via['drill_mm']}, mutation=True))
    elif action == 'copper_pour':
        from ...copper_pours import validate_pour
        from ...routing import backend_net
        pour=validate_pour(command=inputs)
        calls=[_call('get_board_info',{'board':board},bind='routing_identity'),
               _call('add_zone',{'board':board,'name':pour['name'],'net_name':backend_net(name=pour['net']),
                    'layer':pour['layer'],'points':[{'x':p['x']+PCB_ORIGIN_MM[0],'y':p['y']+PCB_ORIGIN_MM[1]} for p in pour['points_mm']],
                    'clearance':pour['clearance_mm'],'min_width':pour['min_width_mm'],
                    'priority':pour['priority'],'pad_connection':pour['pad_connection']},mutation=True,bind='created_pour')]
    elif action == "drc_run":
        # Konnect checks the saved file; live IPC edits are not implicitly saved.
        # A failed save stops this operation before stale-file DRC can run.
        calls = [_call("save_project", {}, mutation=True),
                 _call("run_drc", {"board": board, "output": _path(root, "reports/drc.json")})]
        if inputs.get('refill_zones'):
            # Synchronized backend refills, saves, compares live/saved and only
            # then invokes DRC. Ordinary pre-zone plans remain byte-stable.
            calls[-1]['arguments'].update(sync_live_board=True,refill_zones=True)
            calls[-1]['mutation']=True
    elif action == "project_save_verify":
        if operation.get("phase") == "pcb":
            calls.append(_call("save_project", {}, mutation=True))
        calls.append(_call("get_project_info", {"path": _path(root, "working.kicad_pro")}))
    else:
        return {"status": "unsupported", "diagnostic": {"code": "unsupported_normalized_operation", "message": f"No Konnect mapping for '{action}'", "details": {"action": action}}}
    return {"status": "ready", "backend": BACKEND_NAME, "operation_id": operation.get("id"), "calls": calls}


def pcb_to_local(x: float, y: float) -> list[float]:
    return [x - PCB_ORIGIN_MM[0], y - PCB_ORIGIN_MM[1]]
