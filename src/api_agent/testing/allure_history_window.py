"""Retain configurable windows of genuine Allure-generated history entries."""
import json
from pathlib import Path

from .allure_source import MAX_ARTIFACT, ViewerError
from .allure_trends import TREND_FILES, _read
from .storage import RunStorage

STATUSES = ('failed', 'broken', 'skipped', 'passed', 'unknown')


def _decode(body):
    if len(body) > MAX_ARTIFACT:
        raise ViewerError('Allure history window exceeds size limit')
    return json.loads(body)


def _encode(value):
    body = json.dumps(value, allow_nan=False).encode()
    if len(body) > MAX_ARTIFACT:
        raise ViewerError('Allure history window exceeds size limit')
    return body


def _entries(value):
    if not isinstance(value, dict):
        raise ViewerError('Invalid Allure test history')
    for entry in value.values():
        if not isinstance(entry, dict) or not isinstance(entry.get('items'), list):
            raise ViewerError('Invalid Allure test history entry')
        if any(not isinstance(row, dict) or row.get('status') not in STATUSES for row in entry['items']):
            raise ViewerError('Invalid Allure test history status')
        statistic = entry.get('statistic')
        if not isinstance(statistic, dict) or any(type(statistic.get(key)) is not int or statistic[key] < 0
                                               for key in (*STATUSES, 'total')):
            raise ViewerError('Invalid Allure test history statistics')
        if sum(statistic[key] for key in STATUSES) != statistic['total']:
            raise ViewerError('Conflicting Allure test history statistics')
    return value


def prepare_history(history, count):
    """Bound prior entries before generation, including per-test pass-rate inputs."""
    result = dict(history)
    if 'history.json' in history:
        entries = _entries(_decode(history['history.json']))
        for entry in entries.values():
            entry['items'] = entry['items'][:count - 1]
            entry['statistic'] = {status: sum(row['status'] == status for row in entry['items'])
                                  for status in STATUSES}
            entry['statistic']['total'] = len(entry['items'])
        result['history.json'] = _encode(entries)
    for name in TREND_FILES:
        if name in history:
            rows = _decode(history[name])
            if not isinstance(rows, list):
                raise ViewerError('Invalid Allure Trend history')
            result[name] = _encode(rows[:count - 1])
    return result


def _file(path):
    if path.parent.is_symlink():
        raise ViewerError('Symlinked generated Allure history')
    return RunStorage._read_source(path, max_bytes=MAX_ARTIFACT)


def extend_history(output, prior, count):
    """Restore native truncated tails; never fabricate result UIDs or links."""
    root = Path(output)
    path = root / 'history/history.json'
    if path.exists() or path.is_symlink():
        entries = _entries(_decode(_file(path)))
        previous = _entries(_decode(prior.get('history.json', b'{}')))
        for key, entry in entries.items():
            old = previous.get(key)
            if old is None:
                if entry['statistic']['total'] > len(entry['items']):
                    raise ViewerError('Generated Allure test history is incomplete')
                continue
            added = entry['statistic']['total'] - old['statistic']['total']
            current, earlier = entry['items'], old['items']
            if not 0 <= added <= len(current) or current[added:] != earlier[:len(current) - added]:
                raise ViewerError('Conflicting generated Allure test history')
            entry['items'] = [*current[:added], *earlier][:count]
        path.write_bytes(_encode(entries))
    for name in TREND_FILES:
        path = root / 'history' / name
        if not (path.exists() or path.is_symlink()):
            continue
        current = _read(path)
        previous = _decode(prior.get(name, b'[]'))
        if not isinstance(previous, list) or not current or current[1:] != previous[:len(current) - 1]:
            raise ViewerError('Conflicting generated Allure Trend history')
        body = _encode([current[0], *previous][:count])
        path.write_bytes(body)
        widget = root / 'widgets' / name
        if widget.exists() or widget.is_symlink():
            if _read(widget) != current:
                raise ViewerError('Conflicting generated Allure Trend widget')
            widget.write_bytes(body)
