from __future__ import annotations

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .catalog import Catalog, ModelInfo, ProviderInfo, utc_now
from .config import STATE_FILE, Settings, atomic_write_json
from .store import KeyStore, RefreshState, Stats

AUTO_ALIASES = {"auto", "free", "free-router", "best-free", "free-auto"}


class Registry:
    def __init__(
        self,
        catalog: Catalog,
        keys: KeyStore,
        settings: Settings,
        stats: Stats,
        refresh_state: RefreshState,
        state_path: Path = STATE_FILE,
        model_settings_path: Path | None = None,
    ) -> None:
        self.catalog = catalog
        self.keys = keys
        self.settings = settings
        self.stats = stats
        self.refresh_state = refresh_state
        self.state_path = Path(state_path)
        self.model_settings_path = Path(model_settings_path) if model_settings_path else self.state_path.parent / "model_settings.json"
        self.model_settings: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._last_refresh = ""
        self._load_model_settings()
        self._apply_state(self._load_state())
        self._init_statuses()

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _apply_state(self, state: dict[str, Any]) -> None:
        providers = state.get("providers") if isinstance(state, dict) else None
        if not isinstance(providers, dict):
            return
        self._last_refresh = str(state.get("refreshed_at") or "")
        for provider_id, item in providers.items():
            provider = self.catalog.provider(str(provider_id))
            if provider is None or not isinstance(item, dict):
                continue
            provider.status = str(item.get("status") or provider.status)
            provider.latency_ms = item.get("latency_ms")
            provider.last_checked = str(item.get("last_checked") or "")
            provider.error = str(item.get("error") or "")
            provider.chat_ok = item.get("chat_ok")
            live = item.get("live_models")
            if isinstance(live, list):
                provider.live_model_ids = [str(x) for x in live[:2000]]

    # --------------------------------------------------------- model policy
    def _load_model_settings(self) -> None:
        try:
            raw = json.loads(self.model_settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        self.model_settings = raw if isinstance(raw, dict) else {}

    def save_model_settings(self) -> None:
        try:
            atomic_write_json(self.model_settings_path, self.model_settings, mode=0o600)
        except OSError:
            pass

    def model_policy(self, model: ModelInfo) -> dict[str, Any]:
        raw = self.model_settings.get(model.public_id)
        if not isinstance(raw, dict):
            raw = {}
        try:
            weight = float(raw.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        weight = max(0.0, min(100.0, weight))
        enabled = bool(raw.get("enabled", True)) and bool(model.enabled)
        return {"enabled": enabled, "weight": weight}

    def policy_enabled(self, model: ModelInfo) -> bool:
        return bool(self.model_policy(model)["enabled"])

    def model_weight(self, model: ModelInfo) -> float:
        policy = self.model_policy(model)
        return float(policy["weight"]) if policy["enabled"] else 0.0

    def set_model_policy(self, public_id: str, *, enabled: bool | None = None, weight: float | None = None) -> dict[str, Any]:
        model = self.catalog.model(public_id)
        if model is None:
            raise KeyError(public_id)
        current = self.model_policy(model)
        next_enabled = current["enabled"] if enabled is None else bool(enabled)
        try:
            next_weight = current["weight"] if weight is None else float(weight)
        except (TypeError, ValueError):
            raise ValueError("weight must be a number") from None
        next_weight = max(0.0, min(100.0, next_weight))
        self.model_settings[public_id] = {"enabled": next_enabled, "weight": next_weight}
        self.save_model_settings()
        return {"id": public_id, "enabled": next_enabled and bool(model.enabled), "weight": next_weight}

    def _weighted_sample(self, models: list[ModelInfo], limit: int) -> list[ModelInfo]:
        pool = list(models)
        result: list[ModelInfo] = []
        while pool and len(result) < max(1, limit):
            weights = [max(0.0001, self.model_weight(model)) for model in pool]
            total = sum(weights)
            pick = random.random() * total
            upto = 0.0
            chosen = len(pool) - 1
            for index, weight in enumerate(weights):
                upto += weight
                if pick <= upto:
                    chosen = index
                    break
            result.append(pool.pop(chosen))
        return result

    def weighted_models(
        self,
        *,
        exclude: set[str] | None = None,
        exclude_providers: set[str] | None = None,
        limit: int = 4,
    ) -> list[ModelInfo]:
        excluded = exclude or set()
        blocked_providers = exclude_providers or set()
        groups: dict[str, list[ModelInfo]] = {}
        for model in self.public_models():
            if model.public_id in excluded or not model.auto or self.model_weight(model) <= 0:
                continue
            provider = self.catalog.provider(model.provider_id)
            if provider is None or provider.id in blocked_providers or self.is_cooling(provider):
                continue
            groups.setdefault(provider.id, []).append(model)

        result: list[ModelInfo] = []
        while groups and len(result) < max(1, limit):
            provider_ids = list(groups)
            weights = [sum(max(0.0001, self.model_weight(model)) for model in groups[provider_id]) for provider_id in provider_ids]
            total = sum(weights)
            pick = random.random() * total
            upto = 0.0
            chosen_provider = provider_ids[-1]
            for provider_id, weight in zip(provider_ids, weights):
                upto += weight
                if pick <= upto:
                    chosen_provider = provider_id
                    break
            models = groups.pop(chosen_provider)
            result.extend(self._weighted_sample(models, 1))
        return result

    def _init_statuses(self) -> None:
        for provider in self.catalog.providers.values():
            if provider.adapter != "openai" or not provider.routable_candidate:
                if provider.status in {"catalog", ""}:
                    provider.status = "catalog"
                continue
            if provider.restricted:
                provider.status = "restricted"
                continue
            if provider.status and provider.status != "catalog":
                continue
            if provider.key_required and not self.keys.configured(provider):
                provider.status = "needs_key"
            else:
                provider.status = "checking"

    def save_state(self) -> None:
        payload = {
            "refreshed_at": self._last_refresh or utc_now(),
            "providers": {
                provider.id: {
                    "status": provider.status,
                    "latency_ms": provider.latency_ms,
                    "last_checked": provider.last_checked,
                    "error": provider.error,
                    "live_models": provider.live_model_ids[:500],
                    "chat_ok": provider.chat_ok,
                }
                for provider in self.catalog.providers.values()
            },
        }
        try:
            atomic_write_json(self.state_path, payload, mode=0o600)
        except OSError:
            pass

    # --------------------------------------------------------------- probing
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

    @staticmethod
    def _models_url(provider: ProviderInfo) -> str:
        return provider.base_url.rstrip("/") + "/models"

    def _headers(self, provider: ProviderInfo, key: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "FreeAIHub/0.1 (+local free-tier gateway)",
        }
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    @staticmethod
    def _parse_model_ids(payload: Any) -> list[str]:
        if isinstance(payload, dict):
            data = payload.get("data")
            if data is None:
                data = payload.get("models")
        else:
            data = payload
        if not isinstance(data, list):
            return []
        result: list[str] = []
        for item in data:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                value = item.get("id") or item.get("name") or item.get("model")
                if isinstance(value, str) and value.strip():
                    result.append(value.strip())
        return list(dict.fromkeys(result))

    def _probe_models(self, provider: ProviderInfo, key: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            with self._client() as client:
                response = client.get(self._models_url(provider), headers=self._headers(provider, key))
            latency = (time.perf_counter() - started) * 1000
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                live = self._parse_model_ids(payload)
                return {
                    "status": "online",
                    "latency_ms": round(latency, 1),
                    "live_models": live,
                    "error": "",
                }
            if response.status_code in {401, 403}:
                return {
                    "status": "invalid_key" if key else "needs_key",
                    "latency_ms": round(latency, 1),
                    "live_models": [],
                    "error": f"HTTP {response.status_code}",
                }
            if response.status_code == 404:
                return {
                    "status": "unknown",
                    "latency_ms": round(latency, 1),
                    "live_models": [],
                    "error": "models endpoint not available",
                }
            if response.status_code == 429:
                return {
                    "status": "degraded",
                    "latency_ms": round(latency, 1),
                    "live_models": [],
                    "error": "HTTP 429 rate limited",
                }
            return {
                "status": "degraded",
                "latency_ms": round(latency, 1),
                "live_models": [],
                "error": f"HTTP {response.status_code}",
            }
        except Exception as exc:  # noqa: BLE001 - network boundary
            latency = (time.perf_counter() - started) * 1000
            return {
                "status": "offline",
                "latency_ms": round(latency, 1),
                "live_models": [],
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }

    def _probe_chat(self, provider: ProviderInfo, key: str) -> dict[str, Any]:
        model = next(
            (m for m in provider.models if m.enabled and m.auto and m.kind == "chat"),
            next((m for m in provider.models if m.enabled and m.kind == "chat"), None),
        )
        if model is None:
            return {"ok": False, "status": "catalog", "error": "no chat model in catalog"}
        body = {
            "model": model.upstream_id,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": 8,
            "stream": False,
        }
        started = time.perf_counter()
        try:
            with self._client() as client:
                response = client.post(
                    provider.base_url.rstrip("/") + "/chat/completions",
                    headers={**self._headers(provider, key), "Content-Type": "application/json"},
                    json=body,
                )
            latency = (time.perf_counter() - started) * 1000
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                choices = payload.get("choices") if isinstance(payload, dict) else None
                content = ""
                if isinstance(choices, list) and choices:
                    message = choices[0].get("message") if isinstance(choices[0], dict) else None
                    if isinstance(message, dict):
                        content = str(message.get("content") or message.get("reasoning") or "")[:120]
                return {
                    "ok": True,
                    "status": "online",
                    "latency_ms": round(latency, 1),
                    "model": model.public_id,
                    "reply": content,
                    "error": "",
                }
            return {
                "ok": False,
                "status": "degraded",
                "latency_ms": round(latency, 1),
                "model": model.public_id,
                "error": f"HTTP {response.status_code}",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "status": "offline",
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "model": model.public_id,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }

    def _probe_one(self, provider: ProviderInfo, *, probe_chat: bool) -> dict[str, Any]:
        if provider.restricted:
            return {"provider": provider.id, "status": "restricted", "error": provider.notes}
        if provider.adapter != "openai" or not provider.routable_candidate:
            return {"provider": provider.id, "status": "catalog", "error": ""}
        key = self.keys.get(provider)
        if provider.key_required and not key:
            return {"provider": provider.id, "status": "needs_key", "error": "API key not configured"}
        result = self._probe_models(provider, key)
        if probe_chat and result["status"] in {"online", "unknown"} and provider.keyless:
            chat = self._probe_chat(provider, key)
            result["chat_ok"] = bool(chat.get("ok"))
            if not chat.get("ok") and result["status"] == "online":
                result["status"] = "degraded"
            result["chat_error"] = chat.get("error", "")
        return {"provider": provider.id, **result}

    def _apply_probe(self, provider: ProviderInfo, result: dict[str, Any]) -> None:
        with self._lock:
            provider.status = str(result.get("status") or provider.status)
            provider.latency_ms = result.get("latency_ms")
            provider.last_checked = utc_now()
            provider.error = str(result.get("error") or "")[:300]
            live = result.get("live_models")
            if isinstance(live, list):
                provider.live_model_ids = [str(x) for x in live[:2000]]
            if "chat_ok" in result:
                provider.chat_ok = bool(result.get("chat_ok"))

    def refresh_all(self, *, probe_chat: bool = False, reason: str = "auto") -> dict[str, Any]:
        targets = [
            provider
            for provider in self.catalog.providers.values()
            if provider.routable_candidate and not provider.restricted
        ]
        if not self.refresh_state.begin(len(targets), reason):
            return self.refresh_state.snapshot()
        try:
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = {
                    executor.submit(self._probe_one, provider, probe_chat=probe_chat): provider
                    for provider in targets
                }
                done = 0
                for future in as_completed(futures):
                    provider = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        result = {
                            "provider": provider.id,
                            "status": "offline",
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        }
                    self._apply_probe(provider, result)
                    done += 1
                    self.refresh_state.update(done, result.get("error") if result.get("status") == "offline" else None)
            self._last_refresh = utc_now()
            self.save_state()
        finally:
            self.refresh_state.finish()
        return self.refresh_state.snapshot()

    def refresh_one(self, provider_id: str, *, probe_chat: bool = True) -> dict[str, Any]:
        provider = self.catalog.provider(provider_id)
        if provider is None:
            raise KeyError(provider_id)
        result = self._probe_one(provider, probe_chat=probe_chat)
        self._apply_probe(provider, result)
        self._last_refresh = utc_now()
        self.save_state()
        return {"provider": provider.id, **result}

    # --------------------------------------------------------------- queries
    def key_configured(self, provider: ProviderInfo) -> bool:
        return self.keys.configured(provider)

    def is_cooling(self, provider: ProviderInfo) -> bool:
        return provider.cooldown_until > time.time()

    def mark_failure(self, provider: ProviderInfo, status: int | None = None, retry_after: float = 0.0) -> None:
        with self._lock:
            if retry_after > 0:
                cooldown = min(900.0, max(10.0, retry_after))
            elif status == 429:
                cooldown = 60.0
            elif status and status >= 500:
                cooldown = 30.0
            else:
                cooldown = 20.0
            provider.cooldown_until = time.time() + cooldown

    def note_success(self, provider: ProviderInfo, latency_ms: float | None = None) -> None:
        with self._lock:
            provider.cooldown_until = 0.0
            if latency_ms is not None:
                # Exponential-ish moving average to avoid a single slow call
                # permanently demoting a provider.
                provider.latency_ms = (
                    float(latency_ms)
                    if provider.latency_ms is None
                    else round(float(provider.latency_ms) * 0.7 + float(latency_ms) * 0.3, 1)
                )

    def public_models(self) -> list[ModelInfo]:
        """Models a standard OpenAI client may actually call right now."""
        result: list[ModelInfo] = []
        for model in self.catalog.models:
            provider = self.catalog.provider(model.provider_id)
            if provider is None or not provider.routable_candidate or provider.restricted:
                continue
            if model.kind != "chat" or not model.enabled or not self.policy_enabled(model):
                continue
            if provider.key_required and not self.key_configured(provider):
                continue
            if provider.keyless and not self.settings.get("enable_keyless", True):
                continue
            if not provider.keyless and not self.settings.get("enable_keyed", True):
                continue
            if provider.status in {"offline", "needs_key", "invalid_key", "catalog", "restricted"}:
                continue
            result.append(model)
        return result

    def available_models(self) -> list[ModelInfo]:
        return self.public_models()

    def all_model_dicts(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for model in self.catalog.models:
            provider = self.catalog.provider(model.provider_id)
            if provider is None:
                continue
            key_ok = self.key_configured(provider)
            policy = self.model_policy(model)
            if provider.restricted:
                status = "restricted"
            elif not provider.routable_candidate:
                status = "catalog"
            elif provider.key_required and not key_ok:
                status = "needs_key"
            elif provider.status in {"offline", "invalid_key", "needs_key", "catalog", "restricted"}:
                status = provider.status
            elif not model.enabled or not policy["enabled"]:
                status = "disabled"
            elif provider.status in {"online", "checking", "unknown", "degraded"}:
                status = "available"
            else:
                status = provider.status or "catalog"
            data = model.to_dict()
            data.update(
                {
                    "provider_name": provider.name,
                    "provider_status": provider.status,
                    "provider_tier": provider.tier,
                    "keyless": provider.keyless,
                    "key_required": provider.key_required,
                    "key_configured": key_ok,
                    "status": status,
                    "available": status == "available",
                    "routable": provider.routable_candidate and not provider.restricted,
                    "provider_source": provider.source,
                    "provider_notes": provider.notes,
                    "policy_enabled": policy["enabled"],
                    "weight": policy["weight"],
                }
            )
            result.append(data)
        return result

    def provider_dicts(self) -> list[dict[str, Any]]:
        providers = sorted(
            self.catalog.providers.values(),
            key=lambda p: (
                0 if p.status in {"online", "degraded", "checking"} else 1,
                0 if p.keyless else 1,
                p.name.lower(),
            ),
        )
        return [provider.to_dict(key_configured=self.key_configured(provider)) for provider in providers]

    def resolve(self, model_id: str) -> ModelInfo | None:
        text = str(model_id or "").strip()
        if not text:
            return None
        for model in self.catalog.models:
            if model.public_id == text:
                return model
        lower = text.lower()
        for model in self.catalog.models:
            if model.public_id.lower() == lower:
                return model
        # A bare upstream id is accepted only when unique.
        matches = [m for m in self.catalog.models if m.upstream_id.lower() == lower]
        if len(matches) == 1:
            return matches[0]
        # Accept a provider-qualified id whose model casing differs.
        if "/" in text:
            provider_id, upstream = text.split("/", 1)
            provider = self.catalog.provider(provider_id)
            if provider is not None:
                for model in provider.models:
                    if model.upstream_id.lower() == upstream.lower():
                        return model
        return None

    def _auto_candidates(self) -> list[ModelInfo]:
        candidates = []
        for model in self.public_models():
            provider = self.catalog.provider(model.provider_id)
            if provider is None or self.is_cooling(provider):
                continue
            if not model.auto:
                continue
            candidates.append(model)
        strategy = str(self.settings.get("routing_strategy") or "balanced")
        recent = self.stats.recent(200)
        usage = {}
        for item in recent:
            usage[item.get("model")] = usage.get(item.get("model"), 0) + 1

        def score(model: ModelInfo) -> tuple[float, float, float, float]:
            provider = self.catalog.provider(model.provider_id)
            assert provider is not None
            keyless_bonus = 0.0 if provider.keyless else 35.0
            latency = float(provider.latency_ms or 1200.0)
            used = float(usage.get(model.public_id, 0))
            jitter = random.random() * 12.0
            if strategy == "fastest":
                return (latency, used, keyless_bonus, jitter)
            if strategy == "keyless":
                return (keyless_bonus, latency, used, jitter)
            # balanced: keyless and latency both matter, usage breaks ties.
            return (keyless_bonus * 0.6 + latency * 0.25 + used * 2.0, latency, used, jitter)

        candidates.sort(key=score)
        return candidates

    def auto_models(self, limit: int = 4) -> list[ModelInfo]:
        # Weighted, without replacement.  Weight 0 excludes a model from
        # automatic routing but an explicit request may still pin it.
        return self.weighted_models(limit=max(1, limit))

    def stats_snapshot(self) -> dict[str, Any]:
        catalog_stats = self.catalog.stats()
        models = self.public_models()
        providers = list(self.catalog.providers.values())
        return {
            **catalog_stats,
            "providers_online": sum(1 for p in providers if p.status == "online"),
            "providers_degraded": sum(1 for p in providers if p.status == "degraded"),
            "providers_offline": sum(1 for p in providers if p.status == "offline"),
            "providers_needs_key": sum(1 for p in providers if p.status == "needs_key"),
            "providers_restricted": sum(1 for p in providers if p.status == "restricted"),
            "models_available": len(models),
            "models_keyless_available": sum(1 for m in models if self.catalog.provider(m.provider_id).keyless),
            "refreshed_at": self._last_refresh,
            "live_model_ids": sum(len(p.live_model_ids) for p in providers),
        }
