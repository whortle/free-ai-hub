from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DATA_DIR

# Free-rider names that do not share a base URL with the freellmpool catalog but
# are known to be the same upstream provider.
MANUAL_MATCH: dict[str, str] = {
    "kilo code": "kilo",
    "llm7.io": "llm7",
    "ovhcloud ai endpoints": "ovh",
    "opencode zen": "opencode",
    "groq": "groq",
    "hugging face": "huggingface",
    "aion labs": "aion",
    "modelscope": "modelscope",
    "siliconflow": "siliconflow",
    "cerebras": "cerebras",
    "nvidia nim": "nvidia",
    "openrouter": "openrouter",
    "google gemini": "gemini",
    "cloudflare workers ai": "cloudflare",
    "mistral ai": "mistral",
    "cohere": "cohere",
    "sambanova": "sambanova",
    "z ai (zhipu ai)": "zhipu",
    "ollama cloud": "ollama",
}

# Providers whose free routes are not generally callable without their own
# client.  They stay visible in the catalog but never enter automatic routing.
RESTRICTED_PROVIDERS = {
    "opencode": "OpenCode Zen free routes are limited to the OpenCode client.",
}

CHAT_EXCLUDE_RE = re.compile(
    r"(embed|embedding|rerank|moderation|whisper|transcri|tts|text-to-speech|"
    r"stable-diffusion|sdxl|flux|image-gen|dall-e|audio-)",
    re.I,
)
VISION_RE = re.compile(r"(vl|vision|omni|multimodal|llava|pixtral|image)", re.I)
REASONING_RE = re.compile(r"(reason|think|\br1\b|o[134](?:-|$)|nemotron|qwq|deepseek-r)", re.I)
CODE_RE = re.compile(r"(code|coder|codestral|devstral|starcoder|swe)", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return text or "provider"


def normalize_base(url: str) -> str:
    text = (url or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.rstrip("/")
    text = re.sub(r"/v1$", "", text)
    return text


def parse_context(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().upper().replace(",", "")
    if not text:
        return 0
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?)$", text)
    if not match:
        try:
            return int(float(text))
        except ValueError:
            return 0
    number = float(match.group(1))
    unit = match.group(2)
    factor = {"": 1, "K": 1_000, "M": 1_000_000, "G": 1_000_000_000, "T": 1_000_000_000_000}.get(unit, 1)
    # "128K" here means an approximate context window; use binary-ish 1024
    # only for K/M so 128K -> 131072, closer to provider docs.
    if unit == "K":
        factor = 1024
    elif unit == "M":
        factor = 1024 * 1024
    return int(number * factor)


def infer_modalities(model_id: str, provider_modalities: Iterable[str]) -> list[str]:
    result: set[str] = {"text"}
    for item in provider_modalities:
        item = str(item).strip().lower()
        if item in {"text", "code", "reasoning", "vision", "image", "video", "audio"}:
            result.add(item)
    if VISION_RE.search(model_id):
        result.add("vision")
    if REASONING_RE.search(model_id):
        result.add("reasoning")
    if CODE_RE.search(model_id):
        result.add("code")
    return sorted(result, key=lambda x: ["text", "code", "reasoning", "vision", "image", "video", "audio"].index(x) if x in {"text", "code", "reasoning", "vision", "image", "video", "audio"} else 99)


def is_chat_model(model_id: str) -> bool:
    return not CHAT_EXCLUDE_RE.search(model_id or "")


@dataclass
class ModelInfo:
    provider_id: str
    upstream_id: str
    public_id: str
    name: str
    context: int = 0
    rate_limit: str = ""
    enabled: bool = True
    auto: bool = True
    source: str = ""
    modalities: list[str] = field(default_factory=lambda: ["text"])
    kind: str = "chat"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.public_id,
            "public_id": self.public_id,
            "upstream_id": self.upstream_id,
            "name": self.name,
            "provider_id": self.provider_id,
            "context": self.context,
            "rate_limit": self.rate_limit,
            "enabled": self.enabled,
            "auto": self.auto,
            "source": self.source,
            "modalities": self.modalities,
            "kind": self.kind,
            "notes": self.notes,
        }


@dataclass
class ProviderInfo:
    id: str
    name: str
    base_url: str = ""
    auth: str = "key"  # key | none | optional | catalog
    key_env: str = ""
    key_optional: bool = False
    adapter: str = "openai"
    tier: str = "unknown"
    key_url: str = ""
    credit_card: str = ""
    catalog_free_models: int = 0
    max_context: str = ""
    modalities: list[str] = field(default_factory=list)
    credit_model: str = ""
    models: list[ModelInfo] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    routable_candidate: bool = False
    restricted: bool = False
    notes: str = ""

    # Runtime state (not from the catalog).
    status: str = "catalog"
    latency_ms: float | None = None
    last_checked: str = ""
    error: str = ""
    live_model_ids: list[str] = field(default_factory=list)
    chat_ok: bool | None = None
    cooldown_until: float = 0.0

    @property
    def keyless(self) -> bool:
        return self.auth in {"none", "optional"} or self.key_optional

    @property
    def key_required(self) -> bool:
        return self.auth == "key" and not self.key_optional

    @property
    def model_count(self) -> int:
        return len(self.models)

    @property
    def enabled_model_count(self) -> int:
        return sum(1 for m in self.models if m.enabled and m.kind == "chat")

    @property
    def free_model_count(self) -> int:
        return self.catalog_free_models or self.enabled_model_count

    def upsert_model(self, model: ModelInfo) -> None:
        key = model.upstream_id.lower()
        for index, existing in enumerate(self.models):
            if existing.upstream_id.lower() == key:
                # Merge missing metadata without overwriting curated flags.
                if not existing.context and model.context:
                    existing.context = model.context
                if not existing.rate_limit and model.rate_limit:
                    existing.rate_limit = model.rate_limit
                if existing.source and model.source and model.source not in existing.source:
                    existing.source = existing.source + "+" + model.source
                return
        self.models.append(model)

    def to_dict(self, *, include_models: bool = False, key_configured: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "auth": self.auth,
            "keyless": self.keyless,
            "key_required": self.key_required,
            "key_optional": self.key_optional,
            "key_env": self.key_env,
            "key_configured": bool(key_configured),
            "adapter": self.adapter,
            "tier": self.tier,
            "key_url": self.key_url,
            "credit_card": self.credit_card,
            "catalog_free_models": self.catalog_free_models,
            "free_model_count": self.free_model_count,
            "max_context": self.max_context,
            "modalities": self.modalities,
            "credit_model": self.credit_model,
            "source": self.source,
            "routable": self.routable_candidate,
            "restricted": self.restricted,
            "notes": self.notes,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "last_checked": self.last_checked,
            "error": self.error,
            "live_models": len(self.live_model_ids),
            "chat_ok": self.chat_ok,
            "models_count": self.model_count,
            "enabled_models": self.enabled_model_count,
        }
        if include_models:
            data["models"] = [m.to_dict() for m in sorted(self.models, key=lambda m: m.upstream_id.lower())]
        return data


class Catalog:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = Path(data_dir)
        self.providers: dict[str, ProviderInfo] = {}
        self.models: list[ModelInfo] = []
        self.refreshed_at = ""

    # ------------------------------------------------------------------ load
    def load(self) -> "Catalog":
        freellm_doc = self._load_toml(self.data_dir / "freellmpool.providers.toml")
        free_rider_doc = self._load_json(self.data_dir / "free-rider.providers.json")

        for raw in freellm_doc.get("provider", []):
            self._add_freellm_provider(raw)
        for raw in free_rider_doc.get("providers", []):
            self._merge_free_rider_provider(raw)

        # Materialise the flat model list and stable public ids.
        for provider in self.providers.values():
            for model in provider.models:
                model.provider_id = provider.id
                model.public_id = f"{provider.id}/{model.upstream_id}"
                # Model badges describe the model itself.  Provider-level
                # modality lists (e.g. a provider that also sells image
                # generation) are shown on the provider card, not copied onto
                # every text model.
                model.modalities = infer_modalities(model.upstream_id, [])
                model.kind = "chat" if is_chat_model(model.upstream_id) else "other"
            provider.models.sort(key=lambda m: m.upstream_id.lower())

        self.models = [
            model
            for provider in sorted(self.providers.values(), key=lambda p: p.name.lower())
            for model in provider.models
        ]
        self.refreshed_at = utc_now()
        return self

    @staticmethod
    def _load_toml(path: Path) -> dict[str, Any]:
        try:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _add_freellm_provider(self, raw: dict[str, Any]) -> None:
        provider_id = str(raw.get("id") or "").strip()
        if not provider_id:
            return
        auth = str(raw.get("auth") or "key").strip().lower()
        if auth not in {"none", "key"}:
            auth = "key"
        key_optional = bool(raw.get("key_optional"))
        if key_optional:
            auth = "optional"
        base_url = str(raw.get("base_url") or "").strip()
        adapter = str(raw.get("adapter") or "openai").strip().lower()
        routable = (
            adapter == "openai"
            and bool(base_url)
            and "{" not in base_url
            and auth != "catalog"
        )
        provider = ProviderInfo(
            id=provider_id,
            name=str(raw.get("label") or provider_id),
            base_url=base_url,
            auth=auth,
            key_env=str(raw.get("key_env") or "").strip(),
            key_optional=key_optional,
            adapter=adapter,
            tier="keyless" if auth == "none" else ("optional" if key_optional else "free-tier"),
            source=["freellmpool"],
            routable_candidate=routable,
            restricted=provider_id in RESTRICTED_PROVIDERS,
            notes=RESTRICTED_PROVIDERS.get(provider_id, ""),
        )
        for item in raw.get("models", []) or []:
            upstream_id = str(item.get("name") or "").strip()
            if not upstream_id:
                continue
            provider.upsert_model(
                ModelInfo(
                    provider_id=provider.id,
                    upstream_id=upstream_id,
                    public_id="",
                    name=upstream_id,
                    context=parse_context(item.get("context")),
                    rate_limit=("auto" if item.get("auto", True) else "pin-only"),
                    enabled=bool(item.get("enabled", True)),
                    auto=bool(item.get("auto", True)),
                    source="freellmpool",
                    modalities=infer_modalities(upstream_id, provider.modalities),
                )
            )
        self.providers[provider.id] = provider

    def _find_free_rider_match(self, raw: dict[str, Any]) -> ProviderInfo | None:
        name = str(raw.get("name") or "").strip().lower()
        if name in MANUAL_MATCH and MANUAL_MATCH[name] in self.providers:
            return self.providers[MANUAL_MATCH[name]]
        base = normalize_base(str(raw.get("base_url") or ""))
        if not base:
            return None
        for provider in self.providers.values():
            if normalize_base(provider.base_url) == base:
                return provider
        return None

    def _merge_free_rider_provider(self, raw: dict[str, Any]) -> None:
        name = str(raw.get("name") or "").strip()
        if not name:
            return
        provider = self._find_free_rider_match(raw)
        if provider is None:
            provider_id = slugify(name)
            suffix = 2
            while provider_id in self.providers:
                provider_id = f"{slugify(name)}-{suffix}"
                suffix += 1
            base_url = str(raw.get("base_url") or "").strip()
            adapter = "openai" if base_url and "{" not in base_url else "catalog"
            provider = ProviderInfo(
                id=provider_id,
                name=name,
                base_url=base_url,
                auth="key" if base_url else "catalog",
                key_env="FREEAIHUB_" + provider_id.upper().replace("-", "_") + "_API_KEY",
                adapter=adapter,
                tier=str(raw.get("tier") or "unknown"),
                key_url=str(raw.get("key_url") or ""),
                credit_card=str(raw.get("credit_card") or ""),
                catalog_free_models=int(raw.get("free_models") or 0),
                max_context=str(raw.get("max_context") or ""),
                modalities=[str(x) for x in (raw.get("modalities") or [])],
                credit_model=str(raw.get("credit_model") or ""),
                source=["Free-rider"],
                routable_candidate=adapter == "openai",
                notes="" if base_url else "No public base URL in the directory; catalog only.",
            )
            self.providers[provider.id] = provider
        else:
            provider.source = list(dict.fromkeys([*provider.source, "Free-rider"]))
            provider.tier = str(raw.get("tier") or provider.tier or "unknown")
            provider.key_url = provider.key_url or str(raw.get("key_url") or "")
            provider.credit_card = provider.credit_card or str(raw.get("credit_card") or "")
            provider.catalog_free_models = provider.catalog_free_models or int(raw.get("free_models") or 0)
            provider.max_context = provider.max_context or str(raw.get("max_context") or "")
            if raw.get("modalities"):
                provider.modalities = sorted({*provider.modalities, *[str(x) for x in raw.get("modalities") or []]})
            provider.credit_model = provider.credit_model or str(raw.get("credit_model") or "")
            provider.notes = provider.notes or (RESTRICTED_PROVIDERS.get(provider.id, ""))

        for item in raw.get("best_models", []) or []:
            upstream_id = str(item.get("model_id") or "").strip()
            if not upstream_id:
                continue
            provider.upsert_model(
                ModelInfo(
                    provider_id=provider.id,
                    upstream_id=upstream_id,
                    public_id="",
                    name=upstream_id,
                    context=parse_context(item.get("ctx")),
                    rate_limit=str(item.get("rate_limit") or ""),
                    enabled=True,
                    auto=True,
                    source="Free-rider",
                    modalities=infer_modalities(upstream_id, provider.modalities),
                )
            )

    # --------------------------------------------------------------- queries
    def provider(self, provider_id: str) -> ProviderInfo | None:
        return self.providers.get(provider_id)

    def model(self, public_id: str) -> ModelInfo | None:
        for candidate in self.models:
            if candidate.public_id == public_id:
                return candidate
        return None

    def model_by_upstream(self, upstream_id: str) -> list[ModelInfo]:
        needle = upstream_id.lower()
        return [model for model in self.models if model.upstream_id.lower() == needle]

    def stats(self) -> dict[str, int]:
        providers = list(self.providers.values())
        models = self.models
        return {
            "providers_total": len(providers),
            "providers_keyless": sum(1 for p in providers if p.keyless),
            "providers_keyed": sum(1 for p in providers if p.key_required),
            "providers_catalog": sum(1 for p in providers if p.auth == "catalog" or not p.routable_candidate),
            "models_catalog": sum(p.free_model_count for p in providers),
            "models_known": len(models),
            "models_enabled": sum(1 for m in models if m.enabled and m.kind == "chat"),
            "models_keyless": sum(
                1 for m in models if m.kind == "chat" and self.providers[m.provider_id].keyless
            ),
        }

    def all_models(self) -> list[ModelInfo]:
        return list(self.models)
