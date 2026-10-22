"""Every model call goes through this module, so the provider is one setting.

Two kinds of call:
  * `structured()`  one request -> JSON that fits a pydantic schema, optionally with page
    images (the extraction step reads scans and phone photos this way);
  * `crew_llm()`    the LLM object CrewAI agents run on (intake, matching, cover note).

Free-tier providers fail in boring ways (429 per-minute quota, 503 "high demand",
a request that hangs), so every call has a timeout and retries with backoff. A call
that still fails raises `LLMError`; callers record it as an error, never as a decision.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.config import DEFAULT_MODELS, Provider, settings

_MAX_ATTEMPTS = 5
_BACKOFF_SECONDS = 8
TIMEOUT_SECONDS = 90
_TRANSIENT = ("429", "500", "503", "resource_exhausted", "unavailable", "overloaded", "timeout", "timed out",
              "deadline", "rate limit", "high demand")

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")


@dataclass(frozen=True)
class Image:
    data: bytes
    mime_type: str  # image/png, image/jpeg


class LLMError(RuntimeError):
    """The provider failed (after retries), refused, or returned something unusable."""


class ProviderNotConfigured(LLMError):
    """No API key for the selected provider."""


@dataclass(frozen=True)
class CallStats:
    model: str
    calls: int
    input_tokens: int = 0
    output_tokens: int = 0


def is_transient(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _TRANSIENT)


def with_retries[R](fn: Callable[[], R], attempts: int = _MAX_ATTEMPTS) -> R:
    """Run `fn`, retrying transient provider errors with linear backoff."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not is_transient(exc) or attempt == attempts - 1:
                raise
            time.sleep(_BACKOFF_SECONDS * (attempt + 1))
    raise AssertionError("unreachable")


def provider_and_model() -> tuple[Provider, str]:
    return settings.llm_provider, settings.llm_model_resolved


def _key(provider: Provider) -> str:
    key = settings.api_key(provider)
    if not key:
        names = {"gemini": "GOOGLE_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "nvidia": "NVIDIA_API_KEY"}
        raise ProviderNotConfigured(f"{names[provider]} is not set (LLM_PROVIDER={provider}). Add it to .env.")
    return key


# --- CrewAI ---------------------------------------------------------------------------------
def crew_llm(temperature: float = 0.0):
    """The LLM CrewAI agents run on. CrewAI 1.x talks to Gemini/Anthropic natively (no LiteLLM)."""
    from crewai import LLM

    provider, model = provider_and_model()
    if provider == "gemini":
        return LLM(model=f"gemini/{model}", api_key=_key(provider), temperature=temperature,
                   timeout=TIMEOUT_SECONDS)
    if provider == "anthropic":
        return LLM(model=f"anthropic/{model}", api_key=_key(provider), temperature=temperature,
                   timeout=TIMEOUT_SECONDS, max_tokens=4000)
    return LLM(model=f"openai/{model}", api_key=_key(provider), base_url="https://integrate.api.nvidia.com/v1",
               temperature=temperature, timeout=TIMEOUT_SECONDS)


# --- one structured call ----------------------------------------------------------------------
def structured[T: BaseModel](
    system: str, prompt: str, schema: type[T], images: list[Image] | None = None
) -> tuple[T, CallStats]:
    """One request whose answer must validate against `schema`.

    If the JSON does not fit, the validation error goes back to the model once so it can
    correct itself (InvoiceIQ: this fixed every remaining schema error).
    """
    provider, model = provider_and_model()
    images = images or []
    call = {"gemini": _gemini, "anthropic": _anthropic, "nvidia": _nvidia}[provider]
    tokens_in = tokens_out = 0
    error = ""
    for calls in (1, 2):
        text, t_in, t_out = with_retries(lambda p=prompt: call(model, system, p, schema, images))
        tokens_in += t_in
        tokens_out += t_out
        try:
            return schema.model_validate_json(_strip_fence(text)), CallStats(model, calls, tokens_in, tokens_out)
        except ValidationError as exc:
            error = str(exc)
            prompt = f"{prompt}\n\nYour previous answer did not match the schema:\n{error}\nReturn corrected JSON."
    raise LLMError(f"{model} returned JSON that does not fit {schema.__name__}: {error[:300]}")


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text


def _gemini(model: str, system: str, prompt: str, schema: type[BaseModel], images: list[Image]) -> tuple[str, int, int]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=_key("gemini"), http_options=types.HttpOptions(timeout=TIMEOUT_SECONDS * 1000))
    config = types.GenerateContentConfig(
        system_instruction=system, temperature=0, max_output_tokens=16_000,
        response_mime_type="application/json", response_json_schema=schema.model_json_schema(),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    parts = [types.Part.from_bytes(data=img.data, mime_type=img.mime_type) for img in images]
    response = client.models.generate_content(model=model, contents=[*parts, prompt], config=config)
    if not response.text:
        raise LLMError(f"Gemini returned no text (prompt_feedback={response.prompt_feedback}).")
    usage = response.usage_metadata
    return (response.text, getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0)


def _anthropic(model: str, system: str, prompt: str, schema: type[BaseModel],
               images: list[Image]) -> tuple[str, int, int]:
    import anthropic

    client = anthropic.Anthropic(api_key=_key("anthropic"), timeout=TIMEOUT_SECONDS)
    content = [{"type": "image", "source": {"type": "base64", "media_type": img.mime_type,
                                            "data": base64.b64encode(img.data).decode()}} for img in images]
    content.append({"type": "text", "text": prompt})
    response = client.messages.parse(model=model, max_tokens=16_000, system=system,
                                     messages=[{"role": "user", "content": content}], output_format=schema)
    if response.stop_reason == "refusal":
        raise LLMError("Claude declined to read this document.")
    text = "".join(block.text for block in response.content if block.type == "text")
    return text, response.usage.input_tokens, response.usage.output_tokens


def _nvidia(model: str, system: str, prompt: str, schema: type[BaseModel], images: list[Image]) -> tuple[str, int, int]:
    from openai import OpenAI

    if images:
        raise LLMError("The NVIDIA text models cannot read images; use LLM_PROVIDER=gemini for scans and photos.")
    client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=_key("nvidia"), timeout=TIMEOUT_SECONDS)
    response = client.chat.completions.create(
        model=model, max_tokens=8_000, temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": f"{system}\n\nAnswer with JSON matching this schema:\n"
                                                f"{schema.model_json_schema()}"},
                  {"role": "user", "content": prompt}],
    )
    usage = response.usage
    return (response.choices[0].message.content or "", usage.prompt_tokens if usage else 0,
            usage.completion_tokens if usage else 0)


def default_model(provider: Provider) -> str:
    return DEFAULT_MODELS[provider]
