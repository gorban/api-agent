"""Opt-in application coverage diagnostic inside the canonical E2E executor.

coverage.py's supported subprocess patch follows Python server/worker children.
These artifacts never enforce unit coverage or establish deletion safety.
"""
import ast
import hashlib
import json
import os
from pathlib import Path


def _write(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as output:
        json.dump(value, output, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _source_hashes(sources):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted({file.resolve() for source in sources for file in Path(source).rglob('*.py')
                                if file.is_file()})}


def prepare(directory, sources, args, env, *, selection=None, parallel_jobs=None,
            e2e_reruns=None, flow_identities=()):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    sources = tuple(dict.fromkeys(str(Path(source).resolve()) for source in sources))
    if not sources:
        raise ValueError('E2E coverage requires declared application sources')
    _write(directory / 'source-manifest.json', {'sources': sources,
        'source_hashes': _source_hashes(sources), 'flow_identities': list(flow_identities)})
    _write(directory / 'diagnostic.json', {'version': 1, 'kind': 'e2e-diagnostic',
        'coverage_gate': False, 'status': 'pending', 'selection': None if selection is None else list(selection),
        'parallel_jobs': parallel_jobs, 'e2e_reruns': e2e_reruns,
        'owned_shutdown_complete': False, 'report': None})
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
    """List missed bodies only when line evidence excludes nested body execution."""
    candidates = []
    for name, row in document.get('files', {}).items():
        try:
            tree = ast.parse(Path(name).read_text())
        except (OSError, SyntaxError):
            continue
        missing, executed = set(row.get('missing_lines', [])), set(row.get('executed_lines', []))
        def visit(parent, prefix=''):
            for node in ast.iter_child_nodes(parent):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified = prefix + node.name
                    body = node.body
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                        body = body[1:]
                    if body:
                        lines = set(range(body[0].lineno, node.end_lineno + 1))
                        shared = set()
                        for nested in ast.walk(node):
                            if nested is not node and isinstance(nested, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                for statement in nested.body:
                                    if statement.lineno == nested.lineno:
                                        shared.add(nested.lineno)
                                    lines.difference_update(range(statement.lineno, statement.end_lineno + 1))
                        statements = lines & (missing | executed)
                        if (node.lineno not in lines and not (shared & executed)
                                and statements and statements <= missing):
                            candidates.append({'file': name, 'function': qualified,
                                'line': node.lineno, 'missed_body_lines': sorted(statements),
                                'disposition': 'candidate-only-review-excluded-paths-and-unit-evidence'})
                    visit(node, qualified + '.<locals>.')
                elif isinstance(node, ast.ClassDef):
                    visit(node, prefix + node.name + '.')
                else:
                    visit(node, prefix)
        visit(tree)
    return candidates


def finalize(directory, *, selection, parallel_jobs, e2e_reruns, complete, successful=True):
    """Combine only after owned workers and Python server children are drained."""
    from coverage import Coverage
    from coverage.exceptions import CoverageException
    directory = Path(directory)
    manifest = json.loads((directory / 'source-manifest.json').read_text())
    pending = json.loads((directory / 'diagnostic.json').read_text())
    receipt = {'version': 1, 'kind': 'e2e-diagnostic', 'coverage_gate': False,
        'status': 'incomplete', 'selection': list(selection), 'parallel_jobs': parallel_jobs,
        'e2e_reruns': e2e_reruns, 'owned_shutdown_complete': complete,
        'scenario_successful': successful,
        'instrumentation': 'coverage.py subprocess patch + pytest-cov',
        'process_completeness': 'unknown', 'uninstrumented': ['launcher coordinator', 'native and external processes'],
        'report': None, 'function_body_candidates': [],
        'candidate_disposition': 'No deletion justified without excluded-path, unit and integration review.'}
    if (pending['selection'] is not None and pending['selection'] != list(selection)) or (
            pending['parallel_jobs'] is not None and pending['parallel_jobs'] != parallel_jobs) or (
            pending['e2e_reruns'] is not None and pending['e2e_reruns'] != e2e_reruns):
        receipt['reason'] = 'selection_changed'
    elif _source_hashes(manifest['sources']) != manifest['source_hashes']:
        receipt['reason'] = 'source_identity_changed'
    else:
        try:
            coverage = Coverage(config_file=str(directory / 'coverage.ini'))
            coverage.set_option('run:data_file', str(directory / '.coverage'))
            coverage.load()
            coverage.combine(data_paths=[str(directory)], strict=False, keep=True)
            coverage.save()
            coverage.json_report(outfile=str(directory / 'coverage.json'))
            coverage.html_report(directory=str(directory / 'html'))
            report = json.loads((directory / 'coverage.json').read_text())
            receipt['report'] = str(directory / 'coverage.json')
            receipt['function_body_candidates'] = function_body_candidates(report)
            if _source_hashes(manifest['sources']) != manifest['source_hashes']:
                receipt.update(status='incomplete', reason='source_identity_changed', report=None,
                               function_body_candidates=[])
            elif complete and successful:
                receipt['status'] = 'partial'
                receipt['reason'] = 'process_completeness_unknown'
            else:
                receipt['reason'] = 'shutdown_incomplete' if not complete else 'scenario_failed'
        except (CoverageException, OSError, ValueError):
            receipt.update(reason='coverage_data_unavailable', report=None, function_body_candidates=[])
    _write(directory / 'diagnostic.json', receipt)
    return receipt
