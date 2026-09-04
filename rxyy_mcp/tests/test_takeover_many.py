# -*- coding: utf-8 -*-
"""多选几个中断会话一起交给同一个 agent 接手（09-02 rxyy：多选多个会话让别人接手）。

为什么不能循环调 share_takeover：派给待命壳的第一张就 alias_shell_into → _reap 把壳从
会话表删掉，第二张再按壳 ID 派 = 「接手方会话不存在」。手机批量派单里两个死会话选同
一个壳一直会撞这个。改成：同一接手方的几张单合成一条消息发（0901 六单就是叠成一条
收到的），规则全文只随第 1 张。
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402

WS1 = r"d:\Desktop\cursor工作流"


def _s(sid, conv, name, *, root=WS1, connected=True, pending=False, msgs=1):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.task_root = s.cwd = root
    s.connected = connected
    s.pending = {"id": "q"} if pending else None
    s.queued = []
    s.msg_seq = msgs
    s.handed_off_to = ""
    s.client = None
    s.lock = threading.Lock()
    s.file_path = None
    s.messages = []
    s.rev = 0
    return s


def _fake_prompt(self, sid, digest_repeat=False):
    s = hub.HUB.sessions.get(sid)
    if s is None:
        return {"ok": False, "error": "会话不存在"}
    body = ("你的任务：接手一个此前在rxyy-mcp控制台中断的会话，把它没做完的工作继续完成。\n"
            "【第一个动作·先于读任何文件】现在立刻用 conversation_id=「{c}」调一次 zt\n"
            + ("" if digest_repeat else "【用户级规则 · 必读必守】很长的协议全文\n")
            + "**conversation_id 必须改用「{c}」**\n- 任务标题：{n}\n").format(c=s.conv_key, n=s.name)
    return {"ok": True, "prompt": body, "conversation_id": s.conv_key, "name": s.name}


class _Base(unittest.TestCase):
    def setUp(self):
        self.sent = []      # (target_id, text) 经 send_reply 投出去的
        self.queued = []    # (target_id, text) 经 queue_message 排队的
        self.aliased = []   # (shell, src) alias_shell_into 调用

        def _send(api, sid, text, sel, im, cont, who="", files=None):
            self.sent.append((sid, text))
            return {"ok": True}

        def _queue(api, sid, text, im, who="", files=None):
            self.queued.append((sid, text))
            return {"ok": True}

        def _alias(h, shell, src):
            self.aliased.append((shell.id, src.id))
            with h.lock:
                h.sessions.pop(shell.id, None)   # 真实路径：壳从会话表消失

        self.patches = [
            patch.object(hub.Api, "get_takeover_prompt", _fake_prompt),
            patch.object(hub.Api, "send_reply", _send),
            patch.object(hub.Api, "queue_message", _queue),
            patch.object(hub.Hub, "alias_shell_into", _alias),
            patch.object(hub.HUB, "cfg", {"max_messages": 200}),
            patch.object(hub.HUB, "takeover_aliases", {}),
            patch.object(hub.HUB, "takeover_ledger", {}, create=True),
            patch.object(hub, "log_event", lambda *a, **k: None),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(patch.stopall)
        hub.Api._digest_sent_at.clear()
        self.addCleanup(hub.Api._digest_sent_at.clear)

    def _hub(self, *sessions):
        d = {s.id: s for s in sessions}
        p1 = patch.object(hub.HUB, "sessions", d)
        p2 = patch.object(hub.HUB, "order", list(d))
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        return d


class MergedPromptTests(_Base):
    def test_merged_prompt_keeps_every_ticket_id_and_repeats_the_rulebook_once(self):
        self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                  _s("d2", "bbbb2222", "挂了的二", connected=False),
                  _s("d3", "cccc3333", "挂了的三", connected=False))
        r = hub.Api().get_takeover_prompt_many(["d1", "d2", "d3"])
        self.assertTrue(r["ok"])
        self.assertEqual(3, r["count"])
        p = r["prompt"]
        # 开头必须是接手单原话：_auto_label_on_dispatch 靠 startswith 认它不是派活
        self.assertTrue(p.startswith(hub.TAKEOVER_PROMPT_HEAD), p[:60])
        for c in ("aaaa1111", "bbbb2222", "cccc3333"):
            self.assertIn("conversation_id 必须改用「{}」".format(c), p)
        self.assertEqual(1, p.count("很长的协议全文"), "规则全文只许随第 1 张")
        self.assertIn("第 1/3 单", p)
        self.assertIn("第 3/3 单", p)
        self.assertLess(p.index("aaaa1111"), p.index("bbbb2222"))
        self.assertLess(p.index("bbbb2222"), p.index("cccc3333"))
        # 认接手单目标的两处都取第一个匹配 = 第 1 张
        self.assertEqual("aaaa1111", hub.takeover_prompt_target(p))
        self.assertEqual("aaaa1111", r["conversation_id"])

    def test_braces_in_session_names_do_not_blow_up(self):
        # {项目名} 事故同类：会话名里的花括号不许被 .format 当占位符
        self._hub(_s("d1", "aaaa1111", "修 {项目名} 路径", connected=False),
                  _s("d2", "bbbb2222", "任务{foo}", connected=False))
        r = hub.Api().get_takeover_prompt_many(["d1", "d2"])
        self.assertTrue(r["ok"])
        self.assertIn("修 {项目名} 路径", r["prompt"])
        self.assertIn("任务{foo}", r["prompt"])

    def test_single_id_degrades_to_the_plain_prompt(self):
        self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False))
        r = hub.Api().get_takeover_prompt_many(["d1", "d1", ""])
        self.assertTrue(r["ok"])
        self.assertEqual(1, r["count"])
        self.assertNotIn("批量接手", r["prompt"])

    def test_missing_sessions_are_reported_not_fatal(self):
        self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False))
        r = hub.Api().get_takeover_prompt_many(["d1", "ghost"])
        self.assertTrue(r["ok"])
        self.assertEqual(1, r["count"])
        self.assertEqual(1, len(r["failed"]))
        self.assertIn("ghost", r["failed"][0])

    def test_nothing_selected_or_too_many(self):
        self._hub()
        self.assertFalse(hub.Api().get_takeover_prompt_many([])["ok"])
        ids = ["x{}".format(i) for i in range(hub.Api.TAKEOVER_MANY_MAX + 1)]
        r = hub.Api().get_takeover_prompt_many(ids)
        self.assertFalse(r["ok"])
        self.assertIn("最多", r["error"])


class ShareTakeoverManyTests(_Base):
    def test_one_message_to_a_shell_aliases_it_to_the_first_ticket_only(self):
        d = self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                      _s("d2", "bbbb2222", "挂了的二", connected=False),
                      _s("b", "c2", "待命·乙", pending=True))
        with patch.object(hub.Hub, "_is_checkin_shellish", lambda h, x: True):
            r = hub.Api().share_takeover_many(["d1", "d2"], "b", who="控制台")
        self.assertTrue(r["ok"], r)
        self.assertEqual(2, r["count"])
        self.assertEqual(["挂了的一", "挂了的二"], r["names"])
        self.assertFalse(r["queued"])
        # 只发一条，两张单都在里面
        self.assertEqual(1, len(self.sent))
        self.assertEqual("b", self.sent[0][0])
        self.assertIn("aaaa1111", self.sent[0][1])
        self.assertIn("bbbb2222", self.sent[0][1])
        self.assertEqual([], self.queued)
        # 壳归并只给第 1 张
        self.assertEqual([("b", "d1")], self.aliased)
        self.assertNotIn("b", d)
        # 两张单都挂上「接手在途」；第 2 张预置 warned 免得 15 分钟误报
        t1, t2 = d["d1"].takeover_dispatched, d["d2"].takeover_dispatched
        self.assertEqual("待命·乙", t1["to_name"])
        self.assertEqual((1, 2, False), (t1["batch_pos"], t1["batch_total"], t1["warned"]))
        self.assertEqual((2, 2, True), (t2["batch_pos"], t2["batch_total"], t2["warned"]))
        self.assertIn("第 1 张", d["d1"].messages[-1]["html"])
        self.assertIn("无需改 ID", d["d1"].messages[-1]["html"])
        self.assertIn("第 2 张", d["d2"].messages[-1]["html"])
        self.assertIn("切到本会话 ID", d["d2"].messages[-1]["html"])

    def test_to_a_busy_agent_queues_once_and_records_the_first_successor(self):
        d = self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                      _s("d2", "bbbb2222", "挂了的二", connected=False),
                      _s("b", "c2", "rxyy tools·干活中", msgs=30))
        with patch.object(hub.Hub, "_is_checkin_shellish", lambda h, x: False):
            r = hub.Api().share_takeover_many(["d1", "d2"], "b", who="控制台")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["queued"])
        self.assertEqual(1, len(self.queued))
        self.assertEqual([], self.aliased)
        self.assertIn("b", d)
        self.assertEqual("aaaa1111", d["b"].handed_off_to)

    def test_the_rulebook_is_dropped_when_that_agent_already_got_it(self):
        self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                  _s("d2", "bbbb2222", "挂了的二", connected=False),
                  _s("d3", "cccc3333", "挂了的三", connected=False),
                  _s("b", "c2", "rxyy tools·干活中", msgs=30))
        with patch.object(hub.Hub, "_is_checkin_shellish", lambda h, x: False):
            hub.Api().share_takeover_many(["d1", "d2"], "b")
            hub.Api().share_takeover_many(["d3"], "b")
        self.assertEqual(2, len(self.queued))
        self.assertEqual(1, self.queued[0][1].count("很长的协议全文"))
        self.assertNotIn("很长的协议全文", self.queued[1][1], "同一接手方第二批不该再发规则全文")

    def test_refuses_a_target_that_is_itself_selected_or_offline(self):
        self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                  _s("d2", "bbbb2222", "挂了的二", connected=False),
                  _s("off", "c9", "掉线的", connected=False))
        r = hub.Api().share_takeover_many(["d1", "d2"], "d1")
        self.assertFalse(r["ok"])
        self.assertIn("自己接手自己", r["error"])
        r = hub.Api().share_takeover_many(["d1", "d2"], "off")
        self.assertFalse(r["ok"])
        self.assertIn("不在线", r["error"])
        r = hub.Api().share_takeover_many(["d1", "d2"], "nope")
        self.assertIn("不存在", r["error"])
        self.assertEqual([], self.sent + self.queued)

    def test_auto_target_picks_an_online_agent_that_is_not_in_the_batch(self):
        self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                  _s("d2", "bbbb2222", "挂了的二", connected=False),
                  _s("b", "c2", "待命·乙", pending=True))
        with patch.object(hub.Hub, "_is_checkin_shellish", lambda h, x: False):
            r = hub.Api().share_takeover_many(["d1", "d2"], "auto")
        self.assertTrue(r["ok"], r)
        self.assertEqual("待命·乙", r["target"])

    def test_no_online_agent_says_so(self):
        self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                  _s("d2", "bbbb2222", "挂了的二", connected=False))
        r = hub.Api().share_takeover_many(["d1", "d2"], "")
        self.assertFalse(r["ok"])
        self.assertIn("没有在线", r["error"])

    def test_one_id_falls_through_to_the_single_path(self):
        self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                  _s("b", "c2", "待命·乙", pending=True))
        with patch.object(hub.Hub, "_is_checkin_shellish", lambda h, x: True):
            r = hub.Api().share_takeover_many(["d1"], "b", who="控制台")
        self.assertTrue(r["ok"], r)
        self.assertEqual(["挂了的一"], r["names"])
        self.assertEqual(1, r["count"])
        self.assertEqual(1, len(self.sent))
        self.assertNotIn("批量接手", self.sent[0][1])


class BatchGroupsByTargetTests(_Base):
    """手机 /api/takeover_batch：同一接手方的几对合成一条；「自动」先把人分开。"""

    def test_two_dead_sessions_to_the_same_shell_no_longer_fail_on_the_second(self):
        d = self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                      _s("d2", "bbbb2222", "挂了的二", connected=False),
                      _s("b", "c2", "待命·乙", pending=True))
        with patch.object(hub.Hub, "_is_checkin_shellish", lambda h, x: True):
            r = hub.Api().share_takeover_batch(
                [{"sid": "d1", "target": "b"}, {"sid": "d2", "target": "b"}], who="手机")
        self.assertTrue(r["ok"], r)
        self.assertEqual(2, len(r["done"]), r)
        self.assertEqual([], r["failed"])
        self.assertEqual(1, len(self.sent), "同一接手方只该收到一条合并消息")
        self.assertNotIn("b", d)
        self.assertEqual([("b", "d1")], self.aliased)

    def test_auto_spreads_over_idle_agents_before_doubling_up(self):
        self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                  _s("d2", "bbbb2222", "挂了的二", connected=False),
                  _s("d3", "cccc3333", "挂了的三", connected=False),
                  _s("b1", "c1", "待命·甲", pending=True),
                  _s("b2", "c2", "待命·乙", pending=True))
        with patch.object(hub.Hub, "_is_checkin_shellish", lambda h, x: False):
            r = hub.Api().share_takeover_batch(
                [{"sid": "d1", "target": "auto"}, {"sid": "d2", "target": ""},
                 {"sid": "d3", "target": "auto"}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(3, len(r["done"]))
        targets = sorted(x.split(" → ")[1] for x in r["done"])
        # 两个闲人各分到活，第三张才合到其中一人头上
        self.assertEqual({"待命·甲", "待命·乙"}, set(targets))
        self.assertEqual(2, len(self.sent), "两个接手方 = 两条消息（其中一条是合并单）")

    def test_mixed_batch_keeps_single_dispatch_for_lone_targets(self):
        self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                  _s("d2", "bbbb2222", "挂了的二", connected=False),
                  _s("b1", "c1", "待命·甲", pending=True),
                  _s("b2", "c2", "待命·乙", pending=True))
        with patch.object(hub.Hub, "_is_checkin_shellish", lambda h, x: False):
            r = hub.Api().share_takeover_batch(
                [{"sid": "d1", "target": "b1"}, {"sid": "d2", "target": "b2"},
                 {"sid": "d9", "target": "b1"}])
        self.assertEqual(2, len(r["done"]))
        self.assertEqual(1, len(r["failed"]))
        self.assertIn("d9", r["failed"][0])
        # d1+d9 分到 b1：d9 不存在只算一张，走单张路；b2 也是单张
        for _sid, text in self.sent:
            self.assertNotIn("批量接手", text)

    def test_no_agent_online_reports_each_pair(self):
        self._hub(_s("d1", "aaaa1111", "挂了的一", connected=False),
                  _s("d2", "bbbb2222", "挂了的二", connected=False))
        r = hub.Api().share_takeover_batch([{"sid": "d1"}, {"sid": "d2", "target": "auto"}])
        self.assertFalse(r["ok"])
        self.assertEqual(2, len(r["failed"]))
        self.assertIn("没有在线", r["failed"][0])


class PickTargetExcludeTests(unittest.TestCase):
    def test_exclude_skips_agents_already_taken_this_batch(self):
        dead = _s("d", "cd", "挂了的", connected=False)
        a = _s("a", "c1", "待命·甲", pending=True)
        b = _s("b", "c2", "待命·乙", pending=False)
        with patch.object(hub.HUB, "sessions", {"d": dead, "a": a, "b": b}), \
             patch.object(hub.HUB, "order", ["d", "a", "b"]):
            self.assertEqual("a", hub.Api().pick_takeover_target("d").id)
            self.assertEqual("b", hub.Api().pick_takeover_target("d", exclude={"a"}).id)
            self.assertIsNone(hub.Api().pick_takeover_target("d", exclude={"a", "b"}))


class DesktopUiWiringTests(unittest.TestCase):
    """桌面 ui.html 的多选接线：右键菜单项 / Ctrl+点 / 顶部选中条 / 两个批量动作。"""

    @classmethod
    def setUpClass(cls):
        cls.ui = (MODULE_DIR / "ui.html").read_text(encoding="utf-8")

    def test_menu_has_the_batch_actions_and_handlers(self):
        for needle in ('data-action="pick"', 'data-action="dispatch-many"',
                       'data-action="takeover-many"', 'data-action="pick-clear"',
                       'action === "dispatch-many"', 'action === "takeover-many"',
                       'action === "pick"', 'action === "pick-clear"'):
            self.assertIn(needle, self.ui, needle)

    def test_ctrl_click_toggles_pick_instead_of_switching(self):
        self.assertIn("e.ctrlKey || e.metaKey", self.ui)
        self.assertIn("togglePick(s.id)", self.ui)

    def test_batch_calls_reach_the_new_api_methods(self):
        self.assertIn("api().share_takeover_many(ids, btn.dataset.take", self.ui)
        self.assertIn("api().get_takeover_prompt_many(ids)", self.ui)
        self.assertIn("function buildPickBar", self.ui)
        self.assertIn('picked.has(s.id) ? " picked"', self.ui)

    def test_pick_state_is_part_of_the_tabs_signature(self):
        # 不进签名，勾选后列表不重画，勾了也看不见
        sig = self.ui[self.ui.index("function tabsSignature()"):]
        sig = sig[:sig.index("\n}\n")]
        self.assertIn("picked.has(s.id)", sig)


if __name__ == "__main__":
    unittest.main()
