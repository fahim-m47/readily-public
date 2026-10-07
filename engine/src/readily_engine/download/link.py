"""Fetch one web page a reader asked to read: the Engine's second egress.

The webview cannot reach the internet (its CSP allows only the Engine), so
"Open link" asks the Engine to fetch the page and hands the bytes back for
the webview to turn into text. Nothing the reader typed or narrated is sent:
the request is a bare GET for the URL they gave (threat model, egress
inventory row 5).

The URL comes from the reader, so it is the one outbound request whose
destination the Engine does not choose, and everything here is about not
letting it name the reader's own machine or network (SSRF): https only,
no credentials in the URL, one DNS answer checked for public addresses and
then connected to directly, so a second lookup cannot rebind the name to
127.0.0.1, and every redirect checked the same way. What comes back is
capped in size and time, and must be an HTML or plain-text page.

Standard library only: `http.client` reads no proxy settings and follows no
redirects on its own, which is what lets each hop be checked here.
"""

import http.client
import io
import ipaddress
import re
import socket
import ssl
import time
from collections.abc import Buffer, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urljoin, urlsplit

# The largest page body read, in bytes; a larger one is refused rather
# than cut short, since a truncated article reads as a finished one.
BODY_LIMIT = 4 * 1024 * 1024
MAX_REDIRECTS = 5
# Every wait on the socket (connecting, the TLS handshake, sending, each
# read of the headers or the body) lasts at most the shorter of these two:
# the limit on one wait, and what is left of the whole fetch's budget,
# redirects included. A server that drips a byte per wait therefore cannot
# hold an Engine worker past TOTAL_SECONDS. The DNS lookup is the one wait
# outside the budget: the OS resolver bounds it with its own retry timeouts,
# and a lookup that outlives the budget fails the fetch as it returns.
TIMEOUT_SECONDS = 15
TOTAL_SECONDS = 30
PAGE_TYPES = frozenset({"text/html", "text/plain"})
REDIRECTS = frozenset({301, 302, 303, 307, 308})
# The statuses under which the body is the whole page: 200, and 203 when a
# proxy transformed it on the way. No other 2xx is: the request never sends
# Range, so a 206 is a server answering something else, and 201, 202, 204,
# 205 and 226 carry no page at all.
PAGE_STATUSES = frozenset({200, 203})
USER_AGENT = "Readily (+https://github.com/fahim-m47/readily-public)"
CHUNK_BYTES = 64 * 1024
CHARSET = re.compile(r"[a-z0-9][a-z0-9._:-]{0,39}")

LinkFailure = Literal["refused", "unreachable", "too_large", "not_a_page"]


class LinkError(Exception):
    """Why a link was not read. `refused` means no connection was made to
    it: not https, credentials in it, or an address off the public
    internet. The rest describe what the far end did."""

    def __init__(self, kind: LinkFailure) -> None:
        super().__init__(kind)
        self.kind: LinkFailure = kind


@dataclass(frozen=True)
class Page:
    body: bytes
    # "text/html" or "text/plain", with the page's charset when it named a
    # well-formed one.
    content_type: str


class Response(Protocol):
    status: int
    # What is left of the body's declared length, as `http.client` keeps
    # it: None for a chunked body (whose Content-Length is ignored) or one
    # with no usable Content-Length, and counted down as `read1` consumes.
    length: int | None

    def getheader(self, name: str, default: str | None = None) -> str | None: ...

    def read1(self, amount: int) -> bytes: ...

    def close(self) -> None: ...


class Connection(Protocol):
    def request(self, method: str, url: str, headers: dict[str, str]) -> None: ...

    def getresponse(self) -> Response: ...

    def close(self) -> None: ...


Resolver = Callable[[str, int], Sequence[str]]
# Opens a connection to (host, address, port) whose every wait ends by the
# `time.monotonic()` deadline.
Connector = Callable[[str, str, int, float], Connection]


def _remaining(deadline: float) -> float:
    """How long the next wait on the socket may last, or `TimeoutError`
    once the fetch's budget is spent."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("the fetch's budget is spent")
    return min(TIMEOUT_SECONDS, remaining)


class _DeadlineReader(io.RawIOBase):
    """Reads from `sock` through its own unbuffered `makefile`, each read
    waiting at most what is left of the budget. The makefile rather than
    `recv_into` because it holds the socket open past `sock.close()`, as
    `http.client` counts on: when the server will close the connection, it
    hands the socket to the response and closes the connection at once,
    and the body is read after that."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self.sock = sock
        self.raw = sock.makefile("rb", buffering=0)
        self.deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int | None:
        self.sock.settimeout(_remaining(self.deadline))
        return self.raw.readinto(buffer)

    def close(self) -> None:
        self.raw.close()
        super().close()


class _DeadlineSocket:
    """`sock` as `http.client` uses one (`sendall`, `makefile`, `close`),
    with every wait ending by `deadline`. Setting the socket's timeout once
    before a phase would not do: `http.client` reads the headers in as many
    waits as the server takes to send them, and each would start afresh.
    `close` drops only this connection's hold on the socket, as a plain
    socket's does; a `makefile` still reading keeps it open."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self.sock = sock
        self.deadline = deadline

    def sendall(self, data: Buffer) -> None:
        self.sock.settimeout(_remaining(self.deadline))
        self.sock.sendall(data)

    def makefile(self, mode: Literal["rb"]) -> io.BufferedReader:
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline))

    def close(self) -> None:
        self.sock.close()


class _CheckedConnection(http.client.HTTPSConnection):
    """An HTTPS connection to an address `fetch_page` has already checked:
    TCP goes to that address, and TLS still verifies the certificate
    against the hostname, which is also sent as SNI and Host. Connecting,
    the handshake and everything after wait no longer than `deadline`."""

    def __init__(self, host: str, address: str, port: int, deadline: float) -> None:
        self.tls = ssl.create_default_context()
        super().__init__(host, port, context=self.tls)
        self.address = address
        self.deadline = deadline

    def connect(self) -> None:
        raw = socket.create_connection(
            (self.address, self.port), _remaining(self.deadline)
        )
        try:
            # The handshake runs inside `wrap_socket`, under the timeout the
            # raw socket carries at that moment.
            raw.settimeout(_remaining(self.deadline))
            secure = self.tls.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
        self.sock = _DeadlineSocket(secure, self.deadline)


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        # An address that carries an IPv4 one is judged by both.
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is not None and not embedded.is_global:
            return False
    return ip.is_global


def _target(url: str) -> tuple[str, int, str]:
    """The host, port and request target of a URL the Engine may fetch, or
    `refused`."""
    try:
        parts = urlsplit(url)
        port = parts.port or 443
        host = parts.hostname
    except ValueError:
        raise LinkError("refused") from None
    if parts.scheme != "https" or not host:
        raise LinkError("refused")
    if parts.username is not None or parts.password is not None:
        raise LinkError("refused")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise LinkError("refused") from None
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return host, port, target


def _address(host: str, port: int, resolve: Resolver) -> str:
    """The one address to connect to: `host` itself when it is an address,
    or the first of its DNS answers, all of which must be public."""
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = list(resolve(host, port))
        except (OSError, UnicodeError):
            raise LinkError("unreachable") from None
    try:
        public = bool(addresses) and all(_is_public(a) for a in addresses)
    except ValueError:
        public = False
    if not public:
        raise LinkError("refused")
    return addresses[0]


def _media_type(header: str) -> str:
    """The page's type and charset, rebuilt from their parts so nothing else
    in the upstream header reaches the webview."""
    kind, *params = header.split(";")
    content_type = kind.strip().lower()
    for param in params:
        name, _, value = param.partition("=")
        charset = value.strip().strip('"').lower()
        if name.strip().lower() == "charset" and CHARSET.fullmatch(charset):
            return f"{content_type}; charset={charset}"
    return content_type


def _body(response: Response) -> bytes:
    """The whole body: `too_large` past `BODY_LIMIT`, and `unreachable` when
    it stops short of its declared length, since `http.client` reports a
    cut-short chunked body but hands a cut-short length-delimited one back
    as complete, with `response.length` still above zero."""
    if response.length is not None and response.length > BODY_LIMIT:
        raise LinkError("too_large")
    body = bytearray()
    while chunk := response.read1(min(CHUNK_BYTES, BODY_LIMIT + 1 - len(body))):
        body += chunk
        if len(body) > BODY_LIMIT:
            raise LinkError("too_large")
    if response.length is not None and response.length > 0:
        raise LinkError("unreachable")
    return bytes(body)


def fetch_page(
    url: str,
    *,
    resolve: Resolver = _resolve,
    connect: Connector = _CheckedConnection,
) -> Page:
    """GET the page at `url`, following at most `MAX_REDIRECTS` redirects,
    each checked as the first was. Raises `LinkError`; `resolve` and
    `connect` are seams for tests, which must not reach the network."""
    deadline = time.monotonic() + TOTAL_SECONDS
    for _ in range(MAX_REDIRECTS + 1):
        host, port, target = _target(url)
        address = _address(host, port, resolve)
        connection = response = None
        try:
            connection = connect(host, address, port, deadline)
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "text/html, text/plain;q=0.9",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "User-Agent": USER_AGENT,
                },
            )
            response = connection.getresponse()
            location = response.getheader("Location")
            if response.status in REDIRECTS and location:
                url = urljoin(url, location)
                continue
            if response.status not in PAGE_STATUSES:
                raise LinkError("unreachable")
            encoding = (response.getheader("Content-Encoding") or "").strip().lower()
            if encoding not in ("", "identity"):
                raise LinkError("unreachable")
            content_type = _media_type(response.getheader("Content-Type") or "")
            if content_type.partition(";")[0] not in PAGE_TYPES:
                raise LinkError("not_a_page")
            return Page(_body(response), content_type)
        except (OSError, http.client.HTTPException, UnicodeError, ValueError):
            raise LinkError("unreachable") from None
        finally:
            # The response owns the socket once the server will close the
            # connection, so a redirect or a refused page releases it here.
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
    raise LinkError("unreachable")
