"""Operation-owned, synchronous authentication transport.

Values and live transcripts must never be included in diagnostic representations.
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Reply:
    action: str
    value: str = field(default='', repr=False)


class Channel(Protocol):
    def write(self, text: str) -> None: ...
    def read(self, timeout: float) -> Reply | None: ...
    def close(self) -> None: ...


class Interaction(Protocol):
    def open(self, label: str) -> Channel: ...
