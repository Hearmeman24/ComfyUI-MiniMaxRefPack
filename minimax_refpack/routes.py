"""The HTTP surface the node's browser UI calls.

Importing this module is what registers the routes (see `__init__.py`), so registration
happens at import time, below. `server` and `folder_paths` only exist inside a running
ComfyUI process, so both imports are guarded — a plain `pytest` run (no ComfyUI on
sys.path) must not explode, it just leaves the routes unregistered. Handler functions are
still plain `async def`s that tests call directly with a fake request stub.

SECURITY: `file` and `name` arrive off the wire and get turned into filesystem paths. The
server may run `ComfyUI --listen` with no auth in front of it, so a traversal
here reads the whole filesystem. Every path this module builds goes through `_safe_join`,
which mirrors the containment check ComfyUI's own upload route uses:
`os.path.abspath` + `os.path.commonpath` at ComfyUI server.py:412-415 (v0.32.0).
"""

from __future__ import annotations

import math
import os

from aiohttp import web

from .refs import ReferenceError, validate_crop
from . import endpoint, logs, media, prompt

try:
    from server import PromptServer
except ImportError:  # pragma: no cover - exercised only when ComfyUI isn't on sys.path
    PromptServer = None

try:
    import folder_paths
except ImportError:  # pragma: no cover - same as above
    folder_paths = None


def _safe_join(base_dir: str, name: str) -> str | None:
    """Resolve `name` under `base_dir`, or None if it could escape it.

    The ".." substring block is stricter than the commonpath check alone: it also
    catches a component like "..%2f..%2fetc" which, undecoded, contains no real path
    separator and would resolve harmlessly inside base_dir on this filesystem, but is a
    traversal payload once a client or proxy decodes it upstream of us.
    """
    if not name or not isinstance(name, str):
        return None
    if ".." in name or name.startswith("/") or name.startswith("\\") or os.path.isabs(name):
        return None
    base_dir = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(base_dir, name))
    # CU/server.py:412-415 - the same containment check the stock upload route uses.
    if os.path.commonpath((base_dir, target)) != base_dir:
        return None
    return target


# ---- handlers ---------------------------------------------------------------
# Plain functions, not nested under the route decorators, so tests can call them
# directly with a fake request (a stub exposing .query, .match_info, async .json()).


async def probe_route(request: web.Request) -> web.Response:
    input_dir = folder_paths.get_input_directory()
    name = request.query.get("file", "")
    path = _safe_join(input_dir, name)
    if path is None or not os.path.isfile(path):
        logs.warn("probe", file=name, status=404)
        return web.Response(status=404)
    info = media.probe(path)
    logs.debug("probe", file=name, kind=info["kind"], duration=info["duration"],
               has_audio=info["has_audio"])
    return web.json_response(info)


def _parse_crop_param(raw: str | None) -> list[float] | None:
    """`crop=x,y,w,h` comma-separated fractions -> validated list; None when absent.
    Raises ValueError on junk - the route turns that into a 400, never a traceback."""
    if raw is None:
        return None
    try:
        crop = [float(p) for p in raw.split(",")]
    except ValueError:
        raise ValueError(f"crop must be four comma-separated numbers, got {raw!r}") from None
    try:
        return validate_crop(crop)
    except ReferenceError as e:
        raise ValueError(str(e)) from None


def _parse_seconds_param(raw: str | None) -> float | None:
    """`t=<seconds>` -> float; None when absent. Raises ValueError on junk."""
    if raw is None:
        return None
    try:
        t = float(raw)
    except ValueError:
        raise ValueError(f"t must be a number of seconds, got {raw!r}") from None
    if not math.isfinite(t) or t < 0:
        raise ValueError(f"t must be a finite number of seconds >= 0, got {raw!r}")
    return t


async def thumb_route(request: web.Request) -> web.Response:
    input_dir = folder_paths.get_input_directory()
    name = request.query.get("file", "")
    path = _safe_join(input_dir, name)
    if path is None or not os.path.isfile(path):
        # Not chatter: a tile asking for a file the server cannot serve is the exact
        # shape of the "no preview" bug, and it should be visible without --verbose.
        logs.warn("thumb", file=name, status=404)
        return web.Response(status=404)
    try:
        crop = _parse_crop_param(request.query.get("crop"))
        at_seconds = _parse_seconds_param(request.query.get("t"))
    except ValueError as e:
        logs.warn("thumb", file=name, status=400, reason=str(e))
        return web.Response(status=400, text=str(e))
    # One line per tile per redraw - DEBUG, or a full reference set drowns the console.
    logs.debug("thumb", file=name, crop=crop, t=at_seconds)
    return web.Response(
        body=media.thumbnail_png(path, crop=crop, at_seconds=at_seconds),
        content_type="image/png",
    )


async def list_files_route(request: web.Request) -> web.Response:
    kind = request.query.get("kind", "")
    if kind not in ("image", "video", "audio"):
        return web.Response(status=400, text="kind must be image, video or audio")
    input_dir = folder_paths.get_input_directory()
    # CU/folder_paths.py:229 - the same content-type filter the core input pickers use.
    files = [f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f))]
    return web.json_response({"files": folder_paths.filter_files_content_types(files, [kind])})


async def system_prompt_route(request: web.Request) -> web.Response:
    """Read-only: the modal's "Load default" button. The user's edit lives in the
    workflow's `system_prompt` widget, never written back here - there is no write
    route for this on purpose."""
    return web.json_response({"default": prompt._read_system_prompt()})


async def detect_route(request):
    """Which OpenAI-compatible servers this ComfyUI can reach on the usual local ports.

    Runs the probe HERE rather than in the browser, deliberately. `localhost` has to mean
    whatever the ComfyUI process can reach: a Dockerised or pod ComfyUI must be told the
    truth about its own network instead of the user's laptop, and the browser could not
    read the responses anyway without every local server opting into CORS.

    SECURITY: `base` is loopback-only and is never resolved (endpoint.is_loopback). This
    process will happily connect to whatever it is told to, and ComfyUI often runs with no
    auth in front of it, so an unrestricted version of this route is a port scanner and an
    SSRF probe for anyone holding the URL. Only a literal 127.0.0.1 / localhost / ::1 is
    accepted; anything else is refused without a connection being attempted.
    """
    base = (request.query.get("base") or "").strip()
    if base:
        if not endpoint.is_loopback(base):
            logs.log("detect_refused", reason="not_loopback")
            return web.json_response(
                {"error": "only loopback addresses can be probed from here; type a "
                          "remote URL into api_base by hand instead"},
                status=400,
            )
        models = prompt._models_at(base)
        if models is None:
            return web.json_response({"servers": []})
        return web.json_response({"servers": [{"label": "custom", "base": base,
                                               "models": models}]})

    return web.json_response({"servers": prompt.detect_local_servers()})


# ---- registration -------------------------------------------------------------

_ROUTES: tuple[tuple[str, str, object], ...] = (
    ("GET", "/minimax_refpack/probe", probe_route),
    ("GET", "/minimax_refpack/thumb", thumb_route),
    ("GET", "/minimax_refpack/files", list_files_route),
    ("GET", "/minimax_refpack/system_prompt", system_prompt_route),
    ("GET", "/minimax_refpack/detect", detect_route),
)

if PromptServer is not None and getattr(PromptServer, "instance", None) is not None:
    _method_fn = {"GET": PromptServer.instance.routes.get}
    for _method, _path, _handler in _ROUTES:
        _method_fn[_method](_path)(_handler)
