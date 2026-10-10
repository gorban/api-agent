"""An in-memory API key owned by one setup session."""
from api_agent.providers.contracts import ContractError, SessionClosedError


class APIKeySession:
    def __init__(self, key):
        if not isinstance(key, str) or not key.strip():
            raise ContractError("The selected LLM User must supply a nonempty API key in its password credential.")
        self._key = key
        self._closed = False

    async def __call__(self):
        if self._closed:
            raise SessionClosedError("GPT API-key session is closed.")
        return self._key

    async def aclose(self):
        self._closed = True
        self._key = None

    async def __aenter__(self):
        if self._closed:
            raise SessionClosedError("GPT API-key session is closed.")
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.aclose()
