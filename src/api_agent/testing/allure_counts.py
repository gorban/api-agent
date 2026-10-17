"""Logical-test archive counts from verified Allure result artifacts."""
import json
import re

from .allure_source import ViewerError


def _object(pairs):
    row = dict(pairs)
    if len(row) != len(pairs):
        raise ViewerError('Ambiguous Allure result counts')
    return row


def result_counts(manifest, files):
    groups = {}
    for name, body in files.items():
        row = json.loads(body, object_pairs_hook=_object)
        parameters = row.get('parameters', [])
        semantic, seen, attempt = [], set(), 1
        for parameter in parameters:
            key = parameter['name']
            if key in seen:
                raise ViewerError('Ambiguous Allure result parameters')
            seen.add(key)
            if key == 'attempt' and parameter.get('excluded') is True:
                value = parameter['value']
                if type(value) is not str or re.fullmatch('[1-9][0-9]{0,3}', value) is None:
                    raise ViewerError('Invalid Allure attempt counter')
                attempt = int(value)
            elif parameter.get('excluded') is not True:
                semantic.append({'name': key, 'value': parameter['value']})
        identity = json.dumps([row.get('historyId') or row['uuid'], row.get('fullName'),
                               sorted(semantic, key=lambda item: item['name'])], sort_keys=True, allow_nan=False)
        attempts = groups.setdefault(identity, {})
        if attempt in attempts:
            raise ViewerError('Ambiguous Allure attempt sequence')
        attempts[attempt] = row['status']
    first = final = 0
    uncertain = False
    expected = manifest.get('metadata', {}).get('e2e_reruns') if manifest['mode'] == 'e2e' else None
    if expected is not None and (type(expected) is not int or expected < 0):
        raise ViewerError('Invalid Allure confirmation count')
    for attempts in groups.values():
        if sorted(attempts) != list(range(1, len(attempts) + 1)):
            raise ViewerError('Incomplete Allure attempt sequence')
        statuses = list(attempts[number] for number in range(1, len(attempts) + 1))
        if manifest['mode'] == 'e2e' and expected is not None and len(statuses) > expected + 1:
            raise ViewerError('Allure attempts exceed the confirmation policy')
        first += statuses[0] == 'passed'
        if manifest['mode'] != 'e2e':
            final += statuses[-1] == 'passed'
        elif statuses[0] == 'passed':
            final += all(status == 'passed' for status in statuses)
        elif len(statuses) > 1 and expected is None:
            uncertain = True
        else:
            final += (expected is not None and expected > 0 and len(statuses) == expected + 1
                      and all(status == 'passed' for status in statuses[1:]))
    if uncertain or manifest['status'] == 'interrupted':
        final = None
    return {'first': first, 'final': final, 'total': len(groups)}


def count_label(mode, counts):
    total = counts['total']
    def fraction(value, percentage=False):
        if value is None:
            return 'unavailable'
        text = f'{value} / {total}'
        return text + f' ({100 * value / total if total else 0:.1f}%)' if percentage else text
    if mode == 'e2e':
        return f'first {fraction(counts["first"], True)} — final {fraction(counts["final"], True)}'
    return 'pass counts unavailable' if counts['final'] is None else fraction(counts['final']) + ' passed'
