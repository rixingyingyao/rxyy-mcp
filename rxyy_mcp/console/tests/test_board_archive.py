# -*- coding: utf-8 -*-
"""卡片归档（软删除）与「测试不许写真库」那道闸。

两件事都源于 2026-08-13 那次事故：工单派活新加的「顺手建卡」在测试里没打桩，
一跑就往 rxyy 的真面板落了 6 张 [SCB-*] 垃圾卡。当时才发现两个缺口——
面板上**没有任何办法删卡**（只能停服务、备份、手改 .board.json），
以及**测试能直接写真库而毫无阻拦**。这里把两个口子都锁上。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CONSOLE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONSOLE_DIR))

from api.board.service import BoardService
from api.board.storage import BoardStorage, default_data_dir


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = BoardService(BoardStorage(Path(self.tmp.name)))
        self.card = self.svc.create_card({"title": "误建的卡", "project": "x"})["card"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_archived_cards_drop_out_of_the_default_list(self):
        self.assertEqual(1, len(self.svc.list_cards()["cards"]))
        reply = self.svc.archive_card(self.card["id"], {"reason": "测试漏打桩落进来的"})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([], self.svc.list_cards()["cards"], "归档的卡不该再占列")
        # 但数据一个字没少，明确要看时还在
        shown = self.svc.list_cards({"archived": True})["cards"]
        self.assertEqual(1, len(shown))
        self.assertTrue(shown[0]["archived"])

    def test_archiving_is_reversible(self):
        self.svc.archive_card(self.card["id"])
        back = self.svc.archive_card(self.card["id"], {"archived": False})
        self.assertTrue(back["ok"], back)
        self.assertEqual(1, len(self.svc.list_cards()["cards"]))

    def test_archiving_twice_is_a_no_op_not_an_error(self):
        self.svc.archive_card(self.card["id"])
        again = self.svc.archive_card(self.card["id"])
        self.assertTrue(again["ok"])
        self.assertFalse(again["changed"])

    def test_archive_leaves_a_trace(self):
        self.svc.archive_card(self.card["id"], {"reason": "误建"})
        events = self.svc.get_card(self.card["id"])["events"]
        self.assertEqual("archive", events[-1]["kind"])
        self.assertIn("误建", events[-1]["text"])

    def test_version_conflict_is_respected(self):
        stale = self.card["version"]
        self.svc.comment_card(self.card["id"], {"body": "先动一下"})
        reply = self.svc.archive_card(self.card["id"], {"version": stale})
        self.assertFalse(reply["ok"])
        self.assertEqual("VERSION_CONFLICT", reply["code"])


class RealBoardGuardTests(unittest.TestCase):
    """这条是回归锁：跑测试时写真库必须当场炸，而不是安静写进去。"""

    def test_writing_the_real_board_from_a_test_explodes(self):
        self.assertIn("PYTEST_CURRENT_TEST", os.environ, "这条锁只在 pytest 里成立")
        storage = BoardStorage()                      # 不传目录 = 真库
        self.assertEqual(default_data_dir(), storage.data_dir)
        with self.assertRaises(RuntimeError) as caught:
            storage.add_card({"id": "x", "title": "不该落盘的卡", "events": []})
        self.assertIn("不许写真看板库", str(caught.exception))

    def test_a_tmp_dir_is_still_free_to_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = BoardStorage(Path(tmp))
            BoardService(storage).create_card({"title": "临时库随便写"})
            self.assertEqual(1, len(storage.list_cards()))

    def test_the_guard_is_off_outside_pytest(self):
        # 控制台自己跑的时候当然要能写；这里模拟「不在 pytest 里」
        env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(BoardStorage().  # noqa: SLF001 - 就是要验这个内部标记
                             _guarded)


if __name__ == "__main__":
    unittest.main()
