# Footprint fidelity and backend selection

OOMP library content is authoritative. New PCB instances retain source mounting
attributes, description/tags, model paths/transforms, graphics, custom fields,
and Reference/Value styling. Schematic reference/value/net assignment and
authored PCB position/side/rotation are instance overrides, not permission to
replace library styling with generic defaults.

`footprint_fidelity.verify_saved_footprints(bundle_root=...)` independently reads
the saved board and project-local libraries. It compares unknown content too;
there is no blanket skip for non-copper graphics. It accounts for native
side/local-coordinate transforms and upright text, ignores backend identifiers
and instance nets, and uses a one-nanometre numeric tolerance. Models remain
definition-local under KiCad's native footprint transforms. Exact electrical
endpoints, hole mechanics and authored placements have separate checks; a
fidelity comparison alone does not prove them.

Native semantic normalization includes an explicitly false jumper flag, a
redundant closing polygon vertex, and stroke `default` versus `solid` only.
KiCad 10.0.6 `EDA_SHAPE::GetLineStyle` resolves DEFAULT to SOLID unconditionally
and `Serialize` uses that getter (not a global preference):
https://raw.githubusercontent.com/KiCad/kicad-source-mirror/10.0.6/common/eda_shape.cpp
lines 200–217 and 2526–2531. Stroke widths, dash/dot styles, polygon geometry,
true jumper flags and thermal angles remain checked. This is effective native
geometry equivalence, not a new metadata-loss exception or source modification.

Final PCB save verification fails on missing or mismatched fidelity data. DRC
is preceded by a Konnect save, and its input/output PCB hashes are recorded in
the operation ledger. A successful invocation is not a clean electrical report.

Projects can select an isolated tested backend with `konnect/backend.json`:
`executable` is relative to project root (or an explicit absolute path), and
`sha256` pins the executable. `backend_config.backend_path` selects it; the
transport verifies its hash before launching and records binary provenance.
Without a project selection the established installed backend is used. No
binary is downloaded, built or replaced by ordinary compilation. A backend
that loses source data now fails saved verification rather than passing.

See `../konnect/FIDELITY_REPAIR.md` for this project's isolated repair provenance.
Unsupported library clauses must be diagnosed before mutation; add support
and corresponding tests rather than stripping them from OOMP source assets.
# CQ-010 prototype exception

The main project's Python settings explicitly enable
`prototype_allow_pad_fabrication_omission`. Shared factories and new projects
default to False. Only missing `pad_prop_heatsink`/`pad_prop_mechanical` pad
properties qualify; no other metadata or geometry differences are excused.
The isolated sync backend receives this opt-in on both dry-run/apply and binds
it into the revision. Its stderr identifies each omitted source pad property.
Source and packaged libraries remain unchanged; omission is only in typed IPC
construction because KiCad 10.0.6 has no fabrication-property field.

Independent saved-footprint comparison first attempts exact matching, then
allows an otherwise-identical pad lacking the single approved property. Missing
pads, altered geometry, wrong/new properties and other clauses still fail.
`reports/footprint_fidelity.json` contains `accepted_exceptions` and explicitly
distinguishes `accepted_prototype_metadata_loss` from `lossless_semantic_match`.
The executor retains these warnings and the final batch report lists them.
An eventual backend preserving the property matches exactly with no exception
entry; remove the Python opt-in when the compatible path is proven.
