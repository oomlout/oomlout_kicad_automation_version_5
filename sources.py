"""Managed OOMP source refresh, asset validation, and local library packaging."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml


SOURCE_OWNER_FILE = ".oomlout_kicad_source.json"
BUNDLE_OWNER_FILE = ".oomlout_kicad_bundle.json"
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
EXCLUDED_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDED_NAMES = {"Thumbs.db", ".DS_Store", SOURCE_OWNER_FILE}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".lock", ".tmp"}


class SourceError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def diagnostic(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _included(relative: Path) -> bool:
    return (
        not any(part in EXCLUDED_PARTS for part in relative.parts)
        and relative.name not in EXCLUDED_NAMES
        and relative.suffix.lower() not in EXCLUDED_SUFFIXES
    )


def hash_folder(root: str | Path) -> tuple[str, list[str]]:
    """Hash sorted relative paths and bytes with fixed length delimiters."""

    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise SourceError("source_missing", f"Source folder does not exist: {root_path}")
    files: list[Path] = []
    for current, directories, filenames in os.walk(root_path, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            candidate = current_path / name
            relative = candidate.relative_to(root_path)
            if not _included(relative):
                directories.remove(name)
                continue
            if not _inside(candidate, root_path):
                raise SourceError(
                    "source_path_escape",
                    f"Source directory resolves outside its part root: {relative.as_posix()}",
                    path=relative.as_posix(),
                )
        for name in filenames:
            path = current_path / name
            relative = path.relative_to(root_path)
            if not _included(relative):
                continue
            if not _inside(path, root_path):
                raise SourceError(
                    "source_path_escape",
                    f"Source file resolves outside its part root: {relative.as_posix()}",
                    path=relative.as_posix(),
                )
            files.append(path)
    digest = hashlib.sha256()
    relatives: list[str] = []
    for path in sorted(files, key=lambda item: item.relative_to(root_path).as_posix()):
        relative = path.relative_to(root_path).as_posix()
        data = path.read_bytes()
        relative_bytes = relative.encode("utf-8")
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        relatives.append(relative)
    return digest.hexdigest(), relatives


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _asset_identity_valid(kind: str, source_id: str, text: str) -> bool:
    if kind == "symbol":
        return f'(symbol "{source_id}"' in text
    if kind in {"machine_solder", "hand_solder"}:
        escaped = re.escape(source_id)
        return bool(
            re.search(rf"\(footprint\s+\"{escaped}\"", text)
            or re.search(rf"\(footprint\s+{escaped}(?:\s|\))", text)
            or re.search(rf"\(module\s+{escaped}(?:\s|\))", text)
        )
    return False


def inspect_source(
    source_root: str | Path,
    source_id: str,
    *,
    asset_resolver=None,
) -> dict[str, Any]:
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise SourceError(
            "invalid_source_id", f"Unsafe OOMP source ID: {source_id}", source_id=source_id
        )
    part_root = Path(source_root).resolve() / "parts" / source_id
    if not _inside(part_root, Path(source_root).resolve() / "parts"):
        raise SourceError("source_target_escape", "Source ID escapes the parts root")
    working_path = part_root / "working.yaml"
    manifest_path = part_root / "data" / "kicad" / "manifest.yaml"
    if not working_path.is_file() or not manifest_path.is_file():
        raise SourceError(
            "source_metadata_missing",
            f"Source {source_id} lacks working.yaml or data/kicad/manifest.yaml",
            source_id=source_id,
        )
    working = yaml.safe_load(working_path.read_text(encoding="utf-8"))
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(working, dict) or not isinstance(manifest, dict):
        raise SourceError("source_metadata_invalid", f"Source metadata is not a mapping: {source_id}")
    if manifest.get("oomp_id") != source_id:
        raise SourceError(
            "manifest_identity_mismatch",
            f"Manifest identity does not match {source_id}",
            actual=manifest.get("oomp_id"),
        )
    kicad_root = manifest_path.parent.resolve()
    assets: list[dict[str, Any]] = []
    structural_errors: list[dict[str, Any]] = []
    for entry in manifest.get("assets", []):
        item = dict(entry) if isinstance(entry, dict) else {}
        kind = item.get("kind")
        relative_text = item.get("file")
        valid = True
        findings: list[str] = []
        if kind not in {"symbol", "machine_solder", "hand_solder"}:
            valid = False
            findings.append("unsupported_asset_kind")
        if not isinstance(relative_text, str) or not relative_text:
            valid = False
            findings.append("invalid_asset_path")
            asset_path = kicad_root
        else:
            relative = Path(relative_text)
            if relative.is_absolute():
                valid = False
                findings.append("absolute_asset_path")
            unresolved_asset_path = kicad_root / relative
            asset_path = (
                asset_resolver(unresolved_asset_path)
                if asset_resolver is not None
                else unresolved_asset_path.resolve()
            )
            asset_path = Path(asset_path)
            if not _inside(asset_path, kicad_root):
                valid = False
                findings.append("asset_path_escape")
        if valid and not asset_path.is_file():
            valid = False
            findings.append("asset_missing")
        actual_hash = _sha256(asset_path) if valid else None
        if valid and actual_hash != item.get("sha256"):
            valid = False
            findings.append("asset_hash_mismatch")
        if valid:
            try:
                text = asset_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                valid = False
                findings.append("asset_not_utf8")
            else:
                if not _asset_identity_valid(kind, source_id, text):
                    valid = False
                    findings.append("asset_internal_identity_mismatch")
        asset = {
            "kind": kind,
            "file": relative_text,
            "sha256": actual_hash,
            "declared_sha256": item.get("sha256"),
            "valid": valid,
            "findings": findings,
            "manifest_entry": item,
            "_path": str(asset_path),
        }
        assets.append(asset)
        if findings:
            structural_errors.append(
                {
                    "code": findings[0],
                    "message": f"Invalid {kind or 'unknown'} asset for {source_id}",
                    "details": {"source_id": source_id, "file": relative_text, "findings": findings},
                }
            )
    valid_by_kind = {
        kind: next((asset for asset in assets if asset["kind"] == kind and asset["valid"]), None)
        for kind in ("symbol", "machine_solder", "hand_solder")
    }
    selected = valid_by_kind["machine_solder"] or valid_by_kind["hand_solder"]
    warnings: list[dict[str, Any]] = []
    if valid_by_kind["machine_solder"] is None and valid_by_kind["hand_solder"] is not None:
        warnings.append(
            {
                "code": "hand_solder_fallback",
                "message": f"{source_id} uses hand-solder footprint because no valid machine asset exists",
                "details": {"source_id": source_id},
            }
        )
    folder_hash, folder_files = hash_folder(part_root)
    ready = valid_by_kind["symbol"] is not None and selected is not None
    return {
        "source_id": source_id,
        "source_hash": folder_hash,
        "source_files": folder_files,
        "working": working,
        "manifest": manifest,
        "assets": assets,
        "valid_assets": {key: value for key, value in valid_by_kind.items() if value is not None},
        "selected_footprint_kind": selected["kind"] if selected else None,
        "status": "ready" if ready else "unresolved",
        "diagnostics": structural_errors,
        "warnings": warnings,
        "issues": list(manifest.get("issues", [])),
        "license": manifest.get("license"),
        "cache": {
            key: working.get(key)
            for key in ("value", "name", "name_proper", "manufacturer", "part_number", "package", "lcsc_part_number")
        },
        "_part_root": str(part_root),
    }


def _repository_state(path: Path, *, cache: dict | None = None) -> dict[str, Any]:
    try:
        top = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
        ).strip()
        if cache is not None and top in cache:
            return cache[top]
        head = subprocess.check_output(
            ["git", "-C", top, "rev-parse", "HEAD"], text=True, encoding="utf-8"
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", top, "status", "--porcelain=v1", "--untracked-files=all"],
            text=True,
            encoding="utf-8",
        ).splitlines()
        result = {"repository": top, "commit": head, "dirty": bool(status), "status": status}
        if cache is not None:
            cache[top] = result
        return result
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"repository": None, "commit": None, "dirty": None, "status": []}


def sync_source(
    inspection: dict[str, Any], parts_root: str | Path, *, copy_function=shutil.copy2
) -> dict[str, Any]:
    source_id = inspection["source_id"]
    destination_root = Path(parts_root).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / source_id
    if not _inside(destination, destination_root):
        raise SourceError("managed_target_escape", "Managed source target escapes parts root")
    previous_hash = None
    if destination.exists():
        marker = destination / SOURCE_OWNER_FILE
        if not marker.is_file():
            raise SourceError(
                "unowned_source_target",
                f"Refusing to replace unowned source target: {destination}",
                destination=str(destination),
            )
        ownership = json.loads(marker.read_text(encoding="utf-8"))
        if ownership.get("source_id") != source_id:
            raise SourceError("source_owner_mismatch", f"Ownership mismatch at {destination}")
        previous_hash = ownership.get("source_hash")
        if previous_hash == inspection["source_hash"]:
            return {
                "source_id": source_id,
                "outcome": "no_op",
                "previous_hash": previous_hash,
                "source_hash": inspection["source_hash"],
                "destination": str(destination),
            }

    temp = Path(tempfile.mkdtemp(prefix=f".{source_id}.tmp-", dir=destination_root))
    backup = destination_root / f".{source_id}.previous"
    promoted = False
    try:
        shutil.rmtree(temp)
        shutil.copytree(inspection["_part_root"], temp, copy_function=copy_function)
        marker = {
            "owner": "oomlout_kicad_automation_version_5",
            "source_id": source_id,
            "source_hash": inspection["source_hash"],
        }
        (temp / SOURCE_OWNER_FILE).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        copied_hash, _ = hash_folder(temp)
        if copied_hash != inspection["source_hash"]:
            raise SourceError(
                "source_copy_verification_failed",
                f"Copied source hash differs for {source_id}",
                expected=inspection["source_hash"],
                actual=copied_hash,
            )
        if backup.exists():
            raise SourceError("stale_refresh_backup", f"Unexpected refresh backup exists: {backup}")
        if destination.exists():
            destination.replace(backup)
        temp.replace(destination)
        promoted = True
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if not destination.exists() and backup.exists():
            backup.replace(destination)
        raise
    finally:
        if temp.exists():
            shutil.rmtree(temp)
        if promoted and backup.exists():
            shutil.rmtree(backup)
    return {
        "source_id": source_id,
        "outcome": "copied" if previous_hash is None else "refreshed",
        "previous_hash": previous_hash,
        "source_hash": inspection["source_hash"],
        "destination": str(destination),
    }


def canonical_source_record(inspection: dict[str, Any]) -> dict[str, Any]:
    assets = [
        {
            "kind": asset["kind"],
            "file": asset["file"],
            "sha256": asset["sha256"],
            "valid": asset["valid"],
            "findings": asset["findings"],
        }
        for asset in inspection["assets"]
    ]
    return {
        "oomp_id": inspection["source_id"],
        "source_hash": inspection["source_hash"],
        "status": inspection["status"],
        "selected_footprint_kind": inspection["selected_footprint_kind"],
        "assets": assets,
        "issues": inspection["issues"],
        "license": inspection["license"],
    }


def refresh_sources(
    source_ids: Iterable[str], source_root: str | Path, parts_root: str | Path
) -> dict[str, Any]:
    inspections: dict[str, dict[str, Any]] = {}
    canonical: list[dict[str, Any]] = []
    runtime: list[dict[str, Any]] = []
    # One provenance snapshot per repository per refresh. Repeating the entire
    # dirty-file inventory for every part grows with archived run evidence and
    # can exhaust memory during YAML serialization. Store repository snapshots
    # once in the runtime repositories table, without YAML aliases.
    repository_cache: dict[str, dict] = {}
    for source_id in dict.fromkeys(source_ids):
        try:
            inspection = inspect_source(source_root, source_id)
            inspections[source_id] = inspection
            canonical.append(canonical_source_record(inspection))
            if inspection["diagnostics"]:
                runtime.append(
                    {
                        "source_id": source_id,
                        "outcome": "invalid_assets_not_copied",
                        "diagnostics": inspection["diagnostics"],
                    }
                )
                continue
            refresh = sync_source(inspection, parts_root)
            state=_repository_state(Path(inspection["_part_root"]), cache=repository_cache)
            refresh.update({k:v for k,v in state.items() if k!='status'})
            if state.get('repository') is not None:
                repository_cache[state['repository']]=state
            runtime.append(refresh)
        except SourceError as exc:
            canonical.append(
                {
                    "oomp_id": source_id,
                    "source_hash": None,
                    "status": "unresolved",
                    "selected_footprint_kind": None,
                    "assets": [],
                    "issues": [],
                    "license": None,
                    "diagnostics": [exc.diagnostic()],
                }
            )
            runtime.append(
                {"source_id": source_id, "outcome": "failed", "diagnostics": [exc.diagnostic()]}
            )
    return {
        "canonical": {"format_version": 1, "sources": canonical},
        "runtime": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_root": str(Path(source_root).resolve()),
            "sources": runtime,
            "repositories": list(repository_cache.values()),
        },
        "inspections": inspections,
    }


def _extract_symbols(text: str) -> list[str]:
    starts: list[int] = []
    expressions: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
        elif character == "(":
            if depth == 1 and text.startswith("(symbol ", index):
                starts.append(index)
            depth += 1
        elif character == ")":
            depth -= 1
            if starts and depth == 1:
                expressions.append(text[starts.pop(): index + 1])
        index += 1
    return expressions


def packaged_footprint_bytes(*, source: bytes) -> bytes:
    """Normalize only the root header whitespace for Konnect's literal parser.

    KiCad permits a newline before the quoted name; Konnect 0.12.1's
    place_component requires '(footprint "'. Preserve all geometry verbatim.
    Source copies/manifests remain byte-for-byte original.
    """
    return re.sub(rb'\A(\s*\(footprint)\s+(?=")', rb'\1 ', source, count=1)


def build_local_libraries(
    inspections: dict[str, dict[str, Any]], output_root: str | Path, board_project_id: str
) -> dict[str, Any]:
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    owner_path = root / BUNDLE_OWNER_FILE
    managed_names = {"libraries", "sym-lib-table", "fp-lib-table", "sources"}
    if any((root / name).exists() for name in managed_names) and not owner_path.is_file():
        raise SourceError(
            "unowned_bundle_target", f"Refusing to replace unowned compiler bundle at {root}"
        )
    if owner_path.is_file():
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        if owner.get("board_project_id") != board_project_id:
            raise SourceError("bundle_owner_mismatch", f"Compiler bundle belongs to another board")

    temp = Path(tempfile.mkdtemp(prefix=".libraries.tmp-", dir=root))
    try:
        machine = temp / "OOMP_MachineSolder.pretty"
        hand = temp / "OOMP_HandSolder.pretty"
        machine.mkdir()
        hand.mkdir()
        symbols: list[str] = []
        packaged: list[dict[str, Any]] = []
        for source_id in sorted(inspections):
            inspection = inspections[source_id]
            for kind, asset in sorted(inspection["valid_assets"].items()):
                path = Path(asset["_path"])
                if kind == "symbol":
                    expressions = _extract_symbols(path.read_text(encoding="utf-8"))
                    if len(expressions) != 1:
                        raise SourceError(
                            "symbol_bundle_parse_failed",
                            f"Expected one symbol expression for {source_id}",
                            count=len(expressions),
                        )
                    symbols.extend(expressions)
                record = {"oomp_id": source_id, "kind": kind, "sha256": asset["sha256"]}
                if kind in {"machine_solder", "hand_solder"}:
                    original = path.read_bytes()
                    payload = packaged_footprint_bytes(source=original)
                    destination = machine if kind == "machine_solder" else hand
                    (destination / f"{source_id}.kicad_mod").write_bytes(payload)
                    record.update(packaged_sha256=hashlib.sha256(payload).hexdigest(),
                                  header_whitespace_normalized=payload != original)
                packaged.append(record)
        symbol_text = "(kicad_symbol_lib (version 20231120) (generator oomlout_kicad_automation)\n"
        symbol_text += "\n".join(symbols) + "\n)\n"
        (temp / "OOMP.kicad_sym").write_text(symbol_text, encoding="utf-8", newline="\n")

        libraries = root / "libraries"
        if libraries.exists():
            shutil.rmtree(libraries)
        temp.replace(libraries)
        (root / "sym-lib-table").write_text(
            '(sym_lib_table\n  (lib (name "OOMP")(type "KiCad")(uri "${KIPRJMOD}/libraries/OOMP.kicad_sym")(options "")(descr ""))\n)\n',
            encoding="utf-8",
            newline="\n",
        )
        (root / "fp-lib-table").write_text(
            '(fp_lib_table\n  (lib (name "OOMP_MachineSolder")(type "KiCad")(uri "${KIPRJMOD}/libraries/OOMP_MachineSolder.pretty")(options "")(descr ""))\n  (lib (name "OOMP_HandSolder")(type "KiCad")(uri "${KIPRJMOD}/libraries/OOMP_HandSolder.pretty")(options "")(descr ""))\n)\n',
            encoding="utf-8",
            newline="\n",
        )
        owner_path.write_text(
            json.dumps(
                {"owner": "oomlout_kicad_automation_version_5", "board_project_id": board_project_id},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return {"status": "ready", "packaged": packaged}
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def write_source_reports(result: dict[str, Any], output_root: str | Path) -> None:
    root = Path(output_root)
    (root / "sources").mkdir(parents=True, exist_ok=True)
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "sources" / "manifest.yaml").write_text(
        yaml.safe_dump(result["canonical"], sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    (root / "reports" / "source_refresh.yaml").write_text(
        yaml.safe_dump(result["runtime"], sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
