"""任务面板的业务规则：领卡/挪列/评论/验收/退回。

规矩（与 docs/plans/2026-08-13-task-board-design.md 一致）：
- backlog=想法池（不许领），todo=批准待领；
- 领卡：卡必须无主、在 todo、依赖卡全 done；领了自动挂 in_progress + 绑会话；
- 挪列：执行者只能动自己名下的卡（in_progress→in_review / 回 todo=放弃）；
  控制台（conversation_id="console"）可做规划挪动（todo↔backlog↔blocked）；
- done 只有 board_review 能进（控制台验收按钮），agent 无权自我完结；
- 全部写操作走乐观锁 version，冲突返回 VERSION_CONFLICT 不落盘。
"""

from __future__ import annotations

import time
from typing import Any

from .storage import (
    VALID_STATUSES,
    BoardStorage,
    clip_text,
    new_card,
    normalize_assignee,
    normalize_review,
    normalize_status,
)

CONSOLE_ID = "console"  # 控制台 UI 的 conversation_id 记号


def oa_bound_board_names(projects: list[Any] | None) -> list[str]:
    """已绑 OA 的启用仓挂到看板下拉：有业务名用业务名，否则用仓库名。

    不建卡、不写 .board.json。验收看板的「项目」本来就只是卡上的字符串；
    不把已绑仓挂上去，新开的心理项目之类永远不会出现在下拉里。
    未绑 / 停用的仓不挂——那些是自用工具或还没指归属。
    """
    names: list[str] = []
    seen: set[str] = set()
    for p in projects or []:
        if not getattr(p, "enabled", 0):
            continue
        if not str(getattr(p, "oa_project_id", "") or "").strip():
            continue
        name = (str(getattr(p, "biz_name", "") or "").strip()
                or str(getattr(p, "name", "") or "").strip())
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def load_oa_bound_board_names() -> list[str]:
    try:
        from api.workflow_db import wfdb
        return oa_bound_board_names(wfdb().list_projects())
    except Exception:  # noqa: BLE001  看板不能因为项目管理库读失败整页空白
        return []


def _event(kind: str, conversation_id: str, text: str) -> dict:
    return {"ts": time.time(), "kind": kind,
            "conversation_id": str(conversation_id or "")[:64],
            "text": clip_text(text, 4000)}


class BoardService:
    def __init__(self, storage: BoardStorage | None = None,
                 project_names_loader=None):
        self.storage = storage or BoardStorage()
        # None = 现读项目管理库；测试传入假名单，避免碰到本机 workflow.db
        self._project_names_loader = project_names_loader

    def _board_projects(self, cards: list[dict]) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for card in cards:
            name = str(card.get("project") or "").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        loader = (self._project_names_loader if self._project_names_loader is not None
                  else load_oa_bound_board_names)
        try:
            extra = loader() or []
        except Exception:  # noqa: BLE001
            extra = []
        for name in extra:
            name = str(name or "").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        names.sort()
        return names

    # ---- 查询 ---------------------------------------------------------------
    def list_cards(self, filters: dict | None = None) -> dict:
        filters = filters if isinstance(filters, dict) else {}
        project = str(filters.get("project") or "").strip()
        status = str(filters.get("status") or "").strip()
        all_cards = self.storage.list_cards()
        projects = self._board_projects(all_cards)
        cards = all_cards
        # 归档的默认收起来：面板是 rxyy 在外面唯一的真相来源，作废/误建的卡不该占位
        if not filters.get("archived"):
            cards = [c for c in cards if not c.get("archived")]
        if project:
            cards = [c for c in cards if c["project"] == project]
        if status:
            cards = [c for c in cards if c["status"] == status]
        # 列表不带 events 正文，省流量；详情走 get_card
        slim = []
        for c in cards:
            item = {k: v for k, v in c.items() if k != "events"}
            item["event_count"] = len(c.get("events") or [])
            slim.append(item)
        return {"ok": True, "cards": slim, "projects": projects}

    def get_card(self, card_id: str) -> dict:
        card = self.storage.find(card_id)
        if card is None:
            return {"ok": False, "error": "卡片不存在", "code": "NOT_FOUND"}
        events = card.pop("events", [])
        return {"ok": True, "card": card, "events": events}

    def archive_card(self, card_id: str, args: dict | None = None) -> dict:
        """归档/取消归档：软删除，数据一个字不动，随时能翻回来。

        面板一直没有「删」——误建的卡（比如测试漏打桩落进来的那 6 张）只能停服务、
        备份、手改 .board.json。那不是 rxyy 在手机上能干的事，所以给一个收起来的开关；
        真要彻底删仍然只能手改文件，那是有意的：数据不轻易消失。
        """
        args = args if isinstance(args, dict) else {}
        want = args.get("archived")
        want = True if want is None else bool(want)
        conv = str(args.get("conversation_id") or CONSOLE_ID).strip() or CONSOLE_ID
        reason = clip_text(args.get("reason"), 200)

        def apply(card):
            if bool(card.get("archived")) == want:
                return "NO_CHANGE"
            card["archived"] = want
            card["events"].append(_event(
                "archive", conv,
                ("归档：收起不再占列" if want else "取消归档：放回面板") +
                ("（{}）".format(reason) if reason else "")))
            card["last_activity"] = "已归档" if want else "已取消归档"
            return None

        card, code = self.storage.mutate(card_id, args.get("version"), apply)
        if card is None:
            if code == "NO_CHANGE":
                return {"ok": True, "changed": False,
                        "msg": "本来就是" + ("归档" if want else "未归档") + "状态"}
            return {"ok": False, "error": _err_text(code), "code": code}
        return {"ok": True, "changed": True,
                "msg": "已归档（数据还在，随时能翻回来）" if want else "已放回面板",
                "card": {k: v for k, v in card.items() if k != "events"}}

    # ---- 建卡 ---------------------------------------------------------------
    def create_card(self, fields: dict | None = None) -> dict:
        fields = fields if isinstance(fields, dict) else {}
        try:
            card = new_card(fields)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "code": "INVALID"}
        known = {c["id"] for c in self.storage.list_cards()}
        missing = [x for x in card["blocked_by"] if x not in known]
        if missing:
            return {"ok": False, "code": "INVALID",
                    "error": "依赖卡不存在：{}".format("、".join(missing))}
        self.storage.add_card(card)
        slim = {k: v for k, v in card.items() if k != "events"}
        return {"ok": True, "card": slim}

    # ---- 领卡 ---------------------------------------------------------------
    def claim_card(self, card_id: str, args: dict | None = None) -> dict:
        args = args if isinstance(args, dict) else {}
        conv = str(args.get("conversation_id") or "").strip()
        if not conv or conv == CONSOLE_ID:
            return {"ok": False, "error": "领卡必须带 agent 的 conversation_id", "code": "INVALID"}
        assignee = {
            "conversation_id": conv,
            "agent_type": str(args.get("agent_type") or "other"),
            "tab_name": str(args.get("tab_name") or ""),
        }
        done_ids = {c["id"] for c in self.storage.list_cards() if c["status"] == "done"}

        def apply(card):
            pending = [x for x in card["blocked_by"] if x not in done_ids]
            if pending:
                return "BLOCKED"
            if card["status"] == "todo":
                if card["assignee"]:
                    return "CLAIMED"
                card["status"] = "in_progress"
                card["assignee"] = normalize_assignee(assignee)
                card["last_activity"] = "已领卡"
                card["events"].append(_event(
                    "claim", conv, "领卡（{}）".format(assignee["agent_type"])))
                return None
            if card["status"] == "in_review":
                # 待验收还能拍给 agent 把没做完的做完：接手后回到处理中，旧复核作废
                card["status"] = "in_progress"
                card["assignee"] = normalize_assignee(assignee)
                card["review"] = None
                card["last_activity"] = "接手待验收返工"
                card["events"].append(_event(
                    "claim", conv, "接手待验收卡返工（{}）".format(assignee["agent_type"])))
                return None
            return "CLAIMED" if card["assignee"] else "NOT_CLAIMABLE"

        # 领卡刻意不校验 version：它只是对「认领人」字段的一次原子 test-and-set，
        # 不覆盖任何人的正文。带上版本号的话，两个 agent 同时抢会收到「版本冲突，
        # 请刷新重试」——于是抢输的那个原地重试打转，而它本该听见「已被人领走」
        # 然后去领下一张。锁内的无主判定才是真正的护栏。
        card, code = self.storage.mutate(card_id, None, apply)
        if card is None:
            return {"ok": False, "error": _err_text(code), "code": code}
        return {"ok": True, "card": {k: v for k, v in card.items() if k != "events"}}

    # ---- 挪列 ---------------------------------------------------------------
    def move_card(self, card_id: str, args: dict | None = None) -> dict:
        args = args if isinstance(args, dict) else {}
        # 不给会话 ID 就是 rxyy 在控制台上手动拖卡：认成 console，走规划挪动那条分支。
        # 当成「某个匿名 agent」的话，它跟卡上任何一个认领人都对不上，
        # rxyy 自己拖自己的板会被判越权。
        conv = str(args.get("conversation_id") or CONSOLE_ID).strip()
        # 不认识的目标状态必须当场拒掉。normalize_status 会把它悄悄归一成 todo，
        # 于是接口回 ok、卡却纹丝不动——「成功了但什么都没发生」比报错难查得多
        # （08-13 实测：move 到 canceled 返回 ok，卡还在原地）
        raw_target = str(args.get("status") or "").strip().lower()
        if raw_target not in VALID_STATUSES:
            return {"ok": False, "code": "INVALID_STATUS",
                    "error": "没有『{}』这个状态，只能是：{}".format(
                        raw_target or "(空)", "、".join(VALID_STATUSES))}
        target = normalize_status(raw_target)
        if target == "done":
            return {"ok": False, "code": "USE_REVIEW",
                    "error": "done 只能由控制台验收按钮进（board_review）"}

        def apply(card):
            if conv == CONSOLE_ID:
                # 控制台规划挪动：不许把有主的执行中卡硬拖走
                if card["status"] == "in_progress" and target not in ("in_review",):
                    return "IN_PROGRESS_LOCKED"
                if target == "todo":
                    card["assignee"] = None
            else:
                owner = (card.get("assignee") or {}).get("conversation_id")
                if owner != conv:
                    return "NOT_OWNER"
                if card["status"] == "in_progress" and target == "in_review":
                    pass  # 交付
                elif card["status"] == "in_progress" and target == "todo":
                    card["assignee"] = None  # 主动放弃
                elif card["status"] == "in_review" and target == "in_progress":
                    pass  # 撤回返工
                else:
                    return "INVALID_MOVE"
            note = clip_text(args.get("note"), 500)
            card["events"].append(_event("move", conv or CONSOLE_ID,
                                         "{} → {}{}".format(card["status"], target,
                                                            ("：" + note) if note else "")))
            # 离开待验收就把复核结论作废：返工之后那条「复核通过」说的已经不是
            # 现在这版东西了，留着只会误导下一个看板的人
            if target != "in_review":
                card["review"] = None
            card["status"] = target
            card["last_activity"] = "挪到 " + target
            return None

        card, code = self.storage.mutate(card_id, args.get("version"), apply)
        if card is None:
            return {"ok": False, "error": _err_text(code), "code": code}
        return {"ok": True, "card": {k: v for k, v in card.items() if k != "events"}}

    # ---- 评论 / 进度 ---------------------------------------------------------
    def comment_card(self, card_id: str, args: dict | None = None) -> dict:
        args = args if isinstance(args, dict) else {}
        body = clip_text(args.get("body"), 4000)
        if not body:
            return {"ok": False, "error": "评论内容不能为空", "code": "INVALID"}
        conv = str(args.get("conversation_id") or CONSOLE_ID).strip()

        def apply(card):
            card["events"].append(_event("comment", conv, body))
            card["last_activity"] = body[:80]
            return None

        card, code = self.storage.mutate(card_id, None, apply)
        if card is None:
            return {"ok": False, "error": _err_text(code), "code": code}
        return {"ok": True, "card": {k: v for k, v in card.items() if k != "events"}}

    def note_activity(self, conversation_id: str, text: str) -> dict:
        """zt 进度上报的挂点：把一行活动写到该会话名下所有执行中的卡（P4 用）。

        连续的进度会折叠成一条：agent 干一个小时能报几十次，而每张卡的事件是有
        上限的，一路追加会把领卡、评论、交付这些真正要留痕的记录挤出去。进度关心
        的是「此刻在干什么」，所以末条还是同一个会话的进度时就地替换。
        """
        conv = str(conversation_id or "").strip()
        text = clip_text(text, 200)
        if not conv or not text:
            return {"ok": False, "error": "参数不全", "code": "INVALID"}
        touched = []
        for card in self.storage.list_cards():
            if card["status"] != "in_progress":
                continue
            if (card.get("assignee") or {}).get("conversation_id") != conv:
                continue

            def apply(c):
                events = c["events"]
                last = events[-1] if events else None
                if last and last["kind"] == "zt" and last["conversation_id"] == conv:
                    events[-1] = _event("zt", conv, text)
                else:
                    events.append(_event("zt", conv, text))
                c["last_activity"] = text[:80]
                return None

            done, _ = self.storage.mutate(card["id"], None, apply)
            if done is not None:
                touched.append(card["id"])
        return {"ok": True, "touched": touched}

    # ---- 派活：把卡递给某个在线 agent ---------------------------------------
    # 递过去而已，**卡的归属仍然只能由它自己 claim 产生**。直接把卡改成「它在做」
    # 的话，万一它没收到、或者当场就死了，面板上就挂着一个从没开工的人名，rxyy
    # 看到的是假进度——那比没有派活功能更坏。
    def dispatch_targets(self) -> dict:
        """能派给谁：hub 上还连着的 agent tab。"""
        try:
            from api.taskstage.core import HubClient, normalize_sessions
            sessions = normalize_sessions(HubClient().state())
        except ConnectionError as exc:
            return {"ok": False, "code": "HUB_OFFLINE", "error": str(exc), "sessions": []}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": "HUB_ERROR", "error": repr(exc), "sessions": []}
        live = [s for s in sessions if s.get("connected")]
        return {"ok": True, "sessions": live}

    def dispatch_card(self, card_id: str, args: dict | None = None) -> dict:
        reply = self.dispatch_cards([card_id], args)
        if reply.get("ok") and reply.get("cards"):
            reply["card"] = reply["cards"][0]
        return reply

    def dispatch_cards(self, card_ids, args: dict | None = None) -> dict:
        """把一张或多张卡递给同一个在线 agent。多张合成一条消息。

        只递过去，**不改归属**——跟单张派一样，领卡还得对方自己 claim。
        已完成/已归档的跳过；一张都派不出去才失败，避免半批写进流水、半批没发出去。
        """
        args = args if isinstance(args, dict) else {}
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            return {"ok": False, "error": "要指定派给哪个 agent", "code": "INVALID"}

        ids, seen = [], set()
        for raw in card_ids or []:
            cid = str(raw or "").strip()
            if cid and cid not in seen:
                seen.add(cid)
                ids.append(cid)
        if not ids:
            return {"ok": False, "error": "没有选中任何卡", "code": "INVALID"}

        # 目标得还连着。派给一个已经断线的 tab，消息就是石沉大海，而面板上却
        # 记了一笔「已派给它」——比派不出去更难查
        session, err = self._live_session(session_id)
        if err:
            return err

        cards, skipped = [], []
        for cid in ids:
            reply = self.get_card(cid)
            if not reply.get("ok"):
                skipped.append({"id": cid, "code": reply.get("code") or "NOT_FOUND",
                                "error": reply.get("error") or "卡片不存在"})
                continue
            card = reply["card"]
            reason = _dispatch_block_reason(card)
            if reason:
                skipped.append({"id": cid, "code": "NOT_DISPATCHABLE", "error": reason})
                continue
            cards.append(card)
        if not cards:
            first = skipped[0]
            return {"ok": False, "code": first["code"], "error": first["error"],
                    "skipped": skipped}

        note = clip_text(args.get("note"), 500)
        text = (_dispatch_batch_text(cards, note) if len(cards) > 1
                else _dispatch_text(cards[0], note))
        try:
            from api.taskstage.core import HubClient
            sent = HubClient().send(session_id, text)
        except ConnectionError as exc:
            return {"ok": False, "code": "HUB_OFFLINE", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": "HUB_ERROR", "error": repr(exc)}
        if not sent.get("ok"):
            return {"ok": False, "code": "SEND_FAILED",
                    "error": sent.get("error") or "hub 没能把消息递出去"}

        tab = (clip_text(args.get("tab_name"), 64)
               or str((session or {}).get("name") or "")[:64]
               or session_id[:8])
        extra = "（批量 {} 张）".format(len(cards)) if len(cards) > 1 else ""
        event_text = ("已派给 {}，等它自己领".format(tab) + extra
                      + ("：" + note if note else ""))
        activity = "已派给 " + tab + "，待领"
        slim = []
        for card in cards:
            def apply(c, _text=event_text, _act=activity):
                c["events"].append(_event("dispatch", CONSOLE_ID, _text))
                c["last_activity"] = _act
                return None

            done, _code = self.storage.mutate(card["id"], None, apply)
            if done is not None:
                slim.append({k: v for k, v in done.items() if k != "events"})

        msg = "已派 {} 张给 {}".format(len(slim), tab)
        if len(cards) > 1:
            msg += "（合成一条）"
        if skipped:
            msg += "，跳过 {} 张".format(len(skipped))
        return {"ok": True, "qid": sent.get("qid") or "", "msg": msg,
                "sent": [c["id"] for c in slim], "skipped": skipped, "cards": slim}

    def _live_session(self, session_id: str):
        targets = self.dispatch_targets()
        if not targets.get("ok"):
            return None, {"ok": False, "code": targets.get("code") or "HUB_ERROR",
                          "error": targets.get("error") or "拿不到在线 agent 列表"}
        online = {s["id"]: s for s in targets["sessions"]}
        if session_id not in online:
            return None, {"ok": False, "code": "TARGET_OFFLINE",
                          "error": "那个 agent 已经不在线了，换一个或等它回来"}
        return online[session_id], None

    # ---- 会话级聚合：给 hub 的钩子用 -------------------------------------
    # hub 手上只有 conversation_id，没有卡片 ID 更没有 version。让它「先 list 再
    # move」等于把看板的业务判断搬进全队的生命线，还平白多一跳、多一个竞态窗口。
    # 这两个方法让 hub 只管发事实（这个会话收工了 / 这个会话死了），版本与规则
    # 都在控制台这一个进程里串行解决。
    def finish_session(self, conversation_id: str, note: str = "") -> dict:
        """该会话收工：名下执行中的卡全部挪到待验收（仍然轮不到它自己 done）。"""
        conv = str(conversation_id or "").strip()
        if not conv or conv == CONSOLE_ID:
            return {"ok": False, "error": "要带 agent 的 conversation_id", "code": "INVALID"}
        moved = []
        for card in self._cards_of(conv, ("in_progress",)):
            reply = self.move_card(card["id"], {
                "status": "in_review", "conversation_id": conv,
                "note": clip_text(note, 500) or "收工信号", "version": card["version"]})
            if reply.get("ok"):
                moved.append(card["id"])
        return {"ok": True, "moved": moved}

    def release_session(self, conversation_id: str, reason: str = "") -> dict:
        """该会话没了（判死/掉线）：名下**还没干完**的卡退回待认领，别占着坑。

        只动 in_progress。待验收的卡活已经交了，就等 rxyy 点头，执行者是死是活跟
        它没关系了——退了等于把验收队列清空，人白干（08-13 实测：一次误判把两张
        已交付的卡打回了待认领）。
        """
        conv = str(conversation_id or "").strip()
        if not conv or conv == CONSOLE_ID:
            return {"ok": False, "error": "要带 agent 的 conversation_id", "code": "INVALID"}
        released = []
        for card in self._cards_of(conv, ("in_progress",)):
            reply = self.release_card(card["id"], {
                "conversation_id": CONSOLE_ID,  # 会话已经死了，由系统代为退卡
                "reason": clip_text(reason, 500) or "会话已失联，卡自动退回",
                "version": card["version"]})
            if reply.get("ok"):
                released.append(card["id"])
        return {"ok": True, "released": released}

    def _cards_of(self, conversation_id: str, statuses) -> list:
        return [c for c in self.storage.list_cards()
                if c["status"] in statuses
                and (c.get("assignee") or {}).get("conversation_id") == conversation_id]

    # ---- 复核 ---------------------------------------------------------------
    def review_note(self, card_id: str, args: dict | None = None) -> dict:
        """别的 agent 替 rxyy 先验一道，写个「过 / 不过 + 理由」。

        **不改状态**：卡照旧停在待验收等 rxyy 点头。这一层解决的是「他人在外面、
        一眼看不出这活到底成没成」，不是替他签字。所以只加结论，不动流转。
        """
        args = args if isinstance(args, dict) else {}
        conv = str(args.get("conversation_id") or "").strip()
        verdict = str(args.get("verdict") or "").strip().lower()
        body = clip_text(args.get("body"), 2000)
        if not conv or conv == CONSOLE_ID:
            return {"ok": False, "error": "复核要带 agent 的 conversation_id", "code": "INVALID"}
        if verdict not in ("pass", "fail"):
            return {"ok": False, "error": "复核结论只能是 pass 或 fail", "code": "INVALID"}
        if not body:
            return {"ok": False, "error": "复核必须写理由：验了什么、怎么验的", "code": "INVALID"}

        def apply(card):
            if card["status"] != "in_review":
                return "NOT_IN_REVIEW"
            # 自己复核自己等于没复核。这一条是这层的全部意义所在
            if (card.get("assignee") or {}).get("conversation_id") == conv:
                return "SELF_REVIEW"
            card["review"] = normalize_review({
                "verdict": verdict, "by": conv,
                "tab_name": args.get("tab_name"), "text": body})
            card["last_activity"] = ("复核通过：" if verdict == "pass" else "复核未过：") + body[:60]
            card["events"].append(_event(
                "review_note", conv,
                ("复核通过 · " if verdict == "pass" else "复核未过 · ") + body))
            return None

        card, code = self.storage.mutate(card_id, args.get("version"), apply)
        if card is None:
            return {"ok": False, "error": _err_text(code), "code": code}
        return {"ok": True, "card": {k: v for k, v in card.items() if k != "events"}}

    # ---- 验收 / 退回 ---------------------------------------------------------
    def review_card(self, card_id: str, args: dict | None = None) -> dict:
        args = args if isinstance(args, dict) else {}

        def apply(card):
            if card["status"] != "in_review":
                return "NOT_IN_REVIEW"
            card["status"] = "done"
            card["last_activity"] = "验收通过"
            card["events"].append(_event("review", CONSOLE_ID, "rxyy 验收通过"))
            return None

        card, code = self.storage.mutate(card_id, args.get("version"), apply)
        if card is None:
            return {"ok": False, "error": _err_text(code), "code": code}
        return {"ok": True, "card": {k: v for k, v in card.items() if k != "events"}}

    def release_card(self, card_id: str, args: dict | None = None) -> dict:
        args = args if isinstance(args, dict) else {}
        conv = str(args.get("conversation_id") or CONSOLE_ID).strip()
        reason = clip_text(args.get("reason"), 500) or "退回待领"

        def apply(card):
            if card["status"] not in ("in_progress", "in_review"):
                return "NOT_ACTIVE"
            owner = (card.get("assignee") or {}).get("conversation_id")
            if conv != CONSOLE_ID and owner != conv:
                return "NOT_OWNER"
            card["status"] = "todo"
            card["assignee"] = None
            card["review"] = None
            card["last_activity"] = "退回：" + reason[:60]
            card["events"].append(_event("release", conv, reason))
            return None

        card, code = self.storage.mutate(card_id, args.get("version"), apply)
        if card is None:
            return {"ok": False, "error": _err_text(code), "code": code}
        return {"ok": True, "card": {k: v for k, v in card.items() if k != "events"}}


def _dispatch_block_reason(card: dict) -> str:
    if card.get("archived"):
        return "归档的卡先放回面板再派"
    if card.get("status") == "done":
        return "已完成的卡不用再派"
    return ""


def _claim_cmd(card_id: str) -> str:
    return (r"  python D:\桌面\working\cursor工作流\scripts\boardctl.py claim "
            "{} --thread-id <你的会话ID> --agent-type cursor".format(card_id))


def _dispatch_text(card: dict, note: str = "") -> str:
    """派给 agent 的那条消息。把领卡命令一并给它，省得它自己去翻技能文档。"""
    status = card.get("status") or ""
    lines = ["【派活·任务面板】{}".format(card["title"])]
    lines.append("当前列：{}".format(status))
    if card.get("project"):
        lines.append("项目：{}".format(card["project"]))
    if card.get("desc"):
        lines.append("需求：{}".format(card["desc"][:1500]))
    if note:
        lines.append("附言：{}".format(note))
    lines.append("")
    claim = _claim_cmd(card["id"])
    if status == "in_review":
        lines.append("这张卡在「待验收」。没做完的做完：先 claim 接手（会回到处理中，旧复核作废）：")
        lines.append(claim)
        lines.append("干完再 deliver，卡会重新进「待验收」等 rxyy 点头。")
    elif status == "todo":
        lines.append("先领卡再动手（领了才算你的，别人就抢不走）：")
        lines.append(claim)
        lines.append("干完写交付说明再 deliver，卡会进「待验收」等 rxyy 点头；"
                     "干不动就 release 退回，别占着。详见技能 manage-board。")
    elif status == "backlog":
        lines.append("这张还在想法池，可以先看需求；要动手得等 rxyy 批准进「待认领」后再 claim。")
        lines.append(claim)
    else:
        lines.append("先看清卡上现任是谁。要接手：待验收可直接 claim；处理中的得原执行者 release。")
        lines.append(claim)
        lines.append("详见技能 manage-board。")
    return "\n".join(lines)


def _dispatch_batch_text(cards: list, note: str = "") -> str:
    """多张合成一条。agent 一次看全能统筹，也不会把队列刷成一屏。"""
    lines = ["【派活·任务面板·批量】共 {} 张卡".format(len(cards))]
    if note:
        lines.append("附言：{}".format(note))
    lines.append("派发不等于领卡。一次只 claim 一张再做，做完 deliver 再领下一张。")
    lines.append("")
    for i, card in enumerate(cards, 1):
        status = card.get("status") or ""
        lines.append("{}. [{}] {}".format(i, status, card["title"]))
        lines.append("   id：{}".format(card["id"]))
        if card.get("project"):
            lines.append("   项目：{}".format(card["project"]))
        if card.get("desc"):
            lines.append("   需求：{}".format(card["desc"][:400]))
        if status == "in_review":
            lines.append("   待验收：claim 接手会回到处理中，旧复核作废")
        elif status == "backlog":
            lines.append("   想法池：先看需求，动手要等批准")
        elif status == "todo":
            lines.append("   待认领：先 claim 再动手")
        lines.append(_claim_cmd(card["id"]))
        lines.append("")
    return "\n".join(lines).rstrip()


def _err_text(code: str) -> str:
    return {
        "VERSION_CONFLICT": "卡片刚被别人改过，请刷新后重试",
        "NOT_FOUND": "卡片不存在",
        "CLAIMED": "这张卡已被别的会话领走",
        "NOT_CLAIMABLE": "这张卡现在不能领（想法池要先批准；处理中的得原执行者退回）",
        "BLOCKED": "依赖卡未完成，这张卡还被卡着",
        # agent 干到一半被判过一次失联时，卡会被自动退回待认领——它自己不知道，
        # 回头交付就撞这条。所以这句话得告诉它下一步该干嘛，而不是只说「不行」
        "NOT_OWNER": "这张卡不在你名下（若你中途掉过线，卡可能已被自动退回待认领，重新领一次即可）",
        "INVALID_MOVE": "不允许的状态流转",
        "IN_PROGRESS_LOCKED": "执行中的卡只能由执行者交付或放弃",
        "NOT_IN_REVIEW": "只有『待验收』列的卡能验收",
        "SELF_REVIEW": "自己复核自己等于没复核，换个 agent 来",
        "NOT_ACTIVE": "只有执行中/待验收的卡能退回",
        "NOT_DISPATCHABLE": "已完成或已归档的卡不能派",
        "TARGET_OFFLINE": "那个 agent 不在线，派不过去",
        "HUB_OFFLINE": "连不上rxyy-mcp，派不出去",
        "SEND_FAILED": "消息没能递给那个 agent",
        "INVALID": "参数无效",
        "INVALID_STATUS": "没有这个状态",
    }.get(str(code), str(code) or "未知错误")


__all__ = ["BoardService", "CONSOLE_ID"]
