"""Surrounding native Trend widgets without changing a report's test history."""
import json
from pathlib import Path

from .allure_source import MAX_ARTIFACT, ViewerError
from .storage import RunStorage

TREND_FILES = ('history-trend.json', 'duration-trend.json', 'retry-trend.json', 'categories-trend.json')


def _read(path):
    if path.parent.is_symlink():
        raise ViewerError('Symlinked generated Allure Trend data')
    body = RunStorage._read_source(path, max_bytes=MAX_ARTIFACT)
    if len(body) > MAX_ARTIFACT:
        raise ViewerError('Generated Allure Trend data exceeds size limit')
    value = json.loads(body)
    if not isinstance(value, list):
        raise ViewerError('Invalid generated Allure Trend data')
    if len(value) > 20:
        raise ViewerError('Allure native Trend limit exceeded')
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get('data'), dict):
            raise ViewerError('Invalid generated Allure Trend entry')
        order, url = row.get('buildOrder'), row.get('reportUrl')
        if order is not None and type(order) is not int:
            raise ViewerError('Invalid generated Allure Trend order')
        if url is not None and not isinstance(url, str):
            raise ViewerError('Invalid generated Allure Trend URL')
    return value


def surrounding_widgets(selected, latest, target, later_count=19):
    """Use the generated chain's overlap, preserving optional executor metadata."""
    if type(later_count) is not int or not 1 <= later_count <= 19:
        raise ViewerError('Invalid surrounding Allure Trend window')
    for name in TREND_FILES:
        before, after = Path(selected) / 'widgets' / name, Path(latest) / 'widgets' / name
        if not (before.exists() or before.is_symlink()) and not (after.exists() or after.is_symlink()):
            continue
        earlier, later = _read(before), _read(after)
        overlap = later[later_count:]
        if len(later) < later_count or overlap != earlier[:len(overlap)]:
            raise ViewerError('Conflicting generated Allure Trend entries')
        value = [*later[:later_count], *earlier]
        body = json.dumps(value, allow_nan=False).encode()
        if len(body) > MAX_ARTIFACT:
            raise ViewerError('Allure surrounding Trend data exceeds size limit')
        (Path(target) / 'widgets' / name).write_bytes(body)
