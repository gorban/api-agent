"""Single-flight, read-only configuration refresh outside the UI event loop."""
from copy import copy, deepcopy
from dataclasses import dataclass


def controls(session):
    """Inputs owned by UI actions, as opposed to files reread by the resolver."""
    return (session.context, session._provider_base_schema, session.argv,
            dict(session.environ), deepcopy(session._choices), session._tool_selection,
            session._automatic_tool_selection, session._tool_intents, session.dotenv_path,
            deepcopy(session._runtime_requirements), session._runtime_requirement_token)


def fork_session(session):
    fork = copy(session)
    fork.environ = dict(session.environ)
    fork._choices = deepcopy(session._choices)
    fork._definition_cache = session._definition_cache.fork()
    return fork


def read_snapshot(session):
    snapshot = session.review_snapshot()
    return snapshot, snapshot.sources_current()


@dataclass(frozen=True)
class CleanupSnapshot:
    rows: tuple
    dotenv_inputs: tuple
    source_reads: object

    def sources_current(self):
        from api_agent.config.dotenv import read_dotenv
        return (all(read_dotenv(path) == dict(entries)
                    for path, entries in self.dotenv_inputs)
                and self.source_reads.current())


def read_cleanup_snapshot(session, retained):
    from api_agent.config.source_reads import capture_reads
    with capture_reads() as reads:
        prepared = session._prepare_resolution(())
        rows = tuple(session.cleanup_rows(retained=retained, _prepared=prepared))
        inputs = prepared[-1]
    snapshot = CleanupSnapshot(rows, inputs, reads.snapshot())
    return snapshot, snapshot.sources_current()


class ReviewRefresher:
    label = 'configuration review'

    def read(self, session):
        return read_snapshot(session)

    def apply(self, snapshot):
        self.app.apply_configuration_snapshot(self.screen, snapshot)

    def __init__(self, app, screen):
        self.app, self.screen = app, screen
        self.generation = 0
        self.worker = None

    def invalidate(self):
        self.generation += 1

    def request(self):
        self.invalidate()
        if self.worker is None:
            self.worker = self.app.run_worker(self.run(), group='configuration-display')

    async def run(self):
        source_retries = 0
        try:
            while self.app.screen is self.screen:
                generation = self.generation
                session = self.app.session
                inputs = controls(session)
                fork = fork_session(session)
                try:
                    snapshot, current = await self.app.background_reads.run(
                        self.label, self.read, fork)
                    error = None
                except Exception:
                    snapshot, error = None, 'Configuration refresh failed; move to retry.'
                if self.app.screen is not self.screen:
                    return
                if generation != self.generation:
                    source_retries = 0
                    continue
                if self.app.session is not session or controls(session) != inputs:
                    return
                if error is None and current:
                    try:
                        # No await separates this small source check from the
                        # UI update. Discovery/composition stays in the worker.
                        current = snapshot.sources_current()
                    except Exception:
                        error = 'Configuration refresh failed; move to retry.'
                if error is not None or not current:
                    source_retries += 1
                    if source_retries < 3:
                        continue
                    if error is None:
                        error = 'Configuration sources keep changing; move to retry.'
                if error is None:
                    self.apply(snapshot)
                else:
                    self.screen.query_one('#error').update(error)
                return
        finally:
            self.worker = None


class CleanupRefresher(ReviewRefresher):
    label = 'configuration cleanup'

    def __init__(self, app, screen):
        super().__init__(app, screen)
        self.retained = app.cleanup_names

    def read(self, session):
        return read_cleanup_snapshot(session, self.retained)

    def apply(self, snapshot):
        self.app.apply_cleanup_rows(self.screen, snapshot.rows)
        self.screen.query_one('#error').update('')
