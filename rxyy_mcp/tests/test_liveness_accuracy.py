# -*- coding: utf-8 -*-
"""存活判定的准确性：zt 采信窗口、时间文案、打包目录假工作区、团队归组祖先归并"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

WS1 = r"d:\Desktop\cursor工作流"
WS2 = r"c:\Users\Administrator\AICodebrain"
PACKED = WS1 + r"\dist\rxyy-tools\_internal\rxyy-mcp"
LIVE_RESIDENT = r"C:\Users\sunrise\AppData\Local\rxyy-tools\live\rxyy-mcp"


class SanitizeWsPathTests(unittest.TestCase):
    def test_packaged_internal_path_folds_back_to_repo_root(self):
        self.assertEqual(WS1, hub.sanitize_ws_path(PACKED))
        self.assertEqual(WS1, hub.sanitize_ws_path(WS1 + r"\dist\rxyy-tools"))

    def test_case_and_slash_variants_still_match(self):
        self.assertEqual(r"D:\Desktop\cursor工作流",
                         hub.sanitize_ws_path("D:/Desktop/cursor工作流/DIST/RXYY-TOOLS/_internal"))

    def test_normal_paths_pass_through(self):
        self.assertEqual(WS1, hub.sanitize_ws_path(WS1))
        self.assertEqual("", hub.sanitize_ws_path(""))
        # 相对路径没有可折回的父目录，别硬剥
        self.assertEqual(r"dist\rxyy-tools", hub.sanitize_ws_path(r"dist\rxyy-tools"))

    def test_live_resident_path_is_rejected_not_kept_as_workspace(self):
        live = r"C:\Users\sunrise\AppData\Local\rxyy-tools\live\rxyy-mcp"
        self.assertTrue(hub.is_runtime_ws_path(live))
        self.assertEqual("", hub.sanitize_ws_path(live))
        self.assertFalse(hub.is_runtime_ws_path(WS1))

    def test_scrub_runtime_team_cfg_drops_live_roots(self):
        live = r"C:\Users\sunrise\AppData\Local\rxyy-tools\live\rxyy-mcp"
        cfg = {"team_seats": {live: [], WS1: [{"id": "a"}]},
               "team_boards": {live: {"text": "x"}}}
        bulletin = {live: [], "_scopes": {}}
        self.assertTrue(hub.scrub_runtime_team_cfg(cfg, bulletin))
        self.assertNotIn(live, cfg["team_seats"])
        self.assertIn(WS1, cfg["team_seats"])
        self.assertNotIn(live, bulletin)
        self.assertIn("_scopes", bulletin)

    def test_runtime_cwd_does_not_overwrite_a_real_workspace(self):
        s = _live_session()
        s.cwd = WS1
        s.task_root = WS1
        s.rev = 1
        live = r"C:\Users\sunrise\AppData\Local\rxyy-tools\live\rxyy-mcp"
        self.assertFalse(hub.apply_session_cwd(s, live))
        self.assertEqual(WS1, s.cwd)
        s.cwd = live
        hub.heal_session_paths(s)
        self.assertEqual(WS1, s.cwd)
        s.cwd = ""
        hub.heal_session_paths(s)
        self.assertEqual(WS1, s.cwd)


class AgeTextTests(unittest.TestCase):
    def test_hours_do_not_read_like_clock_time(self):
        api = hub.Api()
        self.assertEqual("30秒前", api._age_text(30))
        self.assertEqual("1分钟前", api._age_text(90))
        # 原来输出「21时13分前」，读起来像时刻 21:13
        self.assertEqual("21小时前", api._age_text(21 * 3600 + 13 * 60))
        self.assertEqual("3天前", api._age_text(3 * 86400 + 3600))


def _live_session(**kw):
    s = hub.Session.__new__(hub.Session)
    s.id = kw.get("id", "s1")
    s.connected = kw.get("connected", True)
    s.pending = None
    s.recon_deadline = None
    s.queued = []
    s.lock = threading.Lock()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class ZtFreshnessTests(unittest.TestCase):
    """zt 自报要在 10 分钟内被采信；transcript 与 zt 谁新听谁"""

    def _liveness(self, s, act=None):
        api = hub.Api()
        with patch.object(hub, "read_cursor_activity", lambda uuid: act or {}):
            return api._agent_liveness(s, time.time())

    def test_fresh_zt_report_beats_an_older_transcript(self):
        # 3 分钟前刚自报 developing、transcript 14 分钟没动：是在干活，不是「刚才还在动」
        now = time.time()
        s = _live_session(agent_status="developing", agent_status_ts=now - 180,
                          last_heartbeat=now)
        r = self._liveness(s, act={"updated_ts": now - 14 * 60})
        self.assertEqual("working", r["state"])
        self.assertIn("自报", r["label"])

    def test_fresh_transcript_stays_the_primary_evidence(self):
        now = time.time()
        s = _live_session(agent_status="developing", agent_status_ts=now - 170,
                          last_heartbeat=now)
        r = self._liveness(s, act={"updated_ts": now - 60})
        self.assertEqual("working", r["state"])

    def test_stale_zt_report_is_not_trusted_forever(self):
        # 11 分钟前的自报不再算「干活中」，但展示时刻要取最新证据（11分钟，不是14分钟）
        now = time.time()
        s = _live_session(agent_status="developing", agent_status_ts=now - 11 * 60,
                          last_heartbeat=now)
        r = self._liveness(s, act={"updated_ts": now - 14 * 60})
        self.assertEqual("recent", r["state"])
        self.assertIn("11分钟前", r["label"])


class TurnEndedDirectDetectionTests(unittest.TestCase):
    """直接探测（07-31 用户拍板：去掉自动问话，改为不需要 agent 配合的探测）：
    对话流水最后一个事件是 turn_ended = 这轮真收工了，agent 不会再自己说话。
    但流水必须验明正身（文件里出现过本对话 ID）——就近猜来的别人文件不认账。"""

    CONV = "c1"

    def _transcript(self, td, lines, mention=True):
        import json as _json
        p = Path(td) / "t.jsonl"
        rows = []
        if mention:
            # agent 调 zhi 时 conversation_id 必进工具参数——正常流水都长这样
            rows.append({"role": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "zhi",
                 "input": {"conversation_id": self.CONV, "message": "报到"}}]}})
        rows.extend(lines)
        p.write_text("\n".join(_json.dumps(x) for x in rows) + "\n", encoding="utf-8")
        return str(p)

    def _sess(self, now, tp):
        # last_zhi_ts = 11 分钟前：跟本次 hub 说过话（没说过的不判——重启失忆≠沉默），
        # 之后沉默超过 10 分钟采信闸
        return _live_session(agent_status="", agent_status_ts=0,
                             last_heartbeat=now, processing_since=None,
                             cursor_uuid=None, transcript_path=tp,
                             conv_key=self.CONV, last_zhi_ts=now - 11 * 60)

    def _liveness(self, s):
        api = hub.Api()
        with patch.object(hub, "read_cursor_activity", lambda uuid: {}):
            return api._agent_liveness(s, time.time())

    def test_turn_ended_tail_means_agent_wont_speak_again(self):
        import tempfile
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            tp = self._transcript(td, [
                {"role": "assistant", "message": {"content": []}},
                {"type": "turn_ended", "status": "completed"},
            ])
            r = self._liveness(self._sess(now, tp))
        self.assertEqual("turn_done", r["state"])
        self.assertIn("本轮已结束", r["label"])

    def test_error_ended_turn_reads_as_interrupted(self):
        import tempfile
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            tp = self._transcript(td, [{"type": "turn_ended", "status": "error"}])
            r = self._liveness(self._sess(now, tp))
        self.assertEqual("turn_done", r["state"])
        self.assertIn("中断", r["label"])

    def test_someone_elses_ended_transcript_is_not_trusted(self):
        # 就近猜的定位可能指到别人的收工文件：文件里没有本对话 ID 就不许下结论
        # （07-31 实测：cursor工作流1 被隔壁死会话的 turn_ended 判成「已中断」）
        import tempfile
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            tp = self._transcript(td, [{"type": "turn_ended", "status": "error"}],
                                  mention=False)
            r = self._liveness(self._sess(now, tp))
        self.assertNotEqual("turn_done", r["state"])

    def test_agent_recently_talking_to_console_is_not_turn_done(self):
        # 刚跟控制台说过话（zhi/zt）的必然活着，甭管流水里写了什么
        # （07-31 实测：AICodebrain集成 刚 zhi 完就被判中断）
        import tempfile
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            tp = self._transcript(td, [{"type": "turn_ended", "status": "error"}])
            s = self._sess(now, tp)
            s.last_zhi_ts = now - 30
            r = self._liveness(s)
        self.assertNotEqual("turn_done", r["state"])

    def test_never_spoke_to_this_hub_is_amnesia_not_silence(self):
        # 重启后 last_zhi_ts 归零：那是 hub 失忆，不是它沉默了 10 分钟
        # （07-31 实测：重启后 AICodebrain集成 又被判了一次中断）
        import tempfile
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            tp = self._transcript(td, [{"type": "turn_ended", "status": "error"}])
            s = self._sess(now, tp)
            s.last_zhi_ts = 0
            r = self._liveness(s)
        self.assertNotEqual("turn_done", r["state"])

    def test_new_turn_after_turn_ended_is_working_again(self):
        import tempfile
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            tp = self._transcript(td, [
                {"type": "turn_ended", "status": "completed"},
                {"role": "assistant", "message": {"content": []}},
            ])
            r = self._liveness(self._sess(now, tp))
        self.assertNotEqual("turn_done", r["state"])
        self.assertEqual("working", r["state"])  # 文件刚写过 = 正在动

    def test_waiting_on_zhi_wins_over_turn_state(self):
        now = time.time()
        s = _live_session(agent_status="", agent_status_ts=0,
                          last_heartbeat=now, processing_since=None,
                          cursor_uuid=None, transcript_path=None,
                          conv_key=self.CONV)
        s.pending = {"id": "q1", "created": now}
        api = hub.Api()
        with patch.object(hub, "read_cursor_activity", lambda uuid: {}):
            r = api._agent_liveness(s, now)
        self.assertEqual("waiting", r["state"])


class TeamGroupAncestorMergeTests(unittest.TestCase):
    def _team_state(self, sessions):
        api = hub.Api()
        d = {s.id: s for s in sessions}
        live = {"state": "idle", "label": "", "sure": True, "evidence": [], "death": None}
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", list(d)),
              # team_state 现在会展示仅有旧根级黑板的默认项目；该归并测试只测
              # session root，不能读进运行时/其他用例遗留的全局黑板状态。
              patch.object(hub.HUB, "bulletin", {}),
              patch.object(hub.HUB, "_window_tag", lambda pid, cwd=None, transcript_path=None: ""),
              patch.dict(hub.HUB.cfg, {"team_seats": {}}),
              patch.object(hub.Api, "agentboard", lambda self: {"items": []}),
              patch.object(hub.Api, "_agent_liveness", lambda self, s, now: dict(live)),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub.Api, "_team_role", lambda self, s: ""),
              patch.object(hub.Api, "_team_assign", lambda self, s: ""),
              patch.object(hub.Api, "_seat_of", lambda self, conv: (None, {})),
              patch.object(hub.Api, "_team_board", lambda self, root, project=None: {"text": "", "updated_at": 0}),
              patch.object(hub.Api, "_seats", lambda self, root, project=None: [])):
            return api.team_state()

    def _sess(self, sid, root, name="tab"):
        # connected=True 才配得上下面钉死的 idle：idle 是「已收工（通道还在）」，
        # 真实代码只在 s.connected 时给得出来；断了通道的现在不占面板行
        return _live_session(id=sid, conv_key="c" + sid, name=name, cwd=root,
                             task_root=root, pid=1, connected=True,
                             agent_status="", agent_activity="",
                             last_heartbeat=0, cursor_uuid=None)

    def test_packaged_dir_group_merges_into_its_repo_group(self):
        # 历史脏数据：task_root 落在打包目录里的 tab 不该单独成组
        r = self._team_state([self._sess("s1", WS1), self._sess("s2", PACKED),
                              self._sess("s3", WS2)])
        roots = {p["root"]: len(p["agents"]) for p in r["projects"]}
        self.assertEqual({WS1: 2, WS2: 1},
                         {k: v for k, v in roots.items()})

    def test_live_resident_group_is_dropped_but_packaged_still_merges(self):
        # 丢运行时组这一步必须排在祖先归并之后：先丢就把打包目录里的 tab 一起
        # 抹掉了（它本该并进自己的仓库组）。常驻区在面板里没有祖先，归并不到
        # 谁，才是真该丢的那一个。
        r = self._team_state([self._sess("s1", WS1), self._sess("s2", PACKED),
                              self._sess("s3", LIVE_RESIDENT)])
        roots = {p["root"]: len(p["agents"]) for p in r["projects"]}
        self.assertEqual({WS1: 2}, roots)

    def test_disjoint_projects_stay_apart(self):
        r = self._team_state([self._sess("s1", WS1), self._sess("s2", WS2)])
        self.assertEqual(2, len(r["projects"]))


class TeamPanelEndedFoldTests(unittest.TestCase):
    """已终止的会话不占团队面板行（07-31 用户拍板），折成 ended 计数"""

    def _team_state(self, sessions, per_state, board_items=None):
        api = hub.Api()
        d = {s.id: s for s in sessions}
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", list(d)),
              patch.object(hub.HUB, "_window_tag", lambda pid, cwd=None, transcript_path=None: ""),
              patch.dict(hub.HUB.cfg, {"team_seats": {}}),
              patch.object(hub.Api, "agentboard", lambda self: {"items": board_items or []}),
              patch.object(hub.Api, "_agent_liveness",
                           lambda self, s, now: {"state": per_state.get(s.id, "idle"),
                                                 "label": "", "sure": True,
                                                 "evidence": [], "death": None}),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub.Api, "_team_role", lambda self, s: ""),
              patch.object(hub.Api, "_team_assign", lambda self, s: ""),
              patch.object(hub.Api, "_seat_of", lambda self, conv: (None, {})),
              patch.object(hub.Api, "_team_board", lambda self, root, project=None: {"text": "", "updated_at": 0}),
              patch.object(hub.Api, "_seats", lambda self, root, project=None: [])):
            return api.team_state()

    def _sess(self, sid, root, connected, uuid=None):
        return _live_session(id=sid, conv_key="c" + sid, name="tab" + sid, cwd=root,
                             task_root=root, pid=1, connected=connected,
                             agent_status="", agent_activity="",
                             last_heartbeat=0, cursor_uuid=uuid)

    def test_dead_disconnected_sessions_fold_into_ended_count(self):
        alive = self._sess("s1", WS1, True)
        corpse = self._sess("s2", WS1, False)
        r = self._team_state([alive, corpse], {"s1": "idle", "s2": "dead"})
        p = [x for x in r["projects"] if x["root"] == WS1][0]
        self.assertEqual(["s1"], [a["id"] for a in p["agents"]])
        self.assertEqual(1, p["ended"])

    def test_reconnecting_sessions_still_get_a_row(self):
        # 重连宽限里的不算死，留在面板上（它可能马上回来）。hub / MCP 一重启
        # 全队都在这个状态里，把它们也藏掉的话面板会整个空掉。
        rec = self._sess("s1", WS1, False)
        r = self._team_state([rec], {"s1": "recon"})
        p = [x for x in r["projects"] if x["root"] == WS1][0]
        self.assertEqual(1, len(p["agents"]))
        self.assertEqual(0, p["ended"])
        self.assertEqual(0, p["offline"])

    def test_detached_sessions_fold_into_offline_count(self):
        # 通道断了但没死透的（ide=Cursor 里还活着·通道未接、lost=失联）同样
        # 不占面板行（08-04 用户：断掉的挂掉的就别显示了），但跟「已终止」分开计
        alive = self._sess("s1", WS1, True)
        detached = self._sess("s2", WS1, False)
        lost = self._sess("s3", WS1, False)
        r = self._team_state([alive, detached, lost],
                             {"s1": "idle", "s2": "ide", "s3": "lost"})
        p = [x for x in r["projects"] if x["root"] == WS1][0]
        self.assertEqual(["s1"], [a["id"] for a in p["agents"]])
        self.assertEqual(2, p["offline"])
        self.assertEqual(0, p["ended"])

    def test_auto_root_follows_file_locks(self):
        # 在干哪个项目的活就归到哪个项目：它的活跃锁全在 WS1 → 归属自动搬过去
        s = self._sess("s1", WS2, True, uuid="abcd1234-xxxx")
        items = [{"root": WS1, "owner": "abcd1234", "stale": False,
                  "file": "console/api/codebrain_api.py"}]
        persisted_roots = []
        with (patch.object(hub.HUB, "add_message", lambda self_s, m: None),
              patch.object(hub.HUB, "save_state",
                           side_effect=lambda: persisted_roots.append(s.task_root)) as save_state):
            r = self._team_state([s], {"s1": "working"}, board_items=items)
        self.assertEqual(WS1, s.task_root)
        roots = [p["root"] for p in r["projects"] if p["agents"]]
        self.assertEqual([WS1], roots)
        save_state.assert_called_once_with()
        self.assertEqual([WS1], persisted_roots)

    def test_cross_workspace_takeover_keeps_the_project_it_was_given(self):
        # 人坐在 WS2、接的是 WS1 的活（task_root=WS1、cwd=WS2，这是接手的常态）。
        # 它在自己工作区 WS2 里顺手改了个文件，不能因此被从 WS1 的队伍里拽走
        s = self._sess("s1", WS1, True, uuid="abcd1234-xxxx")
        s.cwd = WS2
        items = [{"root": WS2, "owner": "abcd1234", "stale": False,
                  "file": "notes.md"}]
        with patch.object(hub.HUB, "add_message", lambda self_s, m: None):
            r = self._team_state([s], {"s1": "working"}, board_items=items)
        self.assertEqual(WS1, s.task_root)
        self.assertEqual([WS1], [p["root"] for p in r["projects"] if p["agents"]])


if __name__ == "__main__":
    unittest.main()
