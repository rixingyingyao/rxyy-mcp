# -*- coding: utf-8 -*-
"""Cursor 窗口总线（#6 最小方案，09-03 rxyy 拍板）：hub ↔ 扩展宿主的文件命令总线。

三层各自钉死：
1. ext_bus 协议：instances.json 判活、req/res 文件形状、取结果即删、超时撤请求、TTL 清扫
2. hub 侧预绑定：hub 自己指定的 composerId 在 agent 首次 zhi/zt 到达时直接认领、不扫库；
   被认领会话持有的 uid 不抢；过期作废
3. Api：ext_batch_open 预建壳 + 报到词 ≤1000B + createNew 失败即收壳；ext_takeover_new
   等报到后原路走 share_takeover 并把 composerId 钉到被接手的 tab；ext_open_cursor 先试出生窗口
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import ext_bus  # noqa: E402
import hub  # noqa: E402

WS1 = r"d:\Desktop\cursor工作流"


def _write_instances(root: Path, items):
    root.mkdir(parents=True, exist_ok=True)
    (root / "instances.json").write_text(json.dumps({"instances": items}), encoding="utf-8")


class _BusRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "extbus"
        ext_bus.set_root(self.root)
        self.addCleanup(lambda: ext_bus.set_root(None))
        self.addCleanup(self.tmp.cleanup)


class ProtocolTests(_BusRoot):
    def test_live_instances_drops_stale_and_bad_rows(self):
        now = time.time() * 1000
        _write_instances(self.root, [
            {"id": "aaa", "label": "cursor工作流", "updatedAt": now - 1000, "workspace": WS1, "pid": 1},
            {"id": "bbb", "label": "老窗口", "updatedAt": now - 5 * 60 * 1000},
            {"label": "没 id"}, "garbage",
        ])
        live = ext_bus.live_instances(now_ms=now)
        self.assertEqual(["aaa"], [x["id"] for x in live])
        self.assertEqual(WS1, live[0]["workspace"])
        self.assertLessEqual(live[0]["age_ms"], 1500)

    def test_missing_or_broken_instances_file_is_empty_not_crash(self):
        self.assertEqual([], ext_bus.live_instances())
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "instances.json").write_text("{not json", encoding="utf-8")
        self.assertEqual([], ext_bus.live_instances())

    def test_post_command_writes_req_with_protocol_fields(self):
        cid = ext_bus.post_command("win-1", "batchOpen", {"items": [{"name": "x"}]})
        files = list((self.root / "cmds" / "win-1").iterdir())
        self.assertEqual([cid + ".req.json"], [f.name for f in files])
        req = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual({"id": cid, "action": "batchOpen", "from": "hub"},
                         {k: req[k] for k in ("id", "action", "from")})
        self.assertEqual([{"name": "x"}], req["payload"]["items"])
        self.assertLess(abs(req["createdAt"] - time.time() * 1000), 5000)
        self.assertFalse(list((self.root / "cmds" / "win-1").glob("*.tmp")), "原子写不许留 tmp")

    def test_await_result_returns_and_removes_res_file(self):
        cid = ext_bus.post_command("win-1", "ping")
        res = self.root / "cmds" / "win-1" / (cid + ".res.json")

        def responder():
            time.sleep(0.2)
            res.write_text(json.dumps({"id": cid, "result": {"ok": True, "pong": 1}}), encoding="utf-8")
        threading.Thread(target=responder, daemon=True).start()
        got = ext_bus.await_result("win-1", cid, timeout=3)
        self.assertEqual({"ok": True, "pong": 1}, got)
        self.assertFalse(res.exists(), "结果取走即删")

    def test_await_timeout_returns_none_and_withdraws_request(self):
        cid = ext_bus.post_command("win-1", "batchOpen", {})
        req = self.root / "cmds" / "win-1" / (cid + ".req.json")
        self.assertTrue(req.exists())
        t0 = time.time()
        self.assertIsNone(ext_bus.await_result("win-1", cid, timeout=0.4))
        self.assertLess(time.time() - t0, 2.0)
        self.assertFalse(req.exists(), "超时要把请求撤回，别让泵几分钟后醒来替我们再开一个对话")

    def test_call_without_instance_says_so_without_writing(self):
        r = ext_bus.call("nobody", "ping")
        self.assertFalse(r["ok"])
        self.assertEqual("NO_INSTANCE", r["code"])
        self.assertFalse((self.root / "cmds").exists())

    def test_call_roundtrip_with_fake_pump(self):
        _write_instances(self.root, [{"id": "win-1", "label": "w", "updatedAt": time.time() * 1000}])

        def pump():
            d = self.root / "cmds" / "win-1"
            for _ in range(50):
                time.sleep(0.05)
                for f in list(d.glob("*.req.json")) if d.exists() else []:
                    req = json.loads(f.read_text(encoding="utf-8"))
                    f.unlink()
                    (d / (req["id"] + ".res.json")).write_text(json.dumps({
                        "id": req["id"], "result": {"created": 1, "verified": 1,
                                                    "echo": req["payload"]}}), encoding="utf-8")
                    return
        threading.Thread(target=pump, daemon=True).start()
        r = ext_bus.call("win-1", "batchOpen", {"items": [1]}, timeout=5)
        self.assertTrue(r["ok"], r)            # 结果没写 ok 时默认成功
        self.assertEqual(1, r["created"])
        self.assertEqual({"items": [1]}, r["echo"])

    def test_call_timeout_shape(self):
        _write_instances(self.root, [{"id": "win-1", "label": "w", "updatedAt": time.time() * 1000}])
        r = ext_bus.call("win-1", "ping", timeout=0.3)
        self.assertFalse(r["ok"])
        self.assertEqual("TIMEOUT", r["code"])

    def test_sweep_expired_only_touches_old_bus_files(self):
        d = self.root / "cmds" / "win-1"
        d.mkdir(parents=True)
        old = d / "old.req.json"
        old.write_text("{}", encoding="utf-8")
        import os
        stale = time.time() - 10 * 60
        os.utime(old, (stale, stale))
        fresh = d / "fresh.res.json"
        fresh.write_text("{}", encoding="utf-8")
        other = d / "note.txt"
        other.write_text("x", encoding="utf-8")
        os.utime(other, (stale, stale))
        self.assertEqual(1, ext_bus.sweep_expired())
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists())

    def test_bus_root_default_is_user_level_not_data_dir(self):
        ext_bus.set_root(None)
        with patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\x\AppData\Local", "RXYY_MCP_EXTBUS_ROOT": ""}):
            self.assertEqual(Path(r"C:\Users\x\AppData\Local\rxyy-mcp\extbus"), ext_bus.bus_root())
        with patch.dict("os.environ", {"RXYY_MCP_EXTBUS_ROOT": r"D:\bus"}):
            self.assertEqual(Path(r"D:\bus"), ext_bus.bus_root())


def _s(sid, conv, name, *, root=WS1, connected=True, pending=False, uuid=None, claimed=False):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.task_root = s.cwd = root
    s.connected = connected
    s.pending = {"id": "q", "message": "报到"} if pending else None
    s.queued = []
    s.msg_seq = 1
    s.handed_off_to = ""
    s.client = None
    s.lock = threading.Lock()
    s.file_path = None
    s.messages = []
    s.rev = 0
    s.cursor_uuid = uuid
    s.transcript_path = None
    s.uuid_verified = bool(uuid)
    s.claimed_task_ts = time.time() if claimed else 0
    return s


class _HubBase(_BusRoot):
    def setUp(self):
        super().setUp()
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


class PreboundBindTests(_HubBase):
    def test_first_call_binds_prebound_uuid_without_scanning(self):
        s = _s("s1", "abcd1234", "待命·cursor工作流", connected=True)
        self.sessions[s.id] = s
        hub.HUB.prebound_composers["abcd1234"] = {"uuid": "U-new", "ts": time.time(), "instance": "win-1"}
        with patch.object(hub, "cursor_db_session_for_conv", side_effect=AssertionError("不该扫库")), \
                patch.object(hub, "generating_now_session", side_effect=AssertionError("不该扫库")):
            hub.HUB._verify_identity_by_generating(s)
        self.assertEqual("U-new", s.cursor_uuid)
        self.assertTrue(s.uuid_verified)
        self.assertEqual("win-1", s.ext_instance)
        self.assertNotIn("abcd1234", hub.HUB.prebound_composers, "认领即消费，一次性")

    def test_prebound_takes_uuid_back_from_unclaimed_guess_but_not_from_claimed(self):
        s = _s("s1", "abcd1234", "待命·x")
        guess = _s("g", "gggg0000", "待命·y", uuid="U-new")
        self.sessions.update({s.id: s, guess.id: guess})
        hub.HUB.prebound_composers["abcd1234"] = {"uuid": "U-new", "ts": time.time()}
        with patch.object(hub.Hub, "_claimed_task", lambda self, x: False):
            self.assertTrue(hub.HUB._bind_prebound_composer(s, time.time()))
        self.assertEqual("U-new", s.cursor_uuid)
        self.assertIsNone(guess.cursor_uuid)
        self.assertFalse(guess.uuid_verified)
        # 被认领会话持有 → 不抢，预绑定作废，让扫库老路兜底
        s2 = _s("s2", "eeee5555", "待命·z")
        owner = _s("o", "oooo1111", "干活中", uuid="U-busy")
        self.sessions.update({s2.id: s2, owner.id: owner})
        hub.HUB.prebound_composers["eeee5555"] = {"uuid": "U-busy", "ts": time.time()}
        with patch.object(hub.Hub, "_claimed_task", lambda self, x: x is owner):
            self.assertFalse(hub.HUB._bind_prebound_composer(s2, time.time()))
        self.assertIsNone(s2.cursor_uuid)
        self.assertEqual("U-busy", owner.cursor_uuid)

    def test_expired_prebound_is_discarded(self):
        s = _s("s1", "abcd1234", "待命·x")
        self.sessions[s.id] = s
        hub.HUB.prebound_composers["abcd1234"] = {"uuid": "U-old", "ts": time.time() - 2 * 3600}
        self.assertFalse(hub.HUB._bind_prebound_composer(s, time.time()))
        self.assertIsNone(s.cursor_uuid)
        self.assertNotIn("abcd1234", hub.HUB.prebound_composers)

    def test_no_table_or_no_entry_is_a_noop(self):
        s = _s("s1", "abcd1234", "待命·x")
        self.assertFalse(hub.HUB._bind_prebound_composer(s, time.time()))
        with patch.object(hub.HUB, "prebound_composers", None):
            self.assertFalse(hub.HUB._bind_prebound_composer(s, time.time()))


class _ApiBase(_HubBase):
    def setUp(self):
        super().setUp()
        self.inst = {"id": "win-1", "label": "cursor工作流", "workspace": WS1,
                     "pid": 7, "version": "3.17.8", "updatedAt": time.time() * 1000, "age_ms": 100}
        self.calls = []
        self.reply = {"ok": True, "created": 1, "verified": 1, "results": []}

        def fake_call(instance_id, action, payload=None, timeout=25):
            self.calls.append((instance_id, action, payload))
            r = dict(self.reply)
            if action == "batchOpen" and not r.get("results"):
                r["results"] = [{"ok": True, "composerId": it["composerId"], "mounted": True}
                                for it in (payload or {}).get("items", [])]
                r["created"] = len(r["results"])
            return r
        patch.object(ext_bus, "live_instances", lambda *a, **k: [self.inst]).start()
        patch.object(ext_bus, "call", fake_call).start()


class BatchOpenTests(_ApiBase):
    def test_opens_n_shells_with_short_checkin_prompt_and_prebinding(self):
        r = hub.Api().ext_batch_open("win-1", 3, who="控制台")
        self.assertTrue(r["ok"], r)
        self.assertEqual(3, r["count"])
        self.assertEqual(3, len(self.sessions))
        inst, action, payload = self.calls[0]
        self.assertEqual(("win-1", "batchOpen"), (inst, action))
        self.assertEqual(3, len(payload["items"]))
        self.assertTrue(payload["autoSubmit"])
        for it, opened in zip(payload["items"], r["opened"]):
            # 首条消息过 Cursor 的 1024B 字节闸（08-31 事故口径），且带预分配对话 ID 与工作区
            self.assertLessEqual(len(it["prompt"].encode("utf-8")), hub.CURSOR_FIRST_MSG_MAX_UTF8)
            self.assertIn(opened["conversation_id"], it["prompt"])
            self.assertIn("cursor工作流", it["prompt"])
            self.assertEqual(it["composerId"], opened["composerId"])
            shell = self.sessions[opened["session_id"]]
            self.assertFalse(shell.connected)
            self.assertIsNone(shell.peer_ip)                 # 本机报到复活同壳
            self.assertEqual("win-1", shell.ext_instance)
            pre = hub.HUB.prebound_composers[opened["conversation_id"]]
            self.assertEqual(it["composerId"], pre["uuid"])
        self.assertTrue(all(o["name"].startswith("待命·cursor工作流") for o in r["opened"]))

    def test_count_is_clamped_to_fifty_split_into_ten_item_extension_calls_and_names_used(self):
        r = hub.Api().ext_batch_open("win-1", 99, names=["甲", "乙"])
        self.assertEqual(50, r["count"])
        batches = [call[2]["items"] for call in self.calls if call[1] == "batchOpen"]
        self.assertEqual([10, 10, 10, 10, 10], [len(items) for items in batches])
        self.assertEqual(50, sum(len(items) for items in batches))
        self.assertEqual("甲", r["opened"][0]["name"])
        self.assertEqual("乙", r["opened"][1]["name"])
        self.assertTrue(r["opened"][2]["name"].startswith("待命·"))

    def test_create_failure_reaps_the_pre_made_shell(self):
        self.reply = {"ok": False, "error": "composer.createNew 不存在", "results": []}

        def fake_call(instance_id, action, payload=None, timeout=25):
            return dict(self.reply)
        with patch.object(ext_bus, "call", fake_call):
            r = hub.Api().ext_batch_open("win-1", 2)
        self.assertFalse(r["ok"])
        self.assertIn("createNew", r["error"])
        self.assertEqual({}, self.sessions, "开不出对话就不能留一个永远重连中的 tab")
        self.assertEqual({}, hub.HUB.prebound_composers)

    def test_partial_failure_keeps_only_the_opened_ones(self):
        def fake_call(instance_id, action, payload=None, timeout=25):
            items = payload["items"]
            return {"ok": True, "created": 1, "verified": 1, "results": [
                {"ok": True, "composerId": items[0]["composerId"], "mounted": True},
                {"ok": False, "composerId": items[1]["composerId"], "error": "boom"}]}
        with patch.object(ext_bus, "call", fake_call):
            r = hub.Api().ext_batch_open("win-1", 2)
        self.assertTrue(r["ok"])
        self.assertEqual(1, r["count"])
        self.assertEqual(1, len(self.sessions))

    def test_shell_gets_the_composer_id_bound_right_away_for_death_probing(self):
        # 09-03 17:25 实测：无头开的对话被 AI 网关并发闸一句正文拒了。绑之前壳只会
        # 挂「重连中」到超时；绑上 composerId 后死因探针能读那个对话、当场说清原因
        r = hub.Api().ext_batch_open("win-1", 1)
        shell = self.sessions[r["opened"][0]["session_id"]]
        self.assertEqual(r["opened"][0]["composerId"], shell.cursor_uuid)
        self.assertEqual(shell.cursor_uuid, shell.death_probe_uid)
        self.assertFalse(shell.uuid_verified, "报到那一下才算验讫")

    def test_no_instance_online_explains_how_to_install(self):
        with patch.object(ext_bus, "live_instances", lambda *a, **k: []):
            r = hub.Api().ext_batch_open("", 1)
        self.assertFalse(r["ok"])
        self.assertIn("extbus_build.py", r["error"])
        self.assertEqual({}, self.sessions)

    def test_auto_pick_by_workspace_else_requires_choice(self):
        other = dict(self.inst, id="win-2", label="别的项目", workspace=r"d:\Desktop\other")
        with patch.object(ext_bus, "live_instances", lambda *a, **k: [self.inst, other]):
            r = hub.Api().ext_batch_open("auto", 1, cwd=WS1)
            self.assertTrue(r["ok"], r)
            self.assertEqual("win-1", r["instance"]["id"])
            r2 = hub.Api().ext_batch_open("", 1, cwd="")
            self.assertFalse(r2["ok"])
            self.assertIn("2 个", r2["error"])


class TakeoverNewTests(_ApiBase):
    def setUp(self):
        super().setUp()
        self.dead = _s("d1", "dead0001", "挂了的活", connected=False)
        self.sessions[self.dead.id] = self.dead
        self.dispatched = []

        def fake_share(api, session_id, target_id="", who=""):
            self.dispatched.append(("one", session_id, target_id, who))
            shell = hub.HUB.sessions.pop(target_id)          # 真实路径：alias_shell_into 把壳收了
            return {"ok": True, "target": shell.name, "name": "挂了的活", "queued": False}

        def fake_share_many(api, session_ids, target_id="", who=""):
            self.dispatched.append(("many", tuple(session_ids), target_id, who))
            shell = hub.HUB.sessions.pop(target_id)
            return {"ok": True, "target": shell.name, "count": len(session_ids)}
        patch.object(hub.Api, "share_takeover", fake_share).start()
        patch.object(hub.Api, "share_takeover_many", fake_share_many).start()
        patch.object(hub.Api, "EXT_CHECKIN_WAIT_SECS", 3).start()

    def _wait(self, pred, secs=4.0):
        t0 = time.time()
        while time.time() - t0 < secs:
            if pred():
                return True
            time.sleep(0.05)
        return False

    def test_opens_one_then_dispatches_when_shell_checks_in(self):
        r = hub.Api().ext_takeover_new("d1", "win-1", who="控制台")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["pending_checkin"])
        opened = r["opened"]
        shell = self.sessions[opened["session_id"]]
        self.assertTrue(shell.name.startswith("接手·挂了的活"))
        self.assertEqual(1, sum(1 for m in self.dead.messages if "无头开了新对话" in m["html"]))
        self.assertEqual([], self.dispatched, "报到前不许派单")
        # 模拟 agent 报到：壳连上且挂着报到 zhi
        shell.connected = True
        shell.pending = {"id": "q1", "message": "已就位"}
        self.assertTrue(self._wait(lambda: bool(self.dispatched)))
        self.assertEqual(("one", "d1", opened["session_id"], "控制台"), self.dispatched[0])
        # composerId 直接钉到被接手的 tab 上，不等扫库
        self.assertTrue(self._wait(lambda: self.dead.cursor_uuid == opened["composerId"]))
        self.assertTrue(self.dead.uuid_verified)

    def test_many_sessions_go_through_share_takeover_many(self):
        d2 = _s("d2", "dead0002", "挂了的二", connected=False)
        self.sessions[d2.id] = d2
        r = hub.Api().ext_takeover_new(["d1", "d2"], "win-1", who="手机")
        self.assertTrue(r["ok"], r)
        self.assertEqual(2, r["count"])
        shell = self.sessions[r["opened"]["session_id"]]
        self.assertEqual("接手·2个会话", shell.name)
        shell.connected = True
        shell.pending = {"id": "q1"}
        self.assertTrue(self._wait(lambda: bool(self.dispatched)))
        self.assertEqual(("many", ("d1", "d2"), r["opened"]["session_id"], "手机"), self.dispatched[0])
        self.assertTrue(self._wait(lambda: self.dead.cursor_uuid == r["opened"]["composerId"]))

    def test_gateway_concurrency_rejection_stops_waiting_and_explains(self):
        r = hub.Api().ext_takeover_new("d1", "win-1")
        shell = self.sessions[r["opened"]["session_id"]]
        shell.death_info = {"gate": True, "gate_kind": "gateway_text",
                            "reason": "AI 网关并发满（口令名额用完，不会自动重试）",
                            "advice": "先关掉别的正在跑的 Cursor 对话腾出名额"}
        self.assertTrue(self._wait(lambda: any("没跑起来" in m["html"] for m in self.dead.messages)))
        self.assertTrue(any("腾出名额" in m["html"] for m in self.dead.messages))
        self.assertEqual([], self.dispatched, "撞闸就别派单")

    def test_timeout_without_checkin_tells_the_tab(self):
        r = hub.Api().ext_takeover_new("d1", "win-1")
        self.assertTrue(r["ok"], r)
        self.assertTrue(self._wait(lambda: any("没报到" in m["html"] for m in self.dead.messages), secs=6))
        self.assertEqual([], self.dispatched)

    def test_unknown_session_rejected_before_opening_anything(self):
        r = hub.Api().ext_takeover_new("nope", "win-1")
        self.assertFalse(r["ok"])
        self.assertEqual([], self.calls)


class OpenCursorTests(_ApiBase):
    def test_tries_birth_window_first_then_others(self):
        s = _s("s1", "abcd1234", "活", uuid="U-1")
        s.ext_instance = "win-2"
        self.sessions[s.id] = s
        win2 = dict(self.inst, id="win-2", label="第二个窗口")
        order = []

        def fake_call(instance_id, action, payload=None, timeout=25):
            order.append(instance_id)
            self.assertEqual(("openCursor", {"composerId": "U-1"}), (action, payload))
            return {"ok": instance_id == "win-1", "via": "composer.openComposer",
                    "error": "" if instance_id == "win-1" else "not here"}
        with patch.object(ext_bus, "live_instances", lambda *a, **k: [self.inst, win2]), \
                patch.object(ext_bus, "call", fake_call):
            r = hub.Api().ext_open_cursor("s1")
        self.assertTrue(r["ok"])
        self.assertEqual(["win-2", "win-1"], order)
        self.assertEqual("win-1", r["instance"]["id"])

    def test_unbound_tab_is_refused(self):
        s = _s("s1", "abcd1234", "活")
        self.sessions[s.id] = s
        r = hub.Api().ext_open_cursor("s1")
        self.assertFalse(r["ok"])
        self.assertIn("还没定位", r["error"])
        self.assertEqual([], self.calls)


class ExtensionPackageTests(unittest.TestCase):
    """扩展包体与打包脚本的静态锚点：不装进 Cursor 也能钉住协议不漂。"""

    def test_manifest_and_source_anchor(self):
        ext = MODULE_DIR / "extbus-ext"
        meta = json.loads((ext / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(("rxyy", "rxyy_mcp-extbus", "./extension.js"),
                         (meta["publisher"], meta["name"], meta["main"]))
        props = meta["contributes"]["configuration"]["properties"]
        for k in ("rxyy_mcp.extbus.root", "rxyy_mcp.extbus.createCommand",
                  "rxyy_mcp.extbus.orderedIdsCommand", "rxyy_mcp.extbus.openCommands"):
            self.assertIn(k, props)
        src = (ext / "extension.js").read_text(encoding="utf-8")
        for anchor in ("composer.createNew", "composer.getOrderedSelectedComposerIds",
                       "aichat.openAgentById", "composer.openComposer",
                       "'.req.json'", "'.res.json'", "instances.json",
                       "'batchOpen'", "'openCursor'", "'ping'", "autoSubmit",
                       "dontRefreshReactiveContext", "hasChangedContext"):
            self.assertIn(anchor, src, anchor)
        # context 键表 35 个 + fileSelections 兜底：少一个 createNew 就炸
        import re
        keys = re.search(r"const CONTEXT_KEYS = \[(.*?)\];", src, re.S).group(1)
        self.assertEqual(35, len(re.findall(r"'([A-Za-z]+)'", keys)))
        for k in ("extraContext", "subagentSelections", "browserSelections", "notepads"):
            self.assertIn("'%s'" % k, keys)
        # 不注入、不碰安装文件：代码（去掉注释）里不许出现这些字样
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        for banned in ("workbench.desktop.main.js", "extensionHostProcess", "workbench.html", ".cursor/rules"):
            self.assertNotIn(banned, code)

    def test_console_and_phone_wiring_anchors(self):
        ui = (MODULE_DIR / "ui.html").read_text(encoding="utf-8")
        for anchor in ('data-action="ext-open"', 'data-action="ext-focus"', "ext_instances()",
                       "ext_takeover_new(ids, btn.dataset.ext",
                       # 左栏底部「新建对话」直接开 Bajie 同款轻弹窗；默认 UI 不露起名前缀，
                       # 仍走同一 ext_batch_open 后端（第 6 个参数显式留空）。
                       'ext_batch_open(w.id, n, "", [], "控制台", "")',
                       "ext_open_cursor(sessionId)", "async function openExtOpenModal",
                       "async function extInstancesQuiet"):
            self.assertIn(anchor, ui, anchor)
        share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        for anchor in ('"/api/ext_instances"', '"/api/takeover_new"', "async function pickTakeoverTarget"):
            self.assertIn(anchor, share, anchor)
        srv = (MODULE_DIR / "share_server.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/takeover_new"', srv)
        self.assertIn("api.ext_takeover_new(sids, payload.get(\"instance\")", srv)
        self.assertIn('path == "/api/ext_instances"', srv)
        self.assertIn("api.ext_instances()", srv)
        # 两条新路由都在「总令牌」闸后面：/api/takeover_new 走 startswith("/api/takeover") 那道，
        # /api/ext_instances 自己带一道
        idx = srv.index('path == "/api/ext_instances"')
        self.assertIn('self._scope() != "full"', srv[idx: idx + 400])

    def test_build_vsix_is_a_well_formed_zip(self):
        import zipfile
        import extbus_build
        with tempfile.TemporaryDirectory() as td:
            out = extbus_build.build_vsix(Path(td))
            self.assertEqual("rxyy.rxyy_mcp-extbus-0.1.0.vsix", out.name)
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
                self.assertEqual({"[Content_Types].xml", "extension.vsixmanifest",
                                  "extension/package.json", "extension/extension.js",
                                  "extension/README.md"}, names)
                man = z.read("extension.vsixmanifest").decode("utf-8")
                self.assertIn('Id="rxyy_mcp-extbus"', man)
                self.assertIn('Publisher="rxyy"', man)
                self.assertIn("Microsoft.VisualStudio.Code.Engine", man)
                self.assertIn('Path="extension/package.json"', man)


if __name__ == "__main__":
    unittest.main()
