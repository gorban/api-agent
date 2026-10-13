from dataclasses import FrozenInstanceError

import pytest

from api_agent.runtime.reporting_identity import (
    ExternalSelection, InstallationId, ManagedSelection, PublicationAccess,
    ReportingEndpoint, ReportingOperation, ReportingRegistration,
    ReleaseDisposition, bind_managed_publication,
)

ID = InstallationId("12345678-1234-4234-8234-123456789abc")
OTHER_ID = InstallationId("12345678-1234-4234-8234-123456789abd")
ENDPOINT = ReportingEndpoint("https://reports.example")


@pytest.mark.parametrize("value", [None, 1, True, b"id"])
def test_id_requires_text(value):
    with pytest.raises(TypeError, match="must be text"):
        InstallationId(value)


@pytest.mark.parametrize("value", ["", "checkout/path", "12345678123442348234123456789abc", "12345678-1234-4234-8234-123456789ABC"])
def test_id_requires_canonical_uuid(value):
    with pytest.raises(ValueError, match="canonical UUID"):
        InstallationId(value)


@pytest.mark.parametrize("value", [None, 1, True, b"https://reports.example"])
def test_endpoint_requires_text(value):
    with pytest.raises(TypeError, match="must be text"):
        ReportingEndpoint(value)


@pytest.mark.parametrize("value", [
    "", " https://reports.example", "https://reports.example\n", "https://réports.example",
    "https://user:password@reports.example", "https://reports.example?secret=x", "https://reports.example#secret",
    "https://reports.example/%61", "https://reports.example\\x", "ftp://reports.example", "http:/reports.example",
    "https://", "http://reports.example:", "http://reports.example:0", "http://reports.example:65536",
    "http://[::1]suffix", "http://reports.example:no", "http://[broken]", "http://[a:b]", "http://127.01.0.1", "http://123",
    "http://-bad.example", "http://bad-.example", "http://bad..example", "http://example.", "http://" + "a" * 254,
    "http://reports.example/a/../b", "http://reports.example/./b", "http://reports.example/a//b", "http://reports.example/a;b",
])
def test_endpoint_rejects_unsafe_forms_without_echo(value):
    with pytest.raises(ValueError) as error:
        ReportingEndpoint(value)

    assert str(error.value) == "Invalid reporting endpoint."
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__


@pytest.mark.parametrize(("value", "expected"), [
    ("HTTPS://REPORTS.EXAMPLE:443", "https://reports.example/"),
    ("http://reports.example:80/", "http://reports.example/"),
    ("https://reports.example:8443/Case-_.~", "https://reports.example:8443/Case-_.~"),
    ("http://127.0.0.1", "http://127.0.0.1/"),
    ("https://[2001:0db8::1]:443/", "https://[2001:db8::1]/"),
])
def test_endpoint_normalizes_equivalent_forms(value, expected):
    endpoint = ReportingEndpoint(value)

    assert endpoint.value == expected
    assert endpoint == ReportingEndpoint(expected)


@pytest.mark.parametrize("other", ["http://reports.example", "https://reports.example:444", "https://reports.example/path", "https://reports.example/Path/"])
def test_endpoint_preserves_routing_differences(other):
    endpoint = ReportingEndpoint(other)

    assert endpoint != ENDPOINT


@pytest.mark.parametrize(("factory", "args"), [
    (ManagedSelection, ("id", ENDPOINT)), (ManagedSelection, (ID, "url")),
    (ReportingRegistration, ("id", ENDPOINT)), (ReportingRegistration, (ID, "url")),
    (ExternalSelection, ("url",)), (PublicationAccess, ("selection",)),
])
def test_declarations_validate_types(factory, args):
    with pytest.raises(TypeError):
        factory(*args)


@pytest.mark.parametrize(("selection", "registration"), [(None, ReportingRegistration(ID, ENDPOINT)), (ManagedSelection(ID, ENDPOINT), None)])
def test_binding_requires_typed_selection_and_registration(selection, registration):
    with pytest.raises(TypeError):
        bind_managed_publication(selection, registration)


@pytest.mark.parametrize("factory", [bind_managed_publication, PublicationAccess])
@pytest.mark.parametrize(("registration", "message"), [
    (ReportingRegistration(OTHER_ID, ENDPOINT), "installation"),
    (ReportingRegistration(ID, ReportingEndpoint("https://other.example")), "endpoint"),
])
def test_binding_rejects_each_mismatch_independently(factory, registration, message):
    selection = ManagedSelection(ID, ENDPOINT)

    with pytest.raises(ValueError, match=message):
        factory(selection, registration)


def test_binding_accepts_equivalent_registered_endpoint():
    selection = ManagedSelection(ID, ENDPOINT)
    registration = ReportingRegistration(ID, ReportingEndpoint("HTTPS://REPORTS.EXAMPLE:443/"))

    access = bind_managed_publication(selection, registration)

    assert access.selection == selection


@pytest.fixture(params=["managed", "external"])
def access(request):
    if request.param == "managed":
        return bind_managed_publication(ManagedSelection(ID, ENDPOINT), ReportingRegistration(ID, ENDPOINT))
    return PublicationAccess(ExternalSelection(ENDPOINT))


@pytest.mark.parametrize("operation", [ReportingOperation.PUBLISH, ReportingOperation.RELINQUISH])
def test_publication_operations_are_allowed(access, operation):
    result = access.require_operation(operation)

    assert result is None


@pytest.mark.parametrize("operation", [ReportingOperation.STOP, ReportingOperation.RECREATE, ReportingOperation.DELETE_DATA])
def test_identity_never_grants_administration(access, operation):
    with pytest.raises(PermissionError, match="cannot administer"):
        access.require_operation(operation)


@pytest.mark.parametrize("operation", ["publish", None, True])
def test_operation_requires_explicit_enum(access, operation):
    with pytest.raises(TypeError):
        access.require_operation(operation)


def test_release_always_retains_service_and_data(access):
    first = access.release()
    repeated = access.release()

    assert first is ReleaseDisposition.RETAIN_SERVICE_AND_DATA
    assert repeated is first


def test_external_selection_has_no_inferred_owner():
    selection = ExternalSelection(ENDPOINT)

    assert not hasattr(selection, "installation_id")


@pytest.mark.parametrize("value", [ID, ENDPOINT, ManagedSelection(ID, ENDPOINT), ExternalSelection(ENDPOINT), ReportingRegistration(ID, ENDPOINT), PublicationAccess(ExternalSelection(ENDPOINT))])
def test_contract_values_are_immutable(value):
    with pytest.raises(FrozenInstanceError):
        value.value = "changed"


def test_managed_access_cannot_bypass_registration():
    selection = ManagedSelection(ID, ENDPOINT)

    with pytest.raises(TypeError, match="ReportingRegistration"):
        PublicationAccess(selection)


def test_external_access_rejects_managed_registration():
    selection = ExternalSelection(ENDPOINT)
    registration = ReportingRegistration(ID, ENDPOINT)

    with pytest.raises(ValueError, match="cannot claim"):
        PublicationAccess(selection, registration)


@pytest.mark.parametrize("host", ["0x7f000001", "0x7f.0.0.1", "127.0.0.0x1", "0X7F.1", "1.2.3.0xff"])
def test_endpoint_rejects_legacy_hex_numeric_hosts(host):
    value = f"http://{host}"

    with pytest.raises(ValueError, match="Invalid reporting endpoint"):
        ReportingEndpoint(value)


@pytest.mark.parametrize("host", ["0x7f.example", "0x7f000001.example", "127.reports.example", "0xreports", "1.2.3.reports", "reports0xff.example"])
def test_endpoint_preserves_dns_near_numeric_hosts(host):
    value = f"http://{host}"

    endpoint = ReportingEndpoint(value)

    assert endpoint.value == value + "/"
