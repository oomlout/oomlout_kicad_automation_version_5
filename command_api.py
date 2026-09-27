"""Readable dictionary-only boundary for board authoring actions.

Board definitions call every action with keyword-only ``board`` and ``command``
arguments.  The command remains a visible plain dictionary in Python source so
humans and future automation can understand or edit it without remembering a
positional signature.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def dispatch_command(
    *,
    operation: Callable[..., dict[str, Any]],
    board: dict[str, Any],
    command: dict[str, Any],
    required_keys: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Validate and dispatch one keyword-only dictionary command."""

    if not isinstance(command, dict):
        raise TypeError("command must be a dictionary")
    missing = set(required_keys) - set(command)
    if missing:
        raise ValueError(f"command is missing keys: {sorted(missing)}")
    return operation(board=board, **dict(command))


def connection_command(command: dict[str, Any]) -> dict[str, Any]:
    """Translate the readable two-endpoint connection shape to authoring keys."""

    if not isinstance(command, dict):
        raise TypeError("command must be a dictionary")
    missing = {"end_a", "end_b"} - set(command)
    if missing:
        raise ValueError(f"connection command is missing keys: {sorted(missing)}")

    endpoints: dict[str, dict[str, Any]] = {}
    for endpoint_name in ("end_a", "end_b"):
        endpoint = command[endpoint_name]
        if not isinstance(endpoint, dict):
            raise TypeError(f"connection {endpoint_name} must be a dictionary")
        endpoint_missing = {"part", "pin_name"} - set(endpoint)
        if endpoint_missing:
            raise ValueError(
                f"connection {endpoint_name} is missing keys: {sorted(endpoint_missing)}"
            )
        endpoints[endpoint_name] = endpoint

    translated = {
        "part_a": endpoints["end_a"]["part"],
        "pin_a": endpoints["end_a"]["pin_name"],
        "part_b": endpoints["end_b"]["part"],
        "pin_b": endpoints["end_b"]["pin_name"],
    }
    for source, target in (
        (("end_a", "pin_number"), "pin_number_a"),
        (("end_b", "pin_number"), "pin_number_b"),
    ):
        endpoint_name, key = source
        if key in endpoints[endpoint_name]:
            translated[target] = str(endpoints[endpoint_name][key])
    if "net_name" in command:
        translated["net_name"] = command["net_name"]
    return translated
