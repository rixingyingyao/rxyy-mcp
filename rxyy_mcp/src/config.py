"""开源壳用的精简配置：只留数据目录与可选模型服务，不含公司/账号池常量。"""
from __future__ import annotations

import os
from pathlib import Path


def _app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    raw = (os.getenv("RXYY_DATA_DIR") or os.getenv("RXYY_MCP_DATA_DIR") or "").strip()
    if raw and Path(raw).is_dir():
        return Path(raw)
    root = _app_root()
    try:
        for line in (root / "data-dir.txt").read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().strip('"')
            if line and not line.startswith("#") and Path(line).is_dir():
                return Path(line)
    except (OSError, UnicodeDecodeError):
        pass
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    fallback = local / "rxyy-mcp" / "data"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


APP_ROOT: Path = _app_root()
DATA_DIR: Path = _data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

BAILIAN_API_KEY: str = os.getenv("DASHSCOPE_API_KEY", "")
BAILIAN_BASE_URL: str = os.getenv(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
BAILIAN_MODEL: str = os.getenv("DASHSCOPE_MODEL", "qwen-plus")
DESKTOP_ROOT: str = os.getenv("RXYY_DESKTOP_ROOT", str(Path.home() / "Desktop"))
if os.path.isdir(r"D:\Desktop"):
    DESKTOP_ROOT = os.getenv("RXYY_DESKTOP_ROOT", r"D:\Desktop")
if os.path.isdir(r"D:\桌面"):
    DESKTOP_ROOT = os.getenv("RXYY_DESKTOP_ROOT", r"D:\桌面")
WORKFLOW_DB_PATH: Path = DATA_DIR / "workflow.db"


def desktop_root() -> Path:
    for cand in (os.getenv("RXYY_DESKTOP_ROOT"), DESKTOP_ROOT):
        cand = str(cand or "").strip()
        if cand and Path(cand).is_dir():
            return Path(cand)
    return Path(DESKTOP_ROOT)
