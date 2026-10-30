"""Backend-local operation authentication owner; no worker IPC transport.

A backend must route all consumers for the selected identity to this instance.
Only readiness and bounded catalog metadata are consumer results. Provider
sessions, credential readers and browsers remain inside the creating process.
"""
import asyncio
import math
import time
from uuid import uuid4

from .contracts import ContractError, SessionClosedError
from .sessions import _Owner, _drain, _spawn
from .control import OperationalError, CleanupPendingError, finish_cleanup
from .progress import independent_timeout_scope


class RevocableCredentials(_Owner):
    """Own asynchronous reads and reject replies after operation revocation.

    The supplied reader must already enforce the selected role/account/kinds.
    Cancellation does not prove a helper stopped; surviving work stays owned.
    """
    def __init__(self, operation_id, reader, *, timeout=5):
        super().__init__(timeout)
        if not isinstance(operation_id, str) or not operation_id or not callable(reader):
            raise ContractError('Invalid operation credential binding')
        self.operation_id = operation_id
        self._reader = reader
        self._reads = set()

    def revoke(self):
        self._context()
        self._closed = True

    async def __call__(self, *args, **kwargs):
        return await self._read(self._reader, *args, **kwargs)

    async def login_credentials(self, *args, **kwargs):
        self._check()
        reader = getattr(self._reader, 'login_credentials', None)
        if reader is None:
            return None
        return await self._read(reader, *args, **kwargs)

    async def _read(self, reader, *args, **kwargs):
        self._check()
        try:
            task = _spawn(reader(*args, **kwargs))
            self._reads.add(task)
            task.add_done_callback(self._reads.discard)
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if self._closed and not asyncio.current_task().cancelling():
                raise SessionClosedError('Credential binding is revoked') from None
            raise
        except (ContractError, SessionClosedError, OperationalError, CleanupPendingError):
            raise
        except Exception:
            raise OperationalError("unknown") from None
        self._check()
        return result

    async def _dispose(self):
        await _drain(self._reads, self._timeout, self._cancel_requested)


class SharedSetupOwner(_Owner):
    """One lazy owner for one operation's selected provider/account identity.

    ``owner_factory(credentials)`` synchronously creates an LLMSessionOwner
    (or an equivalent owner with async call/aclose). It must not acquire resources
    before returning ownership. Concurrent checkpoints share a task, including
    the provider's cache/refresh path. A cancelled waiter never cancels setup.
    Failed authentication checkpoints remain failed for this operation. Catalog
    failures can be retrieved again without replacing the provider or identity.
    This is deliberately not a cross-process registry.

    Wrapped completions may explicitly preserve provider-owned payload capture;
    None retains marker discovery on an ordinary bound completion method.
    """
    def __init__(self, operation_id, selected_identity, owner_factory, *, credentials=None, describe=None, complete=None, captures_effective_payload=None, validate_request=None, timeout=5, catalog_ttl=3600, max_attempts=5, inference_timeout=None, withhold_inference_content=False):
        super().__init__(timeout)
        if type(catalog_ttl) not in (int, float):
            raise ContractError('Invalid shared setup binding')
        try:
            catalog_ttl = float(catalog_ttl)
        except OverflowError:
            raise ContractError('Invalid shared setup binding') from None
        if (not isinstance(operation_id, str) or not operation_id
                or not isinstance(selected_identity, str) or not selected_identity
                or not callable(owner_factory)
                or (captures_effective_payload is not None and type(captures_effective_payload) is not bool)
                or not math.isfinite(catalog_ttl) or catalog_ttl < 0):
            raise ContractError('Invalid shared setup binding')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 20:
            raise ContractError('Invalid inference attempt limit')
        if inference_timeout is not None and (type(inference_timeout) not in (int, float) or not math.isfinite(inference_timeout) or inference_timeout <= 0):
            raise ContractError('Invalid inference timeout')
        self._max_attempts = max_attempts
        self._inference_timeout = inference_timeout
        self.operation_id = operation_id
        self._selected_identity = selected_identity
        self._factory = owner_factory
        self._credentials = (RevocableCredentials(operation_id, credentials, timeout=timeout)
                             if credentials is not None else None)
        self._owner = None
        self._ready = None
        self._disposal_started = False
        self._describe = describe
        self._catalog = None
        self._catalog_expires_at = 0.0
        self._catalog_ttl = catalog_ttl
        self._complete = complete
        self._captures_effective_payload = captures_effective_payload
        self._withhold_inference_content = withhold_inference_content is True
        self._validate_request = validate_request
        self._calls = set()

    @property
    def selected_identity(self):
        return self._selected_identity

    async def _checkpoint(self):
        if self._owner is None:
            try:
                owner = self._factory(self._credentials)
                if not callable(getattr(owner, 'call', None)) or not callable(getattr(owner, 'aclose', None)):
                    raise ContractError('Invalid shared session owner')
                self._owner = owner
            except ContractError:
                raise
            except Exception:
                raise OperationalError("unknown") from None
        self._check()

        async def ready(*, auth):
            if auth is None:
                raise ContractError('Provider did not provide authentication')
            self._check()
        await self._owner.call(ready)
        self._check()

    async def ensure_ready(self, *, selected_identity=None):
        self._check()
        if selected_identity is not None and selected_identity != self._selected_identity:
            raise ContractError("Selected identity is outside shared binding")
        # Success may be checked again to let the one provider owner refresh.
        # Failures are sticky so multiple waiters cannot initiate auth replay.
        if self._ready is None or (self._ready.done() and not self._ready.cancelled()
                                   and self._ready.exception() is None):
            with independent_timeout_scope():
                self._ready = _spawn(self._checkpoint())
        await asyncio.shield(self._ready)
        self._check()

    async def describe(self, *, selected_identity=None):
        await self.ensure_ready(selected_identity=selected_identity)
        if not callable(self._describe):
            from .contracts import SetupPrerequisiteError
            raise SetupPrerequisiteError('The selected LLM provider has no catalog capability.')
        if self._catalog is None or (self._catalog.done() and time.monotonic() >= self._catalog_expires_at):
            with independent_timeout_scope():
                self._catalog = _spawn(self._validated_catalog())
                self._catalog.add_done_callback(self._settle_catalog)
        task = self._catalog
        result = await asyncio.shield(task)
        self._check()
        return result

    def _settle_catalog(self, task):
        if self._catalog is task and (task.cancelled() or task.exception() is not None or not task.result()):
            self._catalog = None

    async def _validated_catalog(self):
        result = await self._owner.call(self._describe)
        from .catalog import catalog_to_wire
        catalog_to_wire(result)
        self._catalog_expires_at = time.monotonic() + self._catalog_ttl
        return result

    async def complete(self, request, *, selected_identity=None):
        from api_agent.workflows.inference_capture import bind_response_dispatch
        with bind_response_dispatch() as dispatch:
            try:
                self._check()
                task = _spawn(self._complete_request(request, selected_identity=selected_identity))
                self._calls.add(task)
                try:
                    result = await asyncio.shield(task)
                    self._check()
                    return result
                finally:
                    try:
                        await finish_cleanup(_drain([task], self._timeout, self._cancel_requested))
                    finally:
                        if task.done():
                            self._calls.discard(task)
                            self._cancel_requested.discard(task)
            except BaseException as error:
                from api_agent.workflows.inference_capture import retain_response_failure
                retain_response_failure(error, dispatch['response'], dispatch['usage'])
                raise

    async def _complete_request(self, request, *, selected_identity=None):
        from .inference import async_request_to_wire, result_to_wire
        request_wire = await async_request_to_wire(request)
        self._check()
        if self._validate_request is not None:
            self._validate_request(request)
        await self.ensure_ready(selected_identity=selected_identity)
        if not callable(self._complete):
            from .contracts import SetupPrerequisiteError
            raise SetupPrerequisiteError('The selected LLM provider has no completion capability.')
        selection = None
        maximum = request.payload.generation.max_output_tokens
        if maximum is not None or any(message.cache_boundary is not None for message in request.payload.messages):
            if not callable(self._describe) and maximum is not None:
                raise OperationalError('unsupported')
            if callable(self._describe):
                from .rich_inference import resolve_catalog_selection
                selection = await resolve_catalog_selection(lambda: self.describe(selected_identity=selected_identity), request)
        async def invoke(*, auth):
            from api_agent.workflows.inference_capture import capture_inference, bind_inference_call, prepare_capture_auth
            await prepare_capture_auth(auth)
            cause_id = str(uuid4())
            with bind_inference_call(cause_id):
                effective_capture = self._captures_effective_payload
                if effective_capture is None:
                    implementation = getattr(self._complete, '__self__', None)
                    effective_capture = getattr(implementation, 'captures_effective_payload', False) is True
                from .inference_retry import retryable, retry_delay, retry_hint_supported
                from .generation_attempts import begin_transport_attempt, finish_transport_attempt
                from .progress import component_timeout
                async with component_timeout('LLM inference', self._inference_timeout) as budget:
                    index = 1
                    while True:
                        token = begin_transport_attempt(index)
                        attempt_dispatch = None
                        if not effective_capture:
                            capture_inference('input', request_wire, cause_id, auth=auth, fallback=True)
                        try:
                            from api_agent.workflows.inference_capture import isolated_response_dispatch, promote_response_dispatch
                            with isolated_response_dispatch() as attempt_dispatch:
                                result = await self._complete(request, auth=auth)
                            promote_response_dispatch(attempt_dispatch)
                        except BaseException as error:
                            conflict = None
                            if attempt_dispatch is not None:
                                from api_agent.workflows.inference_capture import retain_response_failure
                                from .contracts import ContractError
                                try:
                                    retain_response_failure(error, attempt_dispatch['response'], attempt_dispatch['usage'])
                                except ContractError as rejected:
                                    conflict = rejected
                                    from .inference import error_usage
                                    if error_usage(error) is None and error_usage(conflict) is not None:
                                        error.usage = conflict.usage
                            delay = (retry_delay(index, getattr(error, 'retry_after', None))
                                     if (conflict is None and index < self._max_attempts and retryable(error)
                                         and retry_hint_supported(getattr(error, 'retry_after', None))) else None)
                            remaining = budget.remaining
                            if delay is not None and remaining is not None and remaining <= delay:
                                delay = None
                            finish_transport_attempt(token, error=error, retry_delay_seconds=delay,
                                                     response=attempt_dispatch['response'] if attempt_dispatch is not None else None)
                            if delay is None:
                                if attempt_dispatch is not None:
                                    promote_response_dispatch(attempt_dispatch)
                                from api_agent.workflows.inference_capture import record_response_failure
                                record_response_failure(conflict or error)
                                if conflict is not None:
                                    raise conflict from error
                                raise
                            await asyncio.sleep(delay)
                            index += 1
                        else:
                            capture_inference('output', result_to_wire(result), cause_id, auth=auth, fallback=True)
                            finish_transport_attempt(token, result=result, response=attempt_dispatch['response'])
                            break
                self._check()
                from .rich_inference import async_validate_result
                await async_validate_result(request, result, selection=selection)
                return result
        from api_agent.workflows.inference_capture import withholding_inference_content
        with withholding_inference_content(self._withhold_inference_content):
            return await self._owner.call(invoke)

    async def aclose(self):
        self._context()
        if self._credentials is not None:
            self._credentials.revoke()
        await super().aclose()

    async def _stop_tasks(self):
        await _drain(self._calls, self._timeout, self._cancel_requested)
        await _drain([task for task in (self._ready, self._catalog) if task is not None],
                     self._timeout, self._cancel_requested)

    async def settle_execution(self):
        """Drain shielded checkpoints before the session's final measurements."""
        self._context()
        if self._credentials is not None:
            self._credentials.revoke()
        await self._stop_tasks()
        if self._owner is not None:
            await self._owner.settle_execution()
        if self._credentials is not None:
            await self._credentials.aclose()

    async def _dispose(self):
        # Equivalent third-party session owners need only call/aclose. Their
        # aclose below owns any nested setup; prepublication uses our session.
        await self._stop_tasks()
        if not self._disposal_started:
            self._disposal_started = True
            # Reverse release order keeps credential helpers until auth stops.
            if self._credentials is not None:
                self._resources.append((self._credentials, 'aclose'))
            if self._owner is not None:
                self._resources.append((self._owner, 'aclose'))
        await self._release()
