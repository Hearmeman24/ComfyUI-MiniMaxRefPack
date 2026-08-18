"""Drag-and-drop file routing, as the shipped browser code actually decides it.

Dropped files are sorted by what they ARE, not by where the pointer landed, which is
what lets one gesture fill all three sections. The router is extracted from
web/refpack.js between its MMRP-DROP markers and executed under node, so this tests
the shipped code rather than a copy of it that can drift out from under the tests.

SCOPE, honestly: this covers the pure router only. The drag listeners, the
preventDefault that stops ComfyUI hijacking the drop into a LoadImage node, and
addFiles' caps and uploads are NOT exercised here - they need a real browser.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
REFPACK_JS = REPO_ROOT / "web" / "refpack.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _extract_drop_js() -> str:
    """The marked block from the shipped file, or a hard failure if the markers moved."""
    text = REFPACK_JS.read_text()
    start = text.find("// >>> MMRP-DROP")
    end = text.find("// <<< MMRP-DROP")
    assert start != -1 and end != -1, (
        "the MMRP-DROP markers are gone from web/refpack.js - this test extracts the "
        "real shipped code through them, so removing them silently stops the drop "
        "router from being covered at all"
    )
    return text[start:end]


def _run_js(expression: str):
    script = _extract_drop_js() + f"\nconsole.log(JSON.stringify({expression}));\n"
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def _bucket(files):
    """Route `files` and report only shapes that survive the node boundary.

    Never the file objects themselves: JSON.stringify of a real File is `{}`, so a
    test that round-tripped them would pass against a router that dropped everything.
    Names in, counts and rejected names out.
    """
    return _run_js(
        "(() => { const r = bucketDroppedFiles(" + json.dumps(files) + ");"
        " return {image: r.buckets.image.map(f => f.name),"
        "         video: r.buckets.video.map(f => f.name),"
        "         audio: r.buckets.audio.map(f => f.name),"
        "         rejected: r.rejected}; })()"
    )


def _file(name, type_=""):
    return {"name": name, "type": type_}


# ---- the block is still there --------------------------------------------------


def test_the_markers_survive_in_the_shipped_file():
    """The migration test's own failure mode: markers deleted, coverage silently zero."""
    assert "bucketDroppedFiles" in _extract_drop_js()


@requires_node
def test_the_extracted_block_runs_with_nothing_else_around_it():
    """The constraint that makes every other test here possible: the block closes over
    no module-level constant and touches no DOM. A closure over KINDS or CAPS compiles
    fine in the browser and dies right here with a ReferenceError."""
    assert _run_js("typeof bucketDroppedFiles") == "function"


# ---- routing -------------------------------------------------------------------


@requires_node
@pytest.mark.parametrize("mime,kind", [
    ("image/png", "image"),
    ("image/webp", "image"),
    ("video/mp4", "video"),
    ("video/quicktime", "video"),
    ("audio/mpeg", "audio"),
    ("audio/wav", "audio"),
])
def test_each_kind_lands_in_its_own_section(mime, kind):
    got = _bucket([_file("a.bin", mime)])
    assert got[kind] == ["a.bin"]
    assert got["rejected"] == []
    for other in ("image", "video", "audio"):
        if other != kind:
            assert got[other] == []


@requires_node
def test_a_mixed_drop_splits_across_all_three_in_one_gesture():
    """The whole point of routing by MIME rather than by drop position."""
    got = _bucket([
        _file("one.png", "image/png"),
        _file("clip.mp4", "video/mp4"),
        _file("two.jpg", "image/jpeg"),
        _file("track.mp3", "audio/mpeg"),
    ])
    assert got["image"] == ["one.png", "two.jpg"]
    assert got["video"] == ["clip.mp4"]
    assert got["audio"] == ["track.mp3"]
    assert got["rejected"] == []


@requires_node
def test_drop_order_is_preserved_within_a_kind():
    got = _bucket([_file(f"{i}.png", "image/png") for i in range(5)])
    assert got["image"] == ["0.png", "1.png", "2.png", "3.png", "4.png"]


# ---- refusal -------------------------------------------------------------------


@requires_node
def test_a_non_media_file_is_refused_by_name():
    """Named, not silently swallowed: the caller alerts with exactly this list."""
    got = _bucket([_file("notes.pdf", "application/pdf"), _file("ok.png", "image/png")])
    assert got["rejected"] == ["notes.pdf"]
    assert got["image"] == ["ok.png"]


@requires_node
def test_a_folder_or_unnamed_entry_still_reports_something_alertable():
    """An empty name would render as an empty string in the alert - useless to read."""
    got = _bucket([{"name": "", "type": ""}])
    assert got["rejected"] == ["(unnamed)"]


@requires_node
def test_an_empty_drop_is_not_an_error():
    got = _bucket([])
    assert got == {"image": [], "video": [], "audio": [], "rejected": []}


# ---- the extension fallback ----------------------------------------------------


@requires_node
@pytest.mark.parametrize("name,kind", [
    ("clip.mkv", "video"),
    ("song.flac", "audio"),
    ("shot.MOV", "video"),       # case is the OS's business, not ours
    ("frame.avif", "image"),
])
def test_an_empty_mime_type_falls_back_to_the_extension(name, kind):
    """Browsers leave `type` empty for the less common containers."""
    got = _bucket([_file(name, "")])
    assert got[kind] == [name]
    assert got["rejected"] == []


@requires_node
@pytest.mark.parametrize("name,kind", [
    ("clip.mkv", "video"),
    ("frame.heic", "image"),
    ("song.opus", "audio"),
])
def test_octet_stream_falls_back_the_same_way(name, kind):
    """Same failure, different spelling - some systems type these as a blob."""
    got = _bucket([_file(name, "application/octet-stream")])
    assert got[kind] == [name]
    assert got["rejected"] == []


@requires_node
def test_an_unknown_extension_with_no_mime_type_is_refused_not_guessed():
    got = _bucket([_file("archive.tar.gz", ""), _file("noextension", "")])
    assert got["rejected"] == ["archive.tar.gz", "noextension"]


@requires_node
def test_the_mime_type_wins_over_a_misleading_extension():
    got = _bucket([_file("actually_a_video.png", "video/mp4")])
    assert got["video"] == ["actually_a_video.png"]
    assert got["image"] == []


@requires_node
@pytest.mark.parametrize("mime", ["application/pdf", "text/plain", "application/zip"])
def test_a_confident_non_media_type_is_refused_even_with_a_media_extension(mime):
    """The fallback is for a browser that said NOTHING useful, not for one that was
    clear the file is not media. Without this the extension would win and a PDF would
    upload as an image reference - a broken ref instead of a visible refusal."""
    got = _bucket([_file("scan.png", mime)])
    assert got["rejected"] == ["scan.png"]
    assert got["image"] == []


@requires_node
def test_the_mime_prefix_match_is_case_insensitive():
    """Nothing guarantees the case a browser reports, and the lowercasing is easy to
    drop in a later tidy-up."""
    got = _bucket([_file("a.png", "IMAGE/PNG"), _file("b.mp4", "Video/MP4")])
    assert got["image"] == ["a.png"] and got["video"] == ["b.mp4"]
    assert got["rejected"] == []


@requires_node
@pytest.mark.parametrize("missing", ["null", "undefined"])
def test_no_files_at_all_is_not_a_crash(missing):
    """The drop handler reads e.dataTransfer.files; a build that hands back nothing must
    not throw inside an async listener, where it would surface only as a rejection."""
    out = _run_js(
        "(() => { const r = bucketDroppedFiles(" + missing + ");"
        " return [r.buckets.image.length, r.buckets.video.length,"
        "         r.buckets.audio.length, r.rejected.length]; })()"
    )
    assert out == [0, 0, 0, 0]


@requires_node
def test_the_declared_extensions_cover_what_the_pickers_accept():
    """The three prefixes here are the ones KIND_UPLOAD_META hands the file pickers;
    a kind losing its extension list would silently start rejecting typeless files."""
    ext = _run_js("DROP_EXTENSIONS")
    assert set(ext) == {"image", "video", "audio"}
    for kind, names in ext.items():
        assert names, kind
        assert names == [n.lower() for n in names], kind
