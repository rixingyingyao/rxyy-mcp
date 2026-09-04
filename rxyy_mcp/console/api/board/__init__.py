"""任务面板（团队看板）——BoardApi 混入。

前端（桌面壳与 webgw 远程）通过 window.pywebview.api.board_*(...) 调用；
Codex 原生技能走 webgw 的 POST /api/board_*（带控制台远程令牌）。
方案：docs/plans/2026-08-13-task-board-design.md
"""

from __future__ import annotations

import functools
from typing import Any

from .service import BoardService
from .storage import BoardStoreUnreadable


def _says_so_when_the_store_is_unreadable(fn):
    """面板库这一刻读不出来时，照实说给界面，别抛成一个没人接的异常。

    存储层已经把那次写拦住了（见 BoardStoreUnreadable：不拦的话，一次建卡就会把
    全队的卡按「只有我这一张」整份顶掉）。拦下来之后总得有人把话说出去——十几个
    board_* 入口共用这一处，将来新加的入口也自动带上，不必记得再写一遍 try。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BoardStoreUnreadable as exc:
            return {"ok": False, "error": str(exc), "code": "STORE_UNREADABLE"}
    return wrapper


class BoardApi:
    _board_service: BoardService | None = None

    @property
    def _board(self) -> BoardService:
        if self.__class__._board_service is None:
            self.__class__._board_service = BoardService()
        return self.__class__._board_service

    def board_list(self, filters: Any = None) -> dict:
        return self._board.list_cards(filters)

    def board_get(self, card_id: Any = "") -> dict:
        return self._board.get_card(card_id)

    def board_create(self, fields: Any = None) -> dict:
        return self._board.create_card(fields)

    def board_claim(self, card_id: Any = "", args: Any = None) -> dict:
        return self._board.claim_card(card_id, args)

    def board_move(self, card_id: Any = "", args: Any = None) -> dict:
        return self._board.move_card(card_id, args)

    def board_comment(self, card_id: Any = "", args: Any = None) -> dict:
        return self._board.comment_card(card_id, args)

    def board_dispatch_sessions(self) -> dict:
        """能把卡派给谁（hub 上还连着的 agent tab）。"""
        return self._board.dispatch_targets()

    def board_dispatch_card(self, card_id: Any = "", args: Any = None) -> dict:
        """把卡递给某个 agent；卡的归属仍由它自己 claim 产生。"""
        return self._board.dispatch_card(card_id, args)

    def board_dispatch_cards(self, card_ids: Any = None, args: Any = None) -> dict:
        """把多张卡合成一条消息递给同一个 agent；归属仍由它自己 claim。"""
        return self._board.dispatch_cards(card_ids, args)

    def board_review_note(self, card_id: Any = "", args: Any = None) -> dict:
        """别的 agent 先验一道（只写结论，不改状态，仍等 rxyy 点头）。"""
        return self._board.review_note(card_id, args)

    def board_review(self, card_id: Any = "", args: Any = None) -> dict:
        return self._board.review_card(card_id, args)

    def board_release(self, card_id: Any = "", args: Any = None) -> dict:
        return self._board.release_card(card_id, args)

    def board_archive(self, card_id: Any = "", args: Any = None) -> dict:
        """归档/取消归档（软删除：面板上收起来，数据保留）。"""
        return self._board.archive_card(card_id, args)

    def board_note_activity(self, conversation_id: Any = "", text: Any = "") -> dict:
        return self._board.note_activity(conversation_id, text)

    # 会话级聚合：hub 的钩子只发事实（收工了/失联了），版本与规则在这边串行处理
    def board_finish_session(self, conversation_id: Any = "", note: Any = "") -> dict:
        return self._board.finish_session(conversation_id, note)

    def board_release_session(self, conversation_id: Any = "", reason: Any = "") -> dict:
        return self._board.release_session(conversation_id, reason)


for _name, _fn in list(vars(BoardApi).items()):
    if _name.startswith("board_") and callable(_fn):
        setattr(BoardApi, _name, _says_so_when_the_store_is_unreadable(_fn))
del _name, _fn


__all__ = ["BoardApi"]
