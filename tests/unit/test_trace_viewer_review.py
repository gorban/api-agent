"""An accepted empty viewer override stays acknowledged across launches."""
import asyncio

from textual.widgets import DataTable

from api_agent.config import load_shared_schema, read_dotenv
from test_tui import fixture


def test_empty_viewer_default_can_be_accepted_and_stays_saved_after_restart(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        app.schema = load_shared_schema()
        app.session.schema = app.schema
        app.context.anchors = {name: tmp_path for name in ('owner_root', 'base_root', 'pack_root')}
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            row = next(i for i, item in enumerate(table.ordered_rows)
                       if item.key.value == 'TRACE_VIEWER_URL')
            table.move_cursor(row=row)
            await pilot.pause()
            assert app.screen.check_action('accept_default', ())

            await pilot.press('a')
            await pilot.pause()

            assert read_dotenv(tmp_path / '.env')['TRACE_VIEWER_URL'] == ''
            assert app.session.result(_review=True).fields['TRACE_VIEWER_URL'].explicit
            assert 'TRACE_VIEWER_URL' not in app.configuration_pending()
        relaunched = fixture(tmp_path)
        relaunched.schema = app.schema
        relaunched.session.schema = app.schema
        relaunched.context.anchors = app.context.anchors
        async with relaunched.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            table = relaunched.screen.query_one(DataTable)
            assert 'TRACE_VIEWER_URL' not in [item.key.value for item in table.ordered_rows]
            assert 'TRACE_VIEWER_URL' not in relaunched.configuration_pending()
            assert relaunched.session.result(_review=True).values['TRACE_VIEWER_URL'] == ''
    asyncio.run(scenario())
