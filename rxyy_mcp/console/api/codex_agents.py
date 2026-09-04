"""Safe, structured management of the rxyy Codex Agent configuration.

The AICodebrain ``shared/codex-agents`` directory is the source of truth.  The
Codex home is a runtime copy.  This module deliberately exposes only the
managed fields to the WebView; unknown TOML keys and values never cross the
bridge and are retained by ``tomlkit`` when a file is edited.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import tomlkit

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - exercised on the bundled Python 3.8 runtime
    tomllib = None  # type: ignore[assignment]

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 2
AGENT_FILENAME = "luna-worker.toml"
AGENT_NAME = "luna_worker"
MANAGED_MARKER = "# rxyy-tools-managed: aicodebrain/codex-agents/luna-worker.toml"
SYNC_SCRIPT_NAME = "sync-codex-agents.ps1"
SYNC_SCRIPT_MARKER = "# rxyy-tools-managed: aicodebrain/scripts/sync-codex-agents.ps1"
SAFE_SANDBOX_MODE = "workspace-write"
ALLOWED_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
V2_MULTI_AGENT_VERSION = "v2"
MODEL_CAPABILITY_TIMEOUT_SECONDS = 10
MODEL_CAPABILITY_CACHE_TTL_SECONDS = 15
PENDING_CAPABILITY_WARNING = "本机 Codex 模型能力尚未验证；点刷新可核对 V2 子代理兼容性。"
_runtime_model_capability_cache: tuple[float, dict[str, str] | None, str | None] | None = None
DEFAULT_MODEL_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "gpt-5.6-sol",
        "label": "GPT-5.6 Sol",
        "reasoning_efforts": ("low", "medium", "high", "xhigh", "max", "ultra"),
    },
    {
        "id": "gpt-5.6-luna",
        "label": "GPT-5.6 Luna",
        "reasoning_efforts": ("low", "medium", "high", "xhigh", "max"),
    },
    {
        "id": "gpt-5.6-terra",
        "label": "GPT-5.6 Terra",
        "reasoning_efforts": ("low", "medium", "high", "xhigh", "max", "ultra"),
    },
)
SAFE_AGENT_INSTRUCTIONS = (
    "Work only on the bounded subtask and file ownership explicitly delegated by the primary agent.\n"
    "Read the applicable repository rules and relevant source before editing. Preserve unrelated and pre-existing changes.\n"
    "Do not spawn more agents. Do not read credentials, expand scope, deploy, publish, commit, push, or perform destructive or production actions.\n"
    "Never edit a file concurrently owned by another writing agent. Stop and report ambiguity or a required boundary expansion.\n"
    "Implement the smallest complete change, run focused verification, and return changed files, verification evidence, and remaining risks to the primary agent.\n"
    "The primary agent retains architecture decisions, security-sensitive work, final diff review, final verification, and delivery."
)

DEFAULT_PRIMARY: dict[str, Any] = {
    "model": "gpt-5.6-sol",
    "model_reasoning_effort": "ultra",
}
DEFAULT_GLOBAL: dict[str, Any] = {
    "enabled": True,
    "max_concurrent_threads_per_session": 10,
    "default_subagent_model": "gpt-5.6-terra",
    "default_subagent_reasoning_effort": "max",
}
DEFAULT_AGENT: dict[str, Any] = {
    "name": AGENT_NAME,
    "description": "Implements and tests bounded subtasks delegated by the primary Sol agent.",
    "model": "gpt-5.6-terra",
    "model_reasoning_effort": "max",
    "sandbox_mode": SAFE_SANDBOX_MODE,
    "developer_instructions": SAFE_AGENT_INSTRUCTIONS,
}


class AgentConfigError(ValueError):
    """A user-correctable configuration error with no sensitive details."""


class AgentConfigConflict(AgentConfigError):
    """The source changed after the WebView loaded it."""


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attrs & getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def _assert_safe_path(path: Path) -> None:
    """Reject symlinks/junctions in the fixed configuration paths."""
    path = Path(path)
    existing: list[Path] = []
    cur = path
    while True:
        if os.path.lexists(cur):
            existing.append(cur)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    for item in reversed(existing):
        if _is_reparse(item):
            raise AgentConfigError("配置路径包含符号链接或 junction，已拒绝操作")


def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_path(path.parent)
    _assert_safe_path(path)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _read_bytes(path: Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def _revision(manifest: Path, agent: Path) -> str:
    digest = hashlib.sha256()
    for path in (manifest, agent):
        raw = _read_bytes(path)
        digest.update(b"<missing>\0" if raw is None else raw)
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_model_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise AgentConfigError(f"{field} 不能为空且长度不能超过 128")
    if not all(ch.isascii() and (ch.isalnum() or ch in "._:/-") for ch in value):
        raise AgentConfigError(f"{field} 含有不支持的字符")
    return value


def _safe_model_catalog(value: Any) -> list[dict[str, Any]]:
    """Validate the editable, non-secret model catalogue in the source manifest."""
    if not isinstance(value, list) or not value or len(value) > 64:
        raise AgentConfigError("模型目录必须包含 1 到 64 个模型")
    seen: set[str] = set()
    catalog: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise AgentConfigError("模型目录条目格式不正确")
        model_id = _safe_model_id(entry.get("id"), "模型目录型号")
        key = model_id.casefold()
        if key in seen:
            raise AgentConfigError("模型目录包含重复型号")
        seen.add(key)
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 128:
            raise AgentConfigError("模型目录显示名不能为空且长度不能超过 128")
        if any(ord(ch) < 32 for ch in label):
            raise AgentConfigError("模型目录显示名含有不支持的控制字符")
        efforts = entry.get("reasoning_efforts")
        if not isinstance(efforts, list) or not efforts or len(efforts) > len(ALLOWED_EFFORTS):
            raise AgentConfigError("模型目录推理强度格式不正确")
        normalized_efforts: list[str] = []
        for effort in efforts:
            if not isinstance(effort, str) or effort not in ALLOWED_EFFORTS:
                raise AgentConfigError("模型目录包含无效推理强度")
            if effort in normalized_efforts:
                raise AgentConfigError("模型目录包含重复推理强度")
            normalized_efforts.append(effort)
        catalog.append({
            "id": model_id,
            "label": label.strip(),
            "reasoning_efforts": normalized_efforts,
        })
    return catalog


def _default_model_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": entry["id"],
            "label": entry["label"],
            "reasoning_efforts": list(entry["reasoning_efforts"]),
        }
        for entry in DEFAULT_MODEL_CATALOG
    ]


def _query_runtime_model_capabilities() -> tuple[dict[str, str] | None, str | None]:
    """Read only the local CLI's public model capability metadata.

    This intentionally invokes only ``codex debug models`` and projects no
    model instructions, account state, auth data, tokens, stderr, or config
    values into the WebView.
    """
    codex = shutil.which("codex")
    if not codex:
        return None, "未找到本机 Codex CLI，未验证模型 V2 子代理兼容性；保留离线配置管理。"
    try:
        result = subprocess.run(
            [codex, "debug", "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=MODEL_CAPABILITY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "无法读取本机 Codex 模型能力，未验证 V2 子代理兼容性；保留离线配置管理。"
    if result.returncode != 0:
        return None, "本机 Codex 模型能力查询失败，未验证 V2 子代理兼容性；保留离线配置管理。"
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None, "本机 Codex 模型能力输出异常，未验证 V2 子代理兼容性；保留离线配置管理。"
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None, "本机 Codex 模型能力输出异常，未验证 V2 子代理兼容性；保留离线配置管理。"

    versions: dict[str, str] = {}
    malformed = False
    for item in models:
        if not isinstance(item, dict):
            malformed = True
            continue
        slug = item.get("slug")
        version = item.get("multi_agent_version")
        if not isinstance(slug, str) or not slug or len(slug) > 128:
            malformed = True
            continue
        if version is None:
            continue
        if not isinstance(version, str) or not version.strip() or len(version) > 32:
            malformed = True
            continue
        versions[slug.casefold()] = version.strip()
    warning = None
    if malformed:
        warning = "本机 Codex 模型能力输出部分异常，未返回的模型按未知处理；保留离线配置管理。"
    return versions, warning


def _read_runtime_model_capabilities(*, allow_cli: bool = True) -> tuple[dict[str, str] | None, str | None]:
    """Cache the public CLI metadata briefly so one UI action runs at most one CLI process.

    allow_cli=False：只读缓存，不拉起 ``codex debug models``。首屏 get() 用这个，
    避免 CLI 卡 10 秒把整页钉在「读取配置中」。
    """
    global _runtime_model_capability_cache
    now = time.monotonic()
    cached = _runtime_model_capability_cache
    if cached is not None and now - cached[0] < MODEL_CAPABILITY_CACHE_TTL_SECONDS:
        versions, warning = cached[1], cached[2]
        return (dict(versions) if versions is not None else None), warning
    if not allow_cli:
        return None, None
    versions, warning = _query_runtime_model_capabilities()
    stored_versions = dict(versions) if versions is not None else None
    _runtime_model_capability_cache = (now, stored_versions, warning)
    return (dict(stored_versions) if stored_versions is not None else None), warning


def _clear_runtime_model_capability_cache() -> None:
    """Test hook: clear the short-lived capability snapshot without touching Codex state."""
    global _runtime_model_capability_cache
    _runtime_model_capability_cache = None


def _project_model_capabilities(catalog: list[dict[str, Any]], *,
                                allow_cli: bool = True) -> tuple[list[dict[str, Any]], str | None]:
    """Attach ephemeral V2 compatibility fields without modifying the manifest catalogue."""
    versions, warning = _read_runtime_model_capabilities(allow_cli=allow_cli)
    projected: list[dict[str, Any]] = []
    unknown_models: list[str] = []
    for source in catalog:
        entry = dict(source)
        version = versions.get(entry["id"].casefold()) if versions is not None else None
        entry["multi_agent_version"] = version
        entry["v2_subagent_compatible"] = (
            None if version is None else version.casefold() == V2_MULTI_AGENT_VERSION
        )
        if versions is not None and version is None:
            unknown_models.append(entry["id"])
        projected.append(entry)
    if unknown_models:
        shown = "、".join(unknown_models[:5])
        suffix = "" if len(unknown_models) <= 5 else f" 等 {len(unknown_models)} 个"
        missing_warning = (
            f"本机 Codex 未返回 {shown}{suffix} 的模型能力；这些模型按未知处理，保留离线配置管理。"
        )
        warning = f"{warning} {missing_warning}" if warning else missing_warning
    return projected, warning


def _reject_non_v2_subagent_model(model: str, field: str,
                                  versions: dict[str, str] | None) -> None:
    """Reject only a runtime-confirmed non-V2 worker model; unknown stays offline-safe."""
    if versions is None:
        return
    version = versions.get(model.casefold())
    if version is not None and version.casefold() != V2_MULTI_AGENT_VERSION:
        raise AgentConfigError(
            f"{field} {model} 仅支持 multi-agent {version}，不能用于当前 V2 子代理"
        )


def _validate_v2_subagent_models(global_values: dict[str, Any], agent_values: dict[str, Any],
                                 versions: dict[str, str] | None) -> None:
    _reject_non_v2_subagent_model(
        global_values["default_subagent_model"], "默认模型", versions)
    _reject_non_v2_subagent_model(agent_values["model"], "Worker 模型", versions)


def _model_entry(model: str, field: str, catalog: list[dict[str, Any]]) -> dict[str, Any]:
    model_id = _safe_model_id(model, field)
    for entry in catalog:
        if entry["id"].casefold() == model_id.casefold():
            return entry
    raise AgentConfigError(f"{field} 不在模型目录中")


def _safe_model(value: Any, field: str, catalog: list[dict[str, Any]]) -> str:
    return _model_entry(value, field, catalog)["id"]


def _safe_effort(value: Any, field: str, model: str,
                 catalog: list[dict[str, Any]]) -> str:
    if not isinstance(value, str) or value not in ALLOWED_EFFORTS:
        raise AgentConfigError(f"{field} 不是有效推理强度")
    entry = _model_entry(model, "模型", catalog)
    if value not in entry["reasoning_efforts"]:
        raise AgentConfigError(f"{entry['id']} 不支持 {value} 推理强度")
    return value


def _safe_global(value: Any, catalog: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentConfigError("全局默认值格式不正确")
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise AgentConfigError("启用开关必须是布尔值")
    max_threads = value.get("max_concurrency")
    if isinstance(max_threads, bool) or not isinstance(max_threads, int) or not 1 <= max_threads <= 16:
        raise AgentConfigError("最大并发数必须是 1 到 16 的整数")
    model = _safe_model(value.get("default_model"), "默认模型", catalog)
    effort = _safe_effort(value.get("default_reasoning_effort"), "默认推理强度", model, catalog)
    return {
        "enabled": enabled,
        "max_concurrent_threads_per_session": max_threads,
        "default_subagent_model": model,
        "default_subagent_reasoning_effort": effort,
    }


def _safe_primary(value: Any, catalog: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentConfigError("主代理默认值格式不正确")
    model = _safe_model(value.get("model"), "主代理模型", catalog)
    effort = _safe_effort(value.get("model_reasoning_effort"), "主代理推理强度", model, catalog)
    return {"model": model, "model_reasoning_effort": effort}


def _safe_agent(value: Any, catalog: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentConfigError("Luna Worker 配置格式不正确")
    name = value.get("name")
    if name != AGENT_NAME:
        raise AgentConfigError(f"Luna Worker 名称必须保持为 {AGENT_NAME}")
    description = value.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 4000:
        raise AgentConfigError("Luna Worker 使用说明不能为空且长度不能超过 4000")
    model = _safe_model(value.get("model"), "Luna Worker 模型", catalog)
    effort = _safe_effort(value.get("model_reasoning_effort"), "Luna Worker 推理强度", model, catalog)
    sandbox_mode = value.get("sandbox_mode", SAFE_SANDBOX_MODE)
    if sandbox_mode != SAFE_SANDBOX_MODE:
        raise AgentConfigError(f"Luna Worker 沙箱必须保持为 {SAFE_SANDBOX_MODE}")
    instructions = value.get("developer_instructions")
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 40000:
        raise AgentConfigError("Luna Worker 指令不能为空且长度不能超过 40000")
    normalized_instructions = instructions.replace("\r\n", "\n").rstrip("\n")
    if normalized_instructions != SAFE_AGENT_INSTRUCTIONS:
        raise AgentConfigError("Luna Worker 内建安全指令固定不可修改")
    return {
        "name": name,
        "description": description,
        "model": model,
        "model_reasoning_effort": effort,
        "sandbox_mode": sandbox_mode,
        "developer_instructions": SAFE_AGENT_INSTRUCTIONS,
    }


def _toml_value(doc: Any, key: str) -> Any:
    value = doc.get(key)
    if value is None:
        return None
    return value.unwrap() if hasattr(value, "unwrap") else value


def _load_runtime_toml(text: str) -> dict[str, Any]:
    if tomllib is not None:
        return tomllib.loads(text)
    return tomlkit.parse(text).unwrap()


class CodexAgentsManager:
    """Manager with injectable roots for offline tests."""

    def __init__(self, aicodebrain_root: str | Path | None = None,
                 codex_home: str | Path | None = None) -> None:
        if aicodebrain_root is None:
            aicodebrain_root = Path.home() / "AICodebrain"
        if codex_home is None:
            codex_home = Path.home() / ".codex"
        self.aicodebrain_root = Path(aicodebrain_root).expanduser()
        self.codex_home = Path(codex_home).expanduser()
        self.source_root = self.aicodebrain_root / "shared" / "codex-agents"
        self.manifest_path = self.source_root / MANIFEST_NAME
        self.agent_source_path = self.source_root / AGENT_FILENAME
        self.runtime_agent_path = self.codex_home / "agents" / AGENT_FILENAME
        self.runtime_config_path = self.codex_home / "config.toml"
        for path in (self.source_root, self.manifest_path, self.agent_source_path,
                     self.codex_home, self.runtime_agent_path, self.runtime_config_path):
            _assert_safe_path(path)

    def _load_manifest(self) -> tuple[dict[str, Any] | None, str | None]:
        raw = _read_text(self.manifest_path)
        if not raw:
            return None, "母本 manifest.json 不存在"
        try:
            data = json.loads(raw)
        except Exception:
            return None, "母本 manifest.json 无法解析"
        if (data.get("schema_version") != MANIFEST_SCHEMA_VERSION
                or data.get("managed_agents") != [AGENT_FILENAME]
                or not isinstance(data.get("model_catalog"), list)
                or not isinstance(data.get("primary_defaults"), dict)
                or not isinstance(data.get("global_defaults"), dict)):
            return None, "母本 manifest.json 版本或白名单不受支持"
        try:
            catalog = _safe_model_catalog(data["model_catalog"])
            data["model_catalog"] = catalog
            data["primary_defaults"] = _safe_primary(data["primary_defaults"], catalog)
            g = data["global_defaults"]
            global_values = _safe_global({
                "enabled": g.get("enabled"),
                "max_concurrency": g.get("max_concurrent_threads_per_session"),
                "default_model": g.get("default_subagent_model"),
                "default_reasoning_effort": g.get("default_subagent_reasoning_effort"),
            }, catalog)
        except AgentConfigError as exc:
            return None, str(exc)
        data["global_defaults"] = global_values
        return data, None

    def _load_agent_document(self, catalog: list[dict[str, Any]]) -> tuple[Any | None, dict[str, Any] | None, str | None]:
        raw = _read_text(self.agent_source_path)
        if not raw:
            return None, None, "母本 luna-worker.toml 不存在"
        try:
            doc = tomlkit.parse(raw)
            values = _safe_agent({
                "name": _toml_value(doc, "name"),
                "description": _toml_value(doc, "description"),
                "model": _toml_value(doc, "model"),
                "model_reasoning_effort": _toml_value(doc, "model_reasoning_effort"),
                "sandbox_mode": _toml_value(doc, "sandbox_mode"),
                "developer_instructions": _toml_value(doc, "developer_instructions"),
            }, catalog)
        except AgentConfigError as exc:
            return None, None, str(exc)
        except Exception:
            return None, None, "母本 luna-worker.toml 无法解析"
        if not raw.startswith(MANAGED_MARKER):
            return None, None, "母本 luna-worker.toml 缺少 rxyy tools 管理标记"
        return doc, values, None

    def _runtime_state(self, source_primary: dict[str, Any] | None,
                       source_global: dict[str, Any] | None,
                       source_agent: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
        warnings: list[str] = []
        config_exists = self.runtime_config_path.is_file()
        agent_exists = self.runtime_agent_path.is_file()
        runtime_primary: dict[str, Any] | None = None
        runtime_global: dict[str, Any] | None = None
        if config_exists:
            try:
                parsed = _load_runtime_toml(_read_text(self.runtime_config_path))
                runtime_primary = {
                    "model": parsed.get("model"),
                    "model_reasoning_effort": parsed.get("model_reasoning_effort"),
                }
                table = parsed.get("agents") or {}
                runtime_global = {
                    "enabled": table.get("enabled"),
                    "max_concurrent_threads_per_session": table.get("max_concurrent_threads_per_session"),
                    "default_subagent_model": table.get("default_subagent_model"),
                    "default_subagent_reasoning_effort": table.get("default_subagent_reasoning_effort"),
                }
            except Exception:
                warnings.append("Codex config.toml 当前无法解析")
        else:
            warnings.append("Codex config.toml 不存在")
        if not agent_exists:
            warnings.append("Codex 全局 luna-worker.toml 尚未安装")
        elif not _read_text(self.runtime_agent_path).startswith(MANAGED_MARKER):
            warnings.append("Codex 全局 luna-worker.toml 不是 rxyy tools 管理副本")
        primary_match = bool(source_primary and runtime_primary == source_primary)
        global_match = bool(source_global and runtime_global == source_global)
        agent_match = bool(source_agent and agent_exists
                           and _read_bytes(self.runtime_agent_path) == _read_bytes(self.agent_source_path))
        source_exists = bool(source_primary and source_global and source_agent)
        drift = not (source_exists and primary_match and global_match and agent_match)
        if not source_exists:
            state = "missing"
        elif warnings and "当前无法解析" in " ".join(warnings):
            state = "invalid"
        elif drift:
            state = "differs"
        else:
            state = "synced"
        return {
            "state": state,
            "drift": drift,
            "source_exists": source_exists,
            "runtime_config_exists": config_exists,
            "runtime_agent_exists": agent_exists,
            "primary_match": primary_match,
            "global_match": global_match,
            "agent_match": agent_match,
            "warnings": warnings,
            "backup_available": False,
        }, warnings

    def get(self, refresh_capabilities: bool = False) -> dict[str, Any]:
        manifest, manifest_error = self._load_manifest()
        catalog = list(manifest["model_catalog"]) if manifest else _default_model_catalog()
        projected_catalog, capability_warning = _project_model_capabilities(
            catalog, allow_cli=refresh_capabilities)
        if (
            not refresh_capabilities
            and capability_warning is None
            and all(entry.get("multi_agent_version") is None for entry in projected_catalog)
        ):
            capability_warning = PENDING_CAPABILITY_WARNING
        _doc, agent, agent_error = self._load_agent_document(catalog)
        source_primary = manifest["primary_defaults"] if manifest else None
        source_global = manifest["global_defaults"] if manifest else None
        errors = [x for x in (manifest_error, agent_error) if x]
        status, warnings = self._runtime_state(source_primary, source_global, agent)
        warnings = errors + warnings
        if errors:
            status["state"] = "invalid" if self.manifest_path.exists() or self.agent_source_path.exists() else "missing"
            status["drift"] = True
        primary_out = dict(source_primary or DEFAULT_PRIMARY)
        primary_out = {
            "model": str(primary_out.get("model", DEFAULT_PRIMARY["model"])),
            "model_reasoning_effort": str(primary_out.get(
                "model_reasoning_effort", DEFAULT_PRIMARY["model_reasoning_effort"])),
        }
        global_out = dict(source_global or DEFAULT_GLOBAL)
        global_out = {
            "enabled": bool(global_out.get("enabled", True)),
            "max_concurrency": int(global_out.get(
                "max_concurrent_threads_per_session",
                DEFAULT_GLOBAL["max_concurrent_threads_per_session"],
            )),
            "default_model": str(global_out.get("default_subagent_model", DEFAULT_GLOBAL["default_subagent_model"])),
            "default_reasoning_effort": str(global_out.get("default_subagent_reasoning_effort", "max")),
        }
        agent_out = dict(agent or DEFAULT_AGENT)
        agent_out = {
            "name": agent_out.get("name", AGENT_NAME),
            "description": agent_out.get("description", DEFAULT_AGENT["description"]),
            "model": agent_out.get("model", DEFAULT_AGENT["model"]),
            "model_reasoning_effort": agent_out.get("model_reasoning_effort", "max"),
            "sandbox_mode": agent_out.get("sandbox_mode", SAFE_SANDBOX_MODE),
            "developer_instructions": agent_out.get("developer_instructions", DEFAULT_AGENT["developer_instructions"]),
            "installed": self.runtime_agent_path.is_file(),
        }
        status["warnings"] = warnings
        return {
            "ok": True,
            "model_catalog": projected_catalog,
            "model_capability_warning": capability_warning,
            "primary": primary_out,
            "global": global_out,
            "agent": agent_out,
            "status": status,
            "revision": _revision(self.manifest_path, self.agent_source_path),
            "paths": {
                "source_root": str(self.source_root),
                "codex_home": str(self.codex_home),
                "runtime_config": str(self.runtime_config_path),
                "runtime_agent": str(self.runtime_agent_path),
            },
            "recommendation": (
                "需要子代理时不要 spawn_agent（Terra/V2），绕无头 Luna max："
                "codex exec --skip-git-repo-check --approve-for-me -m gpt-5.6-luna -c model_reasoning_effort=max。"
                "--approve-for-me 不能和 -s 一起用；只读探针改用 -s read-only。"
                "Luna 仍标 multi-agent v1，不能配给 V2 spawn_agent。"
                "主代理 Sol 负责拆解与验收。"
            ),
        }

    def _source_manifest_text(self, model_catalog: list[dict[str, Any]],
                              primary_values: dict[str, Any],
                              global_values: dict[str, Any]) -> str:
        current = {}
        raw = _read_text(self.manifest_path)
        if raw:
            try:
                current = json.loads(raw)
            except Exception:
                current = {}
        current["schema_version"] = MANIFEST_SCHEMA_VERSION
        current["managed_agents"] = [AGENT_FILENAME]
        current["model_catalog"] = model_catalog
        current["primary_defaults"] = primary_values
        current["global_defaults"] = global_values
        return json.dumps(current, ensure_ascii=False, indent=2) + "\n"

    def _source_agent_text(self, agent_values: dict[str, Any], restore: bool = False) -> str:
        raw = "" if restore else _read_text(self.agent_source_path)
        if raw:
            try:
                doc = tomlkit.parse(raw)
            except Exception as exc:
                raise AgentConfigError("母本 luna-worker.toml 无法解析，无法保留未展示配置") from exc
        else:
            doc = tomlkit.document()
        for key in ("name", "description", "model", "model_reasoning_effort", "sandbox_mode"):
            doc[key] = agent_values[key]
        doc["developer_instructions"] = tomlkit.string(
            agent_values["developer_instructions"], multiline=True)
        rendered = tomlkit.dumps(doc)
        if not rendered.startswith(MANAGED_MARKER):
            rendered = MANAGED_MARKER + "\n" + rendered
        return rendered

    def _sync_subprocess(self) -> dict[str, Any]:
        script = self.aicodebrain_root / "scripts" / SYNC_SCRIPT_NAME
        if not script.is_file():
            raise AgentConfigError("找不到 AICodebrain Codex Agents 同步脚本")
        _assert_safe_path(self.aicodebrain_root)
        _assert_safe_path(script)
        try:
            trusted_root = self.aicodebrain_root.resolve(strict=True)
            resolved_script = script.resolve(strict=True)
        except OSError as exc:
            raise AgentConfigError("AICodebrain 同步脚本路径无效") from exc
        if resolved_script != trusted_root / "scripts" / SYNC_SCRIPT_NAME:
            raise AgentConfigError("AICodebrain 同步脚本路径不受信任")
        if not _read_text(resolved_script).startswith(SYNC_SCRIPT_MARKER + "\n"):
            raise AgentConfigError("AICodebrain 同步脚本缺少管理标记")
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            raise AgentConfigError("找不到 PowerShell，无法同步 Codex 配置")
        try:
            result = subprocess.run(
                [powershell, "-NoProfile", "-File", str(resolved_script),
                 "-CodexHome", str(self.codex_home), "-ConfigureDefaults"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentConfigError("Codex 配置同步未完成") from exc
        if result.returncode != 0:
            raise AgentConfigError("Codex 配置同步失败，源文件未被删除")
        return {"ok": True}

    def sync(self) -> dict[str, Any]:
        before = self.get()
        if before["status"]["warnings"] and any("母本" in w for w in before["status"]["warnings"]):
            raise AgentConfigError("母本配置无效，先恢复推荐配置或修正母本")
        versions, _capability_warning = _read_runtime_model_capabilities()
        _validate_v2_subagent_models({
            "default_subagent_model": before["global"]["default_model"],
        }, {"model": before["agent"]["model"]}, versions)
        self._sync_subprocess()
        after = self.get()
        return {"ok": True, "changed": bool(before["status"]["drift"]),
                "message": "已同步到 Codex" if before["status"]["drift"] else "Codex 已是最新",
                "status": after["status"], "revision": after["revision"],
                "model_capability_warning": after["model_capability_warning"]}

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AgentConfigError("保存参数格式不正确")
        current_revision = _revision(self.manifest_path, self.agent_source_path)
        expected = payload.get("revision")
        if expected and expected != current_revision:
            raise AgentConfigConflict("母本已被外部修改，请刷新后再保存")
        current_manifest, manifest_error = self._load_manifest()
        if "model_catalog" in payload:
            model_catalog = _safe_model_catalog(payload.get("model_catalog"))
        elif current_manifest:
            model_catalog = list(current_manifest["model_catalog"])
        elif self.manifest_path.exists() and manifest_error:
            raise AgentConfigError(manifest_error)
        else:
            model_catalog = _default_model_catalog()
        primary_values = _safe_primary(payload.get("primary"), model_catalog)
        global_values = _safe_global(payload.get("global"), model_catalog)
        agent_values = _safe_agent(payload.get("agent"), model_catalog)
        versions, _capability_warning = _read_runtime_model_capabilities()
        _validate_v2_subagent_models(global_values, agent_values, versions)
        manifest_text = self._source_manifest_text(model_catalog, primary_values, global_values)
        agent_text = self._source_agent_text(agent_values)
        old_manifest = _read_bytes(self.manifest_path)
        old_agent = _read_bytes(self.agent_source_path)
        source_changed = old_manifest != manifest_text.encode("utf-8") or old_agent != agent_text.encode("utf-8")
        before = self.get()
        try:
            _atomic_write(self.manifest_path, manifest_text)
            _atomic_write(self.agent_source_path, agent_text)
            self._sync_subprocess()
        except Exception:
            try:
                if old_manifest is None:
                    self.manifest_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.manifest_path, old_manifest.decode("utf-8"))
                if old_agent is None:
                    self.agent_source_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.agent_source_path, old_agent.decode("utf-8"))
            except Exception:
                pass
            raise
        after = self.get()
        return {"ok": True, "changed": bool(source_changed or before["status"]["drift"]),
                "message": "已保存并同步到 Codex", "status": after["status"],
                "revision": after["revision"],
                "model_capability_warning": after["model_capability_warning"]}

    def restore(self) -> dict[str, Any]:
        versions, _capability_warning = _read_runtime_model_capabilities()
        _validate_v2_subagent_models(DEFAULT_GLOBAL, DEFAULT_AGENT, versions)
        manifest_text = json.dumps({
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "managed_agents": [AGENT_FILENAME],
            "model_catalog": _default_model_catalog(),
            "primary_defaults": DEFAULT_PRIMARY,
            "global_defaults": DEFAULT_GLOBAL,
        }, ensure_ascii=False, indent=2) + "\n"
        agent_text = self._source_agent_text(DEFAULT_AGENT, restore=True)
        old_manifest = _read_bytes(self.manifest_path)
        old_agent = _read_bytes(self.agent_source_path)
        try:
            _atomic_write(self.manifest_path, manifest_text)
            _atomic_write(self.agent_source_path, agent_text)
            self._sync_subprocess()
        except Exception:
            try:
                if old_manifest is None:
                    self.manifest_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.manifest_path, old_manifest.decode("utf-8"))
                if old_agent is None:
                    self.agent_source_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.agent_source_path, old_agent.decode("utf-8"))
            except Exception:
                pass
            raise
        after = self.get()
        return {"ok": True, "changed": True, "message": "已恢复推荐配置并同步到 Codex",
                "status": after["status"], "revision": after["revision"],
                "model_capability_warning": after["model_capability_warning"]}
