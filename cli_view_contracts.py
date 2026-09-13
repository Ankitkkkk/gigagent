"""Dependency-free values shared by CLI models and presentations."""

from dataclasses import dataclass


def terminal_text(value):
    """Keep chat content from emitting terminal control sequences."""
    return "".join(c for c in str(value) if c in "\n\t" or
                   (c.isprintable() and c != "\x1b"))


def _safe(value):
    return "".join(char for char in str(value)
                   if char.isprintable() and char != "\x1b")


def channel_transcript(messages, channel):
    """Return current-channel records in canonical display order."""
    return sorted((message for message in messages.values()
                   if message.get("channel", "general") == channel),
                  key=lambda message: (message.get("timestamp", 0), message["id"]))


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
    old_channel: str | None = None
    new_channel: str | None = None


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
