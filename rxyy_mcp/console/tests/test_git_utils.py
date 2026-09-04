# -*- coding: utf-8 -*-
"""日报取数的两个坑（都由 8-06 那份日报实测出来）。

一、这台机器上的分支常落后于已经 fetch 下来的远端。公司机 8-06 在 live_api 提的
    两笔当天就推上去了，家里 Syncthing 同步回来的是 remotes/github/review，本地
    review 分支还停在 8-05——只走 HEAD 的 git log 一笔都看不见。
二、剪枝只看 .git/logs/HEAD 的 mtime。只 fetch 不合并时 logs/HEAD 根本不动，
    于是 live_api 连探都没被探到就整个仓库剪掉了。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CONSOLE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONSOLE_DIR))

from api.git_utils import _logs_head_mtime_date, commits_in_range  # noqa: E402

ME = "rixingyingyao"
COLLEAGUE = "xumin"


def _git(repo: str, *args: str, author: str = ME, when: str = "2026-08-06 17:54:00") -> None:
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=f"{author}@example.com",
        GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL=f"{author}@example.com",
        GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when,
    )
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, env=env)


class LaggingBranchTests(unittest.TestCase):
    """复刻 live_api：本人的提交只存在于远端跟踪分支上，本地分支落后。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        repo = cls.repo = str(Path(cls._tmp.name) / "live_api")
        os.makedirs(repo)
        subprocess.run(["git", "-C", repo, "init", "-q", "-b", "review"],
                       check=True, capture_output=True)

        (Path(repo) / "a.txt").write_text("1", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "线A staging 首次部署", when="2026-08-05 10:00:00")

        # 只在远端跟踪分支上前进，本地 review 停在 8-05——就是家里那份克隆的样子
        (Path(repo) / "a.txt").write_text("2", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "记录 staging 微信登录未配置第三次复发的排查与修复")
        (Path(repo) / "b.txt").write_text("3", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "同事改的，不该算我头上",
             author=COLLEAGUE, when="2026-08-06 18:00:00")
        ahead = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                               check=True, capture_output=True, text=True).stdout.strip()
        _git(repo, "update-ref", "refs/remotes/github/review", ahead)
        _git(repo, "reset", "-q", "--hard", "HEAD~2")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _subjects(self, **kw):
        return [c["subject"] for c in commits_in_range(self.repo, "2026-08-06", "2026-08-06", **kw)]

    def test_head_alone_cannot_see_it_which_is_how_08_06_lost_live_api(self):
        self.assertEqual([], self._subjects())

    def test_giving_an_author_reaches_the_remote_tracking_branch(self):
        self.assertEqual(["记录 staging 微信登录未配置第三次复发的排查与修复"],
                         self._subjects(author=ME))

    def test_a_colleague_on_that_same_branch_is_still_left_out(self):
        self.assertNotIn("同事改的，不该算我头上", self._subjects(author=ME))
        self.assertEqual(["同事改的，不该算我头上"], self._subjects(author=COLLEAGUE))


class PruneDateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".git" / "logs").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _touch(self, rel: str, ts: float):
        p = self.repo / ".git" / rel
        p.write_text("x", encoding="utf-8")
        os.utime(p, (ts, ts))

    def test_a_fetch_that_was_never_merged_still_counts_as_activity(self):
        # 1754409600 = 2026-08-06 00:00 本地时间当天；下面两个日期只需分属两天
        self._touch("logs/HEAD", 1754236800.0)   # 08-04
        self._touch("FETCH_HEAD", 1754409600.0)  # 08-06
        newer = _logs_head_mtime_date(str(self.repo))
        self._touch("FETCH_HEAD", 1754236800.0)
        self.assertGreater(newer, _logs_head_mtime_date(str(self.repo)))

    def test_a_repo_with_neither_file_reports_no_activity_rather_than_1970(self):
        self.assertEqual("", _logs_head_mtime_date(str(self.repo)))


if __name__ == "__main__":
    unittest.main()
