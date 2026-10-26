from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

from .catalog import ModelInfo, ProviderInfo
from .config import Settings
from .registry import AUTO_ALIASES, Registry
from .store import KeyStore, Stats, estimate_tokens


class RouterError(Exception):
    def __init__(self, status: int, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": {
                "message": self.message,
                "type": self.code,
                "code": self.code,
            }
        }
        if self.details is not None:
            payload["error"]["details"] = self.details
        return payload


@dataclass
class ChatPlan:
    provider: ProviderInfo
    model: ModelInfo
    url: str
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass
class ChatResult:
    data: dict[str, Any]
    provider: ProviderInfo
    model: ModelInfo
    latency_ms: float


@dataclass
class StreamResult:
    response: httpx.Response
    client: httpx.Client
    provider: ProviderInfo
    model: ModelInfo
    started: float
    payload: dict[str, Any]


class Router:
    def __init__(self, registry: Registry, settings: Settings, keys: KeyStore, stats: Stats) -> None:
        self.registry = registry
        self.settings = settings
        self.keys = keys
        self.stats = stats

    # ------------------------------------------------------------------ utils
    def _client(self) -> httpx.Client:
        timeout = httpx.Timeout(
            connect=float(self.settings.get("connect_timeout_sec") or 15),
            read=float(self.settings.get("request_timeout_sec") or 90),
            write=30.0,
            pool=30.0,
        )
        proxy = str(self.settings.get("upstream_proxy") or "").strip()
        kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
        if proxy:
            kwargs["proxy"] = proxy
            kwargs["trust_env"] = False
        else:
            kwargs["trust_env"] = bool(self.settings.get("trust_env_proxy"))
        return httpx.Client(**kwargs)

    def _headers(self, provider: ProviderInfo, *, stream: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "FreeAIHub/0.1 (+local free-tier gateway)",
        }
        key = self.keys.get(provider)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _url(self, provider: ProviderInfo) -> str:
        return provider.base_url.rstrip("/") + "/chat/completions"

    @staticmethod
    def _is_auto(model_id: str) -> bool:
        return str(model_id or "").strip().lower() in AUTO_ALIASES

    def _check_provider(self, provider: ProviderInfo, model: ModelInfo) -> None:
        if provider.restricted:
            raise RouterError(403, "provider_restricted", provider.notes or "This provider is restricted.")
        if not provider.routable_candidate:
            raise RouterError(400, "provider_not_routable", f"{provider.name} is catalog-only.")
        if provider.keyless and not self.settings.get("enable_keyless", True):
            raise RouterError(403, "keyless_disabled", "Keyless providers are disabled in Settings.")
        if not provider.keyless and not self.settings.get("enable_keyed", True):
            raise RouterError(403, "keyed_disabled", "Keyed providers are disabled in Settings.")
        if provider.key_required and not self.keys.configured(provider):
            raise RouterError(
                503,
                "provider_needs_key",
                f"{provider.name} requires an API key. Add it in Providers or set {provider.key_env or 'its env var'}.",
            )
        if provider.status in {"offline", "needs_key", "invalid_key", "catalog", "restricted"}:
            raise RouterError(
                503,
                "provider_unavailable",
                f"{provider.name} is currently {provider.status}. Refresh providers or add a key.",
            )
        if model.kind != "chat":
            raise RouterError(400, "model_not_chat", "This model is not a chat model.")
        if not model.enabled or not self.registry.policy_enabled(model):
            raise RouterError(400, "model_disabled", "This model is disabled in the gateway.")

    def _plan_for(self, model: ModelInfo, payload: dict[str, Any]) -> ChatPlan:
        provider = self.registry.catalog.provider(model.provider_id)
        if provider is None:
            raise RouterError(404, "provider_not_found", "Provider not found.")
        self._check_provider(provider, model)
        body = dict(payload)
        body["model"] = model.upstream_id
        stream = bool(body.get("stream"))
        if stream:
            body["stream"] = True
        return ChatPlan(
            provider=provider,
            model=model,
            url=self._url(provider),
            headers=self._headers(provider, stream=stream),
            body=body,
        )

    def plans(self, payload: dict[str, Any]) -> list[ChatPlan]:
        if not isinstance(payload, dict):
            raise RouterError(400, "invalid_request", "Request body must be a JSON object.")
        model_id = str(payload.get("model") or "").strip()
        if not model_id:
            raise RouterError(400, "missing_model", "The 'model' field is required.")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise RouterError(400, "invalid_messages", "The 'messages' field must be a non-empty array.")
        retries = max(1, int(self.settings.get("max_retries") or 2) + 1)
        if self._is_auto(model_id):
            candidates = self.registry.auto_models(limit=retries)
            if not candidates:
                raise RouterError(
                    503,
                    "no_free_routes",
                    "No free routes are available yet. Refresh providers or add a provider key.",
                )
            return [self._plan_for(model, payload) for model in candidates]

        model = self.registry.resolve(model_id)
        plans: list[ChatPlan] = []
        last_error: RouterError | None = None
        if model is not None:
            # An explicitly requested model is tried first.  If it is offline,
            # disabled or rate-limited, the gateway falls back to the weighted
            # pool instead of making the client guess another model name.
            try:
                plans.append(self._plan_for(model, payload))
            except RouterError as exc:
                last_error = exc

        exclude = {model.public_id} if model is not None else set()
        blocked_providers = {model.provider_id} if model is not None else set()
        for fallback in self.registry.weighted_models(
            exclude=exclude,
            exclude_providers=blocked_providers,
            limit=retries,
        ):
            try:
                plans.append(self._plan_for(fallback, payload))
            except RouterError:
                continue
        if not plans:
            raise last_error or RouterError(
                404,
                "model_not_found",
                f"Model '{model_id}' is not in the catalog and no weighted fallback is available.",
            )
        return plans

    # --------------------------------------------------------------- nonstream
    def complete(self, payload: dict[str, Any]) -> ChatResult:
        plans = self.plans(payload)
        last_error: RouterError | None = None
        for plan in plans:
            if self.registry.is_cooling(plan.provider):
                continue
            started = time.perf_counter()
            try:
                with self._client() as client:
                    response = client.post(plan.url, headers=plan.headers, json=plan.body)
                latency = (time.perf_counter() - started) * 1000
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        raise RouterError(502, "invalid_upstream_json", "Upstream returned non-JSON content.")
                    if not isinstance(data, dict):
                        raise RouterError(502, "invalid_upstream_json", "Upstream returned an unexpected payload.")
                    usage, estimated = self._usage_from_response(payload, data)
                    self.registry.note_success(plan.provider, latency)
                    self.stats.record(
                        provider=plan.provider.id,
                        model=plan.model.public_id,
                        status=200,
                        ok=True,
                        latency_ms=latency,
                        tokens_in=usage["tokens_in"],
                        tokens_out=usage["tokens_out"],
                        cached_tokens=usage.get("cached_tokens", 0),
                        cache_reported=usage.get("cache_reported", False),
                        stream=False,
                        estimated=estimated,
                    )
                    return ChatResult(data=data, provider=plan.provider, model=plan.model, latency_ms=latency)
                text = response.text[:800]
                retry_after = _retry_after(response)
                self.registry.mark_failure(plan.provider, response.status_code, retry_after)
                self.stats.record(
                    provider=plan.provider.id,
                    model=plan.model.public_id,
                    status=response.status_code,
                    ok=False,
                    latency_ms=latency,
                    error=text[:300],
                )
                last_error = RouterError(
                    response.status_code if response.status_code >= 400 else 502,
                    "upstream_error",
                    f"{plan.provider.name} returned HTTP {response.status_code}: {text[:300]}",
                )
            except RouterError as exc:
                last_error = exc
                break
            except httpx.HTTPError as exc:
                latency = (time.perf_counter() - started) * 1000
                self.registry.mark_failure(plan.provider)
                self.stats.record(
                    provider=plan.provider.id,
                    model=plan.model.public_id,
                    status=502,
                    ok=False,
                    latency_ms=latency,
                    error=f"{type(exc).__name__}: {exc}",
                )
                last_error = RouterError(502, "upstream_unreachable", f"{plan.provider.name}: {exc}")
        raise last_error or RouterError(502, "no_route", "No route succeeded.")

    # ------------------------------------------------------------------ stream
    def stream(self, payload: dict[str, Any]) -> StreamResult:
        plans = self.plans(payload)
        last_error: RouterError | None = None
        for plan in plans:
            if self.registry.is_cooling(plan.provider):
                continue
            started = time.perf_counter()
            client = self._client()
            try:
                request = client.build_request("POST", plan.url, headers=plan.headers, json=plan.body)
                response = client.send(request, stream=True)
                if response.status_code == 200:
                    return StreamResult(response=response, client=client, provider=plan.provider, model=plan.model, started=started, payload=payload)
                text = response.read().decode("utf-8", errors="replace")[:800]
                latency = (time.perf_counter() - started) * 1000
                retry_after = _retry_after(response)
                response.close()
                client.close()
                self.registry.mark_failure(plan.provider, response.status_code, retry_after)
                self.stats.record(
                    provider=plan.provider.id,
                    model=plan.model.public_id,
                    status=response.status_code,
                    ok=False,
                    latency_ms=latency,
                    stream=True,
                    error=text[:300],
                )
                last_error = RouterError(
                    response.status_code if response.status_code >= 400 else 502,
                    "upstream_error",
                    f"{plan.provider.name} returned HTTP {response.status_code}: {text[:300]}",
                )
            except httpx.HTTPError as exc:
                client.close()
                latency = (time.perf_counter() - started) * 1000
                self.registry.mark_failure(plan.provider)
                self.stats.record(
                    provider=plan.provider.id,
                    model=plan.model.public_id,
                    status=502,
                    ok=False,
                    latency_ms=latency,
                    stream=True,
                    error=f"{type(exc).__name__}: {exc}",
                )
                last_error = RouterError(502, "upstream_unreachable", f"{plan.provider.name}: {exc}")
        raise last_error or RouterError(502, "no_route", "No route succeeded.")

    def iter_stream(self, result: StreamResult) -> Iterator[bytes]:
        response = result.response
        buffer = ""
        text_parts: list[str] = []
        usage: dict[str, int] | None = None
        first_token_at: float | None = None
        finished = False
        try:
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                yield chunk
                try:
                    buffer += chunk.decode("utf-8", errors="ignore")
                except Exception:
                    continue
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and isinstance(event.get("usage"), dict):
                        usage = {
                            "tokens_in": int(event["usage"].get("prompt_tokens") or 0),
                            "tokens_out": int(event["usage"].get("completion_tokens") or 0),
                            "cached_tokens": _cached_tokens(event["usage"]),
                            "cache_reported": _cache_reported(event["usage"]),
                        }
                    choices = event.get("choices") if isinstance(event, dict) else None
                    if isinstance(choices, list) and choices:
                        delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                        if isinstance(delta, dict):
                            content = delta.get("content")
                            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                            if first_token_at is None and (isinstance(content, str) and content or isinstance(reasoning, str) and reasoning):
                                first_token_at = time.perf_counter()
                            if isinstance(content, str):
                                text_parts.append(content)
        finally:
            latency = (time.perf_counter() - result.started) * 1000
            try:
                response.close()
            finally:
                result.client.close()
            estimated = usage is None
            if usage is None:
                out_text = "".join(text_parts)
                usage = {
                    "tokens_in": estimate_tokens(_messages_text_from_payload(result.payload)),
                    "tokens_out": estimate_tokens(out_text),
                    "cached_tokens": 0,
                    "cache_reported": False,
                }
            first_token_ms = (first_token_at - result.started) * 1000 if first_token_at else 0
            self.registry.note_success(result.provider, latency)
            self.stats.record(
                provider=result.provider.id,
                model=result.model.public_id,
                status=200,
                ok=True,
                latency_ms=latency,
                tokens_in=usage.get("tokens_in", 0),
                tokens_out=usage.get("tokens_out", 0),
                cached_tokens=usage.get("cached_tokens", 0),
                cache_reported=usage.get("cache_reported", False),
                first_token_ms=first_token_ms,
                stream=True,
                estimated=estimated,
            )

    # -------------------------------------------------------------------- test
    def test(self, *, provider_id: str | None = None, model_id: str | None = None, prompt: str = "Reply with exactly: OK") -> dict[str, Any]:
        if model_id:
            model = self.registry.resolve(model_id)
            if model is None:
                raise RouterError(404, "model_not_found", f"Model '{model_id}' is not in the catalog.")
        elif provider_id:
            provider = self.registry.catalog.provider(provider_id)
            if provider is None:
                raise RouterError(404, "provider_not_found", f"Provider '{provider_id}' not found.")
            model = next((m for m in provider.models if m.enabled and m.kind == "chat"), None)
            if model is None:
                raise RouterError(400, "no_chat_model", "This provider has no chat model in the catalog.")
        else:
            candidates = self.registry.auto_models(limit=1)
            if not candidates:
                raise RouterError(503, "no_free_routes", "No free route is available.")
            model = candidates[0]
        payload = {
            "model": model.public_id,
            "messages": [{"role": "user", "content": prompt or "Reply with exactly: OK"}],
            "max_tokens": 16,
            "stream": False,
        }
        started = time.perf_counter()
        try:
            result = self.complete(payload)
            latency = (time.perf_counter() - started) * 1000
            content = _message_text(result.data) or _reasoning_text(result.data)
            return {
                "ok": True,
                "provider": result.provider.id,
                "provider_name": result.provider.name,
                "model": result.model.public_id,
                "latency_ms": round(latency, 1),
                "reply": content[:1000],
            }
        except RouterError as exc:
            return {
                "ok": False,
                "provider": provider_id or "",
                "model": model_id or model.public_id,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": exc.message,
                "code": exc.code,
            }

    # ------------------------------------------------------------------- usage
    @staticmethod
    def _usage_from_response(payload: dict[str, Any], data: dict[str, Any]) -> tuple[dict[str, int], bool]:
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            if input_tokens or output_tokens:
                return {
                    "tokens_in": input_tokens,
                    "tokens_out": output_tokens,
                    "cached_tokens": _cached_tokens(usage),
                    "cache_reported": _cache_reported(usage),
                }, False
        text = _message_text(data) or _reasoning_text(data)
        return {
            "tokens_in": estimate_tokens(_messages_text_from_payload(payload)),
            "tokens_out": estimate_tokens(text),
            "cached_tokens": 0,
            "cache_reported": False,
        }, True


def _cache_reported(usage: dict[str, Any]) -> bool:
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    if isinstance(details, dict) and "cached_tokens" in details:
        return True
    return any(key in usage for key in ("cache_read_input_tokens", "prompt_cache_hit_tokens", "cached_tokens"))


def _cached_tokens(usage: dict[str, Any]) -> int:
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    if isinstance(details, dict):
        value = details.get("cached_tokens")
        if value is not None:
            return max(0, int(value or 0))
    for key in ("cache_read_input_tokens", "prompt_cache_hit_tokens", "cached_tokens"):
        if usage.get(key) is not None:
            return max(0, int(usage.get(key) or 0))
    return 0


def _messages_text_from_payload(payload: dict[str, Any]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    parts = []
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        parts.append(item["text"])
    return "\n".join(parts)


def _messages_text(model_id: str) -> str:
    return model_id


def _message_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    return ""


def _reasoning_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if isinstance(message, dict) and isinstance(message.get("reasoning"), str):
        return message["reasoning"]
    return ""


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("retry-after")
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        return 0.0
