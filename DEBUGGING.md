# Following a deterministic run

Run `action_run_deterministic_kicad.bat` from the project root. Debug output is on by default. The terminal prints the evidence directory immediately, then reports action starts, finishes, errors, component references, operation IDs, MCP tools and elapsed times.

Main prototype routes live in `working_oomp_populate_main_routing.py`, called by
the same main board definition in populate and build. Six reviewed dictionary
commands currently generate eleven F.Cu segments on three nets. These are an
incremental routing demonstration, not a full-board autorouter. Remaining nets
require explicit further path planning; do not omit USB/RF because impedance
qualification is out of scope. Compile has no AI, IPC or routing mutation.

The final report places routing status near the top as well as the end. Exit
code **3** / `PCB_GENERATED_ROUTING_INCOMPLETE` means generation ran but routing
is absent, unverified or still has unconnected items. It is neither a backend
crash nor full completion. Successful path execution must also pass saved
geometry/net/width/layer verification. Exit 0 after live execution requires
routing proof and zero reported unconnected items; checker findings still need
review. `--build-only` remains compile-only success, never live success.

Tests are separate: run `run_tests.bat` for OFFLINE tests, LOCAL_ASSET tests and compileall. It saves results in `plan/evidence/test_runs/<UTC timestamp>/` and does not build the main board or invoke live MCP. The deterministic runner records tests as `NOT_RUN`; `--build-only` now means compile only, and `--skip-tests` has been removed. Runtime integrity checks and live ERC/DRC operations remain in the deterministic workflow.

`--resume-pcb` reuses an existing owned schematic-phase run without rebuilding. It rechecks frozen-bundle integrity and exact live PCB identity, and the executor rejects stale plans or mismatched ownership. It cannot be combined with `--build-only`. Tool-success status alone is not schematic acceptance; saved-netlist comparison is still mandatory, including on resume. See current `plan/PROGRESS.md` for gate status, not historical failed-run evidence.

The runner and prototype PCB probe launch `pcbnew.exe` with the exact generated
`.kicad_pcb` path directly, not its folder or `.kicad_pro`. No click or ENTER is
required. Use the shared execution-only `live_board.KonnectBoardSession` for
Konnect-backed launches. It checks live identity and delegates launch/polling to
`ensure_live_board`. On Windows, automatically launched editors receive a
board-specific process-local TEMP/TMP directory under the user's temporary
directory (`oomlout-kicad/<board-path-hash>`). This separates their IPC named
pipes; KiCad 10.0.6's shared default can otherwise reach another editor.
The selected endpoint is passed explicitly to both preflight and execution
Konnect children, with effective-setting readback. No global environment,
preferences, installed backend, electrical content or compiler hashes change.
The private runtime directory is retained while the editor may use it; it is
not a design asset and must not be deleted during an active editor session.

An existing editor is reused only after exact live identity proof. If the
requested board has a lock but is unreachable, stop immediately with its path:
save and close that PCB normally once, then rerun `--resume-pcb`. Never delete
its lock or open a duplicate. Other projects may remain open. For a newly
launched editor, make up to 12 read-only identity checks, five seconds apart.
Tool-call time is additional. Unproven identity or launch failure records BLOCKED without
PCB mutations. Unrelated editors and unsaved-work dialogs are never closed or
dismissed. `--non-interactive` retains its explicit no-launch behavior. The
batch's final pause only keeps the finished report visible; it is not a PCB gate.

## Where things live

| Location in the project | Purpose |
|---|---|
| `working_oomp_populate_main.py` | Editable, verbose main-board parts, modules, connections and placements |
| `source_file/` | Project source definitions and source-overlay generation |
| `parts_source/` | Generated source records/assets consumed by OOMP |
| `parts/` | Materialized OOMP records, including component details |
| `parts/oomp_project_github_oomlout_oobb_electronic_breakout_mcu_esp32_simple_test/kicad_board_files/` | Main project's frozen execution bundle and `working.kicad_pro`, `working.kicad_sch`, `working.kicad_pcb` |
| `oomlout_kicad_automation_version_5/actions/` | Seven authoring actions and their expansion entry points |
| `oomlout_kicad_automation_version_5/working.py` | Offline compilation |
| `oomlout_kicad_automation_version_5/executor.py` | Frozen-plan execution and operation results |
| `oomlout_kicad_automation_version_5/adapters/konnect/transport.py` | Communication with the Konnect process |

The generated KiCad files existing does **not** prove the live board is complete. Check execution results and development gates in `plan/PROGRESS.md`.

## Logs

Each batch run writes to `plan/evidence/batch_runs/<UTC timestamp>/`:

- `debug.log`: readable, immediately appended action/compiler/executor/transport events.
- `debug.jsonl`: the same events as one JSON object per line, for later tools.
- `run.log` and `status.json`: batch-level steps and final status; separate test output files contain test details.
- `konnect_schematic/konnect_stderr.log`, `konnect_preflight/endpoint_*/konnect_stderr.log`, or `konnect_pcb/konnect_stderr.log`: backend stderr for the corresponding phase, drained continuously. Only a bounded tail is copied into shutdown results.
- `konnect_preflight/identity_attempts.jsonl` and `identity.json`: all read-only connection attempts and the latest result, including observed board paths and endpoint provenance.

Backend phase logs only exist when that phase was started. The separate test runner suppresses repetitive action traces while retaining its test reports. New deterministic runs do not contain test output files.

CQ-017: child Konnect processes default to `RUST_LOG=konnect_ipc=debug` so native
request/response boundaries identify a stalled IPC command without logging its
electrical payload. An explicitly supplied filter is preserved; the parent/global
environment is not changed. Native `NNG recv failed: Timed out` mutation replies
are recorded as uncertain even when the backend claims rollback; no automatic
tool retry occurs. Independent state verification is still required.

To follow a particular run in a second PowerShell window, substitute the evidence path printed by the batch:

```powershell
Get-Content -LiteralPath 'C:\gh\oobb_electronic_breakout_mcu_esp32_simple_test\plan\evidence\batch_runs\<UTC timestamp>\debug.log' -Tail 30 -Wait
```

## Reading progress

- `START`: entering the named action, compiler step, operation or tool request. This is written before doing the work.
- `END`: that invocation returned without raising. It is **not** proof the requested operation succeeded; check `RESULT` and the execution ledger.
- `RESULT`: returned status or semantic tool result, including reported failures.
- `ERROR`: raised exception, message and elapsed time.
- `WAITING`: an MCP response is still outstanding. Repeats every five seconds with request ID, tool, elapsed time, process ID and backend log path. This is not a timeout or a success.
- `SKIPPED` / `BLOCKED`: work did not execute successfully; reasons/dependencies are recorded.
- `SAVED`: executor results have been written.

If the terminal appears stuck, inspect its last `START`/`WAITING` event and the associated backend log. These traces are available even before the final `mcp_results.yaml` ledger is saved. There is no automatic retry of a possibly applied mutation. Ctrl+C interrupts the batch and records `INTERRUPTED`; process cleanup can take a few seconds. Inspect the last call and existing results before rerunning an interrupted live mutation.

Failure exceptions summarize operation IDs, status, certainty and causes rather
than repeating raw calls. Final checker output retains counts, truncation flags
and rule summaries; inspect native reports and `mcp_results.yaml` for every item.
Saved route verification explicitly reports one-nanometre representation deltas,
never hides them as unexplained formatting or alters the electrical oracle.

If compilation reports Windows `WinError 32` for `kicad_board_files`, something has that generated directory in use. Close applications/windows using the generated project, including its KiCad windows, before rebuilding. The log does not identify the exact lock owner.

## Developer convention

All action entry points (`main`, `get_high_level_action`, `expand_action`) use `@traced(scope="actions.<action>.<entry_point>")` from `debug.py`. Follow this convention for future actions; the discovery-driven debug test checks all discovered actions. Use `span(scope=..., **context)` around long steps and `emit(event=..., scope=..., **context)` for results. Keep context concise; do not dump full boards or raw MCP payloads.

Debug timestamps and timings are runtime evidence only, never canonical plan inputs. Standalone action calls print to the terminal; call `debug.configure(directory=...)` for persistent files outside the batch workflow. Set `KICAD_DEBUG=0` to suppress debug events when needed. Backend stderr is still drained.
