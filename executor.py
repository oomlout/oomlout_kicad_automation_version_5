"""Dependency-aware external execution of immutable KiCad plans."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .prototype import verify_saved_copper
from .design_rules import expected_rules, verify_saved_rules
from .drc_profiles import validate_frozen_profile, verify_saved_drc_profile
from .footprint_fidelity import verify_saved_footprints
from .routing import validate_live_route, verify_saved_routes
from typing import Any

import yaml

from .adapters.konnect.working import BACKEND_NAME, translate_operation, validate_tool_schemas
from .expansion import validate_operations
from .plans import PlanError, verify_frozen_bundle
from .debug import describe, emit, span, traced
from .schematic_checks import apply_project_metadata, verify_saved_schematic, verify_pcb_worksheet


EXECUTION_OWNER = ".execution-owner.json"
GENERATED_PATHS = ("working.kicad_pro", "working.kicad_sch", "working.kicad_pcb", "mcp_results.yaml")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_fresh_execution(root: str | Path) -> dict[str, Any]:
    target = Path(root).resolve()
    owner_path = target / EXECUTION_OWNER
    existing = [target / name for name in GENERATED_PATHS if (target / name).exists()]
    if existing and not owner_path.is_file():
        raise PlanError(
            "unowned_execution_output",
            "Refusing to replace unowned generated execution output",
            paths=[path.name for path in existing],
        )
    checkpoint = None
    if owner_path.is_file() and existing:
        previous = json.loads(owner_path.read_text(encoding="utf-8"))
        previous_id = str(previous.get("run_id", "previous"))
        checkpoint = target / "reports" / "checkpoints" / previous_id
        checkpoint.mkdir(parents=True, exist_ok=False)
        for path in existing:
            resolved = path.resolve()
            resolved.relative_to(target)
            shutil.move(str(resolved), checkpoint / path.name)
    run_id = str(uuid.uuid4())
    owner_path.write_text(
        json.dumps({"owner": "oomlout_kicad_automation_version_5", "run_id": run_id}, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {"run_id": run_id, "checkpoint": str(checkpoint) if checkpoint else None}


def _structured(result: Any) -> Any:
    if isinstance(result, dict) and isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        for item in result["content"]:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    continue
    return result


def interpret_tool_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict) and result.get("isError") is True:
        return {"success": False, "reason": "mcp_is_error", "structured": _structured(result)}
    structured = _structured(result)
    if isinstance(structured, dict):
        if "error" in structured and structured["error"]:
            return {"success": False, "reason": "json_rpc_error", "structured": structured}
        if structured.get("ipc_available") is False or structured.get("ipc_failure"):
            return {"success": False, "reason": "ipc_unavailable", "structured": structured}
        if structured.get("success") is False or structured.get("ok") is False:
            return {"success": False, "reason": "nested_failure", "structured": structured}
        if str(structured.get("status", "")).lower() in {"error", "failed", "blocked", "unavailable", "conflict"}:
            return {"success": False, "reason": "nested_status_failure", "structured": structured}
    return {"success": True, "reason": None, "structured": structured}


def _find_key(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value and value[key] is not None:
                return value[key]
        for nested in value.values():
            found = _find_key(nested, keys)
            if found is not None:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _find_key(nested, keys)
            if found is not None:
                return found
    return None


def _resolve_binding(value: Any, bindings: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_resolve_binding(item, bindings) for item in value]
    if isinstance(value, dict):
        if "$bind" in value:
            binding = str(value["$bind"])
            if binding == "sync_revision":
                revision = _find_key(bindings.get("sync_revision"), {"plan_revision", "revision"})
                if revision is None:
                    raise ValueError("dry-run result did not contain a plan revision")
                return revision
            if binding.startswith("pin_location."):
                axis = binding.rsplit(".", 1)[1]
                pin_number = str(value.get("pin_number"))
                structured = bindings.get("pin_location")
                pins = _find_key(structured, {"pins"})
                if isinstance(pins, list):
                    for pin in pins:
                        if isinstance(pin, dict) and str(pin.get("number", pin.get("pin_number"))) == pin_number:
                            if axis in pin:
                                return pin[axis]
                            position = pin.get("position")
                            if isinstance(position, dict) and axis in position:
                                return position[axis]
                            if axis in {"offset_x", "offset_y"}:
                                x = pin.get("x")
                                y = pin.get("y")
                                orientation = pin.get("orientation_degrees")
                                distance = value.get("distance_mm")
                                if not all(
                                    isinstance(item, (int, float))
                                    and not isinstance(item, bool)
                                    for item in (x, y, orientation, distance)
                                ):
                                    raise ValueError(
                                        f"pin {pin_number} offset metadata was not returned"
                                    )
                                angle = math.radians(float(orientation))
                                if axis == "offset_x":
                                    return round(float(x) + float(distance) * math.cos(angle), 9)
                                return round(float(y) - float(distance) * math.sin(angle), 9)
                raise ValueError(f"pin {pin_number} location was not returned")
        return {key: _resolve_binding(item, bindings) for key, item in value.items()}
    return value


@traced(scope="executor.execute_frozen_plan")
def execute_frozen_plan(
    bundle_root: str | Path,
    transport: Any,
    *,
    phase: str = "full",
) -> dict[str, Any]:
    root = Path(bundle_root).resolve()
    emit(event="TARGET", scope="executor", bundle=str(root), phase=phase,
         results_file=str(root / "mcp_results.yaml"))
    if phase not in {"schematic", "pcb", "full"}:
        raise ValueError("phase must be 'schematic', 'pcb', or 'full'")
    integrity = verify_frozen_bundle(root)
    if integrity["status"] != "ready":
        raise PlanError("bundle_integrity_failed", "Frozen bundle failed integrity verification", diagnostics=integrity["diagnostics"])
    plan_path = root / "mcp_plan.yaml"
    plan_hash_before = _sha(plan_path)
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    if plan.get("mcp_backend") != BACKEND_NAME:
        raise PlanError("backend_mismatch", "Plan backend does not match Konnect adapter")
    graph_diagnostics = validate_operations(plan.get("operations", []))
    if graph_diagnostics:
        raise PlanError("invalid_operation_graph", "Operation graph is invalid", diagnostics=graph_diagnostics)
    resumed_from: dict[str, Any] | None = None
    if phase == "pcb":
        owner_path = root / EXECUTION_OWNER
        ledger_path = root / "mcp_results.yaml"
        if not owner_path.is_file() or not ledger_path.is_file():
            raise PlanError(
                "pcb_resume_missing_schematic_run",
                "PCB resume requires an owned completed schematic-phase run",
            )
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        previous = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get("phase") != "schematic":
            raise PlanError(
                "pcb_resume_invalid_ledger",
                "PCB resume requires the immediately preceding schematic-phase ledger",
            )
        if previous.get("run_id") != owner.get("run_id"):
            raise PlanError(
                "pcb_resume_owner_mismatch",
                "PCB resume ledger does not match the owned execution run",
            )
        if previous.get("input_plan_sha256") != plan_hash_before:
            raise PlanError(
                "pcb_resume_stale_plan",
                "PCB resume input plan differs from the schematic-phase plan",
            )
        previous_results = {
            item.get("operation_id"): item
            for item in previous.get("results", [])
            if isinstance(item, dict) and item.get("operation_id")
        }
        missing_or_failed = [
            item["id"]
            for item in plan["operations"]
            if item.get("phase") == "schematic"
            and item.get("action") != "erc_run"
            and previous_results.get(item["id"], {}).get("status") != "success"
        ]
        if missing_or_failed:
            raise PlanError(
                "pcb_resume_schematic_incomplete",
                "PCB resume requires every gating schematic operation to have succeeded",
                operations=missing_or_failed,
            )
        for name in ("working.kicad_pro", "working.kicad_sch", "working.kicad_pcb"):
            if not (root / name).is_file():
                raise PlanError(
                    "pcb_resume_output_missing",
                    f"PCB resume requires the saved schematic output '{name}'",
                )
        fresh = {"run_id": str(owner["run_id"]), "checkpoint": None}
        results = {
            item["id"]: copy.deepcopy(previous_results[item["id"]])
            for item in plan["operations"]
            if item.get("phase") == "schematic"
        }
        pending = {
            item["id"]: item
            for item in plan["operations"]
            if item.get("phase") == "pcb"
        }
        resumed_from = {
            "phase": previous.get("phase"),
            "started_at": previous.get("started_at"),
            "run_id": previous.get("run_id"),
        }
    else:
        fresh = prepare_fresh_execution(root)
        results = {}
        pending = {item["id"]: item for item in plan["operations"]}
    session: dict[str, Any] = {}
    emit(event="RUN", scope="executor", run_id=fresh["run_id"], operations=len(pending),
         checkpoint=fresh.get("checkpoint"))
    try:
        with span(scope="executor.initialize"):
            session["initialize"] = transport.initialize()
        with span(scope="executor.list_tools"):
            tools = transport.list_tools()
        tool_items = tools.get("tools", tools) if isinstance(tools, dict) else tools
        session["tool_schema_sha256"] = hashlib.sha256(
            json.dumps(tool_items, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        session["tool_names"] = sorted(item.get("name") for item in tool_items)
        schema_diagnostics = validate_tool_schemas(tool_items)
        rule_command = expected_rules(plan=plan)
        drc_profiles = [op["inputs"] for op in plan["operations"] if op["action"] == "drc_profile_configure"]
        if len(drc_profiles) > 1: raise PlanError("duplicate_drc_profile", "Only one frozen DRC profile is allowed")
        drc_policy = validate_frozen_profile(profile=drc_profiles[0]) if drc_profiles else None
        if drc_policy:
            missing = {"set_drc_severities", "get_drc_severities"} - {t["name"] for t in tool_items}
            schema_diagnostics.extend({"code": "required_konnect_tool_missing", "tool": name} for name in sorted(missing))
        if rule_command:
            missing = {"set_design_rules", "create_netclass", "get_design_rules", "get_netclasses"} - {t["name"] for t in tool_items}
            schema_diagnostics.extend({"code": "required_konnect_tool_missing", "tool": name} for name in sorted(missing))
        if any(op['action']=='copper_pour' for op in plan['operations']):
            schemas={t['name']:t.get('inputSchema',{}).get('properties',{}) for t in tool_items}
            for tool,fields in {'add_zone':{'board','net_name','layer','points','name','priority','pad_connection','clearance','min_width'},
                                'run_drc':{'sync_live_board','refill_zones'},'get_board_info':{'board'}}.items():
                if not fields<=set(schemas.get(tool,{})):
                    schema_diagnostics.append({'code':'pour_capability_missing','tool':tool,'missing':sorted(fields-set(schemas.get(tool,{})))})
        if schema_diagnostics:
            raise PlanError("backend_capability_missing", "Required Konnect tools are unavailable", diagnostics=schema_diagnostics)
        if any(op.get("inputs", {}).get("profile", {}).get("prototype_allow_pad_fabrication_omission") for op in plan["operations"]):
            sync_schema = next(t for t in tool_items if t["name"] == "update_pcb_from_schematic")
            if "prototype_allow_pad_fabrication_omission" not in sync_schema.get("inputSchema", {}).get("properties", {}):
                raise PlanError("backend_capability_missing", "Selected backend lacks explicit CQ-010 prototype exception support")
        while pending:
            progressed = False
            for operation_id_value, operation in list(pending.items()):
                ordering = [*operation.get("depends_on", []), *operation.get("after_attempt", [])]
                if any(item not in results for item in ordering):
                    continue
                progressed = True
                del pending[operation_id_value]
                emit(event="START", scope="executor.operation", operation_id=operation_id_value,
                     completed=len(results), total=len(plan["operations"]), phase=operation.get("phase"),
                     action=operation.get("action"), inputs=describe(values=operation.get("inputs", {})))
                base = {
                    "operation_id": operation_id_value,
                    "action": operation.get("action"),
                    "phase": operation.get("phase"),
                    "calls": [],
                    "warnings": [],
                    "errors": [],
                }
                if phase == "schematic" and operation.get("phase") == "pcb":
                    results[operation_id_value] = {**base, "status": "skipped", "reason": "phase_not_requested"}
                    emit(event="SKIPPED", scope="executor.operation", operation_id=operation_id_value, reason="phase_not_requested")
                    continue
                if operation.get("resolution_status") != "ready":
                    results[operation_id_value] = {**base, "status": "blocked", "reason": "plan_operation_unresolved"}
                    emit(event="BLOCKED", scope="executor.operation", operation_id=operation_id_value, reason="plan_operation_unresolved")
                    continue
                failed_dependencies = [
                    dependency
                    for dependency in operation.get("depends_on", [])
                    if results[dependency]["status"] != "success"
                ]
                if failed_dependencies:
                    results[operation_id_value] = {**base, "status": "blocked", "reason": "dependency_failed", "blocked_by": failed_dependencies}
                    emit(event="BLOCKED", scope="executor.operation", operation_id=operation_id_value, blocked_by=failed_dependencies)
                    continue
                translation = translate_operation(operation, root)
                if translation["status"] != "ready":
                    results[operation_id_value] = {**base, "status": "blocked", "reason": "adapter_unsupported", "errors": [translation["diagnostic"]]}
                    emit(event="BLOCKED", scope="executor.operation", operation_id=operation_id_value, reason="adapter_unsupported")
                    continue
                bindings: dict[str, Any] = {}
                status = "success"
                certainty = "certain"
                for call_index, call in enumerate(translation["calls"], start=1):
                    try:
                        with span(scope="executor.tool", operation_id=operation_id_value,
                                  tool=call["tool"], call=call_index, calls=len(translation["calls"])):
                            arguments = _resolve_binding(call["arguments"], bindings)
                            if call["tool"] == "run_drc":
                                if drc_policy:
                                    base["drc_profile_verification"] = verify_saved_drc_profile(bundle_root=root, profile=drc_policy)
                                    if base["drc_profile_verification"]["status"] != "ready":
                                        raise ValueError("Saved DRC profile differs from frozen intent; DRC not run")
                                if rule_command:
                                    base["design_rules_verification"] = verify_saved_rules(bundle_root=root, command=rule_command)
                                    if base["design_rules_verification"]["status"] != "ready":
                                        raise ValueError("Saved design rules differ from frozen intent; DRC not run")
                                base["drc_input_pcb_sha256"] = _sha(root / "working.kicad_pcb")
                            raw = transport.call_tool(call["tool"], arguments)
                            if call["tool"] == "run_drc":
                                base["drc_output_pcb_sha256"] = _sha(root / "working.kicad_pcb")
                                if base["drc_input_pcb_sha256"] != base["drc_output_pcb_sha256"]:
                                    raise ValueError("Saved board changed during DRC; checker evidence is not revision-bound")
                                drc_report = Path(arguments.get("output", ""))
                                if drc_report.is_file():
                                    base["drc_report_sha256"] = _sha(drc_report)
                    except Exception as exc:
                        status = "failed"
                        certainty = "uncertain" if call.get("mutation") else "certain"
                        base["errors"].append({"code": "transport_or_binding_failure", "message": str(exc), "tool": call["tool"]})
                        break
                    interpreted = interpret_tool_result(raw)
                    emit(event="RESULT", scope="executor.tool", operation_id=operation_id_value,
                         tool=call["tool"], success=interpreted["success"], reason=interpreted["reason"])
                    base["calls"].append({"tool": call["tool"], "arguments": arguments, "raw_result": raw, "semantic": interpreted})
                    if not interpreted["success"]:
                        status = "failed"
                        # Native IPC timeouts can arrive as MCP error replies, not
                        # Python exceptions. A rollback message alone is not an
                        # independent proof of mutation state. Never retry here.
                        failure_text = json.dumps(raw, default=str).lower()
                        if call.get("mutation") and "nng recv failed: timed out" in failure_text:
                            certainty = "uncertain"
                        base["errors"].append({"code": interpreted["reason"], "tool": call["tool"]})
                        break
                    if call['tool']=='run_drc':
                        if call.get('arguments',{}).get('refill_zones') and not (
                            interpreted['structured'].get('zones_refilled') is True and
                            interpreted['structured'].get('live_board_synced') is True and
                            interpreted['structured'].get('zone_refill_source')=='ipc'):
                            status='failed'
                            base['errors'].append({'code':'native_pour_fill_save_not_proven'})
                            break
                        from .electrical_audit import run_electrical_audit
                        base['electrical_audit']=run_electrical_audit(bundle_root=root,
                            output=root/'reports/electrical_audits'/fresh['run_id'],transport=transport)
                        if base['electrical_audit']['status']!='ready':
                            status='failed'
                            base['errors'].append({'code':'electrical_audit_unavailable','diagnostics':base['electrical_audit']['diagnostics']})
                            break
                    if call.get("bind"):
                        bindings[call["bind"]] = interpreted["structured"]
                    if call.get('bind')=='created_pour' and interpreted['structured'].get('source')!='ipc':
                        status='failed'
                        base['errors'].append({'code':'pour_requires_live_ipc'})
                        break
                    if call.get('bind') in {'routing_identity','route_end'}:
                        try:
                            if call['bind']=='routing_identity':
                                identity=interpreted['structured']
                                if identity.get('source')!='ipc' or Path(identity.get('file','')).resolve()!=root/'working.kicad_pcb':
                                    raise ValueError('Exact live routing target not proven')
                            else:
                                validate_live_route(route=operation['inputs'],start=bindings['route_start'],end=bindings['route_end'])
                        except (ValueError,KeyError,TypeError) as exc:
                            status='failed'
                            base['errors'].append({'code':'routing_preflight_failed','message':str(exc)})
                            break
                    if call.get("bind") == "saved_schematic_gate":
                        if drc_policy:
                            base["drc_profile_verification"] = verify_saved_drc_profile(bundle_root=root, profile=drc_policy)
                            if base["drc_profile_verification"]["status"] != "ready":
                                status = "failed"
                                base["errors"].append({"code": "saved_drc_profile_mismatch"})
                                break
                        if rule_command:
                            base["design_rules_verification"] = verify_saved_rules(bundle_root=root, command=rule_command)
                            if base["design_rules_verification"]["status"] != "ready":
                                status = "failed"
                                base["errors"].append({"code": "saved_design_rule_mismatch"})
                                break  # Also enforced on resume, before either sync call.
                        with span(scope="executor.pre_sync_verification"):
                            verification = verify_saved_schematic(bundle_root=root)
                        base["saved_schematic_verification"] = verification
                        if verification["status"] != "ready":
                            status = "failed"
                            base["errors"].append({"code": "saved_schematic_mismatch", "report": "reports/schematic_verification.json"})
                            break  # In particular: do not call either dry-run or apply sync.
                        copper = verify_saved_copper(bundle_root=root, profile=operation["inputs"]["profile"])
                        base["saved_copper_verification"] = copper
                        if copper["status"] != "ready":
                            status = "failed"
                            base["errors"].append({"code": "saved_copper_mismatch", "report": "reports/copper_verification.json"})
                            break
                        worksheet = verify_pcb_worksheet(bundle_root=root, profile=operation["inputs"]["profile"])
                        base["pcb_worksheet_verification"] = worksheet
                        if worksheet["status"] != "ready":
                            status = "failed"
                            base["errors"].append({"code": "pcb_worksheet_mismatch"})
                            break
                if status == 'success' and operation['action']=='project_save_verify' and operation['phase']=='pcb':
                    routes=[op['inputs'] for op in plan['operations'] if op['action']=='route_path']
                    pours=[op['inputs'] for op in plan['operations'] if op['action']=='copper_pour']
                    if routes or pours:
                        base['routing_verification']=verify_saved_routes(bundle_root=root,routes=routes,pours=pours)
                        if base['routing_verification']['status']!='ready':
                            status='failed'
                            base['errors'].append({'code':'saved_routing_mismatch'})
                if status == "success" and drc_policy and (operation["action"] == "drc_profile_configure" or
                        (operation["action"] == "project_save_verify" and operation["phase"] == "pcb")):
                    base["drc_profile_verification"] = verify_saved_drc_profile(bundle_root=root, profile=drc_policy)
                    if base["drc_profile_verification"]["status"] != "ready":
                        status = "failed"
                        base["errors"].append({"code": "saved_drc_profile_mismatch"})
                if status == "success" and rule_command and (operation["action"] == "design_rules_configure" or
                        (operation["action"] == "project_save_verify" and operation["phase"] == "pcb")):
                    base["design_rules_verification"] = verify_saved_rules(bundle_root=root, command=rule_command)
                    if base["design_rules_verification"]["status"] != "ready":
                        status = "failed"
                        base["errors"].append({"code": "saved_design_rule_mismatch"})
                if status == "success" and operation["action"] == "project_create" and operation["inputs"]["profile"].get("pcb_borderless", False):
                    try:
                        base["pcb_metadata"] = apply_project_metadata(
                            bundle_root=root, worksheet="libraries/blank.kicad_wks", section="pcbnew")
                    except Exception as exc:
                        status = "failed"
                        base["errors"].append({"code": "pcb_metadata_failed", "message": str(exc)})
                if status == "success" and (operation["action"] == "project_create" or
                        (operation["action"] == "project_save_verify" and operation["phase"] == "pcb")):
                    copper = verify_saved_copper(bundle_root=root, profile=operation["inputs"]["profile"])
                    base["saved_copper_verification"] = copper
                    if copper["status"] != "ready":
                        status = "failed"
                        base["errors"].append({"code": "saved_copper_mismatch", "report": "reports/copper_verification.json"})
                    worksheet = verify_pcb_worksheet(bundle_root=root, profile=operation["inputs"]["profile"])
                    base["pcb_worksheet_verification"] = worksheet
                    if worksheet["status"] != "ready":
                        status = "failed"
                        base["errors"].append({"code": "pcb_worksheet_mismatch"})
                if status == "success" and operation["action"] == "schematic_configure" and operation["inputs"].get("worksheet"):
                    try:
                        base["metadata"] = apply_project_metadata(bundle_root=root, worksheet=operation["inputs"]["worksheet"])
                    except Exception as exc:
                        status = "failed"
                        base["errors"].append({"code": "project_metadata_failed", "message": str(exc)})
                if status == "success" and operation["action"] == "project_save_verify" and operation["phase"] == "pcb":
                    try:
                        fidelity = verify_saved_footprints(bundle_root=root)
                        base["footprint_fidelity"] = fidelity
                        base["warnings"].extend(fidelity.get("accepted_exceptions", []))
                        if fidelity["status"] != "ready":
                            status = "failed"
                            base["errors"].append({"code":"footprint_fidelity_mismatch","report":"reports/footprint_fidelity.json"})
                    except Exception as exc:
                        status = "failed"
                        base["errors"].append({"code":"footprint_fidelity_unavailable","message":str(exc)})
                if status == "success" and operation["action"] == "schematic_verify":
                    with span(scope="executor.saved_schematic_verification"):
                        verification = verify_saved_schematic(bundle_root=root)
                    base["saved_schematic_verification"] = verification
                    if verification["status"] != "ready":
                        status = "failed"
                        base["errors"].append({"code": "saved_schematic_mismatch", "report": "reports/schematic_verification.json"})
                results[operation_id_value] = {**base, "status": status, "certainty": certainty}
                emit(event="RESULT", scope="executor.operation", operation_id=operation_id_value,
                     status=status, certainty=certainty, errors=base["errors"])
            if not progressed:
                raise PlanError("executor_dependency_deadlock", "No executable operation remains")
    finally:
        try:
            session["shutdown"] = transport.shutdown()
        except Exception as exc:
            session["shutdown"] = {"error": str(exc)}
    plan_hash_after = _sha(plan_path)
    if plan_hash_after != plan_hash_before:
        raise PlanError("plan_changed_during_execution", "Input MCP plan changed during execution")
    ordered = [results[item["id"]] for item in plan["operations"]]
    ledger = {
        "format_version": 1,
        "run_id": fresh["run_id"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "backend": BACKEND_NAME,
        "phase": phase,
        "input_plan_sha256": plan_hash_before,
        "input_plan_unchanged": True,
        "session": session,
        "results": ordered,
    }
    if resumed_from is not None:
        ledger["resumed_from"] = resumed_from
    (root / "mcp_results.yaml").write_text(
        yaml.safe_dump(ledger, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    emit(event="SAVED", scope="executor", path=str(root / "mcp_results.yaml"), operations=len(ordered))
    return ledger
