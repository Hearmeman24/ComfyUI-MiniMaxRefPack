"""Transport: where a call actually goes, and what credential rides with it.

The headline test here is `test_nothing_reaches_openrouter_on_a_local_run`. Before this
work `job_type` defaulted to "auto", "auto" called `classify_mode`, and `classify_mode`
hardcoded OpenRouter's URL and swallowed every exception - so a run the user believed was
local posted their direction text and their key to openrouter.ai and said nothing about
it. That is the regression this file exists to prevent coming back.
"""

import pytest

from minimax_refpack import endpoint as ep
from minimax_refpack import prompt
from minimax_refpack.refs import Reference, ReferenceSet


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {
            "choices": [{"message": {"content": "written"}}]
        }
        self.text = "ok"
        self.headers = {}

    def json(self):
        return self._body


@pytest.fixture
def calls(monkeypatch):
    """Every outbound POST, captured."""
    seen = []

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        seen.append({"url": url, "headers": headers or {}, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(prompt.requests, "post", fake_post)
    return seen


def _mixed_set(tmp_path):
    """One video + one image: the only shape that reaches the auto classifier."""
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.mp4").write_bytes(b"x")
    return ReferenceSet([Reference(file="a.png", kind="image"),
                         Reference(file="b.mp4", kind="video")])


# ---- the leak ------------------------------------------------------------------


def test_nothing_reaches_openrouter_on_a_local_run(calls, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-paid-key")
    monkeypatch.setattr(prompt, "_build_content", lambda *a, **k: [{"type": "text", "text": "t"}])

    prompt.write_prompt(
        references=_mixed_set(tmp_path), input_dir=str(tmp_path), direction="swap her in",
        api_key="", model="local-model", job_type="auto",
        provider="local", api_base="http://localhost:1234/v1",
    )

    # Two calls, not one: the classifier AND the writer. If this drops to one the test has
    # stopped exercising the path that used to leak, and would pass for the wrong reason.
    assert len(calls) == 2, f"expected classifier + writer, got {len(calls)}"
    for call in calls:
        assert "openrouter.ai" not in call["url"], f"leaked to {call['url']}"
        assert "sk-paid-key" not in str(call["headers"])


def test_an_ambient_openrouter_key_is_never_sent_to_a_local_server(calls, tmp_path, monkeypatch):
    """api_base is free text. An env key must not follow it to whatever host is typed."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-paid-key")
    monkeypatch.setattr(prompt, "_build_content", lambda *a, **k: [{"type": "text", "text": "t"}])

    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
        api_key="", model="m", job_type="standard",
        provider="local", api_base="http://evil.example/v1",
    )

    for call in calls:
        assert "sk-paid-key" not in str(call["headers"])
        assert "Authorization" not in call["headers"]


def test_a_key_typed_into_the_node_is_still_sent_to_a_local_server(calls, tmp_path):
    """vLLM started with --api-key wants one, so the box must still work."""
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
        api_key="typed-key", model="m", job_type="standard",
        provider="local", api_base="http://localhost:8000/v1",
    )
    assert calls[0]["headers"]["Authorization"] == "Bearer typed-key"


def test_the_classifier_uses_the_writers_model_off_openrouter(calls, tmp_path):
    """A Gemini model id means nothing to a server holding one local model."""
    prompt.classify_mode(
        direction="swap the man for the robot", references=_mixed_set(tmp_path),
        api_key="k", model="qwen2.5-vl:7b",
        ep=ep.resolve("local", "http://localhost:1234/v1"),
    )
    assert calls[0]["json"]["model"] == "qwen2.5-vl:7b"
    assert "openrouter.ai" not in calls[0]["url"]


def test_the_classifier_declines_rather_than_posting_an_empty_model(calls, tmp_path):
    out = prompt.classify_mode(
        direction="swap the man for the robot", references=_mixed_set(tmp_path),
        api_key="k", model="", ep=ep.resolve("local", "http://localhost:1234/v1"),
    )
    assert out == "standard"
    assert calls == []


# ---- openrouter is unchanged ---------------------------------------------------


def test_openrouter_still_goes_to_openrouter_with_todays_timeout(calls, tmp_path):
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
        api_key="k", model="m", job_type="standard",
    )
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert calls[0]["timeout"] == 120
    assert calls[0]["headers"]["Authorization"] == "Bearer k"


def test_openrouter_still_sends_reasoning_and_local_does_not(calls, tmp_path):
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
        api_key="k", model="m", job_type="standard", reasoning_effort="high",
    )
    assert calls[0]["json"]["reasoning"] == {"effort": "high"}

    calls.clear()
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
        api_key="k", model="m", job_type="standard", reasoning_effort="high",
        provider="local", api_base="http://localhost:1234/v1",
    )
    assert "reasoning" not in calls[0]["json"]


def test_a_local_run_waits_far_longer(calls, tmp_path):
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
        api_key="", model="m", job_type="standard",
        provider="local", api_base="http://localhost:1234/v1",
    )
    assert calls[0]["timeout"] >= 900


def test_a_local_run_needs_no_key_anywhere(tmp_path, calls, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_KEY", raising=False)
    out = prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
        api_key="", model="m", job_type="standard",
        provider="local", api_base="http://localhost:1234/v1",
    )
    assert out == "written"


def test_openrouter_still_demands_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_KEY", raising=False)
    with pytest.raises(prompt.PromptError):
        prompt.write_prompt(
            references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
            api_key="", model="m", job_type="standard",
        )


# ---- offline behaviour ---------------------------------------------------------


def test_the_model_list_makes_no_network_call_on_a_local_endpoint(monkeypatch):
    """This runs inside INPUT_TYPES; an offline user must not pay a timeout for it."""
    def explode(*a, **k):
        raise AssertionError("available_models must not hit the network for a local endpoint")

    monkeypatch.setattr(prompt.requests, "get", explode)
    models = prompt.available_models(ep.resolve("local", "http://localhost:1234/v1"))
    assert models == [prompt.DEFAULT_MODEL]


# ---- failure reporting ---------------------------------------------------------


def test_a_timeout_names_the_endpoint_and_the_limit(monkeypatch, tmp_path):
    def timeout(*a, **k):
        raise prompt.requests.Timeout("slow")

    monkeypatch.setattr(prompt.requests, "post", timeout)
    with pytest.raises(prompt.PromptError) as e:
        prompt.write_prompt(
            references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
            api_key="", model="m", job_type="standard",
            provider="local", api_base="http://localhost:1234/v1",
        )
    msg = str(e.value)
    assert "900" in msg
    assert "localhost:1234" in msg


def test_a_transport_error_never_carries_the_underlying_message(monkeypatch, tmp_path):
    """The exception is out of our control and could hold the Authorization header."""
    def boom(*a, **k):
        raise prompt.requests.ConnectionError("Bearer sk-should-never-appear")

    monkeypatch.setattr(prompt.requests, "post", boom)
    with pytest.raises(prompt.PromptError) as e:
        prompt.write_prompt(
            references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
            api_key="k", model="m", job_type="standard",
        )
    assert "sk-should-never-appear" not in str(e.value)
    assert "ConnectionError" in str(e.value)


# ---- reasoning models that answer with nothing ---------------------------------


def test_a_reasoning_only_answer_says_so_instead_of_empty_completion(monkeypatch, tmp_path):
    """Observed for real on LM Studio + gemma-4-e2b 2026-08-17: the model spent its whole
    budget in reasoning_content and returned content="". "empty completion" would send
    the user hunting an outage that never happened.
    """
    body = {"choices": [{"message": {"content": "", "reasoning_content": "thinking..."}}]}
    monkeypatch.setattr(prompt.requests, "post", lambda *a, **k: FakeResponse(200, body))
    with pytest.raises(prompt.PromptError) as e:
        prompt.write_prompt(
            references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
            api_key="", model="m", job_type="standard",
            provider="local", api_base="http://localhost:1234/v1",
        )
    msg = str(e.value)
    assert "only reasoning" in msg
    assert "token limit" in msg


def test_a_genuinely_empty_answer_still_reads_as_empty(monkeypatch, tmp_path):
    body = {"choices": [{"message": {"content": "   "}}]}
    monkeypatch.setattr(prompt.requests, "post", lambda *a, **k: FakeResponse(200, body))
    with pytest.raises(prompt.PromptError) as e:
        prompt.write_prompt(
            references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
            api_key="k", model="m", job_type="standard",
        )
    assert "empty completion" in str(e.value)


def test_failures_name_the_endpoint_the_user_actually_used(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.requests, "post", lambda *a, **k: FakeResponse(500, {"error": {"message": "boom"}}))
    with pytest.raises(prompt.PromptError) as e:
        prompt.write_prompt(
            references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
            api_key="", model="m", job_type="standard",
            provider="local", api_base="http://localhost:1234/v1",
        )
    assert "localhost:1234" in str(e.value)
    assert "OpenRouter" not in str(e.value)
