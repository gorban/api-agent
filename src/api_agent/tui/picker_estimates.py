"""Single-flight local cost estimates without blocking picker input."""
import asyncio
from copy import deepcopy
from threading import Event

from .configuration_refresh import controls, fork_session


def build_estimator(context, tests, result):
    from api_agent.testing.cost_history import estimator_for
    from .test_configuration import profile_cost_configs
    configs = profile_cost_configs(tests, result, context)
    snapshot = estimator_for(context, tests, result.values, test_configs=configs)
    snapshot.picker_configuration = deepcopy(result.values)
    snapshot.picker_provenance = tuple((name, field.source, field.explicit)
        for name, field in sorted(getattr(result, 'fields', {}).items()) if '_TEST_' in name)
    return snapshot


class PickerEstimates:
    def __init__(self, app, screen, selection):
        self.app, self.screen, self.selection = app, screen, selection
        self.visit = app._cost_history_generation
        self.generation = 0
        self.worker = None
        self.cancel = Event()

    def interrupt(self):
        self.cancel.set()

    def current(self):
        return (self.screen in self.app.screen_stack and self.app.selection is self.selection
                and self.visit == self.app._cost_history_generation
                and not self.app.test_run_active() and not self.app._inspection_shutdown
                and not getattr(self.screen, 'configuration_error', ''))

    def request(self):
        self.generation += 1
        if self.worker is None:
            self.worker = self.app.run_worker(self.run(), group='picker-estimates')

    def read(self, session, cancel=None):
        from api_agent.testing.history_projection import cancellable_projection, check_projection
        with cancellable_projection(self.cancel if cancel is None else cancel):
            from api_agent.providers.loading import provider_location
            from .test_configuration import _profile_revision
            review = session.review_snapshot()
            source = None
            location = review.execution.values.get('LLM_PROVIDER')
            if location:
                factory, _ = provider_location(location, owner_root=session.context.owner_root)
                source = factory.parent / 'test-profiles.toml'
            before = _profile_revision(source)
            snapshot = build_estimator(self.app.context, self.selection.tests, review.execution)
            try:
                check_projection()
            except Exception:
                snapshot.close()
                raise
            return snapshot, review, source, before

    @staticmethod
    def discard(read):
        if not read.cancelled() and read.exception() is None:
            read.result()[0].close()

    async def run(self):
        source_retries = 0
        try:
            while self.current():
                generation = self.generation
                session = self.app.session
                inputs = controls(session)
                fork = fork_session(session)
                self.cancel = Event()
                try:
                    read = asyncio.create_task(self.app.background_reads.run(
                        'picker cost estimates', self.read, fork, self.cancel))
                    try:
                        snapshot, review, source, before = await asyncio.shield(read)
                    except asyncio.CancelledError:
                        read.add_done_callback(self.discard)
                        raise
                except Exception as error:
                    from api_agent.testing.history_projection import ProjectionCancelled
                    if isinstance(error, ProjectionCancelled):
                        continue
                    return
                if not self.current():
                    snapshot.close()
                    return
                if (generation != self.generation or self.app.session is not session
                        or controls(session) != inputs):
                    snapshot.close()
                    continue
                from .test_configuration import _profile_revision
                try:
                    sources_current = review.sources_current() and _profile_revision(source) == before
                except Exception:
                    sources_current = False
                if not sources_current:
                    snapshot.close()
                    source_retries += 1
                    if source_retries < 3:
                        continue
                    return
                self.app.cost_estimator = snapshot
                self.app.refresh_picker_costs(self.screen, self.selection)
                self.app.run_worker(self.app.synchronize_cost_history(snapshot, self.selection),
                                    group='cost-history', exclusive=True)
                return
        finally:
            self.worker = None
