"""Mode-independent per-test outcomes and runner events."""
from dataclasses import dataclass
from .collection import TestIdentity


@dataclass(frozen=True)
class TestResult:
    __test__ = False
    identity: TestIdentity
    status: str
    duration: float = 0
    traceback: str = ''


@dataclass(frozen=True)
class RunnerEvent:
    kind: str
    repo: str = ''
    identity: TestIdentity | None = None
    status: str | None = None
    duration: float = 0
    traceback: str = ''
    text: str = ''


