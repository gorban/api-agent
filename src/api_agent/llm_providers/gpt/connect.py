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
from .login import LoginWorkflow


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
            r'^(Oops, an error occurred!|Performing security verification)$')).first
        try:
            visible = await blocked.is_visible()
        except Exception:
            code = 'browser_closed' if self.page.is_closed() else 'browser_unavailable'
            raise AuthError(code) from None
        if visible:
            if not self.allow_interaction:
                raise AuthError('login_interaction_required')
            if not self.notified:
                print('GPT login shows an error or security-verification page. Retry or complete sign-in in the visible browser.', flush=True)
                self.notified = True


async def submit_login_username(session, username, *, guard):
    """Own submission while monitoring visible blockers and browser closure."""
    async def submit():
        await session.page.get_by_role("textbox", name="Email address", exact=True).fill(username, timeout=0)
        await session.page.get_by_role("button", name="Continue", exact=True).click(timeout=0)
    await guard.check()
    task = asyncio.create_task(submit())
    try:
        await wait_browser_result(task, session, on_wait=guard.check)
    except (asyncio.CancelledError, AuthError):
        raise
    except SessionClosedError:
        raise AuthError('browser_closed') from None
    except Exception:
        raise AuthError("username_submission_failed") from None
    finally:
        task.cancel()
        async def drain():
            await asyncio.gather(task, return_exceptions=True)
        await _dispose(drain())


async def connect_account(store, *, registration=None, open_browser=None,
                          client=None, timeout=180, clock=time.time, profile=None, browser_owner=None,
                          app_name="api-agent", login_username=None, login_credentials=None,
                          allow_interaction=True):
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
                            await session.page.goto(attempt.authorization_url, wait_until="domcontentloaded")
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            raise AuthError("browser_unavailable") from None
                        if login_username is not None:
                            guard = LoginPageGuard(session.page, allow_interaction=allow_interaction)
                            if login_credentials is None:
                                await submit_login_username(session, login_username, guard=guard)
                            else:
                                workflow = LoginWorkflow(session, login_username, login_credentials,
                                                         app_name, guard, allow_interaction=allow_interaction)
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
                        except (AuthError, InteractionRequiredError, asyncio.CancelledError):
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
    finally:
        try:
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
