"""Independent work bounds of optional rejected-response evidence admission."""
import pytest

from api_agent.providers import rejected_evidence as evidence
from api_agent.providers.contracts import ContractError


def test_fragment_matcher_enforces_its_own_leaf_limit():
    with pytest.raises(ContractError, match='carrier protection limit'):
        evidence._guard_response_fragments([''] * 129, set())


def test_fragment_matcher_checks_whole_credentials_independently():
    with pytest.raises(ContractError, match='safely released'):
        evidence._guard_response_fragments(['synthetic-credential'], {'synthetic-credential'})


def test_empty_evidence_strings_are_harmless():
    assert evidence.guard_rejected_evidence({'content':''}, {'synthetic-credential'}) == {'content':''}


def test_leaf_traversal_has_an_independent_work_limit(monkeypatch):
    monkeypatch.setattr(evidence, '_MAX_WORK', 5)
    # Isolate traversal admission from the later matcher's separate work bound.
    monkeypatch.setattr(evidence, '_guard_response_fragments', lambda *args: None)
    with pytest.raises(ContractError, match='carrier protection limit'):
        evidence.guard_rejected_evidence({'x':'12345'}, set())


def test_embedded_parser_charges_work_before_decoding(monkeypatch):
    monkeypatch.setattr(evidence, '_MAX_WORK', 10)
    monkeypatch.setattr(evidence, '_guard_response_fragments', lambda *args: None)
    with pytest.raises(ContractError, match='ambiguous JSON carrier'):
        evidence.guard_rejected_evidence({'x':'"12345"'}, set())


def test_nested_carriers_have_an_independent_depth_limit(monkeypatch):
    monkeypatch.setattr(evidence, '_MAX_DEPTH', 2)
    with pytest.raises(ContractError, match='carrier protection limit'):
        evidence.guard_rejected_evidence({'x':{'y':{'z':'safe'}}}, set())
