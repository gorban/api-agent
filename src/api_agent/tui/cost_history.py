"""Presentation of frozen historical estimates; no storage or pricing work."""
from .test_results import compact_cost


def cost_cell(prediction):
    if prediction.get('known_zero'):
        return ''
    return compact_cost(prediction.get('estimated_cost_usd'))


def total_cell(snapshot, ids):
    total = snapshot.total(set(ids))
    value = cost_cell(total)
    return value + (' *' if total.get('incomplete') and value != '—' else '')


def selected_summary(snapshot, ids):
    total = snapshot.total(set(ids))
    amount = cost_cell(total)
    return ('Selected estimate: $' + amount if amount not in {'', '—'} else
            'Selected estimate: ' + ('no inference calls' if amount == '' else '—')) + (
                ' (incomplete; some tests unknown)' if total.get('incomplete') else '')
