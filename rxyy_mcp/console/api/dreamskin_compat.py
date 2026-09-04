"""Dream Skin 26.803 兼容补丁：固化在 rxyy tools 源里，升级引擎后可重打。

Codex 26.803 把首页帽和输入框壳的 CSS Module 改名了，官方契约还停在 26.727。
补丁只改已安装引擎的 selectors / renderer / CSS / Safe CSS parts，不动 hub。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

BEGIN = "/* rxyy-tools-compat:begin */"
END = "/* rxyy-tools-compat:end */"
LEGACY_CSS_HINT = "Codex 26.803 dropped .composer-surface-chrome"

HOME_UTILITY = (
    '[class*="_homeUtilityBar_"], [class*="_ComposerHomeUtilityBar_"]'
)
COMPOSER_CHROME = (
    '.composer-surface-chrome, [class*="_ComposerLayoutFooter_"]'
)
COMPOSER_SHELL = '[class*="_ComposerLayoutBody_"]'
UTILITY_CSS = (
    ':is([class*="_homeUtilityBar_"], [class*="_ComposerHomeUtilityBar_"])'
)

EXTRA_PARTS = ("home-utility", "composer-shell")

SELECTOR_UPSERTS = {
    "home-utility": HOME_UTILITY,
    "composer-chrome": COMPOSER_CHROME,
    "composer-shell": COMPOSER_SHELL,
}

OVERLAY_CSS = """
html[data-dream-skin="active"] [class*="_ComposerLayoutBody_"] {
  background: rgb(var(--ds-panel-rgb) / .94) !important;
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
  border-radius: 22px !important;
  box-shadow:
    0 10px 28px rgb(var(--ds-bg-rgb) / .14),
    0 0 0 1px #8A8F98 !important;
}

html[data-dream-skin="active"][data-dream-art-wide="true"] [class*="_ComposerLayoutBody_"] {
  background: var(--ds-immersive-composer-solid) !important;
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
  box-shadow:
    0 10px 30px rgb(var(--ds-bg-rgb) / .20),
    0 0 0 1px var(--ds-immersive-line) !important;
}

html[data-dream-skin="active"] [data-ds-part="composer"],
html[data-dream-skin="active"] [data-ds-part="composer-shell"],
html[data-dream-skin="active"] [class*="_ComposerLayoutFooter_"] {
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
}

html[data-dream-skin="active"],
html[data-dream-skin="active"] body {
  --color-text-default: var(--ds-text) !important;
  --color-text-primary: var(--ds-text) !important;
  --color-text-secondary-solid: rgb(var(--ds-text-rgb) / .78) !important;
  --color-text-tertiary: rgb(var(--ds-muted-rgb) / .86) !important;
  --color-token-text-primary: var(--ds-text) !important;
  --color-token-text-secondary: rgb(var(--ds-muted-rgb) / .86) !important;
  --color-token-text-tertiary: rgb(var(--ds-muted-rgb) / .86) !important;
  --color-codex-terminal-foreground: var(--ds-text) !important;
  --vscode-foreground: var(--ds-text) !important;
  --vscode-sideBar-foreground: var(--ds-text) !important;
  --vscode-editor-foreground: var(--ds-text) !important;
  --vscode-icon-foreground: var(--ds-text) !important;
}
html[data-dream-skin="active"][data-dream-shell="light"] {
  --color-token-main-surface-primary: var(--ds-theme-color-background, var(--ds-bg)) !important;
  --color-token-main-surface-secondary: var(--ds-theme-color-panel, var(--ds-panel)) !important;
  --color-background-primary: var(--ds-theme-color-background, var(--ds-bg)) !important;
  --vscode-editor-background: var(--ds-theme-color-background, var(--ds-bg)) !important;
}
html[data-dream-skin="active"] .text-default {
  color: var(--ds-text) !important;
}
html[data-dream-skin="active"] .text-tertiary,
html[data-dream-skin="active"] .text-codex-description {
  color: var(--ds-muted) !important;
}
""".strip()

_ADDPART_HOME = '    addPart(desired, "home-utility", selectorNodes("home-utility"));\n'
_ADDPART_SHELL = '    addPart(desired, "composer-shell", selectorNodes("composer-shell"));\n'
_ADDPART_COMPOSER = (
    '    addPart(desired, "composer", '
    '[...selectorNodes("composer-chrome"), ...fallbackComposerNodes()]);'
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _upsert_selector_list(selectors: list[dict[str, Any]]) -> bool:
    changed = False
    by_key = {str(item.get("key") or ""): item for item in selectors
              if isinstance(item, dict)}
    for key, selector in SELECTOR_UPSERTS.items():
        current = by_key.get(key)
        if current is None:
            selectors.append({
                "key": key,
                "selector": selector,
                "tier": "L2",
                "scope": "home+thread" if key != "home-utility" else "home",
                "required": False,
            })
            changed = True
            continue
        if str(current.get("selector") or "") != selector:
            current["selector"] = selector
            changed = True
    return changed


def _patch_selectors_json(path: Path) -> bool:
    if not path.is_file():
        return False
    raw = json.loads(_read(path))
    selectors = raw.get("selectors")
    if not isinstance(selectors, list):
        return False
    if not _upsert_selector_list(selectors):
        return False
    _write(path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    return True


_VALIDATOR_PARTS_OLD = '  "composer-toolbar",\n  "dialog",'
_VALIDATOR_PARTS_NEW = (
    '  "composer-toolbar",\n  "home-utility",\n  "composer-shell",\n  "dialog",'
)


def _patch_validator_parts(path: Path) -> bool:
    """官方 apply 走的是 validator 里写死的 SAFE_CSS_PARTS，不是 policy.json。

    主题编辑器会写出 home-utility / composer-shell，不补这一处，「启动并应用」
    就会被 PowerShell 包成 NativeCommandError。
    """
    if not path.is_file():
        return False
    text = _read(path)
    if '"home-utility"' in text and '"composer-shell"' in text:
        return False
    if _VALIDATOR_PARTS_OLD not in text:
        return False
    _write(path, text.replace(_VALIDATOR_PARTS_OLD, _VALIDATOR_PARTS_NEW, 1))
    return True


def _patch_policy_parts(path: Path) -> bool:
    if not path.is_file():
        return False
    raw = json.loads(_read(path))
    parts = raw.get("parts")
    if not isinstance(parts, list):
        return False
    changed = False
    for part in EXTRA_PARTS:
        if part not in parts:
            if "composer-toolbar" in parts:
                parts.insert(parts.index("composer-toolbar") + 1, part)
            else:
                parts.append(part)
            changed = True
    if not changed:
        return False
    raw["parts"] = parts
    _write(path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    return True


MANAGED_INK_OLD = 'ink = "#4A235F"'
MANAGED_INK_NEW = 'ink = "#1B1D22"'
MANAGED_SURFACE_OLD = 'surface = "#FFF4FA"'
MANAGED_SURFACE_NEW = 'surface = "#F6F7F9"'
MANAGED_ACCENT_OLD = 'accent = "#B65CFF"'
MANAGED_ACCENT_NEW = 'accent = "#3B4148"'
MANAGED_SKILL_OLD = 'skill = "#C47BFF"'
MANAGED_SKILL_NEW = 'skill = "#63666D"'
MANAGED_CHROME_REPLACEMENTS = (
    (MANAGED_INK_OLD, MANAGED_INK_NEW),
    (MANAGED_SURFACE_OLD, MANAGED_SURFACE_NEW),
    (MANAGED_ACCENT_OLD, MANAGED_ACCENT_NEW),
    (MANAGED_SKILL_OLD, MANAGED_SKILL_NEW),
)


def _patch_managed_chrome(path: Path) -> bool:
    """官方 start-dream-skin 会把浅色 chrome 写回粉底粉紫。

    只改 ink 不够：surface=#FFF4FA 是整页淡粉底，accent=#B65CFF 是粉边。
    雪白拍板：底 #F6F7F9、强调 #3B4148。各项独立替换，因为现役机上 ink 往往已经补过。
    """
    if not path.is_file():
        return False
    text = _read(path)
    original = text
    for old, new in MANAGED_CHROME_REPLACEMENTS:
        if old in text:
            text = text.replace(old, new)
    if text == original:
        return False
    _write(path, text)
    return True


def _patch_managed_ink(path: Path) -> bool:
    return _patch_managed_chrome(path)


def _managed_ink_ok(path: Path) -> bool:
    if not path.is_file():
        return True
    text = _read(path)
    return MANAGED_INK_NEW in text and MANAGED_INK_OLD not in text


def _managed_surface_ok(path: Path) -> bool:
    if not path.is_file():
        return True
    text = _read(path)
    return MANAGED_SURFACE_NEW in text and MANAGED_SURFACE_OLD not in text


def _managed_accent_ok(path: Path) -> bool:
    if not path.is_file():
        return True
    text = _read(path)
    if MANAGED_ACCENT_OLD in text:
        return False
    if 'accent = "' in text:
        return MANAGED_ACCENT_NEW in text
    return True


INJECTOR_TIMEOUT_OLD = "options.timeoutMs > 120000"
INJECTOR_TIMEOUT_NEW = "options.timeoutMs > 180000"
START_WAIT_REPLACEMENTS = (
    ("$verifyDeadline = (Get-Date).AddSeconds(90)",
     "$verifyDeadline = (Get-Date).AddSeconds(210)"),
    ("'--timeout-ms', '30000')",
     "'--timeout-ms', '180000')"),
    ("'--timeout-ms', '15000')",
     "'--timeout-ms', '180000')"),
)


def _patch_text(path: Path, old: str, new: str) -> bool:
    if not path.is_file():
        return False
    text = _read(path)
    if old not in text:
        return False
    _write(path, text.replace(old, new))
    return True


def _patch_injector_timeout(path: Path) -> bool:
    """26.810 空壳要约 2.5 分钟才有侧栏，官方上限 120s 不够。"""
    return _patch_text(path, INJECTOR_TIMEOUT_OLD, INJECTOR_TIMEOUT_NEW)


def _patch_start_wait(path: Path) -> bool:
    if not path.is_file():
        return False
    text = _read(path)
    original = text
    for old, new in START_WAIT_REPLACEMENTS:
        if old in text:
            text = text.replace(old, new)
    if text == original:
        return False
    _write(path, text)
    return True


def _start_wait_ok(path: Path) -> bool:
    if not path.is_file():
        return True
    text = _read(path)
    return "AddSeconds(210)" in text and "AddSeconds(90)" not in text


def _injector_timeout_ok(path: Path) -> bool:
    if not path.is_file():
        return True
    text = _read(path)
    return INJECTOR_TIMEOUT_NEW in text and INJECTOR_TIMEOUT_OLD not in text


def _patch_css(path: Path) -> bool:
    if not path.is_file():
        return False
    text = _read(path)
    original = text
    if "[class*=\"_homeUtilityBar_\"]" in text and "ComposerHomeUtilityBar" not in text:
        text = text.replace('[class*="_homeUtilityBar_"]', UTILITY_CSS)
    if BEGIN in text and END in text:
        text = re.sub(
            re.escape(BEGIN) + r".*?" + re.escape(END),
            BEGIN + "\n" + OVERLAY_CSS + "\n" + END,
            text,
            count=1,
            flags=re.S,
        )
    else:
        hint_at = text.find(LEGACY_CSS_HINT)
        if hint_at >= 0:
            comment_at = text.rfind("/*", 0, hint_at)
            newer_at = text.find("/* Newer app shells", hint_at)
            if comment_at >= 0 and newer_at > comment_at:
                text = text[:comment_at] + text[newer_at:]
        text = text.rstrip() + "\n\n" + BEGIN + "\n" + OVERLAY_CSS + "\n" + END + "\n"
    if text == original:
        return False
    _write(path, text)
    return True


def _patch_renderer(path: Path) -> bool:
    if not path.is_file():
        return False
    text = _read(path)
    original = text
    match = re.search(r"const SELECTOR_CONTRACT = (\{.*?\});", text)
    if match:
        try:
            contract = json.loads(match.group(1))
        except json.JSONDecodeError:
            contract = None
        if isinstance(contract, dict) and isinstance(contract.get("selectors"), list):
            if _upsert_selector_list(contract["selectors"]):
                dumped = json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
                text = text[:match.start(1)] + dumped + text[match.end(1):]
    if _ADDPART_HOME not in text and _ADDPART_COMPOSER in text:
        text = text.replace(
            _ADDPART_COMPOSER,
            _ADDPART_HOME + _ADDPART_SHELL + _ADDPART_COMPOSER,
            1,
        )
    if text == original:
        return False
    _write(path, text)
    return True


def inspect_root(root: Path) -> dict[str, Any]:
    css = root / "assets" / "dream-skin.css"
    selectors = root / "assets" / "selectors.json"
    renderer = root / "assets" / "renderer-inject.js"
    policy = root / "assets" / "safe-css-policy.json"
    validator = root / "assets" / "safe-css-validator.mjs"
    css_text = _read(css) if css.is_file() else ""
    sel_text = _read(selectors) if selectors.is_file() else ""
    js_text = _read(renderer) if renderer.is_file() else ""
    pol_text = _read(policy) if policy.is_file() else ""
    val_text = _read(validator) if validator.is_file() else ""
    checks = [
        {"id": "utility-selector",
         "ok": "ComposerHomeUtilityBar" in sel_text or "ComposerHomeUtilityBar" in js_text,
         "label": "首页帽选择器跟上 26.803"},
        {"id": "shell-selector",
         "ok": "ComposerLayoutBody" in sel_text or "ComposerLayoutBody" in js_text,
         "label": "输入框外壳选择器跟上 26.803"},
        {"id": "overlay-css",
         "ok": BEGIN in css_text or "ComposerLayoutBody" in css_text,
         "label": "石墨灰外壳补丁已写入 CSS"},
        {"id": "snow-ink",
         "ok": "--color-token-text-primary" in css_text,
         "label": "雪白字色覆盖已写入 CSS"},
        {"id": "snow-surface-css",
         "ok": "--color-token-main-surface-primary" in css_text,
         "label": "浅色 token 底色跟主题走，不再吃官方粉底"},
        {"id": "managed-ink",
         "ok": _managed_ink_ok(root / "scripts" / "config-utf8.ps1"),
         "label": "官方浅色 chrome 不再把 ink 写回粉紫"},
        {"id": "managed-surface",
         "ok": _managed_surface_ok(root / "scripts" / "config-utf8.ps1"),
         "label": "官方浅色 chrome 不再把 surface 写回粉底 #FFF4FA"},
        {"id": "managed-accent",
         "ok": _managed_accent_ok(root / "scripts" / "config-utf8.ps1"),
         "label": "官方浅色 chrome 不再把 accent 写回粉紫边"},
        {"id": "shell-wait",
         "ok": _start_wait_ok(root / "scripts" / "start-dream-skin.ps1"),
         "label": "官方启动会等到 Codex 壳出现（约 3.5 分钟）"},
        {"id": "injector-timeout",
         "ok": _injector_timeout_ok(root / "scripts" / "injector.mjs"),
         "label": "注入器允许等到 180 秒"},
        {"id": "home-utility-part",
         "ok": "home-utility" in pol_text and _ADDPART_HOME.strip() in js_text,
         "label": "主题编辑器可调首页项目帽"},
        {"id": "composer-shell-part",
         "ok": "composer-shell" in pol_text and _ADDPART_SHELL.strip() in js_text,
         "label": "主题编辑器可调输入框外壳"},
        {"id": "validator-parts",
         "ok": (not validator.is_file()) or (
             '"home-utility"' in val_text and '"composer-shell"' in val_text),
         "label": "官方校验器认得首页帽和输入框外壳"},
    ]
    return {
        "root": str(root),
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "present": css.is_file() or selectors.is_file(),
    }


def apply_root(root: Path) -> dict[str, Any]:
    changed = []
    if _patch_selectors_json(root / "assets" / "selectors.json"):
        changed.append("selectors.json")
    if _patch_renderer(root / "assets" / "renderer-inject.js"):
        changed.append("renderer-inject.js")
    if _patch_policy_parts(root / "assets" / "safe-css-policy.json"):
        changed.append("safe-css-policy.json")
    if _patch_validator_parts(root / "assets" / "safe-css-validator.mjs"):
        changed.append("safe-css-validator.mjs")
    if _patch_css(root / "assets" / "dream-skin.css"):
        changed.append("dream-skin.css")
    if _patch_managed_chrome(root / "scripts" / "config-utf8.ps1"):
        changed.append("config-utf8.ps1")
    if _patch_injector_timeout(root / "scripts" / "injector.mjs"):
        changed.append("injector.mjs")
    if _patch_start_wait(root / "scripts" / "start-dream-skin.ps1"):
        changed.append("start-dream-skin.ps1")
    if _patch_start_wait(root / "scripts" / "verify-dream-skin.ps1"):
        changed.append("verify-dream-skin.ps1")
    report = inspect_root(root)
    report["changed"] = changed
    return report


def apply_all(roots: list[Path]) -> dict[str, Any]:
    reports = [apply_root(root) for root in roots]
    live = [item for item in reports if item.get("present")]
    if not live:
        return {
            "ok": True,
            "skipped": True,
            "reason": "未安装 Dream Skin 引擎",
            "roots": reports,
        }
    return {
        "ok": all(item["ok"] for item in live),
        "patched": any(item.get("changed") for item in live),
        "roots": reports,
    }


def inspect_all(roots: list[Path]) -> dict[str, Any]:
    reports = [inspect_root(root) for root in roots]
    live = [item for item in reports if item.get("present")]
    if not live:
        return {
            "ok": True,
            "skipped": True,
            "reason": "未安装 Dream Skin 引擎",
            "roots": reports,
        }
    return {
        "ok": all(item["ok"] for item in live),
        "roots": reports,
    }


def touch_active_theme() -> bool:
    path = (Path(os.environ.get("LOCALAPPDATA", "")) / "CodexDreamSkin"
            / "active-theme" / "theme.css")
    if not path.is_file():
        return False
    os.utime(path, None)
    return True
