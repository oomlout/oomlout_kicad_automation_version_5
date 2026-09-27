"""Action discovery and the compiler orchestration boundary.

Stage 01 intentionally stops before compilation. The seam reports that state
explicitly so callers cannot mistake an empty result for a successful plan.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from .connectivity import symbols_from_inspections
from .plans import PlanError, compile_frozen_bundle
from .sources import SourceError, build_local_libraries, refresh_sources
from .debug import emit, span, traced


REQUIRED_ACTION_CALLABLES = (
    "main",
    "get_high_level_action",
    "expand_action",
)


def _action_signature_problems(module: ModuleType) -> list[str]:
    """Enforce the keyword-only dictionary boundary for future actions."""

    problems: list[str] = []
    for function_name, required in (
        ("main", ("board", "command")),
        ("expand_action", ("action",)),
    ):
        signature = inspect.signature(getattr(module, function_name))
        positional = [
            parameter.name
            for parameter in signature.parameters.values()
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        ]
        if positional:
            problems.append(
                f"{function_name} has positional parameters: {', '.join(positional)}"
            )
        for parameter_name in required:
            parameter = signature.parameters.get(parameter_name)
            if parameter is None or parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
                problems.append(f"{function_name}.{parameter_name} must be keyword-only")
    return problems


def _diagnostic(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details}


def _load_action_module(action_name: str, module_path: Path, use_package: bool) -> ModuleType:
    if use_package:
        package = __package__ or "oomlout_kicad_automation_version_5"
        return importlib.import_module(f"{package}.actions.{action_name}.working")

    digest = hashlib.sha256(str(module_path.resolve()).encode("utf-8")).hexdigest()[:16]
    module_name = f"_kicad_action_fixture_{action_name}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create an import specification for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@traced(scope="compiler.discover_actions")
def discover_actions(actions_root: str | Path | None = None) -> dict[str, Any]:
    """Discover action folders in deterministic name order and validate their API."""

    default_root = Path(__file__).resolve().parent / "actions"
    root = default_root if actions_root is None else Path(actions_root)
    use_package = root.resolve() == default_root.resolve()
    actions: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []

    if not root.is_dir():
        diagnostics.append(
            _diagnostic(
                "actions_root_missing",
                f"Action root does not exist: {root}",
                actions_root=str(root),
            )
        )
        return {"status": "invalid", "actions": actions, "diagnostics": diagnostics}

    for folder in sorted(root.iterdir(), key=lambda item: item.name):
        if not folder.is_dir() or folder.name.startswith("_"):
            continue
        module_path = folder / "working.py"
        if not module_path.is_file():
            diagnostics.append(
                _diagnostic(
                    "action_module_missing",
                    f"Action folder '{folder.name}' has no working.py",
                    action=folder.name,
                    path=str(module_path),
                )
            )
            continue
        try:
            module = _load_action_module(folder.name, module_path, use_package)
        except Exception as exc:  # named diagnostic is the public failure boundary
            diagnostics.append(
                _diagnostic(
                    "action_import_failed",
                    f"Action '{folder.name}' could not be imported: {exc}",
                    action=folder.name,
                    path=str(module_path),
                    exception_type=type(exc).__name__,
                )
            )
            continue

        missing = [
            name for name in REQUIRED_ACTION_CALLABLES if not callable(getattr(module, name, None))
        ]
        if missing:
            diagnostics.append(
                _diagnostic(
                    "invalid_action_api",
                    f"Action '{folder.name}' is missing required callables: {', '.join(missing)}",
                    action=folder.name,
                    path=str(module_path),
                    missing=missing,
                )
            )
            continue
        signature_problems = _action_signature_problems(module)
        if signature_problems:
            diagnostics.append(
                _diagnostic(
                    "invalid_action_signature",
                    f"Action '{folder.name}' does not use the dictionary-only command API",
                    action=folder.name,
                    path=str(module_path),
                    problems=signature_problems,
                )
            )
            continue
        actions[folder.name] = {
            name: getattr(module, name) for name in REQUIRED_ACTION_CALLABLES
        }

    return {
        "status": "ready" if not diagnostics else "invalid",
        "actions": actions,
        "diagnostics": diagnostics,
    }


@traced(scope="compiler.compile_board")
def compile_board(board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Resolve local sources and publish a deterministic offline compiler bundle."""

    discovery = discover_actions(kwargs.get("actions_root"))
    if discovery["status"] == "invalid":
        return {
            "status": "invalid",
            "board": board,
            "discovered_actions": list(discovery["actions"]),
            "diagnostics": list(discovery["diagnostics"]),
        }
    project_root = Path(kwargs.get("project_root", Path.cwd())).resolve()
    source_root = Path(
        kwargs.get(
            "source_root",
            project_root / "source_file" / "oomp_source_overlay",
        )
    ).resolve()
    parts_root = Path(kwargs.get("parts_root", project_root / "parts")).resolve()
    output_root = Path(
        kwargs.get(
            "output_root",
            parts_root / str(board.get("board_project_id")) / "kicad_board_files",
        )
    ).resolve()
    try:
        emit(event="PATHS", scope="compiler", source_root=str(source_root),
             parts_root=str(parts_root), output_root=str(output_root))
        with span(scope="compiler.refresh_sources", count=len(board.get("source_dependencies", []))):
            refreshed = refresh_sources(
                board.get("source_dependencies", []), source_root, parts_root
            )
        inspections = refreshed["inspections"]
        with span(scope="compiler.symbols"):
            symbols = symbols_from_inspections(inspections)
        with tempfile.TemporaryDirectory(prefix="kicad-local-libraries-") as temp_dir:
            library_root = Path(temp_dir)
            with span(scope="compiler.local_libraries", temporary_path=str(library_root)):
                build_local_libraries(
                    inspections, library_root, str(board.get("board_project_id"))
                )
            with span(scope="compiler.frozen_bundle", output_root=str(output_root)):
                result = compile_frozen_bundle(
                    board,
                    symbols,
                    output_root,
                    inspections=inspections,
                    source_manifest=refreshed["canonical"],
                    libraries_source=library_root,
                )
        reports = output_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        with (reports / "source_refresh.yaml").open("w", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(refreshed["runtime"], stream, sort_keys=False, allow_unicode=True)
        result.update(
            {
                "board": board,
                "discovered_actions": list(discovery["actions"]),
                "diagnostics": list(discovery["diagnostics"]),
                "source_refresh": refreshed["runtime"],
            }
        )
        return result
    except (PlanError, SourceError, OSError, ValueError) as exc:
        emit(event="ERROR", scope="compiler.compile_board", error=str(exc), error_type=type(exc).__name__)
        diagnostic = (
            exc.diagnostic()
            if isinstance(exc, (PlanError, SourceError))
            else _diagnostic(
                "compiler_failed",
                f"Offline compiler failed: {exc}",
                exception_type=type(exc).__name__,
            )
        )
        return {
            "status": "invalid",
            "board": board,
            "discovered_actions": list(discovery["actions"]),
            "diagnostics": [*discovery["diagnostics"], diagnostic],
        }
