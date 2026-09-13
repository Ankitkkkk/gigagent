"""Dependency-free values shared by CLI models and presentations."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ViewEvent:
    source: str
    kind: str
    revision: int
    workspace_id: str | None = None
    agent_id: str | None = None
    message_ids: tuple = ()
    selection_generation: int | None = None
    text: str | None = None


@dataclass(frozen=True)
class ActionOutcome:
    status: str  # completed, cancelled, failed
    message: str | None = None
    workspace_id: str | None = None
    agent_id: str | None = None


@dataclass(frozen=True)
class SubmitOutcome:
    status: str  # completed, cancelled, failed
    keep_running: bool = True
    sent: bool = False  # transport accepted, not a server acknowledgment
    message: str | None = None
