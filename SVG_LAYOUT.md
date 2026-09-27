# SVG-assisted layout and routing studies

Run `action_plan_svg_layout.bat` from the project root. The batch dispatches the
normal `oomlout_ai_roboclick_layout` record's `run_python` action, defined in
`working_oomp_populate_layout.py` and included by `working_oomp_populate_main.py`.
The Python extra accepts Roboclick's `--kwargs` dictionary. This is a **design-time**
action, not a live PCB editor or a replacement for native KiCad checking. It
requires the existing compiled main-board bundle and reads it without changing
it. It does not launch KiCad, run tests, delete parts, or rewrite accepted Python.

Edit `working_oomp_layout.py` to change the explicit constraints and objective.
The generic reusable boundary is:

```python
from oomlout_kicad_automation_version_5.actions.plan_svg_layout.working import main

main(
    board={"bundle_path": "path/to/compiled/kicad_board_files"},
    command={
        "svg_roots": ["path/to/oomp_electronic_version_5/parts"],
        "output": "path/to/a/new/layout_study",
        "settings": {
            "fixed_references": ["J1"],
            "track_width_mm": 0.25,
            "clearance_mm": 0.20,
        },
    },
)
```

All dimensions are board-local millimetres. The existing Konnect compiler adds
its own board origin later. Public action arguments are keyword-only dictionaries.
Action discovery includes this action, but frozen execution-plan expansion
explicitly refuses it: no optimization, AI, network or live calls during compile.

## What it does

1. Read the frozen placement, electrical net/NC tables and exact selected
   machine-/hand-solder footprint libraries. Verify the packaged asset hashes;
   source and packaged hashes differ only where the existing packager records
   its header-whitespace normalization. No new normalization is introduced here.
2. Import `data/working_svg_assembly.svg` for each exact OOMP source ID. Read its
   physical mm/viewBox and pin markers. Fit only orthogonal orientation and
   translation, not arbitrary stretching. Record residuals and equivalent fits.
   These are assembly drawings, not solder-land definitions. Unregistered
   artwork stays in the asset collection, not guessed onto the PCB. Missing
   artwork uses the exact selected footprint's pad/envelope preview; never a
   substitute part. DTDs, scripts and external/style resources are rejected.
3. Deterministically improve weighted half-perimeter wirelength (HPWL), global
   movable-component spread and displacement. Block names never enter the
   objective: blocks may interleave. Pour nets are excluded from HPWL, not pulled
   together as if ground needed point-to-point tracks. Tie ordering is stable; no
   random seed or stochastic optimizer. Keep configured anchors, mechanical
   holes, outline, reservations, rotation policy and component gaps. The main
   study fixes only connectors, antenna and holes; other parts can move up to
   20 mm and rotate orthogonally. Editable pairwise proximity limits protect
   clock/RF/decoupling relationships. Dense packages reserve extra routing space
   outside the courtyard. This is a bounded heuristic, not proof
   of the globally shortest or most aesthetically pleasing board.
4. Build stable per-net physical-pad spanning trees and propose two-layer tracks
   with bounded A*. Avoid other-net pads/copper, NC pads, holes, mechanical
   envelopes, reservations and via/drill collisions. Same-net drills still
   exclude new vias. Duplicate numbered lands retain selectors; coincident
   same-net/common-layer copper is already touching and needs no zero-length
   track. Keep trace width fixed and explicit; **optimize length, not width**.
   Analytically checked short fanouts reach the raster without clearing obstacle
   cells. USB/UART/header nets use authored group order and shared-axis cost;
   higher via cost and a modest bottom-layer penalty favor fewer transitions.
   These are soft preferences, not a minimum-via or differential-pair guarantee.
   Every actual layer transition still requires an explicit, clearance-checked
   through via in the normal `route_path` contract.
   GND uses explicit two-sided `copper_pour` rectangles excluding the antenna
   reservation and mechanical envelopes. No pairwise GND MST tracks are emitted.
   Pour boundaries are **unfilled intent**; native fill, islands, thermal spokes,
   interlayer connectivity and any required stitching remain native audit gates.
5. Independently check candidate segment/pad/via math and endpoint selectors.
   Fail the offline study on unexplained collisions or component-envelope
   overlap. This conservative rectangular-pad model is not native DRC.
6. Render a before/after Matplotlib PNG/SVG and an assembly SVG with imported
   artwork. Export readable placement and `route_path` dictionaries in a draft
   Python module. Hash inputs and check they remain unchanged.

## Artifacts and interpretation

Each run creates a new directory under `plan/evidence/svg_layout/`; an existing
output is refused. No one “latest” file is overwritten.

- `layout_comparison.png` / `.svg`: baseline airwires versus candidate tracks;
  red F.Cu, blue B.Cu, dashed ochre unresolved links. Pad boxes are conservative
  envelopes, not an exact rendering of rounded/custom shapes.
- `assembly_before.svg` / `assembly_after.svg`: registered OOMP artwork plus
  source pad envelopes. Artwork fit details are in `source_assets.json`.
- `layout_candidate.py`: editable `PLACEMENT_COMMANDS`, `ROUTE_COMMANDS` and
  `COPPER_POUR_COMMANDS` (also `copper_pour_intents.json`).
  Reference/instance IDs remain distinct. Placement commands need their owning
  part object when deliberately promoted to `place_pcb_part`.
- `route_intents.json`: the normal explicit route dictionaries; usable as input
  to the existing scratch endpoint-preflight harness **after** rebuilding the
  candidate placements on an owned scratch board, not on the old live board.
- `placements.json`, `settings.json`, `source_assets.json`, `assets/`: reproducible
  inputs, source IDs/hashes, moves, objective/metrics and imported original SVGs.
- `unresolved_connections.json`: failed candidate links with exact endpoints and
  reasons. These are planner links, **not** KiCad's native unconnected-item count.
- `draft_checks.json`, `report.json`, `actions.log`: offline findings, canonical
  candidate hash, all progress and explicit outstanding live gates.

Exit 0 means a proposal was generated with no unresolved planner links or pending
ground-fill task; it still
does not mean native acceptance. Exit 3 means a generated partial proposal;
other failures remain errors. Roboclick correctly reports exit 3 as incomplete,
not success. Its normal action captures child stdout; `actions.log` in the study
directory records progress during that wait. Tests stay in `run_tests.bat`.

## Limits and promotion

Front-side components, rectangular boards and two copper layers only in v1.
Back-side components, custom pads, offset drills and arced courtyards are rejected
instead of guessed. The fixed OOBB outline is not resized; “unused space” is
represented by global spread and wirelength. Generic grid defaults are .15 mm,
.25 mm traces and .20 mm clearance; this project's explicit overrides are a
.10 mm grid, .17 mm clearance and 10 mm-equivalent via cost,
matching its frozen design rules. There is no controlled-impedance prerequisite.
Fine-pitch escape can be blocked by the conservative grid even when a finer
analytic fanout would work. No blocked endpoint is carved through obstacles.

Do **not** concatenate this draft with the old reviewed tracks. A moved placement
invalidates old absolute route coordinates. Review/promote a consistent placement
and route set into authoritative Python; fresh compile/sync through Konnect,
independent saved-netlist and NC gate, exact live endpoint checks, saved footprint
fidelity/layers/geometry, exhaustive native electrical audit and retained ERC/DRC
are still mandatory. Two clean live replays remain required for acceptance.
No direct PCB-file authoring or oracle rewrite is introduced.

## Native copper-pour action

`actions.copper_pour.working.main(board=board, command={...})` declares an explicit
rectangle with `name`, `net`, `layer`, `points_mm`, `clearance_mm`, `min_width_mm`,
`pad_connection`, `priority`, `reason`. No implicit net selection or polygon
editing. Compile verifies the net and board bounds and only emits plans.
Execution requires exact live PCB identity, creates the zone via Konnect IPC,
then requests synchronized native refill/save before DRC. The executor requires
positive refill/save evidence and independently reads saved zone name/net/layer,
boundary, priority, clearance, minimum thickness, pad mode and filled polygons.
Unexpected/duplicate/missing/unfilled zones fail, as do extra tracks/vias/arcs.
Filled polygons alone cannot pass connectivity: the independent all-track native
electrical audit, retained unconnected count and normal replay gates still apply.
The action is implemented/offline tested; new live pour acceptance is pending.

Dependencies: Python, PyYAML, NumPy and Matplotlib (the tested local environment
has NumPy 2.4.4 / Matplotlib 3.10.9). No raster image-generation service is used.

## Reusable routing policy and beginning-to-end boundary

These are framework policies, not ESP32 reference-name patches. A new project
using `plan_svg_layout` without settings overrides receives them too:

- Horizontal, vertical or 45-degree segments **including exact pad-to-grid
  escapes**. Every proposed route declares `angle_policy: orthogonal_45`;
  route validation and the independent draft checker reject other angles.
  Historical explicitly authored routes without the new field remain readable;
  they are not silently rewritten or considered evidence for the new policy.
- Reserve legal outward exits of dense fine-pitch packages before long routes,
  including narrow ground-pad access. Reservations are obstacles for other nets,
  not generated copper. Missing reservations are reported, not confused with
  proven unroutable endpoints. No reference names or logical blocks are special.
- Default two bounded fresh-draft routing passes; at most five. Failed net groups
  receive earlier priority on retry while authored bus order stays intact. Retry
  via penalty defaults to 0.6 times the initial cost. Width, clearance, keepouts
  and via geometry never relax. Keep the best result by unresolved count, then
  via count, then length; a worse retry cannot replace it. The main study opts
  into three passes through its normal editable OOMP settings.
- Distinguish fixed-geometry/fanout limits from occupied exits, search-budget
  exhaustion and remaining congested search space. Compare escape availability
  with and without proposed copper. Retain each pass, not just the winning count.
- Ground is explicitly **not** considered connected by reservations, front-layer
  preferences or zone rectangles. Native refill/save, island/thermal/interlayer
  continuity and required stitching must be checked on the actual saved board.

`routing_passes.json` retains orders/costs/failures; `fanout_reservations.json`
retains both protected exits and unavailable reservations. The report names the
selected pass and unproven ground properties. Settings are propagated through
`working_oomp_populate_layout.py` into the normal OOMP record, not hidden in an AI
conversation. Synthetic-project regressions exercise these shared defaults.

The reproducible process remains: OOMP source records -> pure compiled assets
and electrical oracle -> design-time Roboclick Python layout study -> coherent
reviewed Python placement/route/pour definitions -> fresh deterministic Konnect
execution -> native fill/save/check -> independent saved-board acceptance ->
two clean live replays. Do not wire the optimizer into ordinary compilation or
promote an incomplete study merely to make the batch exit successfully. The
current deterministic batch still consumes the previously accepted partial
route definitions until the new candidate is coherently promoted. Study output
is not proof that this entire chain has completed.

## Ground-first iterative policy (V1.f)

The planner constructs `ground_plan.json` before signal routing: physical ground
lands, both layer zone intents, mechanical/antenna exclusions and pending native
fill status. Ground pin exits get priority in a bounded joint escape selector.
The compiler now sequences declared `copper_pour` operations before `route_path`
operations, regardless of declaration order; final native refill/save/checks
still follow signals. Ground copper is not treated as an immovable obstacle to
every signal, nor is plane connectivity assumed from a rectangle.

`ground_access.json` compares conservative per-layer free-space regions and pad
access before/after signals. It deliberately does not invent interlayer links or
thermal spokes. These diagnostics can reveal severed access, but are NOT KiCad
fill polygons, electrical connectivity, RF return-path proof or acceptance.

Shared defaults:

- `layer_preferred_axes={'F.Cu':'x','B.Cu':'y'}` and
  `layer_cross_axis_cost=1.35`; reverse x/y explicitly if appropriate. Diagonal
  and perpendicular travel remain allowed; preferences cannot block escapes.
- Joint fanouts: at most six candidates per pin, 5,000 search nodes, preferred
  reach 1.2 mm / minimum 0.6 mm. Branch-and-bound considers conflicts together,
  prioritizes ground then the number of protected pins, and records incomplete
  selection honestly. These reservations are constraints, not copper.
- `adaptive_grid_alignment=True`: on a blocked raster endpoint, try grids
  aligned to the endpoint coordinates. Pitch, half-diagonal guard, copper width,
  clearance and drill rules are unchanged. Exact fanouts still check all
  obstacles. `escape_distances_mm` bounds the analytic search to 3.6 mm by default.
- Two local repair rounds: identify competing draft nets, preserve unrelated
  routes, reroute affected bus groups and keep a trial only if its completion /
  via / length score improves. Congestion history costs discourage reuse of
  blocked endpoint neighborhoods by other nets. This is a legal-draft heuristic,
  NOT a full overlapping-resource PathFinder implementation.
- One placement-feedback trial: use failed endpoint neighborhoods to propose
  new legal placements, route again and independently check the geometry. Keep
  the best result, never silently replace it with a worse last trial.

Project-specific relationships remain human-editable in `working_oomp_layout.py`:
the MCU/header rule uses actual shared terminal centroids, penalizes distance and
unrelated reset/control components inside a 4 mm corridor. A graded obstruction
cost avoids a flat penalty that traps small moves. Explicit `placement_groups`
permit an IC and nearby support components to translate together, preserving
relative positions and all existing proximity/edge/mechanical constraints.
The shared engine contains no U1/J2/J3/SW1/SW2 special cases.

Additional evidence: `routing_repairs.json`, `routing_searches.json`,
`congestion_history.json`, `placement_feedback.json`, relationship metrics in
`placements.json` and report summaries. Rejected placement trials retain their
placements, routes and findings. Roboclick's child timeout is 2,400 seconds for
the main bounded multi-pass study; its progress remains in `actions.log`.

Still separate work: true same-net branch/junction endpoint support, direction-
state bend minimization, external Freerouting benchmarking and coherent native
candidate promotion/replay. `routing_capability_probe.py` only discovers the
pinned backend's optional export/integration toolsets and checks local runtime
availability; it does not run a benchmark or change the editor. Ordinary builds
remain offline compilation, and a partial candidate is not routed-board success.
