"""Deterministic KiCad symbol, reference, pin, net, and no-connect compiler.

This module only interprets authored dictionaries and local symbol bytes.  It does
not write KiCad design files or call a backend.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Iterable


AUTO_NET_MAX_LENGTH = 63
AUTO_NET_HASH_LENGTH = 12
_REFERENCE_RE = re.compile(r"^([A-Za-z]+)([1-9][0-9]*)$")
_UNIT_SUFFIX_RE = re.compile(r"_(\d+)_(\d+)$")
_SAFE_NET_RE = re.compile(r"[^A-Za-z0-9]+")
_NET_ALIASES = {"3V3": "+3V3", "5V": "+5V", "GND": "GND"}


class SymbolParseError(ValueError):
    """Raised for malformed S-expressions, never for a supported unresolved symbol."""


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character in "()":
            tokens.append(character)
            index += 1
            continue
        if character == '"':
            index += 1
            value: list[str] = []
            while index < len(text):
                character = text[index]
                if character == '"':
                    index += 1
                    break
                if character == "\\" and index + 1 < len(text):
                    index += 1
                    escaped = text[index]
                    value.append({"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped))
                else:
                    value.append(character)
                index += 1
            else:
                raise SymbolParseError("unterminated quoted string")
            tokens.append("".join(value))
            continue
        start = index
        while index < len(text) and not text[index].isspace() and text[index] not in "()":
            index += 1
        tokens.append(text[start:index])
    return tokens


def _parse_sexpression(text: str) -> list[Any]:
    tokens = _tokenize(text)
    stack: list[list[Any]] = []
    roots: list[Any] = []
    for token in tokens:
        if token == "(":
            expression: list[Any] = []
            if stack:
                stack[-1].append(expression)
            else:
                roots.append(expression)
            stack.append(expression)
        elif token == ")":
            if not stack:
                raise SymbolParseError("unexpected closing parenthesis")
            stack.pop()
        elif stack:
            stack[-1].append(token)
        else:
            raise SymbolParseError("atom outside root expression")
    if stack:
        raise SymbolParseError("unclosed parenthesis")
    if len(roots) != 1 or not isinstance(roots[0], list):
        raise SymbolParseError("expected exactly one root expression")
    return roots[0]


def _children(expression: list[Any], tag: str) -> list[list[Any]]:
    return [
        item
        for item in expression[1:]
        if isinstance(item, list) and item and item[0] == tag
    ]


def _property(expression: list[Any], name: str) -> str | None:
    for item in _children(expression, "property"):
        if len(item) >= 3 and item[1] == name:
            return str(item[2])
    return None


def _pin_record(expression: list[Any], unit: int) -> dict[str, Any] | None:
    if len(expression) < 3:
        return None
    name_items = _children(expression, "name")
    number_items = _children(expression, "number")
    at_items = _children(expression, "at")
    if not name_items or len(name_items[0]) < 2 or not number_items or len(number_items[0]) < 2:
        return None
    position = None
    orientation = None
    if at_items and len(at_items[0]) >= 3:
        try:
            position = [float(at_items[0][1]), float(at_items[0][2])]
            orientation = float(at_items[0][3]) if len(at_items[0]) >= 4 else 0.0
        except ValueError:
            position = None
            orientation = None
    return {
        "name": str(name_items[0][1]),
        "number": str(number_items[0][1]),
        "electrical_type": str(expression[1]),
        "unit": unit,
        "position": position,
        "orientation": orientation,
    }


def _own_pins(expression: list[Any]) -> list[dict[str, Any]]:
    pins: list[dict[str, Any]] = []
    for item in _children(expression, "pin"):
        record = _pin_record(item, 1)
        if record is not None:
            pins.append(record)
    symbol_name = str(expression[1]) if len(expression) >= 2 else ""
    for nested in _children(expression, "symbol"):
        nested_name = str(nested[1]) if len(nested) >= 2 else ""
        match = _UNIT_SUFFIX_RE.search(nested_name)
        unit = int(match.group(1)) if match and nested_name.startswith(symbol_name + "_") else 1
        for item in _children(nested, "pin"):
            record = _pin_record(item, unit)
            if record is not None:
                pins.append(record)
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for pin in pins:
        key = (
            pin["unit"],
            pin["number"],
            pin["name"],
            pin["electrical_type"],
            tuple(pin["position"] or []),
            pin["orientation"],
        )
        unique[key] = pin
    return list(unique.values())


def _apply_blank_resistor_pin_name_rule(
    reference: str | None, pins: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Give wholly unnamed resistor pins their exact pin numbers as names.

    The exception is deliberately narrow: it applies only to ``R`` symbols
    whose pin names are all blank and whose pin numbers are all non-empty and
    unique.  The source name and the reason for synthesis stay in each pin
    record so callers never mistake the invented semantic name for source
    metadata.
    """

    if reference != "R" or not pins or any(pin["name"] != "" for pin in pins):
        return pins
    numbers = [pin["number"] for pin in pins]
    if any(number == "" for number in numbers) or len(set(numbers)) != len(numbers):
        return pins
    synthesized: list[dict[str, Any]] = []
    for pin in pins:
        record = dict(pin)
        record["raw_name"] = pin["name"]
        record["name"] = pin["number"]
        record["name_source"] = "synthesized_blank_resistor_pin_number"
        synthesized.append(record)
    return synthesized


def parse_symbol_text(text: str, symbol_name: str | None = None) -> dict[str, Any]:
    """Parse one exact symbol from a KiCad symbol library.

    Same-library ``extends`` inheritance is resolved.  A missing base or a true
    electrical multi-unit definition is returned as explicitly unsupported.
    """

    try:
        root = _parse_sexpression(text)
    except SymbolParseError as exc:
        return {
            "status": "unresolved",
            "name": symbol_name,
            "reference_prefix": None,
            "pins": [],
            "diagnostics": [{"code": "symbol_parse_failed", "message": str(exc)}],
        }
    if not root or root[0] != "kicad_symbol_lib":
        return {
            "status": "unresolved",
            "name": symbol_name,
            "reference_prefix": None,
            "pins": [],
            "diagnostics": [
                {"code": "not_kicad_symbol_library", "message": "Unexpected symbol root"}
            ],
        }
    definitions = {
        str(item[1]): item
        for item in _children(root, "symbol")
        if len(item) >= 2
    }
    if symbol_name is None:
        if len(definitions) != 1:
            return {
                "status": "unresolved",
                "name": None,
                "reference_prefix": None,
                "pins": [],
                "diagnostics": [
                    {
                        "code": "symbol_name_required",
                        "message": "A symbol name is required for a multi-symbol library",
                    }
                ],
            }
        symbol_name = next(iter(definitions))
    if symbol_name not in definitions:
        return {
            "status": "unresolved",
            "name": symbol_name,
            "reference_prefix": None,
            "pins": [],
            "diagnostics": [
                {"code": "symbol_not_found", "message": f"Symbol '{symbol_name}' was not found"}
            ],
        }

    def resolve(name: str, trail: tuple[str, ...] = ()) -> tuple[str | None, list[dict[str, Any]], list[str]]:
        if name in trail:
            raise SymbolParseError("symbol inheritance cycle: " + " -> ".join((*trail, name)))
        expression = definitions[name]
        base_items = _children(expression, "extends")
        reference = _property(expression, "Reference")
        pins: list[dict[str, Any]] = []
        inheritance: list[str] = []
        if base_items and len(base_items[0]) >= 2:
            base_name = str(base_items[0][1])
            if base_name not in definitions:
                raise LookupError(base_name)
            base_reference, pins, inherited = resolve(base_name, (*trail, name))
            reference = reference or base_reference
            inheritance = [base_name, *inherited]
        own = _own_pins(expression)
        if own:
            inherited_by_slot = {(pin["unit"], pin["number"]): pin for pin in pins}
            for pin in own:
                inherited_by_slot[(pin["unit"], pin["number"])] = pin
            pins = list(inherited_by_slot.values())
        return reference, pins, inheritance

    try:
        reference, pins, inheritance = resolve(symbol_name)
    except LookupError as exc:
        base_name = str(exc.args[0])
        return {
            "status": "unsupported_inheritance",
            "name": symbol_name,
            "reference_prefix": None,
            "pins": [],
            "diagnostics": [
                {
                    "code": "unsupported_external_inheritance",
                    "message": f"Inherited base symbol '{base_name}' is not in the exact asset",
                    "base_symbol": base_name,
                }
            ],
        }
    except SymbolParseError as exc:
        return {
            "status": "unresolved",
            "name": symbol_name,
            "reference_prefix": None,
            "pins": [],
            "diagnostics": [{"code": "symbol_inheritance_cycle", "message": str(exc)}],
        }
    pins = _apply_blank_resistor_pin_name_rule(reference, pins)
    units = sorted({pin["unit"] for pin in pins if pin["unit"] > 0})
    if len(units) > 1:
        return {
            "status": "unsupported_multi_unit",
            "name": symbol_name,
            "reference_prefix": reference,
            "pins": pins,
            "electrical_units": units,
            "inheritance": inheritance,
            "diagnostics": [
                {
                    "code": "unsupported_multi_unit_symbol",
                    "message": f"Symbol '{symbol_name}' has electrical units {units}",
                }
            ],
        }
    if not reference or not re.fullmatch(r"[A-Za-z]+", reference):
        return {
            "status": "unresolved",
            "name": symbol_name,
            "reference_prefix": reference,
            "pins": pins,
            "electrical_units": units,
            "inheritance": inheritance,
            "diagnostics": [
                {
                    "code": "invalid_symbol_reference_prefix",
                    "message": f"Symbol '{symbol_name}' has no valid Reference prefix",
                }
            ],
        }
    return {
        "status": "ready",
        "name": symbol_name,
        "reference_prefix": reference,
        "pins": sorted(pins, key=lambda pin: (pin["unit"], pin["number"], pin["name"])),
        "electrical_units": units,
        "inheritance": inheritance,
        "diagnostics": [],
    }


def parse_symbol_file(path: str | Path, symbol_name: str | None = None) -> dict[str, Any]:
    return parse_symbol_text(Path(path).read_text(encoding="utf-8"), symbol_name)


class _Findings:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, code: str, message: str, *, severity: str = "error", **details: Any) -> str:
        finding_id = f"F{len(self.items) + 1:04d}"
        self.items.append(
            {
                "id": finding_id,
                "code": code,
                "severity": severity,
                "message": message,
                "details": details,
            }
        )
        return finding_id


def allocate_references(
    parts: Iterable[dict[str, Any]], symbols: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Reserve all preferred references, then allocate lowest free positive IDs."""

    findings = _Findings()
    records: list[dict[str, Any]] = []
    for part in parts:
        source_id = part.get("oomp_part_id")
        symbol = symbols.get(str(source_id))
        mechanical = bool(part.get("mechanical_only"))
        prefix = "H" if mechanical else (
            symbol.get("reference_prefix") if symbol and symbol.get("status") == "ready" else None
        )
        finding_ids: list[str] = []
        if prefix is None:
            finding_ids.append(
                findings.add(
                    "unresolved_symbol",
                    "No supported exact symbol is available for reference allocation",
                    creation_key=part.get("_creation_key"),
                    source_id=source_id,
                    symbol_status=symbol.get("status") if symbol else "missing",
                )
            )
        records.append(
            {
                "creation_key": part.get("_creation_key"),
                "instance_id": part.get("instance_id"),
                "source_id": source_id,
                "mechanical_only": mechanical,
                "reference_prefix": prefix,
                "requested_preferred_id": part.get("preferred_id"),
                "assigned_reference": None,
                "resolution_status": "ready" if prefix and part.get("instance_id") else "unresolved",
                "finding_ids": finding_ids,
                "pins": list(symbol.get("pins", [])) if symbol and symbol.get("status") == "ready" else [],
            }
        )
    reserved: dict[str, dict[int, str]] = {}
    for record in records:
        preferred = record["requested_preferred_id"]
        prefix = record["reference_prefix"]
        if preferred is None or prefix is None:
            continue
        match = _REFERENCE_RE.fullmatch(str(preferred))
        if not match:
            record["finding_ids"].append(
                findings.add(
                    "invalid_preferred_reference",
                    f"Preferred reference '{preferred}' is not letters plus a positive integer",
                    severity="warning",
                    creation_key=record["creation_key"],
                )
            )
            continue
        preferred_prefix, number_text = match.groups()
        if preferred_prefix != prefix:
            record["finding_ids"].append(
                findings.add(
                    "preferred_reference_prefix_mismatch",
                    f"Preferred reference '{preferred}' does not match symbol prefix '{prefix}'",
                    severity="warning",
                    creation_key=record["creation_key"],
                )
            )
            continue
        number = int(number_text)
        prefix_reservations = reserved.setdefault(prefix, {})
        if number in prefix_reservations:
            record["finding_ids"].append(
                findings.add(
                    "duplicate_preferred_reference",
                    f"Preferred reference '{preferred}' was already reserved",
                    severity="warning",
                    creation_key=record["creation_key"],
                    first_creation_key=prefix_reservations[number],
                )
            )
            continue
        prefix_reservations[number] = str(record["creation_key"])
        record["assigned_reference"] = preferred
    for record in records:
        prefix = record["reference_prefix"]
        if prefix is None or record["assigned_reference"] is not None:
            continue
        used = reserved.setdefault(prefix, {})
        number = 1
        while number in used:
            number += 1
        record["assigned_reference"] = f"{prefix}{number}"
        used[number] = str(record["creation_key"])
    return {"parts": records, "findings": findings.items}


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}

    def add(self, value: tuple[str, str]) -> None:
        self.parent.setdefault(value, value)

    def find(self, value: tuple[str, str]) -> tuple[str, str]:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: tuple[str, str], right: tuple[str, str]) -> None:
        self.add(left)
        self.add(right)
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            if root_right < root_left:
                root_left, root_right = root_right, root_left
            self.parent[root_right] = root_left


def canonical_net_name(name: str) -> str:
    return _NET_ALIASES.get(name, name)


def _endpoint_key(endpoint: dict[str, Any]) -> tuple[str, str]:
    return (str(endpoint["instance_id"]), str(endpoint["pin_number"]))


def _endpoint_public(endpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": endpoint["instance_id"],
        "reference": endpoint["reference"],
        "pin_name": endpoint["pin_name"],
        "pin_number": endpoint["pin_number"],
        "electrical_type": endpoint["electrical_type"],
    }


def _resolve_endpoint(
    part: dict[str, Any] | None,
    pin_name: Any,
    pin_number: Any,
    findings: _Findings,
    *,
    source_action_id: str,
    side: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    ids: list[str] = []
    if part is None or part.get("resolution_status") != "ready":
        ids.append(
            findings.add(
                "endpoint_part_unresolved",
                "Endpoint part is not resolved",
                source_action_id=source_action_id,
                endpoint=side,
                pin_name=pin_name,
            )
        )
        return None, ids
    if part.get("mechanical_only"):
        ids.append(
            findings.add(
                "mechanical_part_has_no_electrical_pins",
                "Mechanical-only parts cannot be electrical endpoints",
                source_action_id=source_action_id,
                endpoint=side,
            )
        )
        return None, ids
    if pin_number is not None and not isinstance(pin_number, str):
        ids.append(
            findings.add(
                "invalid_pin_number",
                "Pin-number overrides must be strings",
                source_action_id=source_action_id,
                endpoint=side,
                pin_number=pin_number,
            )
        )
        return None, ids
    matches = [pin for pin in part["pins"] if pin["name"] == pin_name]
    if pin_number is not None:
        matches = [pin for pin in matches if pin["number"] == pin_number]
    if not matches:
        ids.append(
            findings.add(
                "pin_not_found",
                "No exact symbol pin matches the requested name and number",
                source_action_id=source_action_id,
                endpoint=side,
                pin_name=pin_name,
                pin_number=pin_number,
                reference=part.get("assigned_reference"),
            )
        )
        return None, ids
    if len(matches) != 1:
        ids.append(
            findings.add(
                "ambiguous_pin_name",
                "The semantic pin name matches more than one exact symbol pin",
                source_action_id=source_action_id,
                endpoint=side,
                pin_name=pin_name,
                matching_numbers=sorted(pin["number"] for pin in matches),
                reference=part.get("assigned_reference"),
            )
        )
        return None, ids
    pin = matches[0]
    return {
        "instance_id": part["instance_id"],
        "reference": part["assigned_reference"],
        "pin_name": pin["name"],
        "pin_number": pin["number"],
        "electrical_type": pin["electrical_type"],
    }, ids


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def generated_net_name(
    endpoints: Iterable[tuple[str, str]],
    used_names: set[str] | None = None,
    digest_function: Callable[[bytes], str] = _digest,
) -> str:
    """Create the frozen v1 bounded automatic net name.

    The normal suffix is 12 hex characters.  Collisions extend it by four up to
    the full digest while shrinking the readable prefix.  A full-digest collision
    uses a secondary SHA-256 suffix derived from the endpoint payload.
    """

    ordered = sorted((str(instance), str(number)) for instance, number in endpoints)
    payload = "\n".join(f"{instance}\0{number}" for instance, number in ordered).encode("utf-8")
    digest = digest_function(payload).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("digest_function must return 64 lowercase/uppercase hex characters")
    readable = "_".join(
        _SAFE_NET_RE.sub("_", f"{instance}_{number}").strip("_")
        for instance, number in ordered
    ) or "unnamed"
    used = used_names or set()
    for suffix_length in range(AUTO_NET_HASH_LENGTH, 65, 4):
        suffix = digest[:suffix_length]
        prefix_length = AUTO_NET_MAX_LENGTH - len("net__") - len(suffix)
        candidate = f"net_{readable[:max(1, prefix_length)]}_{suffix}"
        if candidate not in used:
            return candidate
    secondary = hashlib.sha256(payload + b"\0auto-net-collision").hexdigest()
    for length in range(8, 65, 4):
        suffix = f"{digest[:16]}_{secondary[:length]}"
        prefix_length = AUTO_NET_MAX_LENGTH - len("net__") - len(suffix)
        candidate = f"net_{readable[:max(1, prefix_length)]}_{suffix}"
        if candidate not in used:
            return candidate
    raise RuntimeError("unable to allocate deterministic automatic net name")


def compile_connectivity(
    board: dict[str, Any],
    symbols: dict[str, dict[str, Any]],
    *,
    digest_function: Callable[[bytes], str] = _digest,
) -> dict[str, Any]:
    """Compile resolved parts, nets, NCs, histories, and diagnostics."""

    allocation = allocate_references(board.get("parts", []), symbols)
    findings = _Findings()
    findings.items.extend(allocation["findings"])
    by_creation = {part["creation_key"]: part for part in allocation["parts"]}
    original_to_resolved = {
        id(original): by_creation.get(original.get("_creation_key"))
        for original in board.get("parts", [])
    }
    union = _UnionFind()
    endpoint_records: dict[tuple[str, str], dict[str, Any]] = {}
    histories: list[dict[str, Any]] = []
    named_anchors: dict[str, tuple[str, str]] = {}
    edge_names: list[tuple[tuple[str, str], str, str]] = []
    for connection in board.get("connections", []):
        action_id = str(connection.get("_creation_key"))
        left, left_ids = _resolve_endpoint(
            original_to_resolved.get(id(connection.get("part_a"))),
            connection.get("pin_a"),
            connection.get("pin_number_a"),
            findings,
            source_action_id=action_id,
            side="a",
        )
        right, right_ids = _resolve_endpoint(
            original_to_resolved.get(id(connection.get("part_b"))),
            connection.get("pin_b"),
            connection.get("pin_number_b"),
            findings,
            source_action_id=action_id,
            side="b",
        )
        history = {
            "source_action_id": action_id,
            "requested": {
                "part_a": connection.get("part_a", {}).get("instance_id") if isinstance(connection.get("part_a"), dict) else None,
                "pin_a": connection.get("pin_a"),
                "pin_number_a": connection.get("pin_number_a"),
                "part_b": connection.get("part_b", {}).get("instance_id") if isinstance(connection.get("part_b"), dict) else None,
                "pin_b": connection.get("pin_b"),
                "pin_number_b": connection.get("pin_number_b"),
                "net_name": connection.get("net_name"),
            },
            "resolution_status": "unresolved",
            "finding_ids": [*left_ids, *right_ids],
            "resolved_endpoints": [],
            "resolved_net_name": None,
        }
        if left is not None and right is not None:
            left_key = _endpoint_key(left)
            right_key = _endpoint_key(right)
            endpoint_records[left_key] = left
            endpoint_records[right_key] = right
            union.union(left_key, right_key)
            history["resolution_status"] = "ready"
            history["resolved_endpoints"] = [_endpoint_public(left), _endpoint_public(right)]
            requested_name = connection.get("net_name")
            if requested_name is not None:
                if not isinstance(requested_name, str) or not requested_name:
                    finding_id = findings.add(
                        "invalid_net_name",
                        "Explicit net names must be non-empty strings",
                        source_action_id=action_id,
                        net_name=requested_name,
                    )
                    history["resolution_status"] = "unresolved"
                    history["finding_ids"].append(finding_id)
                else:
                    canonical = canonical_net_name(requested_name)
                    edge_names.append((left_key, canonical, action_id))
                    anchor = named_anchors.get(canonical)
                    if anchor is None:
                        named_anchors[canonical] = left_key
                    else:
                        union.union(anchor, left_key)
        histories.append(history)

    components: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for endpoint in union.parent:
        components.setdefault(union.find(endpoint), []).append(endpoint)
    names_by_root: dict[tuple[str, str], dict[str, list[str]]] = {}
    for endpoint, name, action_id in edge_names:
        root = union.find(endpoint)
        names_by_root.setdefault(root, {}).setdefault(name, []).append(action_id)
    explicit_names = {name for _, name, _ in edge_names}
    used_names = set(explicit_names)
    nets: list[dict[str, Any]] = []
    root_to_name: dict[tuple[str, str], str] = {}
    conflict_findings: dict[tuple[str, str], str] = {}
    for root, endpoint_keys in sorted(components.items(), key=lambda item: sorted(item[1])):
        named = names_by_root.get(root, {})
        if len(named) > 1:
            finding_id = findings.add(
                "conflicting_net_names",
                "One connected component has multiple explicit canonical net names",
                net_names=sorted(named),
                endpoints=[list(item) for item in sorted(endpoint_keys)],
                source_action_ids=sorted({action for actions in named.values() for action in actions}),
            )
            conflict_findings[root] = finding_id
            continue
        if named:
            name = next(iter(named))
            explicit = True
        else:
            name = generated_net_name(endpoint_keys, used_names, digest_function)
            explicit = False
        used_names.add(name)
        root_to_name[root] = name
        source_ids = sorted(
            history["source_action_id"]
            for history in histories
            if history["resolution_status"] == "ready"
            and history["resolved_endpoints"]
            and union.find(_endpoint_key(history["resolved_endpoints"][0])) == root
        )
        nets.append(
            {
                "name": name,
                "explicit_name": explicit,
                "endpoints": [
                    _endpoint_public(endpoint_records[key]) for key in sorted(endpoint_keys)
                ],
                "source_action_ids": source_ids,
                "resolution_status": "ready",
                "finding_ids": [],
            }
        )
    for history in histories:
        if history["resolution_status"] != "ready" or not history["resolved_endpoints"]:
            continue
        root = union.find(_endpoint_key(history["resolved_endpoints"][0]))
        if root in conflict_findings:
            history["resolution_status"] = "blocked"
            history["finding_ids"].append(conflict_findings[root])
        else:
            history["resolved_net_name"] = root_to_name[root]

    connected = {
        _endpoint_key(endpoint)
        for net in nets
        for endpoint in net["endpoints"]
    }
    no_connects: list[dict[str, Any]] = []
    no_connect_history: list[dict[str, Any]] = []
    emitted_nc: set[tuple[str, str]] = set()
    for item in board.get("no_connects", []):
        action_id = str(item.get("_creation_key"))
        endpoint, ids = _resolve_endpoint(
            original_to_resolved.get(id(item.get("part"))),
            item.get("pin"),
            item.get("pin_number"),
            findings,
            source_action_id=action_id,
            side="nc",
        )
        history = {
            "source_action_id": action_id,
            "resolution_status": "unresolved",
            "finding_ids": ids,
            "resolved_endpoint": _endpoint_public(endpoint) if endpoint else None,
        }
        if endpoint is not None:
            key = _endpoint_key(endpoint)
            if key in connected:
                finding_id = findings.add(
                    "connected_and_no_connect",
                    "A resolved endpoint is both connected and explicitly no-connect",
                    source_action_id=action_id,
                    endpoint=list(key),
                )
                history["resolution_status"] = "blocked"
                history["finding_ids"].append(finding_id)
            else:
                history["resolution_status"] = "ready"
                if key not in emitted_nc:
                    no_connects.append(
                        {
                            "endpoint": _endpoint_public(endpoint),
                            "source_action_ids": [action_id],
                            "resolution_status": "ready",
                            "finding_ids": [],
                        }
                    )
                    emitted_nc.add(key)
                else:
                    next(nc for nc in no_connects if _endpoint_key(nc["endpoint"]) == key)[
                        "source_action_ids"
                    ].append(action_id)
        no_connect_history.append(history)
    nets.sort(key=lambda net: net["name"])
    no_connects.sort(key=lambda nc: _endpoint_key(nc["endpoint"]))
    has_errors = any(item["severity"] == "error" for item in findings.items)
    return {
        "status": "partial" if has_errors else "ready",
        "parts": allocation["parts"],
        "reference_allocation": [
            {
                key: part[key]
                for key in (
                    "creation_key",
                    "instance_id",
                    "source_id",
                    "reference_prefix",
                    "requested_preferred_id",
                    "assigned_reference",
                    "resolution_status",
                    "finding_ids",
                )
            }
            for part in allocation["parts"]
        ],
        "nets": nets,
        "no_connects": no_connects,
        "connection_history": histories,
        "no_connect_history": no_connect_history,
        "findings": findings.items,
        "automatic_net_naming": {
            "maximum_length": AUTO_NET_MAX_LENGTH,
            "initial_hash_length": AUTO_NET_HASH_LENGTH,
            "collision_extension": "extend SHA-256 suffix by four hex characters, then secondary SHA-256",
        },
    }


def symbols_from_inspections(inspections: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Parse the exact valid symbol asset selected by each source inspection."""

    result: dict[str, dict[str, Any]] = {}
    for source_id, inspection in inspections.items():
        asset = inspection.get("valid_assets", {}).get("symbol")
        if asset is None:
            result[source_id] = {
                "status": "unresolved",
                "name": source_id,
                "reference_prefix": None,
                "pins": [],
                "diagnostics": [
                    {"code": "valid_symbol_asset_missing", "message": "No valid symbol asset"}
                ],
            }
        else:
            result[source_id] = parse_symbol_file(asset["_path"], source_id)
    return result
