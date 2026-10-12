"""Presentation of frozen historical estimates; no storage or pricing work."""
from .test_results import compact_cost


def cost_cell(prediction):
    if prediction.get('incomplete'):
        return '—'
    if prediction.get('known_zero'):
        return ''
    return compact_cost(prediction.get('estimated_cost_usd'))


def total_cell(snapshot, ids):
    return cost_cell(snapshot.total(set(ids)))


def selected_summary(snapshot, ids):
    total = snapshot.total(set(ids))
    amount = cost_cell(total)
    if amount == '—':
        label = 'not enough historical data'
    elif amount == '':
        label = 'no inference calls'
    else:
        label = '< $' + amount[1:] if amount.startswith('<') else '$' + amount
    summary = 'Selected estimate: ' + label
    if getattr(snapshot, 'sync_unavailable', False):
        summary += ' (history refresh incomplete)'
    return summary
