# -*- coding: utf-8 -*-
"""控制台唤起 Cursor 对话对不上 bajie（09-03 11:28 rxyy 实测口径）。

rxyy 原话：「bajie 那个 mcp 做的挺好……直接从前端就能唤 cursor IDE 窗口的会话，这个功能
做了吗？跟其做成一样的逻辑+ui」。7ae86ff 的底座（ext_bus + 扩展 + 四个 ext_* API）当时
已经在了，差的全在控制台这一层，四条各自钉死：

1. 数量与起名：只有「每个窗口 1/2/3/4 个」四个死按钮，开出来的 tab 一律叫
   「待命·<工作区>」，一批开五个分不清谁是谁——bajie 侧栏是「N 个 + 起名前缀」。前缀在
   服务端 ext_batch_open(name_prefix=) 展开而不是只在控制台拼字符串，手机页和 MCP
   直调才能拿到同一套命名。
2. 过程：开完只有一个 toast，几个成了几个没成、挂载确认没确认全看不见。
3. 多窗口：没有作用域，装了扩展的几个 Cursor 窗口的会话混在一列里，也没法指定新建到哪。
4. 出生窗口：hub 早把 ext_instance 记在会话对象上了（_ext_prepare_shell），却没进
   get_state 的 tab 载荷，前端既不能按它过滤也不能显示。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import ext_bus  # noqa: E402
import hub  # noqa: E402

WS1 = r"d:\Desktop\cursor工作流"
UI = (MODULE_DIR / "ui.html").read_text(encoding="utf-8")


class _OpenBase(unittest.TestCase):
    """与 test_ext_bus 同一套夹具：假 instances + 假 batchOpen，不碰真窗口。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ext_bus.set_root(Path(self.tmp.name) / "extbus")
        self.addCleanup(lambda: ext_bus.set_root(None))
        self.addCleanup(self.tmp.cleanup)
        self.sessions = {}
        for p in (patch.object(hub.HUB, "sessions", self.sessions),
                  patch.object(hub.HUB, "order", []),
                  patch.object(hub.HUB, "prebound_composers", {}),
                  patch.object(hub.HUB, "cfg", {"max_messages": 200}),
                  patch.object(hub.Hub, "save_state", lambda self: None),
                  patch.object(hub.Hub, "_init_file", lambda self, s: None),
                  patch.object(hub.Hub, "save_msg_images", lambda self, p: []),
                  patch.object(hub, "log_event", lambda *a, **k: None)):
            p.start()
        self.addCleanup(patch.stopall)
        self.inst = {"id": "win-1", "label": "cursor工作流", "workspace": WS1, "pid": 7,
                     "version": "3.17.8", "updatedAt": time.time() * 1000, "age_ms": 100}

        def fake_call(instance_id, action, payload=None, timeout=25):
            items = (payload or {}).get("items", [])
            return {"ok": True, "created": len(items), "verified": len(items),
                    "results": [{"ok": True, "composerId": it["composerId"], "mounted": True}
                                for it in items]}
        patch.object(ext_bus, "live_instances", lambda *a, **k: [self.inst]).start()
        patch.object(ext_bus, "call", fake_call).start()

    def opened_names(self, count=1, **kw):
        r = hub.Api().ext_batch_open("win-1", count, **kw)
        self.assertTrue(r["ok"], r)
        return [o["name"] for o in r["opened"]]


class NamePrefixTests(_OpenBase):
    """① 起名前缀：一批开出来的 tab 得能分得清谁是谁。"""

    def test_prefix_numbers_every_tab_so_one_batch_is_tellable_apart(self):
        self.assertEqual(["审校·1", "审校·2", "审校·3"],
                         self.opened_names(3, name_prefix="审校"))

    def test_single_open_keeps_the_prefix_bare(self):
        # 只开一个还叫「审校·1」是噪音
        self.assertEqual(["审校"], self.opened_names(1, name_prefix="审校"))

    def test_no_prefix_repeats_the_workspace_name_which_is_why_prefix_exists(self):
        got = self.opened_names(3)
        self.assertEqual("待命·cursor工作流", got[0])
        # 同名壳由 create_spawn_shell 追一段随机后缀区分，一批开出来是
        # 待命·cursor工作流 / 待命·cursor工作流·1fb / 待命·cursor工作流·7a2——
        # 互不相同但认不出谁是谁，这正是起名前缀要解决的事
        for nm in got[1:]:
            self.assertTrue(nm.startswith("待命·cursor工作流·"), nm)
        self.assertEqual(3, len(set(got)))

    def test_explicit_names_win_and_the_prefix_only_fills_the_rest(self):
        self.assertEqual(["甲", "审校·2", "审校·3"],
                         self.opened_names(3, names=["甲"], name_prefix="审校"))

    def test_absurd_prefix_is_clipped_not_rejected(self):
        got = self.opened_names(1, name_prefix="长" * 60)
        self.assertEqual(24, len(got[0]), "前缀截到 24 字，一条 tab 名不把侧栏撑爆")

    def test_prefix_is_only_a_default_so_old_callers_are_untouched(self):
        # ext_takeover_new 一直用 names= 传「接手·xxx」，多一个默认参数不能改它的名字
        self.assertEqual(["接手·某会话"], self.opened_names(1, names=["接手·某会话"]))

    def test_who_stays_the_fifth_positional_arg(self):
        # 控制台是按位置传的（instance, count, cwd, names, who, name_prefix）；
        # name_prefix 插在 who 前面就会把「控制台」当成前缀，一批 tab 全叫「控制台·1」
        r = hub.Api().ext_batch_open("win-1", 1, "", [], "控制台", "审校")
        self.assertEqual(["审校"], [o["name"] for o in r["opened"]])


class BirthWindowPayloadTests(unittest.TestCase):
    """④ 出生窗口得进 tab 载荷，前端才能按它过滤 / 显示。"""

    def test_get_state_ships_ext_instance(self):
        src = (MODULE_DIR / "hub_api.py").read_text(encoding="utf-8")
        self.assertIn('"ext_instance": str(getattr(s, "ext_instance", "") or ""),', src)


class ConsoleScopeTests(unittest.TestCase):
    """③ 窗口作用域条。"""

    def test_scope_bar_exists(self):
        for anchor in ('id="extScopeBar"', 'id="extScopeSel"', 'id="extScopeHid"',
                       "function sessionInScope", "function renderExtScopeBar",
                       "async function refreshExtInsts"):
            self.assertIn(anchor, UI, anchor)

    def test_one_window_means_no_bar_and_no_stuck_scope(self):
        # 关到只剩一个窗口时作用域自动松开，否则列表可能空着还没人知道为什么
        self.assertIn("if (extInsts.length < 2)", UI)
        self.assertIn('localStorage.removeItem("cj_ext_scope")', UI)

    def test_hand_opened_sessions_fall_back_to_the_workspace(self):
        # 只认 ext_instance 会把人手粘报到词开的会话全藏了——那比不过滤还难用
        self.assertIn("if (s.ext_instance) return s.ext_instance === w.id;", UI)
        self.assertIn("normWs(s.cwd) === normWs(w.workspace)", UI)

    def test_scope_is_remembered_filters_the_list_and_aims_the_new_dialog(self):
        self.assertIn('localStorage.setItem("cj_ext_scope"', UI)
        self.assertIn("if (!sessionInScope(s)) continue;", UI)
        self.assertIn("if (scoped) exoWin = scoped.id;", UI)
        self.assertIn('+ "#w" + extScope;', UI, "作用域进 tab 签名，换了才重画")


class ConsoleOpenModalTests(unittest.TestCase):
    """bf297f5 把 Bajie 的轻弹窗做重了：这里钉住截图里的默认新建体验。"""

    def test_new_chat_is_a_fixed_full_width_row_below_the_scroller(self):
        for anchor in ('id="newChatRow"', 'id="newChatBtn"',
                       '$("newChatBtn").onclick = openExtOpenModal'):
            self.assertIn(anchor, UI, anchor)
        self.assertLess(UI.index('id="tabs" title='), UI.index('id="newChatRow"'))
        self.assertLess(UI.index('id="newChatRow"'), UI.index('id="legend"'))
        self.assertNotIn("buildPlusTab(box);", UI)

    def test_default_modal_only_has_bajies_count_field_and_two_actions(self):
        for anchor in ('id="extOpenModal"', '>新建对话</div>',
                       '批量创建数量（1-50）', 'id="exo_count" min="1" max="50"',
                       'id="exoCancel">取消</button>', 'id="exoGo" class="primary">新建</button>'):
            self.assertIn(anchor, UI, anchor)
        for old in ('id="exo_prefix"', 'id="exoQuick"', 'id="exoNameHint"', 'id="exoRes"'):
            self.assertNotIn(old, UI, old)

    def test_count_is_clamped_to_bajies_fifty_in_the_console_too(self):
        self.assertIn("Math.max(1, Math.min(50, n))", UI)

    def test_only_ambiguous_multi_window_creation_adds_a_target_picker(self):
        for anchor in ('id="exoWindowRow"', '目标项目窗口', 'id="exo_win"',
                       'const needsChoice = extInsts.length > 1 && !scoped;',
                       '$("exoWindowRow").style.display = needsChoice ? "block" : "none";'):
            self.assertIn(anchor, UI, anchor)

    def test_creation_progress_and_result_use_toasts_and_busy_button_copy(self):
        for anchor in ('$("exoGo").textContent = busy ? "创建中…" : "新建";',
                       'toast(n === 1 ? "正在创建对话…"',
                       'count === 1 ? "已新建对话" : "已新建 " + count + " 个对话"'):
            self.assertIn(anchor, UI, anchor)

    def test_old_plus_menu_survives_as_the_right_click_secondary_entry(self):
        self.assertIn('$("newChatBtn").oncontextmenu = (e) => {', UI)
        self.assertIn("showPlusMenu(r.left, r.top);", UI)
        self.assertIn('id="plusMenu"', UI)

    def test_no_window_online_disables_create_and_uses_a_toast(self):
        self.assertIn('$("exoGo").disabled = !extInsts.length;', UI)
        self.assertIn('toast("没有在线的 Cursor 项目窗口', UI)

    def test_birth_window_shows_up_on_the_tab(self):
        self.assertIn("function extBirthLabel", UI)
        self.assertIn("出生窗口：", UI)

    def test_the_old_fixed_1234_button_menu_is_gone(self):
        # 用布尔断言而不是 assertNotIn：ui.html 有 17 万字符，assertNotIn 一失败就把整页
        # 糊进测试报告（本轮在旧提交上跑红时实测 28 万字符输出）
        self.assertFalse("showExtOpenMenu" in UI, "旧的 1/2/3/4 菜单函数还在")
        self.assertFalse("[1, 2, 3, 4].map(n =>" in UI, "旧的四个死按钮还在")


if __name__ == "__main__":
    unittest.main()
