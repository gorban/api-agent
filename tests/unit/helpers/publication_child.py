"""Disposable pytest entry point; never imported by the unit collector."""
import json
import os
from pathlib import Path
import sys

import coverage

settings = json.loads(Path(sys.argv[1]).read_text())
tracer = coverage.Coverage(data_file=settings['data_file'], branch=True,
                          source=settings['source'], omit=['*/tests/*'], config_file=False)
tracer.set_option('run:relative_files', settings['relative_files'])
tracer.set_option('run:context', settings['context'])
tracer.start()
# Prove the child actually traced a known production body, including branch data.
from api_agent.testing.processes import disposal_complete
from types import SimpleNamespace
assert disposal_complete(SimpleNamespace()) is False

import pytest

class Admission:
    def pytest_runtest_setup(self, item):
        Path(settings['entered']).write_text(item.nodeid)

Path(settings['admitted']).write_text('admitted')
try:
    status = pytest.main(['-q', '-p', 'no:cacheprovider', '-p', 'no:cov', settings['node']],
                         plugins=[Admission()])
finally:
    tracer.stop()
    tracer.save()
    data = tracer.get_data()
    import inspect
    filename = inspect.getsourcefile(disposal_complete)
    if settings['relative_files']:
        filename = os.path.relpath(filename)
    line = disposal_complete.__code__.co_firstlineno + 2
    assert data.has_arcs() and line in (data.lines(filename) or []), 'Child production tracing absent'
    Path(settings['traced']).write_text('traced')
sys.exit(status)
