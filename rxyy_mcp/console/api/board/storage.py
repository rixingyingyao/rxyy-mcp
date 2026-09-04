"""任务面板（团队看板）的本地持久化：原子写 ``.board.json``。

与任务安排站（taskstage）同一套落盘手法：线程锁 + 临时文件 + os.replace。
卡片带乐观锁 version，事件流水内嵌在卡片里（封顶截断，防止单卡撑爆文件）。
存储独立于 taskstage，互不影响；数据在 rxyy tools 数据目录的 board 子目录。
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

STORE_NAME = ".board.json"

MAX_CARDS = 1000
MAX_EVENTS_PER_CARD = 200
MAX_TEXT = 20000

VALID_STATUSES = ("backlog", "todo", "in_progress", "in_review", "done", "blocked")
VALID_PRIORITIES = ("high", "normal", "low")
VALID_AGENT_TYPES = ("cursor", "codex", "other")
EVENT_KINDS = ("create", "claim", "move", "comment", "zt", "release", "review",
               "review_note", "dispatch", "update", "archive")


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return Path(base) / "rxyy-mcp" / "board" if base else Path.home() / "rxyy-board"


class BoardStoreUnreadable(OSError):
    """库文件在，但这一次读不出来（半截 JSON、被独占、被 DLP 插了一手）。

    绝不能把它当成「面板是空的」：紧接着那次建卡会把一整份全量按「只有我这一张」
    落回去，全队的卡（待领/处理中/待验收/已交付）一次抹平。
    """


def _refuse_writing_the_real_board() -> bool:
    """测试里不许写真库。

    2026-08-13 实案：工单派活新加了「顺手建卡」，而那条测试只打桩了 hub 与记录存储，
    建卡直接 new 了真 BoardService——一跑就往 rxyy 的真面板落了 6 张 [SCB-*] 垃圾卡，
    只能备份后手改文件清掉。面板是他在外面唯一的真相来源，混进测试数据比功能缺失更坏，
    所以这道闸放在最底层：只要在 pytest 里跑、且写的正好是真数据目录，直接炸给你看，
    而不是安静地写进去。测试要用就传个 tmp 目录（现有测试本来就都这么写）。
    """
    return "PYTEST_CURRENT_TEST" in os.environ


def _now() -> float:
    return time.time()


def normalize_priority(value: Any) -> str:
    value = str(value or "").strip().lower()
    # 前端可能给中文，收进来统一成英文枚举
    zh = {"高": "high", "中": "normal", "低": "low"}
    value = zh.get(value, value)
    return value if value in VALID_PRIORITIES else "normal"


def normalize_status(value: Any) -> str:
    value = str(value or "").strip().lower()
    return value if value in VALID_STATUSES else "todo"


def normalize_labels(value: Any) -> list:
    if isinstance(value, str):
        parts = [value]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = []
    labels: list = []
    for part in parts:
        text = str(part or "").strip()
        if text and text not in labels:
            labels.append(text[:24])
        if len(labels) >= 12:
            break
    return labels


def clip_text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def trim_events(events: list) -> list:
    """事件封顶。超了先扔最老的进度（zt），扔光了才动别的。

    进度是 agent 每隔几分钟报一次的流水账，领卡、评论、交付、验收才是这张卡的
    审计链。一刀切按时间截断的话，一个话痨 agent 干半天就能把「谁领的、他说交付了
    什么」全顶出去，卡上只剩一串「developing…」。
    """
    if len(events) <= MAX_EVENTS_PER_CARD:
        return events
    keep = list(events)
    drop = len(keep) - MAX_EVENTS_PER_CARD
    for index, item in enumerate(keep):
        if drop <= 0:
            break
        if item.get("kind") == "zt":
            keep[index] = None
            drop -= 1
    keep = [e for e in keep if e is not None]
    return keep[-MAX_EVENTS_PER_CARD:]


def normalize_review(item: Any):
    """规整复核结论：别的 agent 替 rxyy 先验一道的结果，不改状态、只作参考。"""
    if not isinstance(item, dict) or not item.get("by"):
        return None
    verdict = str(item.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "fail"):
        return None
    return {
        "verdict": verdict,
        "by": str(item["by"])[:64],
        "tab_name": str(item.get("tab_name") or "")[:64],
        "text": clip_text(item.get("text"), 2000),
        "ts": float(item.get("ts") or 0) or _now(),
    }


def normalize_assignee(item: Any):
    """规整认领人；没有 conversation_id 一律当无主（None）。"""
    if not isinstance(item, dict) or not item.get("conversation_id"):
        return None
    agent_type = str(item.get("agent_type") or "").strip().lower()
    if agent_type not in VALID_AGENT_TYPES:
        agent_type = "other"
    return {
        "conversation_id": str(item["conversation_id"])[:64],
        "agent_type": agent_type,
        "tab_name": str(item.get("tab_name") or "")[:64],
    }


class BoardStorage:
    """读写看板卡片（线程安全 + 原子落盘 + 乐观锁自增）。"""

    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.store_path = self.data_dir / STORE_NAME
        self._lock = threading.RLock()
        # 见 _refuse_writing_the_real_board：测试里指到真库就当场拦下
        self._guarded = (_refuse_writing_the_real_board()
                         and self.data_dir == default_data_dir())

    # ---- 底层读写 ----------------------------------------------------------
    def _read(self, strict: bool = False) -> dict:
        """strict=True 用于「读出来是要写回去的」那条路：这时读失败必须炸出来，
        不能和「面板本来就是空的」共用一个 {"cards": []}——见 BoardStoreUnreadable。"""
        try:
            raw = self.store_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {"cards": []}  # 还没建过库，这才是真的空
        except (OSError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError 不是 OSError：这台机器的企业 DLP（TSD）会按扩展名
            # 透明加密，被它插一手读出来的就不是 utf-8，漏掉这一档照样清库
            if strict:
                raise BoardStoreUnreadable(
                    "面板库暂时读不出来（{}），本次没有写入，库文件原样保留：{}".format(
                        exc, self.store_path)) from exc
            return {"cards": []}
        try:
            value = json.loads(raw)
        except (ValueError, TypeError) as exc:
            if strict:
                raise BoardStoreUnreadable(
                    "面板库内容不是完整的 JSON，本次没有写入，库文件原样保留：{}".format(
                        self.store_path)) from exc
            return {"cards": []}
        # 合法 JSON 但形状不对：它本来就没装卡，当空库处理即可
        if not isinstance(value, dict) or not isinstance(value.get("cards"), list):
            return {"cards": []}
        return value

    def _write(self, value: dict) -> None:
        if self._guarded:
            raise RuntimeError(
                "测试里不许写真看板库（{}）。给 BoardStorage 传个 tmp 目录，"
                "或者把调到它的那条入口打桩——2026-08-13 就是这么往 rxyy 的真面板"
                "落了 6 张测试卡的。".format(self.store_path))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.store_path.with_name(
            "{}.tmp.{}.{}".format(self.store_path.name, os.getpid(), uuid.uuid4().hex)
        )
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(temporary, self.store_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    # ---- 规整 --------------------------------------------------------------
    def _clean_event(self, item: Any):
        if not isinstance(item, dict):
            return None
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in EVENT_KINDS:
            return None
        return {
            "ts": float(item.get("ts") or 0) or _now(),
            "kind": kind,
            "conversation_id": str(item.get("conversation_id") or "")[:64],
            "text": clip_text(item.get("text"), 4000),
        }

    def _clean_card(self, item: Any):
        if not isinstance(item, dict) or not item.get("id"):
            return None
        events = []
        for ev in item.get("events") or []:
            cleaned = self._clean_event(ev)
            if cleaned is not None:
                events.append(cleaned)
        events = trim_events(events)
        return {
            "id": str(item["id"]),
            "title": clip_text(item.get("title"), 200),
            "desc": clip_text(item.get("desc")),
            "project": clip_text(item.get("project"), 80),
            "priority": normalize_priority(item.get("priority")),
            "labels": normalize_labels(item.get("labels")),
            "status": normalize_status(item.get("status")),
            "assignee": normalize_assignee(item.get("assignee")),
            "review": normalize_review(item.get("review")),
            # 归档=软删除：面板上收起来，数据一个字不动（面板本来连删都没有，
            # 误建的卡只能手改 .board.json，这次就是这么清的）
            "archived": bool(item.get("archived")),
            "blocked_by": [str(x) for x in (item.get("blocked_by") or []) if str(x or "").strip()][:20],
            "version": int(item.get("version") or 1),
            "created_at": float(item.get("created_at") or 0) or _now(),
            "updated_at": float(item.get("updated_at") or 0) or _now(),
            "last_activity": clip_text(item.get("last_activity"), 200),
            "events": events,
        }

    # ---- 查询 --------------------------------------------------------------
    def list_cards(self, strict: bool = False) -> list:
        with self._lock:
            cards = []
            for item in self._read(strict=strict).get("cards", []):
                cleaned = self._clean_card(item)
                if cleaned is not None:
                    cards.append(cleaned)
            cards.sort(key=lambda c: -(c.get("updated_at") or 0))
            return cards

    def find(self, card_id: str):
        card_id = str(card_id or "")
        with self._lock:
            for card in self.list_cards():
                if card["id"] == card_id:
                    return card
            return None

    # ---- 写入 --------------------------------------------------------------
    def _save(self, cards: list) -> None:
        self._write({"cards": cards})

    def add_card(self, card: dict) -> dict:
        with self._lock:
            cards = self.list_cards(strict=True)
            if len(cards) >= MAX_CARDS:
                raise ValueError("卡片数量已达上限（{}）".format(MAX_CARDS))
            cards.append(card)
            self._save(cards)
            return copy.deepcopy(card)

    def upsert_card_replica(self, card: dict):
        """双活同步应用侧专用：按 id 原样落一张卡片副本（有则整体替换，无则插入）。

        与 add_card 的两个刻意不同：不查 MAX_CARDS（副本数量已被对端自己的上限
        约束，拒收只会让重放线停在这条事件上反复重试），且 updated_at/version
        由调用方给定——时间戳是 LWW 的定序依据，洗成本机时刻会把旧改动洗成新的
        （约定见 rxyy-mcp/sync_console.py 门头）。测试写真库的闸照走 _write。
        """
        cleaned = self._clean_card(card)
        if cleaned is None:
            raise ValueError("卡片副本缺 id，落不了")
        with self._lock:
            cards = self.list_cards(strict=True)
            for index, current in enumerate(cards):
                if current["id"] == cleaned["id"]:
                    cards[index] = cleaned
                    break
            else:
                cards.append(cleaned)
            self._save(cards)
            return copy.deepcopy(cleaned)

    def mutate(self, card_id: str, expect_version, fn):
        """带乐观锁的原地修改：fn(card) 就地改 card，成功后 version+1 并落盘。

        expect_version 传 None 跳过版本校验（仅限追加评论这类不冲突的写）。
        返回 (card, error_code)：冲突/不存在时 card 为 None。
        """
        card_id = str(card_id or "")
        with self._lock:
            # 读不出来时别回 NOT_FOUND：卡明明在，那句话是在骗人，而且会把人引到
            # 「重建一张」——那一下才是真的把面板清了
            cards = self.list_cards(strict=True)
            for card in cards:
                if card["id"] != card_id:
                    continue
                if expect_version is not None and int(expect_version) != card["version"]:
                    return None, "VERSION_CONFLICT"
                result = fn(card)
                if result is not None:  # fn 返回错误码表示业务拒绝，不落盘
                    return None, str(result)
                card["version"] += 1
                card["updated_at"] = _now()
                card["events"] = trim_events(card.get("events") or [])
                self._save(cards)
                return copy.deepcopy(card), ""
            return None, "NOT_FOUND"


def new_card(fields: dict) -> dict:
    """把外来字段拼成一张全新卡（不落盘，落盘走 BoardStorage.add_card）。"""
    title = clip_text(fields.get("title"), 200)
    if not title:
        raise ValueError("卡片标题不能为空")
    now = _now()
    status = normalize_status(fields.get("status") or "todo")
    if status in ("in_progress", "in_review", "done"):
        status = "todo"  # 新卡不允许直接出生在执行/完成列
    return {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "desc": clip_text(fields.get("desc")),
        "project": clip_text(fields.get("project"), 80),
        "priority": normalize_priority(fields.get("priority")),
        "labels": normalize_labels(fields.get("labels")),
        "status": status,
        "assignee": None,
        "review": None,
        "archived": False,
        "blocked_by": [str(x) for x in (fields.get("blocked_by") or []) if str(x or "").strip()][:20],
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "last_activity": "",
        "events": [{"ts": now, "kind": "create", "conversation_id":
                    str(fields.get("conversation_id") or "console")[:64], "text": "建卡"}],
    }


__all__ = [
    "BoardStorage", "BoardStoreUnreadable", "new_card", "default_data_dir",
    "VALID_STATUSES", "VALID_PRIORITIES", "VALID_AGENT_TYPES",
    "normalize_priority", "normalize_status", "normalize_labels",
    "normalize_assignee", "normalize_review", "clip_text", "trim_events",
    "STORE_NAME", "MAX_CARDS", "MAX_EVENTS_PER_CARD",
]
