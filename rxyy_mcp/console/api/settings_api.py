"""设置页：模型服务 + 桌面扫描根。不含退款/公司日报字段。"""
from __future__ import annotations

import os
from typing import Any

from src.config import (
    BAILIAN_API_KEY,
    BAILIAN_BASE_URL,
    BAILIAN_MODEL,
    DESKTOP_ROOT,
)
from api.workflow_db import wfdb

_MODEL_CHOICES = [
    {"id": "qwen-plus", "label": "qwen-plus"},
    {"id": "qwen-max", "label": "qwen-max"},
    {"id": "qwen-flash", "label": "qwen-flash"},
]


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 12:
        return key[:4] + "…"
    return key[:8] + "…" + key[-4:]


class SettingsApi:
    def settings_get(self) -> dict[str, Any]:
        kv = wfdb()
        bailian_eff = (kv.kv_get("bailian_api_key") or BAILIAN_API_KEY).strip()
        return {
            "ok": True,
            "bailian_api_key": kv.kv_get("bailian_api_key") or "",
            "bailian_api_key_default": (
                _mask(BAILIAN_API_KEY) + "（环境变量）" if BAILIAN_API_KEY else ""
            ),
            "bailian_base_url": kv.kv_get("bailian_base_url") or "",
            "bailian_base_url_default": BAILIAN_BASE_URL,
            "bailian_model": kv.kv_get("bailian_model") or "",
            "bailian_model_default": BAILIAN_MODEL,
            "model_choices": _MODEL_CHOICES,
            "desktop_root": kv.kv_get("desktop_root") or "",
            "desktop_root_default": DESKTOP_ROOT,
            "keys_ready": {"bailian": bool(bailian_eff)},
        }

    def settings_set(self, fields: dict) -> dict[str, Any]:
        allowed = {
            "bailian_api_key", "bailian_base_url", "bailian_model", "desktop_root",
        }
        vals = {
            k: str(v or "").strip()
            for k, v in (fields or {}).items()
            if k in allowed
        }
        if vals.get("desktop_root") and not os.path.isdir(vals["desktop_root"]):
            return {"ok": False, "error": "扫描根目录不存在：" + vals["desktop_root"]}
        kv = wfdb()
        for k, v in vals.items():
            kv.kv_set(k, v)
        return {"ok": True, "saved": len(vals)}

    def settings_test_bailian(self) -> dict[str, Any]:
        import time

        from api.bailian import chat, runtime_cfg

        cfg = runtime_cfg()
        t0 = time.time()
        r = chat([{"role": "user", "content": "回复两个字：正常"}], max_tokens=64, timeout=45)
        return {
            "ok": bool(r),
            "model": (cfg or {}).get("model"),
            "elapsed": round(time.time() - t0, 2),
        }
