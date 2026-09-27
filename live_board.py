"""Execution-only PCB launch and exact-live-identity gate; never used by compile."""
from __future__ import annotations

import subprocess
import time
import os
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Callable

PCB_EDITOR = Path(r"C:\Program Files\KiCad\10.0\bin\pcbnew.exe")


def ensure_live_board(*, board: Path, read_identity: Callable[[], dict],
                      report: Callable[..., None], auto_open: bool = True,
                      editor: Path = PCB_EDITOR, attempts: int = 12,
                      interval_seconds: float = 5,
                      launch_environment: dict[str, str] | None = None) -> bool:
    """Launch the exact PCB once, then poll read-only IPC without a stdin prompt.

    Never close another editor, dismiss a dialog, or treat a file-only response
    as success. Retries apply to identity reads only, not board mutations.
    """
    board = board.resolve()
    if attempts < 1 or interval_seconds < 0:
        raise ValueError("attempts must be positive and interval_seconds nonnegative")
    report("live_pcb_identity", "RUNNING", board=str(board))
    identity = read_identity()
    if identity["exact_live_board"]:
        report("live_pcb_identity", "PASS", requested_open=True, source="ipc")
        return True
    if identity.get("observed_boards"):
        report("pcb_ipc_connection", "MISMATCH", requested_board=str(board),
               observed_boards=identity["observed_boards"])
    if not auto_open:
        report("live_pcb_identity", "BLOCKED", reason="exact PCB is not open over IPC; automatic opening disabled")
        return False
    lock = board.with_name("~" + board.name + ".lck")
    if lock.exists():
        report("live_pcb_identity", "BLOCKED", reason=(
            "Requested PCB already has an editor lock but is not reachable on the selected IPC connection. "
            "Save and close that PCB normally, then retry --resume-pcb so it can launch with its own connection. "
            "No duplicate editor was launched; no locks were removed."), lock=str(lock))
        return False
    try:
        if board.suffix.lower() != ".kicad_pcb" or not board.is_file():
            raise FileNotFoundError(f"PCB target does not exist: {board}")
        if not editor.is_file():
            raise FileNotFoundError(f"PCB Editor does not exist: {editor}")
        options = {"cwd": str(board.parent)}
        if launch_environment is not None:
            options["env"] = launch_environment
        process = subprocess.Popen([str(editor), str(board)], **options)
    except OSError as exc:
        report("live_pcb_identity", "BLOCKED", reason=f"Could not launch PCB Editor: {exc}")
        return False
    report("pcb_editor_launch", "STARTED", board=str(board), executable=str(editor), pid=process.pid)
    for attempt in range(1, attempts + 1):
        report("live_pcb_identity", "WAITING", attempt=attempt, attempts=attempts,
               reason="Waiting automatically for exact PCB IPC; no click or ENTER required")
        time.sleep(interval_seconds)
        if read_identity()["exact_live_board"]:
            report("live_pcb_identity", "PASS", requested_open=True, source="ipc")
            return True
    report("live_pcb_identity", "BLOCKED", reason=(
        "PCB Editor launched but exact live IPC was not proven. Inspect KiCad dialogs/IPC setup; "
        "no board edits were attempted. For the main batch, retry with --resume-pcb."))
    return False


class KonnectBoardSession:
    """Execution-only per-board Windows IPC isolation, with exact identity proof.

    KiCad 10.0.6 derives its socket from the process temporary directory. Give
    automatically launched editors a board-specific TEMP/TMP, not shared global
    preferences. Keep this runtime path outside compiler inputs and output hashes.
    Existing legacy editors may be reused only after exact read-only IPC proof.
    """

    def __init__(self, *, board: Path, executable: Path, evidence: Path):
        self.board = board.resolve()
        self.executable = executable
        self.evidence = evidence
        identity = os.path.normcase(str(self.board)).encode("utf-8")
        self.editor_temp = Path(tempfile.gettempdir()) / "oomlout-kicad" / hashlib.sha256(identity).hexdigest()[:20]
        self.private_address = "ipc://" + str(self.editor_temp / "kicad" / "api.sock") if os.name == "nt" else None
        self.selected_address: str | None = None
        self.proven = False

    def transport(self, *, runtime_directory: Path):
        from .adapters.konnect.transport import StdioKonnectTransport
        if not self.proven:
            raise RuntimeError("Exact PCB IPC identity has not been proven for this session")
        return StdioKonnectTransport(self.executable, runtime_directory, ipc_address=self.selected_address)

    def read_identity(self) -> dict:
        from .adapters.konnect.transport import StdioKonnectTransport
        from .executor import interpret_tool_result
        self.proven = False
        self.selected_address = None
        candidates = [None]
        if self.private_address:
            # Enumerate names only: opening a raw pipe to test it can wedge NNG.
            names = {name.casefold() for name in os.listdir(r"\\.\pipe")}
            if self.private_address[6:].casefold() in names:
                candidates.insert(0, self.private_address)
        self.evidence.mkdir(parents=True, exist_ok=True)
        probes = []
        observed_boards = []
        for index, address in enumerate(candidates):
            transport = StdioKonnectTransport(self.executable, self.evidence / f"endpoint_{index}", ipc_address=address)
            payload = {"requested_endpoint": address}
            try:
                payload["installation"] = transport.initialize()
                opened = interpret_tool_result(transport.call_tool("open_project", {"path": self.board.as_posix()}))
                info = interpret_tool_result(transport.call_tool("get_board_info", {"board": self.board.as_posix()}))
                payload.update(open_project=opened, board_info=info)
                requested, details = opened.get("structured"), info.get("structured")
                requested = requested if isinstance(requested, dict) else {}
                details = details if isinstance(details, dict) else {}
                observed_boards.extend(requested.get("open_boards", []))
                self.proven = bool(opened.get("success") and info.get("success")
                    and requested.get("ipc_available") is True and requested.get("requested_open") is True
                    and details.get("source") == "ipc"
                    and Path(str(details.get("file", ""))).resolve() == self.board)
                if self.proven:
                    self.selected_address = requested.get("ipc_address") or address
            finally:
                payload["shutdown"] = transport.shutdown()
                payload["exact_live_board"] = self.proven
                probes.append(payload)
                with (self.evidence / "identity_attempts.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(payload, default=str) + "\n")
            if self.proven:
                break
        result = {"exact_live_board": self.proven, "ipc_address": self.selected_address,
                  "observed_boards": observed_boards,
                  "payload": {"probes": probes}}
        (self.evidence / "identity.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        return result

    def ensure(self, *, report: Callable[..., None], auto_open: bool = True,
               editor: Path = PCB_EDITOR, attempts: int = 12, interval_seconds: float = 5) -> bool:
        environment = None
        if self.private_address:
            self.editor_temp.mkdir(parents=True, exist_ok=True)
            environment = {**os.environ, "TEMP": str(self.editor_temp), "TMP": str(self.editor_temp)}
        report("pcb_ipc_connection", "SELECTING", board=str(self.board), private_endpoint=self.private_address)
        result = ensure_live_board(board=self.board, read_identity=self.read_identity, report=report,
            auto_open=auto_open, editor=editor, attempts=attempts, interval_seconds=interval_seconds,
            launch_environment=environment)
        if result:
            report("pcb_ipc_connection", "PASS", board=str(self.board), ipc_address=self.selected_address)
        return result
