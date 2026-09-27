"""Current-run-only final reporting; never promote checker findings to success."""
from __future__ import annotations

import json
import re
from collections import Counter
from .drc_profiles import LIGHT_ENABLED


def operation_failure_summary(*, results: list[dict]) -> list[dict]:
    """Keep actionable failures visible; full raw calls remain in the ledger."""
    keys = ("operation_id", "action", "status", "certainty", "reason", "errors", "blocked_by")
    return [{key: row[key] for key in keys if key in row}
            for row in results if row.get("status") != "success"]


def routing_summary(*, results: list[dict], saved_drc: dict | None = None) -> dict:
    """Report actual selected-path execution separately from full connectivity."""
    routes = [r for r in results if r.get("action") == "route_path"]
    pours = [r for r in results if r.get('action') == 'copper_pour']
    copper = routes + pours
    checks = [c.get("semantic", {}).get("structured", {}) for r in results
              if r.get("action") == "drc_run" and r.get("status") == "success"
              for c in r.get("calls", []) if c.get("tool") == "run_drc"
              and c.get("semantic", {}).get("success") is True]
    verification = [r["routing_verification"] for r in results
                    if r.get("status") == "success" and "routing_verification" in r]
    remaining = checks[-1].get("unconnected_items") if checks else None
    if type(remaining) is not int or remaining < 0:
        remaining = None
    findings = saved_drc.get("unconnected_items", []) if saved_drc is not None else (checks[-1].get("violations", []) if checks else [])
    pending_nets = sorted({net for finding in findings
                           if finding.get("rule") == "unconnected_items"
                           for item in finding.get("items", [])
                           for net in re.findall(r"\[([^\]]+)\]", item.get("description", ""))})
    accepted = bool(copper and all(r.get("status") == "success" for r in copper)
                    and verification and all(v.get("status") == "ready" for v in verification))
    status = ("NOT_RUN" if not copper else "ROUTING_INCOMPLETE" if not accepted or remaining
              else "ROUTING_UNVERIFIED" if remaining is None else "ROUTED")
    native_matches = saved_drc is not None and isinstance(saved_drc.get("unconnected_items"), list) and len(saved_drc["unconnected_items"]) == remaining
    if copper and not native_matches:
        status = "ROUTING_UNVERIFIED"
    electrical = [v for v in (saved_drc or {}).get('violations', [])
                  if v.get('rule') in LIGHT_ENABLED or not v.get('rule')]
    audits=[r['electrical_audit'] for r in results if r.get('action')=='drc_run' and 'electrical_audit' in r]
    audit_ready=bool(audits and audits[-1].get('status')=='ready' and audits[-1].get('all_track_errors') is True)
    if audit_ready:
        electrical.extend(audits[-1].get('electrical_findings',[]))
    if status == 'ROUTED':
        if not audit_ready or not isinstance((saved_drc or {}).get('violations'), list):
            status = 'ROUTING_UNVERIFIED'
        elif electrical:
            status = 'ROUTING_ELECTRICAL_FINDINGS'
        elif audits[-1].get('electrical_pass') is not True or audits[-1].get('remaining_unconnected_items') != 0:
            status = 'ROUTING_UNVERIFIED'
    if copper and electrical:
        status = 'ROUTING_ELECTRICAL_FINDINGS'
    return {"status": status, "route_operations": len(routes),
            'copper_pour_operations':len(pours),
            "accepted_declared_paths": accepted,
            "selected_nets": sorted({n for v in verification for n in v.get("selected_nets", [])}),
            "remaining_unconnected_items": remaining if remaining is not None else "not measured",
            "remaining_net_names": pending_nets if checks else "not measured",
            "remaining_net_names_complete": native_matches,
            "saved_verifications": verification,
            "electrical_findings": electrical,
            "electrical_audit": audits[-1] if audits else {'status':'NOT_RUN'},
            "scope": "Declared prototype paths/pours only; all connections require native fill/save/connectivity proof. No manufacturing/RF approval."}


def failure_report(*, report: dict, results: list[dict]) -> str:
    routing = report.get("routing") or routing_summary(results=results)
    # The complete revision-bound reports stay in status.json / mcp_results.yaml.
    # Avoid duplicating thousands of saved coordinates and UUIDs in the console.
    routing_display = {k:v for k,v in routing.items() if k not in {'saved_verifications','electrical_audit','electrical_findings'}}
    routing_display['electrical_finding_count'] = len(routing.get('electrical_findings', []))
    audit = routing.get('electrical_audit', {})
    routing_display['electrical_audit'] = {k:audit[k] for k in
        ('status','electrical_pass','all_track_errors','remaining_unconnected_items','pcb_sha256','diagnostics') if k in audit}
    lines = ["FINAL FAILURE / CHECKER REPORT", f"Run status: {report['status']}",
             "Routing: " + json.dumps(routing_display, ensure_ascii=False),
             "Profile: " + json.dumps(report.get("profile", {"status": "NOT_RESOLVED"}), sort_keys=True)]
    if report.get("error"):
        lines.append("Run failure: " + json.dumps(report["error"], ensure_ascii=False))
    failures = [r for r in results if r.get("status") in {"failed", "blocked"}]
    lines.append(f"Failed/blocked operations: {len(failures)}")
    for result in results:
        if "drc_profile_verification" in result:
            policy = result["drc_profile_verification"]
            lines.append(f"DRC mode: {policy['mode']} (saved verification: {policy['status']}; reduced scope: {policy['reduced_scope']})")
            lines.append("DISABLED DRC checks (not passed): " + ", ".join(policy["disabled_checks"]))
        if "design_rules_verification" in result:
            lines.append("Saved DRC rules: " + json.dumps(result["design_rules_verification"], ensure_ascii=False))
    exceptions = [entry for result in results for entry in result.get("footprint_fidelity", {}).get("accepted_exceptions", [])]
    lines.append(f"Accepted prototype metadata omissions: {len(exceptions)} (not lossless fidelity)")
    lines.extend(json.dumps(entry, ensure_ascii=False) for entry in exceptions)
    for result in failures:
        lines.append(json.dumps({k: result[k] for k in ("operation_id", "action", "status", "reason", "errors", "blocked_by") if k in result}, ensure_ascii=False))
    for step in report.get("steps", []):
        if step["status"] in {"FAILED", "BLOCKED", "NOT_RUN", "SKIPPED"}:
            lines.append("Step: " + json.dumps(step, ensure_ascii=False, default=str))
    for action in ("erc_run", "clearance_run", "drc_run"):
        matches = [r for r in results if r.get("action") == action]
        if not matches:
            lines.append(f"{action}: NOT_RUN in this execution")
        for result in matches:
            lines.append(f"{action}: invocation {result['status']} (not an electrical sign-off)")
            for call in result.get("calls", []):
                semantic = call.get('semantic', {})
                structured = semantic.get('structured', {})
                if isinstance(structured, dict) and 'violations' in structured:
                    summary = {k:v for k,v in structured.items() if k != 'violations'}
                    summary['returned_findings_by_rule'] = dict(Counter(v.get('rule', v.get('type','unknown'))
                        for v in structured['violations']))
                    lines.append(json.dumps({'tool':call.get('tool'),'success':semantic.get('success'),
                        'reason':semantic.get('reason'),'checker_summary':summary}, ensure_ascii=False, default=str))
                    lines.append('Full findings: native checker report and mcp_results.yaml (counts above do not imply sign-off).')
                else:
                    lines.append(json.dumps(semantic, ensure_ascii=False, default=str))
    unrun = [r for r in results if r.get("status") == "skipped"]
    lines.append(f"Operations not run: {len(unrun)}")
    lines.extend(f"{r['action']}: {r.get('reason', 'not run')}" for r in unrun)
    lines.append("Complete route geometry, numeric deltas, native findings and raw calls: status.json and bundle/mcp_results.yaml.")
    lines.append("Controlled impedance / RF qualification: outside default prototype acceptance, NOT tested.")
    lines.append("Evidence: " + str(report.get("evidence", "see status.json")))
    return "\n".join(lines) + "\n"
