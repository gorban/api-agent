"""Pure profile parsing and selection; no credential CLI access."""
import pytest

from api_agent.auth.profiles import SelectionRequired, parse_profiles, select_profiles


def source(user, manager="keeper"):
    return {"manager": manager, "cli_path": "/bin/false", "vault": "vault", "user": user}


def test_zero_single_and_unused_roles():
    profiles = parse_profiles({"LLM User": [source("Ada")], "Extra": [source("Unused"), source("Other")]}, ["LLM User", "Extra", "Missing"])
    result = select_profiles(profiles, ["LLM User", "Missing"])
    assert result.index == 0
    assert result.for_role("LLM User").user == "Ada"
    assert result.attended_roles == ("Missing",)
    assert result.for_role("Missing") is None
    with pytest.raises(ValueError, match="not requested"):
        result.for_role("Extra")
    empty = select_profiles(parse_profiles({}, ["LLM User"]), ["LLM User"])
    assert empty.index is None
    assert empty.attended_roles == ("LLM User",)


def test_ambiguity_options_and_paired_index():
    profiles = parse_profiles({"LLM User": [source("A"), source("B")], "Extra": [source("X"), source("Y", "1password")]}, ["LLM User", "Extra"])
    with pytest.raises(SelectionRequired) as error:
        select_profiles(profiles, ["LLM User", "Extra"])
    assert [(x.role, x.index, x.vault, x.user) for x in error.value.options] == [
        ("LLM User", 0, "vault", "A"), ("LLM User", 1, "vault", "B"),
        ("Extra", 0, "vault", "X"), ("Extra", 1, "vault", "Y")]
    assert select_profiles(profiles, ["LLM User", "Extra"], session_index=1).for_role("Extra").user == "Y"
    assert select_profiles(profiles, ["LLM User"], configured_index=0, session_index=1).for_role("LLM User").user == "A"
    assert select_profiles(profiles, ["LLM User"], configured_index=0, session_index="stale").for_role("LLM User").user == "A"


def test_index_validation_including_singleton_and_empty():
    profiles = parse_profiles({"LLM User": [source("A"), source("B")], "Extra": [source("X")]}, ["LLM User", "Extra", "Empty"])
    for bad in (True, -1, 1.0, "1"):
        with pytest.raises(ValueError, match="nonnegative integer"):
            select_profiles(profiles, ["LLM User"], configured_index=bad)
    paired = select_profiles(profiles, ["LLM User", "Extra"], configured_index=0)
    assert paired.for_role("LLM User").user == "A"
    assert paired.for_role("Extra").user == "X"
    with pytest.raises(ValueError, match="Extra"):
        select_profiles(profiles, ["LLM User", "Extra"], configured_index=1)
    with pytest.raises(ValueError, match="LLM User"):
        select_profiles(profiles, ["LLM User"], configured_index=2)
    assert select_profiles(profiles, ["Empty"], configured_index=9).attended_roles == ("Empty",)


@pytest.mark.parametrize("payload,roles,message", [
    ([], ["LLM User"], "JSON object"),
    ({"Other": []}, ["LLM User"], "unknown role"),
    ({"LLM User": {}}, ["LLM User"], "array"),
    ({"LLM User": [None]}, ["LLM User"], "object"),
    ({"LLM User": [{"manager": "keeper"}]}, ["LLM User"], "exactly"),
    ({"LLM User": [source("", "keeper")]}, ["LLM User"], "nonempty"),
    ({"LLM User": [source("A", "bad")]}, ["LLM User"], "keeper or 1password"),
])
def test_invalid_profiles(payload, roles, message):
    with pytest.raises(ValueError, match=message):
        parse_profiles(payload, roles)


def test_role_validation():
    with pytest.raises(ValueError, match="duplicate"):
        parse_profiles({}, ["LLM User", "LLM User"])
    with pytest.raises(ValueError, match="nonempty"):
        parse_profiles({}, [""])
    with pytest.raises(ValueError, match="sequence"):
        parse_profiles({}, "LLM User")
    profiles = parse_profiles({}, ["LLM User"])
    with pytest.raises(ValueError, match="enabled"):
        select_profiles(profiles, ["Other"])
    with pytest.raises(ValueError, match="Unknown"):
        profiles.for_role("Other")
