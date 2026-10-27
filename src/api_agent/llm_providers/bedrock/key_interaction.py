"""Operation-owned opaque keys through the ordinary private input screen."""
from types import SimpleNamespace

from api_agent.providers.contracts import InteractionRequiredError
from api_agent.providers.control import finish_cleanup
from api_agent.providers.progress import component_timeout
from .login_interaction import LoginInteraction, _LIMIT


class KeyInteraction(LoginInteraction):
    _label = 'Bedrock credentials'
    _message = 'Bedrock credential interaction is unavailable.'

    def __repr__(self):
        return '<KeyInteraction>'

    async def read(self):
        self._live()
        reply = await self._call(lambda: self._channel.read(0.1))
        if reply is None:
            return None
        if (getattr(reply, 'action', None) != 'input'
                or type(getattr(reply, 'value', None)) is not str or len(reply.value) > _LIMIT):
            raise InteractionRequiredError(self._message)
        return reply.value


async def collect_keys(mode, owner):
    """Borrow a private owner; retain it explicitly if disposal needs retry."""
    interaction = KeyInteraction(SimpleNamespace(interactive=True, interaction=owner))
    values = {}
    try:
        async with component_timeout('Bedrock private key input', 60):
            await interaction.open()
            prompts = (('api_key', 'Mantle bearer API key:'),) if mode == 'api_key' else (
                ('access_key', 'Access-key ID:'), ('secret_key', 'Secret access key:'))
            for name, prompt in prompts:
                await interaction.check_ready()
                await interaction.write(prompt + '\n')
                value = None
                while value is None:
                    await interaction.check_ready()
                    value = await interaction.read()
                values[name] = value
            return values
    finally:
        try:
            await finish_cleanup(interaction.aclose())
        except BaseException as error:
            # The provider factory adopts retained owners even during cancellation.
            error.retained_owners = (interaction,)
            raise
