"""Single-flight, read-only configuration refresh outside the UI event loop."""
import asyncio
from copy import copy, deepcopy


def controls(session):
    """Inputs owned by UI actions, as opposed to files reread by the resolver."""
    return (session.context, session._provider_base_schema, session.argv,
            dict(session.environ), deepcopy(session._choices), session._tool_selection,
            session._automatic_tool_selection, session._tool_intents, session.dotenv_path)


def fork_session(session):
    fork = copy(session)
    fork.environ = dict(session.environ)
    fork._choices = deepcopy(session._choices)
    fork._definition_cache = session._definition_cache.fork()
    return fork


def read_snapshot(session):
    snapshot = session.review_snapshot()
    return snapshot, snapshot.sources_current()


class ReviewRefresher:
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
                    snapshot, current = await asyncio.to_thread(read_snapshot, fork)
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
                    self.app.apply_configuration_snapshot(self.screen, snapshot)
                else:
                    self.screen.query_one('#error').update(error)
                return
        finally:
            self.worker = None
