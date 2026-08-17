"""Local-server detection, and the SSRF guard on the route that exposes it.

The security half matters more than the feature half. This process will connect to
whatever it is told to, and ComfyUI is routinely run with `--listen` and no auth, so an
unrestricted detect route is a port scanner for anyone holding the URL. The rule is
loopback-only, and never resolved.
"""

import asyncio
import json

import pytest

from minimax_refpack import endpoint, prompt, routes


class FakeRequest:
    def __init__(self, **query):
        self.query = query


def _json(resp):
    return json.loads(resp.body.decode())


def run(coro):
    """Matches tests/test_routes.py - this repo has no pytest-asyncio."""
    return asyncio.run(coro)


# ---- the loopback guard --------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1234/v1",
    "http://localhost:1234/v1",
    "http://LOCALHOST:1234/v1",
    "https://127.0.0.1:8443/v1",
    "http://[::1]:1234/v1",
])
def test_loopback_addresses_are_allowed(url):
    assert endpoint.is_loopback(url) is True


@pytest.mark.parametrize("url", [
    "http://192.168.1.50:1234/v1",       # LAN
    "http://10.0.0.5:1234/v1",           # LAN
    "http://169.254.169.254/v1",         # cloud metadata, the classic SSRF target
    "http://evil.example/v1",
    "http://127.0.0.1.evil.example/v1",  # prefix that only LOOKS like loopback
    "http://localhost.evil.example/v1",
    "file:///etc/passwd",
    "ftp://127.0.0.1/v1",
    "",
    "not a url",
])
def test_everything_else_is_refused(url):
    assert endpoint.is_loopback(url) is False


def test_a_hostname_that_would_resolve_to_loopback_is_still_refused():
    """Refusing to resolve at all is what closes DNS rebinding. A name that points at
    127.0.0.1 today can point somewhere else on the next lookup."""
    assert endpoint.is_loopback("http://my-local-box.internal/v1") is False


# ---- the route -----------------------------------------------------------------


def test_the_route_refuses_a_non_loopback_base_without_connecting(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("no connection may be attempted for a refused host")

    monkeypatch.setattr(prompt, "_models_at", explode)
    resp = run(routes.detect_route(FakeRequest(base="http://169.254.169.254/v1")))
    assert resp.status == 400
    assert "loopback" in _json(resp)["error"]


def test_the_route_probes_an_explicit_loopback_base(monkeypatch):
    monkeypatch.setattr(prompt, "_models_at", lambda base, *a, **k: ["m1", "m2"])
    resp = run(routes.detect_route(FakeRequest(base="http://127.0.0.1:9999/v1")))
    body = _json(resp)
    assert body["servers"][0]["base"] == "http://127.0.0.1:9999/v1"
    assert body["servers"][0]["models"] == ["m1", "m2"]


def test_a_loopback_base_with_nothing_listening_returns_no_servers(monkeypatch):
    monkeypatch.setattr(prompt, "_models_at", lambda base, *a, **k: None)
    resp = run(routes.detect_route(FakeRequest(base="http://127.0.0.1:9999/v1")))
    assert _json(resp)["servers"] == []


def test_with_no_base_it_sweeps_the_known_candidates(monkeypatch):
    monkeypatch.setattr(prompt, "detect_local_servers",
                        lambda *a, **k: [{"label": "LM Studio",
                                          "base": "http://127.0.0.1:1234/v1",
                                          "models": ["gemma"]}])
    resp = run(routes.detect_route(FakeRequest()))
    assert _json(resp)["servers"][0]["label"] == "LM Studio"


# ---- what counts as a server ---------------------------------------------------


def test_a_port_that_answers_with_the_wrong_shape_is_not_a_server(monkeypatch):
    """8000 and 8080 are shared with most dev tooling. "Something answered" proves
    nothing, so the response has to look like an OpenAI model list."""
    class Resp:
        status_code = 200

        def json(self):
            return {"message": "hello from some unrelated dev server"}

    monkeypatch.setattr(prompt.requests, "get", lambda *a, **k: Resp())
    assert prompt._models_at("http://127.0.0.1:8080/v1") is None


def test_a_non_200_is_not_a_server(monkeypatch):
    class Resp:
        status_code = 404

        def json(self):
            return {"data": []}

    monkeypatch.setattr(prompt.requests, "get", lambda *a, **k: Resp())
    assert prompt._models_at("http://127.0.0.1:8080/v1") is None


def test_a_closed_port_is_not_a_server(monkeypatch):
    def refused(*a, **k):
        raise prompt.requests.ConnectionError("refused")

    monkeypatch.setattr(prompt.requests, "get", refused)
    assert prompt._models_at("http://127.0.0.1:9/v1") is None


def test_a_real_server_with_nothing_loaded_is_still_a_server(monkeypatch):
    """An empty list is a real answer and must not read as "no server"."""
    class Resp:
        status_code = 200

        def json(self):
            return {"data": []}

    monkeypatch.setattr(prompt.requests, "get", lambda *a, **k: Resp())
    assert prompt._models_at("http://127.0.0.1:1234/v1") == []


def test_malformed_entries_are_skipped_not_fatal(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "good"}, {"no_id": 1}, "not-a-dict", {"id": 42}]}

    monkeypatch.setattr(prompt.requests, "get", lambda *a, **k: Resp())
    assert prompt._models_at("http://127.0.0.1:1234/v1") == ["good"]


# ---- the sweep -----------------------------------------------------------------


def test_the_sweep_reports_only_what_answered_and_keeps_candidate_order(monkeypatch):
    answers = {
        "http://127.0.0.1:1234/v1": ["gemma"],
        "http://127.0.0.1:11434/v1": ["qwen"],
    }
    monkeypatch.setattr(prompt, "_models_at", lambda base, *a, **k: answers.get(base))
    found = prompt.detect_local_servers()
    assert [f["base"] for f in found] == [
        "http://127.0.0.1:1234/v1", "http://127.0.0.1:11434/v1",
    ]
    assert found[0]["label"] == "LM Studio"
    assert found[1]["label"] == "Ollama"


def test_the_sweep_survives_every_port_being_closed(monkeypatch):
    monkeypatch.setattr(prompt, "_models_at", lambda *a, **k: None)
    assert prompt.detect_local_servers() == []


def test_the_candidate_list_is_loopback_only():
    """A candidate pointing off-box would make the no-argument sweep a scanner."""
    for _label, base in endpoint.LOCAL_CANDIDATES:
        assert endpoint.is_loopback(base), base
