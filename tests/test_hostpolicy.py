"""Step 16b: the SSRF guard on tenant-configured data sources."""

import pytest

from app.core.hostpolicy import HostNotAllowed, resolve_and_check


@pytest.mark.parametrize(
    "host",
    [
        "localhost",            # loopback — could reach the control plane
        "127.0.0.1",
        "169.254.169.254",      # cloud instance metadata
        "10.0.0.1",             # RFC1918
        "192.168.1.1",
        "172.16.0.1",
        "0.0.0.0",
    ],
)
def test_internal_addresses_are_refused(host: str) -> None:
    with pytest.raises(HostNotAllowed):
        resolve_and_check(host, 5432, allow_private=False)


def test_unresolvable_host_is_refused() -> None:
    with pytest.raises(HostNotAllowed):
        resolve_and_check("no-such-host.invalid", 5432, allow_private=False)


def test_public_address_is_allowed() -> None:
    assert resolve_and_check("93.184.216.34", 5432, allow_private=False) == ["93.184.216.34"]


def test_development_override_permits_internal_addresses() -> None:
    """Required for local work, where the warehouse is on a private Docker network."""
    assert resolve_and_check("127.0.0.1", 5432, allow_private=True) == ["127.0.0.1"]


def test_the_control_plane_host_is_refused_by_default() -> None:
    """A tenant naming our own database container must not get a connection."""
    with pytest.raises(HostNotAllowed):
        resolve_and_check("db", 5432, allow_private=False)
