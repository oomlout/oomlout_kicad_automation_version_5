"""Runtime progress only; never include these timestamps in frozen plans."""

from __future__ import annotations

import inspect
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

_lock = threading.Lock()
_directory: Path | None = None


def configure(*, directory: Path | None = None) -> None:
    """Enable persistent debug.log/debug.jsonl alongside a batch run's evidence."""
    global _directory
    _directory = Path(directory) if directory is not None else None
    if _directory is not None:
        _directory.mkdir(parents=True, exist_ok=True)


def describe(*, values: dict) -> dict:
    """Keep context useful and bounded; never dump whole boards or tool payloads."""
    selected = {}
    for key in (
        "board_project_id", "mode", "phase", "id", "action", "reference",
        "preferred_id", "oomp_part_id", "instance_id", "name", "description",
        "net_name", "pin", "pin_name", "pin_a", "pin_b", "pin_number", "pin_number_a",
        "pin_number_b", "schematic", "board", "path", "output_path",
        "pcb_x_mm", "pcb_y_mm", "schematic_x", "schematic_y",
        "pcb_rotation", "pcb_side", "schematic_rotation", "width_units",
        "height_units", "corner_radius_mm", "layers",
        "block", "text", "x", "y", "size", "justify",
    ):
        value = values.get(key)
        if isinstance(value, (str, int, float, bool)):
            selected[key] = value
    for key in ("part", "part_a", "part_b"):
        part = values.get(key)
        if isinstance(part, dict):
            selected[key] = part.get("reference") or part.get("preferred_id") or part.get("instance_id") or part.get("description")
    for key in ("end_a", "end_b"):
        if isinstance(values.get(key), dict):
            selected[key] = describe(values=values[key])
    return selected


def emit(*, event: str, scope: str, **details) -> None:
    """Print immediately and append one durable line per event when configured."""
    if os.environ.get("KICAD_DEBUG", "1").lower() in {"0", "false", "off"}:
        return
    record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, "scope": scope, **details}
    suffix = " ".join(f"{key}={json.dumps(value, ensure_ascii=True, default=str)}" for key, value in details.items())
    line = f"[{record['time']}] [{event}] {scope} {suffix}".rstrip()
    with _lock:
        print(line, flush=True)
        if _directory is not None:
            with (_directory / "debug.log").open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
            with (_directory / "debug.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")


@contextmanager
def span(*, scope: str, **details):
    started = time.monotonic()
    emit(event="START", scope=scope, **details)
    try:
        yield
    except BaseException as exc:
        emit(event="ERROR", scope=scope, elapsed_s=round(time.monotonic() - started, 3), error_type=type(exc).__name__, error=str(exc), **details)
        raise
    else:
        emit(event="END", scope=scope, elapsed_s=round(time.monotonic() - started, 3), **details)


def traced(*, scope: str):
    """Preserve callable signatures while tracing all public action entry points."""
    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs).arguments
            context = describe(values=bound)
            for key in ("board", "command", "action", "kwargs"):
                value = bound.get(key)
                if isinstance(value, dict):
                    context.update(describe(values=value))
            with span(scope=scope, **context):
                result = function(*args, **kwargs)
                if isinstance(result, dict):
                    status = result.get("status", result.get("resolution_status"))
                    if status is not None:
                        emit(event="RESULT", scope=scope, status=status, **context)
                return result
        return wrapped
    return decorate
