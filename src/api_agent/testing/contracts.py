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


def format_test_summary(mode, tests, *, state=None):
    """Shared final outcome counts for terminal and interactive test modes."""
    total = len(tests)
    passed = sum(test.status == 'passed' for test in tests)
    skipped = sum(test.status == 'skipped' for test in tests)
    if state is None:
        state = 'PASS' if total and all(test.status in {'passed', 'skipped'} for test in tests) else 'FAIL'
    summary = f'{mode.capitalize()} results: {state} — {passed}/{total} passed'
    if mode == 'unit':
        if total != passed:
            summary += f'; {total - passed} did not pass'
    else:
        summary += f' ({100 * passed / total if total else 0:.2f}%)'
        if skipped:
            summary += f', {skipped}/{total} skipped ({100 * skipped / total:.2f}%)'
    return summary + '.'

