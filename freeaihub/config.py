from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
DATA_DIR = Path(os.environ.get("FREEAIHUB_DATA_DIR", ROOT / "data")).resolve()

SETTINGS_FILE = DATA_DIR / "settings.json"
KEYS_FILE = DATA_DIR / "keys.json"
STATE_FILE = DATA_DIR / "state.json"
USAGE_FILE = DATA_DIR / "usage.jsonl"
CATALOG_CACHE_FILE = DATA_DIR / "catalog.cache.json"

DEFAULT_PORT = 8899
DEFAULT_DASHBOARD_PASSWORD = "admin"


DEFAULT_SETTINGS: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": DEFAULT_PORT,
    "refresh_interval_sec": 600,
    "request_timeout_sec": 90,
    "connect_timeout_sec": 15,
    "upstream_proxy": "",
    "trust_env_proxy": False,
    "api_key_required": True,
    "api_key": "",
    "dashboard_password_hash": "",
    "routing_strategy": "balanced",
    "max_retries": 2,
    "enable_keyless": True,
    "enable_keyed": True,
    "log_limit": 500,
    "dashboard_title": "Free AI Hub",
}

PUBLIC_FIELDS = (
    "host",
    "port",
    "refresh_interval_sec",
    "request_timeout_sec",
    "connect_timeout_sec",
    "upstream_proxy",
    "trust_env_proxy",
    "api_key_required",
    "routing_strategy",
    "max_retries",
    "enable_keyless",
    "enable_keyed",
    "dashboard_title",
)

EDITABLE_FIELDS = (
    "refresh_interval_sec",
    "request_timeout_sec",
    "connect_timeout_sec",
    "upstream_proxy",
    "trust_env_proxy",
    "api_key_required",
    "api_key",
    "dashboard_password",
    "routing_strategy",
    "max_retries",
    "enable_keyless",
    "enable_keyed",
)


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Any, *, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", mode=mode)


class Settings:
    """Thread-safe settings owned exclusively by this project.

    Provider credentials come only from this project's ``keys.json`` or from
    explicitly named environment variables.
    """

    def __init__(self, path: Path = SETTINGS_FILE) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key in DEFAULT_SETTINGS:
                        self._data[key] = value
        except (OSError, ValueError):
            pass
        self._normalize()

    def _normalize(self) -> None:
        d = self._data
        try:
            d["refresh_interval_sec"] = max(60, int(d.get("refresh_interval_sec") or 600))
        except (TypeError, ValueError):
            d["refresh_interval_sec"] = 600
        try:
            d["request_timeout_sec"] = max(5, int(d.get("request_timeout_sec") or 90))
        except (TypeError, ValueError):
            d["request_timeout_sec"] = 90
        try:
            d["connect_timeout_sec"] = max(3, int(d.get("connect_timeout_sec") or 15))
        except (TypeError, ValueError):
            d["connect_timeout_sec"] = 15
        try:
            d["max_retries"] = max(0, min(5, int(d.get("max_retries") or 2)))
        except (TypeError, ValueError):
            d["max_retries"] = 2
        if d.get("routing_strategy") not in {"balanced", "fastest", "keyless"}:
            d["routing_strategy"] = "balanced"
        d["host"] = str(d.get("host") or "0.0.0.0")
        try:
            port = int(d.get("port") or DEFAULT_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        if port == 8788:
            # Migration from the old default; 8788 is reserved by another app.
            port = DEFAULT_PORT
        d["port"] = port
        d["upstream_proxy"] = str(d.get("upstream_proxy") or "").strip()
        d["api_key"] = str(d.get("api_key") or "").strip()
        d["api_key_required"] = True
        d["dashboard_password_hash"] = str(d.get("dashboard_password_hash") or "").strip()
        d["trust_env_proxy"] = bool(d.get("trust_env_proxy"))
        d["enable_keyless"] = bool(d.get("enable_keyless", True))
        d["enable_keyed"] = bool(d.get("enable_keyed", True))
        d["log_limit"] = max(50, min(2000, int(d.get("log_limit") or 500)))

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def snapshot(self, *, include_secret: bool = False) -> dict[str, Any]:
        key = self.effective_api_key()
        with self._lock:
            data = {field: self._data[field] for field in PUBLIC_FIELDS}
            data["api_key_required"] = True
            data["dashboard_password_set"] = bool(self._data.get("dashboard_password_hash"))
            data["data_dir"] = str(self.path.parent)
        data["api_key_set"] = bool(key)
        data["api_key_masked"] = mask_secret(key)
        if include_secret:
            data["api_key"] = key
        return data

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("settings patch must be an object")
        with self._lock:
            previous = dict(self._data)
            for key, value in patch.items():
                if key not in EDITABLE_FIELDS:
                    continue
                if key == "api_key":
                    value = str(value or "").strip()
                elif key == "dashboard_password":
                    value = str(value or "")
                    if value:
                        self._data["dashboard_password_hash"] = generate_password_hash(value)
                    continue
                elif key in {"refresh_interval_sec", "request_timeout_sec", "connect_timeout_sec", "max_retries"}:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
                elif key in {"api_key_required", "trust_env_proxy", "enable_keyless", "enable_keyed"}:
                    value = bool(value)
                elif key in {"upstream_proxy", "routing_strategy"}:
                    value = str(value or "").strip()
                self._data[key] = value
            self._normalize()
            if not self.effective_api_key():
                self._data = previous
                raise ValueError("api_key cannot be empty; the gateway always requires an API key")
            atomic_write_json(self.path, self._data, mode=0o600)
            return self.snapshot(include_secret=True)

    def effective_api_key(self) -> str:
        env_key = str(os.environ.get("FREEAIHUB_API_KEY") or "").strip()
        with self._lock:
            return env_key or str(self._data.get("api_key") or "").strip()

    def set_generated_api_key(self) -> str:
        key = "fah-" + secrets.token_urlsafe(32)
        with self._lock:
            self._data["api_key"] = key
            self._data["api_key_required"] = True
            atomic_write_json(self.path, self._data, mode=0o600)
        return key

    def ensure_defaults(self) -> dict[str, bool]:
        """Create the first-run API key and dashboard password if missing."""
        generated_key = False
        default_password = False
        changed = False
        with self._lock:
            if not self.effective_api_key():
                self._data["api_key"] = "fah-" + secrets.token_urlsafe(32)
                generated_key = True
                changed = True
            if not self._data.get("dashboard_password_hash"):
                self._data["dashboard_password_hash"] = generate_password_hash(DEFAULT_DASHBOARD_PASSWORD)
                default_password = True
                changed = True
            self._data["api_key_required"] = True
            if changed:
                atomic_write_json(self.path, self._data, mode=0o600)
        return {"api_key_generated": generated_key, "dashboard_password_default": default_password}

    def verify_dashboard_password(self, password: str) -> bool:
        with self._lock:
            hashed = str(self._data.get("dashboard_password_hash") or "")
        return bool(hashed) and check_password_hash(hashed, str(password or ""))

    def set_dashboard_password(self, password: str) -> None:
        password = str(password or "")
        if not password:
            raise ValueError("dashboard password cannot be empty")
        with self._lock:
            self._data["dashboard_password_hash"] = generate_password_hash(password)
            atomic_write_json(self.path, self._data, mode=0o600)

    def ensure_dirs(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        STATIC_DIR.mkdir(parents=True, exist_ok=True)


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "•" * len(value)
    return value[:keep] + "…" + value[-keep:]


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
