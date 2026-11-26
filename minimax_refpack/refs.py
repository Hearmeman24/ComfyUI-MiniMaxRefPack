"""The reference set: its shape, its pack file, and the tags MiniMax will assign it.

FROZEN CONTRACT. Every other module in this pack reads this one. Pure Python — no torch,
no ComfyUI imports — so it can be tested anywhere.

The tag numbering below is not a convention we invented. It is read out of
ComfyUI v0.32.0 `comfy_extras/nodes_minimax_h3.py:216-274`, which builds the tokenizer's
presentation list in this order:

  1. every reference image, in slot order, skipping empty slots      -> <Picture 1..n>  (:231)
  2. per reference video, in slot order:
       its soundtrack's audio entry is appended FIRST                -> <Audio a>       (:259)
       then the video itself                                        -> <Video 1..n>    (:263)
  3. every standalone reference audio, in slot order                 -> <Audio a>       (:273)

So <Audio N> counts video soundtracks before standalone audio, and <Video N> is numbered
independently of <Audio N>. Ordinals are per-type, 1-based, over the COMPACTED list.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

Kind = Literal["image", "video", "audio"]

# Hard caps, from the Autogrow group definitions at nodes_minimax_h3.py:183,187,191,195.
MAX_IMAGES = 9
MAX_VIDEOS = 3
MAX_AUDIOS = 3
MAX_BY_KIND: dict[str, int] = {"image": MAX_IMAGES, "video": MAX_VIDEOS, "audio": MAX_AUDIOS}

KINDS: tuple[Kind, ...] = ("image", "video", "audio")


class ReferenceError(ValueError):
    """A reference set that MiniMax could not accept."""


def validate_crop(crop: Any) -> list[float]:
    """[x, y, w, h] as FRACTIONS of the source frame -> validated float list.

    Fractions, not pixels, deliberately: the same filename gets re-uploaded at a
    different resolution (nodes.py's _files_signature exists because of exactly that),
    and a pixel rect would then silently point at the wrong region. Raises
    ReferenceError unless the rect has positive area and sits inside the unit square.
    """
    if (
        not isinstance(crop, (list, tuple)) or len(crop) != 4
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in crop)
    ):
        raise ReferenceError(f"crop must be [x, y, w, h] fractions of the source, got {crop!r}")
    x, y, w, h = (float(v) for v in crop)
    if w <= 0.0 or h <= 0.0:
        raise ReferenceError(f"crop has no area: {crop!r}")
    # The 1e-9 absorbs float noise from a UI that rounds to 4 decimals, nothing more.
    if x < 0.0 or y < 0.0 or x + w > 1.0 + 1e-9 or y + h > 1.0 + 1e-9:
        raise ReferenceError(f"crop must sit inside 0..1 on both axes: {crop!r}")
    return [x, y, w, h]


def validate_trim(trim: Any) -> list[float]:
    """[start_seconds, end_seconds] -> validated float list. 0 <= start < end."""
    if (
        not isinstance(trim, (list, tuple)) or len(trim) != 2
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in trim)
    ):
        raise ReferenceError(f"trim must be [start_seconds, end_seconds], got {trim!r}")
    start, end = (float(v) for v in trim)
    if start < 0.0:
        raise ReferenceError(f"trim start must be >= 0, got {start}")
    if end <= start:
        raise ReferenceError(f"trim end must come after its start, got {trim!r}")
    return [start, end]


@dataclass
class Reference:
    """One reference asset, named by its filename inside the ComfyUI input directory."""

    kind: Kind
    file: str
    # video only: also emit the clip's soundtrack on the index-matched video_audio_N output.
    # ON by default - a reference video's sound is part of the reference, and a silent
    # clip is handled downstream (the probe clears the flag when there is no audio track).
    use_soundtrack: bool = True
    # Optional edits, applied at load time (media.py). crop = [x, y, w, h] fractions of
    # the source (see validate_crop for why not pixels); image and video only.
    # trim = [start_seconds, end_seconds]; video and audio only. None = untouched, and
    # both are OMITTED from to_dict() when unset so pre-edit references_json values and
    # v1 pack files keep loading and re-serialising unchanged (PACK_VERSION stays 1).
    crop: list[float] | None = None
    trim: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "file": self.file}
        if self.kind == "video":
            d["use_soundtrack"] = bool(self.use_soundtrack)
        if self.crop is not None:
            d["crop"] = list(self.crop)
        if self.trim is not None:
            d["trim"] = list(self.trim)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Reference":
        kind = d.get("kind")
        if kind not in KINDS:
            raise ReferenceError(f"unknown reference kind {kind!r}")
        file = d.get("file")
        if not isinstance(file, str) or not file.strip():
            raise ReferenceError("reference is missing a file name")
        crop = d.get("crop")
        trim = d.get("trim")
        return cls(
            kind=kind,
            file=file.strip(),
            use_soundtrack=bool(d.get("use_soundtrack", True)) if kind == "video" else False,
            # like use_soundtrack, an inapplicable field is dropped rather than fatal
            crop=validate_crop(crop) if crop is not None and kind in ("image", "video") else None,
            trim=validate_trim(trim) if trim is not None and kind in ("video", "audio") else None,
        )


@dataclass
class TaggedReference:
    """A reference plus the socket it lands on and the tag MiniMax will give it."""

    ref: Reference
    slot: int                 # 1-based index within its own kind == its socket ordinal
    tag: str                  # "<Picture 2>" / "<Video 1>" / "<Audio 2>"
    audio_tag: str | None = None   # videos with use_soundtrack: the tag of their soundtrack

    @property
    def kind(self) -> Kind:
        return self.ref.kind

    @property
    def file(self) -> str:
        return self.ref.file

    def to_dict(self) -> dict[str, Any]:
        d = self.ref.to_dict()
        d.update({"slot": self.slot, "tag": self.tag})
        if self.audio_tag is not None:
            d["audio_tag"] = self.audio_tag
        return d


@dataclass
class ReferenceSet:
    """An ordered set of references. Order within a kind is the socket order."""

    references: list[Reference] = field(default_factory=list)

    # ---- construction -------------------------------------------------------

    @classmethod
    def from_json(cls, raw: str | None) -> "ReferenceSet":
        """Parse the node's hidden `references_json` widget. Empty/blank -> empty set."""
        if raw is None:
            return cls()
        raw = raw.strip()
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ReferenceError(f"references are not valid JSON: {e}") from e
        return cls.from_obj(data)

    @classmethod
    def from_obj(cls, data: Any) -> "ReferenceSet":
        """Accept either a bare list of references or a {"references": [...]} envelope."""
        if isinstance(data, dict):
            data = data.get("references", [])
        if not isinstance(data, list):
            raise ReferenceError("references must be a list")
        return cls([Reference.from_dict(d) for d in data if isinstance(d, dict)])

    def to_json(self) -> str:
        return json.dumps({"references": [r.to_dict() for r in self.references]})

    # ---- queries ------------------------------------------------------------

    def of_kind(self, kind: Kind) -> list[Reference]:
        return [r for r in self.references if r.kind == kind]

    def counts(self) -> dict[str, int]:
        return {k: len(self.of_kind(k)) for k in KINDS}

    def is_empty(self) -> bool:
        return not self.references

    def validate(self) -> None:
        """Raise if the set exceeds MiniMax's hard caps."""
        for kind in KINDS:
            n = len(self.of_kind(kind))
            cap = MAX_BY_KIND[kind]
            if n > cap:
                raise ReferenceError(
                    f"MiniMax H3 accepts at most {cap} reference {kind}s, got {n}"
                )

    def validate_kinds(self, allowed: Iterable[str]) -> None:
        """Raise unless every reference is one of `allowed`, naming the first that is not.

        The image-edit node's server-side gate. Its UI only ever offers images, but
        `references_json` is a plain widget value and a /prompt POST can carry anything,
        so the node that has no video or audio sockets refuses such a set outright rather
        than quietly dropping the reference the user thought they attached.
        """
        allowed = tuple(allowed)
        for r in self.references:
            if r.kind not in allowed:
                raise ReferenceError(
                    f"this node takes {' and '.join(k + 's' for k in allowed)} only, "
                    f"got a {r.kind} reference ({r.file})"
                )

    def missing_files(self, input_dir: str) -> list[str]:
        """Filenames that do not resolve under the ComfyUI input directory, in set order."""
        missing = []
        for r in self.references:
            path = os.path.join(input_dir, r.file)
            if not os.path.isfile(path):
                missing.append(r.file)
        return missing

    def resolve(self, input_dir: str) -> list[str]:
        """Alias of missing_files: a pack "resolves" when this comes back empty."""
        return self.missing_files(input_dir)

    # ---- the tag rule -------------------------------------------------------

    def assign_tags(self) -> list[TaggedReference]:
        """Compute the tag MiniMax will assign each reference. See module docstring."""
        self.validate()
        tagged: list[TaggedReference] = []

        for i, ref in enumerate(self.of_kind("image"), start=1):
            tagged.append(TaggedReference(ref=ref, slot=i, tag=f"<Picture {i}>"))

        audio_n = 0
        for i, ref in enumerate(self.of_kind("video"), start=1):
            audio_tag = None
            if ref.use_soundtrack:
                # the soundtrack's <Audio j> is emitted BEFORE its <Video k> (:259 then :263)
                audio_n += 1
                audio_tag = f"<Audio {audio_n}>"
            tagged.append(
                TaggedReference(ref=ref, slot=i, tag=f"<Video {i}>", audio_tag=audio_tag)
            )

        for i, ref in enumerate(self.of_kind("audio"), start=1):
            audio_n += 1
            tagged.append(TaggedReference(ref=ref, slot=i, tag=f"<Audio {audio_n}>"))

        return tagged

    def tag_map(self) -> dict[str, str]:
        """{filename: tag} for prompt context. A video with a soundtrack appears once,
        with both tags joined, so the model can address either."""
        out: dict[str, str] = {}
        for t in self.assign_tags():
            out[t.file] = t.tag if t.audio_tag is None else f"{t.tag} {t.audio_tag}"
        return out


# ---- output socket names ---------------------------------------------------

def output_names() -> tuple[str, ...]:
    """The node's 20 outputs, in declaration order.

    image_1..9 -> ref_images.ref_image_0..8
    video_1..3 -> ref_videos.ref_video_0..2
    video_audio_1..3 -> ref_video_audios.ref_video_audio_0..2  (index-paired to video_N)
    audio_1..3 -> ref_audios.ref_audio_0..2
    prompt -> the MiniMax node's prompt widget
    debug -> the full payload that went to the LLM, for a preview/console node

    APPEND-ONLY. ComfyUI stores a link by its output SLOT INDEX, so inserting a socket
    anywhere but the end silently re-points every existing link in a saved workflow.
    `debug` went on the end for exactly that reason - users have graphs wired to these
    sockets by hand.
    """
    names: list[str] = [f"image_{i}" for i in range(1, MAX_IMAGES + 1)]
    names += [f"video_{i}" for i in range(1, MAX_VIDEOS + 1)]
    names += [f"video_audio_{i}" for i in range(1, MAX_VIDEOS + 1)]
    names += [f"audio_{i}" for i in range(1, MAX_AUDIOS + 1)]
    names.append("prompt")
    names.append("debug")
    return tuple(names)


# The trailing STRING sockets, in output_names() order. Everything before them is a
# media socket that starts as None; these start as "".
_STRING_OUTPUTS = ("prompt", "debug")


def output_types() -> tuple[str, ...]:
    types: list[str] = ["IMAGE"] * MAX_IMAGES
    types += ["IMAGE"] * MAX_VIDEOS
    types += ["AUDIO"] * MAX_VIDEOS
    types += ["AUDIO"] * MAX_AUDIOS
    types += ["STRING"] * len(_STRING_OUTPUTS)
    return tuple(types)


# ---- the image-edit node's outputs --------------------------------------------
#
# MiniMaxH3ImageReferencePack feeds core's MiniMaxH3ImageConditioning, whose `images`
# Autogrow (image_1..image_N, nodes_minimax_h3.py:758) takes image_1 as the picture to
# EDIT and the rest as references, tagged <Picture n> in slot order with empty slots
# skipped (:727-728) - the same compaction the image half of assign_tags() models.
# Same APPEND-ONLY rule as output_names(): this tuple is a wire format.

IMAGE_EDIT_KINDS: tuple[Kind, ...] = ("image",)


def image_output_names() -> tuple[str, ...]:
    """image_1..9, prompt, debug - the image-edit node's 11 outputs, in order."""
    return tuple([f"image_{i}" for i in range(1, MAX_IMAGES + 1)] + list(_STRING_OUTPUTS))


def image_output_types() -> tuple[str, ...]:
    return tuple(["IMAGE"] * MAX_IMAGES + ["STRING"] * len(_STRING_OUTPUTS))


def empty_image_outputs() -> list[Any]:
    return [None] * MAX_IMAGES + [""] * len(_STRING_OUTPUTS)


def slot_index(name: str) -> int:
    """Index of an output socket by name, for building the 20-tuple."""
    return output_names().index(name)


def empty_outputs() -> list[Any]:
    """A full output tuple of Nones (plus empty strings for prompt/debug)."""
    out: list[Any] = [None] * (len(output_names()) - len(_STRING_OUTPUTS))
    out += [""] * len(_STRING_OUTPUTS)
    return out


def iter_kind_slots(kind: Kind) -> Iterable[str]:
    """Socket names for a kind, in slot order."""
    if kind == "image":
        return (f"image_{i}" for i in range(1, MAX_IMAGES + 1))
    if kind == "video":
        return (f"video_{i}" for i in range(1, MAX_VIDEOS + 1))
    return (f"audio_{i}" for i in range(1, MAX_AUDIOS + 1))
