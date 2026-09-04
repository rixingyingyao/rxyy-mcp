"""rxyy-mcp 机器态（config.json / .sessions.json / sdk 注册表）的落盘目录。

机器态过去跟着代码放在 APP_DIR，打包版的 APP_DIR 落在 dist\\rxyy-tools\\_internal\\rxyy-mcp，
而 build.ps1 每次都 `Remove-Item -Recurse dist`——07-29 事故：换成打包版后配置退回空默认、
13 个会话 tab 连同消息一起从控制台消失。故按此顺序把机器态与代码目录解耦：

1. 环境变量 RXYY_MCP_DATA_DIR
2. APP_DIR/datadir.txt 里写的路径（打包时指回开发机源码目录；换电脑该路径不存在则忽略）
3. 打包版：exe 旁的 data/rxyy-mcp（跟着文件夹走，符合 README 「数据在 data\\」的约定）
4. 源码运行：APP_DIR 本身
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
POINTER_NAME = "datadir.txt"
# 只搬机器态，日志/锁/崩溃记录仍留在各自实例的 APP_DIR
MIGRATE_NAMES = ("config.json", ".sessions.json", ".sdk-agents.json")


def _from_env() -> Path | None:
    raw = (os.environ.get("RXYY_MCP_DATA_DIR") or "").strip()
    return Path(raw) if raw else None


def _from_pointer() -> Path | None:
    try:
        # utf-8-sig：指针由 build.ps1 用 Out-File 写出，Windows PowerShell 会带 BOM
        lines = (APP_DIR / POINTER_NAME).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip().strip('"')
        if line and not line.startswith("#"):
            target = Path(line)
            return target if target.is_dir() else None
    return None


def _frozen_default() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent / "data" / "rxyy-mcp"


def _seed_from_app_dir(data_dir: Path) -> None:
    """首次切到新目录时把旧机器态搬过去，避免升级/换目录当场丢配置和 tab。"""
    if data_dir == APP_DIR:
        return
    for name in MIGRATE_NAMES:
        src, dst = APP_DIR / name, data_dir / name
        if src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass


def resolve_data_dir() -> Path:
    for cand in (_from_env(), _from_pointer(), _frozen_default(), APP_DIR):
        if cand is None:
            continue
        try:
            cand.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        _seed_from_app_dir(cand)
        return cand
    return APP_DIR


DATA_DIR = resolve_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
