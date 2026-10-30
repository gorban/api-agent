"""Ordinary provider factory with lazy, explicitly owned Bedrock operations."""
from contextlib import aclosing
from dataclasses import replace
import asyncio
from api_agent.providers.contracts import ContractError, ProviderCapabilities
from api_agent.providers.llm import StreamEvent
from api_agent.providers.rich_inference import async_validate_result
from api_agent.providers.progress import component_timeout, independent_timeout_scope
from api_agent.providers.control import finish_cleanup, OperationalError
from api_agent.telemetry.accounting import compatibility
from . import auth, catalog, pricing
from .sdk import SDKTransport
from .worker import DEFAULT_INFERENCE_TIMEOUT, inference_timeout


class BedrockProvider:
    credential_protection_version = 1
    captures_effective_payload = True
    version = 1
    capabilities = ProviderCapabilities(kind='llm', operations=('describe', 'complete', 'stream'),
                                        features=catalog.FEATURES)

    def __init__(self, config, services=None):
        self.config = dict(config)
        self.services = dict(services or {})
        self.api_mode = auth.validate_config(self.config)
        self.cost_compatibility = compatibility(config)
        self.rate_card = pricing.bundled_rate_card(self.config)
        try:
            self.inference_timeout = inference_timeout(self.config.get(
                'BEDROCK_INFERENCE_TIMEOUT_SECONDS', DEFAULT_INFERENCE_TIMEOUT))
        except ValueError:
            raise ContractError('Invalid Bedrock inference timeout') from None
        self.transport = SDKTransport(timeout=self.inference_timeout)
        self.sessions = []
        self.closed = False
        self._setups = set()
        self._closing = None
        self._session_closes = {}
        self.emissions = self.services.get("emissions")
        self._credential_setup_started = False

    def bind_credential_authority(self, authority):
        from api_agent.telemetry.privacy import EmissionBoundary
        if not isinstance(authority, EmissionBoundary):
            raise ContractError('Bedrock credential authority unavailable.')
        if self.emissions is not None and (not isinstance(self.emissions, EmissionBoundary)
                or self.emissions._authority is not authority._authority):
            raise ContractError('Bedrock credential authority cannot be replaced.')
        if self.emissions is None and self._credential_setup_started:
            raise ContractError('Bedrock credential authority must be bound before setup.')
        self.emissions = authority

    def _join_active_authority(self):
        from api_agent.workflows.inference_capture import active_inference_boundary
        boundary = active_inference_boundary()
        if boundary is not None:
            self.bind_credential_authority(boundary)

    def _ensure_authority(self):
        if self.emissions is None:
            from api_agent.telemetry.privacy import EmissionBoundary
            self.emissions = EmissionBoundary()

    async def _join_credentials(self, values):
        from api_agent.telemetry.privacy import EmissionBoundary
        if not isinstance(self.emissions, EmissionBoundary):
            raise ContractError('Bedrock credential authority unavailable.')
        from api_agent.providers.call_authority import current_call_authority
        call_authority = current_call_authority()
        if call_authority is not None:
            await call_authority.register(self.emissions, values)
            return call_authority.acquire(self.emissions)
        from api_agent.telemetry.privacy import register_secret_async
        for value in values:
            await register_secret_async(self.emissions, value)
        lease = self.emissions.begin_evidence_lease()
        try:
            _, snapshot = self.emissions.snapshot_content()
            return sorted(snapshot._secrets), lease
        except BaseException:
            lease.release()
            raise

    def _open(self):
        if self.closed:
            raise ContractError('Bedrock provider is closed.')

    async def asetup(self, *, interactive, user_index, credentials):
        self._join_active_authority()
        self._ensure_authority()
        self._open()
        self._credential_setup_started = True

        async def setup():
            async with component_timeout('Bedrock provider setup', 60):
                self._open()
                try:
                    session = await auth.create_session(self.config, interactive=interactive,
                        user_index=user_index, credentials=credentials, services=self.services)
                except BaseException as error:
                    self.sessions.extend(getattr(error, 'retained_owners', ()))
                    raise
                self.sessions.append(session)
                self._open()
                if asyncio.current_task().cancelling():
                    await self._close_session(session)
                    raise asyncio.CancelledError
                return session

        # Register an owned child before the first suspension, never the caller.
        with independent_timeout_scope():
            task = asyncio.create_task(setup())
        self._setups.add(task)
        try:
            session = await asyncio.shield(task)
            self._open()
            return session
        except asyncio.CancelledError:
            if not task.cancelling():
                task.cancel()
            async def drain_setup():
                results = await asyncio.gather(task, return_exceptions=True)
                if not isinstance(results[0], BaseException):
                    await self._close_session(results[0])
            await finish_cleanup(drain_setup())
            raise
        finally:
            self._setups.discard(task)

    async def adescribe(self, *, auth=None):
        self._open()
        async with component_timeout('Bedrock regional catalog', 60):
            return catalog.catalog(await self.transport.call('models', {}, auth=auth), self.api_mode)

    def validate_request(self, request):
        self._open()
        from .mantle import validate_request
        from .response_policy import prepare_response
        prepared, _ = prepare_response(request, self.api_mode)
        catalog.selection(prepared.selected_llm, self.api_mode).validate(prepared)
        validate_request(prepared)

    async def acomplete(self, request, *, auth=None):
        from api_agent.providers.call_authority import require_remote_admission
        require_remote_admission(self.emissions)
        self._join_active_authority()
        self._ensure_authority()
        self.validate_request(request)
        from .mantle import encode, decode, RESPONSE_CODES
        from .response_policy import prepare_response
        from api_agent.llm_providers.response_policy import capture_payload, format_payload
        from api_agent.workflows.inference_capture import record_response_dispatch, record_response_failure
        from api_agent.providers.inference import error_usage
        prepared, evidence = prepare_response(request, self.api_mode)
        failure_usage = None
        dispatched = False
        try:
            async with component_timeout('Bedrock completion', self.inference_timeout) as budget:
                payload = {'prefix': catalog.route(request.selected_llm, self.api_mode),
                           'body': format_payload(request, encode(prepared, self.api_mode), evidence)}
                if budget.remaining == 0:
                    raise TimeoutError('Bedrock deadline expired before dispatch.')
                def dispatch():
                    nonlocal dispatched
                    capture_payload(request, payload['body'], evidence, auth=auth)
                    if budget.remaining == 0:
                        raise TimeoutError('Bedrock deadline expired before dispatch.')
                    if evidence is not None:
                        record_response_dispatch(evidence)
                    dispatched = True
                try:
                    response = await self.transport.call(self.api_mode, payload, auth=auth,
                        join=self._join_credentials, on_dispatch=dispatch)
                    from api_agent.workflows.inference_capture import capture_inference
                    result = replace(decode(response, self.api_mode, selected_llm=request.selected_llm), response=evidence)
                    capture_inference('output', {'provider_response': response}, request.request_id, auth=auth,
                                      decoded_visible_text_characters=sum(len(part.text) for part in result.payload.content))
                    from api_agent.providers.generation_attempts import snapshot_transport_result
                    snapshot_transport_result(result)
                    return await async_validate_result(request, result)
                except (OperationalError, TimeoutError, asyncio.CancelledError) as error:
                    failure_usage = error_usage(error)
                    raw_response = getattr(error, 'provider_response', None)
                    if type(raw_response) is dict:
                        from api_agent.workflows.inference_capture import capture_inference
                        capture_inference('output', {'provider_response': raw_response}, request.request_id, auth=auth,
                                          rejected=getattr(error, 'code', None) in RESPONSE_CODES)
                    elif getattr(error, 'code', None) in RESPONSE_CODES:
                        from api_agent.workflows.inference_capture import capture_inference
                        capture_inference('output', {}, request.request_id, auth=auth, unavailable=True)
                    record_response_failure(error)
                    raise
        except (OperationalError, TimeoutError, asyncio.CancelledError) as error:
            error.response = evidence if dispatched else None
            retained = error_usage(error) or failure_usage
            if retained is not None:
                error.usage = retained
            raise

    async def astream(self, request, *, auth=None):
        from api_agent.providers.call_authority import require_remote_admission
        require_remote_admission(self.emissions)
        self._join_active_authority()
        self._ensure_authority()
        self.validate_request(request)
        from .mantle import encode
        from .normalized_stream import ParentStream
        from .response_policy import prepare_response
        from api_agent.llm_providers.response_policy import capture_payload, format_payload
        from api_agent.workflows.inference_capture import record_response_dispatch, record_response_failure
        from api_agent.providers.inference import error_usage
        prepared, evidence = prepare_response(request, self.api_mode)
        failure_usage = None
        dispatched = False
        try:
            decoder = ParentStream(self.api_mode, selected_llm=request.selected_llm)
            async with component_timeout('Bedrock streaming completion', self.inference_timeout) as budget:
                payload = {'prefix': catalog.route(request.selected_llm, self.api_mode),
                           'body': format_payload(request, encode(prepared, self.api_mode), evidence)}
                def dispatch():
                    nonlocal dispatched
                    final_body = dict(payload['body'], stream=True)
                    if self.api_mode == 'completions':
                        final_body['stream_options'] = {'include_usage': True}
                    else:
                        final_body['store'] = False
                    capture_payload(request, final_body, evidence, auth=auth)
                    if budget.remaining == 0:
                        raise TimeoutError('Bedrock stream deadline expired before dispatch.')
                    if evidence is not None:
                        record_response_dispatch(evidence)
                    dispatched = True
                stream = self.transport.stream(self.api_mode + '_stream', payload, auth=auth,
                    join=self._join_credentials, on_dispatch=dispatch)
            async with aclosing(stream):
                while True:
                    if budget.remaining == 0:
                        raise TimeoutError('Bedrock stream deadline exceeded.')
                    async with component_timeout('Bedrock stream read', budget.remaining) as read_budget:
                        if read_budget.remaining == 0:
                            raise TimeoutError('Bedrock stream deadline expired before dispatch.')
                        try:
                            event = await anext(stream)
                        except StopAsyncIteration:
                            break
                        except (OperationalError, TimeoutError, asyncio.CancelledError) as error:
                            failure_usage = error_usage(error)
                            raw_response = getattr(error, 'provider_response', None)
                            if type(raw_response) is dict:
                                from api_agent.workflows.inference_capture import capture_inference
                                capture_inference('output', {'provider_response': raw_response}, request.request_id, auth=auth)
                            record_response_failure(error)
                            raise
                        normalized_events = decoder.feed(event)
                    for normalized in normalized_events:
                        yield normalized
            if budget.remaining == 0:
                raise TimeoutError('Bedrock stream deadline exceeded.')
            async with component_timeout('Bedrock stream result', budget.remaining):
                result = await async_validate_result(request, replace(decoder.finish(), response=evidence))
            from api_agent.providers.inference import result_to_wire
            from api_agent.workflows.inference_capture import capture_inference
            capture_inference('output', result_to_wire(result), request.request_id,
                              auth=auth, fallback=True, partial=True)
            for call in result.payload.calls:
                yield StreamEvent(call, kind='tool_call')
            yield StreamEvent(result, kind='completed')
        except (OperationalError, TimeoutError, asyncio.CancelledError) as error:
            error.response = evidence if dispatched else None
            retained = error_usage(error) or failure_usage
            if retained is not None:
                error.usage = retained
            record_response_failure(error)
            raise

    def quote_cost(self, request, result):
        return pricing.quote(request, result, rate_card=self.rate_card,
                             compatibility=self.cost_compatibility)

    async def _close_session(self, session):
        # Setup handoff cancellation and provider disposal share one owner task.
        if session not in self.sessions:
            return
        key = id(session)
        task = self._session_closes.get(key)
        if task is None or task.done():
            async def close_session():
                async with component_timeout('Bedrock session owner cleanup', 60):
                    try:
                        await session.aclose()
                    except Exception:
                        raise ContractError('Bedrock provider cleanup failed; retry disposal.') from None
                    self.sessions.remove(session)
                    self._session_closes.pop(key)
            with independent_timeout_scope():
                task = asyncio.create_task(close_session())
            self._session_closes[key] = task

        async def wait_session():
            await asyncio.shield(task)
        await finish_cleanup(wait_session())

    async def aclose(self):
        self.closed = True
        if (self._closing is None or self._closing.cancelled()
                or self._closing.done() and self._closing.exception() is not None):
            async def dispose():
                async with component_timeout('Bedrock provider cleanup', 60):
                    setups = tuple(self._setups)
                    for task in setups:
                        if not task.cancelling():
                            task.cancel()
                    await asyncio.gather(*setups, return_exceptions=True)
                    sessions = tuple(self.sessions)
                    results = await asyncio.gather(self.transport.aclose(),
                        *(self._close_session(session) for session in sessions), return_exceptions=True)
                    if any(isinstance(result, BaseException) for result in results):
                        raise ContractError('Bedrock provider cleanup failed; retry disposal.') from None
            with independent_timeout_scope():
                self._closing = asyncio.create_task(dispose())

        async def wait_close():
            await asyncio.shield(self._closing)
        await finish_cleanup(wait_close())


def factory(config, services):
    """Construct without SDK imports, credential lookup or network activity."""
    # The loader may execute this factory in an isolated module namespace.
    # Keep one canonical provider identity for credential authority binding.
    from api_agent.llm_providers.bedrock.factory import BedrockProvider as CanonicalBedrockProvider
    return CanonicalBedrockProvider(config, services)
