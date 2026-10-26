from __future__ import annotations

import logging
import secrets
import threading
from datetime import timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session
from werkzeug.exceptions import HTTPException

from . import __version__
from . import clients as client_tools
from .catalog import Catalog, utc_now
from .config import DATA_DIR, STATIC_DIR, Settings, atomic_write_text
from .protocols import (
    chat_to_messages,
    chat_to_responses,
    messages_stream,
    messages_to_chat,
    responses_stream,
    responses_to_chat,
)
from .registry import Registry
from .router import Router, RouterError
from .store import KeyStore, RefreshState, Stats, estimate_tokens

LOG = logging.getLogger("freeaihub")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(*, start_background: bool = True, data_dir: Path | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["JSON_AS_ASCII"] = False
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

    base_dir = Path(data_dir or DATA_DIR)
    settings = Settings(base_dir / "settings.json")
    settings.ensure_dirs()
    defaults = settings.ensure_defaults()
    app.secret_key = _load_session_secret(base_dir / ".session_secret")
    keys = KeyStore(base_dir / "keys.json")
    stats = Stats(base_dir / "usage.jsonl")
    refresh_state = RefreshState()
    catalog = Catalog(base_dir).load()
    registry = Registry(
        catalog,
        keys,
        settings,
        stats,
        refresh_state,
        state_path=base_dir / "state.json",
        model_settings_path=base_dir / "model_settings.json",
    )
    router = Router(registry, settings, keys, stats)

    app.extensions["freeaihub"] = {
        "settings": settings,
        "keys": keys,
        "stats": stats,
        "refresh_state": refresh_state,
        "catalog": catalog,
        "registry": registry,
        "router": router,
        "defaults": defaults,
        "data_dir": base_dir,
    }

    if start_background:
        _start_refresher(registry, settings, refresh_state)

    # ------------------------------------------------------------------ auth
    @app.before_request
    def _api_auth() -> Response | None:
        if request.method == "OPTIONS":
            return None
        path = request.path
        is_api = (
            path in {"/v1", "/models", "/chat/completions", "/responses", "/messages"}
            or path.startswith("/v1/")
            or path.startswith("/models/")
            or path.startswith("/responses/")
            or path.startswith("/messages/")
        )
        if not is_api:
            return None
        expected = settings.effective_api_key()
        if not expected:
            return jsonify({"error": {"message": "API key is not initialized.", "type": "unauthorized", "code": "api_key_missing"}}), 503
        provided = ""
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            provided = header[7:].strip()
        if not provided:
            provided = request.headers.get("x-api-key", "").strip()
        if not provided or not secrets.compare_digest(provided, expected):
            return jsonify({"error": {"message": "Invalid API key.", "type": "unauthorized", "code": "invalid_api_key"}}), 401
        return None

    @app.before_request
    def _dashboard_auth() -> Response | None:
        path = request.path
        if (
            path.startswith("/v1")
            or path in {"/models", "/chat/completions", "/responses", "/messages"}
            or path.startswith("/models/")
            or path.startswith("/responses/")
            or path.startswith("/messages/")
            or path in {"/login", "/health", "/favicon.svg"}
            or path.startswith("/static/")
        ):
            return None
        if session.get("dashboard") is True:
            return None
        if path.startswith("/api/"):
            return jsonify({"error": {"message": "Dashboard login required.", "type": "unauthorized", "code": "login_required"}}), 401
        return redirect("/login")

    @app.after_request
    def _cors(response: Response) -> Response:
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        response.headers.setdefault(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-API-Key, x-api-key, anthropic-version, anthropic-beta",
        )
        response.headers.setdefault("X-FreeAIHub-Version", __version__)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path == "/" or request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # ---------------------------------------------------------------- static
    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response:
        if request.method == "GET":
            if session.get("dashboard") is True:
                return redirect("/")
            return send_from_directory(STATIC_DIR, "login.html")
        password = request.form.get("password", "")
        if settings.verify_dashboard_password(password):
            session.clear()
            session["dashboard"] = True
            session.permanent = True
            return redirect("/")
        return redirect("/login?error=1")

    @app.get("/logout")
    def logout() -> Response:
        session.clear()
        return redirect("/login")

    @app.get("/")
    def index() -> Response:
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/favicon.svg")
    def favicon() -> Response:
        return send_from_directory(STATIC_DIR, "favicon.svg")

    @app.get("/health")
    def health() -> Response:
        snapshot = registry.stats_snapshot()
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "time": utc_now(),
                "providers_online": snapshot.get("providers_online", 0),
                "models_available": snapshot.get("models_available", 0),
                "refreshing": refresh_state.snapshot()["running"],
            }
        )

    # ------------------------------------------------------------------- API
    @app.get("/v1")
    def api_info() -> Response:
        return jsonify(
            {
                "name": "Free AI Hub",
                "version": __version__,
                "api_base": "/v1",
                "models": "/v1/models",
                "chat_completions": "/v1/chat/completions",
                "responses": "/v1/responses",
                "anthropic_messages": "/v1/messages",
                "dashboard": "/",
                "docs": "/#access",
            }
        )

    @app.get("/v1/models")
    @app.get("/models")
    def list_models() -> Response:
        models = registry.public_models()
        data = []
        for model in models:
            provider = catalog.provider(model.provider_id)
            if provider is None:
                continue
            data.append(
                {
                    "id": model.public_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": provider.id,
                    "name": model.upstream_id,
                    "provider": provider.id,
                    "provider_name": provider.name,
                    "keyless": provider.keyless,
                    "context_length": model.context,
                    "modalities": model.modalities,
                    "free": True,
                    "status": provider.status,
                }
            )
        data.sort(key=lambda item: (not item["keyless"], item["provider"], item["id"]))
        data.insert(
            0,
            {
                "id": "auto",
                "object": "model",
                "created": 0,
                "owned_by": "freeaihub",
                "name": "Auto weighted route",
                "provider": "freeaihub",
                "provider_name": "Free AI Hub",
                "keyless": True,
                "context_length": 0,
                "modalities": ["text"],
                "free": True,
                "status": "available",
            },
        )
        return jsonify({"object": "list", "data": data})

    @app.get("/v1/models/<path:model_id>")
    @app.get("/models/<path:model_id>")
    def get_model(model_id: str) -> Response:
        if model_id == "auto":
            return jsonify(
                {
                    "id": "auto",
                    "object": "model",
                    "created": 0,
                    "owned_by": "freeaihub",
                    "name": "Auto weighted route",
                    "provider": "freeaihub",
                    "keyless": True,
                    "context_length": 0,
                    "modalities": ["text"],
                    "status": "available",
                }
            )
        model = registry.resolve(model_id)
        if model is None:
            return jsonify({"error": {"message": "Model not found.", "type": "model_not_found", "code": "model_not_found"}}), 404
        provider = catalog.provider(model.provider_id)
        if provider is None:
            return jsonify({"error": {"message": "Provider not found.", "type": "provider_not_found", "code": "provider_not_found"}}), 404
        return jsonify(
            {
                "id": model.public_id,
                "object": "model",
                "created": 0,
                "owned_by": provider.id,
                "name": model.upstream_id,
                "provider": provider.id,
                "keyless": provider.keyless,
                "context_length": model.context,
                "modalities": model.modalities,
                "status": provider.status,
            }
        )

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    def chat_completions() -> Response:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise RouterError(400, "invalid_json", "Request body must be a JSON object.")
        if payload.get("stream"):
            result = router.stream(payload)
            response = Response(router.iter_stream(result), mimetype="text/event-stream")
            response.headers["Cache-Control"] = "no-cache"
            response.headers["X-Accel-Buffering"] = "no"
            response.headers["X-FreeAIHub-Provider"] = result.provider.id
            response.headers["X-FreeAIHub-Model"] = result.model.public_id
            return response
        result = router.complete(payload)
        data = result.data
        if isinstance(data, dict):
            # Echo the client's model name (it may be arbitrary) and make
            # reasoning-only responses usable by plain OpenAI clients.
            data["model"] = str(payload.get("model") or data.get("model") or "")
            for choice in data.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if not isinstance(message, dict):
                    continue
                if not message.get("content") and isinstance(message.get("reasoning"), str):
                    message["content"] = message["reasoning"]
                    message.setdefault("reasoning_content", message["reasoning"])
        response = jsonify(data)
        response.headers["X-FreeAIHub-Provider"] = result.provider.id
        response.headers["X-FreeAIHub-Model"] = result.model.public_id
        response.headers["X-FreeAIHub-Latency-Ms"] = str(round(result.latency_ms, 1))
        return response

    def _stream_headers(response: Response, *, protocol: str, provider: str, model: str) -> Response:
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        response.headers["X-FreeAIHub-Protocol"] = protocol
        response.headers["X-FreeAIHub-Provider"] = provider
        response.headers["X-FreeAIHub-Model"] = model
        return response

    @app.post("/v1/responses")
    @app.post("/responses")
    def openai_responses() -> Response:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise RouterError(400, "invalid_json", "Request body must be a JSON object.")
        requested_model = str(body.get("model") or "auto")
        try:
            chat_body = responses_to_chat(body)
        except ValueError as exc:
            raise RouterError(400, "invalid_request", str(exc)) from None
        if body.get("stream"):
            result = router.stream(chat_body)
            response = Response(
                responses_stream(router.iter_stream(result), requested_model),
                mimetype="text/event-stream",
            )
            return _stream_headers(response, protocol="openai-responses", provider=result.provider.id, model=requested_model)
        result = router.complete(chat_body)
        response = jsonify(chat_to_responses(result.data, requested_model))
        response.headers["X-FreeAIHub-Protocol"] = "openai-responses"
        response.headers["X-FreeAIHub-Provider"] = result.provider.id
        response.headers["X-FreeAIHub-Model"] = requested_model
        response.headers["X-FreeAIHub-Latency-Ms"] = str(round(result.latency_ms, 1))
        return response

    @app.post("/v1/responses/input_tokens")
    @app.post("/responses/input_tokens")
    def responses_input_tokens() -> Response:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise RouterError(400, "invalid_json", "Request body must be a JSON object.")
        try:
            chat_body = responses_to_chat(body)
        except ValueError as exc:
            raise RouterError(400, "invalid_request", str(exc)) from None
        text = " ".join(str(message.get("content") or "") for message in chat_body.get("messages", []))
        return jsonify({"input_tokens": estimate_tokens(text)})

    @app.post("/v1/messages")
    @app.post("/messages")
    def anthropic_messages() -> Response:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise RouterError(400, "invalid_json", "Request body must be a JSON object.")
        requested_model = str(body.get("model") or "auto")
        try:
            chat_body = messages_to_chat(body)
        except ValueError as exc:
            raise RouterError(400, "invalid_request", str(exc)) from None
        if body.get("stream"):
            result = router.stream(chat_body)
            response = Response(
                messages_stream(router.iter_stream(result), requested_model),
                mimetype="text/event-stream",
            )
            return _stream_headers(response, protocol="anthropic-messages", provider=result.provider.id, model=requested_model)
        result = router.complete(chat_body)
        response = jsonify(chat_to_messages(result.data, requested_model))
        response.headers["X-FreeAIHub-Protocol"] = "anthropic-messages"
        response.headers["X-FreeAIHub-Provider"] = result.provider.id
        response.headers["X-FreeAIHub-Model"] = requested_model
        response.headers["X-FreeAIHub-Latency-Ms"] = str(round(result.latency_ms, 1))
        return response

    @app.post("/v1/messages/count_tokens")
    @app.post("/messages/count_tokens")
    def anthropic_count_tokens() -> Response:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise RouterError(400, "invalid_json", "Request body must be a JSON object.")
        text = str(body.get("system") or "")
        for message in body.get("messages") or []:
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    text += "\n" + content
                elif isinstance(content, list):
                    text += "\n" + "".join(str(block.get("text") or "") for block in content if isinstance(block, dict))
        return jsonify({"input_tokens": estimate_tokens(text)})

    @app.route("/v1/<path:_path>", methods=["OPTIONS"])
    def api_options(_path: str) -> Response:
        return Response(status=204)

    # ------------------------------------------------------------- dashboard
    @app.get("/api/overview")
    def api_overview() -> Response:
        snapshot = registry.stats_snapshot()
        usage = stats.overview()
        snapshot.update(
            {
                "requests": usage["requests"],
                "successes": usage["successes"],
                "failures": usage["failures"],
                "success_rate": usage["success_rate"],
                "tokens_in": usage["tokens_in"],
                "tokens_out": usage["tokens_out"],
                "tokens_total": usage["tokens_total"],
                "cached_tokens": usage["cached_tokens"],
                "cache_reported": usage["cache_reported"],
                "cache_hit_rate": usage["cache_hit_rate"],
                "avg_speed": usage["avg_speed"],
                "avg_latency_ms": usage["avg_latency_ms"],
                "p95_latency_ms": usage["p95_latency_ms"],
                "models_detail": usage["models_detail"],
                "series": usage["series"],
            }
        )
        keyless = [
            provider.to_dict(key_configured=keys.configured(provider))
            for provider in sorted(catalog.providers.values(), key=lambda p: p.name.lower())
            if provider.keyless and provider.routable_candidate and not provider.restricted
        ]
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "now": utc_now(),
                "counts": snapshot,
                "refresh": refresh_state.snapshot(),
                "settings": settings.snapshot(),
                "keyless": keyless,
                "recent": stats.recent(8),
            }
        )

    @app.get("/api/providers")
    def api_providers() -> Response:
        usage = stats.overview()
        providers = registry.provider_dicts()
        for item in providers:
            provider = catalog.provider(item["id"])
            if provider is None:
                continue
            item["key_masked"] = keys.masked(provider)
            item["key_source"] = keys.source(provider)
            item["key_hints"] = keys.hints(provider)
            item["requests"] = usage["providers"].get(provider.id, 0)
        return jsonify({"ok": True, "providers": providers, "refreshed_at": registry.stats_snapshot().get("refreshed_at")})

    @app.get("/api/clients")
    def api_clients() -> Response:
        return jsonify({"ok": True, "clients": client_tools.detect()})

    @app.post("/api/clients/<client_id>/configure")
    def api_client_configure(client_id: str) -> Response:
        try:
            result = client_tools.configure(client_id, settings.effective_api_key(), int(settings.get("port") or 8899))
        except KeyError:
            return jsonify({"error": {"message": "Unknown client.", "type": "client_not_found", "code": "client_not_found"}}), 404
        except (OSError, ValueError) as exc:
            return jsonify({"error": {"message": str(exc), "type": "client_config_failed", "code": "client_config_failed"}}), 400
        return jsonify({"ok": True, **result})

    @app.post("/api/clients/<client_id>/restore")
    def api_client_restore(client_id: str) -> Response:
        try:
            result = client_tools.restore(client_id)
        except KeyError:
            return jsonify({"error": {"message": "Unknown client.", "type": "client_not_found", "code": "client_not_found"}}), 404
        return jsonify({"ok": True, **result})

    @app.get("/api/models")
    def api_models() -> Response:
        models = registry.all_model_dicts()
        providers = [
            {"id": p.id, "name": p.name, "keyless": p.keyless, "status": p.status}
            for p in sorted(catalog.providers.values(), key=lambda p: p.name.lower())
        ]
        return jsonify({"ok": True, "models": models, "providers": providers})

    @app.post("/api/models/<path:model_id>/settings")
    def api_model_settings(model_id: str) -> Response:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": {"message": "JSON object expected.", "type": "invalid_json", "code": "invalid_json"}}), 400
        enabled = body.get("enabled")
        weight = body.get("weight")
        try:
            registry.set_model_policy(
                model_id,
                enabled=None if enabled is None else bool(enabled),
                weight=None if weight is None else float(weight),
            )
        except KeyError:
            return jsonify({"error": {"message": "Model not found.", "type": "model_not_found", "code": "model_not_found"}}), 404
        except (TypeError, ValueError) as exc:
            return jsonify({"error": {"message": str(exc), "type": "invalid_settings", "code": "invalid_settings"}}), 400
        updated = next((item for item in registry.all_model_dicts() if item["id"] == model_id), None)
        return jsonify({"ok": True, "model": updated})

    @app.get("/api/logs")
    def api_logs() -> Response:
        try:
            limit = max(1, min(500, int(request.args.get("limit", 100))))
        except ValueError:
            limit = 100
        return jsonify({"ok": True, "logs": stats.recent(limit)})

    @app.post("/api/refresh")
    def api_refresh() -> Response:
        if not refresh_state.snapshot()["running"]:
            threading.Thread(
                target=registry.refresh_all,
                kwargs={"probe_chat": True, "reason": "manual"},
                daemon=True,
                name="freeaihub-manual-refresh",
            ).start()
        return jsonify({"ok": True, "refresh": refresh_state.snapshot()}), 202

    @app.get("/api/refresh/status")
    def api_refresh_status() -> Response:
        return jsonify({"ok": True, "refresh": refresh_state.snapshot()})

    @app.post("/api/test")
    def api_test() -> Response:
        body = request.get_json(silent=True) or {}
        try:
            result = router.test(
                provider_id=str(body.get("provider_id") or "") or None,
                model_id=str(body.get("model_id") or "") or None,
                prompt=str(body.get("prompt") or "Reply with exactly: OK"),
            )
        except RouterError as exc:
            return jsonify(exc.to_dict()), exc.status
        return jsonify(result)

    @app.post("/api/providers/<provider_id>/test")
    def api_test_provider(provider_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        try:
            result = router.test(provider_id=provider_id, prompt=str(body.get("prompt") or "Reply with exactly: OK"))
        except RouterError as exc:
            return jsonify(exc.to_dict()), exc.status
        return jsonify(result)

    @app.post("/api/models/<path:model_id>/test")
    def api_test_model(model_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        try:
            result = router.test(model_id=model_id, prompt=str(body.get("prompt") or "Reply with exactly: OK"))
        except RouterError as exc:
            return jsonify(exc.to_dict()), exc.status
        return jsonify(result)

    @app.route("/api/providers/<provider_id>/key", methods=["POST", "DELETE"])
    def api_provider_key(provider_id: str) -> Response:
        provider = catalog.provider(provider_id)
        if provider is None:
            return jsonify({"error": {"message": "Provider not found.", "type": "provider_not_found", "code": "provider_not_found"}}), 404
        if request.method == "DELETE":
            keys.delete(provider_id)
            value = ""
        else:
            body = request.get_json(silent=True) or {}
            value = str(body.get("api_key") or "").strip()
            keys.set(provider_id, value)
        if value:
            threading.Thread(
                target=registry.refresh_one,
                args=(provider_id,),
                kwargs={"probe_chat": True},
                daemon=True,
                name=f"freeaihub-key-test-{provider_id}",
            ).start()
        return jsonify(
            {
                "ok": True,
                "provider_id": provider_id,
                "key_configured": keys.configured(provider),
                "key_masked": keys.masked(provider),
                "key_source": keys.source(provider),
                "key_hint": keys.hints(provider)[0] if keys.hints(provider) else "",
            }
        )

    @app.get("/api/settings")
    def api_settings_get() -> Response:
        return jsonify({"ok": True, "settings": settings.snapshot(include_secret=True)})

    @app.post("/api/settings")
    def api_settings_set() -> Response:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": {"message": "JSON object expected.", "type": "invalid_json", "code": "invalid_json"}}), 400
        try:
            updated = settings.update(body)
        except ValueError as exc:
            return jsonify({"error": {"message": str(exc), "type": "invalid_settings", "code": "invalid_settings"}}), 400
        return jsonify({"ok": True, "settings": updated})

    # --------------------------------------------------------------- errors
    @app.errorhandler(RouterError)
    def _router_error(exc: RouterError) -> tuple[Response, int]:
        if request.path.startswith("/v1/messages") or request.path.startswith("/messages"):
            return (
                jsonify({"type": "error", "error": {"type": "api_error", "message": exc.message}}),
                exc.status,
            )
        return jsonify(exc.to_dict()), exc.status

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException) -> tuple[Response, int]:
        if request.path.startswith("/api/") or request.path.startswith("/v1/"):
            return jsonify({"error": {"message": exc.description, "type": exc.name.lower(), "code": exc.name.lower()}}), exc.code or 500
        return exc  # type: ignore[return-value]

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception) -> tuple[Response, int]:
        LOG.exception("unhandled error on %s", request.path)
        if request.path.startswith("/api/") or request.path.startswith("/v1/"):
            return jsonify({"error": {"message": f"{type(exc).__name__}: {exc}", "type": "internal_error", "code": "internal_error"}}), 500
        return jsonify({"error": {"message": "internal error", "type": "internal_error", "code": "internal_error"}}), 500

    return app


def _load_session_secret(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    value = secrets.token_hex(32)
    atomic_write_text(path, value, mode=0o600)
    return value


def _start_refresher(registry: Registry, settings: Settings, refresh_state: RefreshState) -> None:
    def initial() -> None:
        try:
            registry.refresh_all(probe_chat=False, reason="startup")
        except Exception:  # noqa: BLE001
            LOG.exception("startup refresh failed")

    def loop() -> None:
        while True:
            interval = max(60, int(settings.get("refresh_interval_sec") or 600))
            threading.Event().wait(interval)
            if refresh_state.snapshot()["running"]:
                continue
            try:
                registry.refresh_all(probe_chat=False, reason="scheduled")
            except Exception:  # noqa: BLE001
                LOG.exception("scheduled refresh failed")

    threading.Thread(target=initial, daemon=True, name="freeaihub-startup-refresh").start()
    threading.Thread(target=loop, daemon=True, name="freeaihub-scheduler").start()
