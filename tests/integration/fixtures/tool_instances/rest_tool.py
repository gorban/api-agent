"""Synthetic one-route REST fixture, admitted only by its integration graph."""
from hashlib import sha256
from urllib.parse import urljoin

import httpx

from api_agent.providers.progress import component_timeout
from api_agent.providers.tools import ToolResult, ToolSpec, ToolValidationError


class ReadOwner:
    version = 1

    def __init__(self, config):
        self.endpoint = urljoin(config['ENDPOINT'], config['PATH'])
        self.client = httpx.AsyncClient(headers=config['HEADERS'], timeout=15, trust_env=False)
        self.spec = ToolSpec('hello_read', 'Read hello with one GET request.',
            {'type': 'object', 'properties': {'method': {'const': 'GET'}},
             'required': ['method'], 'additionalProperties': False},
            {'type': 'object', 'properties': {'hello': {'type': 'string'}},
             'required': ['hello'], 'additionalProperties': False},
            sha256(b'synthetic-hello-get-v1').hexdigest())

    async def describe(self):
        return self.spec

    async def validate(self, prepared):
        if prepared.spec is not self.spec or prepared.arguments != {'method': 'GET'}:
            raise ToolValidationError('Only the declared hello GET is supported')

    async def invoke(self, prepared):
        async with component_timeout('Synthetic REST read', 15):
            await self.validate(prepared)
            response = await self.client.get(self.endpoint)
            response.raise_for_status()
            return ToolResult('success', response.json())

    async def aclose(self):
        async with component_timeout('Synthetic REST cleanup', 15):
            await self.client.aclose()


def create_tool(local_config, services):
    return ReadOwner(local_config)
