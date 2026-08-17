"""The 0.3.1 -> 0.3.2 widget migration, on both sides of the wire.

`use_openrouter` (BOOLEAN) became `prompt_provider` (combo) in the SAME widget slot.
litegraph restores widgets_values positionally and does not type-check, so without a
migration every workflow saved at 0.3.1 opens with `true` in a combo and refuses to queue
- a worse regression than the bug 0.3.1 was released to fix.

Two independent guards, tested independently:
  - web/refpack.js repairs the graph the user is looking at. Extracted from the real file
    between its MMRP-MIGRATE markers and executed under node, so this tests the shipped
    code rather than a copy of it that can drift.
  - nodes._provider_of catches an API client or script that posts a stored prompt and
    never loaded a browser at all.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import types

import pytest

from minimax_refpack import endpoint, nodes

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
REFPACK_JS = REPO_ROOT / "web" / "refpack.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _extract_migration_js() -> str:
    """The marked block from the shipped file, or a hard failure if the markers moved."""
    text = REFPACK_JS.read_text()
    start = text.find("// >>> MMRP-MIGRATE")
    end = text.find("// <<< MMRP-MIGRATE")
    assert start != -1 and end != -1, (
        "the MMRP-MIGRATE markers are gone from web/refpack.js - this test extracts the "
        "real shipped code through them, so removing them silently stops the migration "
        "from being covered at all"
    )
    return text[start:end]


def _run_js(expression: str):
    """Evaluate an expression against the extracted block and return its JSON result."""
    script = _extract_migration_js() + f"\nconsole.log(JSON.stringify({expression}));\n"
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


# ---- the browser half ----------------------------------------------------------


@requires_node
@pytest.mark.parametrize("raw,expected", [
    ("true", "openrouter"),
    ("false", "none"),
    ('"openrouter"', "openrouter"),
    ('"local"', "local"),
    ('"none"', "none"),
    ('"LOCAL"', "local"),
    ('" none "', "none"),
    ('""', "openrouter"),
    ("null", "openrouter"),
    ("undefined", "openrouter"),
    ('"nonsense"', "openrouter"),
])
def test_the_shipped_js_maps_every_value_a_saved_graph_can_hold(raw, expected):
    assert _run_js(f"migrateProviderValue({raw})") == expected


@requires_node
def test_the_shipped_js_rewrites_a_legacy_boolean_on_the_widget():
    out = _run_js(
        "(() => { const w = {name:'prompt_provider', value:true};"
        " const changed = migrateProviderWidget(w);"
        " return [changed, w.value]; })()"
    )
    assert out == [True, "openrouter"]


@requires_node
def test_the_shipped_js_leaves_an_already_correct_value_alone():
    """Returning false matters: the caller logs on true, and logging every load is noise."""
    out = _run_js(
        "(() => { const w = {name:'prompt_provider', value:'local'};"
        " const changed = migrateProviderWidget(w);"
        " return [changed, w.value]; })()"
    )
    assert out == [False, "local"]


@requires_node
def test_the_shipped_js_survives_a_missing_widget():
    """A graph from a build that never had this widget hands us undefined."""
    assert _run_js("migrateProviderWidget(undefined)") is False


@requires_node
def test_the_two_implementations_agree():
    """The JS and the Python must not drift; they guard the same failure."""
    cases = ["openrouter", "local", "none", "LOCAL", " none ", "", "nonsense", "true", "false"]
    js = _run_js("[" + ",".join(json.dumps(c) for c in cases) + "].map(migrateProviderValue)")
    py = [endpoint.normalize_provider(c) for c in cases]
    assert js == py


# ---- the server half -----------------------------------------------------------


def test_a_legacy_boolean_alone_decides_the_provider():
    assert nodes._provider_of(None, True) == "openrouter"
    assert nodes._provider_of(None, False) == "none"


def test_a_deliberate_new_value_beats_a_stale_boolean():
    """A graph can carry both. The one the user actually set has to win."""
    assert nodes._provider_of("local", True) == "local"
    assert nodes._provider_of("none", True) == "none"


def test_the_boolean_is_only_trusted_when_the_new_field_says_nothing():
    assert nodes._provider_of("openrouter", False) == "none"
    assert nodes._provider_of("", False) == "none"


def test_no_legacy_field_means_the_new_one_is_used_as_is():
    assert nodes._provider_of("local", None) == "local"
    assert nodes._provider_of("nonsense", None) == "openrouter"


# ---- end to end ----------------------------------------------------------------


def test_a_0_3_1_workflows_widget_values_still_drive_the_node(tmp_path, monkeypatch):
    """The exact widgets_values array a 0.3.1 workflow saved, replayed positionally.

    This is the failure a user would actually report: they open a saved graph and it
    will not run. The boolean sits where the combo now is.
    """
    module = types.ModuleType("folder_paths")
    module.get_input_directory = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "folder_paths", module)

    saved_0_3_1 = [
        "my direction",              # direction
        "",                          # openrouter_api_key
        "google/gemini-3-flash-preview",  # model
        "",                          # references_json
        "",                          # system_prompt
        1280, 720, 8.0,              # width / height / length_seconds
        False,                       # use_openrouter  <- now prompt_provider
        "medium",                    # reasoning_effort
        "auto",                      # job_type
        2048,                        # max_reference_edge
    ]
    # Zipped against the 0.3.1 NAMES, not today's declaration order. Positional replay
    # is the browser's problem and is handled by remapWidgetValues; what reaches Python
    # is always by keyword, so this is what an API client replaying a stored 0.3.1 prompt
    # actually sends - old names and all.
    names_0_3_1 = [
        "direction", "openrouter_api_key", "model", "references_json", "system_prompt",
        "width", "height", "length_seconds", "use_openrouter", "reasoning_effort",
        "job_type", "max_reference_edge",
    ]
    kwargs = dict(zip(names_0_3_1, saved_0_3_1))

    def boom(*a, **k):
        raise AssertionError("a workflow saved with the opt-out must not make a call")

    monkeypatch.setattr(nodes.prompt, "write_prompt", boom, raising=False)

    out = nodes.MiniMaxH3ReferencePack().build(**kwargs)
    from minimax_refpack import refs
    assert out[refs.slot_index("prompt")] == "my direction"
    assert "prompt_provider: none" in out[refs.slot_index("debug")]


# ---- the 0.3.3 reorder ---------------------------------------------------------
#
# The declaration order in nodes.py is a wire format: widgets_values is positional. When
# 0.3.3 regrouped the widgets, every array saved before it began decoding into the wrong
# widgets. These run the SHIPPED js, so the two orders cannot drift apart silently.

W_0_3_1 = [
    "my direction", "sk-key", "google/gemini-3-flash-preview", "", "",
    1280, 720, 8.0, False, "medium", "auto", 2048,
]
W_0_3_2 = [
    "my direction", "sk-key", "google/gemini-3-flash-preview", "", "",
    1280, 720, 8.0, "local", "medium", "auto", 2048,
    "http://127.0.0.1:1234/v1", "google/gemma-4-e2b",
]


def _remap(values):
    return _run_js(f"remapWidgetValues({json.dumps(values)})")


@requires_node
def test_a_0_3_1_array_lands_in_the_right_widgets():
    got = _remap(W_0_3_1)
    assert got["direction"] == "my direction"
    assert got["width"] == 1280 and got["height"] == 720
    assert got["max_reference_edge"] == 2048
    # the boolean became a provider, and the old `model` became openrouter_model
    assert got["prompt_provider"] == "none"
    assert got["openrouter_model"] == "google/gemini-3-flash-preview"


@requires_node
def test_a_0_3_2_array_lands_in_the_right_widgets():
    got = _remap(W_0_3_2)
    assert got["prompt_provider"] == "local"
    assert got["api_base"] == "http://127.0.0.1:1234/v1"
    assert got["local_model_slug"] == "google/gemma-4-e2b"
    assert got["openrouter_model"] == "google/gemini-3-flash-preview"
    assert got["length_seconds"] == 8.0


@requires_node
def test_the_reorder_never_puts_a_number_where_a_combo_goes():
    """The failure mode being prevented: width (1280) landing in prompt_provider, which
    is exactly the class of bug 0.3.1 shipped with an INT slot holding ""."""
    for values in (W_0_3_1, W_0_3_2):
        got = _remap(values)
        assert got["prompt_provider"] in ("openrouter", "local", "none")
        assert isinstance(got["width"], int)
        assert isinstance(got["max_reference_edge"], int)


@requires_node
def test_an_already_current_array_is_left_alone():
    """A 0.3.3 graph must not be remapped a second time on every load."""
    current = [
        "d", "", "", "local", "sk", "google/gemini-3-flash-preview", "medium",
        "http://127.0.0.1:1234/v1", "google/gemma-4-e2b",
        "auto", 1280, 720, 8.0, 2048,
    ]
    got = _remap(current)
    assert got["prompt_provider"] == "local"
    assert got["api_base"] == "http://127.0.0.1:1234/v1"
    assert got["width"] == 1280


@requires_node
def test_an_unrecognised_array_is_refused_rather_than_guessed():
    """Being wrong here scrambles a workflow that was fine, so no-op beats a guess."""
    assert _run_js("remapWidgetValues(['junk', 1, 2, 3])") is None
    assert _run_js("remapWidgetValues(null)") is None
    assert _run_js("remapWidgetValues([])") is None


@requires_node
def test_the_js_layout_matches_the_python_declaration_order():
    """The one assertion that stops the two halves drifting."""
    from minimax_refpack import nodes
    spec = nodes.MiniMaxH3ReferencePack.INPUT_TYPES()
    python_order = list(spec["required"]) + list(spec["optional"])
    js_order = _run_js("ORDER_0_3_3")
    assert js_order == python_order


# ---- provider-field visibility -------------------------------------------------
#
# Found live 2026-08-17: switching mode painted `api_base`'s URL onto the
# `reasoning_effort` row. Zero height keeps a widget out of the LAYOUT but not out of
# the PAINT - litegraph kept drawing the hidden widgets at their last_y from when they
# were visible. Suppressing draw and dropping last_y is the fix, and this pins it.

def _extract_visibility_js() -> str:
    text = REFPACK_JS.read_text()
    start = text.find("// >>> MMRP-VISIBILITY")
    end = text.find("// <<< MMRP-VISIBILITY")
    assert start != -1 and end != -1, "the MMRP-VISIBILITY markers are gone from refpack.js"
    return text[start:end]


def _run_vis_js(expression: str):
    script = _extract_visibility_js() + f"\nconsole.log(JSON.stringify({expression}));\n"
    proc = subprocess.run([NODE, "--input-type=module", "-e", script],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


@requires_node
def test_hiding_a_widget_suppresses_its_paint_and_drops_its_coordinate():
    """The exact live bug: last_y survived hiding, so the widget kept painting there."""
    out = _run_vis_js(
        "(() => { const w = {name:'api_base', type:'text', value:'http://x/v1', last_y:430};"
        " setWidgetVisible(w, false);"
        " return {type:w.type, lastY:w.last_y===undefined, drawIsNoop:typeof w.draw==='function',"
        "         size:w.computeSize()}; })()"
    )
    assert out["type"] == "hidden"
    assert out["lastY"] is True, "last_y must be cleared or the stale row is painted again"
    assert out["drawIsNoop"] is True, "paint must be suppressed, not just layout"
    assert out["size"] == [0, 0]


@requires_node
def test_showing_it_again_restores_the_real_type_and_the_default_paint():
    out = _run_vis_js(
        "(() => { const w = {name:'api_base', type:'text', value:'v'};"
        " setWidgetVisible(w, false); setWidgetVisible(w, true);"
        " return {type:w.type, draw:w.draw===undefined, size:w.computeSize===undefined,"
        "         value:w.value}; })()"
    )
    assert out == {"type": "text", "draw": True, "size": True, "value": "v"}


@requires_node
def test_hiding_never_touches_the_value():
    """A hidden field still serializes; losing its value would be a data bug on save."""
    out = _run_vis_js(
        "(() => { const w = {name:'local_model_slug', type:'text', value:'google/gemma-4-e2b'};"
        " setWidgetVisible(w, false); return w.value; })()"
    )
    assert out == "google/gemma-4-e2b"


@requires_node
def test_reasoning_effort_hides_with_the_openrouter_group():
    """It is OpenRouter-only (endpoint.sends_reasoning), so it must not sit live on local."""
    groups = _run_vis_js("PROVIDER_FIELDS")
    assert "reasoning_effort" in groups["openrouter"]
    assert "reasoning_effort" not in groups["local"]


@requires_node
def test_every_provider_field_is_a_real_declared_widget():
    """A typo here would silently hide nothing at all."""
    from minimax_refpack import nodes
    spec = nodes.MiniMaxH3ReferencePack.INPUT_TYPES()
    declared = set(spec["required"]) | set(spec["optional"])
    for fields in _run_vis_js("PROVIDER_FIELDS").values():
        for name in fields:
            assert name in declared, name
