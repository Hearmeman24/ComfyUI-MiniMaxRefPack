"""MiniMaxH3ImageReferencePack: the image-only References Manager for MiniMax H3 Image edits.

Same fixtures as test_nodes.py (a stub `folder_paths`, media loaders monkeypatched), plus
the JS half: the MMRP-MODES block of web/refpack.js is extracted and run under node, the
way test_migration.py runs MMRP-MIGRATE, so the config-isolation rule is tested in the
shipped code rather than in a copy.
"""

import asyncio
import json
import pathlib
import shutil
import subprocess
import sys
import types

import pytest

from minimax_refpack import endpoint, nodes, prompt, refs, routes

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
REFPACK_JS = REPO_ROOT / "web" / "refpack.js"
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

ImageNode = nodes.MiniMaxH3ImageReferencePack


@pytest.fixture
def fake_folder_paths(tmp_path, monkeypatch):
    module = types.ModuleType("folder_paths")
    module.get_input_directory = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "folder_paths", module)
    return tmp_path


@pytest.fixture
def stub_loader(monkeypatch):
    calls = []

    def load_image(path, crop=None, max_edge=0):
        calls.append({"path": path, "crop": crop, "max_edge": max_edge})
        return f"IMG:{pathlib.Path(path).name}"

    monkeypatch.setattr(nodes.media, "load_image", load_image)
    return calls


def _touch(tmp_path, *names):
    for name in names:
        (tmp_path / name).write_bytes(b"x")


def _refs(*items):
    return json.dumps({"references": [dict(i) for i in items]})


def _img(name, **extra):
    return {"kind": "image", "file": name, **extra}


# ---- registration and the wire format -------------------------------------------


def test_both_nodes_are_registered_under_their_own_keys():
    assert nodes.NODE_CLASS_MAPPINGS["MiniMaxH3ReferencePack"] is nodes.MiniMaxH3ReferencePack
    assert nodes.NODE_CLASS_MAPPINGS["MiniMaxH3ImageReferencePack"] is ImageNode
    assert nodes.NODE_DISPLAY_NAME_MAPPINGS["MiniMaxH3ImageReferencePack"] == (
        "MiniMax Image References Manager"
    )
    assert ImageNode.CATEGORY == "MiniMax H3"


def test_outputs_are_nine_images_then_prompt_then_debug():
    """Append-only wire format: a saved link points at a slot index."""
    assert ImageNode.RETURN_NAMES == tuple(f"image_{i}" for i in range(1, 10)) + ("prompt", "debug")
    assert ImageNode.RETURN_TYPES == ("IMAGE",) * 9 + ("STRING", "STRING")


def test_the_widget_order_is_pinned():
    """widgets_values restores positionally, so this order is a wire format too, and the
    example workflow plus every workflow built against this node depend on it."""
    spec = ImageNode.INPUT_TYPES()
    order = list(spec["required"]) + list(spec["optional"])
    assert order == [
        "direction", "references_json", "system_prompt", "prompt_provider",
        "openrouter_api_key", "openrouter_model", "reasoning_effort",
        "api_base", "local_model_slug", "max_reference_edge",
    ]


def test_no_video_only_widget_leaks_in():
    spec = ImageNode.INPUT_TYPES()
    declared = set(spec["required"]) | set(spec["optional"])
    assert not declared & {"job_type", "width", "height", "length_seconds"}


def test_shared_widgets_keep_the_video_nodes_options():
    """Derived, not retyped: a shared field's choices and default must match."""
    image = ImageNode.INPUT_TYPES()["optional"]
    video = nodes.MiniMaxH3ReferencePack.INPUT_TYPES()["optional"]
    for name in ("prompt_provider", "openrouter_model", "reasoning_effort", "max_reference_edge"):
        assert image[name][0] == video[name][0], name
        assert image[name][1]["default"] == video[name][1]["default"], name


def test_the_video_node_is_untouched():
    """The 0.3.5 declaration, restated. If this moves, every saved R2V workflow moves."""
    spec = nodes.MiniMaxH3ReferencePack.INPUT_TYPES()
    assert list(spec["required"]) + list(spec["optional"]) == [
        "direction", "references_json", "system_prompt", "prompt_provider",
        "openrouter_api_key", "openrouter_model", "reasoning_effort", "api_base",
        "local_model_slug", "job_type", "width", "height", "length_seconds",
        "max_reference_edge",
    ]
    assert len(nodes.MiniMaxH3ReferencePack.RETURN_NAMES) == 20
    assert "image_edit" not in prompt.MODES


# ---- slot placement ---------------------------------------------------------------


def test_images_land_in_order_and_empty_slots_are_none(fake_folder_paths, stub_loader):
    _touch(fake_folder_paths, "edit.png", "ref.jpg")
    out = ImageNode().build(
        direction="make it night", prompt_provider="none",
        references_json=_refs(_img("edit.png"), _img("ref.jpg")),
    )
    by_name = dict(zip(ImageNode.RETURN_NAMES, out))
    assert by_name["image_1"] == "IMG:edit.png"
    assert by_name["image_2"] == "IMG:ref.jpg"
    for i in range(3, 10):
        assert by_name[f"image_{i}"] is None
    assert by_name["prompt"] == "make it night"
    assert "<Picture 1> edit.png (the image to edit)" in by_name["debug"]


def test_crop_and_cap_reach_the_loader(fake_folder_paths, stub_loader):
    _touch(fake_folder_paths, "a.png")
    ImageNode().build(
        prompt_provider="none", max_reference_edge=1024,
        references_json=_refs(_img("a.png", crop=[0.1, 0.1, 0.5, 0.5])),
    )
    assert stub_loader == [{"path": str(fake_folder_paths / "a.png"),
                            "crop": [0.1, 0.1, 0.5, 0.5], "max_edge": 1024}]


def test_reordering_moves_the_edit_target(fake_folder_paths, stub_loader):
    """Tags are positional: whichever image is listed first is image_1, the edit target."""
    _touch(fake_folder_paths, "a.png", "b.png")
    out = ImageNode().build(prompt_provider="none",
                            references_json=_refs(_img("b.png"), _img("a.png")))
    assert out[0] == "IMG:b.png" and out[1] == "IMG:a.png"


def test_the_order_moves_the_is_changed_key(fake_folder_paths):
    _touch(fake_folder_paths, "a.png", "b.png")
    one = ImageNode.IS_CHANGED(references_json=_refs(_img("a.png"), _img("b.png")))
    two = ImageNode.IS_CHANGED(references_json=_refs(_img("b.png"), _img("a.png")))
    assert one != two


def test_over_nine_images_is_refused(fake_folder_paths, stub_loader):
    names = [f"i{n}.png" for n in range(10)]
    _touch(fake_folder_paths, *names)
    with pytest.raises(refs.ReferenceError, match="at most 9"):
        ImageNode().build(prompt_provider="none", references_json=_refs(*map(_img, names)))


# ---- images only, enforced server-side ------------------------------------------


@pytest.mark.parametrize("ref", [
    {"kind": "video", "file": "clip.mp4", "use_soundtrack": True},
    {"kind": "audio", "file": "voice.wav"},
])
def test_a_video_or_audio_reference_is_refused(fake_folder_paths, stub_loader, ref):
    """A crafted /prompt POST cannot smuggle a clip into a node with no socket for it."""
    _touch(fake_folder_paths, "a.png", ref["file"])
    with pytest.raises(refs.ReferenceError, match=f"images only, got a {ref['kind']}"):
        ImageNode().build(prompt_provider="none", references_json=_refs(_img("a.png"), ref))
    assert stub_loader == [], "nothing may be loaded from a set that is refused"


@pytest.mark.parametrize("name", ["clip.mp4", "voice.wav", "notes.txt", "logo.svg"])
def test_an_image_label_on_a_non_image_file_is_refused(fake_folder_paths, stub_loader, name):
    _touch(fake_folder_paths, name)
    with pytest.raises(ValueError, match="images only"):
        ImageNode().build(prompt_provider="none", references_json=_refs(_img(name)))


def test_validate_kinds_is_a_no_op_for_an_allowed_set():
    refs.ReferenceSet.from_json(_refs(_img("a.png"))).validate_kinds(refs.IMAGE_EDIT_KINDS)


# ---- prompt_provider ----------------------------------------------------------------


def test_none_passes_the_direction_through_verbatim(fake_folder_paths, stub_loader, monkeypatch):
    def boom(**_):
        raise AssertionError("prompt_provider=none must not call the writer")

    monkeypatch.setattr(nodes.prompt, "write_prompt", boom)
    _touch(fake_folder_paths, "a.png")
    direction = "  Replace the taxi in <Picture 1> with a pink ice-cream truck.\n"
    out = ImageNode().build(direction=direction, prompt_provider="none",
                            references_json=_refs(_img("a.png")))
    assert out[9] == direction
    assert "no request was made" in out[10]


def test_none_passes_through_with_no_images(fake_folder_paths, monkeypatch):
    monkeypatch.setattr(nodes.prompt, "write_prompt", lambda **_: pytest.fail("called"))
    out = ImageNode().build(direction="a red apple", prompt_provider="none")
    assert out[9] == "a red apple"


def test_openrouter_calls_the_writer_in_the_image_edit_register(
        fake_folder_paths, stub_loader, monkeypatch):
    captured = {}

    def fake_write_prompt(**kwargs):
        captured.update(kwargs)
        return "The target image shows <Picture 1> at night."

    monkeypatch.setattr(nodes.prompt, "write_prompt", fake_write_prompt)
    _touch(fake_folder_paths, "a.png")
    out = ImageNode().build(
        direction="night", prompt_provider="openrouter", openrouter_model="google/x",
        local_model_slug="should-not-be-used", references_json=_refs(_img("a.png")),
    )
    assert out[9] == "The target image shows <Picture 1> at night."
    assert captured["job_type"] == prompt.IMAGE_EDIT
    assert captured["model"] == "google/x"
    assert captured["provider"] == "openrouter"


def test_local_reads_the_local_slug_and_requires_it(fake_folder_paths, stub_loader, monkeypatch):
    captured = {}
    monkeypatch.setattr(nodes.prompt, "write_prompt",
                        lambda **k: captured.update(k) or "p")
    _touch(fake_folder_paths, "a.png")
    ImageNode().build(direction="x", prompt_provider="local", api_base="http://127.0.0.1:1234/v1",
                      local_model_slug="qwen2.5vl:7b", openrouter_model="google/x",
                      references_json=_refs(_img("a.png")))
    assert captured["model"] == "qwen2.5vl:7b"
    with pytest.raises(ValueError, match="local_model_slug is empty"):
        ImageNode().build(direction="x", prompt_provider="local",
                          api_base="http://127.0.0.1:1234/v1",
                          references_json=_refs(_img("a.png")))


def test_the_api_key_never_reaches_debug(fake_folder_paths, stub_loader, monkeypatch):
    def fake(**kwargs):
        kwargs["debug"].append("payload echoing sk-secret-123")
        return "p"

    monkeypatch.setattr(nodes.prompt, "write_prompt", fake)
    _touch(fake_folder_paths, "a.png")
    out = ImageNode().build(direction="x", openrouter_api_key="sk-secret-123",
                            references_json=_refs(_img("a.png")))
    assert "sk-secret-123" not in out[10]


# ---- the packaged system prompt ------------------------------------------------


def test_the_image_edit_prompt_is_packaged_and_distinct():
    text = prompt._read_system_prompt(prompt.IMAGE_EDIT)
    assert "<Picture 1> is THE IMAGE TO EDIT" in text
    assert "ONLY the final prompt text" in text
    assert "expanded_caption" not in text, "the stock pipeline's JSON contract must not leak in"
    assert text != prompt._read_system_prompt()
    assert prompt._read_system_prompt() == prompt._read_system_prompt("standard")


class _Resp:
    status_code = 200

    def __init__(self, text):
        self._text = text

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


def _capture_post(monkeypatch):
    sent = {}

    def fake_post(key, payload, ep=None):
        sent["payload"] = payload
        sent["ep"] = ep
        return _Resp("  The target image shows <Subject 1>.  ")

    monkeypatch.setattr(prompt, "_post_chat", fake_post)
    monkeypatch.setattr(prompt.media, "load_image", lambda path, crop=None: [[[[0.0, 0.0, 0.0]]]])
    monkeypatch.setattr(prompt, "_tensor_to_jpeg_b64", lambda t: "QUJD")
    return sent


def test_a_blank_system_prompt_selects_the_image_edit_file(tmp_path, monkeypatch):
    sent = _capture_post(monkeypatch)
    (tmp_path / "a.png").write_bytes(b"x")
    debug = []
    out = prompt.write_prompt(
        references=refs.ReferenceSet.from_json(_refs(_img("a.png"), _img("b.png"))),
        input_dir=str(tmp_path), direction="night", api_key="k", model="m",
        job_type=prompt.IMAGE_EDIT, debug=debug,
    )
    assert out == "The target image shows <Subject 1>."
    system = sent["payload"]["messages"][0]["content"]
    assert system == prompt._read_system_prompt(prompt.IMAGE_EDIT)
    assert "system_prompt_image_edit.md" in debug[0]
    assert "-> standard" not in debug[0], "image_edit must never be classified"


def test_a_workflow_override_still_wins(tmp_path, monkeypatch):
    sent = _capture_post(monkeypatch)
    (tmp_path / "a.png").write_bytes(b"x")
    prompt.write_prompt(
        references=refs.ReferenceSet.from_json(_refs(_img("a.png"))), input_dir=str(tmp_path),
        direction="d", api_key="k", model="m", job_type=prompt.IMAGE_EDIT,
        system_prompt="my own rules",
    )
    assert sent["payload"]["messages"][0]["content"] == "my own rules"


def test_picture_1_is_labelled_as_the_image_to_edit(tmp_path, monkeypatch):
    sent = _capture_post(monkeypatch)
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(b"x")
    prompt.write_prompt(
        references=refs.ReferenceSet.from_json(_refs(_img("a.png"), _img("b.png"))),
        input_dir=str(tmp_path), direction="d", api_key="k", model="m",
        job_type=prompt.IMAGE_EDIT,
    )
    texts = [p["text"] for p in sent["payload"]["messages"][1]["content"] if p["type"] == "text"]
    assert "<Picture 1>: a.png (the image to edit)" in texts[0]
    assert "<Picture 2>: b.png" in texts[0] and "<Picture 2>: b.png (" not in texts[0]
    assert any("<Picture 1> (a.png) - the image to edit" in t for t in texts)
    assert "TARGET FORMAT" not in texts[0], "a still edit has no target video format"


def test_local_is_not_reported_as_degraded_for_an_image_set(tmp_path, monkeypatch):
    """A local endpoint takes everything an image set holds; 'videos were sent as stills'
    would be a false statement in `debug`."""
    sent = _capture_post(monkeypatch)
    (tmp_path / "a.png").write_bytes(b"x")
    debug = []
    prompt.write_prompt(
        references=refs.ReferenceSet.from_json(_refs(_img("a.png"))), input_dir=str(tmp_path),
        direction="d", api_key="", model="m", job_type=prompt.IMAGE_EDIT, debug=debug,
        provider="local", api_base="http://127.0.0.1:1234/v1",
    )
    assert "DEGRADED" not in debug[0]
    texts = [p["text"] for p in sent["payload"]["messages"][1]["content"] if p["type"] == "text"]
    assert "<Picture 1> (the image to edit)" in texts[0]
    assert "reasoning" not in sent["payload"]


def test_the_video_registers_are_unchanged_by_the_new_one(tmp_path, monkeypatch):
    """No edit-target wording may reach the video node's payload."""
    sent = _capture_post(monkeypatch)
    (tmp_path / "a.png").write_bytes(b"x")
    prompt.write_prompt(
        references=refs.ReferenceSet.from_json(_refs(_img("a.png"))), input_dir=str(tmp_path),
        direction="d", api_key="k", model="m", job_type="standard",
    )
    texts = [p["text"] for p in sent["payload"]["messages"][1]["content"] if p["type"] == "text"]
    assert not any("image to edit" in t for t in texts)
    assert sent["payload"]["messages"][0]["content"] == prompt._read_system_prompt("standard")


# ---- the read-only system prompt route ----------------------------------------


class _Req:
    def __init__(self, query=None):
        self.query = query or {}


def _route(query=None):
    resp = asyncio.run(routes.system_prompt_route(_Req(query)))
    return json.loads(resp.text)["default"]


def test_the_route_serves_the_image_edit_default_on_request():
    assert _route({"mode": "image_edit"}) == prompt._read_system_prompt(prompt.IMAGE_EDIT)


@pytest.mark.parametrize("query", [None, {}, {"mode": "standard"}, {"mode": "../../etc/passwd"}])
def test_the_route_serves_the_original_default_otherwise(query):
    assert _route(query) == prompt._read_system_prompt()


# ---- the JS half: modes and config isolation -------------------------------------


def _run_modes_js(expression: str):
    text = REFPACK_JS.read_text()
    start = text.find("// >>> MMRP-MODES")
    end = text.find("// <<< MMRP-MODES")
    assert start != -1 and end != -1, "the MMRP-MODES markers are gone from web/refpack.js"
    block = text[start:end].replace("export function", "function")
    script = block + f"\nconsole.log(JSON.stringify({expression}));\n"
    proc = subprocess.run([NODE, "--input-type=module", "-e", script],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


VIDEO, IMAGE = "MiniMaxH3ReferencePack", "MiniMaxH3ImageReferencePack"


@requires_node
def test_the_js_knows_both_node_keys():
    assert sorted(_run_modes_js("Object.keys(MODES)")) == sorted([VIDEO, IMAGE])


@requires_node
def test_the_video_mode_restates_the_old_behaviour():
    mode = _run_modes_js(f"MODES['{VIDEO}']")
    assert mode["kinds"] == ["image", "video", "audio"]
    assert mode["migrateLayout"] is True
    assert mode["configNode"] is None, "video configs keep their pre-0.4.0 shape"
    assert mode["configPrefix"] == "minimax-refpack"
    assert not (mode["editTarget"] or mode["reorder"] or mode["dropFiles"])
    assert mode["systemPromptQuery"] == ""


@requires_node
def test_the_image_mode_is_images_only():
    mode = _run_modes_js(f"MODES['{IMAGE}']")
    assert mode["kinds"] == ["image"]
    assert mode["configNode"] == IMAGE
    assert mode["systemPromptQuery"] == "?mode=image_edit"
    assert mode["modelWidget"] in ImageNode.INPUT_TYPES()["optional"]


@requires_node
def test_a_legacy_config_belongs_to_the_video_node():
    assert _run_modes_js("configOwner({version: 1, references: []})") == VIDEO
    assert _run_modes_js(f"configMismatch({{references: []}}, '{VIDEO}')") is None


@requires_node
def test_a_video_config_is_refused_by_the_image_node():
    msg = _run_modes_js(
        f"configMismatch({{version: 1, references: [{{kind: 'image', file: 'a.png'}}]}}, '{IMAGE}')"
    )
    assert msg and "MiniMax References Manager" in msg and "Load it into" in msg


@requires_node
def test_an_image_config_is_refused_by_the_video_node():
    msg = _run_modes_js(f"configMismatch({{node: '{IMAGE}', references: []}}, '{VIDEO}')")
    assert msg and "MiniMax Image References Manager" in msg


@requires_node
def test_an_image_config_loads_into_the_image_node():
    assert _run_modes_js(
        f"configMismatch({{node: '{IMAGE}', references: [{{kind: 'image', file: 'a.png'}}]}}, '{IMAGE}')"
    ) is None


@requires_node
def test_a_crafted_image_config_carrying_a_clip_is_refused():
    msg = _run_modes_js(
        f"configMismatch({{node: '{IMAGE}', references: [{{kind: 'video', file: 'c.mp4'}}]}}, '{IMAGE}')"
    )
    assert msg and "cannot take" in msg


@requires_node
@pytest.mark.parametrize("file,expected", [
    ({"type": "image/png", "name": "a.png"}, True),
    ({"type": "image/webp", "name": "a.webp"}, True),
    ({"type": "", "name": "a.JPG"}, True),
    ({"type": "video/mp4", "name": "a.png"}, False),
    ({"type": "audio/wav", "name": "a.wav"}, False),
    ({"type": "image/svg+xml", "name": "a.svg"}, False),
    ({"type": "", "name": "a.mov"}, False),
])
def test_the_upload_filter_takes_images_only(file, expected):
    assert _run_modes_js(f"isImageFile({json.dumps(file)})") is expected


@requires_node
def test_the_js_and_python_image_checks_agree_on_names():
    for name in ("a.png", "a.jpeg", "a.webp", "a.gif", "a.bmp", "a.tif", "a.mp4", "a.wav", "a.svg"):
        js = _run_modes_js(f"isImageFile({{type: '', name: '{name}'}})")
        assert js is nodes._is_image_file(name), name
