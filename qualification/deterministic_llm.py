"""Explicit, no-network provider used only by the qualification image."""
import asyncio
from pathlib import Path

from api_agent.providers.contracts import ProviderAccessError, ProviderCapabilities
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, StreamEvent, TextContent, Usage


class DeterministicProvider:
    credential_protection_version = 1
    version = 1
    inference_timeout = 5
    capabilities = ProviderCapabilities(
        kind='llm', operations=('describe', 'complete', 'stream'),
        features=('max_output_tokens',),
    )

    def __init__(self, config):
        self.state = Path(config.get('API_AGENT_STATE_DIR', '/var/lib/api-agent'))
        self.authority = None
        self.closed = False

    def bind_credential_authority(self, authority):
        self.authority = authority

    async def asetup(self, *, interactive, user_index, credentials):
        return _Session()

    async def adescribe(self, *, auth=None):
        return (AvailableSelection('deterministic', features=('max_output_tokens',),
                                   max_output_tokens=200000),)

    async def acomplete(self, request, *, auth=None):
        text = ''.join(part.text for message in request.payload.messages
                       for part in message.content if isinstance(part, TextContent))
        if 'controlled-error' in text:
            raise ProviderAccessError('invalid_request', 'controlled deterministic failure')
        if 'block-until-cancelled' in text:
            await asyncio.Event().wait()
        return LLMResult(Message('assistant', (TextContent('deterministic response'),)),
                         Usage(1, 1), actual_model='deterministic')

    async def astream(self, request, *, auth=None):
        result = await self.acomplete(request, auth=auth)
        yield StreamEvent(TextContent('deterministic response'))
        yield StreamEvent(result, 'completed')

    async def aread_usage(self, *, auth=None):
        return ()

    async def aclose(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'deterministic-provider-closed').write_text('closed\n')
        self.closed = True


def create(config, services):
    return DeterministicProvider(config)


class _Session:
    async def __call__(self):
        return 'deterministic-qualification-auth'

    async def aclose(self):
        return None
