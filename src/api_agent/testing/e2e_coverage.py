"""Opt-in application coverage diagnostic inside the canonical E2E executor.

coverage.py's supported subprocess patch follows Python server/worker children.
These artifacts never enforce unit coverage or establish deletion safety.
"""
import ast
import json
from pathlib import Path


def prepare(directory, sources, args, env):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    sources = tuple(dict.fromkeys(str(Path(source).resolve()) for source in sources))
    if not sources:
        raise ValueError('E2E coverage requires declared application sources')
    config = directory / 'coverage.ini'
    config.write_text('[run]\nbranch = true\nparallel = true\npatch = subprocess\n'
        'sigterm = true\nsource =\n' + ''.join('    ' + source + '\n' for source in sources)
        + 'omit = */tests/*, */owned_e2e/*, */qualification_tests/*\n'
        '[report]\ninclude_namespace_packages = true\n')
    env['COVERAGE_FILE'] = str(directory / '.coverage')
    args.extend(['--cov-config=' + str(config), *('--cov=' + source for source in sources),
                 '--cov-branch', '--cov-report='])
    return config


def function_body_candidates(document):
    """List fully missed executable function bodies, never module/import misses."""
    candidates = []
    for name, row in document.get('files', {}).items():
        try:
            tree = ast.parse(Path(name).read_text())
        except (OSError, SyntaxError):
            continue
        missing, executed = set(row.get('missing_lines', [])), set(row.get('executed_lines', []))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                body = body[1:]
            if not body:
                continue
            start, stop = body[0].lineno, node.end_lineno
            statements = {line for line in missing | executed if start <= line <= stop}
            if statements and statements <= missing:
                candidates.append({'file': name, 'function': node.name,
                    'line': node.lineno, 'missed_body_lines': sorted(statements),
                    'disposition': 'candidate-only-review-excluded-paths-and-unit-evidence'})
    return candidates


def finalize(directory, *, selection, parallel_jobs, e2e_reruns, complete):
    """Combine only after owned workers and Python server children are drained."""
    from coverage import Coverage
    directory = Path(directory)
    coverage = Coverage(config_file=str(directory / 'coverage.ini'))
    coverage.set_option('run:data_file', str(directory / '.coverage'))
    coverage.load()
    coverage.combine(data_paths=[str(directory)], strict=False, keep=True)
    coverage.save()
    coverage.json_report(outfile=str(directory / 'coverage.json'))
    coverage.html_report(directory=str(directory / 'html'))
    report = json.loads((directory / 'coverage.json').read_text())
    receipt = {'version': 1, 'kind': 'e2e-diagnostic', 'coverage_gate': False,
        'selection': list(selection), 'parallel_jobs': parallel_jobs, 'e2e_reruns': e2e_reruns,
        'owned_shutdown_complete': complete,
        'instrumentation': 'coverage.py subprocess patch + pytest-cov',
        'report': str(directory / 'coverage.json'),
        'function_body_candidates': function_body_candidates(report),
        'candidate_disposition': 'No deletion justified without excluded-path, unit and integration review.'}
    (directory / 'diagnostic.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt
