"""Logical E2E completions reach consumers while other cases still run."""

import pytest

from api_agent.testing import integration
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.terminal_progress import TerminalTestProgress


def _identity(name):
    return TestIdentity(
        f"suite::test_{name}",
        "suite",
        "test_cases.py",
        f"test_cases.py::test_{name}",
        tags=frozenset({"requires_ui_session"}),
    )


@pytest.mark.parametrize(
    ("allowance", "first_status"),
    [(0, "passed"), (0, "failed"), (2, "passed"), (2, "skipped")],
)
def test_logical_completion_is_visible_before_other_case_finishes(
    monkeypatch, allowance, first_status
):
    first, second = _identity("first"), _identity("second")
    events, lines, physical = [], [], []
    progress = TerminalTestProgress([first, second], lines.append)

    def observe(event):
        events.append(event)
        progress(event)

    def execute(_context, tests, **options):
        assert tuple(tests) == (first, second)
        options["before_run"]()
        options["on_event"](RunnerEvent("test_started", first.repo, first))
        options["on_event"](
            RunnerEvent("test_finished", first.repo, first, first_status, 1.25)
        )
        # This assertion executes inside the still-running wave, before the
        # second test starts or its result exists.
        assert [
            (e.identity.id, e.status) for e in events if e.kind == "test_finished"
        ] == [(first.id, first_status)]
        assert len(lines) == 1 and "1/2 completed" in lines[0]
        options["on_result"](TestResult(first, first_status, 1.25))
        options["on_event"](RunnerEvent("test_started", second.repo, second))
        options["on_event"](
            RunnerEvent("test_finished", second.repo, second, "passed", 2)
        )
        options["on_result"](TestResult(second, "passed", 2))
        return integration.IntegrationRunResult(int(first_status == "failed"), (), "")

    monkeypatch.setattr(integration, "_run_integration_tests", execute)
    result = integration.run_integration_tests(
        object(),
        [first, second],
        accepted=True,
        config={"TUI_MODE": "e2e", "E2E_RERUNS": allowance},
        on_event=observe,
        on_result=physical.append,
    )
    finished = [event for event in events if event.kind == "test_finished"]
    assert [(event.identity.id, event.status) for event in finished] == [
        (first.id, first_status),
        (second.id, "passed"),
    ]
    assert len(lines) == 2 and "2/2 completed" in lines[1]
    assert [(row.logical_id, row.attempt) for row in physical] == [
        (first.id, 1),
        (second.id, 1),
    ]
    assert [row.status for row in result.tests] == [first_status, "passed"]


@pytest.mark.parametrize(
    ("statuses", "final_status"),
    [(["failed", "passed", "passed"], "passed"), (["failed", "failed"], "failed")],
)
def test_failed_case_remains_pending_until_confirmation_is_final(
    monkeypatch, statuses, final_status
):
    test, other = _identity("retried"), _identity("other")
    events, physical = [], []
    attempts = iter(statuses)
    wave = 0

    def execute(_context, tests, **options):
        nonlocal wave
        wave += 1
        options["before_run"]()
        current = next(attempts)
        assert test in tests
        options["on_event"](RunnerEvent("test_started", test.repo, test))
        options["on_event"](
            RunnerEvent("test_finished", test.repo, test, current, wave)
        )
        finished = [
            e for e in events if e.kind == "test_finished" and e.identity.id == test.id
        ]
        if wave < len(statuses):
            assert finished == []
        else:
            assert [(e.identity.id, e.status, e.duration) for e in finished] == [
                (test.id, final_status, sum(range(1, len(statuses) + 1)))
            ]
        options["on_result"](TestResult(test, current, wave))
        if other in tests:
            options["on_event"](RunnerEvent("test_started", other.repo, other))
            options["on_event"](
                RunnerEvent("test_finished", other.repo, other, "passed", 1)
            )
            options["on_result"](TestResult(other, "passed", 1))
        return integration.IntegrationRunResult(int(current == "failed"), (), "")

    monkeypatch.setattr(integration, "_run_integration_tests", execute)
    result = integration.run_integration_tests(
        object(),
        [test, other],
        accepted=True,
        config={"TUI_MODE": "e2e", "E2E_RERUNS": 2},
        on_event=events.append,
        on_result=physical.append,
    )
    assert result.tests[0].status == final_status
    assert result.tests[0].first_status == "failed"
    assert [row.attempt for row in physical if row.logical_id == test.id] == list(
        range(1, len(statuses) + 1)
    )
    assert len(
        {row.identity.id for row in physical if row.logical_id == test.id}
    ) == len(statuses)
    assert [(e.identity.id, e.status) for e in events if e.kind == "test_finished"] == [
        (other.id, "passed"),
        (test.id, final_status),
    ]


def test_failing_early_observer_does_not_disable_attempt_recording(monkeypatch):
    first, second = _identity("first"), _identity("second")
    observed, physical, publications = [], [], []
    failures = []

    def observe(event):
        if event.kind == "test_finished" and event.identity == first:
            raise RuntimeError("synthetic observer failure")
        observed.append(event)

    def execute(_context, tests, **options):
        options["before_run"]()
        event_disabled = False
        for test in tests:
            for event in (
                RunnerEvent("test_started", test.repo, test),
                RunnerEvent("test_finished", test.repo, test, "passed", 1),
            ):
                if not event_disabled:
                    try:
                        options["on_event"](event)
                    except RuntimeError:
                        # Mirrors the inner runner's separately disabled
                        # on_event channel; on_result must stay enabled.
                        failures.append("on_event")
                        event_disabled = True
            options["on_result"](TestResult(test, "passed", 1))
        return integration.IntegrationRunResult(
            1, (), "", infrastructure_failed=bool(failures)
        )

    monkeypatch.setattr(integration, "_run_integration_tests", execute)
    result = integration.run_integration_tests(
        object(),
        [first, second],
        accepted=True,
        config={"TUI_MODE": "e2e", "E2E_RERUNS": 0},
        on_event=observe,
        on_result=physical.append,
        after_run=publications.append,
    )
    assert failures == ["on_event"]
    assert [row.logical_id for row in physical] == [first.id, second.id]
    assert len(result.attempts) == 2
    assert result.infrastructure_failed and result.exit_code == 1
    assert publications == [result]
    assert [
        (e.identity.id, e.status) for e in observed if e.kind == "test_finished"
    ] == [(second.id, "passed")]


def test_cancellation_after_early_completion_preserves_finished_result(monkeypatch):
    from threading import Event

    first, second = _identity("first"), _identity("second")
    cancel, events, physical, publications = Event(), [], [], []

    def observe(event):
        events.append(event)
        if event.kind == "test_finished" and event.identity == first:
            cancel.set()

    def execute(_context, tests, **options):
        options["before_run"]()
        options["on_event"](RunnerEvent("test_started", first.repo, first))
        options["on_event"](
            RunnerEvent("test_finished", first.repo, first, "passed", 1)
        )
        options["on_result"](TestResult(first, "passed", 1))
        assert cancel.is_set()
        return integration.IntegrationRunResult(130, (), "", cancelled=True)

    monkeypatch.setattr(integration, "_run_integration_tests", execute)
    result = integration.run_integration_tests(
        object(),
        [first, second],
        accepted=True,
        config={"TUI_MODE": "e2e", "E2E_RERUNS": 0},
        cancel_event=cancel,
        on_event=observe,
        on_result=physical.append,
        after_run=publications.append,
    )
    assert result.exit_code == 130 and result.cancelled
    assert [row.status for row in result.tests] == ["passed", "cancelled"]
    assert [row.logical_id for row in physical] == [first.id]
    assert publications == [result]
    assert [(e.identity.id, e.status) for e in events if e.kind == "test_finished"] == [
        (first.id, "passed"),
        (second.id, "cancelled"),
    ]
