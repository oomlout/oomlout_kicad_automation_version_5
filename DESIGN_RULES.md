# Explicit DRC settings

## Reusable checking modes (CQ-012 / CQ-013)

Every newly compiled project defaults to **light**. This is prototype testing,
not manufacturing acceptance. Python factories/scaffolds and implicit config
fallbacks use the same profile. Select a mode in the authoritative board function:

```python
from oomlout_kicad_automation_version_5.actions.set_drc_mode.working import main as _set_drc_mode

_set_drc_mode(board=board, command={
    "mode": "light",  # or "normal", "tight"
    "reason": "Electrical-only prototype testing",
})
```

- Light enables at error severity: `shorting_items`, `tracks_crossing`,
  `zones_intersect`, `unconnected_items`, `net_conflict`, `isolated_copper`,
  `starved_thermal`, `track_dangling`, `via_dangling`, `invalid_outline`,
  `items_not_allowed`. All other 51 reviewed rules are ignored, including
  copper/pad, hole and courtyard clearances, drilling, mask and silk checks.
- Normal restores `drc_normal_kicad_10.json`, the 62-key native saved KiCad
  10.0.6 map recorded before these changes. Normally ignored rules stay ignored.
- Tight enables all 62 reviewed rules at error severity. It does not tighten
  dimensions. All modes retain explicitly authored numerical constraints.

Optional independent booleans `solder_mask_checks` (`solder_mask_bridge`) and
`silkscreen_mask_checks` (`silk_over_copper`) override the selected mode.
For example, normal with both booleans `False` disables just those two checks.
`True` restores the native severity (error in tight). Unknown keys/rules and
duplicate declarations are rejected; an unreviewed KiCad rule fails closed.

Compilation freezes `drc_profile_configure` after project creation and before
schematic configuration. It emits no design files. Execution uses the isolated
Konnect `set_drc_severities`/`get_drc_severities`, changing only project-local
severity metadata before the editor opens. The shared installation, global
preferences, PCB bytes and unrelated project settings are untouched.
Do not use these file-backed settings against an already open editor; change
Python and start a fresh deterministic run.

Saved exact-map readback is mandatory after configuration, before sync including
resume, after save-before-DRC, and after final save. Evidence lives in
`reports/drc_profile_verification.json`; the actual batch report lists the mode,
reduced scope and every disabled check. Disabled is not passed. Light cannot
satisfy any gate requiring physical clearance or fabrication checks. Independent
saved netlist/NC, footprint fidelity, layer/outline/hole verification and ERC
are not relaxed. Numerical 0.17 mm limits below remain a main-board override.

## Numerical constraints

Use the discovered dictionary action in the authoritative board function:

```python
from oomlout_kicad_automation_version_5.actions.set_design_rules.working import main as _set_design_rules

_set_design_rules(board=board, command={
    "minimum_clearance_mm": 0.17,
    "default_netclass_clearance_mm": 0.17,
    "reason": "Temporary user-approved prototype clearance",
})
```

This is an explicit **main-project override**, not a global 0.17 mm default.
New projects without this action keep native settings. The reusable action also
supports `minimum_track_width_mm`, `minimum_through_hole_diameter_mm`,
`minimum_via_diameter_mm`, and `minimum_hole_to_hole_mm`. All are optional finite
positive millimetre values; omitted fields remain unchanged. Unknown keys,
empty commands/reasons and duplicate declarations are rejected. Through-hole
diameter is a board constraint, not merely a via-only drill setting.

Compilation emits one frozen `design_rules_configure` operation and no KiCad
design files. Execution uses supported Konnect `set_design_rules` and
`create_netclass` calls, followed by their read-only getters. `pcb_routing` is
loaded for netclass tools. Only supplied fields are passed; the Default class
is completed with native values when absent/incomplete. Existing classes,
patterns, unrelated project settings and global preferences are preserved.
No direct project JSON edit or shared backend binary replacement is used.

The operation runs after project creation, before schematic generation and
before PCB Editor opens: KiCad loads these settings on project open. Do not
use this file-backed action to alter a project already open in PCB Editor.
Use a fresh deterministic run after changing the Python declaration.

Saved `.kicad_pro` readback independently checks the requested settings after
configuration, before PCB synchronization (including resume), after saving
before DRC, and after final PCB save. A mismatch fails the operation; it is not
repaired behind an open editor. Reports include requested/actual values and
project hash in `reports/design_rules_verification.json` and the actual batch's
final checker report. Custom `.kicad_dru` rules and explicitly named netclasses
may impose tighter constraints; this action never suppresses their findings.

Live scratch evidence: `plan/evidence/design_rules_probe_20260924_c` proves both
0.17 values and semantic preservation of other settings/class values through
the pinned Konnect build. Earlier failed probe assertions are retained.
Offline tests verify strict input validation, pure compilation/dependencies,
saved mismatches, adapter field selection, default isolation and resume refusal.
# Mandatory electrical audit

Every executor saved-board DRC (including resume) also checks a byte-identical
saved PCB snapshot. KiCad 10 gates track collision tests on the clearance engine,
so simply leaving `tracks_crossing=error` with `clearance=ignore` is insufficient.
Supported Konnect metadata operations enable clearance only in the snapshot;
native read-only `kicad-cli pcb drc --all-track-errors --severity-all` then checks
all collisions. Main project settings and all design bytes remain unchanged.

`reports/electrical_audit.json` records input/native-report hashes, complete
electrical findings, remaining connections and separately retained physical
findings. Positive physical clearance violations do not gate light mode; shorts,
crossings, zero-distance overlaps and the normal light electrical/outline/
keepout checks do. Missing, failed or stale audit cannot produce full routing
success. The clean harness binds it to the current ledger and saved PCB.
Evidence: `plan/evidence/electrical_audit_20260924.md`.
