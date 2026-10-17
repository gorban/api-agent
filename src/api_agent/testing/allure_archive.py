"""Linear-space, cancellable preparation of an immutable Allure run index."""
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
import hashlib
import html
from pathlib import Path

from .allure_source import MAX_ARTIFACT, ViewerError, validate_manifest


@dataclass(frozen=True)
class HistoryWindow(Sequence):
    rows: tuple = field(repr=False)
    positions: range

    def __len__(self):
        return len(self.positions)

    def __getitem__(self, index):
        selected = self.positions[index]
        return HistoryWindow(self.rows, selected) if isinstance(selected, range) else self.rows[selected]


def checkpoint(budget, stop):
    if stop.is_set():
        raise asyncio.CancelledError()
    remaining = budget.remaining
    if remaining is not None and remaining <= 0:
        raise TimeoutError('Allure archive preparation timed out')


def prepare(source, manifests, count, budget, stop, output):
    """Share one snapshot per mode; materialize history only on route selection."""
    digests, groups, positions = {}, {}, {}
    context = hashlib.sha256((str(count) + ':').encode())
    for row in reversed(manifests):
        checkpoint(budget, stop)
        identifier = row['run_id']
        digest = validate_manifest(row, repo=source.repo)
        digests[identifier] = digest
        context.update(digest.encode())
        group = groups.setdefault(row['mode'], [])
        positions[identifier] = len(group)
        group.append(row)
    groups = {mode: tuple(rows) for mode, rows in groups.items()}
    context = context.hexdigest()
    windows, routes, summaries, links = {}, {}, {}, []
    size = 0
    for row in manifests:
        checkpoint(budget, stop)
        identifier, mode = row['run_id'], row['mode']
        digest, index = digests[identifier], positions[identifier]
        group = groups[mode]
        previous = HistoryWindow(group, range(index - 1, max(-1, index - count), -1))
        later = HistoryWindow(group, range(index + 1, min(len(group), index + count)))
        windows[identifier] = previous
        route = f'/open/{identifier}/{digest}/{context}'
        routes[route] = (source, row, previous, later)
        summary = f'/counts/{identifier}/{digest}'
        summaries[summary] = (source, row)
        prefix = f'{row["finished_at"]} — {mode} — '
        state = {'failed': 'run error', 'interrupted': 'interrupted'}.get(row['status'])
        suffix = (f' — {state}' if state else '') + f' — {identifier}'
        link = (f'<li><a href="{html.escape(route, quote=True)}">{html.escape(prefix)}'
                f'<span data-summary="{html.escape(summary, quote=True)}">Loading pass counts…</span>'
                f'{html.escape(suffix)}</a></li>')
        size += len(link.encode())
        if size > MAX_ARTIFACT:
            raise ViewerError('Allure archive index exceeds size limit')
        links.append(link)
    body = ('<!doctype html><meta charset="utf-8"><title>Allure run archive</title>'
        f'<h1>Allure run archive</h1><p>All finalized runs, newest first, including the most recent {count}. '
        'Select a run for its exact results. In test details, History links to earlier results of that test. '
        f'Trend charts show up to {count - 1} earlier and {count - 1} later launches of the same type; click a bar to open its report. '
        'Counts cover recorded tests; interrupted runs may be incomplete.</p><ol>'
        + ''.join(links) + '</ol><script>async function counts(){'
        'for(const row of document.querySelectorAll("[data-summary]")){try{'
        'const response=await fetch(row.dataset.summary,{cache:"no-store"});'
        'if(!response.ok)throw Error();row.textContent=(await response.json()).label;'
        '}catch{row.textContent="pass counts unavailable"}}}counts();</script>').encode()
    if len(body) > MAX_ARTIFACT:
        raise ViewerError('Allure archive index exceeds size limit')
    checkpoint(budget, stop)
    Path(output).write_bytes(body)
    checkpoint(budget, stop)
    return windows, routes, summaries
