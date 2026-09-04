# -*- coding: utf-8 -*-
"""任务面板的规矩（08-13：多 agent 并行的那几条底线）。

看板的价值全在「谁也别想含糊过去」这几条上，所以逐条钉死：
1. 一张卡同时只能被一个会话领走——两个 agent 抢，第二个必须吃闭门羹；
2. 拿旧 version 写必须失败**且不落盘**（否则并行改会互相盖）；
3. 依赖没做完的卡领不动；
4. agent 无权自我完结——done 只有控制台验收那一条路；
5. 别人名下的卡动不了；
6. 退回之后卡回到待领且无主，能被另一个 agent 接手。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CONSOLE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONSOLE_DIR))

from api.board.service import CONSOLE_ID, BoardService
from api.board.storage import BoardStorage


class BoardRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = BoardService(BoardStorage(Path(self.tmp.name)),
                                project_names_loader=lambda: [])

    def tearDown(self):
        self.tmp.cleanup()

    def _new(self, title="活儿", **fields):
        reply = self.svc.create_card(dict(title=title, **fields))
        self.assertTrue(reply["ok"], reply)
        return reply["card"]

    def _claim(self, card, conv, agent_type="cursor"):
        return self.svc.claim_card(card["id"], {
            "conversation_id": conv, "agent_type": agent_type,
            "version": card["version"]})

    def test_only_one_agent_can_claim_a_card(self):
        card = self._new()
        first = self._claim(card, "7edf659a")
        self.assertTrue(first["ok"])
        self.assertEqual("in_progress", first["card"]["status"])
        # 抢输的那个要听见「已被领走」而不是「版本冲突」：后者会让它原地重试打转
        second = self._claim(card, "bc18e44d")
        self.assertFalse(second["ok"])
        self.assertEqual("CLAIMED", second["code"])
        # 抢输的那个不能把认领人改掉
        self.assertEqual("7edf659a", self.svc.get_card(card["id"])["card"]["assignee"]["conversation_id"])

    def test_stale_version_write_is_refused_and_not_persisted(self):
        card = self._new()
        claimed = self._claim(card, "7edf659a")["card"]
        stale = self.svc.move_card(card["id"], {
            "status": "in_review", "conversation_id": "7edf659a",
            "version": card["version"]})  # 领卡后 version 已经涨过
        self.assertFalse(stale["ok"])
        self.assertEqual("VERSION_CONFLICT", stale["code"])
        self.assertEqual("in_progress", self.svc.get_card(card["id"])["card"]["status"])
        fresh = self.svc.move_card(card["id"], {
            "status": "in_review", "conversation_id": "7edf659a",
            "version": claimed["version"]})
        self.assertTrue(fresh["ok"], fresh)

    def test_card_blocked_by_unfinished_dependency_cannot_be_claimed(self):
        first = self._new("先做这个")
        second = self._new("依赖上一张", blocked_by=[first["id"]])
        blocked = self._claim(second, "bc18e44d")
        self.assertFalse(blocked["ok"])
        self.assertEqual("BLOCKED", blocked["code"])
        # 把依赖走完整条流程（领 → 交付 → 验收），后一张才解锁
        working = self._claim(first, "7edf659a")["card"]
        delivered = self.svc.move_card(first["id"], {
            "status": "in_review", "conversation_id": "7edf659a",
            "version": working["version"]})["card"]
        self.svc.review_card(first["id"], {"version": delivered["version"]})
        self.assertTrue(self._claim(second, "bc18e44d")["ok"])

    def test_agent_cannot_mark_its_own_card_done(self):
        card = self._new()
        working = self._claim(card, "7edf659a")["card"]
        refused = self.svc.move_card(card["id"], {
            "status": "done", "conversation_id": "7edf659a",
            "version": working["version"]})
        self.assertFalse(refused["ok"])
        self.assertEqual("USE_REVIEW", refused["code"])
        # 没到待验收的卡，控制台也点不了验收
        early = self.svc.review_card(card["id"], {"version": working["version"]})
        self.assertFalse(early["ok"])
        self.assertEqual("NOT_IN_REVIEW", early["code"])

    def test_agent_cannot_touch_someone_elses_card(self):
        card = self._new()
        working = self._claim(card, "7edf659a")["card"]
        intruder = self.svc.move_card(card["id"], {
            "status": "in_review", "conversation_id": "bc18e44d",
            "version": working["version"]})
        self.assertFalse(intruder["ok"])
        self.assertEqual("NOT_OWNER", intruder["code"])

    def test_released_card_goes_back_to_the_queue_for_anyone(self):
        card = self._new()
        working = self._claim(card, "7edf659a")["card"]
        back = self.svc.release_card(card["id"], {
            "conversation_id": CONSOLE_ID, "version": working["version"],
            "reason": "会话判死"})
        self.assertTrue(back["ok"], back)
        self.assertEqual("todo", back["card"]["status"])
        self.assertIsNone(back["card"]["assignee"])
        self.assertTrue(self._claim(back["card"], "bc18e44d", "codex")["ok"])

    def test_console_can_shuffle_the_queue_without_an_agent_id(self):
        # 控制台上手动拖卡不带会话 ID，别把 rxyy 当成对不上号的匿名 agent 挡在外面
        card = self._new()
        parked = self.svc.move_card(card["id"], {
            "status": "backlog", "version": card["version"]})
        self.assertTrue(parked["ok"], parked)
        self.assertEqual("backlog", parked["card"]["status"])
        # 但执行中的卡仍然只有执行者能交付，控制台不能替它签字
        back = self.svc.move_card(card["id"], {
            "status": "todo", "version": parked["card"]["version"]})["card"]
        working = self._claim(back, "7edf659a")["card"]
        snatch = self.svc.move_card(card["id"], {
            "status": "backlog", "version": working["version"]})
        self.assertFalse(snatch["ok"])
        self.assertEqual("IN_PROGRESS_LOCKED", snatch["code"])

    def test_backlog_card_is_an_idea_not_a_job(self):
        card = self._new(status="backlog")
        self.assertEqual("backlog", card["status"])
        refused = self._claim(card, "7edf659a")
        self.assertFalse(refused["ok"])
        self.assertEqual("NOT_CLAIMABLE", refused["code"])

    def test_new_card_cannot_be_born_in_progress(self):
        self.assertEqual("todo", self._new(status="in_progress")["status"])

    def test_progress_report_only_lands_on_the_reporters_own_card(self):
        mine = self._claim(self._new("我的"), "7edf659a")["card"]
        others = self._claim(self._new("别人的"), "bc18e44d")["card"]
        self.svc.note_activity("7edf659a", "跑通了第一版")
        self.assertEqual("跑通了第一版", self.svc.get_card(mine["id"])["card"]["last_activity"])
        self.assertNotEqual("跑通了第一版",
                            self.svc.get_card(others["id"])["card"]["last_activity"])

    def test_hub_can_finish_and_release_a_whole_session(self):
        # hub 手上只有会话 ID，没有卡片 ID 更没有 version，所以给它会话级的口
        mine_a = self._claim(self._new("甲"), "7edf659a")["card"]
        mine_b = self._claim(self._new("乙"), "7edf659a")["card"]
        others = self._claim(self._new("别人的"), "bc18e44d")["card"]
        finished = self.svc.finish_session("7edf659a", "黑板收工信号")
        self.assertEqual({mine_a["id"], mine_b["id"]}, set(finished["moved"]))
        self.assertEqual("in_review", self.svc.get_card(mine_a["id"])["card"]["status"])
        self.assertEqual("in_progress", self.svc.get_card(others["id"])["card"]["status"])
        # 会话判死：还在干的那张退回待认领，别人能接手
        gone = self.svc.release_session("7edf659a", "会话失联")
        self.assertEqual([], gone["released"], "两张都交付了，没有还在干的卡可退")

    def test_a_dead_session_does_not_take_back_what_it_already_delivered(self):
        # 待验收 = 活已交完，就等 rxyy 点头；执行者这时候掉线不该把卡打回重来，
        # 否则一次误判就把验收队列清空，人白干（08-13 实测中招两张）
        working = self._claim(self._new("还在干"), "7edf659a")["card"]
        delivered = self._claim(self._new("已交付"), "7edf659a")["card"]
        self.svc.move_card(delivered["id"], {
            "status": "in_review", "conversation_id": "7edf659a",
            "version": delivered["version"]})
        gone = self.svc.release_session("7edf659a", "会话失联")
        self.assertEqual([working["id"]], gone["released"])
        self.assertEqual("todo", self.svc.get_card(working["id"])["card"]["status"])
        kept = self.svc.get_card(delivered["id"])["card"]
        self.assertEqual("in_review", kept["status"])
        self.assertEqual("7edf659a", kept["assignee"]["conversation_id"])

    def test_another_agent_can_pre_check_the_work_but_still_cannot_close_it(self):
        # rxyy 在外面，一眼看不出活成没成，所以让别的 agent 先验一道写个结论；
        # 但卡照旧停在待验收，签字权还在他手上
        card = self._claim(self._new(), "7edf659a")["card"]
        delivered = self.svc.move_card(card["id"], {
            "status": "in_review", "conversation_id": "7edf659a",
            "version": card["version"]})["card"]
        checked = self.svc.review_note(card["id"], {
            "conversation_id": "bc18e44d", "verdict": "pass",
            "body": "跑了 13 条测试全过，页面点开也对", "version": delivered["version"]})
        self.assertTrue(checked["ok"], checked)
        self.assertEqual("in_review", checked["card"]["status"], "复核不该改状态")
        self.assertEqual("pass", checked["card"]["review"]["verdict"])
        self.assertEqual("bc18e44d", checked["card"]["review"]["by"])

    def test_you_cannot_pre_check_your_own_work(self):
        card = self._claim(self._new(), "7edf659a")["card"]
        delivered = self.svc.move_card(card["id"], {
            "status": "in_review", "conversation_id": "7edf659a",
            "version": card["version"]})["card"]
        mine = self.svc.review_note(card["id"], {
            "conversation_id": "7edf659a", "verdict": "pass",
            "body": "我觉得挺好", "version": delivered["version"]})
        self.assertFalse(mine["ok"])
        self.assertEqual("SELF_REVIEW", mine["code"])
        # 结论必须带理由，光说个「过」没有意义
        empty = self.svc.review_note(card["id"], {
            "conversation_id": "bc18e44d", "verdict": "pass", "body": ""})
        self.assertFalse(empty["ok"])
        self.assertEqual("INVALID", empty["code"])

    def test_sending_a_card_back_voids_the_previous_pre_check(self):
        # 返工之后那条「复核通过」说的已经不是现在这版东西了
        card = self._claim(self._new(), "7edf659a")["card"]
        delivered = self.svc.move_card(card["id"], {
            "status": "in_review", "conversation_id": "7edf659a",
            "version": card["version"]})["card"]
        self.svc.review_note(card["id"], {
            "conversation_id": "bc18e44d", "verdict": "pass",
            "body": "看着没问题", "version": delivered["version"]})
        current = self.svc.get_card(card["id"])["card"]
        back = self.svc.move_card(card["id"], {
            "status": "in_progress", "conversation_id": "7edf659a",
            "version": current["version"], "note": "还得返工"})
        self.assertTrue(back["ok"], back)
        self.assertIsNone(back["card"]["review"])

    def test_dispatching_hands_the_work_over_without_claiming_it_for_anyone(self):
        # 派活只是把活递过去：万一它没收到或当场就死了，卡不能挂在一个从没开工的
        # 人名下——那样 rxyy 在面板上看到的是假进度，比没有派活功能更坏
        card = self._new("去干这个")
        sent = {}

        class _Hub:
            def state(self):
                return {"sessions": [{"id": "sess-1", "name": "rxyy tools·看板UI",
                                      "connected": True}]}

            def send(self, sid, text, images=None, files=None):
                sent["sid"], sent["text"] = sid, text
                return {"ok": True, "qid": "q1"}

        with patch("api.taskstage.core.HubClient", lambda *a, **k: _Hub()):
            reply = self.svc.dispatch_card(card["id"], {
                "session_id": "sess-1", "tab_name": "rxyy tools·看板UI", "note": "优先做"})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual("sess-1", sent["sid"])
        self.assertIn(card["id"], sent["text"], "得把领卡命令给它，省得它去翻文档")
        after = self.svc.get_card(card["id"])["card"]
        self.assertEqual("todo", after["status"], "派了不等于领了")
        self.assertIsNone(after["assignee"])
        self.assertIn("已派给", after["last_activity"])

    def test_dispatching_to_a_dead_tab_is_refused(self):
        # 派给断了线的 tab，消息石沉大海而卡上却记了一笔「已派给它」，比派不出去更难查
        card = self._new("给个离线的")
        hit = []

        class _Hub:
            def state(self):
                return {"sessions": [
                    {"id": "live", "name": "在线的", "connected": True},
                    {"id": "dead", "name": "断线的", "connected": False}]}

            def send(self, *a, **k):
                hit.append(a)
                return {"ok": True}

        with patch("api.taskstage.core.HubClient", lambda *a, **k: _Hub()):
            reply = self.svc.dispatch_card(card["id"], {"session_id": "dead"})
        self.assertFalse(reply["ok"])
        self.assertEqual("TARGET_OFFLINE", reply["code"])
        self.assertEqual([], hit, "不在线就别惊动 hub")

    def test_in_review_and_backlog_can_be_dispatched_but_done_cannot(self):
        idea = self._new("还没批准的想法", status="backlog")
        working = self._claim(self._new("已经有人在做"), "bc18e44d")["card"]
        delivered = self.svc.move_card(working["id"], {
            "status": "in_review", "conversation_id": "bc18e44d",
            "version": working["version"]})["card"]
        done = self._claim(self._new("已经验收"), "7edf659a")["card"]
        done = self.svc.move_card(done["id"], {
            "status": "in_review", "conversation_id": "7edf659a",
            "version": done["version"]})["card"]
        done = self.svc.review_card(done["id"], {"version": done["version"]})["card"]

        class _Hub:
            def state(self):
                return {"sessions": [{"id": "sess-1", "name": "在线", "connected": True}]}

            def send(self, *a, **k):
                return {"ok": True, "qid": "q1"}

        with patch("api.taskstage.core.HubClient", lambda *a, **k: _Hub()):
            self.assertTrue(self.svc.dispatch_card(idea["id"], {"session_id": "sess-1"})["ok"])
            self.assertTrue(self.svc.dispatch_card(delivered["id"], {"session_id": "sess-1"})["ok"])
            refused = self.svc.dispatch_card(done["id"], {"session_id": "sess-1"})
        self.assertFalse(refused["ok"])
        self.assertEqual("NOT_DISPATCHABLE", refused["code"])
        self.assertEqual("backlog", self.svc.get_card(idea["id"])["card"]["status"])
        self.assertEqual("in_review", self.svc.get_card(delivered["id"])["card"]["status"])

    def test_batch_dispatch_sends_one_message_and_does_not_claim(self):
        first = self._new("待验收甲")
        second = self._new("待验收乙")
        working = self._claim(first, "bc18e44d")["card"]
        delivered = self.svc.move_card(first["id"], {
            "status": "in_review", "conversation_id": "bc18e44d",
            "version": working["version"]})["card"]
        done = self._claim(self._new("已经验收"), "7edf659a")["card"]
        done = self.svc.move_card(done["id"], {
            "status": "in_review", "conversation_id": "7edf659a",
            "version": done["version"]})["card"]
        done = self.svc.review_card(done["id"], {"version": done["version"]})["card"]
        hits = []

        class _Hub:
            def state(self):
                return {"sessions": [{"id": "sess-1", "name": "rxyy tools·任务派工",
                                      "connected": True}]}

            def send(self, sid, text, images=None, files=None):
                hits.append((sid, text))
                return {"ok": True, "qid": "q-batch"}

        with patch("api.taskstage.core.HubClient", lambda *a, **k: _Hub()):
            reply = self.svc.dispatch_cards(
                [delivered["id"], second["id"], done["id"]],
                {"session_id": "sess-1", "tab_name": "rxyy tools·任务派工",
                 "note": "没做完的做完"})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(1, len(hits), "多张必须合成一条，别把队列刷成一屏")
        self.assertIn(delivered["id"], hits[0][1])
        self.assertIn(second["id"], hits[0][1])
        self.assertNotIn(done["id"], hits[0][1])
        self.assertEqual([delivered["id"], second["id"]], reply["sent"])
        self.assertEqual(1, len(reply["skipped"]))
        self.assertEqual("todo", self.svc.get_card(second["id"])["card"]["status"])
        self.assertIsNone(self.svc.get_card(second["id"])["card"]["assignee"])
        self.assertEqual("in_review", self.svc.get_card(delivered["id"])["card"]["status"])

    def test_batch_dispatch_refuses_when_nothing_is_sendable(self):
        done = self._claim(self._new("已经验收"), "7edf659a")["card"]
        done = self.svc.move_card(done["id"], {
            "status": "in_review", "conversation_id": "7edf659a",
            "version": done["version"]})["card"]
        done = self.svc.review_card(done["id"], {"version": done["version"]})["card"]
        hit = []

        class _Hub:
            def state(self):
                return {"sessions": [{"id": "sess-1", "name": "在线", "connected": True}]}

            def send(self, *a, **k):
                hit.append(a)
                return {"ok": True}

        with patch("api.taskstage.core.HubClient", lambda *a, **k: _Hub()):
            empty = self.svc.dispatch_cards([], {"session_id": "sess-1"})
            refused = self.svc.dispatch_cards([done["id"]], {"session_id": "sess-1"})
        self.assertEqual("INVALID", empty["code"])
        self.assertEqual("NOT_DISPATCHABLE", refused["code"])
        self.assertEqual([], hit)

    def test_claiming_an_in_review_card_takes_it_back_for_rework(self):
        card = self._claim(self._new("待补的活"), "7edf659a")["card"]
        delivered = self.svc.move_card(card["id"], {
            "status": "in_review", "conversation_id": "7edf659a",
            "version": card["version"]})["card"]
        self.svc.review_note(card["id"], {
            "conversation_id": "bc18e44d", "verdict": "fail",
            "body": "还缺截图", "version": delivered["version"]})
        taken = self.svc.claim_card(card["id"], {
            "conversation_id": "6d81660e", "agent_type": "cursor",
            "tab_name": "rxyy tools·任务派工"})
        self.assertTrue(taken["ok"], taken)
        after = taken["card"]
        self.assertEqual("in_progress", after["status"])
        self.assertEqual("6d81660e", after["assignee"]["conversation_id"])
        self.assertIsNone(after["review"])

    def test_chatty_progress_cannot_squeeze_out_the_audit_trail(self):
        # agent 报几十条进度也不能把「谁领的、他说交付了什么」顶出事件表
        card = self._claim(self._new(), "7edf659a")["card"]
        self.svc.comment_card(card["id"], {"body": "这条必须留住", "conversation_id": "7edf659a"})
        for i in range(300):
            self.svc.note_activity("7edf659a", "developing · 第 %d 步" % i)
        events = self.svc.get_card(card["id"])["events"]
        kinds = [e["kind"] for e in events]
        self.assertIn("claim", kinds)
        self.assertIn("comment", kinds)
        self.assertEqual("这条必须留住", [e["text"] for e in events if e["kind"] == "comment"][0])
        # 连续进度就地折叠，只留最新那条
        self.assertEqual(1, kinds.count("zt"))
        self.assertEqual("developing · 第 299 步", [e["text"] for e in events if e["kind"] == "zt"][0])

    def test_list_stays_slim_and_filters_by_project(self):
        self._new("甲项目的活", project="codex-dream")
        self._new("乙项目的活", project="rxyy-tools")
        both = self.svc.list_cards()
        self.assertEqual(2, len(both["cards"]))
        self.assertNotIn("events", both["cards"][0])  # 列表不驮事件正文
        only = self.svc.list_cards({"project": "codex-dream"})
        self.assertEqual(["甲项目的活"], [c["title"] for c in only["cards"]])
        # 按项目过滤时下拉名单不能跟着塌，否则切回「全部」会丢别的项目
        self.assertEqual(["codex-dream", "rxyy-tools"], only["projects"])


class OaBoundBoardNamesTests(unittest.TestCase):
    """已绑 OA 的启用仓要出现在看板下拉，没有卡也占一项。"""

    def test_bound_enabled_repo_uses_biz_name(self):
        from api.board.service import oa_bound_board_names
        p = _P("Mental_Health_Assessment", oa="180262", biz="心理健康管理平台")
        self.assertEqual(["心理健康管理平台"], oa_bound_board_names([p]))

    def test_bound_repo_without_biz_name_uses_folder_name(self):
        from api.board.service import oa_bound_board_names
        p = _P("graphiti", oa="51648", biz="")
        self.assertEqual(["graphiti"], oa_bound_board_names([p]))

    def test_unbound_or_disabled_repos_are_not_hung(self):
        from api.board.service import oa_bound_board_names
        rows = [
            _P("mcp持久化", oa="", biz=""),
            _P("old", enabled=0, oa="51648", biz="旧仓"),
        ]
        self.assertEqual([], oa_bound_board_names(rows))

    def test_empty_board_still_lists_oa_bound_names(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        svc = BoardService(BoardStorage(Path(tmp.name)),
                           project_names_loader=lambda: ["心理健康管理平台"])
        listed = svc.list_cards()
        self.assertEqual([], listed["cards"])
        self.assertEqual(["心理健康管理平台"], listed["projects"])

    def test_filtering_cards_does_not_drop_hung_oa_names(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        svc = BoardService(BoardStorage(Path(tmp.name)),
                           project_names_loader=lambda: ["心理健康管理平台"])
        svc.create_card({"title": "甲", "project": "codex-dream"})
        only = svc.list_cards({"project": "codex-dream"})
        self.assertEqual(["甲"], [c["title"] for c in only["cards"]])
        self.assertEqual(["codex-dream", "心理健康管理平台"], only["projects"])


class BoardUiContractTests(unittest.TestCase):
    def test_refresh_merges_backend_hung_project_names(self):
        text = (Path(__file__).resolve().parents[1] / "web" / "board.js"
                ).read_text(encoding="utf-8")
        self.assertIn("result.projects", text)
        self.assertIn("已绑 OA 的仓由后端挂进", text)


class _P:
    def __init__(self, name, enabled=1, oa="", biz=""):
        self.name = name
        self.enabled = enabled
        self.oa_project_id = oa
        self.biz_name = biz


if __name__ == "__main__":
    unittest.main()
