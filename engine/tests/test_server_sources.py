"""`POST /v1/sources/fetch`: the route in front of the guarded link fetch.

The fetch itself is faked at the app seam; `test_download_link.py` pins
what it will reach. These tests pin the wire contract.
"""

import pytest
from conftest import AUTH, TOKEN
from fastapi.testclient import TestClient

from readily_engine.download.link import LinkError, Page
from readily_engine.server.app import create_app


def client_for(fetch) -> TestClient:
    return TestClient(create_app(token=TOKEN, fetch_link=fetch))


def test_returns_the_page_bytes_and_type_it_fetched() -> None:
    asked: list[str] = []

    def fetch(url: str) -> Page:
        asked.append(url)
        return Page(b"<p>caf\xe9</p>", "text/html; charset=iso-8859-1")

    response = client_for(fetch).post(
        "/v1/sources/fetch", json={"url": "https://example.com/"}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.content == b"<p>caf\xe9</p>"
    assert response.headers["content-type"] == "text/html; charset=iso-8859-1"
    assert asked == ["https://example.com/"]


def test_does_not_name_a_charset_the_page_did_not() -> None:
    response = client_for(lambda url: Page(b"<p>Tide</p>", "text/html")).post(
        "/v1/sources/fetch", json={"url": "https://example.com/"}, headers=AUTH
    )

    assert response.headers["content-type"] == "text/html"


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("refused", 422, "link_refused"),
        ("unreachable", 502, "link_unreachable"),
        ("too_large", 502, "link_too_large"),
        ("not_a_page", 502, "link_not_a_page"),
    ],
)
def test_says_why_a_link_was_not_read(kind, status: int, code: str) -> None:
    def fetch(url: str) -> Page:
        raise LinkError(kind)

    response = client_for(fetch).post(
        "/v1/sources/fetch", json={"url": "https://example.com/"}, headers=AUTH
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    "body", [{}, {"url": ""}, {"url": "https://example.com/", "follow": True}]
)
def test_refuses_a_malformed_body_without_fetching(body: dict[str, object]) -> None:
    def fetch(url: str) -> Page:
        raise AssertionError("fetched")

    response = client_for(fetch).post("/v1/sources/fetch", json=body, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_needs_the_launch_token() -> None:
    def fetch(url: str) -> Page:
        raise AssertionError("fetched")

    response = client_for(fetch).post(
        "/v1/sources/fetch", json={"url": "https://example.com/"}
    )

    assert response.status_code == 401
