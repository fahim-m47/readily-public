"""The localhost API is token-guarded: every route requires the per-launch
bearer token, browser origins outside the webview are rejected, and
non-HTTP protocols are refused (threat model B3). No route is exempt,
/health included."""

import pytest
from conftest import AUTH, TOKEN
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from readily_engine.server.app import create_app


def client() -> TestClient:
    return TestClient(create_app(token=TOKEN))


def test_health_without_token_is_401():
    response = client().get("/health")
    assert response.status_code == 401


def test_health_with_wrong_token_is_401():
    response = client().get("/health", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_health_with_malformed_authorization_is_401():
    response = client().get("/health", headers={"Authorization": TOKEN})
    assert response.status_code == 401


def test_health_with_token_answers():
    response = client().get("/health", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unknown_route_with_token_is_404_without_token_is_401():
    # Auth is checked before routing: an unauthenticated probe learns nothing
    # about which routes exist.
    assert client().get("/nope").status_code == 401
    assert client().get("/nope", headers=AUTH).status_code == 404


def test_browser_origin_outside_webview_is_403():
    # A malicious web page can't forge its Origin — even with the token,
    # a foreign browser origin is rejected.
    headers = {**AUTH, "Origin": "https://evil.example"}
    assert client().get("/health", headers=headers).status_code == 403


def test_webview_origin_is_allowed():
    headers = {**AUTH, "Origin": "tauri://localhost"}
    assert client().get("/health", headers=headers).status_code == 200


def test_a_shipped_app_does_not_trust_vites_origin(monkeypatch):
    # A release build is served from its bundle. If it trusted
    # http://127.0.0.1:1420, any page the user's own machine serves there
    # would be a permitted origin for the life of the product.
    monkeypatch.delenv("READILY_ENGINE_ALLOW_DEV_ORIGIN", raising=False)
    headers = {**AUTH, "Origin": "http://127.0.0.1:1420"}

    assert client().get("/health", headers=headers).status_code == 403


def test_a_debug_supervisor_may_admit_vites_origin(monkeypatch):
    # Only the supervisor says so, and only a debug build of it does.
    monkeypatch.setenv("READILY_ENGINE_ALLOW_DEV_ORIGIN", "1")
    headers = {**AUTH, "Origin": "http://127.0.0.1:1420"}

    assert client().get("/health", headers=headers).status_code == 200


def test_foreign_origin_without_token_is_401_not_403():
    # Token before origin: an unauthenticated probe learns nothing about
    # the origin policy.
    headers = {"Origin": "https://evil.example"}
    assert client().get("/health", headers=headers).status_code == 401


def test_history_routes_require_the_bearer_token():
    unauthenticated = client()
    assert unauthenticated.get("/v1/history").status_code == 401
    assert unauthenticated.get("/v1/history/n-1").status_code == 401
    assert unauthenticated.post("/v1/history/n-1/resume").status_code == 401
    assert unauthenticated.post("/v1/history/n-1/export").status_code == 401
    assert unauthenticated.get("/v1/export/events").status_code == 401


def test_websocket_handshake_is_refused():
    # "Every route requires the token" must hold for protocols too: the
    # middleware refuses ws handshakes outright rather than passing them
    # through unauthenticated.
    with pytest.raises(WebSocketDisconnect), client().websocket_connect("/health"):
        pass
