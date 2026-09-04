"""pywebview JS API 桥接（开源壳：只挂通用模块）。"""
from __future__ import annotations

from typing import Any

from api.board import BoardApi
from api.codexcfg_api import CodexCfgApi
from api.envcheck_api import EnvCheckApi
from api.install_status import InstallStatusApi
from api.projects_api import ProjectsApi
from api.settings_api import SettingsApi

APP_NAME = "rxyy mcp"
APP_VERSION = "0.1.0"
APP_STAGE = "opensource"


class Api(ProjectsApi, BoardApi, EnvCheckApi, InstallStatusApi,
          SettingsApi, CodexCfgApi):
    def __init__(self) -> None:
        self._window: Any = None

    def bind(self, window: Any) -> None:
        self._window = window

    def app_info(self) -> dict[str, Any]:
        return {"name": APP_NAME, "version": APP_VERSION, "stage": APP_STAGE}

    def ping(self) -> str:
        return "pong"
