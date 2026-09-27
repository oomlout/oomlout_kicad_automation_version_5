# Borderless PCB drawing-sheet default

`prototype.project_settings()` defaults `pcb_borderless` to `True`. This flows
through `authoring.new_board`, main/example factories, normal OOMP population,
compiler fallback/profile, `pcb/board.yaml`, and project-create/sync/final-save
plan inputs. It removes the drawing-sheet frame/title block, **not Edge.Cuts**.
To opt out, author `settings={"pcb_borderless": False}`; the executor leaves the
fresh project's PCB worksheet setting alone. Non-booleans are rejected.

The compiler packages `libraries/blank.kicad_wks` when either PCB or schematic
borderless intent needs it. Its contents participate in frozen library hashes.
Normal compilation never opens KiCad or edits `.kicad_pro`/design files.

CQ-009's metadata-only exception is implemented by the same allowlisted helper
as CQ-006. After Konnect creates the fresh owned project, logged execution sets
only `.kicad_pro`'s `pcbnew.page_layout_descr_file` to that portable relative path,
before launching PCB Editor. Other settings/global preferences are preserved.
All PCB content remains authored through Konnect. Never patch an already-open
project's metadata to force the setting; rebuild through the owned fresh route.

Saved-setting verification runs before PCB synchronization, including resume,
and at project creation/final PCB save. A missing asset or changed selection
fails the gate. This check is not visual proof: retain native save/reopen,
relocation, clean-rebuild and visual evidence separately. Export-only suppression
flags do not satisfy the gate. Existing historical bundles are not rewritten.

Default-bearing locations: `prototype.py` (shared default/profile/validation),
`authoring.py` (factory consumer), `working_oomp_populate_main.py` and
`examples/prototype_project.py` (factory consumers), `plans.py` (fallback,
packaging and frozen profile), `executor.py` (fresh metadata application and
saved gates), `schematic_checks.py` (allowlisted metadata/readback). The final
runner report prints the profile including `pcb_borderless`. No KiCad-specific
worksheet default exists in the unrelated SCAD templates.
