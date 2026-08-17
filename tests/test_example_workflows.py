"""The shipped example workflows, checked against the node's own INPUT_TYPES.

Two user-reported breakages came from this file rather than from the code, and both
were invisible to every other test because nothing loaded the JSON we hand out:

  - "The value for MiniMax References Manager's max_reference_edge couldn't be
    converted to INT" - widgets_values carried "" in the slot litegraph restores
    max_reference_edge from. Positional restore means a wrong value in slot N is a
    wrong value for widget N, so a type check per slot is the whole guard.
  - "Unknown pack: MinimaxH3referencepack" - the node's properties carried no
    cnr_id, so ComfyUI could not map the missing node to its registry pack and
    guessed a name from the node type.

These run without ComfyUI: INPUT_TYPES is read straight off the class, and the only
entry that touches the network (`model`, whose list comes from OpenRouter) falls back
to a hardcoded list, so the widget ORDER is stable either way.
"""

import json
import pathlib

import pytest

from minimax_refpack import nodes

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / "example_workflows"

# What `comfy node publish` registered this pack as. ComfyUI reads properties.cnr_id
# to offer "Install Missing Custom Nodes"; without it the user gets an unresolvable
# pack name and a dead end.
REGISTRY_ID = "comfyui-minimaxrefpack"

WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.json"))


def _widget_spec():
    """(name, type_spec) per widget, in the order litegraph restores them."""
    spec = nodes.MiniMaxH3ReferencePack.INPUT_TYPES()
    ordered = list(spec.get("required", {}).items()) + list(spec.get("optional", {}).items())
    return [(name, entry[0]) for name, entry in ordered]


def _pack_nodes(path):
    graph = json.loads(path.read_text())
    return [n for n in graph.get("nodes", []) if n.get("type") == "MiniMaxH3ReferencePack"]


def test_there_is_at_least_one_example_workflow():
    assert WORKFLOWS, f"no workflows found under {WORKFLOW_DIR}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_is_valid_json(path):
    json.loads(path.read_text())


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_saved_widget_values_match_the_declared_types(path):
    """A saved value in slot N must be loadable into widget N.

    ComfyUI validates widgets by declared type before the node ever runs, so a
    string in an INT slot fails the prompt with the error the user pasted, not
    with anything this repo controls.
    """
    checkers = {
        "INT": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "FLOAT": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "STRING": lambda v: isinstance(v, str),
        "BOOLEAN": lambda v: isinstance(v, bool),
    }
    widgets = _widget_spec()

    for node in _pack_nodes(path):
        values = node.get("widgets_values") or []
        assert len(values) <= len(widgets), (
            f"{path.name}: {len(values)} saved values for {len(widgets)} widgets - "
            "a stale value would land in the wrong slot"
        )
        for value, (name, type_spec) in zip(values, widgets):
            if isinstance(type_spec, list):        # a combo: the value must be one of them
                assert value in type_spec, (
                    f"{path.name}: {name}={value!r} is not one of {type_spec}"
                )
                continue
            check = checkers.get(type_spec)
            if check is None:
                continue
            assert check(value), (
                f"{path.name}: {name} is declared {type_spec} but the workflow saves "
                f"{value!r} ({type(value).__name__}). ComfyUI will refuse to run this."
            )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_the_node_declares_its_registry_pack(path):
    """Without cnr_id, "Install Missing Custom Nodes" cannot find this pack."""
    for node in _pack_nodes(path):
        properties = node.get("properties") or {}
        assert properties.get("cnr_id") == REGISTRY_ID, (
            f"{path.name}: properties.cnr_id is {properties.get('cnr_id')!r}, expected "
            f"{REGISTRY_ID!r} - otherwise ComfyUI reports 'Unknown pack' for a pack that "
            "is published and installable."
        )
        assert properties.get("ver"), f"{path.name}: properties.ver is missing"
