"""Outbound URL safety checks (SSRF hardening).

Run before the server makes a request to a *user-supplied* URL — e.g. the custom
embedding endpoint set via ``POST /api/embeddings/endpoint``, which then triggers
an outbound ``httpx`` call.

Odysseus is local-first: pointing the embedding endpoint at a loopback or LAN
address (a local vLLM / llama.cpp / Ollama server) is a normal, intended setup.
So this guard does **not** blanket-block private addresses by default — that would
break the primary use case. What it *always* rejects:

  - a non-HTTP(S) scheme (``file://``, ``gopher://``, ``ftp://`` …), and
  - the link-local range (``169.254.0.0/16`` / ``fe80::/10``), i.e. the cloud
    instance-metadata SSRF credential-exfil vector — nobody serves embeddings
    there — plus multicast / non-loopback reserved / unspecified addresses.

For exposed multi-tenant deployments, set ``EMBEDDING_BLOCK_PRIVATE_IPS=true`` to
additionally reject all private and loopback targets (full SSRF lockdown).
"""

import ipaddress
import socket
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlparse

ALLOWED_SCHEMES = ("http", "https")

# RFC 6598 shared address space (carrier-grade NAT). It is not globally
# routable, but CPython does not classify it as ``is_private`` (it is "shared",
# not "private"), so the is_private/is_loopback checks miss it. Reject the range
# explicitly. This closes exactly the shared-space gap without coupling strict
# mode to ``is_global``'s broader definition, which has shifted across CPython
# versions for other special ranges.
_SHARED_ADDRESS_SPACE_V4 = ipaddress.ip_network("100.64.0.0/10")

_LOCALHOST_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
# Alibaba's metadata endpoint is shared address space, not link-local.
_METADATA_IPS = {ipaddress.ip_address("100.100.100.200")}


def _default_resolver(host: str) -> List[str]:
    """Resolve a hostname to the list of IP strings it maps to (A + AAAA)."""
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _classify(ip: ipaddress._BaseAddress, *, block_private: bool) -> Optional[str]:
    """Return a rejection reason for an IP, or None if it is allowed."""
    # IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) — judge the embedded v4.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _METADATA_IPS:
        return f"metadata endpoint blocked: {ip}"
    if ip.is_link_local:
        return f"link-local address blocked (SSRF metadata risk): {ip}"
    # IPv6 loopback is also reserved; its policy must match IPv4 loopback.
    if ip.is_loopback:
        if block_private:
            return f"private/shared/loopback address blocked: {ip}"
        return None
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return f"disallowed address: {ip}"
    if block_private and (
        ip.is_private
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _SHARED_ADDRESS_SPACE_V4)
    ):
        return f"private/shared/loopback address blocked: {ip}"
    return None


def check_outbound_url(
    url: str,
    *,
    block_private: bool = False,
    resolver: Optional[Callable[[str], List[str]]] = None,
) -> Tuple[bool, str]:
    """Validate a user-supplied outbound URL.

    Returns ``(ok, reason)``. ``ok`` is True only when the URL is safe to fetch.
    ``resolver`` is injectable so callers/tests can avoid real DNS.
    """
    if not isinstance(url, str):
        return False, "URL must be a string"
    if not url or not url.strip():
        return False, "URL is required"
    try:
        parsed = urlparse(url.strip())
    except Exception as e:  # pragma: no cover - urlparse is very tolerant
        return False, f"unparseable URL: {e}"

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False, f"scheme must be http or https, got '{parsed.scheme or '(none)'}'"
    host = parsed.hostname
    if not host:
        return False, "URL has no host"

    resolve = resolver or _default_resolver
    try:
        raw_ips = resolve(host)
    except Exception as e:
        return False, f"host does not resolve: {e}"
    if not raw_ips:
        return False, "host does not resolve"

    saw_ip = False
    for raw in raw_ips:
        if not isinstance(raw, str):
            continue
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])  # strip IPv6 zone id
        except ValueError:
            continue
        saw_ip = True
        reason = _classify(ip, block_private=block_private)
        if reason:
            return False, reason
    if not saw_ip:
        return False, "host does not resolve to an IP"
    return True, "ok"


def check_outbound_host(
    host: str,
    *,
    block_private: bool = False,
    resolver: Optional[Callable[[str], List[str]]] = None,
) -> Tuple[bool, str]:
    """Validate a bare mail hostname/IP, rejecting unresolved destinations.

    Saving an unresolvable hostname is unsafe: another consumer of the stored
    configuration may later connect after its DNS records change.
    """
    if not isinstance(host, str):
        return False, "host must be a string"
    host = host.strip()
    if not host:
        return False, "host is required"
    if any(ch.isspace() for ch in host) or any(ch in host for ch in "/\\@?#"):
        return False, "host must be a bare hostname or IP address"
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ip = ipaddress.ip_address(literal.split("%")[0])
    except ValueError:
        if ":" in literal:
            return False, "host must be a bare hostname or IP address"
    else:
        reason = _classify(ip, block_private=block_private)
        return (False, reason) if reason else (True, "ok")
    name = host.lower().rstrip(".")
    if name in _LOCALHOST_NAMES or name.endswith(".localhost"):
        if block_private:
            return False, f"private/shared/loopback address blocked: {host}"
        return True, "ok"
    try:
        ips = (resolver or _default_resolver)(host)
        if not ips:
            return False, "host does not resolve to an IP"
        saw_ip = False
        for raw in ips:
            # Malformed answers fail closed, including mixed valid/invalid ones.
            ip = ipaddress.ip_address(raw.split("%")[0])
            saw_ip = True
            reason = _classify(ip, block_private=block_private)
            if reason:
                return False, reason
    except Exception as exc:
        return False, f"host does not resolve to valid IP addresses: {exc}"
    if not saw_ip:
        return False, "host does not resolve to an IP"
    return True, "ok"
class OutboundAddressBlocked(PermissionError):
    """A non-HTTP outbound host resolves into a disallowed address range."""


def connect_outbound_tcp(
    host: str,
    port: int,
    *,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    block_private: bool = False,
    source_address=None,
    resolver: Optional[Callable[..., list]] = None,
) -> socket.socket:
    """Open a TCP connection to *host* under the outbound address policy.

    For raw TCP clients (IMAP/SMTP). The host is resolved exactly once, every
    resolved address is judged with the same policy as check_outbound_url, and
    the socket connects only to those already-judged addresses. A second
    lookup at connect time (what socket.create_connection(host) does) would let
    a rebinding name pass the check with a public answer and then connect to
    metadata/private space. TLS callers keep wrapping the returned socket with
    ``server_hostname=host``, so SNI and certificate checks are unchanged.

    An injected resolver must implement socket.getaddrinfo's argument and
    result contract. Resolver errors propagate without an unvetted retry.
    """
    if isinstance(port, bool) or not isinstance(port, (int, str)):
        raise ValueError("port must be an integer between 1 and 65535")
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    resolve = resolver or socket.getaddrinfo
    infos = resolve(host, port, 0, socket.SOCK_STREAM)
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(str(sockaddr[0]).split("%")[0])
        reason = _classify(ip, block_private=block_private)
        if reason:
            raise OutboundAddressBlocked(reason)
    last_error: Optional[OSError] = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # same contract as create_connection
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    if last_error is not None:
        raise last_error
    raise socket.gaierror(socket.EAI_NONAME, f"{host} did not resolve to an address")
