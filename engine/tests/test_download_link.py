"""The guarded fetch behind "Open link": what it will reach, and what it
brings back.

The resolver and the connection are faked at the module's seams, so no test
touches the network; each fake records what the fetch asked of it. The tests
of what the fetch does with real bytes on the wire run `http.client` itself
against a server thread on the loopback interface.
"""

import socket
import threading
import time
from collections.abc import Callable

import pytest

from readily_engine.download import link
from readily_engine.download.link import (
    BODY_LIMIT,
    LinkError,
    _CheckedConnection,
    fetch_page,
)

PUBLIC = "93.184.215.14"


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"<p>Tide</p>",
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.body = body
        self.read_bytes = 0
        declared = self.headers.get("Content-Length")
        self.length = int(declared) if declared is not None else None

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def read1(self, amount: int) -> bytes:
        chunk = self.body[self.read_bytes : self.read_bytes + amount]
        self.read_bytes += len(chunk)
        if self.length is not None:
            self.length -= len(chunk)
        return chunk

    def close(self) -> None:
        pass


class FakeWeb:
    """Answers each connection with the next response, and remembers who
    was connected to and what was asked."""

    def __init__(
        self,
        *responses: FakeResponse,
        addresses: dict[str, list[str]] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.addresses = addresses or {}
        self.connections: list[tuple[str, str, int]] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def resolve(self, host: str, port: int) -> list[str]:
        if host not in self.addresses:
            raise OSError("no such host")
        return self.addresses[host]

    def connect(self, host: str, address: str, port: int, deadline: float) -> "FakeWeb":
        self.connections.append((host, address, port))
        return self

    def request(self, method: str, target: str, headers: dict[str, str]) -> None:
        self.requests.append((method, target, headers))

    def getresponse(self) -> FakeResponse:
        return self.responses.pop(0)

    def close(self) -> None:
        pass

    def fetch(self, url: str):
        return fetch_page(url, resolve=self.resolve, connect=self.connect)


def web(*responses: FakeResponse, **addresses: list[str]) -> FakeWeb:
    return FakeWeb(
        *responses,
        addresses={"example.com": [PUBLIC], **addresses},
    )


class NoTLS:
    """Stands in for the fetch's TLS context where the server is the loopback
    one, which has no certificate: the socket goes through unwrapped."""

    def wrap_socket(self, sock: socket.socket, server_hostname: str) -> socket.socket:
        return sock


class LocalServer:
    """A loopback server that answers its one connection the way `serve`
    says, reached through the fetch's own connection class and standing in
    for a public host at the resolver seam."""

    def __init__(self, serve: Callable[[socket.socket], None]) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.answer, args=(serve,), daemon=True).start()

    def answer(self, serve: Callable[[socket.socket], None]) -> None:
        with self.listener, self.listener.accept()[0] as client:
            try:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += client.recv(4096)
                serve(client)
            except OSError:
                pass

    def resolve(self, host: str, port: int) -> list[str]:
        return [PUBLIC]

    def connect(
        self, host: str, address: str, port: int, deadline: float
    ) -> _CheckedConnection:
        connection = _CheckedConnection(host, "127.0.0.1", self.port, deadline)
        connection.tls = NoTLS()
        return connection

    def fetch(self, url: str):
        return fetch_page(url, resolve=self.resolve, connect=self.connect)


def failure(fake: FakeWeb | LocalServer, url: str) -> str:
    with pytest.raises(LinkError) as raised:
        fake.fetch(url)
    return raised.value.kind


def test_reads_a_public_page_through_the_address_it_checked() -> None:
    fake = web(FakeResponse(body=b"<p>Tide</p>"))

    page = fake.fetch("https://example.com/tides?day=1#noon")

    assert page.body == b"<p>Tide</p>"
    assert page.content_type == "text/html; charset=utf-8"
    assert fake.connections == [("example.com", PUBLIC, 443)]
    method, target, headers = fake.requests[0]
    assert (method, target) == ("GET", "/tides?day=1")
    assert headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "ftp://example.com/",
        "https://reader:secret@example.com/",
        "https://reader@example.com/",
        "https:///nothing",
        "https://example.com:99999/",
    ],
)
def test_refuses_anything_but_plain_https(url: str) -> None:
    fake = web()

    assert failure(fake, url) == "refused"
    assert fake.connections == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.8",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "2002:7f00:1::1",
    ],
)
def test_refuses_a_host_that_resolves_off_the_public_internet(address: str) -> None:
    fake = web(**{"inside.example": [address]})

    assert failure(fake, "https://inside.example/") == "refused"
    assert fake.connections == []


def test_refuses_a_host_with_any_private_address_among_its_answers() -> None:
    fake = web(**{"mixed.example": [PUBLIC, "127.0.0.1"]})

    assert failure(fake, "https://mixed.example/") == "refused"
    assert fake.connections == []


def test_refuses_an_address_literal_off_the_public_internet() -> None:
    fake = web()

    assert failure(fake, "https://127.0.0.1:8080/") == "refused"
    assert failure(fake, "https://[::1]/") == "refused"


def test_follows_a_redirect_and_checks_where_it_lands() -> None:
    fake = web(
        FakeResponse(301, {"Location": "/moved"}),
        FakeResponse(302, {"Location": "https://inside.example/admin"}),
        **{"inside.example": ["10.0.0.8"]},
    )

    assert failure(fake, "https://example.com/start") == "refused"
    assert [target for _, target, _ in fake.requests] == ["/start", "/moved"]
    assert len(fake.connections) == 2


def test_refuses_a_redirect_to_plain_http() -> None:
    fake = web(FakeResponse(308, {"Location": "http://example.com/"}))

    assert failure(fake, "https://example.com/") == "refused"


def test_gives_up_after_too_many_redirects() -> None:
    fake = web(*[FakeResponse(302, {"Location": "/again"}) for _ in range(10)])

    assert failure(fake, "https://example.com/") == "unreachable"
    assert len(fake.connections) == 6


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(body=b"x" * (BODY_LIMIT + 1)),
        FakeResponse(headers={"Content-Length": str(BODY_LIMIT + 1)}, body=b""),
    ],
)
def test_refuses_a_page_over_the_size_cap(response: FakeResponse) -> None:
    assert failure(web(response), "https://example.com/") == "too_large"
    assert response.read_bytes <= BODY_LIMIT + 1


def test_reads_a_page_exactly_at_the_cap() -> None:
    page = web(FakeResponse(body=b"x" * BODY_LIMIT)).fetch("https://example.com/")

    assert len(page.body) == BODY_LIMIT


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "image/png", "application/json", "text/css", ""],
)
def test_refuses_what_is_not_a_page_before_reading_it(content_type: str) -> None:
    response = FakeResponse(headers={"Content-Type": content_type})

    assert failure(web(response), "https://example.com/") == "not_a_page"
    assert response.read_bytes == 0


def test_passes_on_plain_text_and_only_a_well_formed_charset() -> None:
    latin = 'TEXT/Plain; Charset="ISO-8859-1"'
    plain = web(FakeResponse(headers={"Content-Type": latin}))
    odd = web(FakeResponse(headers={"Content-Type": 'text/html; charset="a b"'}))

    assert plain.fetch("https://example.com/").content_type == (
        "text/plain; charset=iso-8859-1"
    )
    assert odd.fetch("https://example.com/").content_type == "text/html"


def test_refuses_a_compressed_body_it_did_not_ask_for() -> None:
    response = FakeResponse(headers={"Content-Encoding": "gzip"})

    assert failure(web(response), "https://example.com/") == "unreachable"


@pytest.mark.parametrize("status", [404, 500, 304])
def test_an_error_status_is_unreachable(status: int) -> None:
    assert failure(web(FakeResponse(status)), "https://example.com/") == "unreachable"


def test_a_partial_response_is_unreachable() -> None:
    def partial(client: socket.socket) -> None:
        client.sendall(
            b"HTTP/1.1 206 Partial Content\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-Range: bytes 0-26/500\r\n"
            b"Content-Length: 27\r\n\r\n"
            b"<p>The tide came in and the"
        )

    assert failure(LocalServer(partial), "https://example.com/") == "unreachable"


@pytest.mark.parametrize("status", [201, 202, 204, 205, 226])
def test_a_success_that_is_not_the_page_itself_is_unreachable(status: int) -> None:
    assert failure(web(FakeResponse(status)), "https://example.com/") == "unreachable"


def test_a_page_a_proxy_transformed_still_reads() -> None:
    assert web(FakeResponse(203)).fetch("https://example.com/").body == b"<p>Tide</p>"


def test_a_name_that_does_not_resolve_is_unreachable() -> None:
    fake = web()

    assert failure(fake, "https://nowhere.example/") == "unreachable"
    assert fake.connections == []


def test_a_connection_that_fails_is_unreachable() -> None:
    def refuse(host: str, address: str, port: int, deadline: float):
        raise ConnectionRefusedError

    fake = web()
    with pytest.raises(LinkError) as raised:
        fetch_page("https://example.com/", resolve=fake.resolve, connect=refuse)

    assert raised.value.kind == "unreachable"


def test_a_server_dripping_its_headers_cannot_hold_the_fetch_past_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(link, "TOTAL_SECONDS", 0.2)

    def drip(client: socket.socket) -> None:
        client.sendall(b"HTTP/1.1 200 OK\r\n")
        for byte in b"Content-Type: text/html\r\n\r\n":
            time.sleep(0.05)
            client.sendall(bytes([byte]))
        client.sendall(b"<p>Tide</p>")

    server = LocalServer(drip)
    started = time.monotonic()

    assert failure(server, "https://example.com/") == "unreachable"
    assert time.monotonic() - started < 1


def test_a_body_cut_short_of_its_content_length_is_unreachable() -> None:
    def cut_short(client: socket.socket) -> None:
        client.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-Length: 500\r\n\r\n"
            b"<p>The tide came in and the"
        )

    assert failure(LocalServer(cut_short), "https://example.com/") == "unreachable"


def test_a_chunked_body_cut_short_is_unreachable() -> None:
    def cut_short(client: socket.socket) -> None:
        client.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"1f4\r\n<p>The tide came in and the"
        )

    assert failure(LocalServer(cut_short), "https://example.com/") == "unreachable"


@pytest.mark.parametrize("stale_length", [500_000, BODY_LIMIT + 1])
def test_a_chunked_body_reads_whole_whatever_a_stale_content_length_says(
    stale_length: int,
) -> None:
    body = b"x" * (20 * 1024)

    def chunked(client: socket.socket) -> None:
        client.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-Length: %d\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"%x\r\n%s\r\n0\r\n\r\n" % (stale_length, len(body), body)
        )

    assert LocalServer(chunked).fetch("https://example.com/").body == body


def test_a_page_the_server_closes_the_connection_after_reads_whole() -> None:
    body = b"x" * (64 * 1024)

    def close_after(client: socket.socket) -> None:
        client.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            b"Connection: close\r\n"
            b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
        )

    assert LocalServer(close_after).fetch("https://example.com/").body == body
