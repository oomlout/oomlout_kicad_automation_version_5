# Reviewed deterministic prototype routing

AI experiments use the exact live Konnect PCB on an owned scratch project.
Query pads, route a bounded set, save, retain native DRC and inspect the copper.
Accepted geometry is then explicitly written into Python `route_path` command
dictionaries. Normal compilation makes no live/AI decisions. Changed Python
needs a fresh execution; never resume an older immutable plan.

Design-time `tests/kicad/support/route_proposal.py` and `route_group_proposal.py`
can propose bounded paths using actual IPC pad measurements plus saved copper.
Their output is explicitly PROPOSAL_NOT_ACCEPTED. Review endpoints, dimensions,
reserved regions and interactions, trial through Konnect, save and run exhaustive
normal DRC before manually transcribing accepted choices into Python. These
helpers do not run in ordinary compilation or rewrite authoritative intent.

The reusable default is two copper layers, no controlled-impedance prerequisite.
Existing single-layer commands retain their meaning. For a layer-changing path:

```python
route_path(board=board, command={
    "name": "supply_bridge", "net": "+3V3", "layer": "F.Cu", "width_mm": 0.25,
    "start": {"reference": "J1", "pin_number": "1"},
    "end": {"reference": "R1", "pin_number": "1"},
    "points_mm": [{"x": 14, "y": 14}, {"x": 16.825, "y": 14}, {"x": 20, "y": 10.825}],
    "segment_layers": ["F.Cu", "B.Cu"],
    "vias": [{"x": 16.825, "y": 14, "diameter_mm": 0.6, "drill_mm": 0.3}],
    "reason": "Reviewed scratch MCP experiment, see project evidence",
})
```

All coordinates are board-local millimetres. Each segment has one layer;
every layer transition needs exactly one explicit through-via. No inner layers,
blind/buried vias, guessed default via dimensions or automatic route invention.
Saved verification compares every track and via (net, coordinates, dimensions,
layers); undeclared arcs/zones are rejected. End pads must match the actual
first/last copper layers. Native DRC must be run after saving the populated PCB.

For repeated pad numbers, an endpoint can explicitly add `pad_selector` with
`size_mm: {x: ..., y: ...}`, `shape` and `pad_type`, copied from actual MCP
readback. The route endpoint coordinate is also required to match. Exactly one
physical pad must match; it is never selected by first item or unstable UUID.
Logical reference/pin/net membership remains checked against the independent
electrical intent. This is useful for thermal pad arrays, not permission to
guess ambiguous schematic pin mappings.

The clean-test harness requires the same complete frozen operation ledger,
current saved board and native DRC hashes, verified oracle/fidelity/profile,
zero measured unconnected items and no retained electrical/outline/keepout
findings. It retains ERC and disabled-check details. Passing reduced light DRC
does not mean normal/tight checks or physical RF/manufacturing qualification passed.

Every execution DRC also includes the mandatory snapshot electrical audit in
`electrical_audit.py`; see `DESIGN_RULES.md`. A light project severity map alone
is insufficient because KiCad gates some collision checks on clearance being
enabled. No successful routing report may omit this same-revision exhaustive
native check. Preserve negative fixtures showing that the guard catches faults.
