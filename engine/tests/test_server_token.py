"""Per-launch token sourcing: 128 bits, from the supervisor's environment —
never argv — with a generated fallback for standalone runs (threat model B3)."""

from readily_engine.server.auth import launch_token, new_token


def test_new_token_is_128_bit_hex():
    token = new_token()
    assert len(token) == 32
    int(token, 16)  # raises if not hex


def test_launch_token_prefers_supervisor_environment(monkeypatch):
    monkeypatch.setenv("READILY_ENGINE_TOKEN", "t" * 32)
    assert launch_token() == "t" * 32


def test_launch_token_generates_and_announces_when_unset(monkeypatch, capsys):
    monkeypatch.delenv("READILY_ENGINE_TOKEN", raising=False)
    token = launch_token()
    int(token, 16)
    assert token in capsys.readouterr().err
