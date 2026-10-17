"""Connect a personal account through a single local browser callback."""
import argparse
import asyncio
import math
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

from api_agent.providers.contracts import InteractionRequiredError, SessionClosedError
from api_agent.providers.sessions import wait_browser_result
from api_agent.runtime.managed_browser import _dispose
from .contracts import AuthError
from .login import LoginWorkflow, finish_login
from .firstparty import FirstPartyWorkflow, authenticate_first_party
from api_agent.providers.control import CleanupPendingError


class CallbackListener:
    """A bounded, loopback-only listener with a single accepted callback."""

    async def __aenter__(self):
        self.result = asyncio.get_running_loop().create_future()
        self.tasks = set()
        self.expected_state = None
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=4096)
        port = self.server.sockets[0].getsockname()[1]
        self.host = f"127.0.0.1:{port}"
        self.redirect_uri = f"http://{self.host}/auth/callback"
        return self

    async def __aexit__(self, *_):
        await _dispose(self._close())

    async def _close(self):
        self.server.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.server.wait_closed()
        self.result.cancel()

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                if len(raw) > 4096:
                    raise ValueError
                lines = raw.decode("ascii").split("\r\n")
                method, target, version = lines[0].split(" ")
                parsed = urlsplit(target)
                if method != "GET" or version != "HTTP/1.1" or parsed.path != "/auth/callback" or parsed.scheme or parsed.netloc or parsed.fragment:
                    raise ValueError
                if any(not line or ":" not in line for line in lines[1:-2]):
                    raise ValueError
                if re.search(r"%(?![0-9a-fA-F]{2})", parsed.query):
                    raise ValueError
                header_pairs = [line.lower().split(":", 1) for line in lines[1:-2]]
                hosts = [value.strip() for name, value in header_pairs if name == "host"]
                if hosts != [self.host]:
                    raise ValueError
                headers = dict(header_pairs)
                if "transfer-encoding" in headers or headers.get("content-length", "0").strip() != "0":
                    raise ValueError
                pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, errors="strict")
                parameters = dict(pairs)
                if len(parameters) != len(pairs) or parameters.get("state") != self.expected_state:
                    raise ValueError
                keys = set(parameters)
                if not (({"state", "code"} <= keys <= {"state", "code", "client_id", "scope"}) or ({"state", "error"} <= keys <= {"state", "error", "error_description", "error_uri"})):
                    raise ValueError
                if not all(parameters.values()) or self.result.done():
                    raise ValueError
            except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                status, body = "400 Bad Request", "Callback rejected."
            else:
                self.result.set_result(parameters)
                status, body = "200 OK", "Callback received. You may close this window."
            payload = body.encode("ascii")
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(payload)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n".encode("ascii") + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.tasks.discard(task)


class LoginPageGuard:
    """Report observed login blockers without reading page text or credentials."""
    def __init__(self, page, *, allow_interaction):
        self.page = page
        self.allow_interaction = allow_interaction
        self.notified = False

    async def check(self):
        blocked = self.page.get_by_role('heading', name=re.compile(
            r'^(Oops, an error occurred!|Performing security verification|Use your passkey|Sign in with a passkey|Use your security key|Insert your security key|Check your email|Verify your email|Check your phone|Verify your phone number|Choose a verification method|Choose how to verify|Verify your identity)$')).first
        try:
            location = urlsplit(self.page.url)
            route_challenge = (location.scheme == 'https' and location.netloc == 'auth.openai.com'
                               and location.path == '/auth_challenge/passkey')
            visible = route_challenge or await blocked.is_visible()
        except Exception:
            code = 'browser_closed' if self.page.is_closed() else 'browser_unavailable'
            raise AuthError(code) from None
        if visible:
            if not self.allow_interaction:
                raise AuthError('login_interaction_required')
            if not self.notified:
                print('GPT login needs human attention. Retry or complete sign-in in the visible browser.', flush=True)
                self.notified = True
        else:
            self.notified = False
        return visible


async def submit_login_username(session, username, *, guard):
    """Submit only a recognized username stage using the shared pinned workflow."""
    workflow = LoginWorkflow(session, username, None, "api-agent", guard,
                             allow_interaction=guard.allow_interaction)
    async def submit():
        while workflow.last_action is None:
            await workflow.advance()
            if workflow.last_action is None:
                await asyncio.sleep(.1)
    task = asyncio.create_task(submit())
    primary = None
    try:
        try:
            await wait_browser_result(task, session)
        except (asyncio.CancelledError, AuthError, CleanupPendingError):
            raise
        except SessionClosedError:
            raise AuthError('browser_closed') from None
        except Exception:
            raise AuthError("username_submission_failed") from None
    except BaseException as error:
        primary = error
        raise
    finally:
        task.cancel()
        async def drain():
            await asyncio.gather(task, return_exceptions=True)
        try:
            await _dispose(drain())
        finally:
            await finish_login(workflow, primary)


async def connect_account(store, *, registration=None, open_browser=None,
                          client=None, timeout=180, clock=time.time, profile=None, browser_owner=None,
                          app_name="api-agent", login_username=None, login_credentials=None,
                          allow_interaction=True, login_material=None):
    """Persist only a fully verified connection; clean up on every exit."""
    from .oauth import begin_authorization, finish_authorization
    if type(allow_interaction) is not bool:
        raise AuthError('invalid_interaction_policy')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 600 or not math.isfinite(timeout):
        raise AuthError("invalid_timeout")
    if login_username is not None:
        if not isinstance(login_username, str) or not login_username.strip() or open_browser is not None:
            raise AuthError("invalid_login_username")
        login_username = login_username.strip()
    if login_credentials is not None and (not callable(login_credentials) or login_username is None):
        raise AuthError('invalid_login_credentials')
    if profile is not None:
        registration = store.load(profile)
    owned = client is None
    client = httpx.AsyncClient() if owned else client
    workflow = None
    authenticator = None
    material = None
    primary = None
    try:
        try:
            from api_agent.providers.progress import report_timeout
            async with report_timeout('GPT login', timeout) as budget, asyncio.timeout(budget.remaining):
                async with CallbackListener() as listener:
                    attempt = begin_authorization(store.host_id(), listener.redirect_uri,
                                                  registration=registration, app_name=app_name)
                    listener.expected_state = attempt.state
                    session = None
                    guard = None
                    workflow = None
                    if open_browser is None:
                        if browser_owner is None:
                            from .browser import subscription_browser
                            browser_owner = subscription_browser()
                        try:
                            session = await browser_owner.start()
                            if login_material is not None:
                                from .passkeys import SoftwarePasskey, require_private_transport
                                require_private_transport()
                                material_task = asyncio.create_task(login_material())
                                try:
                                    material = await wait_browser_result(material_task, session)
                                finally:
                                    material_task.cancel()
                                    async def drain_material():
                                        await asyncio.gather(material_task, return_exceptions=True)
                                    await _dispose(drain_material())
                                if material is not None and material.passkey_available:
                                    authenticator = SoftwarePasskey(session, material.passkey,
                                                                   callback_url=listener.redirect_uri,
                                                                   first_party=True)
                                    await authenticator.prepare()
                                    guard = LoginPageGuard(session.page, allow_interaction=allow_interaction)
                                    workflow = FirstPartyWorkflow(session, login_username, login_credentials,
                                        app_name, guard, allow_interaction=allow_interaction,
                                        material=material, software_passkey=True)
                                    print('GPT selected-login passkey: signing in through ChatGPT before app authorization.', flush=True)
                                    await authenticate_first_party(workflow, session, callback=listener.result)
                                    workflow.authorizing = True
                            if not listener.result.done():
                                await session.page.goto(attempt.authorization_url, wait_until="domcontentloaded")
                        except (AuthError, InteractionRequiredError, CleanupPendingError):
                            raise
                        except asyncio.CancelledError:
                            raise
                        except SessionClosedError:
                            raise AuthError('browser_closed') from None
                        except Exception:
                            raise AuthError("browser_unavailable") from None
                        guard = LoginPageGuard(session.page, allow_interaction=allow_interaction)
                        if login_username is not None and workflow is None:
                            extra = ({'material': material, 'software_passkey': authenticator is not None}
                                     if material is not None else {})
                            workflow = LoginWorkflow(session, login_username, login_credentials,
                                                     app_name, guard, allow_interaction=allow_interaction,
                                                     **extra)
                        material = None
                    elif not open_browser(attempt.authorization_url):
                        raise AuthError("browser_unavailable")
                    if session is None:
                        parameters = await listener.result
                    else:
                        try:
                            parameters = await wait_browser_result(listener.result, session,
                                on_wait=workflow.advance if workflow is not None else guard.check if guard is not None else None)
                        except SessionClosedError:
                            raise AuthError("browser_closed") from None
                        except (AuthError, InteractionRequiredError, asyncio.CancelledError, CleanupPendingError):
                            raise
                        except Exception:
                            raise AuthError('login_step_failed') from None
                    connection = await finish_authorization(attempt, parameters, client=client, now=clock())
                    if login_username is not None and connection.email.strip().casefold() != login_username.casefold():
                        raise AuthError("account_mismatch")
                    if profile is not None and store.profile_id(connection) != profile:
                        raise AuthError("account_mismatch")
                    return await store.save_serialized(connection)
        except TimeoutError:
            raise AuthError("callback_timeout") from None
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if workflow is not None:
                await finish_login(workflow, primary)
        finally:
            try:
                try:
                    if authenticator is not None:
                        await authenticator.aclose()
                except BaseException as error:
                    browser_owner.prevent_inspection()
                    if primary is None:
                        raise
                    primary.cleanup_error = getattr(error, 'cleanup_error', error)
                    primary.retained_owners = getattr(error, 'retained_owners', (authenticator,))
                finally:
                    material = None
                    if browser_owner is not None:
                        await _dispose(browser_owner.aclose())
            finally:
                if owned:
                    await _dispose(client.aclose())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Connect a personal subscription account.")
    parser.add_argument("command", choices=("connect", "list"))
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".config/api-agent/gpt")
    parser.add_argument("--profile", help="Reconnect an existing profile")
    parser.add_argument("--timeout", type=int, choices=range(1, 601), default=180, metavar="SECONDS")
    args = parser.parse_args(argv)
    from .connections import ConnectionStore
    store = ConnectionStore(args.state_dir)
    try:
        if args.command == "list":
            for connection in store.list():
                print(f"{store.profile_id(connection)} {connection.email}")
        else:
            profile = asyncio.run(connect_account(store, timeout=args.timeout, profile=args.profile))
            connection = store.load(profile)
            print(f"Connected {connection.email} ({profile}). Plan permission: {'allowed' if 'chatgpt.tokens.use.direct' in connection.scopes else 'unavailable'}")
    except AuthError:
        parser.exit(1, "Account connection failed.\n")


if __name__ == "__main__":
    main()
