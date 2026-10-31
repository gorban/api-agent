"""Per-call admission for opaque Responses state, owned by the provider session."""
from dataclasses import replace
import hmac
import json
import re

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.rich_inference import continuation


class _FrozenWorkerAuth:
    """Borrow one admitted credential snapshot without owning its session."""
    def __init__(self, values):
        self._values = values

    def __repr__(self):
        return '<FrozenBedrockWorkerAuth>'

    def worker_config(self):
        return dict(self._values)


class NativeScope:
    def __init__(self, key, request, endpoint, api_mode):
        self.key = key
        self.request = request
        self.route = (endpoint, api_mode, request.selected_llm)
        self.identity = None

    def wrap_auth(self, auth):
        async def admitted():
            owner = await auth()
            values = dict(owner.worker_config())
            if any(type(key) is not str or value is not None and type(value) is not str
                   for key, value in values.items()):
                raise ContractError('Invalid Bedrock credential snapshot.')
            mode, region = values.get('mode'), values.get('region')
            if (mode in ('api_key', 'access_key', 'temporary') and type(region) is str
                    and re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', region)):
                self.identity = hmac.digest(self.key, json.dumps(values, sort_keys=True,
                    separators=(',', ':'), allow_nan=False).encode(), 'sha256').hex()
            for message in self.request.payload.messages:
                if message.continuation is not None:
                    envelope = continuation(message.continuation)
                    self._ready()
                    if not hmac.compare_digest(envelope['provider'], self._provider(envelope['state'])):
                        raise ProviderAccessError('invalid_request',
                            'Bedrock continuation belongs to different credentials, model, route or provider owner.')
            return _FrozenWorkerAuth(values)
        return admitted

    def _ready(self):
        if self.identity is None:
            raise ProviderAccessError('unsupported',
                'Bedrock native continuation requires frozen credentials and an explicit resolved region; '
                'use a session adapter that supplies a verified credential snapshot.')

    def _provider(self, state):
        body = json.dumps([self.route, self.identity, state], sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode()
        return 'bedrock.responses.' + hmac.digest(self.key, body, 'sha256').hex()

    def seal(self, result):
        value = result.payload.continuation
        if value is None:
            return result
        try:
            self._ready()
            envelope = continuation(value)
            if envelope['provider'] != 'bedrock.responses':
                raise ProviderAccessError('unknown', 'Invalid Bedrock native continuation publication.')
        except (ProviderAccessError, ContractError) as error:
            error.usage = result.usage
            error.execution = 'possibly_executed'
            raise
        envelope['provider'] = self._provider(envelope['state'])
        return replace(result, payload=replace(result.payload,
            continuation=json.dumps(envelope, separators=(',', ':'), ensure_ascii=True)))
