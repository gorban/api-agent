"""Role-scoped credential binding shared by consuming provider operations."""
import asyncio

from api_agent.providers.contracts import ContractError, SetupPrerequisiteError, UnattendedCredentialError
from api_agent.providers.sessions import ScopedRoleCredentials


def bind_role_credentials(config, roles, index, *, registry, emissions, pending_tasks,
                          interaction_owner=None, index_source=None):
    """Validate every required descriptor before launch; retrieve secrets lazily."""
    from .profiles import normalize_auth, parse_profiles, validate_role_entries
    from . import credentials as helpers
    try:
        canonical = normalize_auth(config.get('UNATTENDED_AUTH', {}), registry=registry)
        sources = {}
        for role in roles:
            entries = canonical['roles'].get(role, [])
            validate_role_entries(role, entries)
            if not entries:
                raise SetupPrerequisiteError(f'{role}: configure an account in unattended authentication setup.')
            if not canonical['password_manager']['manager']:
                raise SetupPrerequisiteError('Configure the shared password manager before unattended authentication.')
            profiles = parse_profiles({'password_manager':canonical['password_manager'], 'roles':{role:entries}},
                                      (role,), registry=registry)
            if type(index) is not int or index < 0 or index >= len(profiles.for_role(role)):
                raise ValueError
            sources[role] = profiles.for_role(role)[index]
    except SetupPrerequisiteError:
        raise
    except (ValueError, TypeError):
        raise ContractError('The selected unattended login descriptor is incomplete or invalid.') from None

    async def retrieve(selected_role, selected_index, kind, **options):
        helper = {'username': helpers.get_username, 'password': helpers.get_password,
                  'totp': helpers.get_totp, 'login': helpers.get_login_credentials}[kind]
        interaction = None if interaction_owner is None else interaction_owner.fork()
        try:
            from api_agent.providers.progress import report_timeout
            async with report_timeout('Password-manager ' + kind, None) as budget:
                def call_helper():
                    # A queued worker must not restart the captured budget.
                    remaining = budget.remaining
                    timeout = helpers.DEFAULT_TIMEOUT if remaining is None else min(helpers.DEFAULT_TIMEOUT, remaining)
                    return helper(sources[selected_role], timeout=timeout,
                                  interaction=interaction, recovery_timeout=remaining, **options)
                task = asyncio.create_task(asyncio.to_thread(call_helper))
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)
                try:
                    value = await asyncio.shield(task)
                    from api_agent.telemetry.privacy import register_secret_async
                    if type(value) is str and value:
                        await register_secret_async(emissions, value)
                    elif kind == 'login':
                        for secret in value.secret_values():
                            await register_secret_async(emissions, secret)
                    return value
                except asyncio.CancelledError:
                    if interaction is not None:
                        interaction.cancel()
                    from api_agent.runtime.tasks import settle
                    await settle(asyncio.gather(task, return_exceptions=True))
                    raise
        except helpers.CredentialCapabilityUnsupportedError:
            return None
        except (helpers.CredentialLoginRequiredError, helpers.CredentialRecoveryError) as error:
            raise UnattendedCredentialError(str(error),
                index_source=index_source) from None
        except helpers.CredentialError:
            raise UnattendedCredentialError(
                "Password-manager " + kind + " retrieval is unavailable. Sign in to the configured CLI and check the login entry.",
                index_source=index_source) from None
        finally:
            if interaction is not None:
                interaction.close()
    async def retrieve_login(selected_role, selected_index, **options):
        return await retrieve(selected_role, selected_index, 'login', **options)
    return ScopedRoleCredentials(roles, index, retrieve, retrieve_login=retrieve_login)
