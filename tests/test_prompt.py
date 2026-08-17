"""Tests for the OpenRouter prompt writer. All network calls are mocked."""

import numpy as np
import pytest
import requests

from minimax_refpack import prompt
from minimax_refpack.refs import Reference, ReferenceSet


# ---- fixtures ---------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


def _models_payload(entries):
    return {"data": entries}


def _model(model_id, modalities):
    return {"id": model_id, "architecture": {"input_modalities": modalities}}


@pytest.fixture(autouse=True)
def _reset_models_cache():
    prompt._models_cache = None
    prompt._models_cache_at = 0.0
    yield
    prompt._models_cache = None
    prompt._models_cache_at = 0.0


def img(name):
    return Reference(kind="image", file=name)


def vid(name, sound=False):
    return Reference(kind="video", file=name, use_soundtrack=sound)


def aud(name):
    return Reference(kind="audio", file=name)


def fake_load_image(path, crop=None):
    return np.random.rand(1, 4, 4, 3).astype(np.float32)


def fake_video_clip_bytes(path, crop=None, trim=None):
    """The whole clip as bytes - what the VLM now receives instead of sampled stills."""
    return b"\x00\x00\x00 ftypmp42 fake", "video/mp4"


def fake_load_video_with_audio(path, target_fps=24, crop=None, trim=None):
    frames = np.random.rand(9, 4, 4, 3).astype(np.float32)
    audio = {"waveform": np.random.uniform(-1, 1, size=(1, 2, 480)).astype(np.float32), "sample_rate": 24000}
    return frames, audio


def fake_load_video_no_audio(path, target_fps=24, crop=None, trim=None):
    frames = np.random.rand(9, 4, 4, 3).astype(np.float32)
    return frames, None


# ---- available_models: modality filter --------------------------------------


def test_filters_to_the_four_modality_superset(monkeypatch):
    payload = _models_payload(
        [
            _model(prompt.DEFAULT_MODEL, ["text", "image", "file", "audio", "video"]),
            _model("some/text-image-only", ["text", "image"]),
            _model("some/full-house", ["text", "image", "audio", "video"]),
        ]
    )
    monkeypatch.setattr(prompt.requests, "get", lambda *a, **k: FakeResponse(200, payload))
    models = prompt.available_models()
    assert "some/text-image-only" not in models
    assert prompt.DEFAULT_MODEL in models
    assert "some/full-house" in models


def test_default_model_is_sorted_first_when_present(monkeypatch):
    payload = _models_payload(
        [
            _model("aaa/before", ["text", "image", "audio", "video"]),
            _model(prompt.DEFAULT_MODEL, ["text", "image", "audio", "video"]),
            _model("zzz/after", ["text", "image", "audio", "video"]),
        ]
    )
    monkeypatch.setattr(prompt.requests, "get", lambda *a, **k: FakeResponse(200, payload))
    models = prompt.available_models()
    assert models[0] == prompt.DEFAULT_MODEL
    assert models[1:] == sorted(models[1:])


def test_available_models_caches_between_calls(monkeypatch):
    calls = []

    def fake_get(*a, **k):
        calls.append(1)
        return FakeResponse(200, _models_payload([_model(prompt.DEFAULT_MODEL, ["text", "image", "audio", "video"])]))

    monkeypatch.setattr(prompt.requests, "get", fake_get)
    prompt.available_models()
    prompt.available_models()
    assert len(calls) == 1


# ---- available_models: must never raise, must never hang --------------------


def test_available_models_falls_back_on_network_failure(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(prompt.requests, "get", boom)
    models = prompt.available_models()
    assert models == prompt._FALLBACK_MODELS
    assert models[0] == prompt.DEFAULT_MODEL


def test_available_models_falls_back_on_malformed_body(monkeypatch):
    monkeypatch.setattr(prompt.requests, "get", lambda *a, **k: FakeResponse(200, {"unexpected": "shape"}))
    models = prompt.available_models()
    assert models[0] == prompt.DEFAULT_MODEL


def test_available_models_falls_back_when_nothing_matches(monkeypatch):
    payload = _models_payload([_model("text/only", ["text"])])
    monkeypatch.setattr(prompt.requests, "get", lambda *a, **k: FakeResponse(200, payload))
    models = prompt.available_models()
    assert models == prompt._FALLBACK_MODELS


def test_available_models_uses_a_short_timeout(monkeypatch):
    captured = {}

    def fake_get(url, timeout=None):
        captured["timeout"] = timeout
        return FakeResponse(200, _models_payload([]))

    monkeypatch.setattr(prompt.requests, "get", fake_get)
    prompt.available_models()
    assert captured["timeout"] is not None
    assert captured["timeout"] <= 10


# ---- key resolution -----------------------------------------------------------


def test_key_from_argument_wins(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    monkeypatch.setenv("LLM_KEY", "llm-key")
    assert prompt._resolve_api_key(" arg-key ") == "arg-key"


def test_key_falls_back_to_openrouter_api_key_env(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert prompt._resolve_api_key("") == "env-key"


def test_key_falls_back_to_llm_key_env(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("LLM_KEY", "llm-key")
    assert prompt._resolve_api_key("  ") == "llm-key"


def test_no_key_anywhere_raises(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_KEY", raising=False)
    with pytest.raises(prompt.PromptError):
        prompt._resolve_api_key("")


def test_key_never_appears_in_the_no_key_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_KEY", raising=False)
    with pytest.raises(prompt.PromptError) as e:
        prompt._resolve_api_key("")
    assert "sk-" not in str(e.value)


def test_key_never_leaks_into_a_request_exception(monkeypatch, tmp_path):
    secret = "sk-or-v1-do-not-leak-me"

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError(f"connection reset while sending key={secret}")

    monkeypatch.setattr(prompt.requests, "post", boom)
    refset = ReferenceSet([])
    with pytest.raises(prompt.PromptError) as e:
        prompt.write_prompt(
            references=refset, input_dir=str(tmp_path), direction="x", api_key=secret, model=prompt.DEFAULT_MODEL
        )
    assert secret not in str(e.value)


def test_key_never_leaks_into_a_non_200_error(monkeypatch, tmp_path):
    secret = "sk-or-v1-do-not-leak-me-either"
    monkeypatch.setattr(
        prompt.requests,
        "post",
        lambda *a, **k: FakeResponse(401, {"error": {"message": "invalid key"}}, text="unauthorized"),
    )
    refset = ReferenceSet([])
    with pytest.raises(prompt.PromptError) as e:
        prompt.write_prompt(
            references=refset, input_dir=str(tmp_path), direction="x", api_key=secret, model=prompt.DEFAULT_MODEL
        )
    assert secret not in str(e.value)


# ---- payload assembly ---------------------------------------------------------


def test_mixed_set_manifest_matches_assign_tags(monkeypatch, tmp_path):
    (tmp_path / "vo.wav").write_bytes(b"RIFF....fake wav bytes")

    refset = ReferenceSet([img("a.jpg"), img("b.jpg"), vid("clip.mp4", sound=True), aud("vo.wav")])
    tagged = {t.file: t for t in refset.assign_tags()}

    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    monkeypatch.setattr(prompt.media, "load_video", fake_load_video_with_audio, raising=False)
    monkeypatch.setattr(prompt.media, "video_clip_bytes", fake_video_clip_bytes, raising=False)

    content = prompt._build_content(refset, str(tmp_path), "handheld, warm light")
    manifest_text = content[0]["text"]

    assert manifest_text.startswith("USER DIRECTION:\nhandheld, warm light")
    assert f"{tagged['a.jpg'].tag}: a.jpg" in manifest_text
    assert f"{tagged['b.jpg'].tag}: b.jpg" in manifest_text
    assert f"{tagged['clip.mp4'].tag} {tagged['clip.mp4'].audio_tag}: clip.mp4" in manifest_text
    assert f"{tagged['vo.wav'].tag}: vo.wav" in manifest_text


def test_every_media_part_is_preceded_by_its_own_kind_label(monkeypatch, tmp_path):
    """The manifest alone leaves nine bare images to be told apart by position.
    Each media part carries its own `<kind>_reference <Tag>` line immediately before it,
    which is what system_prompt.md's per-type handling rules key off."""
    (tmp_path / "vo.wav").write_bytes(b"RIFF....fake wav bytes")
    refset = ReferenceSet([img("a.jpg"), img("b.jpg"), vid("clip.mp4", sound=True), aud("vo.wav")])
    tagged = {t.file: t for t in refset.assign_tags()}

    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    monkeypatch.setattr(prompt.media, "load_video", fake_load_video_with_audio, raising=False)
    monkeypatch.setattr(prompt.media, "video_clip_bytes", fake_video_clip_bytes, raising=False)

    content = prompt._build_content(refset, str(tmp_path), "direction")

    # every media part has a text part somewhere before it
    for i, part in enumerate(content):
        if part.get("type") in ("image_url", "input_audio"):
            assert any(p.get("type") == "text" for p in content[:i])

    labels = [p["text"] for p in content if p.get("type") == "text"]
    joined = "\n".join(labels)
    assert f"image_reference {tagged['a.jpg'].tag} (a.jpg)" in joined
    assert f"image_reference {tagged['b.jpg'].tag} (b.jpg)" in joined
    assert f"video_reference {tagged['clip.mp4'].tag} (clip.mp4)" in joined
    assert f"audio_reference {tagged['vo.wav'].tag} (vo.wav)" in joined
    # the clip's own soundtrack is INSIDE the video part, so it gets a manifest line
    # rather than a label + a duplicate payload
    assert f"{tagged['clip.mp4'].audio_tag}: the soundtrack inside" in content[0]["text"]
    assert f"audio_reference {tagged['clip.mp4'].audio_tag} - the soundtrack" not in joined


def test_the_video_label_sits_directly_before_its_clip(monkeypatch, tmp_path):
    refset = ReferenceSet([vid("clip.mp4", sound=False)])
    monkeypatch.setattr(prompt.media, "video_clip_bytes", fake_video_clip_bytes, raising=False)

    content = prompt._build_content(refset, str(tmp_path), "direction")
    # [0] manifest, [1] the video label, [2] the clip itself
    assert content[1]["type"] == "text"
    assert content[1]["text"].startswith("video_reference <Video 1> (clip.mp4)")
    assert content[2]["type"] == "video_url"
    assert len(content) == 3


def test_a_mixed_set_sends_stills_as_images_and_the_clip_as_a_video(monkeypatch, tmp_path):
    (tmp_path / "vo.wav").write_bytes(b"fake wav bytes")
    refset = ReferenceSet([img("a.jpg"), img("b.jpg"), vid("clip.mp4", sound=True), aud("vo.wav")])

    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    monkeypatch.setattr(prompt.media, "video_clip_bytes", fake_video_clip_bytes, raising=False)

    content = prompt._build_content(refset, str(tmp_path), "direction")

    # the two stills, and nothing sampled out of the clip
    assert len([p for p in content if p.get("type") == "image_url"]) == 2
    assert len([p for p in content if p.get("type") == "video_url"]) == 1
    # only the STANDALONE wav: the clip's own soundtrack rides inside the video file
    audio_parts = [p for p in content if p.get("type") == "input_audio"]
    assert len(audio_parts) == 1
    assert all(a["input_audio"]["format"] == "wav" for a in audio_parts)


def test_video_without_soundtrack_emits_no_audio_part(monkeypatch, tmp_path):
    refset = ReferenceSet([vid("clip.mp4", sound=False)])
    monkeypatch.setattr(prompt.media, "load_video", fake_load_video_with_audio, raising=False)
    monkeypatch.setattr(prompt.media, "video_clip_bytes", fake_video_clip_bytes, raising=False)

    content = prompt._build_content(refset, str(tmp_path), "direction")
    assert not any(p.get("type") == "input_audio" for p in content)
    assert sum(1 for p in content if p.get("type") == "video_url") == 1


def test_video_soundtrack_missing_from_media_degrades_to_text(monkeypatch, tmp_path):
    # a TRIMMED clip: the re-encoded window is video-only, so its soundtrack is loaded
    # separately - and this is the path where "could not be read" can still happen
    refset = ReferenceSet([Reference(kind="video", file="clip.mp4", use_soundtrack=True,
                                     trim=[1.0, 3.0])])
    monkeypatch.setattr(prompt.media, "load_video", fake_load_video_no_audio, raising=False)
    monkeypatch.setattr(prompt.media, "video_clip_bytes", fake_video_clip_bytes, raising=False)

    content = prompt._build_content(refset, str(tmp_path), "direction")
    assert not any(p.get("type") == "input_audio" for p in content)
    assert "could not be read" in content[0]["text"]


def test_audio_fallback_to_text_on_odd_format(tmp_path):
    (tmp_path / "weird.ogg").write_bytes(b"not really ogg")
    refset = ReferenceSet([aud("weird.ogg")])

    content = prompt._build_content(refset, str(tmp_path), "direction")
    audio_parts = [p for p in content if p.get("type") == "input_audio"]
    text_parts = [p for p in content if p.get("type") == "text"]

    assert audio_parts == []
    assert any("weird.ogg" in p["text"] for p in text_parts)


def test_supported_audio_format_is_inlined(tmp_path):
    (tmp_path / "vo.mp3").write_bytes(b"fake mp3 bytes")
    refset = ReferenceSet([aud("vo.mp3")])

    content = prompt._build_content(refset, str(tmp_path), "direction")
    audio_parts = [p for p in content if p.get("type") == "input_audio"]
    assert len(audio_parts) == 1
    assert audio_parts[0]["input_audio"]["format"] == "mp3"


# ---- zero references: the payload is the direction alone -------------------------


def test_build_content_with_no_references_is_one_clean_text_part(tmp_path):
    content = prompt._build_content(ReferenceSet([]), str(tmp_path), "a neon alley chase")

    assert len(content) == 1
    text = content[0]["text"]
    assert text.startswith("USER DIRECTION:\na neon alley chase")
    # no dangling heading over an empty list - the system prompt keys "manifest is
    # absent" off exactly this
    assert "Reference manifest" not in text
    assert "<Picture" not in text and "<Video" not in text and "<Audio" not in text


def test_build_content_with_no_references_keeps_the_target_format(tmp_path):
    content = prompt._build_content(
        ReferenceSet([]), str(tmp_path), "d", width=1280, height=720, length_seconds=8.0
    )
    text = content[0]["text"]
    assert "TARGET FORMAT:" in text
    assert "frame: 1280 x 720 (aspect ratio 16:9)" in text
    assert "Reference manifest" not in text


def test_no_reference_auto_resolves_standard_without_a_classifier_call(monkeypatch, tmp_path):
    """classify_mode's structural gate (needs >=1 video AND >=1 image) makes the
    zero-reference case a free 'standard' - confirmed, not assumed: exactly one HTTP
    call happens, and it is the write, not the classifier."""
    calls = []

    def counting_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls.append(json.get("model"))
        return FakeResponse(200, {"choices": [{"message": {"content": "p"}}]})

    monkeypatch.setattr(prompt.requests, "post", counting_post)
    sink = []
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="a chase",
        api_key="k", model="m", job_type="auto", debug=sink,
    )

    assert calls == ["m"]
    assert prompt.CLASSIFIER_MODEL not in calls
    assert sink[0].startswith("job_type: auto -> standard")


def test_packaged_system_prompt_covers_the_no_reference_case():
    """The packaged writer prompt opens with "the target always has reference assets
    attached" - with the zero-reference path live, it must also say what to do when
    the manifest is absent."""
    std = prompt._read_system_prompt("standard")
    assert "manifest is absent" in std


def test_packaged_system_prompt_resolves_the_no_reference_contradictions():
    """The first live zero-reference run produced a false `[reference generation]`
    prefix and three meaningless `fully_preserved` lines for invented subjects -
    because the file offered no legal alternative. Now it must: a task type that
    means "generated from nothing" (exclusive, never combined), and an explicitly
    EMPTY retention_analysis, carved out of both the EXACT PAIRING rule and the
    final checklist's count check so the rules stop contradicting each other."""
    std = prompt._read_system_prompt("standard")
    # the new task type exists, is shown as the exact prefix, and is exclusive
    assert "`pure generation`" in std
    assert "[pure generation]" in std
    assert "never combines" in std
    # the enum count in the not-a-marker rule tracked the addition
    assert "The six names" not in std
    # retention_analysis is empty by design with no manifest - stated at the rule...
    assert "this section is empty by design" in std
    # ...and at the checklist item that counts labels against lines
    assert "With no manifest the correct line count is zero." in std


def test_reference_carrying_prompt_rules_are_untouched():
    """The manifest-present register must not move: every pre-existing task type,
    the pairing mandate, the newly-generated exclusion and the worked example all
    survive the no-reference amendments verbatim."""
    std = prompt._read_system_prompt("standard")
    for task_type in ("keyframe completion", "reference generation", "video editing",
                      "video continuation", "audio reuse", "audio reference"):
        assert f"`{task_type}`" in std
    assert "EXACT PAIRING, BOTH WAYS." in std
    assert "give each one exactly one line here" in std
    assert "No entries for newly generated content." in std
    assert "`newly_generated` does not exist." in std
    assert "[reference generation + audio reference]" in std  # the worked example
    for marker in ("fully_preserved", "partially_preserved", "attribute_transfer",
                   "fully_copy", "partially_copy", "weak_reference"):
        assert marker in std


# ---- TARGET FORMAT block (node width/height/length -> leading text part) --------


def test_target_format_block_appears_between_direction_and_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    refset = ReferenceSet([img("a.jpg")])

    content = prompt._build_content(
        refset, str(tmp_path), "handheld", width=1280, height=720, length_seconds=8.0
    )
    text = content[0]["text"]

    assert "TARGET FORMAT:" in text
    assert "frame: 1280 x 720 (aspect ratio 16:9)" in text
    assert "duration: 8.000 seconds" in text
    assert text.index("USER DIRECTION:") < text.index("TARGET FORMAT:") < text.index("Reference manifest:")


def test_target_format_aspect_ratio_is_reduced(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    refset = ReferenceSet([img("a.jpg")])

    content = prompt._build_content(
        refset, str(tmp_path), "d", width=640, height=480, length_seconds=0
    )
    assert "frame: 640 x 480 (aspect ratio 4:3)" in content[0]["text"]


def test_target_format_omits_duration_when_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    refset = ReferenceSet([img("a.jpg")])

    content = prompt._build_content(
        refset, str(tmp_path), "d", width=1280, height=720, length_seconds=0
    )
    text = content[0]["text"]
    assert "frame: 1280 x 720" in text
    assert "duration" not in text


def test_target_format_omits_frame_when_either_dimension_is_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    refset = ReferenceSet([img("a.jpg")])

    content = prompt._build_content(
        refset, str(tmp_path), "d", width=0, height=720, length_seconds=8.0
    )
    text = content[0]["text"]
    assert "frame:" not in text
    assert "duration: 8.000 seconds" in text


def test_no_target_format_when_nothing_is_specified(monkeypatch, tmp_path):
    """Existing callers pass no width/height/length - the payload must not change."""
    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    refset = ReferenceSet([img("a.jpg")])

    content = prompt._build_content(refset, str(tmp_path), "d")
    assert "TARGET FORMAT" not in content[0]["text"]


def test_write_prompt_threads_target_format_into_the_payload(monkeypatch, tmp_path):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(prompt.requests, "post", fake_post)
    refset = ReferenceSet([])
    prompt.write_prompt(
        references=refset, input_dir=str(tmp_path), direction="x", api_key="k",
        model=prompt.DEFAULT_MODEL, width=1920, height=1080, length_seconds=12.25,
    )
    text = captured["json"]["messages"][1]["content"][0]["text"]
    assert "frame: 1920 x 1080 (aspect ratio 16:9)" in text
    assert "duration: 12.250 seconds" in text


# ---- what the VLM actually receives -------------------------------------------


def _decode_part(part):
    """(mime, PIL image) behind an image_url part."""
    import base64
    import io

    from PIL import Image

    url = part["image_url"]["url"]
    head, b64 = url.split(",", 1)
    return head[len("data:") : head.find(";")], Image.open(io.BytesIO(base64.b64decode(b64)))


def _oversized_image(path, crop=None):
    return np.random.rand(1, 1000, 2000, 3).astype(np.float32)


def _oversized_video(path, target_fps=24, crop=None, trim=None):
    return np.random.rand(9, 1000, 2000, 3).astype(np.float32), None


# ---- the VLM sees the same crop/trim the pack emits -----------------------------


def test_build_content_passes_crop_and_trim_to_the_loaders(monkeypatch, tmp_path):
    calls = {}

    def li(path, crop=None):
        calls["image"] = crop
        return np.random.rand(1, 4, 4, 3).astype(np.float32)

    def lv(path, crop=None, trim=None):
        calls["video"] = (crop, trim)
        return b"clip bytes", "video/mp4"

    monkeypatch.setattr(prompt.media, "load_image", li, raising=False)
    monkeypatch.setattr(prompt.media, "video_clip_bytes", lv, raising=False)

    refset = ReferenceSet([
        Reference(kind="image", file="a.jpg", crop=[0.1, 0.1, 0.8, 0.8]),
        Reference(kind="video", file="v.mp4", use_soundtrack=False,
                  crop=[0, 0, 0.5, 0.5], trim=[2.0, 6.5]),
    ])
    prompt._build_content(refset, str(tmp_path), "d")

    assert calls["image"] == [0.1, 0.1, 0.8, 0.8]
    assert calls["video"] == ([0, 0, 0.5, 0.5], [2.0, 6.5])


def test_trimmed_standalone_audio_is_decoded_and_inlined_as_wav(monkeypatch, tmp_path):
    """The raw-file inline would hand the VLM the WHOLE file; a trimmed reference goes
    through load_audio so the VLM hears the same span the pack's audio_N socket emits."""
    calls = {}

    def la(path, trim=None):
        calls["trim"] = trim
        return {
            "waveform": np.random.uniform(-1, 1, size=(1, 1, 480)).astype(np.float32),
            "sample_rate": 24000,
        }

    monkeypatch.setattr(prompt.media, "load_audio", la, raising=False)
    (tmp_path / "vo.mp3").write_bytes(b"whole-file bytes that must NOT be inlined")

    refset = ReferenceSet([Reference(kind="audio", file="vo.mp3", trim=[1.0, 2.0])])
    content = prompt._build_content(refset, str(tmp_path), "d")

    audio_parts = [p for p in content if p.get("type") == "input_audio"]
    assert len(audio_parts) == 1
    assert audio_parts[0]["input_audio"]["format"] == "wav"
    assert calls["trim"] == [1.0, 2.0]


def test_references_go_out_as_jpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    content = prompt._build_content(ReferenceSet([img("a.jpg")]), str(tmp_path), "direction")

    mime, _ = _decode_part(content[2])
    assert mime == "image/jpeg"


def test_an_oversized_reference_is_capped_on_its_long_edge(monkeypatch, tmp_path):
    """A 5000x2550 sheet was ~10MB of base64 and ~1.8s of PNG compression per call, and
    none of it survived the trip - Gemini tiles images at 768px."""
    monkeypatch.setattr(prompt.media, "load_image", _oversized_image, raising=False)
    content = prompt._build_content(ReferenceSet([img("a.jpg")]), str(tmp_path), "direction")

    _, image = _decode_part(content[2])
    assert max(image.size) == prompt.VLM_IMAGE_LONG_EDGE
    assert image.size == (prompt.VLM_IMAGE_LONG_EDGE, prompt.VLM_IMAGE_LONG_EDGE // 2)  # 2:1 kept


def test_a_small_reference_is_never_upscaled(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    content = prompt._build_content(ReferenceSet([img("a.jpg")]), str(tmp_path), "direction")

    _, image = _decode_part(content[2])
    assert image.size == (4, 4)


def test_render_payload_reports_the_jpeg_mime(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    content = prompt._build_content(ReferenceSet([img("a.jpg")]), str(tmp_path), "direction")

    rendered = prompt.render_payload({"messages": [{"role": "user", "content": content}]})
    assert "image_url \u00b7 image/jpeg" in rendered
    # The base64 is stubbed, but the real data: prefix stays so the reader sees the
    # actual wire shape rather than a summary of it.
    assert "data:image/jpeg;base64,<BASE64_STRING>" in rendered
    assert prompt.BASE64_PLACEHOLDER in rendered


# ---- write_prompt: the full round trip ----------------------------------------


def test_write_prompt_returns_stripped_completion(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.media, "load_image", fake_load_image, raising=False)
    monkeypatch.setattr(
        prompt.requests,
        "post",
        lambda *a, **k: FakeResponse(200, {"choices": [{"message": {"content": "  a fine prompt  "}}]}),
    )
    refset = ReferenceSet([img("a.jpg")])
    out = prompt.write_prompt(
        references=refset, input_dir=str(tmp_path), direction="steer", api_key="key", model=prompt.DEFAULT_MODEL
    )
    assert out == "a fine prompt"


def test_write_prompt_sends_bearer_header(monkeypatch, tmp_path):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(prompt.requests, "post", fake_post)
    refset = ReferenceSet([])
    prompt.write_prompt(
        references=refset, input_dir=str(tmp_path), direction="steer", api_key="my-key", model=prompt.DEFAULT_MODEL
    )
    assert captured["url"].startswith("https://")
    assert captured["headers"]["Authorization"] == "Bearer my-key"
    assert captured["timeout"] is not None


def test_write_prompt_401_is_a_prompt_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        prompt.requests,
        "post",
        lambda *a, **k: FakeResponse(401, {"error": {"message": "invalid api key"}}, text="unauthorized"),
    )
    refset = ReferenceSet([])
    with pytest.raises(prompt.PromptError) as e:
        prompt.write_prompt(references=refset, input_dir=str(tmp_path), direction="x", api_key="k", model=prompt.DEFAULT_MODEL)
    assert "401" in str(e.value)


def test_write_prompt_500_is_a_prompt_error(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.requests, "post", lambda *a, **k: FakeResponse(500, None, text="server exploded"))
    refset = ReferenceSet([])
    with pytest.raises(prompt.PromptError) as e:
        prompt.write_prompt(references=refset, input_dir=str(tmp_path), direction="x", api_key="k", model=prompt.DEFAULT_MODEL)
    assert "500" in str(e.value)


def test_write_prompt_empty_completion_is_a_prompt_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        prompt.requests,
        "post",
        lambda *a, **k: FakeResponse(200, {"choices": [{"message": {"content": "   "}}]}),
    )
    refset = ReferenceSet([])
    with pytest.raises(prompt.PromptError):
        prompt.write_prompt(references=refset, input_dir=str(tmp_path), direction="x", api_key="k", model=prompt.DEFAULT_MODEL)


def test_write_prompt_malformed_body_is_a_prompt_error(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.requests, "post", lambda *a, **k: FakeResponse(200, {"unexpected": "shape"}))
    refset = ReferenceSet([])
    with pytest.raises(prompt.PromptError):
        prompt.write_prompt(references=refset, input_dir=str(tmp_path), direction="x", api_key="k", model=prompt.DEFAULT_MODEL)


def test_write_prompt_reads_system_prompt_at_call_time(monkeypatch, tmp_path):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(prompt.requests, "post", fake_post)
    refset = ReferenceSet([])
    prompt.write_prompt(references=refset, input_dir=str(tmp_path), direction="x", api_key="k", model=prompt.DEFAULT_MODEL)
    system_message = captured["json"]["messages"][0]
    assert system_message["role"] == "system"
    with open(prompt._SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        assert system_message["content"] == f.read()


# ---- system_prompt override ---------------------------------------------------


def test_write_prompt_uses_a_supplied_system_prompt_verbatim(monkeypatch, tmp_path):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(prompt.requests, "post", fake_post)
    refset = ReferenceSet([])
    custom = "You are a custom MiniMax prompt writer.\nBe terse."
    prompt.write_prompt(
        references=refset, input_dir=str(tmp_path), direction="x", api_key="k",
        model=prompt.DEFAULT_MODEL, system_prompt=custom,
    )
    system_message = captured["json"]["messages"][0]
    assert system_message["role"] == "system"
    assert system_message["content"] == custom


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_write_prompt_blank_system_prompt_falls_back_to_packaged_file(monkeypatch, tmp_path, blank):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(prompt.requests, "post", fake_post)
    refset = ReferenceSet([])
    prompt.write_prompt(
        references=refset, input_dir=str(tmp_path), direction="x", api_key="k",
        model=prompt.DEFAULT_MODEL, system_prompt=blank,
    )
    system_message = captured["json"]["messages"][0]
    with open(prompt._SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        assert system_message["content"] == f.read()


def test_write_prompt_fallback_still_reads_the_file_at_call_time(monkeypatch, tmp_path):
    """Same guarantee as test_write_prompt_reads_system_prompt_at_call_time, but proven
    with an explicit blank system_prompt argument rather than the default omission."""
    captured = []

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.append(json["messages"][0]["content"])
        return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(prompt.requests, "post", fake_post)
    refset = ReferenceSet([])
    original = prompt._read_system_prompt()

    prompt.write_prompt(
        references=refset, input_dir=str(tmp_path), direction="x", api_key="k",
        model=prompt.DEFAULT_MODEL, system_prompt="",
    )

    edited = original + "\n\nEDITED FOR THIS TEST"
    with open(prompt._SYSTEM_PROMPT_PATH, "w", encoding="utf-8") as f:
        f.write(edited)
    try:
        prompt.write_prompt(
            references=refset, input_dir=str(tmp_path), direction="x", api_key="k",
            model=prompt.DEFAULT_MODEL, system_prompt="",
        )
    finally:
        with open(prompt._SYSTEM_PROMPT_PATH, "w", encoding="utf-8") as f:
            f.write(original)

    assert captured[0] == original
    assert captured[1] == edited


# ---- reasoning effort -----------------------------------------------------------


def _capture_payload(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured["payload"] = json
        return FakeResponse(200, {"choices": [{"message": {"content": "p"}}]})

    monkeypatch.setattr(prompt.requests, "post", fake_post)
    return captured


def test_reasoning_effort_defaults_to_medium(monkeypatch, tmp_path):
    captured = _capture_payload(monkeypatch)
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="d",
        api_key="k", model=prompt.DEFAULT_MODEL,
    )
    assert captured["payload"]["reasoning"] == {"effort": "medium"}
    assert prompt.DEFAULT_REASONING_EFFORT == "medium"


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high"])
def test_every_legal_effort_goes_on_the_wire(monkeypatch, tmp_path, effort):
    """'none' is OpenRouter's own 'do not reason' value, so it is sent, not dropped."""
    captured = _capture_payload(monkeypatch)
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="d",
        api_key="k", model=prompt.DEFAULT_MODEL, reasoning_effort=effort,
    )
    assert captured["payload"]["reasoning"] == {"effort": effort}


def test_an_unrecognised_effort_is_dropped_rather_than_sent(monkeypatch, tmp_path):
    """A bad widget value must not turn into a 400 that kills a whole queue."""
    captured = _capture_payload(monkeypatch)
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="d",
        api_key="k", model=prompt.DEFAULT_MODEL, reasoning_effort="ludicrous",
    )
    assert "reasoning" not in captured["payload"]


def test_render_payload_shows_the_reasoning_field(monkeypatch, tmp_path):
    sink = []
    _capture_payload(monkeypatch)
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="d",
        api_key="k", model=prompt.DEFAULT_MODEL, reasoning_effort="high", debug=sink,
    )
    assert "reasoning: {'effort': 'high'}" in sink[0]


def test_debug_shows_the_user_message_before_the_system_prompt(monkeypatch, tmp_path):
    """Display order only: the ~190-line system prompt buried the direction at the
    bottom of the debug socket. The payload itself must still send system first."""
    captured = _capture_payload(monkeypatch)
    sink = []
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="MY DIRECTION HERE",
        api_key="k", model=prompt.DEFAULT_MODEL, system_prompt="THE SYSTEM RULES", debug=sink,
    )
    text = sink[0]
    assert text.index("===== USER MESSAGE") < text.index("===== SYSTEM MESSAGE")
    assert text.index("MY DIRECTION HERE") < text.index("THE SYSTEM RULES")

    # ...but the wire order is unchanged: system is still messages[0].
    assert [m["role"] for m in captured["payload"]["messages"]] == ["system", "user"]


# ---- job_type routing ------------------------------------------------------------


def _refset_with(video=0, image=0):
    refs_list = [img(f"i{i}.png") for i in range(image)] + [vid(f"v{i}.mp4") for i in range(video)]
    return ReferenceSet(refs_list)


def test_replacement_is_impossible_without_both_a_video_and_an_image(monkeypatch):
    """The structural gate runs before the classifier, so most jobs never pay for it."""
    def boom(*a, **k):
        raise AssertionError("classifier must not be called when the set can't support a swap")

    monkeypatch.setattr(prompt.requests, "post", boom)
    for refset in (_refset_with(video=1), _refset_with(image=1), _refset_with()):
        assert prompt.classify_mode(
            direction="swap the bottle for the can", references=refset, api_key="k"
        ) == "standard"


def test_classifier_routes_to_replacement(monkeypatch):
    monkeypatch.setattr(
        prompt.requests, "post",
        lambda *a, **k: FakeResponse(200, {"choices": [{"message": {"content": "REPLACEMENT"}}]}),
    )
    got = prompt.classify_mode(
        direction="swap the bottle for the can in the image",
        references=_refset_with(video=1, image=1), api_key="k",
    )
    assert got == "replacement"


@pytest.mark.parametrize("failure", [
    lambda *a, **k: FakeResponse(429, {"error": {"message": "rate limited"}}),
    lambda *a, **k: FakeResponse(200, {"choices": [{"message": {"content": "banana"}}]}),
    lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("down")),
])
def test_any_classifier_failure_falls_back_to_standard(monkeypatch, failure):
    """A classifier that 429s or answers nonsense must never block a run, and must never
    route into replacement - the wrong-way error produces a completely wrong format."""
    monkeypatch.setattr(prompt.requests, "post", failure)
    assert prompt.classify_mode(
        direction="swap it", references=_refset_with(video=1, image=1), api_key="k"
    ) == "standard"


def test_explicit_job_type_never_calls_the_classifier(monkeypatch, tmp_path):
    calls = []
    real_post = prompt.requests.post

    def counting_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls.append(json.get("model"))
        return FakeResponse(200, {"choices": [{"message": {"content": "p"}}]})

    monkeypatch.setattr(prompt.requests, "post", counting_post)
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="d",
        api_key="k", model="m", job_type="replacement",
    )
    # exactly one call - the write - and never the classifier model
    assert len(calls) == 1
    assert prompt.CLASSIFIER_MODEL not in calls


def test_each_job_type_loads_its_own_system_prompt():
    """Both registers emit MiniMax's six-section Ref2VA IR - a replacement is a
    documented `video editing` task, not a bespoke format - so what separates them is
    the task type and the editing semantics, not the section names."""
    std = prompt._read_system_prompt("standard")
    rep = prompt._read_system_prompt("replacement")
    assert std != rep

    # both speak the same IR
    for text in (std, rep):
        assert "subject_definitions" in text
        assert "retention_analysis" in text

    # only the replacement register carries the editing task type and its fixed opener
    assert "[video editing]" in rep
    assert "The target video is an edited version of <Video 1>." in rep
    assert "[video editing]" not in std

    # and the replacement register must forbid the ten-section prose shape that the
    # source template produced, which returned headingless prose in eval/replacement
    assert "NEVER emit the ten-section prose format" in rep


def test_replacement_prompt_uses_minimax_tag_spelling():
    """The source template used <Image_1>/<Video_1>; MiniMax binds <Picture N>/<Video N>.
    The wrong spelling may appear ONLY where it is taught against - never in the format
    spec the model copies from."""
    rep = prompt._read_system_prompt("replacement")
    assert "<Picture N>" in rep
    assert "underscore" in rep, "the prompt should say why the wrong spelling fails"

    _, _, after = rep.partition("=== OUTPUT FORMAT")
    fmt, _, _checklist = after.partition("=== RULES ===")
    assert fmt, "OUTPUT FORMAT section not found"
    assert "<Image_1>" not in fmt, "the wrong spelling leaked into the format spec"
    assert "<Video_1>" not in fmt
    assert "<Picture N>" in fmt


def test_debug_records_the_routing_decision(monkeypatch, tmp_path):
    monkeypatch.setattr(
        prompt.requests, "post",
        lambda *a, **k: FakeResponse(200, {"choices": [{"message": {"content": "p"}}]}),
    )
    sink = []
    prompt.write_prompt(
        references=ReferenceSet([]), input_dir=str(tmp_path), direction="d",
        api_key="k", model="m", job_type="replacement", debug=sink,
    )
    assert len(sink) == 1, "the node reads debug_sink[0]; exactly one entry"
    assert "job_type: replacement -> replacement" in sink[0]


# ---- structured logging -------------------------------------------------------


def test_write_prompt_logs_the_call_and_its_latency(monkeypatch, tmp_path, caplog):
    import logging

    monkeypatch.setattr(
        prompt.requests, "post",
        lambda *a, **k: FakeResponse(200, {
            "choices": [{"message": {"content": "a prompt"}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "cost": 0.0141},
        }),
    )

    with caplog.at_level(logging.INFO, logger="MiniMaxRefPack"):
        prompt.write_prompt(
            references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
            api_key="sk-or-v1-deadbeef", model="m", job_type="standard",
        )

    lines = [r.getMessage() for r in caplog.records if r.name == "MiniMaxRefPack"]
    # event=chat, not event=openrouter: the same call can now go to a local server, and a
    # log line that names the wrong destination is worse than a generic one.
    call = next(ln for ln in lines if "event=chat" in ln)
    assert "model=m" in call
    assert "status=200" in call
    assert "ms=" in call
    assert "prompt_tokens=1200" in call
    assert "completion_tokens=300" in call
    assert "cost=0.014" in call
    assert not any("deadbeef" in ln for ln in lines)


def test_a_failed_openrouter_call_is_logged_as_a_failure(monkeypatch, tmp_path, caplog):
    import logging

    monkeypatch.setattr(prompt.requests, "post", lambda *a, **k: FakeResponse(429, {}, text="slow down"))

    with caplog.at_level(logging.INFO, logger="MiniMaxRefPack"):
        with pytest.raises(prompt.PromptError):
            prompt.write_prompt(
                references=ReferenceSet([]), input_dir=str(tmp_path), direction="x",
                api_key="k", model="m", job_type="standard",
            )

    lines = [r.getMessage() for r in caplog.records if r.name == "MiniMaxRefPack"]
    assert any("event=chat" in ln and "status=429" in ln for ln in lines)
    assert any("endpoint=openrouter" in ln for ln in lines)


# ---- videos go whole, not as sampled frames -------------------------------------


def _fake_video_bytes(monkeypatch, data=b"MP4DATA", mime="video/mp4"):
    monkeypatch.setattr(prompt.media, "video_clip_bytes",
                        lambda path, crop=None, trim=None: (data, mime), raising=False)


def test_a_video_reference_goes_as_one_video_part(monkeypatch, tmp_path):
    _fake_video_bytes(monkeypatch)
    refset = ReferenceSet([Reference(kind="video", file="v.mp4", use_soundtrack=False)])

    parts = prompt._build_content(refset, str(tmp_path), "d")

    videos = [p for p in parts if p["type"] == "video_url"]
    assert len(videos) == 1
    assert videos[0]["video_url"]["url"].startswith("data:video/mp4;base64,")
    # the whole point: no sampled stills any more
    assert [p for p in parts if p["type"] == "image_url"] == []


def test_the_video_part_carries_the_real_bytes(monkeypatch, tmp_path):
    import base64

    _fake_video_bytes(monkeypatch, data=b"\x00\x01\x02clip")
    refset = ReferenceSet([Reference(kind="video", file="v.mp4", use_soundtrack=False)])

    parts = prompt._build_content(refset, str(tmp_path), "d")
    url = next(p for p in parts if p["type"] == "video_url")["video_url"]["url"]

    assert base64.b64decode(url.split(",", 1)[1]) == b"\x00\x01\x02clip"


def test_the_manifest_says_a_clip_follows_not_a_frame_count(monkeypatch, tmp_path):
    _fake_video_bytes(monkeypatch)
    refset = ReferenceSet([Reference(kind="video", file="v.mp4", use_soundtrack=False)])

    text = prompt._build_content(refset, str(tmp_path), "d")[0]["text"]

    assert "frames of <Video 1>" not in text
    assert "<Video 1>" in text


def test_an_untouched_clip_does_not_send_its_soundtrack_twice(monkeypatch, tmp_path):
    """The file itself carries its audio - a separate WAV part would be the same sound
    paid for twice (the live probe billed 250 audio tokens for the video's own track)."""
    _fake_video_bytes(monkeypatch)
    monkeypatch.setattr(prompt.media, "load_video", _never_load_video, raising=False)
    refset = ReferenceSet([Reference(kind="video", file="v.mp4", use_soundtrack=True)])

    parts = prompt._build_content(refset, str(tmp_path), "d")

    assert [p for p in parts if p["type"] == "input_audio"] == []
    text = parts[0]["text"]
    assert "<Audio 1>" in text   # the tag still exists, the payload just isn't duplicated


def _never_load_video(*a, **k):
    raise AssertionError("an untouched clip must not be decoded")


def test_an_edited_clip_still_sends_its_soundtrack_separately(monkeypatch, tmp_path):
    """The re-encoded window is video-only, so the sound has to come the old way."""
    _fake_video_bytes(monkeypatch)
    monkeypatch.setattr(
        prompt.media, "load_video",
        lambda path, target_fps=24, crop=None, trim=None: (None, {"waveform": None, "sample_rate": 48000}),
        raising=False,
    )
    monkeypatch.setattr(prompt, "_video_audio_part",
                        lambda audio, tag, name: {"type": "input_audio", "input_audio": {"data": "x", "format": "wav"}})
    refset = ReferenceSet([Reference(kind="video", file="v.mp4", use_soundtrack=True, trim=[1.0, 3.0])])

    parts = prompt._build_content(refset, str(tmp_path), "d")

    assert len([p for p in parts if p["type"] == "input_audio"]) == 1
    assert len([p for p in parts if p["type"] == "video_url"]) == 1


def test_the_logged_payload_size_counts_the_video():
    """A 5MB clip must not be logged as a 300-byte payload."""
    parts = [
        {"type": "text", "text": "x" * 10},
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + "A" * 1000}},
    ]

    assert prompt._payload_bytes(parts) == 10 + len("data:video/mp4;base64,") + 1000
