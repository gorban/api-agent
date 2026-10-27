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

    def _open(self):
        if self.closed:
            raise ContractError('Bedrock provider is closed.')

    async def asetup(self, *, interactive, user_index, credentials):
        self._open()

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
        self.validate_request(request)
        from .mantle import encode, decode
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
                capture_payload(request, payload['body'], evidence, auth=auth)
                if budget.remaining == 0:
                    raise TimeoutError('Bedrock deadline expired before dispatch.')
                if evidence is not None:
                    record_response_dispatch(evidence)
                dispatched = True
                try:
                    response = await self.transport.call(self.api_mode, payload, auth=auth)
                    result = decode(response, self.api_mode, selected_llm=request.selected_llm)
                    return await async_validate_result(request, replace(result, response=evidence))
                except (OperationalError, TimeoutError, asyncio.CancelledError) as error:
                    failure_usage = error_usage(error)
                    record_response_failure(error)
                    raise
        except (OperationalError, TimeoutError, asyncio.CancelledError) as error:
            error.response = evidence if dispatched else None
            retained = error_usage(error) or failure_usage
            if retained is not None:
                error.usage = retained
            raise

    async def astream(self, request, *, auth=None):
        self.validate_request(request)
        from .mantle import encode, StreamDecoder
        from .response_policy import prepare_response
        from api_agent.llm_providers.response_policy import capture_payload, format_payload
        from api_agent.workflows.inference_capture import record_response_dispatch, record_response_failure
        from api_agent.providers.inference import error_usage
        prepared, evidence = prepare_response(request, self.api_mode)
        failure_usage = None
        dispatched = False
        try:
            decoder = StreamDecoder(self.api_mode, selected_llm=request.selected_llm)
            async with component_timeout('Bedrock streaming completion', self.inference_timeout) as budget:
                payload = {'prefix': catalog.route(request.selected_llm, self.api_mode),
                           'body': format_payload(request, encode(prepared, self.api_mode), evidence)}
                stream = self.transport.stream(self.api_mode + '_stream', payload, auth=auth)
            async with aclosing(stream):
                while True:
                    if budget.remaining == 0:
                        raise TimeoutError('Bedrock stream deadline exceeded.')
                    async with component_timeout('Bedrock stream read', budget.remaining) as read_budget:
                        if read_budget.remaining == 0:
                            raise TimeoutError('Bedrock stream deadline expired before dispatch.')
                        if not dispatched:
                            capture_payload(request, payload['body'], evidence, auth=auth)
                            if read_budget.remaining == 0:
                                raise TimeoutError('Bedrock stream deadline expired before dispatch.')
                            if evidence is not None:
                                record_response_dispatch(evidence)
                            dispatched = True
                        try:
                            event = await anext(stream)
                        except StopAsyncIteration:
                            break
                        except (OperationalError, TimeoutError, asyncio.CancelledError) as error:
                            failure_usage = error_usage(error)
                            record_response_failure(error)
                            raise
                        normalized_events = decoder.feed(event)
                    for normalized in normalized_events:
                        yield normalized
            if budget.remaining == 0:
                raise TimeoutError('Bedrock stream deadline exceeded.')
            async with component_timeout('Bedrock stream result', budget.remaining):
                result = await async_validate_result(request, replace(decoder.finish(), response=evidence))
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
    return BedrockProvider(config, services)
