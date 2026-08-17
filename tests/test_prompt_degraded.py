"""What goes on the wire when the endpoint cannot take everything.

The rule, set with Aviv 2026-08-17: a video becomes still frames, NO audio is sent at all
(not the video's soundtrack, not a standalone clip), and the manifest says so. Tags never
move, because they are the contract between this prompt and the node's 20 sockets.
"""

import pytest

from minimax_refpack import endpoint as ep
from minimax_refpack import prompt
from minimax_refpack.refs import Reference, ReferenceSet

LOCAL = ep.resolve("local", "http://localhost:1234/v1")
OPENROUTER = ep.resolve("openrouter")


@pytest.fixture
def fake_media(monkeypatch):
    """No real decoding: media.py has its own tests and ffmpeg is not the subject here."""
    monkeypatch.setattr(prompt.media, "load_video", lambda *a, **k: ([object()] * 30, {"x": 1}))
    monkeypatch.setattr(prompt.media, "load_image", lambda *a, **k: object())
    monkeypatch.setattr(prompt.media, "load_audio", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(prompt.media, "video_clip_bytes", lambda *a, **k: (b"mp4", "video/mp4"))
    monkeypatch.setattr(prompt, "_tensor_to_jpeg_b64", lambda t: "BASE64")


def _kinds(parts):
    return [p["type"] for p in parts]


def _text(parts):
    return "\n".join(p.get("text", "") for p in parts if p["type"] == "text")


def _one_of_each(tmp_path):
    for name in ("a.png", "b.mp4", "c.wav"):
        (tmp_path / name).write_bytes(b"x")
    return ReferenceSet([
        Reference(file="a.png", kind="image"),
        Reference(file="b.mp4", kind="video"),
        Reference(file="c.wav", kind="audio"),
    ])


# ---- the degraded shape --------------------------------------------------------


def test_a_video_becomes_six_still_images(fake_media, tmp_path):
    parts = prompt._build_content(
        _one_of_each(tmp_path), str(tmp_path), "go", accepts=LOCAL.accepts
    )
    assert _kinds(parts).count("video_url") == 0
    # one for the reference image, six for the clip
    assert _kinds(parts).count("image_url") == 7


def test_no_audio_of_any_kind_is_sent(fake_media, tmp_path):
    parts = prompt._build_content(
        _one_of_each(tmp_path), str(tmp_path), "go", accepts=LOCAL.accepts
    )
    assert "input_audio" not in _kinds(parts)


def test_the_manifest_says_what_was_withheld(fake_media, tmp_path):
    text = _text(prompt._build_content(
        _one_of_each(tmp_path), str(tmp_path), "go", accepts=LOCAL.accepts
    ))
    assert "REFERENCES" in text
    assert "(6 still frames, no sound)" in text
    assert "<Audio 1> (not sent)" in text
    # The 108-char filenames that used to repeat per frame now appear exactly once.
    assert text.count("b.mp4") == 1


def test_the_frames_are_labelled_in_playback_order_and_say_no_sound(fake_media, tmp_path):
    text = _text(prompt._build_content(
        _one_of_each(tmp_path), str(tmp_path), "go", accepts=LOCAL.accepts
    ))
    # ONE label for the whole run, not one per frame: six labels each claiming to be a
    # video read as six separate items, which is the mis-grouping this format fixes.
    assert "the next 6 images are still frames from ONE video" in text
    assert "They are not separate pictures" in text
    assert "no sound" in text.lower()
    assert text.count("ONE video") == 1


def test_tags_do_not_move_when_the_endpoint_degrades(fake_media, tmp_path):
    """<Picture 1>/<Video 1>/<Audio 1> must mean the same thing on both paths, or a saved
    workflow's sockets stop matching the prompt that describes them."""
    refset = _one_of_each(tmp_path)
    full = _text(prompt._build_content(refset, str(tmp_path), "go", accepts=OPENROUTER.accepts))
    degraded = _text(prompt._build_content(refset, str(tmp_path), "go", accepts=LOCAL.accepts))
    for tag in ("<Picture 1>", "<Video 1>", "<Audio 1>"):
        assert tag in full
        assert tag in degraded


def test_a_short_clip_sends_every_frame_it_has_rather_than_repeating(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt.media, "load_video", lambda *a, **k: ([object()] * 3, None))
    monkeypatch.setattr(prompt, "_tensor_to_jpeg_b64", lambda t: "B64")
    (tmp_path / "b.mp4").write_bytes(b"x")
    parts = prompt._build_content(
        ReferenceSet([Reference(file="b.mp4", kind="video")]),
        str(tmp_path), "go", accepts=LOCAL.accepts,
    )
    assert _kinds(parts).count("image_url") == 3


# ---- openrouter is untouched ---------------------------------------------------


def test_openrouter_still_gets_the_whole_clip_and_its_audio(fake_media, tmp_path):
    parts = prompt._build_content(
        _one_of_each(tmp_path), str(tmp_path), "go", accepts=OPENROUTER.accepts
    )
    assert _kinds(parts).count("video_url") == 1
    assert "input_audio" in _kinds(parts)


def test_omitting_accepts_behaves_exactly_like_openrouter(fake_media, tmp_path):
    """Every existing caller omits it; none of them may change behaviour."""
    refset = _one_of_each(tmp_path)
    default = prompt._build_content(refset, str(tmp_path), "go")
    explicit = prompt._build_content(refset, str(tmp_path), "go", accepts=OPENROUTER.accepts)
    assert _kinds(default) == _kinds(explicit)


# ---- the frame picker ----------------------------------------------------------


@pytest.mark.parametrize("n,count,expected", [
    (30, 6, [0, 6, 12, 17, 23, 29]),
    (6, 6, [0, 1, 2, 3, 4, 5]),
    (3, 6, [0, 1, 2]),
    (1, 6, [0]),
    (0, 6, []),
])
def test_evenly_spaced_covers_the_whole_clip(n, count, expected):
    assert prompt._evenly_spaced(n, count) == expected


def test_evenly_spaced_always_includes_the_first_and_last_frame():
    picked = prompt._evenly_spaced(1000, 6)
    assert picked[0] == 0
    assert picked[-1] == 999


# ---- the debug output ----------------------------------------------------------


def test_the_debug_output_announces_the_degradation(fake_media, tmp_path, monkeypatch):
    monkeypatch.setattr(
        prompt.requests, "post",
        lambda *a, **k: type("R", (), {
            "status_code": 200, "headers": {},
            "json": lambda self: {"choices": [{"message": {"content": "ok"}}]},
        })(),
    )
    sink = []
    prompt.write_prompt(
        references=_one_of_each(tmp_path), input_dir=str(tmp_path), direction="go",
        api_key="", model="m", job_type="standard", debug=sink,
        provider="local", api_base="http://localhost:1234/v1",
    )
    assert len(sink) == 1
    assert "DEGRADED" in sink[0]
    assert "audio" in sink[0] and "video" in sink[0]
    assert "localhost:1234" in sink[0]


# ---- the system prompt and the manifest must agree -----------------------------


@pytest.mark.parametrize("mode", ["standard", "replacement"])
def test_the_system_prompt_documents_the_markers_the_code_actually_emits(mode):
    """The rules key off literal manifest text. If the wording in _build_content drifts
    from the wording in the .md, the writer silently stops honouring the degraded rules
    and starts inventing voice timbres again - with nothing failing.
    """
    text = prompt._read_system_prompt(mode)
    assert "WHEN A REFERENCE WAS WITHHELD" in text
    assert "still frames from ONE video" in text
    assert "not separate pictures" in text.lower()
    assert "(not sent)" in text


@pytest.mark.parametrize("mode", ["standard", "replacement"])
def test_the_system_prompt_forbids_inventing_withheld_audio(mode):
    text = prompt._read_system_prompt(mode).lower()
    assert "retention" in text
    assert "invent" in text


def test_the_emitted_manifest_uses_the_documented_wording(fake_media, tmp_path):
    """The other half of the pin: what the code writes, checked against the .md."""
    text = _text(prompt._build_content(
        _one_of_each(tmp_path), str(tmp_path), "go", accepts=LOCAL.accepts
    ))
    rules = prompt._read_system_prompt("standard")
    for marker in ("still frames from ONE video", "(not sent)"):
        assert marker in text, f"the code stopped emitting {marker!r}"
        assert marker in rules, f"the system prompt stopped documenting {marker!r}"


# ---- the debug render ----------------------------------------------------------


def _rendered(tmp_path, fake_media_applied=True):
    ep = LOCAL
    content = prompt._build_content(_one_of_each(tmp_path), str(tmp_path), "go",
                                    width=1280, height=720, length_seconds=8.0,
                                    accepts=ep.accepts)
    payload = {"model": "m", "messages": [
        {"role": "system", "content": "SYSTEM RULES"},
        {"role": "user", "content": content}]}
    return prompt.render_payload(payload, ep)


def test_no_raw_base64_ever_reaches_the_debug_socket(fake_media, tmp_path):
    """The debug output is a socket users paste into screenshots and Discord. A single
    un-stubbed image would be hundreds of KB of noise; a stubbed one is one line."""
    text = _rendered(tmp_path)
    assert "BASE64" in text
    assert text.count(prompt.BASE64_PLACEHOLDER) >= 7
    # The stub keeps the real data: prefix, so the shape sent is still visible.
    assert "data:image/jpeg;base64," + prompt.BASE64_PLACEHOLDER in text
    # ...and nothing that looks like an actual payload survives.
    for line in text.splitlines():
        assert len(line) < 400, f"suspiciously long line: {line[:80]}"


def test_the_render_names_the_endpoint_it_posts_to(fake_media, tmp_path):
    assert "POST http://localhost:1234/v1/chat/completions" in _rendered(tmp_path)


def test_the_render_says_reasoning_was_not_sent(fake_media, tmp_path):
    assert "reasoning: (not sent to this endpoint)" in _rendered(tmp_path)


def test_the_index_counts_every_part_by_type(fake_media, tmp_path):
    text = _rendered(tmp_path)
    # 1 manifest + 1 image label + 1 image + 6 frame labels + 6 frames = 15
    # 1 manifest + 1 picture label + 1 image + 1 video label + 6 frames = 10
    assert "10 content parts" in text
    assert "7x image_url" in text
    assert "3x text" in text


def test_every_part_is_numbered_so_nothing_is_ambiguous(fake_media, tmp_path):
    text = _rendered(tmp_path)
    assert "[1/10 · text]" in text
    assert "[10/10 · image_url" in text


def test_the_render_carries_direction_target_format_and_manifest(fake_media, tmp_path):
    text = _rendered(tmp_path)
    assert "USER DIRECTION:" in text
    assert "frame: 1280 x 720 (aspect ratio 16:9)" in text
    assert "duration: 8.000 seconds" in text
    assert "REFERENCES" in text
    assert "files:" in text
    assert "<Picture 1>" in text and "<Video 1>" in text


def test_the_system_prompt_is_shown_in_full(fake_media, tmp_path):
    assert "SYSTEM RULES" in _rendered(tmp_path)
