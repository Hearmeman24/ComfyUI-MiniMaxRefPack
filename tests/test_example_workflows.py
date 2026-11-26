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
import re

import pytest

from minimax_refpack import nodes

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / "example_workflows"

# What `comfy node publish` registered this pack as. ComfyUI reads properties.cnr_id
# to offer "Install Missing Custom Nodes"; without it the user gets an unresolvable
# pack name and a dead end.
REGISTRY_ID = "comfyui-minimaxrefpack"


def _package_version() -> str:
    """The version pyproject declares, which is what `comfy node publish` uploads."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    assert match, "no version in pyproject.toml"
    return match.group(1)

WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.json"))


# Every node type this pack registers; each is checked against its own declaration.
PACK_CLASSES = dict(nodes.NODE_CLASS_MAPPINGS)


def _widget_spec(node_type="MiniMaxH3ReferencePack"):
    """(name, type_spec) per widget, in the order litegraph restores them."""
    spec = PACK_CLASSES[node_type].INPUT_TYPES()
    ordered = list(spec.get("required", {}).items()) + list(spec.get("optional", {}).items())
    return [(name, entry[0]) for name, entry in ordered]


def _pack_nodes(path):
    graph = json.loads(path.read_text())
    return [n for n in graph.get("nodes", []) if n.get("type") in PACK_CLASSES]


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
    for node in _pack_nodes(path):
        widgets = _widget_spec(node["type"])
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


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_the_workflows_version_stamp_tracks_the_release(path):
    """`properties.ver` must equal the version being published, on every release.

    This is a chore that will otherwise be forgotten, so it is a test rather than a
    habit. ComfyUI stamps this field from whatever the AUTHOR had installed, and a dev
    checkout stamps a git SHA: the file handed over on 2026-08-17 carried
    760cc261e7bc... which is 0.2.1, three releases stale. Shipping that means "Install
    Missing Custom Nodes" resolves against an old git ref instead of the published
    version, which is a quieter version of the "Unknown pack" bug 0.3.1 shipped with.
    """
    expected = _package_version()
    for node in _pack_nodes(path):
        properties = node.get("properties") or {}
        assert properties.get("ver") == expected, (
            f"{path.name}: properties.ver is {properties.get('ver')!r} but pyproject "
            f"declares {expected!r}. Bump the workflow's ver when you bump the release."
        )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_the_workflows_claim_registry_provenance_not_a_git_checkout(path):
    """`aux_id` is what ComfyUI stamps for a git-installed pack. A shipped workflow is
    installed from the registry, so it carries `cnr_id` alone; having both invites the
    frontend to resolve the wrong one."""
    for node in _pack_nodes(path):
        properties = node.get("properties") or {}
        assert "aux_id" not in properties, (
            f"{path.name}: properties carries aux_id={properties.get('aux_id')!r}, which "
            "means this was saved from a dev checkout rather than a registry install"
        )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_link_points_at_a_node_that_is_not_there(path):
    """Deleting a node by hand can leave a link behind, which breaks the graph on load."""
    graph = json.loads(path.read_text())
    ids = {n["id"] for n in graph.get("nodes", [])}
    dangling = [l for l in graph.get("links", []) if l[1] not in ids or l[3] not in ids]
    assert not dangling, f"{path.name}: {len(dangling)} link(s) reference a missing node"


# Preview nodes cache their last result INTO the saved graph. Whatever the author last
# generated therefore ships with the workflow. On 2026-08-17 that was 2,910 characters of
# explicit generated prose sitting in a Display Any node, which would have gone to a
# public repo and the ComfyUI registry listing. The direction field had already been
# cleaned by hand; this one was invisible because nobody thinks of a display as content.
PREVIEW_NODES = ("Display Any (rgthree)", "PreviewAny", "ShowText|pysssss", "Note")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_preview_nodes_ship_empty(path):
    graph = json.loads(path.read_text())
    for node in graph.get("nodes", []):
        if node.get("type") not in PREVIEW_NODES:
            continue
        for value in node.get("widgets_values") or []:
            assert not (isinstance(value, str) and value.strip()), (
                f"{path.name}: {node['type']} carries {len(value)} chars of a previous "
                "run. Clear it before shipping: whatever you last generated goes public "
                "with the workflow."
            )


# ---- the image-edit example ------------------------------------------------------

IMAGE_EDIT_WORKFLOW = WORKFLOW_DIR / "MiniMax H3 Image - Edit - Reference Manager.json"


def test_the_image_edit_example_ships():
    assert IMAGE_EDIT_WORKFLOW.exists()


def test_the_image_edit_example_wires_the_pack_into_the_conditioning_node():
    """image_1 (the picture to edit) must reach images.image_1, and the written prompt
    must reach the conditioning node's prompt - otherwise the example edits nothing."""
    graph = json.loads(IMAGE_EDIT_WORKFLOW.read_text())
    by_id = {n["id"]: n for n in graph["nodes"]}
    packs = [n for n in graph["nodes"] if n["type"] == "MiniMaxH3ImageReferencePack"]
    conds = [n for n in graph["nodes"] if n["type"] == "MiniMaxH3ImageConditioning"]
    assert len(packs) == 1 and len(conds) == 1
    pack, cond = packs[0], conds[0]
    names = [o["name"] for o in pack["outputs"]]
    assert names == [f"image_{i}" for i in range(1, 10)] + ["prompt", "debug"]

    # Follow each pack output downstream, through resize nodes only (the example
    # brings the picture to edit up to ~4 MP like the stock edit workflow does).
    passthrough = {"ResizeImageMaskNode"}
    wired = {}

    def follow(node_id, slot, name):
        for _lid, src, src_slot, dst, dst_slot, _t in graph["links"]:
            if src != node_id or src_slot != slot:
                continue
            if dst == cond["id"]:
                wired.setdefault(name, set()).add(cond["inputs"][dst_slot]["name"])
            elif by_id[dst]["type"] in passthrough:
                follow(dst, 0, name)

    for slot, name in enumerate(names):
        follow(pack["id"], slot, name)
    # The conditioning node's Autogrow names its slots image_0, image_1, ... and sorts
    # them by that number before tagging (nodes_minimax_h3.py:727), so what must hold is
    # ORDER: pack image_k lands on the k-th slot, and image_1 - the picture to edit -
    # on the lowest one.
    slots = []
    for i in range(1, 10):
        targets = wired.get(f"image_{i}")
        assert targets and len(targets) == 1, (f"image_{i}", wired)
        (name,) = targets
        assert name.startswith("images.image_"), name
        slots.append(int(name.rsplit("_", 1)[1]))
    assert slots == sorted(slots) and len(set(slots)) == 9, slots
    assert wired.get("prompt") == {"prompt"}

    # Every link is mirrored on both ends, or the frontend drops it on load.
    for link_id, src, src_slot, dst, dst_slot, _type in graph["links"]:
        assert link_id in (by_id[src]["outputs"][src_slot].get("links") or []), link_id
        assert by_id[dst]["inputs"][dst_slot].get("link") == link_id, link_id


def test_the_image_edit_example_names_the_shipped_models():
    graph = json.loads(IMAGE_EDIT_WORKFLOW.read_text())
    values = {n["type"]: n.get("widgets_values") for n in graph["nodes"]}
    assert values["UNETLoader"][0] == "minimax_h3_image_pruned_int8_convrot.safetensors"
    assert values["CLIPLoader"][:2] == ["qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "minimax"]
    assert values["VAELoader"][0] == "ostris_h3_image_vae_fp16.safetensors"
