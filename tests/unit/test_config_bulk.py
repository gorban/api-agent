from types import SimpleNamespace

import pytest

from api_agent.config import schema_from_dict
from api_agent.config.dotenv import read_dotenv
from api_agent.config.session import ConfigSession, DotenvEditor
from api_agent.tui.config_bulk import plan_review, plan_cleanup


@pytest.fixture
def session(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir()
    pack.mkdir()
    context = SimpleNamespace(base_root=base, pack_root=pack, dotenv_path=pack / '.env',
                              anchors={'base_root': base, 'pack_root': pack, 'owner_root': pack},
                              external_pack=True)
    schema = schema_from_dict({'version': 1, 'fields': {
        'FIRST': {'type': 'str', 'default': 'one'},
        'SECOND': {'type': 'str', 'default': 'two'},
        'SECRET': {'type': 'str', 'default': 'private', 'sensitive': True},
        'MISSING': {'type': 'str', 'review_optional': True},
    }})
    return ConfigSession(context, schema, environ={})


def test_review_plan_reports_skipped_and_never_copies_masked_secrets(session):
    snapshot = session.review_snapshot()
    plan = plan_review(snapshot, ('SECOND', 'SECRET', 'MISSING', 'FIRST'), 'copy')
    assert plan.names == ('SECOND', 'FIRST')
    assert plan.skipped == ('SECRET', 'MISSING')
    assert plan.copy_text == 'SECOND=two\nFIRST=one'


def test_review_accept_and_unset_preserve_distinct_sources(session):
    base, pack = session.target_paths
    base.write_text('FIRST=base\nSECOND=base\n')
    pack.write_text('FIRST=pack\n')
    plan = plan_review(session.review_snapshot(), ('FIRST', 'SECOND', 'MISSING'), 'unset')
    assert plan.names == ('FIRST', 'SECOND')
    assert plan.skipped == ('MISSING',)
    session.unset_saved_many(plan.names)
    assert read_dotenv(base) == {'FIRST': 'base'}
    assert read_dotenv(pack) == {}
    session.save_defaults(['SECOND'])
    assert read_dotenv(pack) == {'SECOND': 'two'}


def test_cleanup_plan_keeps_visit_membership_and_delete_removes_all_layers(session):
    base, pack = session.target_paths
    base.write_text('FIRST=base\nSECOND=base\n')
    pack.write_text('FIRST=pack\n')
    rows = session.cleanup_rows(retained={'FIRST'})
    plan = plan_cleanup(rows, ('FIRST', 'SECOND', 'MISSING'), 'delete', membership={'FIRST'})
    assert plan.names == ('FIRST',)
    assert plan.skipped == ('SECOND', 'MISSING')
    session.delete_keys(plan.names)
    assert read_dotenv(base) == {'SECOND': 'base'}
    assert read_dotenv(pack) == {}


@pytest.mark.parametrize('operation', ['unset', 'delete'])
def test_interrupted_multi_file_removal_rolls_back_all_sources(session, monkeypatch, operation):
    base, pack = session.target_paths
    base.write_text('FIRST=base\n')
    pack.write_text('SECOND=pack\n')
    action = (lambda: session.unset_saved_many(('FIRST', 'SECOND'))) if operation == 'unset' else (
        lambda: session.delete_keys(('FIRST', 'SECOND')))
    before = (base.read_bytes(), pack.read_bytes())
    original = DotenvEditor.delete
    calls = 0
    def interrupted(editor, names):
        nonlocal calls
        calls += 1
        original(editor, names)
        if calls == 2:
            raise OSError('interrupted')
    monkeypatch.setattr(DotenvEditor, 'delete', interrupted)
    with pytest.raises(OSError, match='interrupted'):
        action()
    assert (base.read_bytes(), pack.read_bytes()) == before


def test_interrupted_default_write_restores_target(session, monkeypatch):
    target = session.dotenv_path
    target.write_text('# original\n')
    original = DotenvEditor.update
    def interrupted(editor, values):
        original(editor, values)
        raise OSError('interrupted')
    monkeypatch.setattr(DotenvEditor, 'update', interrupted)
    with pytest.raises(OSError, match='interrupted'):
        session.save_defaults(('FIRST', 'SECOND'))
    assert target.read_text() == '# original\n'


def test_accept_plan_uses_fresh_conditional_candidate_and_target(session):
    base, pack = session.target_paths
    session.switch_target(base)
    snapshot = session.review_snapshot()
    plan = plan_review(snapshot, ('FIRST', 'MISSING', 'SECOND'), 'accept')
    assert plan.names == ('FIRST', 'SECOND')
    assert plan.skipped == ('MISSING',)
    session.save_defaults(plan.names)
    assert read_dotenv(base) == {'FIRST': 'one', 'SECOND': 'two'}
    assert not pack.exists()
    assert not snapshot.sources_current()
    assert plan_review(session.review_snapshot(), plan.names, 'accept').names == ()


def test_single_copy_keeps_existing_format_and_empty_sensitive_value(session):
    session.set_value('SECRET', '')
    plan = plan_review(session.review_snapshot(), ('SECRET',), 'copy')
    assert plan.names == ('SECRET',)
    assert plan.copy_text == ''


def test_unknown_action_is_rejected(session):
    with pytest.raises(ValueError, match='Unknown review action'):
        plan_review(session.review_snapshot(), (), 'unknown')
    with pytest.raises(ValueError, match='Unknown cleanup action'):
        plan_cleanup(session.cleanup_rows(), (), 'unknown', membership=set())


def test_no_copyable_selection_has_no_clipboard_payload(session):
    plan = plan_review(session.review_snapshot(), ('SECRET',), 'copy')
    assert plan.names == ()
    assert plan.skipped == ('SECRET',)
    assert plan.copy_text is None


def test_cleanup_copy_filters_membership_and_sensitive_values(session):
    base, pack = session.target_paths
    base.write_text('FIRST=base\nSECRET=hidden\n')
    pack.write_text('SECOND=pack\n')
    rows = session.cleanup_rows(retained={'MISSING'})
    plan = plan_cleanup(rows, ('SECOND', 'SECRET', 'FIRST', 'MISSING'), 'copy',
                        membership={'FIRST', 'SECOND', 'SECRET', 'MISSING'})
    assert plan.names == ('SECOND', 'FIRST')
    assert plan.skipped == ('SECRET', 'MISSING')
    assert plan.copy_text == 'SECOND=pack\nFIRST=base'
    excluded = plan_cleanup(rows, ('FIRST',), 'copy', membership={'SECOND'})
    assert excluded.names == ()
    assert excluded.skipped == ('FIRST',)
    assert excluded.copy_text is None


def test_cleanup_unknown_action_rejected_with_populated_membership(session):
    rows = session.cleanup_rows()
    assert rows
    with pytest.raises(ValueError, match='Unknown cleanup action'):
        plan_cleanup(rows, ('FIRST',), 'unknown', membership={'FIRST'})


def test_batch_delete_rejects_symlink_without_changing_any_source(session, tmp_path):
    base, pack = session.target_paths
    base.write_text('FIRST=base\n')
    destination = tmp_path / 'external-synthetic.env'
    destination.write_text('FIRST=pack\n')
    pack.symlink_to(destination)
    with pytest.raises(ValueError, match='Refusing to edit symlink'):
        session.delete_keys(('FIRST',))
    assert base.read_text() == 'FIRST=base\n'
    assert destination.read_text() == 'FIRST=pack\n'
    assert pack.is_symlink()


def test_save_defaults_removes_other_source_created_after_candidate_snapshot(session, monkeypatch):
    base, pack = session.target_paths
    original = session.default_candidates
    def candidates_before_concurrent_write():
        candidates = original()
        base.write_text('FIRST=stale\nSECOND=keep\n')
        return candidates
    monkeypatch.setattr(session, 'default_candidates', candidates_before_concurrent_write)
    session.save_defaults(('FIRST',))
    assert read_dotenv(pack) == {'FIRST': 'one'}
    assert read_dotenv(base) == {'SECOND': 'keep'}
