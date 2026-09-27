"""Explicit project-local Konnect selection; never replace a shared install."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLED_KONNECT = Path(r"C:\od\OneDrive\Documents\KiCad\10.0\3rdparty\plugins\com_github_mixelpixx_konnect\bin\konnect.exe")


def backend_path(*, project_root: Path = PROJECT_ROOT) -> Path:
    manifest = project_root/"konnect/backend.json"
    if not manifest.exists(): return INSTALLED_KONNECT
    config = json.loads(manifest.read_text(encoding="utf-8"))
    path = Path(config["executable"])
    return (project_root/path).resolve() if not path.is_absolute() else path.resolve()


def backend_evidence(*, executable: Path, project_root: Path = PROJECT_ROOT) -> dict:
    executable = executable.resolve()
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest = project_root/"konnect/backend.json"
    evidence = {"executable":str(executable),"sha256":digest}
    if manifest.exists() and executable == backend_path(project_root=project_root):
        config = json.loads(manifest.read_text(encoding="utf-8"))
        if digest != config["sha256"]: raise ValueError("Configured Konnect binary hash changed; revalidate the build before use")
        for name, expected in config.get("runtime_files", {}).items():
            runtime = (executable.parent/name).resolve()
            if not runtime.is_relative_to(executable.parent): raise ValueError("Backend runtime path escape")
            if hashlib.sha256(runtime.read_bytes()).hexdigest() != expected:
                raise ValueError("Configured Konnect runtime hash changed; revalidate the build before use")
        evidence["project_configuration"] = config
    return evidence
