"""Bind generic component deadline reports to the current pytest item."""
import json
import os

import pytest


def write_lazy_plugin(directory):
    """Defer application imports until coverage has started in pytest workers."""
    path = directory / 'api_agent_component_plugin.py'
    path.write_text('''import pytest
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    from api_agent.testing.progress import pytest_runtest_protocol as delegate
    yield from delegate(item, nextitem)
''')
    return 'api_agent_component_plugin'


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Bind component timing in each worker, including xdist workers."""
    from dataclasses import asdict
    from api_agent.providers.progress import progress_sink
    path = os.environ.get('API_AGENT_COMPONENT_EVENTS')
    def send(component):
        row = {'kind': 'component_progress', 'node': item.nodeid,
               'component': asdict(component)}
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, allow_nan=False) + '\n')
    with progress_sink(send if path else None):
        yield
