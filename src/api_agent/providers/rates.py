"""Validated public text rate snapshots; initialization never authenticates."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
import math
import os
import re
from pathlib import Path
import stat
import time
from uuid import uuid4

from .contracts import ContractError

OPENAI = 'https://developers.openai.com/api/docs/pricing'
OPENROUTER = 'https://openrouter.ai/api/v1/models'
_KEYS = {'service_tier', 'input', 'cached_input', 'output', 'threshold', 'input_multiplier', 'output_multiplier'}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12


def _text(value):
    return type(value) is str and 0 < len(value) <= 200 and value.isprintable()


def validate_rate_card(card):
    if (type(card) is not dict or set(card) != {'version', 'source', 'revision', 'verified_at', 'fallback', 'currency', 'unit', 'models'}
            or type(card['version']) is not int or card['version'] != 1 or card['source'] not in (OPENAI, OPENROUTER)
            or not _text(card['revision']) or type(card['fallback']) is not bool or card['currency'] != 'USD'
            or card['unit'] != 'token' or type(card['models']) is not dict or not card['models'] or len(card['models']) > 10000):
        raise ContractError('Invalid rate snapshot')
    try:
        stamp = datetime.fromisoformat(card['verified_at'])
        if stamp.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise ContractError('Invalid rate snapshot timestamp') from None
    for model, rate in card['models'].items():
        if (not _text(model) or type(rate) is not dict or set(rate) not in (_KEYS, _KEYS | {'cache_write'}) or rate['service_tier'] != 'standard'
                or any(value is not None and not _number(value) for value in (rate['input'], rate['cached_input'], rate['output'], rate.get('cache_write')))
                or any(not _number(rate[name]) or rate[name] < 1 for name in ('input_multiplier', 'output_multiplier'))
                or (rate['threshold'] is not None and (type(rate['threshold']) is not int or rate['threshold'] <= 0))):
            raise ContractError('Invalid model rate')
    return card


def _rate(input_rate, cached, output, *, long=False, cache_write='input'):
    return dict(service_tier='standard', input=input_rate, cached_input=cached, output=output,
                cache_write=input_rate if cache_write == 'input' else cache_write,
                threshold=272000 if long else None, input_multiplier=2 if long else 1, output_multiplier=1.5 if long else 1)


def _card(source, models, raw, *, now=None, fallback=False):
    return validate_rate_card(dict(version=1, source=source, revision=sha256(raw).hexdigest(),
        verified_at=(now or datetime.now(timezone.utc)).isoformat(), fallback=fallback,
        currency='USD', unit='token', models=models))


def bundled_rate_card():
    """Reviewed Standard text fallback, official pricing page, 2026-10-11."""
    rates = {'gpt-6-luna': (.1, .01, .5, True), 'gpt-6-sol': (2, .2, 10, True),
             'gpt-6.1-sol': (2, .1, 10, True), 'gpt-4.1-nano': (.1, .025, .4, False)}
    models = {model: _rate(*(value / 1000000 for value in row[:3]), long=row[3],
                          cache_write=row[0] * (1.25 if row[3] else 1) / 1000000) for model, row in rates.items()}
    return _card(OPENAI, models, b'official-standard-cache-writes-2026-10-11', now=datetime(2026, 10, 11, tzinfo=timezone.utc), fallback=True)


USAGE_COST_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_tokens',
                     'cache_creation_tokens', 'reasoning_tokens', 'total_tokens')


def price_usage(card, model, usage):
    return price_usage_parts(card, model, usage)['total_tokens']


def price_usage_parts(card, model, usage):
    """Price disjoint input categories; cached/write/reasoning detail is inclusive."""
    parts = dict.fromkeys(USAGE_COST_FIELDS)
    validate_rate_card(card)
    rate = card['models'].get(model)
    values = usage if type(usage) is dict else vars(usage)
    from .llm import Usage
    try:
        tokens = Usage(**values)
    except (TypeError, ValueError):
        raise ContractError('Invalid priced usage') from None
    if rate is None or all(rate[name] is None for name in ('input', 'cached_input', 'output')) or tokens.input_tokens is None or tokens.output_tokens is None:
        return parts
    # Unknown cached usage cannot be assumed to be zero.
    if tokens.cached_input_tokens is None:
        return parts
    remaining = tokens.input_tokens - tokens.cached_input_tokens
    writes = tokens.cache_write_tokens
    if writes is None and remaining and rate.get('cache_write') != rate['input']:
        return parts
    counts = (remaining - (writes or 0), tokens.cached_input_tokens, writes or 0, tokens.output_tokens)
    names = ('input', 'cached_input', 'cache_write', 'output')
    if any(count and rate.get(name) is None for count, name in zip(counts, names)):
        return parts
    long = rate['threshold'] is not None and tokens.input_tokens > rate['threshold']
    factors = (rate['input_multiplier'],) * 3 + (rate['output_multiplier'],) if long else (1, 1, 1, 1)
    fresh, cached, written, output = (count * (rate.get(name) or 0) * factor
        for count, name, factor in zip(counts, names, factors))
    parts.update(input_tokens=math.fsum((fresh, cached, written)), output_tokens=output,
        cache_creation_tokens=written if writes is not None else None,
        cache_read_tokens=cached, reasoning_tokens=None if tokens.reasoning_tokens is None else
        tokens.reasoning_tokens * (rate['output'] or 0) * factors[3],
        total_tokens=math.fsum((fresh, cached, written, output)))
    return parts


def parse_openrouter(raw, *, now=None):
    data = json.loads(raw)
    if type(data) is not dict or type(data.get('data')) is not list:
        raise ContractError('Invalid pricing catalog')
    rows = data['data']
    models = {}
    for row in rows:
        if type(row) is not dict or type(row.get('pricing', {})) is not dict:
            raise ContractError('Invalid catalog pricing row')
        pricing = row.get('pricing', {})
        def amount(key):
            value = pricing.get(key)
            return None if value is None else float(value)
        if row['id'] in models:
            raise ContractError('Duplicate catalog model')
        if any(pricing.get(key) not in (None, '0', '0.0', 0) for key in ('request', 'image')):
            continue
        # Missing/zero write quotes do not establish that writes are free.
        write = amount('input_cache_write')
        models[row['id']] = _rate(amount('prompt'), amount('input_cache_read'), amount('completion'),
                                 cache_write=write if write else None)
    return _card(OPENROUTER, models, raw, now=now)


def _unpack(value):
    if type(value) is not list or not value or value[0] not in (0, 1):
        raise ContractError('Invalid packed pricing data')
    if len(value) == 1:
        return None
    if len(value) != 2:
        raise ContractError('Invalid packed pricing length')
    payload = value[1]
    if value[0] == 1:
        if type(payload) is not list:
            raise ContractError('Invalid packed pricing array')
        return [_unpack(item) for item in payload]
    return {key: _unpack(item) for key, item in payload.items()} if type(payload) is dict else payload


def _usd(value):
    if value in (None, '-'):
        return None
    if not _number(value):
        raise ContractError('Invalid public token price')
    return value / 1000000


def _context_rate(short, long, threshold):
    # Ratios must apply equally to cached/uncached input; otherwise unsupported.
    if threshold is None or any(value is None or value <= 0 for value in short + long):
        return _rate(None, None, None)
    factors = [right / left for left, right in zip(short, long)]
    if factors[0] != factors[1] or min(factors) < 1:
        return _rate(None, None, None)
    if factors[0] != factors[2]:
        return _rate(None, None, None)
    rate = _rate(short[0], short[1], short[3], cache_write=short[2])
    rate.update(threshold=threshold, input_multiplier=factors[0], output_multiplier=factors[3])
    return rate


class _PricingHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.context = {}
        self.context_explicit = set()
        self.current_models = []
        self.ambiguous = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag != 'astro-island' or attrs.get('component-export') not in ('TextTokenPricingTables', 'GroupedPricingTable'):
            return
        props = {key: _unpack(value) for key, value in json.loads(attrs['props']).items()}
        if attrs['component-export'] == 'TextTokenPricingTables':
            if props['tier'] == 'standard':
                rows = props['rows']
                if type(rows) is not list or any(type(row) is not list or len(row) not in (4, 5) for row in rows):
                    raise ContractError('Invalid Standard pricing rows')
                self.rows.extend(rows)
                self.current_models = [row[0] for row in rows]
        elif props.get('tier') in (None, 'standard') and props.get('headings') == ['Model', 'Short context input', 'Short context cached input', 'Short context cache writes', 'Short context output', 'Long context input', 'Long context cached input', 'Long context cache writes', 'Long context output']:
            groups = props['headingGroups']
            labels = json.dumps(groups, ensure_ascii=False)
            thresholds = re.findall(r'[>≤]([0-9]+)K input tokens', labels)
            threshold = int(thresholds[0]) * 1000 if len(thresholds) == 2 and thresholds[0] == thresholds[1] else None
            for group in props['groups']:
                model, rows = group['model'], group['rows']
                if type(rows) is not list or len(rows) != 1 or len(rows[0]) != 8 or not _text(model):
                    raise ContractError('Invalid context pricing rows')
                row = rows[0]
                short = [_usd(row[index]) for index in (0, 1, 2, 3)]
                long = [_usd(row[index]) for index in (4, 5, 6, 7)]
                rate = _context_rate(short, long, threshold)
                if model in self.context and self.context[model] != rate:
                    raise ContractError('Conflicting context rates')
                self.context[model] = rate
                if props.get('tier') == 'standard':
                    self.context_explicit.add(model)

    def handle_data(self, data):
        if data.strip() == 'Long context':
            self.ambiguous.update(self.current_models)

    def handle_endtag(self, tag):
        if tag == 'astro-island':
            self.current_models = []


def parse_openai(raw, *, now=None):
    parser = _PricingHTML()
    parser.feed(raw.decode('utf-8'))
    models = {}
    short_models = {}
    for row in parser.rows:
        model = row[0]
        annotated = re.fullmatch(r'(gpt-[^ ]+) \((?:<|≤)([0-9]+)K context length\)', model) if type(model) is str else None
        if annotated:
            # A short-only annotation cannot price larger input; retain unknown.
            model = annotated[1]
            rate = _rate(None, None, None)
        elif not _text(model) or ' ' in model or not model.startswith('gpt-'):
            continue
        else:
            rate = _rate(*[_usd(value) for value in row[1:3] + [row[-1]]],
                         cache_write=_usd(row[3]) if len(row) == 5 else 'input')
            if model in parser.ambiguous:
                rate = _rate(None, None, None)
        short = _rate(*[_usd(value) for value in row[1:3] + [row[-1]]],
                      cache_write=_usd(row[3]) if len(row) == 5 else 'input')
        if model in short_models and short_models[model] != short:
            raise ContractError('Conflicting Standard rates')
        short_models[model] = short
        if model in models and models[model] != rate:
            raise ContractError('Conflicting Standard rates')
        models[model] = rate
    for model, rate in parser.context.items():
        if model not in short_models and model not in parser.context_explicit:
            models[model] = _rate(None, None, None)
            continue
        if model in short_models and any(rate[name] is not None and rate[name] != short_models[model][name] for name in ('input', 'cached_input', 'cache_write', 'output')):
            raise ContractError('Grouped context rates conflict with Standard prices')
        models[model] = rate
    return _card(OPENAI, models, raw, now=now)


def compact_rate_card(card, config=None, *, model=None):
    """Keep a bounded frozen transport card and a single model per call."""
    if card is None:
        return None
    validate_rate_card(card)
    result = deepcopy(card)
    if model is not None:
        result['models'] = {model: deepcopy(card['models'].get(model, _rate(None, None, None)))}
        return result
    import regex
    config = config or {}
    preferred = []
    deadline = time.monotonic() + .2
    try:
        patterns = [regex.compile(config.get(key, '(?!)')) for key in ('LLM_DEFAULT_MODEL', 'LLM_ALLOWED_MODELS')]
        for pattern in patterns:
            for name in sorted(card['models']):
                if time.monotonic() >= deadline:
                    raise TimeoutError('Pricing selection budget')
                if pattern.fullmatch(name, timeout=.002):
                    preferred.append(name)
    except (regex.error, TypeError, TimeoutError):
        pass
    ordered = dict.fromkeys(preferred + sorted(card['models']))
    models = {}
    size = len(json.dumps({**result, 'models': {}}, allow_nan=False).encode())
    for name in ordered:
        row = card['models'][name]
        extra = len(json.dumps({name: row}, allow_nan=False).encode()) + 2
        if size + extra > 262144:
            continue
        size += extra
        models[name] = row
    result['models'] = models
    return validate_rate_card(result)


def _source(config):
    from api_agent.llm_providers.gpt import factory
    selected = config.get('LLM_PROVIDER')
    if selected != str(Path(factory.__file__).resolve()) + ':factory':
        return None
    endpoint = config.get('GPT_BASE_URL', 'https://api.openai.com/v1')
    if config.get('GPT_AUTH_MODE', 'subscription') == 'subscription' or endpoint == 'https://api.openai.com/v1':
        return OPENAI
    return OPENROUTER if endpoint == 'https://openrouter.ai/api/v1' else None


def _cache_path(context, source):
    root = getattr(context, 'owner_root', None)
    return None if root is None else Path(root) / '.api-agent' / 'pricing' / (sha256(source.encode()).hexdigest() + '.json')


def _read_cache(path):
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            raw = stream.read(2097153)
        if len(raw) > 2097152:
            return None
        data = json.loads(raw)
        if type(data) is not dict or set(data) != {'card', 'etag', 'last_modified'}:
            return None
        validate_rate_card(data['card'])
        for name in ('etag', 'last_modified'):
            if data[name] is not None and (not _text(data[name]) or '\r' in data[name] or '\n' in data[name]):
                return None
        return data
    except (OSError, ValueError, TypeError, ContractError, RecursionError):
        return None


def cached_rate_card(context=None, config=None):
    """Picker lookup is entirely local and returns an independent snapshot."""
    config = config or {}
    source = _source(config)
    if source is None:
        return None
    card = config.get('_COST_RATE_CARD')
    if card is not None:
        try:
            validate_rate_card(card)
            if card['source'] == source:
                return compact_rate_card(card, config)
        except ContractError:
            pass
    cached = _read_cache(_cache_path(context, source))
    if cached is not None and cached['card']['source'] == source:
        return compact_rate_card(cached['card'], config)
    return bundled_rate_card() if source == OPENAI else None


def freeze_pricing(context, config):
    """One bounded unauthenticated public refresh; failure retains validated rates."""
    import httpx
    from .progress import report_timeout_sync
    source = _source(config)
    if source is None:
        return None
    fallback = cached_rate_card(context, config)
    path = _cache_path(context, source)
    cached = _read_cache(path)
    headers = {'Accept-Encoding': 'identity'}
    if cached is not None:
        for name, header in (('etag', 'If-None-Match'), ('last_modified', 'If-Modified-Since')):
            if cached[name] is not None:
                headers[header] = cached[name]
    try:
        with report_timeout_sync('Public text pricing', 5) as budget:
            deadline = time.monotonic() + budget.remaining
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=budget.remaining) as client:
                with client.stream('GET', source, headers=headers) as response:
                    if response.status_code == 304 and cached is not None:
                        return compact_rate_card(cached['card'], config)
                    response.raise_for_status()
                    if response.headers.get('content-encoding', '').lower() not in ('', 'identity'):
                        raise ValueError('Unsupported encoding')
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline:
                            raise TimeoutError('Pricing deadline')
                        if len(raw) + len(chunk) > 2097152:
                            raise ValueError('Pricing limit')
                        raw.extend(chunk)
                    validators = {name: response.headers.get(header) for name, header in
                                  (('etag', 'etag'), ('last_modified', 'last-modified'))}
            card = (parse_openrouter if source == OPENROUTER else parse_openai)(bytes(raw))
            if path is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
                    with temporary.open('x', encoding='utf-8') as stream:
                        stream.write(json.dumps({'card': card, **validators}, allow_nan=False))
                    temporary.replace(path)
                except OSError:
                    pass
            return compact_rate_card(card, config)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, ContractError, TimeoutError, IndexError, AttributeError, RecursionError):
        return fallback
