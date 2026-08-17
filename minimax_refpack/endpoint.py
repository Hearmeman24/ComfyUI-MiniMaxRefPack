"""Where a chat call goes, what it may carry, and how long to wait for it.

One value object so that "am I talking to OpenRouter or to something on localhost" is
answered once, at the top of a run, instead of being re-derived at each of the three
places that used to hardcode a URL (the writer, the classifier, the model list).

Deliberately pure: no network, no key lookup, no exceptions except the one programmer
error of asking for a local endpoint without saying where. Key POLICY lives in prompt.py,
because `_resolve_api_key` is a pinned surface with its own tests and because raising
PromptError from here would import the module that imports this one.

The security-relevant part: a `local` endpoint is built only from the base URL the user
typed. It can never fall back to OpenRouter's host, which is what makes "nothing leaves
the machine" a property of the code rather than a promise in the README.
"""

from __future__ import annotations

from dataclasses import dataclass

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

PROVIDERS = ("openrouter", "local", "none")
DEFAULT_PROVIDER = "openrouter"

# OpenRouter reports per-model input modalities and the node filters on all four
# (prompt.py:35). Nothing else does, so a local endpoint is assumed to be a vision chat
# model and no more. Confirmed against Ollama's OpenAI-compatibility docs 2026-08-17:
# /v1/chat/completions documents base64 images; video and audio are not mentioned, and
# /v1/models carries no capability field to probe.
_OPENROUTER_ACCEPTS = frozenset({"text", "image", "audio", "video"})
_LOCAL_ACCEPTS = frozenset({"text", "image"})

# OpenRouter's numbers are today's constants, kept byte for byte so the openrouter path
# is unchanged. The local numbers assume the worst realistic case rather than the best:
# a large model on CPU, first token minutes away. A too-long timeout costs a user who
# already knows their machine is slow; a too-short one looks like a broken node.
_OPENROUTER_TIMEOUTS = {"chat": 120, "classify": 20, "models": 5}
_LOCAL_TIMEOUTS = {"chat": 900, "classify": 120, "models": 5}


# Where a local server is likely to be. Probed in this order and reported in this order,
# so the answer is stable between runs. 127.0.0.1 rather than "localhost" on purpose: it
# skips name resolution, which on a machine with a slow or IPv6-first resolver is most of
# the probe's latency.
#
# 8000 and 8080 are shared with half the dev tooling in existence, so finding something
# listening there proves nothing. That is why the probe validates the RESPONSE SHAPE
# before reporting a server, instead of trusting the port.
LOCAL_CANDIDATES = (
    ("LM Studio", "http://127.0.0.1:1234/v1"),
    ("Ollama", "http://127.0.0.1:11434/v1"),
    ("llama.cpp", "http://127.0.0.1:8080/v1"),
    ("vLLM", "http://127.0.0.1:8000/v1"),
    ("Jan", "http://127.0.0.1:1337/v1"),
    ("text-generation-webui", "http://127.0.0.1:5000/v1"),
)

# Literal loopback only, and deliberately NOT resolved. Accepting a hostname and looking
# it up would turn the detect route into a scanner: ComfyUI often runs with no auth in
# front of it, so anyone holding the proxy URL could ask this process to probe arbitrary
# hosts and read back which ones answered. Refusing to resolve at all closes that, and
# closes DNS rebinding with it. A user with a server somewhere else still types the URL
# into api_base by hand; they just do not get to verify it through this route.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def is_loopback(url: str) -> bool:
    """True when this URL names the machine ComfyUI itself is running on, literally."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    return host in _LOOPBACK_HOSTS


@dataclass(frozen=True)
class Endpoint:
    provider: str
    chat_url: str
    models_url: str
    accepts: frozenset
    chat_timeout: int
    classify_timeout: int
    models_timeout: int
    sends_reasoning: bool
    requires_key: bool
    is_openrouter: bool

    def carries(self, kind: str) -> bool:
        """True when a content part of this kind can go on the wire as-is."""
        return kind in self.accepts

    @property
    def degrades(self) -> bool:
        """True when this endpoint cannot take everything the reference set may hold."""
        return not _OPENROUTER_ACCEPTS <= self.accepts

    def describe(self) -> str:
        """One short phrase for a log line or a node warning."""
        if self.is_openrouter:
            return "openrouter"
        return f"local ({self.chat_url})"


def normalize_provider(raw) -> str:
    """Any value that can reach the widget -> one of PROVIDERS.

    Total by design. `prompt_provider` replaced a BOOLEAN in the same widget slot, so a
    workflow saved at 0.3.1 restores True or False here, and litegraph will happily hand
    over whatever was in the JSON. An unrecognised value resolves to the default rather
    than raising, mirroring how an unknown reasoning_effort is dropped instead of sent
    (prompt.py:510-511): a typo must never cost someone a whole queue.
    """
    if raw is True:
        return "openrouter"
    if raw is False:
        return "none"
    text = str(raw or "").strip().lower()
    if text in PROVIDERS:
        return text
    if text == "true":
        return "openrouter"
    if text == "false":
        return "none"
    return DEFAULT_PROVIDER


def resolve(provider, api_base: str = "") -> Endpoint:
    """The endpoint for a provider. `none` never gets here - it makes no call at all."""
    provider = normalize_provider(provider)

    if provider == "local":
        base = (api_base or "").strip().rstrip("/")
        if not base:
            raise ValueError(
                "prompt_provider is 'local' but api_base is empty: give the node the "
                "base URL of your server, for example http://localhost:1234/v1"
            )
        return Endpoint(
            provider="local",
            chat_url=f"{base}/chat/completions",
            models_url=f"{base}/models",
            accepts=_LOCAL_ACCEPTS,
            chat_timeout=_LOCAL_TIMEOUTS["chat"],
            classify_timeout=_LOCAL_TIMEOUTS["classify"],
            models_timeout=_LOCAL_TIMEOUTS["models"],
            sends_reasoning=False,
            requires_key=False,
            is_openrouter=False,
        )

    return Endpoint(
        provider="openrouter",
        chat_url=f"{OPENROUTER_BASE}/chat/completions",
        models_url=f"{OPENROUTER_BASE}/models",
        accepts=_OPENROUTER_ACCEPTS,
        chat_timeout=_OPENROUTER_TIMEOUTS["chat"],
        classify_timeout=_OPENROUTER_TIMEOUTS["classify"],
        models_timeout=_OPENROUTER_TIMEOUTS["models"],
        sends_reasoning=True,
        requires_key=True,
        is_openrouter=True,
    )
