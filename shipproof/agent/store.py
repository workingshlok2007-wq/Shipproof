"""Thread-safe project/packet store with atomic JSON persistence, plus a per-packet event bus for WebSockets."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from shipproof.agent.project import AuditSummary, Project
from shipproof.models import (
    AGENT_ACTOR,
    ApprovalState,
    CoverageLedger,
    ModelError,
    ProofCheck,
    Severity,
    SubmissionPacket,
    UncertaintyFlag,
    UncertaintyKind,
    transitionPacket,
    utcNow,
)

MAX_EVENT_HISTORY = 1000
SUBSCRIBER_QUEUE_SIZE = 1000


class StoreError(RuntimeError):
    pass


class NotFoundError(StoreError):
    pass


class ConflictError(StoreError):
    pass


class ProjectStore:
    def __init__(self, dataFile: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._dataFile = dataFile
        self._projects: dict[str, Project] = {}
        self._packets: dict[str, SubmissionPacket] = {}
        self._summaries: dict[str, AuditSummary] = {}
        self._running: set[str] = set()
        if dataFile is not None and dataFile.is_file():
            self._load(dataFile)

    def _load(self, dataFile: Path) -> None:
        try:
            raw = json.loads(dataFile.read_text(encoding="utf-8"))
            self._projects = {k: Project.model_validate(v) for k, v in raw.get("projects", {}).items()}
            self._packets = {k: SubmissionPacket.model_validate(v) for k, v in raw.get("packets", {}).items()}
            self._summaries = {k: AuditSummary.model_validate(v) for k, v in raw.get("summaries", {}).items()}
        except (OSError, ValueError) as error:
            raise StoreError(f"cannot load data file {dataFile}: {error}") from error
        for packetId, packet in list(self._packets.items()):
            if packet.approvalState is ApprovalState.AUDITING:  # audit was interrupted by a restart
                self._packets[packetId] = self._failedPacket(packet, "Server restarted during the audit.")

    def _save(self) -> None:
        if self._dataFile is None:
            return
        payload = json.dumps({
            "projects": {k: v.model_dump(mode="json") for k, v in self._projects.items()},
            "packets": {k: v.model_dump(mode="json") for k, v in self._packets.items()},
            "summaries": {k: v.model_dump(mode="json") for k, v in self._summaries.items()},
        })
        self._dataFile.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tempPath = tempfile.mkstemp(dir=self._dataFile.parent, prefix=".shipproof-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tempPath, self._dataFile)  # atomic on POSIX and Windows (same volume)
        except OSError as error:
            Path(tempPath).unlink(missing_ok=True)
            raise StoreError(f"cannot persist data file: {error}") from error

    # ---------------------------------------------------------------- projects

    def createProject(self, project: Project) -> Project:
        with self._lock:
            if project.projectId in self._projects:
                raise ConflictError(f"project {project.projectId} already exists")
            self._projects[project.projectId] = project
            self._save()
            return project

    def getProject(self, projectId: str) -> Project:
        with self._lock:
            if projectId not in self._projects:
                raise NotFoundError(f"project {projectId} not found")
            return self._projects[projectId]

    def updateProject(self, projectId: str, change: Callable[[Project], Project]) -> Project:
        with self._lock:
            updated = change(self.getProject(projectId))
            if updated.projectId != projectId:
                raise ConflictError("projectId cannot change")
            self._projects[projectId] = updated
            self._save()
            return updated

    # ---------------------------------------------------------------- packets

    def getPacket(self, packetId: str) -> SubmissionPacket:
        with self._lock:
            if packetId not in self._packets:
                raise NotFoundError(f"packet {packetId} not found")
            return self._packets[packetId]

    def getSummary(self, packetId: str) -> Optional[AuditSummary]:
        with self._lock:
            return self._summaries.get(packetId)

    def updatePacket(self, packetId: str, change: Callable[[SubmissionPacket], SubmissionPacket]) -> SubmissionPacket:
        with self._lock:
            updated = change(self.getPacket(packetId))
            self._packets[packetId] = updated
            self._save()
            return updated

    def beginAudit(self, projectId: str) -> tuple[Project, str]:
        with self._lock:
            project = self.getProject(projectId)
            if projectId in self._running:
                raise ConflictError(f"an audit is already running for {projectId}")
            packetId = f"pkt-{secrets.token_hex(8)}"
            draft = SubmissionPacket(packetId=packetId, charter=project.charter, proofChecks=project.proofChecks,
                                     workbenchChecks=project.workbenchChecks)
            self._packets[packetId] = transitionPacket(draft, ApprovalState.AUDITING, AGENT_ACTOR)
            self._projects[projectId] = project.model_copy(update={"packetIds": project.packetIds + (packetId,)})
            self._running.add(projectId)
            self._save()
            return project, packetId

    def completeAudit(self, projectId: str, packet: SubmissionPacket, summary: AuditSummary,
                      auditedChecks: tuple[ProofCheck, ...], snapshot: tuple[ProofCheck, ...]) -> None:
        """Writes refreshed proof statuses back only for checks the human did not change during the audit."""
        with self._lock:
            try:
                project = self.getProject(projectId)
                unchanged = {c.checkId: c for c in snapshot}
                refreshed = {c.checkId: c for c in auditedChecks}
                merged = tuple(
                    refreshed[c.checkId] if c.checkId in refreshed and unchanged.get(c.checkId) == c else c
                    for c in project.proofChecks
                )
                self._projects[projectId] = project.model_copy(update={"proofChecks": merged})
                self._packets[packet.packetId] = packet
                self._summaries[packet.packetId] = summary
            finally:
                self._running.discard(projectId)
                self._save()

    def failAudit(self, projectId: str, packetId: str, reason: str) -> SubmissionPacket:
        with self._lock:
            try:
                failed = self._failedPacket(self.getPacket(packetId), reason)
                self._packets[packetId] = failed
                return failed
            finally:
                self._running.discard(projectId)
                self._save()

    @staticmethod
    def _failedPacket(packet: SubmissionPacket, reason: str) -> SubmissionPacket:
        """A failed audit still ends at the human boundary, carrying a CRITICAL flag that blocks approval."""
        flag = UncertaintyFlag(flagId=f"f{len(packet.uncertaintyFlags) + 1}", kind=UncertaintyKind.ANALYSIS_FAILED,
                               detail=f"Audit did not complete: {reason}"[:512], severity=Severity.CRITICAL, raisedAt=utcNow())
        updated = packet.model_validate({**packet.model_dump(), "uncertaintyFlags": [*packet.uncertaintyFlags, flag],
                                         "ledger": CoverageLedger(sources=packet.ledger.sources)})
        if updated.approvalState is ApprovalState.AUDITING:
            updated = transitionPacket(updated, ApprovalState.PENDING_HUMAN_APPROVAL, AGENT_ACTOR)
        return updated


class EventBus:
    """Thread-safe publish from audit worker threads; asyncio delivery to WebSocket subscribers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}

    def publish(self, packetId: str, kind: str, data: dict[str, Any]) -> None:
        event = {"packetId": packetId, "kind": kind, "at": utcNow().isoformat(), "data": data}
        with self._lock:
            history = self._history.setdefault(packetId, [])
            history.append(event)
            del history[:-MAX_EVENT_HISTORY]
            subscribers = list(self._subscribers.get(packetId, []))
        for loop, queue in subscribers:
            loop.call_soon_threadsafe(self._offer, queue, event)

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Slow consumer: discard its backlog and tell it to re-fetch the packet instead of growing memory.
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait({"packetId": event["packetId"], "kind": "overflow", "at": event["at"],
                              "data": {"message": "events dropped; re-fetch the packet"}})

    def subscribe(self, packetId: str) -> tuple[asyncio.Queue, list[dict[str, Any]]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subscribers.setdefault(packetId, []).append((loop, queue))
            return queue, list(self._history.get(packetId, []))

    def unsubscribe(self, packetId: str, queue: asyncio.Queue) -> None:
        with self._lock:
            remaining = [(l, q) for l, q in self._subscribers.get(packetId, []) if q is not queue]
            if remaining:
                self._subscribers[packetId] = remaining
            else:
                self._subscribers.pop(packetId, None)
