from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


def _home() -> Path:
    return Path.home()


def _json_load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _json_save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".freeaihub.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _backup(path: Path) -> str:
    if not path.exists():
        return ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.freeaihub-{stamp}.bak")
    shutil.copy2(path, target)
    return str(target)


def _restore_latest(path: Path) -> bool:
    candidates = sorted(path.parent.glob(path.name + ".freeaihub-*.bak"))
    if not candidates:
        return False
    shutil.copy2(candidates[-1], path)
    return True


def _has_backup(path: Path) -> bool:
    return bool(list(path.parent.glob(path.name + ".freeaihub-*.bak")))


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _claude_dir(home: Path) -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")


def _codex_home(home: Path) -> Path:
    return Path(os.environ.get("CODEX_HOME") or home / ".codex")


def _config_home(home: Path) -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")


def _zcode_file(home: Path) -> Path:
    override = os.environ.get("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE")
    if override:
        return Path(override)
    return home / ".zcode" / "v2" / "provider_config.json"


def _dsh_home(home: Path) -> Path:
    return Path(os.environ.get("DSH_HOME") or home / ".dsh")


def _dsh_files(home: Path) -> list[Path]:
    root = _dsh_home(home)
    profile_files = sorted((root / "profiles").glob("*/cordis.patch.yml"))
    files: list[Path] = list(profile_files)
    files.append(root / ".credentials.yaml")
    legacy = root / "settings.yaml"
    if not profile_files and legacy.exists():
        files.append(legacy)
    return files


CLIENTS: list[dict[str, Any]] = [
    {
        "id": "claude-code",
        "name": "Claude Code",
        "protocol": "Anthropic Messages",
        "command": "claude",
        "paths": lambda home: [_claude_dir(home) / "settings.json"],
        "dirs": lambda home: [_claude_dir(home)],
        "action": "auto",
    },
    {
        "id": "codex",
        "name": "Codex CLI",
        "protocol": "OpenAI Responses",
        "command": "codex",
        "paths": lambda home: [_codex_home(home) / "config.toml", _codex_home(home) / "auth.json"],
        "dirs": lambda home: [_codex_home(home)],
        "action": "auto",
    },
    {
        "id": "crush",
        "name": "Crush",
        "protocol": "OpenAI Chat Completions",
        "command": "crush",
        "paths": lambda home: [_config_home(home) / "crush" / "crush.json"],
        "dirs": lambda home: [_config_home(home) / "crush"],
        "action": "auto",
    },
    {
        "id": "dsh",
        "name": "DeepSeek Harness",
        "protocol": "OpenAI Chat Completions",
        "command": "dsh",
        "paths": _dsh_files,
        "dirs": lambda home: [home / ".dsh"],
        "action": "auto",
    },
    {
        "id": "opencode",
        "name": "OpenCode",
        "protocol": "OpenAI Chat Completions",
        "command": "opencode",
        "paths": lambda home: [_config_home(home) / "opencode" / "opencode.json"],
        "dirs": lambda home: [_config_home(home) / "opencode"],
        "action": "auto",
    },
    {
        "id": "zcode",
        "name": "ZCode",
        "protocol": "OpenAI Chat Completions",
        "command": "zcode",
        "paths": lambda home: [_zcode_file(home)],
        "dirs": lambda home: [_zcode_file(home).parent.parent],
        "action": "auto",
    },
]


def _which(command: str) -> str:
    return shutil.which(command) or ""


def _client(client_id: str) -> dict[str, Any]:
    for item in CLIENTS:
        if item["id"] == client_id:
            return item
    raise KeyError(client_id)


def detect() -> list[dict[str, Any]]:
    home = _home()
    result = []
    for definition in CLIENTS:
        paths = [Path(p) for p in definition["paths"](home)]
        command = _which(definition["command"])
        existing = [str(p) for p in paths if p.exists()]
        installed = bool(command or existing or any(Path(d).exists() for d in definition["dirs"](home)))
        configured = False
        for raw in existing:
            path = Path(raw)
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")[:200000]
            except OSError:
                continue
            if "freeaihub" in text.lower() or "127.0.0.1:8899" in text:
                configured = True
                break
        result.append(
            {
                "id": definition["id"],
                "name": definition["name"],
                "protocol": definition["protocol"],
                "installed": installed,
                "command": command,
                "paths": existing,
                "display_paths": [str(Path(p)).replace(str(home), "~") for p in existing] or [str(Path(p)).replace(str(home), "~") for p in paths],
                "configured": configured,
                "has_backup": any(_has_backup(p) for p in paths),
                "action": definition["action"],
                "note": "写入前自动备份" if definition["action"] == "auto" else "DSH 使用 profile YAML，提供配置片段复制",
            }
        )
    return result


def _base(port: int) -> tuple[str, str]:
    root = f"http://127.0.0.1:{port}"
    return root, root + "/v1"


def _apply_claude(api_key: str, port: int) -> dict[str, Any]:
    path = _claude_dir(_home()) / "settings.json"
    backup = _backup(path)
    data = _json_load(path)
    env = dict(data.get("env") or {})
    env["ANTHROPIC_BASE_URL"] = _base(port)[0]
    env["ANTHROPIC_AUTH_TOKEN"] = api_key
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        env[key] = "auto"
    data["env"] = env
    _json_save(path, data)
    return {"path": str(path), "backup": backup}


def _toml_top(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    first_table = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("[")),
        len(lines),
    )
    head = [line for line in lines[:first_table] if not re.match(rf"^{re.escape(key)}\s*=", line)]
    return "\n".join([f'{key} = "{value}"', *head, *lines[first_table:]]).strip() + "\n"


def _toml_table(text: str, table: str, entries: dict[str, str]) -> str:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"[{table}]":
            start = index
            break
    if start is not None:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if lines[index].lstrip().startswith("["):
                end = index
                break
        lines = lines[:start] + lines[end:]
    block = [f"[{table}]"] + [f'{key} = "{value}"' for key, value in entries.items()]
    return "\n".join(lines + [""] + block).strip() + "\n"


def _apply_codex(api_key: str, port: int) -> dict[str, Any]:
    home = _codex_home(_home())
    config = home / "config.toml"
    auth = home / "auth.json"
    backup_config = _backup(config)
    backup_auth = _backup(auth)
    text = config.read_text(encoding="utf-8", errors="replace") if config.exists() else ""
    text = _toml_top(text, "model_provider", "freeaihub")
    text = _toml_top(text, "model", "auto")
    text = _toml_table(
        text,
        "model_providers.freeaihub",
        {
            "name": "Free AI Hub",
            "base_url": _base(port)[1],
            "wire_api": "responses",
            "experimental_bearer_token": api_key,
        },
    )
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text, encoding="utf-8")
    auth_data = _json_load(auth)
    auth_data["OPENAI_API_KEY"] = api_key
    _json_save(auth, auth_data)
    return {"path": str(config), "auth": str(auth), "backup": backup_config or backup_auth}


def _apply_opencode(api_key: str, port: int) -> dict[str, Any]:
    path = _config_home(_home()) / "opencode" / "opencode.json"
    backup = _backup(path)
    data = _json_load(path)
    data.setdefault("$schema", "https://opencode.ai/config.json")
    provider = _merge(
        (data.get("provider") or {}).get("freeaihub") or {},
        {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Free AI Hub",
            "options": {"baseURL": _base(port)[1], "apiKey": api_key},
            "models": {"auto": {"name": "Auto (Free AI Hub)"}},
        },
    )
    data = _merge(data, {"provider": {"freeaihub": provider}, "model": "freeaihub/auto"})
    _json_save(path, data)
    return {"path": str(path), "backup": backup}


def _apply_crush(api_key: str, port: int) -> dict[str, Any]:
    path = _config_home(_home()) / "crush" / "crush.json"
    backup = _backup(path)
    data = _json_load(path)
    provider = {
        "name": "Free AI Hub",
        "type": "openai",
        "base_url": _base(port)[1],
        "api_key": api_key,
        "models": [{"id": "auto", "name": "Auto (Free AI Hub)"}],
    }
    data = _merge(data, {"providers": {"freeaihub": provider}})
    _json_save(path, data)
    return {"path": str(path), "backup": backup}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find_yaml_item(lines: list[str], item_id: str) -> tuple[int, int] | None:
    starts = [index for index, line in enumerate(lines) if line.startswith("- ")]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        match = re.match(r"^-\s+id:\s*['\"]?([^'\"\s]+)", lines[start])
        if match and match.group(1) == item_id:
            return start, end
    return None


def _dsh_provider_lines(port: int) -> list[str]:
    return [
        "      freeaihub:",
        "        displayName: Free AI Hub",
        "        apiKeyEnv: FREEAIHUB_API_KEY",
        "        api: openai-completions",
        f"        baseURL: http://127.0.0.1:{port}/v1",
        "        models:",
        "          - id: auto",
        "            contextWindow: 128000",
        "            maxTokens: 8192",
    ]


def _dsh_patch_upsert(text: str, port: int) -> str:
    lines = text.lstrip("\ufeff").splitlines()
    lines = [line for line in lines if line.strip() != "[]"]
    item = _find_yaml_item(lines, "llm-pi-ai")
    if item is None:
        lines.extend(
            [
                "- id: llm-pi-ai",
                "  name: '@deepseek-ai/dsh-llm-pi-ai'",
                "  config:",
                "    providers:",
                *_dsh_provider_lines(port),
            ]
        )
    else:
        start, end = item
        config_index = next(
            (index for index in range(start + 1, end) if _indent(lines[index]) == 2 and lines[index].strip() == "config:"),
            None,
        )
        if config_index is None:
            lines[end:end] = ["  config:", "    providers:", *_dsh_provider_lines(port)]
        else:
            providers_index = next(
                (index for index in range(config_index + 1, end) if _indent(lines[index]) == 4 and lines[index].strip() == "providers:"),
                None,
            )
            if providers_index is None:
                lines[config_index + 1:config_index + 1] = ["    providers:", *_dsh_provider_lines(port)]
            else:
                existing = next(
                    (index for index in range(providers_index + 1, end) if _indent(lines[index]) == 6 and lines[index].strip().startswith("freeaihub:")),
                    None,
                )
                if existing is None:
                    insert_at = providers_index + 1
                    for index in range(providers_index + 1, end):
                        if lines[index].strip() and _indent(lines[index]) <= 4:
                            break
                        insert_at = index + 1
                    lines[insert_at:insert_at] = _dsh_provider_lines(port)

    default_item = _find_yaml_item(lines, "agent-default-model")
    if default_item is None:
        lines.extend(
            [
                "- id: agent-default-model",
                "  config:",
                "    provider: freeaihub",
                "    model: auto",
            ]
        )
    else:
        start, end = default_item
        config_index = next(
            (index for index in range(start + 1, end) if _indent(lines[index]) == 2 and lines[index].strip() == "config:"),
            None,
        )
        if config_index is None:
            lines[end:end] = ["  config:", "    provider: freeaihub", "    model: auto"]
        else:
            for index in range(config_index + 1, end):
                stripped = lines[index].strip()
                if _indent(lines[index]) == 4 and stripped.startswith("provider:"):
                    lines[index] = "    provider: freeaihub"
                elif _indent(lines[index]) == 4 and stripped.startswith("model:"):
                    lines[index] = "    model: auto"
            # Our `auto` model does not declare reasoning levels; leaving a
            # `reasoningEffort` from the previous default can make DSH reject it.
            start, end = _find_yaml_item(lines, "agent-default-model") or (0, 0)
            lines = [
                line
                for index, line in enumerate(lines)
                if not (
                    start < index < end
                    and _indent(line) == 4
                    and line.strip().startswith(("reasoningEffort:", "reasoning_effort:"))
                )
            ]
    return "\n".join(lines).strip() + "\n"


def _dsh_credentials_upsert(text: str, key: str, value: str) -> str:
    lines = text.lstrip("\ufeff").splitlines()
    for line in lines:
        if _indent(line) == 0 and line.strip().startswith("refs:") and line.strip() != "refs:":
            raise ValueError("DSH credentials `refs:` must be a block mapping.")
    # This key belongs to us; dropping every old spelling is what repairs the
    # previous top-level entry and makes re-applying idempotent.
    lines = [line for line in lines if not re.match(rf"^\s*{re.escape(key)}\s*:", line)]
    refs_index = next(
        (index for index, line in enumerate(lines) if _indent(line) == 0 and line.strip() == "refs:"),
        None,
    )
    if refs_index is None:
        lines.extend(["refs:", f"  {key}: {json.dumps(value)}"])
    else:
        end = len(lines)
        for index in range(refs_index + 1, len(lines)):
            if lines[index].strip() and _indent(lines[index]) == 0:
                end = index
                break
        lines.insert(end, f"  {key}: {json.dumps(value)}")
    return "\n".join(lines).strip() + "\n"


def _apply_dsh(api_key: str, port: int) -> dict[str, Any]:
    root = _dsh_home(_home())
    patches = sorted((root / "profiles").glob("*/cordis.patch.yml"))
    if not patches:
        raise ValueError("No DSH profile cordis.patch.yml found; open DSH once, then retry.")
    files: dict[str, str] = {}
    backups: list[str] = []
    for path in patches:
        backup = _backup(path)
        if backup:
            backups.append(backup)
        files[str(path)] = _dsh_patch_upsert(path.read_text(encoding="utf-8", errors="replace"), port)
        path.write_text(files[str(path)], encoding="utf-8")
    credentials = root / ".credentials.yaml"
    backup = _backup(credentials)
    if backup:
        backups.append(backup)
    text = credentials.read_text(encoding="utf-8", errors="replace") if credentials.exists() else ""
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text(_dsh_credentials_upsert(text, "FREEAIHUB_API_KEY", api_key), encoding="utf-8")
    try:
        os.chmod(credentials, 0o600)
    except OSError:
        pass
    files[str(credentials)] = "FREEAIHUB_API_KEY"
    return {"files": files, "backup": backups[0] if backups else ""}


def _apply_zcode(api_key: str, port: int) -> dict[str, Any]:
    path = _zcode_file(_home())
    backup = _backup(path)
    data = _json_load(path)
    if not data:
        data = {
            "schemaVersion": 1,
            "config": {"providerOrder": [], "providerConfigRules": {"providerRules": []}},
        }
    config = data.setdefault("config", {})
    if not isinstance(config, dict):
        raise ValueError("ZCode config has no config object.")
    provider_holder = config.get("providerConfigRules")
    if not isinstance(provider_holder, dict):
        provider_holder = {}
    rules = provider_holder.get("providerRules")
    if not isinstance(rules, list):
        rules = []
    provider_holder["providerRules"] = rules
    config["providerConfigRules"] = provider_holder
    model_holder = config.get("modelConfigRules")
    if not isinstance(model_holder, dict):
        model_holder = {}
    model_holder.setdefault("providerModelRules", [])
    model_holder.setdefault("manualProviderModelRules", [])
    config["modelConfigRules"] = model_holder
    rule = {
        "providerId": "freeaihub",
        "providerName": "Free AI Hub",
        "config": {
            "group": "standard-personal",
            "access": {"type": "api-key", "apiKey": api_key},
            "api": {"type": "openai-chat-completions", "baseUrl": _base(port)[1]},
            "personalModelIds": ["auto"],
        },
    }
    for index, existing in enumerate(rules):
        if isinstance(existing, dict) and existing.get("providerId") == "freeaihub":
            rules[index] = _merge(existing, rule)
            break
    else:
        rules.append(rule)
    order = config.get("providerOrder")
    if not isinstance(order, list):
        order = []
        config["providerOrder"] = order
    if "freeaihub" not in order:
        order.append("freeaihub")
    config["defaultModelSelection"] = {"providerId": "freeaihub", "modelId": "auto"}
    _json_save(path, data)
    return {"path": str(path), "backup": backup}


def configure(client_id: str, api_key: str, port: int) -> dict[str, Any]:
    if not api_key:
        raise ValueError("API key is required")
    definition = _client(client_id)
    if client_id == "claude-code":
        result = _apply_claude(api_key, port)
    elif client_id == "codex":
        result = _apply_codex(api_key, port)
    elif client_id == "opencode":
        result = _apply_opencode(api_key, port)
    elif client_id == "crush":
        result = _apply_crush(api_key, port)
    elif client_id == "zcode":
        result = _apply_zcode(api_key, port)
    elif client_id == "dsh":
        result = _apply_dsh(api_key, port)
    else:
        raise KeyError(client_id)
    result_files = result.get("files") if isinstance(result.get("files"), dict) else {
        key: value for key, value in result.items() if key != "backup"
    }
    return {
        "client": client_id,
        "action": "written",
        "files": result_files,
        "backup": result.get("backup", ""),
        "message": "已写入配置；重启对应客户端后生效。",
    }


def restore(client_id: str) -> dict[str, Any]:
    definition = _client(client_id)
    restored = []
    for path in definition["paths"](_home()):
        if _restore_latest(Path(path)):
            restored.append(str(path))
    return {"client": client_id, "restored": restored}
