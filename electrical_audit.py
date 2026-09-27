"""KiCad 10 electrical DRC with the copper-clearance engine actually active.

KiCad gates track/graphic collision tests behind clearance severity. A light
project may suppress physical margins, but must not accidentally suppress shorts.
Run on a byte-identical saved PCB copy, enabling only this engine prerequisite
through supported Konnect metadata tools. Never change the open project's mode.
"""
from __future__ import annotations
import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from .drc_profiles import LIGHT_ENABLED


def all_track_drc(*, board: Path, output: Path) -> dict:
    """Read-only independent native check; no save/refill or board mutation flags."""
    cli=Path(shutil.which('kicad-cli') or r'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe')
    args=[str(cli),'pcb','drc','--all-track-errors','--severity-all','--format','json','--output',str(output),str(board)]
    result=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
    evidence={'args':args,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr,
              'cli_sha256':hashlib.sha256(cli.read_bytes()).hexdigest()}
    (output.parent/'native_command.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    if result.returncode!=0: raise ValueError(f'Native all-track electrical check failed: {evidence}')
    return evidence


def run_electrical_audit(*, bundle_root: Path, output: Path, transport) -> dict:
    from .executor import interpret_tool_result
    root=bundle_root.resolve(); output=output.resolve()
    output.relative_to(root/'reports')
    output.mkdir(parents=True,exist_ok=False)
    audit=output/'snapshot'; audit.mkdir()
    files=[p for p in root.glob('working.kicad_*') if p.is_file() and p.suffix in
           {'.kicad_pcb','.kicad_pro','.kicad_sch','.kicad_dru'}]
    inputs={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    calls=[]
    record={'status':'failed','electrical_pass':False,'diagnostics':[],
            'pcb_sha256':inputs.get('working.kicad_pcb'),'input_sha256':inputs,
            'scope':'Electrical safety only; positive physical clearance findings retained but not gated in light mode'}
    def call(*, tool, arguments):
        raw=transport.call_tool(tool,arguments)
        semantic=interpret_tool_result(raw)
        calls.append({'tool':tool,'arguments':arguments,'raw':raw,'semantic':semantic})
        if not semantic['success']: raise ValueError(f'Electrical audit tool failed: {tool}: {semantic}')
        return semantic['structured']
    try:
        for path in files: shutil.copy2(path,audit/path.name)
        original=json.loads((root/'working.kicad_pro').read_text(encoding='utf-8'))
        expected=copy.deepcopy(original)
        severities=expected['board']['design_settings']['rule_severities']
        # Preserve actual numerical constraints and unrelated settings. The
        # active native engine can now report intersections even in light mode.
        overrides={key:'error' for key in LIGHT_ENABLED | {'clearance'}}
        severities.update(overrides)
        call(tool='set_drc_severities',arguments={'board':str(audit/'working.kicad_pcb'),'severities':overrides})
        if json.loads((audit/'working.kicad_pro').read_text(encoding='utf-8')) != expected:
            raise ValueError('Audit project differs beyond declared severity metadata')
        call(tool='run_drc',arguments={'board':str(audit/'working.kicad_pcb'),'output':str(output/'konnect_drc.json'),'limit':10000})
        native_command=all_track_drc(board=audit/'working.kicad_pcb',output=output/'drc.json')
        native=json.loads((output/'drc.json').read_text(encoding='utf-8'))
        if not isinstance(native.get('violations'),list) or not isinstance(native.get('unconnected_items'),list):
            raise ValueError('Native electrical audit is incomplete')
        violations=[{**v,'rule':v.get('type',v.get('rule'))} for v in native['violations']]
        findings=[v for v in violations if v.get('rule') in LIGHT_ENABLED or not v.get('rule')]
        # A zero-distance copper clearance finding is an overlap regardless of
        # whether a native geometry provider labels it clearance or shorting.
        import re
        findings.extend(v for v in violations if v.get('rule')=='clearance'
                        and re.search(r'actual\s+(?:<\s*)?0(?:\.0+)?\s*mm',v.get('description','')))
        for filename,digest in inputs.items():
            if hashlib.sha256((root/filename).read_bytes()).hexdigest()!=digest:
                raise ValueError(f'Original changed during electrical audit: {filename}')
        if hashlib.sha256((audit/'working.kicad_pcb').read_bytes()).hexdigest()!=inputs['working.kicad_pcb']:
            raise ValueError('Audit PCB is not the byte-identical saved revision')
        record.update(status='ready',electrical_pass=not findings,electrical_findings=findings,
                      remaining_unconnected_items=len(native['unconnected_items']),
                      retained_physical_findings=[v for v in violations if v not in findings],
                      all_track_errors=True,native_command=native_command,
                      native_report_sha256=hashlib.sha256((output/'drc.json').read_bytes()).hexdigest())
    except Exception as exc:
        record['diagnostics'].append(f'{type(exc).__name__}: {exc}')
    finally:
        (output/'calls.json').write_text(json.dumps(calls,indent=2),encoding='utf-8')
        (output/'verification.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
        (root/'reports/electrical_audit.json').write_text(json.dumps({**record,'evidence':str(output)},indent=2),encoding='utf-8')
    return record
