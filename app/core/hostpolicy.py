"""Host policy for tenant-configured data sources.

A tenant controls the host and port of a data source. Without a policy that
makes the feature a server-side request forgery primitive: a tenant could point
a "warehouse" at the control-plane database, at cloud metadata endpoints, or at
arbitrary internal services and read connection timing as a port scan.
"""

import ipaddress
import socket

from app.core.config import get_settings


class HostNotAllowed(ValueError):
    """The host is unresolvable or resolves somewhere a tenant may not reach."""


def _blocked_reason(address: str) -> str | None:
    ip = ipaddress.ip_address(address)
    checks = (
        (ip.is_loopback, "loopback"),
        (ip.is_link_local, "link-local"),
        (ip.is_private, "private"),
        (ip.is_reserved, "reserved"),
        (ip.is_multicast, "multicast"),
        (ip.is_unspecified, "unspecified"),
    )
    return next((label for failed, label in checks if failed), None)


def resolve_and_check(host: str, port: int, *, allow_private: bool | None = None) -> list[str]:
    """Resolve `host` and reject it if any address is off-limits.

    Called at write time and again at connect time: DNS can change between the
    two, which is exactly what a rebinding attack relies on.
    """
    if allow_private is None:
        allow_private = get_settings().allow_private_data_source_hosts

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise HostNotAllowed(f"{host!r} does not resolve") from exc

    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise HostNotAllowed(f"{host!r} does not resolve")
    if allow_private:
        return addresses

    for address in addresses:
        reason = _blocked_reason(address)
        if reason is not None:
            raise HostNotAllowed(
                f"{host!r} resolves to {address}, which is {reason}. "
                "Data sources may not point at internal addresses."
            )
    return addresses
