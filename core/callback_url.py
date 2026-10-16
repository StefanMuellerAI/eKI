"""Policy for the optional ``callback_url`` push target (Decision 10b).

``callback_url`` replaces the configured default push target
``{EPRO_BASE_URL}/eki/scl/set-risk-assessment/{project_id}`` for one job.
Because the ePro bearer token travels with the push, the URL is checked twice:

* **Request time** (``core.models``): static checks only -- HTTPS, no
  credentials in the URL, no private/internal IP literal, host on the
  allow-list ``CALLBACK_URL_ALLOWED_HOSTS``.
* **Delivery time** (``workflows.activities.deliver_report_activity``): the
  same static checks against the allow-list valid *then*, plus DNS resolution:
  every address the host resolves to must be public.  The push then connects
  to the validated address (the hostname still drives the ``Host`` header, TLS
  SNI and certificate verification), so a DNS answer that changes between the
  check and the connect (DNS rebinding) cannot redirect the request.

The module is deliberately free of settings access and I/O defaults that
cannot be replaced: the allow-list is passed in, the resolver is injectable.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

DEFAULT_CALLBACK_ALLOWED_HOSTS: tuple[str, ...] = (
    "epro.filmakademie.de",
    "staging.epro.filmakademie.de",
)

MSG_HTTPS_ONLY = "Callback URL must use HTTPS"
MSG_NO_HOST = "Invalid callback URL hostname"
MSG_PRIVATE_IP = "Callback URL cannot point to private/internal IP addresses"
MSG_CREDENTIALS = "Callback URL must not contain credentials"
MSG_NOT_ALLOWED = "Callback URL domain not allowed"
MSG_RESOLVES_PRIVATE = "Callback URL host resolves to a private/internal IP address"

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]


class CallbackUrlError(ValueError):
    """The callback URL violates the policy (never retried, maps to 422 / dead letter)."""


def normalize_host(host: str) -> str:
    """Lower-case and strip the trailing root dot (``EPRO.example.`` == ``epro.example``)."""
    return host.strip().lower().rstrip(".")


def normalize_allowed_hosts(hosts: Iterable[str]) -> frozenset[str]:
    return frozenset(h for h in (normalize_host(str(x)) for x in hosts) if h)


def is_forbidden_address(ip: IPAddress) -> bool:
    """True for every address a push must never reach.

    Private, loopback, link-local, multicast, reserved, unspecified and every
    other non-global range (CGNAT ``100.64.0.0/10``, documentation ranges ...).
    IPv4-mapped IPv6 (``::ffff:a.b.c.d``) is judged by its IPv4 part, whatever
    the Python version's ``ipaddress`` semantics; 6to4 (``2002::/16``) is
    forbidden at least whenever its embedded IPv4 address is.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_forbidden_address(ip.ipv4_mapped)
        if ip.sixtofour is not None and is_forbidden_address(ip.sixtofour):
            return True
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def check_callback_url(url: str, allowed_hosts: Iterable[str]) -> str:
    """Static policy check; returns the normalised host or raises ``CallbackUrlError``.

    Order matters for the error message: an internal IP literal is reported as
    such even when it hides behind userinfo (``https://epro...@127.0.0.1/``).
    """
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise CallbackUrlError(MSG_HTTPS_ONLY)
    hostname = parsed.hostname
    if not hostname:
        raise CallbackUrlError(MSG_NO_HOST)
    try:
        literal: IPAddress | None = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and is_forbidden_address(literal):
        raise CallbackUrlError(MSG_PRIVATE_IP)
    if parsed.username is not None or parsed.password is not None:
        # httpx would turn userinfo into Basic auth next to the ePro bearer token.
        raise CallbackUrlError(MSG_CREDENTIALS)
    allowed = normalize_allowed_hosts(allowed_hosts)
    host = normalize_host(hostname)
    if host not in allowed:
        listed = ", ".join(sorted(allowed)) or "(none configured)"
        raise CallbackUrlError(f"{MSG_NOT_ALLOWED}. Allowed: {listed}")
    return host


async def system_resolver(host: str, port: int) -> list[str]:
    """Resolve ``host`` with the event loop's ``getaddrinfo`` (TCP), de-duplicated."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def resolve_public_addresses(host: str, port: int, resolver: Resolver) -> list[str]:
    """Resolve ``host`` and require every answer to be a public address.

    * Resolution errors (``OSError`` incl. ``socket.gaierror``) propagate: they
      are transient and the delivery is retried.
    * An empty answer is treated the same way (``OSError``).
    * Any non-public address -> ``CallbackUrlError`` (policy violation, no retry).
      All answers are checked, not just the first, so a mixed public/private
      answer cannot be used to reach an internal service on a later connect.
    """
    answers = await resolver(host, port)
    if not answers:
        raise OSError(f"no addresses for callback host (port {port})")
    validated: list[str] = []
    for raw in answers:
        try:
            ip = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError as exc:
            raise CallbackUrlError(MSG_RESOLVES_PRIVATE) from exc
        if is_forbidden_address(ip):
            raise CallbackUrlError(MSG_RESOLVES_PRIVATE)
        validated.append(str(ip))
    return validated
