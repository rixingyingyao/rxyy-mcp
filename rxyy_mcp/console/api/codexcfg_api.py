r"""Codex 配置中心：把散在四处的「装扮」配置收进控制台一页。

**为什么要有这一页。** Codex 的主题、宠物、灵动岛各是各的：主题归 Dream Skin 插件
（状态在 `%LOCALAPPDATA%\CodexDreamSkin`，换主题要去主题目录跑一个 ps1），宠物是
`~/.codex/pets/<id>/` 一堆目录（灵动岛用哪个记在它自己的配置里），灵动岛的位置与
开机自启又分别落在 `%LOCALAPPDATA%\CodexIsland\config.json` 和启动文件夹的快捷方式。
想知道「现在用的是哪套」得翻四个地方，换一次要开三个窗口。

**边界（重要）。** 这里只碰这三样自己的配置文件，不动 Codex 本体、不动 `config.toml`、
不动任何凭证。换主题仍调主题目录里现成的 `apply-art-theme.ps1`（Dream Skin 官方
脚本的封装）。主题编辑只写白名单目录里的 `theme.json`、`theme.css` 和它原本登记的
背景图片，CSS 必须先通过已安装 Dream Skin 的官方 Safe CSS 校验器；每次保存前会把
源文件快照放进 rxyy tools 数据目录，随时可恢复上一版。

方法：
- codexcfg_status()                 一次读齐：主题 / 宠物 / 灵动岛 / 看板
- codexcfg_set_island_pet(pet_id)   换灵动岛头像用的宠物
- codexcfg_set_island_autostart(on) 开机自启开关（改启动文件夹快捷方式的后缀，不删文件）
- codexcfg_theme_editor(theme_id)   读取完整主题元数据与 Safe CSS 可调项
- codexcfg_save_theme(...)          校验、保存，并可继续调用官方脚本应用
- codexcfg_apply_theme(theme_id)    应用一个主题（只允许白名单里扫到的主题目录）
- codexcfg_skin_compat()            检查/重打 Codex 26.803 皮肤锚点补丁
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from api.dreamskin_compat import (
    EXTRA_PARTS,
    apply_all as _compat_apply_all,
    inspect_all as _compat_inspect_all,
    touch_active_theme as _compat_touch_active_theme,
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DREAMSKIN_STATE = Path(os.environ.get("LOCALAPPDATA", "")) / "CodexDreamSkin"
ISLAND_STATE = Path(os.environ.get("LOCALAPPDATA", "")) / "CodexIsland"
CODEX_HOME = Path.home() / ".codex"
STARTUP_DIR = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup")
ISLAND_LNK = "Codex Island.lnk"
DREAMSKIN_LNK = "Codex Dream Skin.lnk"
DREAMSKIN_AUTOSTART_SCRIPT = (Path(os.environ.get("LOCALAPPDATA", "")) / "rxyy-tools"
                              / "scripts" / "codex-dream-skin-autostart.ps1")
# 主题目录长这样才认：一个 theme.json + 一个能一键应用的 ps1
THEME_MARKERS = ("theme.json", "apply-art-theme.ps1")

THEME_TEXT_FIELDS = (
    "brandSubtitle", "tagline", "projectPrefix", "projectLabel", "statusText",
    "quote", "promoTitle", "promoSub", "promoUrl",
)
THEME_COLOR_FIELDS = (
    "background", "panel", "panelAlt", "accent", "accentAlt", "secondary",
    "highlight", "text", "muted", "line",
)
THEME_APPEARANCES = ("auto", "light", "dark")
THEME_SAFE_AREAS = ("left", "right", "none")
THEME_TASK_MODES = ("ambient", "full", "off")
THEME_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
THEME_COLOR_PATTERN = re.compile(
    r"^(?:#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?|#[0-9a-fA-F]{3,4}|"
    r"rgb\(\s*[0-9]{1,3}\s*,\s*[0-9]{1,3}\s*,\s*[0-9]{1,3}\s*\)|"
    r"rgba\(\s*[0-9]{1,3}\s*,\s*[0-9]{1,3}\s*,\s*[0-9]{1,3}\s*,"
    r"\s*(?:0|1|1\.0|0?\.[0-9]{1,6})\s*\))$"
)
THEME_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
THEME_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
THEME_IMAGE_MAX_BYTES = 10 * 1024 * 1024

SAFE_CSS_FALLBACK = {
    "contract": "dreamskin-safe-css/1",
    "parts": [
        "root", "sidebar", "main", "header", "home", "home-hero",
        "project-list", "thread", "message", "composer", "composer-toolbar",
        "home-utility", "composer-shell", "dialog",
    ],
    "states": ["hover", "focus-visible"],
    "variables": [],
    "properties": [
        "backdrop-filter", "background-color", "border-bottom-color",
        "border-bottom-left-radius", "border-bottom-right-radius",
        "border-bottom-style", "border-bottom-width", "border-color",
        "border-left-color", "border-left-style", "border-left-width",
        "border-radius", "border-right-color", "border-right-style",
        "border-right-width", "border-style", "border-top-color",
        "border-top-left-radius", "border-top-right-radius", "border-top-style",
        "border-top-width", "border-width", "box-shadow", "color", "column-gap",
        "font-family", "font-size", "font-weight", "gap", "letter-spacing",
        "line-height", "opacity", "row-gap", "transition-duration",
        "transition-property",
    ],
}


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.tmp.{}".format(path.name, os.getpid()))
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=4),
                             encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.tmp.{}".format(path.name, os.getpid()))
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _dreamskin_autostart_payload() -> str:
    """登录启动器：托盘先常驻，随后无交互地恢复已选主题引擎。

    登录这一刻是全机最堵的时候（一堆启动项同时抢盘），而 Codex 是 Electron 应用、
    冷启动慢。原来这里是「睡 8 秒 → 打一发 → 不成就 exit 1」，任何一次没赶上，
    整个登录周期都没有主题，日志还只留一句 exited with code N，什么线索都不给。
    所以：给足 CDP 等待预算、失败重试、并把启动器真正的报错抄进日志。

    **脚本正文必须全 ASCII**。它由 `powershell.exe -File` 执行，而 Windows PowerShell
    5.1 读没有 BOM 的脚本时按 ANSI(GBK) 解析：正文里只要有一个中文字符，字符串就会
    被拆坏、整个脚本报 "The string is missing the terminator"。中文解释写在这儿，
    不要写进 payload。
    """
    return r'''# rxyy-tools-managed: dreamskin-autostart-v2
[CmdletBinding()]
param(
  [ValidateRange(0, 120)][int]$DelaySeconds = 8,
  [ValidateRange(1, 5)][int]$MaxAttempts = 2,
  [ValidateRange(0, 600)][int]$RetryWaitSeconds = 60,
  [ValidateRange(15, 600)][int]$CdpWaitSeconds = 150
)

$ErrorActionPreference = 'Stop'
$local = $env:LOCALAPPDATA
$stateRoot = Join-Path $local 'CodexDreamSkin'
$logDir = Join-Path $local 'rxyy-tools\logs'
$logPath = Join-Path $logDir 'dreamskin-autostart.log'

function Write-AutostartLog {
  param([string]$Level, [string]$Message)
  try {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    Add-Content -LiteralPath $logPath -Value (
      '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message)
  } catch { }
}

try {
  $scriptRoots = @(
    (Join-Path $local 'Programs\CodexDreamSkin\payload\scripts'),
    (Join-Path $stateRoot 'engine\scripts')
  )
  $root = $scriptRoots | Where-Object {
    (Test-Path -LiteralPath (Join-Path $_ 'start-dream-skin.ps1') -PathType Leaf) -and
    (Test-Path -LiteralPath (Join-Path $_ 'tray-dream-skin.ps1') -PathType Leaf)
  } | Select-Object -First 1
  if ([string]::IsNullOrWhiteSpace($root)) { throw 'Dream Skin scripts are not installed.' }

  $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
  $tray = Join-Path $root 'tray-dream-skin.ps1'
  $start = Join-Path $root 'start-dream-skin.ps1'
  $trayArgs = '-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $tray + '"'
  Start-Process -FilePath $powershell -ArgumentList $trayArgs -WindowStyle Hidden | Out-Null

  if ((Test-Path -LiteralPath (Join-Path $stateRoot 'paused')) -or
      -not (Test-Path -LiteralPath (Join-Path $stateRoot 'active-theme') -PathType Container)) {
    exit 0
  }
  if ($DelaySeconds -gt 0) { Start-Sleep -Seconds $DelaySeconds }

  # start-dream-skin.ps1 only waits 45s for the CDP endpoint by default, which Codex
  # rarely makes during the login rush. That script exposes this variable as the escape
  # hatch; the login path must use it. Never override a value the user already set.
  if (-not $env:DREAM_SKIN_CDP_WAIT_SECONDS) {
    $env:DREAM_SKIN_CDP_WAIT_SECONDS = [string]$CdpWaitSeconds
  }

  New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  $outPath = Join-Path $logDir 'dreamskin-autostart.last-run.log'
  $errPath = Join-Path $logDir 'dreamskin-autostart.last-run.err.log'
  $startArgs = @('-NoProfile', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass',
    '-File', $start, '-RestartExisting', '-RequireUnpaused',
    '-OperationLockTimeoutMilliseconds', '30000')

  $applied = $false
  $lastCode = -1
  for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
    if ($attempt -gt 1 -and $RetryWaitSeconds -gt 0) { Start-Sleep -Seconds $RetryWaitSeconds }
    $proc = Start-Process -FilePath $powershell -ArgumentList $startArgs -WindowStyle Hidden `
      -Wait -PassThru -RedirectStandardOutput $outPath -RedirectStandardError $errPath
    $lastCode = $proc.ExitCode
    if ($lastCode -eq 0) {
      $applied = $true
      if ($attempt -gt 1) {
        Write-AutostartLog 'INFO' ('Theme applied on attempt {0}.' -f $attempt)
      }
      break
    }
    # An exit code alone says nothing. Copy what the starter actually shouted, so the
    # next boot does not need a dig through three separate Dream Skin logs.
    $why = ''
    try {
      $tail = Get-Content -LiteralPath $errPath -Tail 4 -ErrorAction SilentlyContinue
      if ($tail) { $why = (($tail -join ' | ') -replace '\s+', ' ').Trim() }
    } catch { }
    if (-not $why) { $why = 'starter produced no output' }
    Write-AutostartLog 'WARN' ('Attempt {0}/{1} to apply the theme failed (exit {2}): {3}' -f `
        $attempt, $MaxAttempts, $lastCode, $why)
  }
  if (-not $applied) {
    throw ('Gave up after {0} attempts to inject the theme into Codex (last exit {1}). Starter output: {2} ; injector output: {3}' -f `
        $MaxAttempts, $lastCode, $errPath, (Join-Path $stateRoot 'verify.log'))
  }
} catch {
  Write-AutostartLog 'ERROR' $_.Exception.Message
  exit 1
}
'''


def _create_dreamskin_autostart_shortcut(script_path: Path) -> None:
    """用环境变量传路径，避免把本机路径拼进 PowerShell 代码。"""
    shortcut = STARTUP_DIR / DREAMSKIN_LNK
    env = os.environ.copy()
    env.update({
        "RXYY_DREAMSKIN_AUTOSTART": str(script_path),
        "RXYY_DREAMSKIN_SHORTCUT": str(shortcut),
        "RXYY_DREAMSKIN_WORKDIR": str(script_path.parent),
    })
    command = (
        "$ErrorActionPreference='Stop';"
        "$shell=New-Object -ComObject WScript.Shell;"
        "$shortcut=$shell.CreateShortcut($env:RXYY_DREAMSKIN_SHORTCUT);"
        "$shortcut.TargetPath=(Get-Command powershell.exe -ErrorAction Stop).Source;"
        "$shortcut.Arguments='-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass "
        "-File \"'+$env:RXYY_DREAMSKIN_AUTOSTART+'\"';"
        "$shortcut.WorkingDirectory=$env:RXYY_DREAMSKIN_WORKDIR;"
        "$shortcut.Description='Start Codex Dream Skin theme engine and tray';"
        "$shortcut.Save()"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, timeout=30, creationflags=_NO_WINDOW, env=env,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        raise OSError(detail[-400:] or "创建 Dream Skin 开机快捷方式失败")


def _ensure_dreamskin_autostart() -> dict[str, Any]:
    """把已启用的旧托盘启动项幂等升级为“托盘 + 主题引擎”。"""
    shortcut = STARTUP_DIR / DREAMSKIN_LNK
    if not shortcut.is_file():
        return {"enabled": False, "mode": "off", "changed": False,
                "shortcut": str(shortcut)}
    payload = _dreamskin_autostart_payload()
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    state_path = DREAMSKIN_AUTOSTART_SCRIPT.with_suffix(".json")
    state = _read_json(state_path, {}) or {}
    try:
        script_current = (DREAMSKIN_AUTOSTART_SCRIPT.is_file()
                          and DREAMSKIN_AUTOSTART_SCRIPT.read_text(encoding="utf-8") == payload)
        shortcut_mtime = shortcut.stat().st_mtime_ns
        managed = (script_current and state.get("payload_sha256") == digest
                   and state.get("shortcut_mtime_ns") == shortcut_mtime)
        if managed:
            return {"enabled": True, "mode": "managed", "changed": False,
                    "shortcut": str(shortcut), "script": str(DREAMSKIN_AUTOSTART_SCRIPT)}
        _write_text_atomic(DREAMSKIN_AUTOSTART_SCRIPT, payload)
        _create_dreamskin_autostart_shortcut(DREAMSKIN_AUTOSTART_SCRIPT)
        _write_json_atomic(state_path, {
            "schema": 1,
            "payload_sha256": digest,
            "shortcut_mtime_ns": shortcut.stat().st_mtime_ns,
        })
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"enabled": True, "mode": "legacy", "changed": False,
                "shortcut": str(shortcut), "error": "升级主题开机启动失败：%r" % exc}
    return {"enabled": True, "mode": "managed", "changed": True,
            "shortcut": str(shortcut), "script": str(DREAMSKIN_AUTOSTART_SCRIPT)}


def _theme_roots() -> list[Path]:
    """去哪儿找可选主题：环境变量指定的，或桌面根下的 codex_dream。"""
    roots = []
    raw = (os.environ.get("RXYY_CODEX_DREAM") or "").strip()
    if raw:
        roots.append(Path(raw))
    for base in (Path(r"D:\桌面\working"), Path(r"D:\Desktop"), Path.home() / "Desktop"):
        roots.append(base / "codex_dream")
    return [r for r in roots if r.is_dir()]


def _scan_themes() -> list[dict]:
    """扫出所有「能一键应用」的主题目录（含它自己的 theme.json 元信息）。"""
    found: list[dict] = []
    seen: set[str] = set()
    for root in _theme_roots():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if not all((child / marker).is_file() for marker in THEME_MARKERS):
                continue
            meta = _read_json(child / "theme.json", {}) or {}
            # 家机上 D:\Desktop 是 D:\桌面\working 的 Junction，同一个目录会被扫到两遍；
            # 按解析后的真实路径去重，否则主题库里每套都出现两次
            try:
                key = str(child.resolve()).lower()
            except OSError:
                key = str(child).lower()
            if key in seen:
                continue
            seen.add(key)
            preview = next((str(child / name) for name in
                            ("background.jpg", "background.png", "preview.jpg")
                            if (child / name).is_file()), "")
            found.append({
                "id": child.name,
                "name": str(meta.get("name") or child.name),
                "dir": str(child),
                "preview": preview,
                "updated_at": child.stat().st_mtime,
            })
    return found


def _applied_at(active_dir: Path) -> float:
    """「上次应用」是什么时候。

    别拿 theme.css 的 mtime 当答案：应用脚本是拷过去的，Copy-Item 连原文件的时间戳
    一起搬，于是页面上显示的是**主题文件自己被编辑的时间**——08-13 21:12 刚点完应用，
    页面却写着 08-12 22:26（rxyy 当场发现）。落地时真正会变的是那张换名的原画
    `art-<yyyyMMdd>-<HHmmss>-...jpg`，文件名里就带着应用时刻；解析不出来再退回
    「目录里最新的那个 mtime」。
    """
    if not active_dir.is_dir():
        return 0.0
    newest = 0.0
    for child in active_dir.iterdir():
        if not child.is_file():
            continue
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
        if child.name.startswith("art-"):
            parts = child.stem.split("-")
            if len(parts) >= 3 and len(parts[1]) == 8 and len(parts[2]) == 6:
                try:
                    stamp = time.mktime(time.strptime(parts[1] + parts[2], "%Y%m%d%H%M%S"))
                    return stamp
                except (ValueError, OverflowError):
                    pass
    return newest


def _powershell_apply_error(err: str, out: str) -> str:
    """PowerShell 会把 node stderr 包成 NativeCommandError，页面上看不出真因。"""
    blob = "\n".join(part for part in (err, out) if part)
    if not blob:
        return "脚本没报错也没说成功"
    match = re.search(r"SafeCssValidationError:\s*([^\r\n]+)", blob)
    if match:
        return "主题 CSS 没过官方校验：" + match.group(1).strip()
    if "selector/unsupported" in blob:
        return ("主题 CSS 用了官方校验器还不认的部件。"
                "点「重新补丁」后再试一次「启动并应用」。")
    if "NativeCommandError" in blob:
        return "Dream Skin 脚本失败：" + blob[-400:]
    return blob[-600:]


def _dreamskin_start_script() -> Path:
    """Resolve the installed launcher first; the state copy is a recovery fallback."""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = (
        local / "Programs" / "CodexDreamSkin" / "payload" / "scripts"
        / "start-dream-skin.ps1",
        DREAMSKIN_STATE / "engine" / "scripts" / "start-dream-skin.ps1",
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def _theme_target(theme_id: str) -> dict[str, Any] | None:
    wanted = str(theme_id or "").strip()
    target = next((theme for theme in _scan_themes() if theme["id"] == wanted), None)
    if target is None:
        return None
    try:
        resolved = Path(target["dir"]).resolve(strict=True)
        allowed_parents = {root.resolve(strict=True) for root in _theme_roots()}
    except OSError:
        return None
    return target if resolved.parent in allowed_parents else None


def _dreamskin_engine_roots() -> list[Path]:
    roots = [DREAMSKIN_STATE / "engine", _dreamskin_start_script().parent.parent]
    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve()).lower()
        except OSError:
            key = str(root).lower()
        if key not in seen:
            seen.add(key)
            out.append(root)
    return out


def _dreamskin_engine_file(*relative: str) -> Path:
    candidates = [root.joinpath(*relative) for root in _dreamskin_engine_roots()]
    return next((path for path in candidates if path.is_file()), candidates[0])


def _skin_compat(apply: bool = False, repair_if_missing: bool = False) -> dict[str, Any]:
    roots = _dreamskin_engine_roots()
    auto = False
    if not apply and repair_if_missing:
        report = _compat_inspect_all(roots)
        if report.get("skipped") or report.get("ok"):
            return report
        apply = True
        auto = True
    if apply:
        report = _compat_apply_all(roots)
        if report.get("patched"):
            report["touched_active_theme"] = _compat_touch_active_theme()
        if auto:
            report["auto_repaired"] = bool(report.get("patched"))
        return report
    return _compat_inspect_all(roots)


def _safe_css_policy() -> dict[str, Any]:
    raw = _read_json(_dreamskin_engine_file("assets", "safe-css-policy.json"), {}) or {}
    policy: dict[str, Any] = {"contract": str(raw.get("contract") or
                                                  SAFE_CSS_FALLBACK["contract"])}
    for key in ("parts", "states", "variables", "properties"):
        value = raw.get(key)
        fallback = SAFE_CSS_FALLBACK[key]
        policy[key] = ([str(item) for item in value]
                       if isinstance(value, list) and all(isinstance(item, str) for item in value)
                       else list(fallback))
    parts = list(policy["parts"])
    insert_at = (parts.index("composer-toolbar") + 1
                 if "composer-toolbar" in parts else len(parts))
    for part in EXTRA_PARTS:
        if part not in parts:
            parts.insert(insert_at, part)
            insert_at += 1
    policy["parts"] = parts
    return policy


def _theme_image_path(theme_dir: Path, meta: dict[str, Any]) -> Path:
    name = str(meta.get("image") or "").strip()
    if (not name or Path(name).name != name or
            Path(name).suffix.lower() not in THEME_IMAGE_SUFFIXES):
        raise ValueError("theme.json 里的背景图片名不合法")
    path = theme_dir / name
    if not path.is_file():
        raise ValueError("主题登记的背景图片不存在：{}".format(name))
    return path


def _theme_revision(theme_dir: Path) -> str:
    meta = _read_json(theme_dir / "theme.json", {}) or {}
    image = _theme_image_path(theme_dir, meta)
    digest = hashlib.sha256()
    for path in (theme_dir / "theme.json", theme_dir / "theme.css", image):
        if not path.is_file():
            raise ValueError("主题文件不完整：{}".format(path.name))
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_theme_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("{} 必须是文字".format(field))
    if THEME_CONTROL_PATTERN.search(value) or len(value) > maximum:
        raise ValueError("{} 包含控制字符或超过 {} 个字符".format(field, maximum))
    return value.strip()


def _validate_theme_meta(draft: Any, original: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(draft, dict):
        raise ValueError("主题参数格式不正确")
    allowed = {"schemaVersion", "id", "name", "image", "appearance", "art", "colors",
               *THEME_TEXT_FIELDS}
    unknown = sorted(set(draft) - allowed)
    if unknown:
        raise ValueError("主题包含不支持的字段：{}".format("、".join(unknown)))
    if draft.get("schemaVersion") != 1:
        raise ValueError("主题 schemaVersion 必须是 1")
    theme_id = str(draft.get("id") or "")
    if (theme_id != str(original.get("id") or "") or not THEME_ID_PATTERN.fullmatch(theme_id)
            or not 3 <= len(theme_id) <= 64):
        raise ValueError("主题 ID 不能修改，且必须符合 Dream Skin 规则")
    image = str(draft.get("image") or "")
    if image != str(original.get("image") or ""):
        raise ValueError("背景文件名由主题包维护；请使用“更换背景图”替换图片内容")
    name = _validate_theme_text(draft.get("name"), "主题名称", 80)
    if not name:
        raise ValueError("主题名称不能为空")
    appearance = str(draft.get("appearance") or "auto")
    if appearance not in THEME_APPEARANCES:
        raise ValueError("明暗模式只能是 auto / light / dark")

    art = draft.get("art")
    if not isinstance(art, dict) or set(art) - {"focusX", "focusY", "safeArea", "taskMode"}:
        raise ValueError("画面参数格式不正确")
    clean_art: dict[str, Any] = {}
    for field in ("focusX", "focusY"):
        value = art.get(field, 0.5)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("{} 必须是 0 到 1 的数字".format(field))
        number = float(value)
        if not 0 <= number <= 1:
            raise ValueError("{} 必须在 0 到 1 之间".format(field))
        clean_art[field] = number
    safe_area = str(art.get("safeArea") or "none")
    task_mode = str(art.get("taskMode") or "ambient")
    if safe_area not in THEME_SAFE_AREAS:
        raise ValueError("安全留白只能是 left / right / none")
    if task_mode not in THEME_TASK_MODES:
        raise ValueError("任务页背景只能是 ambient / full / off")
    clean_art.update({"safeArea": safe_area, "taskMode": task_mode})

    colors = draft.get("colors")
    if not isinstance(colors, dict) or set(colors) != set(THEME_COLOR_FIELDS):
        raise ValueError("主题必须完整提供 10 个官方颜色字段")
    clean_colors: dict[str, str] = {}
    for field in THEME_COLOR_FIELDS:
        value = str(colors.get(field) or "").strip()
        if len(value) > 64 or not THEME_COLOR_PATTERN.fullmatch(value):
            raise ValueError("颜色 {} 不是 Dream Skin 支持的颜色格式".format(field))
        clean_colors[field] = value

    clean: dict[str, Any] = {
        "schemaVersion": 1,
        "id": theme_id,
        "name": name,
        "image": image,
        "appearance": appearance,
        "art": clean_art,
        "colors": clean_colors,
    }
    for field in THEME_TEXT_FIELDS:
        value = _validate_theme_text(draft.get(field, ""), field,
                                     512 if field == "promoUrl" else 120)
        if value:
            clean[field] = value
    return clean


def _parse_safe_css(source: str) -> list[dict[str, Any]]:
    """Parse the deliberately tiny Safe CSS grammar for the visual form.

    Dream Skin's Node validator remains the security authority.  Its grammar bans comments,
    strings, at-rules and nested blocks, so a small parser can faithfully expose every rule
    without accepting general browser CSS.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError("theme.css 不能为空")
    rule_pattern = re.compile(
        r'\[data-ds-part="([a-z]+(?:-[a-z]+)*)"\](?::(hover|focus-visible))?\s*\{([^{}]*)\}',
        re.DOTALL,
    )
    rules: list[dict[str, Any]] = []
    cursor = 0
    seen_rules: set[tuple[str, str]] = set()
    for match in rule_pattern.finditer(source):
        if source[cursor:match.start()].strip():
            raise ValueError("theme.css 含有表单编辑器不能识别的规则")
        part, state, body = match.group(1), match.group(2) or "", match.group(3)
        key = (part, state)
        if key in seen_rules:
            raise ValueError("theme.css 重复定义了 {} {}".format(part, state or "默认状态"))
        seen_rules.add(key)
        declarations: list[dict[str, str]] = []
        seen_properties: set[str] = set()
        for raw in body.split(";"):
            raw = raw.strip()
            if not raw:
                continue
            if ":" not in raw:
                raise ValueError("theme.css 有一条样式缺少冒号")
            prop, value = (item.strip() for item in raw.split(":", 1))
            if not re.fullmatch(r"[a-z][a-z-]*", prop) or not value:
                raise ValueError("theme.css 样式格式不正确")
            if prop in seen_properties:
                raise ValueError("theme.css 在同一规则里重复了 {}".format(prop))
            seen_properties.add(prop)
            declarations.append({"property": prop, "value": value})
        if not declarations:
            raise ValueError("theme.css 不能包含空规则")
        rules.append({"part": part, "state": state, "declarations": declarations})
        cursor = match.end()
    if source[cursor:].strip() or not rules:
        raise ValueError("theme.css 含有表单编辑器不能识别的内容")
    return rules


def _validate_safe_css(source: str) -> dict[str, Any]:
    if not isinstance(source, str) or not 0 < len(source.encode("utf-8")) <= 256 * 1024:
        raise ValueError("theme.css 必须是 1 到 262144 字节的 UTF-8 文本")
    validator = _dreamskin_engine_file("scripts", "validate-safe-css-file.mjs")
    node = _dreamskin_engine_file("runtime", "node", "node.exe")
    if not validator.is_file() or not node.is_file():
        raise ValueError("找不到 Dream Skin 官方 Safe CSS 校验器，已取消保存")
    with tempfile.TemporaryDirectory(prefix="rxyy-theme-css-") as temporary:
        css_path = Path(temporary) / "theme.css"
        css_path.write_text(source, encoding="utf-8")
        try:
            checked = subprocess.run(
                [str(node), str(validator), str(css_path)], capture_output=True,
                timeout=20, creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Dream Skin CSS 校验器运行失败：{}".format(exc)) from exc
    out = checked.stdout.decode("utf-8", "replace").strip()
    err = checked.stderr.decode("utf-8", "replace").strip()
    if checked.returncode != 0:
        raise ValueError((err or out or "Dream Skin 拒绝了这份 CSS")[-600:])
    try:
        result = json.loads(out)
    except (TypeError, ValueError) as exc:
        raise ValueError("Dream Skin CSS 校验器返回了无法识别的结果") from exc
    if result.get("status") != "validated":
        raise ValueError("Dream Skin 没有确认这份 CSS 安全")
    return result


def _prepare_background_bytes(source: Path, destination: Path) -> bytes:
    if (not source.is_file() or source.stat().st_size < 1 or
            source.stat().st_size > THEME_IMAGE_MAX_BYTES or
            source.suffix.lower() not in THEME_IMAGE_SUFFIXES):
        raise ValueError("背景图必须是 10 MB 以内的 JPG、PNG 或 WebP")
    try:
        from PIL import Image, ImageOps
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            output = io.BytesIO()
            suffix = destination.suffix.lower()
            if suffix in {".jpg", ".jpeg"}:
                image.convert("RGB").save(output, format="JPEG", quality=95,
                                          optimize=True, progressive=True)
            elif suffix == ".png":
                image.save(output, format="PNG", optimize=True)
            elif suffix == ".webp":
                image.save(output, format="WEBP", quality=95, method=6)
            else:
                raise ValueError("主题登记的背景格式不受支持")
    except (OSError, ValueError) as exc:
        raise ValueError("背景图片读不出来：{}".format(exc)) from exc
    data = output.getvalue()
    if not 0 < len(data) <= THEME_IMAGE_MAX_BYTES:
        raise ValueError("转换后的背景图超过 10 MB")
    return data


def _theme_history_dir(theme_dir: Path) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", theme_dir.name).strip("-.") or "theme"
    fingerprint = hashlib.sha256(str(theme_dir.resolve()).lower().encode("utf-8")).hexdigest()[:12]
    return _backup_dir() / "theme-edits" / (safe_name[:48] + "-" + fingerprint)


def _theme_history(theme_dir: Path) -> list[Path]:
    root = _theme_history_dir(theme_dir)
    return sorted(root.glob("theme-edit-*.zip"), reverse=True) if root.is_dir() else []


def _snapshot_theme_source(theme_dir: Path, meta: dict[str, Any]) -> Path:
    image = _theme_image_path(theme_dir, meta)
    out_dir = _theme_history_dir(theme_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = out_dir / ("theme-edit-{}.zip".format(stamp))
    manifest = {
        "schemaVersion": 1,
        "createdAt": time.time(),
        "sourceDir": str(theme_dir.resolve()),
        "revision": _theme_revision(theme_dir),
        "image": image.name,
    }
    with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        bundle.write(theme_dir / "theme.json", "theme.json")
        bundle.write(theme_dir / "theme.css", "theme.css")
        bundle.write(image, image.name)
    return target


def _replace_theme_files(replacements: dict[Path, bytes]) -> None:
    originals = {path: path.read_bytes() for path in replacements}
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for path, data in replacements.items():
            temporary = path.with_name(".{}.rxyy-{}-{}.tmp".format(
                path.name, os.getpid(), time.time_ns()))
            temporary.write_bytes(data)
            staged[path] = temporary
        for path, temporary in staged.items():
            os.replace(temporary, path)
            replaced.append(path)
    except OSError:
        for path in reversed(replaced):
            rollback = path.with_name(".{}.rxyy-rollback-{}-{}.tmp".format(
                path.name, os.getpid(), time.time_ns()))
            try:
                rollback.write_bytes(originals[path])
                os.replace(rollback, path)
            finally:
                rollback.unlink(missing_ok=True)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def _theme_editor_payload(theme_id: str) -> dict[str, Any]:
    target = _theme_target(theme_id)
    if target is None:
        raise ValueError("没有这个可编辑主题")
    theme_dir = Path(target["dir"])
    meta = _read_json(theme_dir / "theme.json", {}) or {}
    css_path = theme_dir / "theme.css"
    if not css_path.is_file():
        raise ValueError("主题缺少 theme.css，不能编辑")
    image = _theme_image_path(theme_dir, meta)
    css = css_path.read_text(encoding="utf-8-sig")
    rules = _parse_safe_css(css)
    history = _theme_history(theme_dir)
    defaults = _read_json(_dreamskin_engine_file("assets", "theme.json"), {}) or {}
    return {
        "ok": True,
        "id": target["id"],
        "dir": str(theme_dir),
        "theme": meta,
        "defaults": {
            "appearance": str(defaults.get("appearance") or "auto"),
            "art": defaults.get("art") if isinstance(defaults.get("art"), dict) else {},
            "colors": (defaults.get("colors")
                       if isinstance(defaults.get("colors"), dict) else {}),
        },
        "css": css,
        "rules": rules,
        "policy": _safe_css_policy(),
        "revision": _theme_revision(theme_dir),
        "background": {
            "name": image.name,
            "path": str(image),
            "preview": _thumb_data_uri(image, width=720),
        },
        "history": {
            "count": len(history),
            "latest_at": history[0].stat().st_mtime if history else 0,
        },
    }


def _process_identity(process_id: int) -> dict[str, str]:
    """Read the minimum Windows process identity used by Dream Skin itself."""
    if process_id <= 0:
        return {}
    command = (
        "$p=Get-Process -Id %d -ErrorAction SilentlyContinue;"
        "$w=Get-CimInstance Win32_Process -Filter \"ProcessId = %d\" "
        "-ErrorAction SilentlyContinue;"
        "if($p -and $w){[pscustomobject]@{"
        "startedAt=$p.StartTime.ToUniversalTime().ToString('o');"
        "executablePath=\"$($w.ExecutablePath)\";commandLine=\"$($w.CommandLine)\"}"
        "|ConvertTo-Json -Compress}" % (process_id, process_id)
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, timeout=10, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace").strip())
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "started_at": str(payload.get("startedAt") or "").strip(),
        "executable_path": str(payload.get("executablePath") or "").strip(),
        "command_line": str(payload.get("commandLine") or "").strip(),
    }


def _path_equal(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.normcase(os.path.normpath(str(left))).rstrip("\\/") == \
            os.path.normcase(os.path.normpath(str(right))).rstrip("\\/")
    except (TypeError, ValueError):
        return False


def _command_has_option(command: str, option: str, value: Any | None = None) -> bool:
    if not command:
        return False
    pattern = r"(?i)(?:^|\s)" + re.escape(option)
    if value is None:
        pattern += r"(?=$|\s)"
    else:
        pattern += r"(?:=|\s+)\"?" + re.escape(str(value)) + r"\"?(?=$|\s)"
    return re.search(pattern, command) is not None


def _dreamskin_runtime() -> dict[str, Any]:
    """Verify the recorded watcher process and its live Codex CDP identity.

    ``active-theme`` only means the files were staged.  The skin is visible only while
    the exact recorded watcher is alive and Codex owns the matching loopback CDP session.
    Checking PID start time, executable and watcher arguments prevents a reused PID or an
    unrelated Node/Chromium process from being mistaken for an active skin.
    """
    state = _read_json(DREAMSKIN_STATE / "state.json", {}) or {}
    try:
        port = int(state.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    browser_id = str(state.get("browserId") or "").strip()
    try:
        injector_pid = int(state.get("injectorPid") or 0)
    except (TypeError, ValueError):
        injector_pid = 0
    injector_started_at = str(state.get("injectorStartedAt") or "").strip()
    injector_path = str(state.get("injectorPath") or "").strip()
    node_path = str(state.get("nodePath") or "").strip()
    base = {"active": False, "port": port or 9335, "reason": ""}
    if (not 1024 <= port <= 65535 or not browser_id or injector_pid <= 0 or
            not injector_started_at or not injector_path or not node_path):
        base["reason"] = "主题引擎没有有效的启动记录"
        return base
    if (DREAMSKIN_STATE / "paused").exists():
        base["reason"] = "Dream Skin 已暂停"
        return base
    identity = _process_identity(injector_pid)
    if not identity:
        base["reason"] = "Dream Skin 注入器进程未运行"
        return base
    expected_stamp = _utc_stamp(injector_started_at)
    actual_stamp = _utc_stamp(identity.get("started_at"))
    if expected_stamp is None or actual_stamp is None or abs(expected_stamp - actual_stamp) >= 0.001:
        base["reason"] = "Dream Skin 注入器 PID 已被其他进程复用"
        return base
    if not _path_equal(identity.get("executable_path"), node_path):
        base["reason"] = "Dream Skin 注入器 PID 指向了其他程序"
        return base
    command_line = identity.get("command_line", "")
    if (injector_path.casefold() not in command_line.casefold() or
            not _command_has_option(command_line, "--watch") or
            not _command_has_option(command_line, "--port", port) or
            not _command_has_option(command_line, "--browser-id", browser_id)):
        base["reason"] = "Dream Skin 注入器进程身份与启动记录不符"
        return base
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:{}/json/version".format(port), timeout=0.45) as reply:
            payload = json.loads(reply.read(131072).decode("utf-8", "replace"))
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        base["reason"] = "Dream Skin 调试端口 {} 未监听".format(port)
        return base
    websocket = str(payload.get("webSocketDebuggerUrl") or "") if isinstance(payload, dict) else ""
    actual_id = websocket.rstrip("/").rsplit("/", 1)[-1] if websocket else ""
    if not actual_id or actual_id.casefold() != browser_id.casefold():
        base["reason"] = "调试端口不是这次 Dream Skin 启动的 Codex"
        return base
    return {"active": True, "port": port, "reason": ""}


def _utc_stamp(value: Any) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    # .NET ToString('o') 会给出 7 位小数；Python 3.8 fromisoformat 只吃到微秒。
    raw = re.sub(r"\.(\d{6})\d+", r".\1", raw)
    try:
        parsed = dt.datetime.fromisoformat(raw)
        # Dream Skin records UTC with ``ToString('o')``.  A timezone-less value is
        # ambiguous, so never use one as proof that a live process is stale.
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(dt.timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _reconcile_reused_injector_pid() -> bool:
    """Archive proven stale state under Dream Skin's own operation mutex.

    Dream Skin deliberately refuses to kill a process whose visible identity differs from
    its saved injector.  After a reboot Windows can reuse that PID for Chrome, leaving every
    later start blocked by the safety check.  A different process creation time proves it is
    a new process instance.  Schema 3 requires every injector field, so partially deleting
    PID fields would make the state unreadable; instead use Dream Skin's official lock and
    archive helper after a locked compare-and-swap recheck.  Ambiguous cases remain untouched.
    """
    if _dreamskin_runtime().get("active"):
        return False
    state_path = DREAMSKIN_STATE / "state.json"
    state = _read_json(state_path, {}) or {}
    try:
        process_id = int(state.get("injectorPid") or 0)
    except (TypeError, ValueError):
        return False
    expected_raw = str(state.get("injectorStartedAt") or "").strip()
    expected = _utc_stamp(expected_raw)
    if process_id <= 0 or expected is None:
        return False
    identity = _process_identity(process_id)
    current = _utc_stamp(identity.get("started_at")) if identity else None
    if current is None or abs(current - expected) < 0.001:
        return False
    common_script = _dreamskin_start_script().parent / "common-windows.ps1"
    if not common_script.is_file():
        return False
    env = os.environ.copy()
    env.update({
        "RXYY_DREAMSKIN_COMMON": str(common_script),
        "RXYY_DREAMSKIN_PID": str(process_id),
        "RXYY_DREAMSKIN_STARTED_AT": expected_raw,
    })
    command = (
        "$ErrorActionPreference='Stop';. $env:RXYY_DREAMSKIN_COMMON;"
        "$operationLock=$null;try{"
        "$operationLock=Enter-DreamSkinOperationLock -TimeoutMilliseconds 30000;"
        "$statePath=Join-Path $env:LOCALAPPDATA 'CodexDreamSkin\\state.json';"
        "$state=Read-DreamSkinState -Path $statePath;"
        "if($null -ne $state -and \"$($state.injectorPid)\" -ceq $env:RXYY_DREAMSKIN_PID "
        "-and \"$($state.injectorStartedAt)\" -ceq $env:RXYY_DREAMSKIN_STARTED_AT){"
        "$p=Get-Process -Id ([int]$env:RXYY_DREAMSKIN_PID) -ErrorAction SilentlyContinue;"
        "if($p){$actual=$p.StartTime.ToUniversalTime().ToString('o');"
        "if($actual -cne $env:RXYY_DREAMSKIN_STARTED_AT){"
        "$archive=Archive-DreamSkinStateFile -Path $statePath;"
        "Write-Output ('STALE_STATE_ARCHIVED='+$archive)}}}}"
        "finally{if($null -ne $operationLock){Exit-DreamSkinOperationLock -Mutex $operationLock}}"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, timeout=45, creationflags=_NO_WINDOW, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else ""
    return "STALE_STATE_ARCHIVED=" in output


def _wait_for_dreamskin_runtime(timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Allow process/CDP state a short visibility window after verified startup."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last = _dreamskin_runtime()
    while not last.get("active") and time.monotonic() < deadline:
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        last = _dreamskin_runtime()
    return last


def _active_theme() -> dict:
    meta = _read_json(DREAMSKIN_STATE / "active-theme" / "theme.json", {}) or {}
    return {
        "name": str(meta.get("name") or ""),
        "installed": (DREAMSKIN_STATE / "active-theme").is_dir(),
        "applied_at": _applied_at(DREAMSKIN_STATE / "active-theme"),
        "engine_version": (DREAMSKIN_STATE / "engine" / "VERSION").read_text(
            encoding="utf-8").strip() if (DREAMSKIN_STATE / "engine" / "VERSION").is_file() else "",
        "runtime": _dreamskin_runtime(),
    }


def _pets() -> list[dict]:
    pets_dir = CODEX_HOME / "pets"
    out = []
    if not pets_dir.is_dir():
        return out
    for child in sorted(pets_dir.iterdir()):
        if not child.is_dir():
            continue
        meta = _read_json(child / "pet.json", {}) or {}
        out.append({
            "id": child.name,
            "name": str(meta.get("name") or child.name.split("--")[0]),
            "author": str(meta.get("author") or (child.name.split("--")[1]
                                                 if "--" in child.name else "")),
            "has_sheet": (child / "spritesheet.webp").is_file(),
        })
    return out


def _island_running() -> bool:
    """灵动岛是 PowerShell 跑的 WPF 窗口，认命令行里的脚本名最准。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name like '%powershell%'\" |"
             " Where-Object { $_.CommandLine -match 'CodexIsland' } | Measure-Object).Count"],
            capture_output=True, timeout=15, creationflags=_NO_WINDOW)
        return int((proc.stdout.decode("utf-8", "replace").strip() or "0")) > 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _island_autostart() -> str:
    """on / off / missing —— off 是「快捷方式还在但改了后缀」，missing 是压根没装。"""
    if (STARTUP_DIR / ISLAND_LNK).is_file():
        return "on"
    if (STARTUP_DIR / (ISLAND_LNK + ".disabled")).is_file():
        return "off"
    return "missing"


def _board_state() -> dict:
    """看板这半在 2026-08-13 变了：dashi(47823) 退役，原生任务面板接手。"""
    import socket
    alive = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            alive = sock.connect_ex(("127.0.0.1", 47823)) == 0
    except OSError:
        alive = False
    lnk = STARTUP_DIR / "Codex Taskboard.lnk"
    return {
        "native_panel": True,
        "dashi_running": alive,
        "dashi_autostart": "on" if lnk.is_file() else (
            "off" if (STARTUP_DIR / "Codex Taskboard.lnk.disabled").is_file() else "missing"),
    }


def _thumb_data_uri(path: Path, width: int = 360) -> str:
    """把主题原画压成一张小缩略图的 data URI。

    原画是 300-600KB 的大图，整页塞进去手机上要等半天；也不能直接给本地绝对路径——
    页面是从 web 目录出来的，file:// 那种路径它加载不了。所以后端压完直接内联。
    """
    try:
        from PIL import Image
    except ImportError:
        return ""
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            ratio = width / float(image.width or width)
            image = image.resize((width, max(1, int(image.height * ratio))))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=72)
    except (OSError, ValueError):
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _backup_dir() -> Path:
    try:
        from src.config import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:  # noqa: BLE001  源码态跑测试时没有 src.config
        base = Path(os.environ.get("LOCALAPPDATA", ".")) / "rxyy-tools"
    return base / "codex-dress"


class CodexCfgApi:
    """Codex 装扮配置中心（挂进控制台 Api 的混入）。"""

    def codexcfg_status(self) -> dict[str, Any]:
        island_cfg = _read_json(ISLAND_STATE / "config.json", {}) or {}
        pets = _pets()
        active_pet = str(island_cfg.get("petId") or "")
        dreamskin_autostart = _ensure_dreamskin_autostart()
        return {
            "ok": True,
            "theme": {
                "active": _active_theme(),
                "library": _scan_themes(),
                "state_dir": str(DREAMSKIN_STATE),
                "compat": _skin_compat(repair_if_missing=True),
                "autostart": dreamskin_autostart,
            },
            "pets": {
                "list": pets,
                "island_pet": active_pet,
                "dir": str(CODEX_HOME / "pets"),
                # Codex 自己设置里选的宠物不在文件里（在设置界面），这里只管灵动岛用哪个
                "note": "这里管的是灵动岛头像用哪个宠物；Codex 自己的宠物在 Codex 设置里选",
            },
            "island": {
                "running": _island_running(),
                "autostart": _island_autostart(),
                "config": {"left": island_cfg.get("left"), "top": island_cfg.get("top")},
                "config_path": str(ISLAND_STATE / "config.json"),
                "log_at": (ISLAND_STATE / "island.log").stat().st_mtime
                if (ISLAND_STATE / "island.log").is_file() else 0,
            },
            "board": _board_state(),
        }

    def codexcfg_set_island_pet(self, pet_id: str = "") -> dict[str, Any]:
        pet_id = str(pet_id or "").strip()
        if not pet_id:
            return {"ok": False, "error": "没给宠物 id"}
        if not (CODEX_HOME / "pets" / pet_id / "pet.json").is_file():
            return {"ok": False, "error": "本机没装这个宠物：" + pet_id}
        config = _read_json(ISLAND_STATE / "config.json", {}) or {}
        if config.get("petId") == pet_id:
            return {"ok": True, "msg": "本来就是它", "changed": False}
        config["petId"] = pet_id
        try:
            _write_json_atomic(ISLAND_STATE / "config.json", config)
        except OSError as exc:
            return {"ok": False, "error": "写灵动岛配置失败：%r" % exc}
        return {
            "ok": True, "changed": True,
            # 灵动岛把头像缓存在 pet-avatar.png，换了要它自己重读；说清楚免得以为没生效
            "msg": "已改成 {}；灵动岛正在跑的话，右键胶囊「切换宠物」即时生效，"
                   "或下次启动时读到".format(pet_id),
        }

    def codexcfg_set_island_autostart(self, enabled: Any = True) -> dict[str, Any]:
        """开机自启开关：只在 .lnk 与 .lnk.disabled 之间改名，绝不删快捷方式。"""
        want_on = bool(enabled)
        live, off = STARTUP_DIR / ISLAND_LNK, STARTUP_DIR / (ISLAND_LNK + ".disabled")
        try:
            if want_on:
                if live.is_file():
                    return {"ok": True, "changed": False, "msg": "本来就开着"}
                if not off.is_file():
                    return {"ok": False, "error": "启动文件夹里没有灵动岛的快捷方式，"
                                                  "先在灵动岛面板里勾一次开机自启"}
                off.rename(live)
            else:
                if off.is_file() and not live.is_file():
                    return {"ok": True, "changed": False, "msg": "本来就关着"}
                if not live.is_file():
                    return {"ok": False, "error": "启动文件夹里没有灵动岛的快捷方式"}
                live.rename(off)
        except OSError as exc:
            return {"ok": False, "error": "改启动项失败：%r" % exc}
        return {"ok": True, "changed": True,
                "msg": "开机自启已" + ("打开" if want_on else "关闭") + "（改的是快捷方式后缀，可随时改回）"}

    def codexcfg_theme_preview(self, theme_id: str = "") -> dict[str, Any]:
        """按需取一张主题缩略图（点开才拉，别开页就灌几百 KB）。"""
        target = next((t for t in _scan_themes() if t["id"] == str(theme_id or "")), None)
        if target is None or not target.get("preview"):
            return {"ok": False, "error": "这个主题没有可预览的原画"}
        uri = _thumb_data_uri(Path(target["preview"]))
        if not uri:
            return {"ok": False, "error": "缩略图生成失败（原画读不了或缺图像库）"}
        return {"ok": True, "id": target["id"], "preview": uri}

    def codexcfg_theme_editor(self, theme_id: str = "") -> dict[str, Any]:
        """Return every schema-backed theme field and the full Safe CSS rule model."""
        try:
            return _theme_editor_payload(str(theme_id or ""))
        except (OSError, TypeError, ValueError) as exc:
            return {"ok": False, "error": "主题编辑器读取失败：{}".format(exc)}

    def codexcfg_pick_theme_background(self, theme_id: str = "") -> dict[str, Any]:
        """Open a local image picker without changing the theme yet."""
        target = _theme_target(str(theme_id or ""))
        if target is None:
            return {"ok": False, "error": "没有这个可编辑主题"}
        window = getattr(self, "_window", None)
        if window is None:
            return {"ok": False, "error": "当前不是桌面窗口，不能打开图片选择器"}
        try:
            import webview
            chosen = window.create_file_dialog(
                webview.FileDialog.OPEN,
                allow_multiple=False,
                file_types=("图片 (*.jpg;*.jpeg;*.png;*.webp)",),
            )
        except Exception as exc:  # noqa: BLE001  pywebview/系统对话框错误要原样回界面
            return {"ok": False, "error": "图片选择器打不开：{}".format(exc)}
        if not chosen:
            return {"ok": True, "cancelled": True}
        source = Path(str(chosen[0]))
        meta = _read_json(Path(target["dir"]) / "theme.json", {}) or {}
        try:
            destination = _theme_image_path(Path(target["dir"]), meta)
            _prepare_background_bytes(source, destination)
            preview = _thumb_data_uri(source, width=720)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(source.resolve()), "preview": preview,
                "name": source.name}

    def codexcfg_save_theme(self, theme_id: str = "", draft: Any = None,
                            css: str = "", revision: str = "",
                            background_path: str = "", apply: Any = False) -> dict[str, Any]:
        """Validate and atomically save a theme source; optionally apply it officially."""
        target = _theme_target(str(theme_id or ""))
        if target is None:
            return {"ok": False, "error": "没有这个可编辑主题"}
        theme_dir = Path(target["dir"])
        current = _read_json(theme_dir / "theme.json", {}) or {}
        try:
            current_revision = _theme_revision(theme_dir)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": "主题源文件不完整：{}".format(exc)}
        if not revision or str(revision).lower() != current_revision.lower():
            return {"ok": False, "conflict": True,
                    "error": "主题文件在编辑期间被其他程序修改；请刷新编辑器后重试"}
        try:
            clean_meta = _validate_theme_meta(draft, current)
            _parse_safe_css(css)
            validation = _validate_safe_css(css)
            image = _theme_image_path(theme_dir, clean_meta)
            replacements: dict[Path, bytes] = {
                theme_dir / "theme.json": (json.dumps(clean_meta, ensure_ascii=False, indent=2)
                                            + "\n").encode("utf-8"),
                theme_dir / "theme.css": css.rstrip().encode("utf-8") + b"\n",
            }
            selected = str(background_path or "").strip()
            if selected:
                replacements[image] = _prepare_background_bytes(Path(selected), image)
            changed = any(path.read_bytes() != data for path, data in replacements.items())
            if changed:
                backup = _snapshot_theme_source(theme_dir, current)
                _replace_theme_files(replacements)
            else:
                backup = None
        except (OSError, TypeError, ValueError, zipfile.BadZipFile) as exc:
            return {"ok": False, "error": "主题没有保存：{}".format(exc)}

        apply_now = apply is True or str(apply).strip().lower() in {"1", "true", "yes", "on"}
        if apply_now:
            applied = self.codexcfg_apply_theme(str(theme_id or ""))
            if not applied.get("ok"):
                return {"ok": False, "saved": True, "changed": changed,
                        "backup": str(backup) if backup else "",
                        "error": "主题已保存，但应用失败：{}".format(
                            applied.get("error") or "未知错误")}
        try:
            editor = _theme_editor_payload(str(theme_id or ""))
        except (OSError, TypeError, ValueError):
            editor = None
        return {
            "ok": True,
            "saved": True,
            "changed": changed,
            "applied": apply_now,
            "validation": validation,
            "backup": str(backup) if backup else "",
            "editor": editor,
            "msg": ("已保存并应用「{}」" if apply_now else
                    ("已保存「{}」" if changed else "「{}」没有变化")).format(
                        clean_meta["name"]),
        }

    def codexcfg_restore_theme_edit(self, theme_id: str = "", apply: Any = False,
                                    revision: str = "") -> dict[str, Any]:
        """Restore the latest source snapshot, keeping the current source as a new snapshot."""
        target = _theme_target(str(theme_id or ""))
        if target is None:
            return {"ok": False, "error": "没有这个可编辑主题"}
        theme_dir = Path(target["dir"])
        try:
            current_revision = _theme_revision(theme_dir)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": "主题源文件不完整：{}".format(exc)}
        if not revision or str(revision).lower() != current_revision.lower():
            return {"ok": False, "conflict": True,
                    "error": "主题文件在编辑期间被其他程序修改；请刷新编辑器后重试"}
        history = _theme_history(theme_dir)
        if not history:
            return {"ok": False, "error": "这个主题还没有可恢复的编辑历史"}
        source = history[0]
        current = _read_json(theme_dir / "theme.json", {}) or {}
        try:
            with zipfile.ZipFile(source) as bundle:
                manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
                expected_dir = str(theme_dir.resolve())
                if (manifest.get("schemaVersion") != 1 or
                        str(manifest.get("sourceDir") or "").lower() != expected_dir.lower()):
                    raise ValueError("历史快照不属于当前主题目录")
                image_name = str(manifest.get("image") or "")
                meta_bytes = bundle.read("theme.json")
                css_bytes = bundle.read("theme.css")
                image_bytes = bundle.read(image_name)
            restored_meta = json.loads(meta_bytes.decode("utf-8-sig"))
            clean_meta = _validate_theme_meta(restored_meta, current)
            if image_name != str(clean_meta.get("image") or ""):
                raise ValueError("历史快照的背景文件名不匹配")
            css_text = css_bytes.decode("utf-8-sig")
            _parse_safe_css(css_text)
            _validate_safe_css(css_text)
            if not 0 < len(image_bytes) <= THEME_IMAGE_MAX_BYTES:
                raise ValueError("历史快照的背景图大小不合法")
            destination = _theme_image_path(theme_dir, current)
            current_backup = _snapshot_theme_source(theme_dir, current)
            _replace_theme_files({
                theme_dir / "theme.json": (json.dumps(clean_meta, ensure_ascii=False, indent=2)
                                            + "\n").encode("utf-8"),
                theme_dir / "theme.css": css_text.rstrip().encode("utf-8") + b"\n",
                destination: image_bytes,
            })
        except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
            return {"ok": False, "error": "主题历史恢复失败：{}".format(exc)}

        apply_now = apply is True or str(apply).strip().lower() in {"1", "true", "yes", "on"}
        if apply_now:
            applied = self.codexcfg_apply_theme(str(theme_id or ""))
            if not applied.get("ok"):
                return {"ok": False, "restored": True,
                        "error": "源文件已恢复，但应用失败：{}".format(
                            applied.get("error") or "未知错误")}
        return {"ok": True, "restored": True, "applied": apply_now,
                "backup": str(current_backup),
                "msg": "已恢复上一版{}".format("并应用" if apply_now else "")}

    def codexcfg_backup(self) -> dict[str, Any]:
        """把整套装扮打个包：当前主题的实际文件 + 灵动岛配置 + 一份清单。

        存的是「现在生效的这一套」，不是主题库里那套源文件——源文件随时会被改
        （今天 gojo-dark 就一直在变），只有 active-theme 里的才是此刻真正在用的。
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_dir = _backup_dir()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": "建不出备份目录：%r" % exc}
        target = out_dir / ("codex-dress-%s.zip" % stamp)
        island_cfg = _read_json(ISLAND_STATE / "config.json", {}) or {}
        manifest = {
            "created_at": time.time(),
            "theme": _active_theme(),
            "island": {"petId": island_cfg.get("petId"),
                       "left": island_cfg.get("left"), "top": island_cfg.get("top"),
                       "autostart": _island_autostart()},
            "pets_installed": [p["id"] for p in _pets()],
        }
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("manifest.json",
                                json.dumps(manifest, ensure_ascii=False, indent=1))
                active = DREAMSKIN_STATE / "active-theme"
                if active.is_dir():
                    for child in active.iterdir():
                        if child.is_file():
                            bundle.write(child, "active-theme/" + child.name)
                cfg_path = ISLAND_STATE / "config.json"
                if cfg_path.is_file():
                    bundle.write(cfg_path, "island/config.json")
        except OSError as exc:
            return {"ok": False, "error": "打包失败：%r" % exc}
        return {"ok": True, "path": str(target),
                "size": target.stat().st_size,
                "msg": "已备份到 {}（{:.1f} MB）".format(target.name,
                                                    target.stat().st_size / 1048576)}

    def codexcfg_backups(self) -> dict[str, Any]:
        out_dir = _backup_dir()
        items = []
        if out_dir.is_dir():
            for child in sorted(out_dir.glob("codex-dress-*.zip"), reverse=True):
                info = child.stat()
                items.append({"name": child.name, "path": str(child),
                              "size": info.st_size, "at": info.st_mtime})
        return {"ok": True, "items": items[:20], "dir": str(out_dir)}

    def codexcfg_restore(self, path: str = "") -> dict[str, Any]:
        """从备份恢复：灵动岛配置直接写回，主题按名字回到主题库那套重新应用。

        **刻意不把 active-theme 的文件直接拷回去**：那套状态是 Dream Skin 插件维护的
        （还要改 Codex 自己的配置、算色、注入），手工塞文件迟早跟插件版本对不上。
        备份里的主题名能在主题库里找到就走官方脚本重新应用，找不到就如实说。
        """
        target = Path(str(path or "").strip())
        if not target.is_file():
            return {"ok": False, "error": "找不到这个备份文件"}
        try:
            with zipfile.ZipFile(target) as bundle:
                manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
                island_raw = (bundle.read("island/config.json").decode("utf-8")
                              if "island/config.json" in bundle.namelist() else "")
        except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            return {"ok": False, "error": "备份读不出来：%r" % exc}

        done, skipped = [], []
        theme_error = ""
        if island_raw:
            try:
                saved = json.loads(island_raw)
                current = _read_json(ISLAND_STATE / "config.json", {}) or {}
                current.update({k: saved[k] for k in ("petId", "left", "top") if k in saved})
                _write_json_atomic(ISLAND_STATE / "config.json", current)
                done.append("灵动岛配置（宠物与位置）")
            except (ValueError, OSError) as exc:
                skipped.append("灵动岛配置：%r" % exc)

        want = str((manifest.get("theme") or {}).get("name") or "")
        if want:
            match = next((t for t in _scan_themes() if t["name"] == want), None)
            if match is None:
                skipped.append("主题「{}」：主题库里没有同名的那套，没法重新应用".format(want))
            else:
                applied = self.codexcfg_apply_theme(match["id"])
                (done if applied.get("ok") else skipped).append(
                    "主题「{}」{}".format(want, "" if applied.get("ok")
                                       else "：" + str(applied.get("error"))[:120]))
                if not applied.get("ok"):
                    theme_error = str(applied.get("error") or "主题没有通过运行态验证")
        message = "已恢复：{}{}".format(
            "、".join(done) or "（没有可恢复项）",
            "；跳过：" + "；".join(skipped) if skipped else "")
        if theme_error:
            return {"ok": False, "partial": bool(done), "restored": done,
                    "skipped": skipped, "error": "恢复未完成：" + theme_error,
                    "msg": message}
        return {"ok": True, "restored": done, "skipped": skipped, "msg": message}

    def codexcfg_apply_theme(self, theme_id: str = "") -> dict[str, Any]:
        """应用主题并启动/验证 Dream Skin，而不是只把文件写进 active-theme。

        不自己拼 CSS、不直接写 Dream Skin 的 active-theme——那套状态由插件的官方脚本
        维护（备份原 config、算色、落 css），绕过去迟早对不上版本。主题目录脚本只负责
        落文件；随后必须调用 Dream Skin 的 start 脚本，必要时重启 Codex，并验证 CDP
        browser id。否则「APPLY_DONE」只代表已选中，不能宣称画面生效。
        """
        theme_id = str(theme_id or "").strip()
        target = next((t for t in _scan_themes() if t["id"] == theme_id), None)
        if target is None:
            return {"ok": False, "error": "没有这个主题（或它目录里缺 theme.json / apply-art-theme.ps1）"}
        _skin_compat(apply=True)
        script = Path(target["dir"]) / "apply-art-theme.ps1"
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script)],
                capture_output=True, timeout=180, cwd=target["dir"],
                creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "应用主题超时（3 分钟没跑完）"}
        except OSError as exc:
            return {"ok": False, "error": "起不来 PowerShell：%r" % exc}
        out = proc.stdout.decode("utf-8", "replace").strip()
        err = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or "APPLY_DONE" not in out:
            return {"ok": False, "error": _powershell_apply_error(err, out)}

        start_script = _dreamskin_start_script()
        if not start_script.is_file():
            return {"ok": False, "staged": True,
                    "error": "主题文件已切换，但找不到 Dream Skin 启动脚本，不能确认画面生效"}
        reconciled_stale_pid = _reconcile_reused_injector_pid()
        try:
            started = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(start_script), "-RestartExisting", "-RequireUnpaused",
                 "-OperationLockTimeoutMilliseconds", "30000"],
                capture_output=True, timeout=300, cwd=str(start_script.parent),
                creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return {"ok": False, "staged": True,
                    "error": "主题文件已切换，但 Dream Skin 启动与可见性验证超时（5 分钟）"}
        except OSError as exc:
            return {"ok": False, "staged": True,
                    "error": "主题文件已切换，但起不来 Dream Skin：%r" % exc}
        started_out = started.stdout.decode("utf-8", "replace").strip()
        started_err = started.stderr.decode("utf-8", "replace").strip()
        if started.returncode != 0:
            return {"ok": False, "staged": True,
                    "error": (started_err or started_out or
                              "主题文件已切换，但 Dream Skin 启动失败")[-600:]}
        # Official starter waits up to 210s for 26.810's empty shell to grow
        # sidebar/home markers (injector timeout 180s). Keep a short settle window.
        runtime = _wait_for_dreamskin_runtime()
        if not runtime.get("active"):
            detail = str(runtime.get("reason") or "运行态验证没有通过")
            return {"ok": False, "staged": True,
                    "error": "主题文件已切换，但主题没有确认生效：" + detail}
        try:
            from api.codex_sidebar_board import keep_injected
            keep_injected()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "changed": True, "runtime_active": True,
                "reconciled_stale_pid": reconciled_stale_pid,
                "msg": "已应用「{}」；Dream Skin 已启动并验证生效".format(target["name"]),
                "output": (out + "\n" + started_out)[-600:], "applied_at": time.time()}

    def codexcfg_sidebar_probe(self) -> dict[str, Any]:
        from api.codex_sidebar_board import probe
        return probe()

    def codexcfg_sidebar_inject(self) -> dict[str, Any]:
        from api.codex_sidebar_board import inject
        return inject()

    def codexcfg_sidebar_remove(self) -> dict[str, Any]:
        from api.codex_sidebar_board import remove
        return remove()

    def codexcfg_skin_compat(self, apply: Any = True) -> dict[str, Any]:
        """检查或重打 Codex 26.803 皮肤锚点补丁（首页帽 / 输入框外壳）。"""
        report = _skin_compat(apply=bool(apply))
        report["ok"] = True if report.get("skipped") else bool(report.get("ok"))
        if report.get("skipped"):
            report["msg"] = report.get("reason") or "未安装 Dream Skin 引擎"
        elif apply:
            report["msg"] = ("已重打 26.803 皮肤补丁" if report.get("patched")
                             else "皮肤锚点补丁已经在")
        else:
            report["msg"] = "皮肤锚点正常" if report.get("ok") else "皮肤锚点缺失，需要重打补丁"
        return report


__all__ = ["CodexCfgApi"]
