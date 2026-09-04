"""阿里百炼（DashScope 兼容模式，OpenAI 协议）最小客户端。

只用标准库 urllib，避免依赖 openai 包（Anaconda 里有没有都能跑）。
key / BaseURL / 模型 的优先级：设置页（kv_config）> 环境变量 > 内置默认。
"""
from __future__ import annotations

import json
import urllib.request as _u

from src.config import BAILIAN_API_KEY, BAILIAN_BASE_URL, BAILIAN_MODEL


def runtime_cfg() -> dict:
    """当前生效的百炼配置（设置页可改，存 kv_config，即改即生效，无需重启）。"""
    try:
        from api.workflow_db import wfdb
        kv = wfdb()
        return {
            "api_key": (kv.kv_get("bailian_api_key") or BAILIAN_API_KEY).strip(),
            "base_url": (kv.kv_get("bailian_base_url") or BAILIAN_BASE_URL).strip(),
            "model": (kv.kv_get("bailian_model") or BAILIAN_MODEL).strip(),
        }
    except Exception:  # noqa: BLE001  DB 不可用时退回内置默认
        return {"api_key": BAILIAN_API_KEY, "base_url": BAILIAN_BASE_URL,
                "model": BAILIAN_MODEL}


def _is_timeout(exc: BaseException) -> bool:
    """读超时判定：urlopen 直接抛 TimeoutError，也可能被 URLError 包一层。"""
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return True
    return "timed out" in str(reason if reason is not None else exc).lower()


def chat(messages: list[dict], model: str = "", temperature: float = 0.3,
         max_tokens: int = 2000, timeout: int = 90, timeout_retries: int = 1) -> dict:
    """非流式对话补全。返回 {ok, content, model, error}。

    qwen3.7-max 等混合思考模型默认开思考（慢且费 token），日报场景一律关闭
    （enable_thinking=false，实测 qwen3.7-max 0.8s vs 开思考 1.4s+）；
    仅思考模型（*-preview）或老模型不认该参数时自动去掉重试一次。

    读超时也重试：08-04 生成日报实测同一份素材第一次 90s 读超时、原样重发 17s 就过，
    而超时以前是一次就整单失败——用户在面板上只看到「生成失败」，全靠自己再点一次。
    """
    cfg = runtime_cfg()
    key = cfg["api_key"]
    if not key:
        return {"ok": False, "error": "未配置百炼 API Key（到「设置」页填写）"}
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": (model or cfg["model"]).strip(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "enable_thinking": False,
    }
    thinking_retry, timeouts_left = True, max(0, int(timeout_retries))
    while True:
        data = json.dumps(payload).encode("utf-8")
        req = _u.Request(url, data=data, method="POST", headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        })
        try:
            with _u.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
            return {"ok": True, "content": content, "model": payload["model"]}
        except _u.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                detail = ""
            if thinking_retry and exc.code == 400 and "enable_thinking" in payload:
                payload.pop("enable_thinking", None)
                thinking_retry = False
                continue
            return {"ok": False, "error": "HTTP %s: %s" % (exc.code, detail[:300])}
        except Exception as exc:  # noqa: BLE001
            if timeouts_left and _is_timeout(exc):
                timeouts_left -= 1
                continue
            return {"ok": False, "error": repr(exc)}
