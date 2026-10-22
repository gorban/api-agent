"""One operation-owned headed startup browser and selected workflow host."""
import asyncio
from urllib.parse import urlsplit

from api_agent.providers.contracts import BrowserSession
from api_agent.providers.control import finish_cleanup, CleanupPendingError
from api_agent.providers.progress import component_timeout
from api_agent.providers.sessions import InspectionOwner
from api_agent.runtime.managed_browser import managed_browser


def _prepare_view(context):
    from api_agent.runtime.view_flow import _prepare_view as prepare
    return prepare(context)


async def _start_viewer_host(bundle, **options):
    from api_agent.runtime.viewer_host import start_viewer_host
    return await start_viewer_host(bundle, **options)


class BorrowedBrowserPage:
    """Give UI preparation its own page without transferring browser ownership."""
    def __init__(self, session):
        self.session = session

    async def start(self):
        return self.session

    async def aclose(self):
        pass


class LocalBrowserOwner:
    def __init__(self, context, config, home_url, *, prepare_ui=None,
                 browser_factory=managed_browser, prepare_view=_prepare_view,
                 host_factory=_start_viewer_host):
        self.context, self.config, self.home_url = context, config, home_url
        self.prepare_ui = prepare_ui
        self.browser_factory, self.prepare_view, self.host_factory = browser_factory, prepare_view, host_factory
        self.inspection = InspectionOwner()
        self.host = self.browser = self.session = self.ui = None
        self._started = self.closed = False
        self._failed_owners = []
        self._starting = None
        self._cancel_requested = False
        self._cleanup_lock = asyncio.Lock()

    @property
    def flow_url(self):
        return None if self.host is None else self.host.entry_url

    async def start(self):
        if self.closed or self._started:
            raise RuntimeError('Local browser owner is one-shot')
        self._started = True
        self._starting = asyncio.current_task()
        try:
            async with component_timeout('Local workflow host preparation', 120):
                _, projection, _, bundle, sources = self.prepare_view(self.context)
                self.host = await self.host_factory(bundle, projection=projection, repository_sources=sources)
            if self.closed:
                raise RuntimeError('Local startup closed during preparation')
            if not (self.config.get('RUN_UI', False) or self.config.get('DEBUG', False)):
                return self
            async with component_timeout('Local startup browser preparation', 120):
                self.browser = self.browser_factory(inspection=self.inspection, headless=False)
                self.session = await self.browser.start()
                response = await self.session.page.goto(self.home_url, wait_until='load')
                if response is None or response.status != 200:
                    raise RuntimeError('Agent home did not load')
                if self.config.get('RUN_UI', False):
                    if self.prepare_ui is None:
                        raise RuntimeError('Enabled local UI requires preparation')
                    page = await self.session.context.new_page()
                    borrowed = BorrowedBrowserPage(BrowserSession(self.session.browser, self.session.context,
                                                                 page, self.session.inspection_endpoint))
                    self.ui = await self.prepare_ui(browser_owner=borrowed)
                    await self._verify_ui(borrowed, page)
                flow_page = await self.session.context.new_page()
                response = await flow_page.goto(self.flow_url, wait_until='load')
                if response is None or response.status != 200:
                    raise RuntimeError('Selected workflow view did not load')
            if self.closed:
                raise RuntimeError('Local startup closed during preparation')
            return self
        except BaseException as error:
            retained = getattr(error, 'retained_owners', ())
            self._failed_owners = list(retained)
            try:
                await finish_cleanup(self.aclose(_starting_failure=True))
            except BaseException as cleanup:
                error.cleanup_error = cleanup
                error.retained_owners = (self,)
            raise
        finally:
            self._starting = None

    async def _verify_ui(self, borrowed, original):
        session = borrowed.session
        if (self.ui.session is not session or session.browser is not self.session.browser
                or session.context is not self.session.context):
            raise RuntimeError('UI preparation replaced the original session')
        page = session.page
        if (page.context is not session.context or page.is_closed()
                or not session.browser.is_connected()):
            raise RuntimeError('UI preparation lost the active page')
        expected = urlsplit(self.config['UI_LISTENER_URL'])
        actual = urlsplit(page.url)
        actual_port = actual.port or (443 if actual.scheme == 'https' else 80)
        expected_port = expected.port or (443 if expected.scheme == 'https' else 80)
        if (actual.scheme, actual.hostname, actual_port) != (expected.scheme, expected.hostname, expected_port):
            raise RuntimeError('UI active page has an unexpected origin')
        if page is not original:
            if not original.is_closed():
                await original.close()
        if list(session.context.pages) != [self.session.page, page]:
            raise RuntimeError('UI preparation left unexpected startup tabs')

    async def aclose(self, *, _starting_failure=False):
        self.closed = True
        starting = self._starting
        if not _starting_failure and starting is not None and starting is not asyncio.current_task() and not starting.done():
            if not self._cancel_requested:
                self._cancel_requested = True
                starting.cancel()
            async with component_timeout('Local browser startup cancellation', 15):
                done, _ = await asyncio.wait({starting}, timeout=10)
                if not done:
                    raise CleanupPendingError((self,), self.aclose)
        async def cleanup():
            while self._failed_owners:
                await self._failed_owners[0].aclose()
                self._failed_owners.pop(0)
            for name in ('ui', 'browser', 'inspection', 'host'):
                owner = getattr(self, name)
                if owner is not None:
                    await owner.aclose()
                    setattr(self, name, None)
        async with self._cleanup_lock:
            await finish_cleanup(cleanup())
