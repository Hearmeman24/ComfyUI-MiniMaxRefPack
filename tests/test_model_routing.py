"""Which model id each provider sends, and the guarantee that they cannot cross.

The bug this file exists to prevent, hit live against OpenRouter 2026-08-17:

    prompt generation failed: openrouter returned 400:
    google/gemma-4-e2b is not a valid model ID

`model_override` was a single generic field that won over the dropdown for EVERY
provider. Set up a local run, switch back to openrouter to compare, and the local slug
was still sitting there and still won. The fix is structural rather than defensive: each
provider reads its own field and cannot see the other's, so there is no ordering or
precedence rule left to get wrong.
"""

import sys
import types

import pytest

from minimax_refpack import nodes, refs


@pytest.fixture
def env(tmp_path, monkeypatch):
    module = types.ModuleType("folder_paths")
    module.get_input_directory = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "folder_paths", module)
    return tmp_path


@pytest.fixture
def sent(monkeypatch):
    """The kwargs write_prompt was called with."""
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return "a prompt"

    monkeypatch.setattr(nodes.prompt, "write_prompt", fake, raising=False)
    return seen


def _build(**kw):
    return nodes.MiniMaxH3ReferencePack().build(**kw)


# ---- the two fields never cross --------------------------------------------------


def test_openrouter_sends_the_dropdown_and_ignores_the_local_slug(env, sent):
    """The exact live failure: a leftover local slug must not reach OpenRouter."""
    _build(
        direction="d", prompt_provider="openrouter",
        openrouter_model="google/gemini-3-flash-preview",
        local_model_slug="google/gemma-4-e2b",
        api_base="http://127.0.0.1:1234/v1",
    )
    assert sent["model"] == "google/gemini-3-flash-preview"


def test_local_sends_the_slug_and_ignores_the_dropdown(env, sent):
    _build(
        direction="d", prompt_provider="local",
        openrouter_model="google/gemini-3-flash-preview",
        local_model_slug="google/gemma-4-e2b",
        api_base="http://127.0.0.1:1234/v1",
    )
    assert sent["model"] == "google/gemma-4-e2b"


def test_switching_provider_back_and_forth_never_carries_a_model_across(env, sent):
    """The user's actual workflow: configure local, switch to openrouter, run."""
    common = dict(
        direction="d", openrouter_model="google/gemini-3-flash-preview",
        local_model_slug="google/gemma-4-e2b", api_base="http://127.0.0.1:1234/v1",
    )
    _build(prompt_provider="local", **common)
    assert sent["model"] == "google/gemma-4-e2b"
    sent.clear()
    _build(prompt_provider="openrouter", **common)
    assert sent["model"] == "google/gemini-3-flash-preview"
    assert "gemma" not in sent["model"]


# ---- local with nothing typed ----------------------------------------------------


def test_local_with_no_slug_fails_with_a_readable_error(env, sent):
    """The dropdown lists OpenRouter models a local server has never heard of, so
    silently falling back to it would 404 with someone else's model id."""
    with pytest.raises(ValueError) as e:
        _build(
            direction="d", prompt_provider="local",
            openrouter_model="google/gemini-3-flash-preview",
            local_model_slug="", api_base="http://127.0.0.1:1234/v1",
        )
    msg = str(e.value)
    assert "local_model_slug" in msg
    assert not sent, "no call may be made when there is no model to call"


def test_whitespace_only_slug_counts_as_empty(env, sent):
    with pytest.raises(ValueError):
        _build(
            direction="d", prompt_provider="local", openrouter_model="m",
            local_model_slug="   ", api_base="http://127.0.0.1:1234/v1",
        )


# ---- none makes no call at all ---------------------------------------------------


def test_provider_none_needs_no_model_of_either_kind(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("provider=none must not call write_prompt")

    monkeypatch.setattr(nodes.prompt, "write_prompt", boom, raising=False)
    out = _build(direction="straight through", prompt_provider="none",
                 openrouter_model="", local_model_slug="")
    assert out[refs.slot_index("prompt")] == "straight through"


# ---- old names still work --------------------------------------------------------


def test_a_legacy_model_kwarg_still_drives_openrouter(env, sent):
    """An API client posting a stored 0.3.1 prompt sends `model`, not `openrouter_model`."""
    _build(direction="d", prompt_provider="openrouter", model="google/gemini-3-flash-preview")
    assert sent["model"] == "google/gemini-3-flash-preview"


def test_a_legacy_model_override_still_drives_local(env, sent):
    _build(direction="d", prompt_provider="local", model="ignored",
           model_override="google/gemma-4-e2b", api_base="http://127.0.0.1:1234/v1")
    assert sent["model"] == "google/gemma-4-e2b"


def test_the_new_name_beats_the_legacy_one_when_both_arrive(env, sent):
    _build(direction="d", prompt_provider="openrouter",
           openrouter_model="new/model", model="old/model")
    assert sent["model"] == "new/model"


# ---- the widget contract ---------------------------------------------------------


def test_the_widgets_are_named_and_ordered_as_declared():
    spec = nodes.MiniMaxH3ReferencePack.INPUT_TYPES()
    assert list(spec["required"]) == ["direction"]
    # Grouped by decision flow (Aviv, 2026-08-17): the mode, then that mode's settings,
    # then what to write, then the target video, then reference prep. This order is a
    # WIRE FORMAT - widgets_values is positional - so changing it means updating
    # ORDER_0_3_3 in web/refpack.js too, which test_migration.py asserts.
    assert list(spec["required"]) + list(spec["optional"]) == [
        "direction", "references_json", "system_prompt", "prompt_provider",
        "openrouter_api_key", "openrouter_model", "reasoning_effort", "api_base",
        "local_model_slug", "job_type", "width", "height", "length_seconds",
        "max_reference_edge",
    ]


def test_the_debug_header_names_the_field_that_was_actually_used(env, sent):
    out = _build(direction="d", prompt_provider="local", openrouter_model="dropdown/model",
                 local_model_slug="local/model", api_base="http://127.0.0.1:1234/v1")
    debug = out[refs.slot_index("debug")]
    assert "local/model" in debug
    assert "dropdown/model" not in debug
