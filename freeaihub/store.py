from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .catalog import ProviderInfo, utc_now
from .config import KEYS_FILE, USAGE_FILE, atomic_write_json, mask_secret


class KeyStore:
    """Provider keys from this project's key file and explicitly named env vars.

    Host client configuration is handled separately by ``freeaihub.clients``;
    this class never reads other applications' credentials.
    """

    def __init__(self, path: Path = KEYS_FILE) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._keys: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._keys = {
                    str(k): str(v).strip()
                    for k, v in raw.items()
                    if isinstance(v, (str, int, float)) and str(v).strip()
                }
        except (OSError, ValueError):
            self._keys = {}

    def _env_key(self, provider: ProviderInfo) -> str:
        for name in (
            provider.key_env,
            f"FREEAIHUB_{provider.id.upper().replace('-', '_')}_API_KEY",
        ):
            if not name:
                continue
            value = str(os.environ.get(name) or "").strip()
            if value and value.lower() not in {"none", "no-key", "null", "-"}:
                return value
        return ""

    def get(self, provider: ProviderInfo) -> str:
        with self._lock:
            value = self._keys.get(provider.id, "").strip()
        if value and value.lower() not in {"none", "no-key", "null", "-"}:
            return value
        return self._env_key(provider)

    def configured(self, provider: ProviderInfo) -> bool:
        return bool(self.get(provider))

    def source(self, provider: ProviderInfo) -> str:
        with self._lock:
            if self._keys.get(provider.id):
                return "file"
        return "env" if self._env_key(provider) else ""

    def set(self, provider_id: str, value: str) -> None:
        value = str(value or "").strip()
        with self._lock:
            if value:
                self._keys[provider_id] = value
            else:
                self._keys.pop(provider_id, None)
            atomic_write_json(self.path, self._keys, mode=0o600)

    def delete(self, provider_id: str) -> None:
        self.set(provider_id, "")

    def masked(self, provider: ProviderInfo) -> str:
        value = self.get(provider)
        return mask_secret(value) if value else ""

    def hints(self, provider: ProviderInfo) -> list[str]:
        hints = []
        if provider.key_env:
            hints.append(provider.key_env)
        hints.append(f"FREEAIHUB_{provider.id.upper().replace('-', '_')}_API_KEY")
        return list(dict.fromkeys(hints))


class Stats:
    def __init__(self, path: Path = USAGE_FILE, limit: int = 500) -> None:
        self.path = Path(path)
        self.limit = limit
        self._lock = threading.Lock()
        self.logs: deque[dict[str, Any]] = deque(maxlen=limit)
        self.total_requests = 0
        self.successes = 0
        self.failures = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cached_tokens = 0
        self.cache_reported = 0
        self.generation_ms = 0.0
        self.latency_sum = 0.0
        self.latency_count = 0
        self.provider_counts: Counter[str] = Counter()
        self.model_counts: Counter[str] = Counter()
        self.minute_buckets: dict[str, list[float]] = {}
        self.started_at = time.time()
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-self.limit :]
        except OSError:
            return
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                self.logs.append(item)
                self.total_requests += 1
                if item.get("ok"):
                    self.successes += 1
                else:
                    self.failures += 1
                tokens_in = int(item.get("tokens_in") or 0)
                tokens_out = int(item.get("tokens_out") or 0)
                cached = int(item.get("cached_tokens") or 0)
                latency = float(item.get("latency_ms") or 0)
                first_token = float(item.get("first_token_ms") or 0)
                self.tokens_in += tokens_in
                self.tokens_out += tokens_out
                self.cached_tokens += cached
                if item.get("cache_reported"):
                    self.cache_reported += 1
                if latency:
                    self.latency_sum += latency
                    self.latency_count += 1
                    if item.get("ok"):
                        self.generation_ms += max(0.0, latency - first_token)
                self.provider_counts[str(item.get("provider") or "")] += 1
                self.model_counts[str(item.get("model") or "")] += 1
                self._bucket_add(item, tokens_in, tokens_out, cached, latency, first_token)

    def _append_file(self, item: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > 5 * 1024 * 1024:
                backup = self.path.with_suffix(".1.jsonl")
                try:
                    if backup.exists():
                        backup.unlink()
                    self.path.replace(backup)
                except OSError:
                    pass
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _bucket_add(
        self,
        item: dict[str, Any],
        tokens_in: int,
        tokens_out: int,
        cached_tokens: int,
        latency_ms: float,
        first_token_ms: float,
    ) -> None:
        try:
            stamp = datetime.fromisoformat(str(item.get("ts") or "").replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            stamp = stamp.astimezone(timezone.utc)
        except ValueError:
            return
        bucket = stamp.replace(minute=(stamp.minute // 5) * 5, second=0, microsecond=0)
        key = bucket.isoformat(timespec="minutes")
        entry = self.minute_buckets.setdefault(key, [0.0] * 7)
        entry[0] += 1
        if not item.get("ok"):
            entry[1] += 1
        entry[2] += float(latency_ms or 0)
        entry[3] += int(tokens_in or 0)
        entry[4] += int(tokens_out or 0)
        entry[5] += int(cached_tokens or 0)
        entry[6] += max(0.0, float(latency_ms or 0) - float(first_token_ms or 0))
        cutoff = bucket - timedelta(hours=2)
        for existing in list(self.minute_buckets):
            try:
                if datetime.fromisoformat(existing) < cutoff:
                    del self.minute_buckets[existing]
            except ValueError:
                del self.minute_buckets[existing]

    def record(
        self,
        *,
        provider: str,
        model: str,
        status: int,
        ok: bool,
        latency_ms: float,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cached_tokens: int = 0,
        cache_reported: bool = False,
        first_token_ms: float = 0,
        stream: bool = False,
        error: str = "",
        estimated: bool = False,
    ) -> None:
        item = {
            "ts": utc_now(),
            "provider": provider,
            "model": model,
            "status": int(status),
            "ok": bool(ok),
            "latency_ms": round(float(latency_ms), 1),
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "cached_tokens": int(cached_tokens or 0),
            "cache_reported": bool(cache_reported),
            "first_token_ms": round(float(first_token_ms or 0), 1),
            "stream": bool(stream),
            "error": str(error or "")[:300],
            "estimated": bool(estimated),
        }
        with self._lock:
            self.logs.append(item)
            self.total_requests += 1
            if ok:
                self.successes += 1
            else:
                self.failures += 1
            self.tokens_in += item["tokens_in"]
            self.tokens_out += item["tokens_out"]
            self.cached_tokens += item["cached_tokens"]
            if cache_reported:
                self.cache_reported += 1
            if latency_ms:
                self.latency_sum += float(latency_ms)
                self.latency_count += 1
                if ok:
                    self.generation_ms += max(0.0, float(latency_ms) - float(first_token_ms or 0))
            self.provider_counts[provider] += 1
            self.model_counts[model] += 1
            self._bucket_add(item, item["tokens_in"], item["tokens_out"], item["cached_tokens"], float(latency_ms or 0), float(first_token_ms or 0))
            self._append_file(item)

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self.logs)
        return list(reversed(items[-max(1, min(limit, 500)) :]))

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
        return ordered[index]

    def overview(self) -> dict[str, Any]:
        with self._lock:
            total = self.total_requests
            success_rate = (self.successes / total) if total else None
            avg_latency = (self.latency_sum / self.latency_count) if self.latency_count else None
            latencies = [float(item.get("latency_ms") or 0) for item in self.logs if item.get("latency_ms")]
            p95 = self._percentile(latencies, 0.95)
            series = self._series_locked()
            model_details = self._model_details_locked()
        return {
            "requests": total,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": success_rate,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_total": self.tokens_in + self.tokens_out,
            "cached_tokens": self.cached_tokens,
            "cache_reported": self.cache_reported,
            "cache_hit_rate": (self.cached_tokens / self.tokens_in) if self.tokens_in and self.cache_reported else None,
            "avg_speed": (self.tokens_out / (self.generation_ms / 1000)) if self.generation_ms > 0 else None,
            "avg_latency_ms": round(avg_latency, 1) if avg_latency is not None else None,
            "p95_latency_ms": round(p95, 1) if p95 is not None else None,
            "providers": dict(self.provider_counts),
            "models": dict(self.model_counts.most_common(20)),
            "models_detail": model_details,
            "series": series,
            "started_at": datetime.fromtimestamp(self.started_at, timezone.utc).isoformat(timespec="seconds"),
        }

    def _model_details_locked(self) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for item in self.logs:
            model = str(item.get("model") or "unknown")
            group = groups.setdefault(
                model,
                {
                    "model": model,
                    "provider": str(item.get("provider") or ""),
                    "requests": 0,
                    "successes": 0,
                    "failures": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "cached_tokens": 0,
                    "cache_reported": 0,
                    "generation_ms": 0.0,
                    "latencies": [],
                },
            )
            group["requests"] += 1
            if item.get("ok"):
                group["successes"] += 1
            else:
                group["failures"] += 1
            group["tokens_in"] += int(item.get("tokens_in") or 0)
            group["tokens_out"] += int(item.get("tokens_out") or 0)
            group["cached_tokens"] += int(item.get("cached_tokens") or 0)
            if item.get("cache_reported"):
                group["cache_reported"] += 1
            latency = float(item.get("latency_ms") or 0)
            if latency:
                group["latencies"].append(latency)
                if item.get("ok"):
                    group["generation_ms"] += max(0.0, latency - float(item.get("first_token_ms") or 0))
        result = []
        for group in groups.values():
            requests = group["requests"]
            result.append(
                {
                    "model": group["model"],
                    "provider": group["provider"],
                    "requests": requests,
                    "successes": group["successes"],
                    "failures": group["failures"],
                    "success_rate": (group["successes"] / requests) if requests else None,
                    "tokens_in": group["tokens_in"],
                    "tokens_out": group["tokens_out"],
                    "tokens_total": group["tokens_in"] + group["tokens_out"],
                    "cached_tokens": group["cached_tokens"],
                    "cache_reported": group["cache_reported"],
                    "cache_hit_rate": (
                        group["cached_tokens"] / group["tokens_in"]
                        if group["tokens_in"] and group["cache_reported"]
                        else None
                    ),
                    "avg_speed": (group["tokens_out"] / (group["generation_ms"] / 1000)) if group["generation_ms"] > 0 else None,
                    "avg_latency_ms": round(sum(group["latencies"]) / len(group["latencies"]), 1) if group["latencies"] else None,
                    "p95_latency_ms": (
                        round(self._percentile(group["latencies"], 0.95), 1)
                        if group["latencies"]
                        else None
                    ),
                }
            )
        return sorted(result, key=lambda row: (-row["requests"], row["model"]))[:30]

    def _series_locked(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        now = now.replace(minute=(now.minute // 5) * 5)
        result = []
        for offset in range(23, -1, -1):
            bucket = now - timedelta(minutes=5 * offset)
            key = bucket.isoformat(timespec="minutes")
            values = list(self.minute_buckets.get(key, [0.0] * 7))
            values += [0.0] * (7 - len(values))
            requests, failures, latency, tokens_in, tokens_out, cached, generation_ms = values[:7]
            result.append(
                {
                    "t": bucket.isoformat(timespec="minutes"),
                    "requests": int(requests),
                    "failures": int(failures),
                    "avg_latency_ms": round(latency / requests, 1) if requests else None,
                    "tokens_in": int(tokens_in),
                    "tokens_out": int(tokens_out),
                    "tokens_total": int(tokens_in + tokens_out),
                    "cached_tokens": int(cached),
                    "avg_speed": (tokens_out / (generation_ms / 1000)) if generation_ms > 0 else None,
                }
            )
        return result


class RefreshState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.total = 0
        self.done = 0
        self.errors: list[str] = []
        self.reason = ""

    def begin(self, total: int, reason: str = "auto") -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.started_at = utc_now()
            self.finished_at = None
            self.total = total
            self.done = 0
            self.errors = []
            self.reason = reason
            return True

    def update(self, done: int, error: str | None = None) -> None:
        with self._lock:
            self.done = done
            if error:
                self.errors.append(error[:300])
                self.errors = self.errors[-20:]

    def finish(self) -> None:
        with self._lock:
            self.running = False
            self.finished_at = utc_now()
            self.done = self.total

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "total": self.total,
                "done": self.done,
                "errors": list(self.errors),
                "reason": self.reason,
            }


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    # Rough mixed Chinese/English estimate.  Only used when the upstream omits
    # usage; the UI marks it as estimated.
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return max(1, int(cjk * 0.7 + (len(text) - cjk) / 4))
