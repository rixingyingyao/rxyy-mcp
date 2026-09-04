"""git 相关工具：扫描桌面 git 仓库 + 按日期抓提交记录（供日报生产）。"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime

_SKIP = {"node_modules", ".git", "venv", ".venv", "__pycache__", "dist", "build",
         ".next", ".nuxt", "target", ".idea", ".vscode", "vendor", "env"}

# git 命令统一参数：不弹认证、UTF-8、静默
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# 「一笔提交都没有」和「git 没跑成」长得一模一样，都是空输出——而这两件事的后果
# 天差地别。08-28 实测：打包和体检同时在跑，git log 大面积撞上 20 秒超时，81 个仓里
# 45 个被判成「近 90 天没有任何提交」，其中包含当天刚提过 3 笔的 cursor工作流、
# playthread-go、smart_radio_api……体检据此建议停用 54 个仓（正常是 36 个）。
# 幸亏是预演。所以失败一律显式说出来，绝不拿空输出冒充成功。
_NO_COMMITS_YET = "does not have any commits yet"


def _ceiling_for(path: str) -> str:
    """把 git 的仓库发现钉在 path 自己身上，不许往上走。

    `git -C <目录>` 找不到 .git 会一路往上翻父目录。家目录本身是个 git 仓库
    （dotfiles）时，任何非仓库目录都会翻到它、以 0 退出、回一份**别人的**提交历史：
    非仓库被当成「这段时间没提交」，而扫 D:\\桌面\\working\\* 时若父目录成了仓库，
    子目录还会互相顶包。GIT_CEILING_DIRECTORIES 里列出的目录自身不参与搜索，
    所以填父目录 = 只认 path 这一层。
    """
    full = os.path.abspath(path)
    parent = os.path.dirname(full)
    # 盘符根：dirname 等于自身，再设天花板会把 path 自己也排除掉
    return "" if parent == full else parent


def _run_git(path: str, args: list[str], timeout: int = 15) -> tuple[bool, str]:
    """(跑成了吗, 标准输出)。跑不成返回 (False, "")，调用方必须区别对待。"""
    try:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
        ceiling = _ceiling_for(path)
        if ceiling:
            env["GIT_CEILING_DIRECTORIES"] = ceiling
        out = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, creationflags=_CREATE_NO_WINDOW,
        )
        if out.returncode == 0:
            return True, out.stdout
        # 刚 git init、一次都没提交过的仓：git log 以 128 退出，但它确实是「没有提交」
        if _NO_COMMITS_YET in (out.stderr or ""):
            return True, ""
        return False, ""
    except Exception:
        return False, ""


def _is_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def _head_branch(path: str) -> str:
    """读 .git/HEAD 取分支名（纯文件 IO，不 spawn git）。"""
    try:
        with open(os.path.join(path, ".git", "HEAD"), encoding="utf-8", errors="ignore") as f:
            ref = f.read().strip()
        if ref.startswith("ref:"):
            return ref.split("/")[-1]
        return ref[:8]  # detached HEAD
    except Exception:
        return ""


def _last_commit_at(path: str) -> str:
    """从 .git/logs/HEAD 最后一行解析最近一次操作时间（纯文件 IO）。

    行格式：<old> <new> <name> <email> <unixts> <tz>\\t<msg>
    """
    logs = os.path.join(path, ".git", "logs", "HEAD")
    try:
        with open(logs, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, 4096)
            f.seek(size - back)
            tail = f.read().decode("utf-8", "ignore").strip().splitlines()
        if not tail:
            return ""
        head_part = tail[-1].split("\t")[0]
        toks = head_part.split()
        # 时间戳是倒数第二个（tz 是最后一个）
        if len(toks) >= 2 and toks[-2].isdigit():
            return datetime.fromtimestamp(int(toks[-2])).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return ""


def repo_git_meta(path: str) -> dict:
    """分支名 + 最近一次 HEAD 操作时间（纯文件 IO，给项目管理刷新用）。"""
    return {"branch": _head_branch(path), "last_commit_at": _last_commit_at(path)}


def _logs_head_mtime_date(path: str) -> str:
    """仓库最后活动日期（YYYY-MM-DD），用于快速判断某天是否有活动。

    取 logs/HEAD 与 FETCH_HEAD 里较新的那个：只 fetch 不合并时 logs/HEAD 不动，
    单看它会把整个仓库剪掉。8-06 的 live_api 就是这样丢的——公司机那两笔当天
    提交已经在 remotes/github/review 上，家里的 review 分支还停在 8-05，
    logs/HEAD 也停在 8-05，日报连探都没探它。
    """
    newest = 0.0
    for rel in (os.path.join(".git", "logs", "HEAD"),
                os.path.join(".git", "FETCH_HEAD"),
                os.path.join(".git", "HEAD")):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(path, rel)))
        except OSError:
            continue
    return datetime.fromtimestamp(newest).strftime("%Y-%m-%d") if newest else ""


def scan_repos(root: str) -> list[dict]:
    """扫描 root 下的 git 仓库：顶层仓库 + 每个非仓库文件夹的一级子目录里的仓库。"""
    repos: list[dict] = []
    seen: set[str] = set()

    def add(p: str):
        p = os.path.normpath(p)
        if p in seen:
            return
        seen.add(p)
        repos.append({
            "path": p,
            "name": os.path.basename(p),
            "branch": _head_branch(p),
            "last_commit_at": _last_commit_at(p),
        })

    try:
        entries = list(os.scandir(root))
    except Exception:
        return repos

    for e in entries:
        if not e.is_dir() or e.name in _SKIP:
            continue
        if _is_repo(e.path):
            add(e.path)
            continue
        try:
            for c in os.scandir(e.path):
                if c.is_dir() and c.name not in _SKIP and _is_repo(c.path):
                    add(c.path)
        except Exception:
            continue
    return repos


# ---------- 「我」是谁 ----------
#
# 日报只该统计本人的活。这道闸以前靠设置页手填一个全局 `report_git_author`，而
# 08-28 实测那个 kv 从加上那天起就是空的 —— 于是**谁的提交都算**：当天 22 笔里
# firecrawl 上游占 8 笔（第三方开源仓，克隆下来看看的），启用列表里同类的还有
# qwen、playthread-go。08-05 那次更糟，同事提的 README／歌曲同步／蒙版关键帧
# 被原样写进了交去 OA 的日报。
#
# 改成从 git config 自己读身份，不用人配。读法是纯文件解析而不是 `git config
# --get`：81 个启用仓各 spawn 一次实测 22.6 秒，而日报每抓一次都要问一遍。

_USER_SECTION = re.compile(r"^\s*\[\s*user\s*(?:\"[^\"]*\")?\s*\]\s*$", re.IGNORECASE)
_SECTION = re.compile(r"^\s*\[")
_KV = re.compile(r"^\s*(name|email)\s*=\s*(.*?)\s*$", re.IGNORECASE)


def _parse_config_user(text: str) -> tuple[str, str]:
    """从 git config 文本里取 [user] 段的 name / email。"""
    name = email = ""
    in_user = False
    for line in text.splitlines():
        if _SECTION.match(line):
            in_user = bool(_USER_SECTION.match(line))
            continue
        if not in_user:
            continue
        m = _KV.match(line.split("#")[0].split(";")[0])
        if not m:
            continue
        val = m.group(2).strip().strip('"')
        if m.group(1).lower() == "name":
            name = name or val
        else:
            email = email or val
    return name, email


def _read_config_user(cfg_path: str) -> tuple[str, str]:
    try:
        with open(cfg_path, encoding="utf-8", errors="ignore") as f:
            return _parse_config_user(f.read())
    except OSError:
        return "", ""


def _git_dir(path: str) -> str:
    """仓库的 .git 目录；worktree／submodule 里 .git 是一行 `gitdir: ...` 的文件。"""
    p = os.path.join(path, ".git")
    if os.path.isdir(p):
        return p
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            head = f.read(4096).strip()
    except OSError:
        return ""
    if head.startswith("gitdir:"):
        target = head.split(":", 1)[1].strip()
        return target if os.path.isabs(target) else os.path.normpath(os.path.join(path, target))
    return ""


_global_user: tuple[str, str] | None = None


def global_identity() -> tuple[str, str]:
    """~/.gitconfig 里的 (name, email)，整台机器算一次。"""
    global _global_user
    if _global_user is not None:
        return _global_user
    # GIT_CONFIG_GLOBAL 是「取代」而不是「加在前面」：git 认它之后就不再读
    # ~/.gitconfig 了（指到 /dev/null 就是彻底关掉全局配置）
    if os.environ.get("GIT_CONFIG_GLOBAL"):
        cands = [os.environ["GIT_CONFIG_GLOBAL"]]
    else:
        home = os.path.expanduser("~")
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        cands = [os.path.join(home, ".gitconfig"), os.path.join(xdg, "git", "config")]
    name = email = ""
    for c in cands:
        n, e = _read_config_user(c)
        name, email = name or n, email or e
        if name and email:
            break
    _global_user = (name, email)
    return _global_user


_identity_cache: dict[str, tuple[str, str]] = {}


def repo_identity_pair(path: str) -> tuple[str, str]:
    """这个仓里「我」叫什么：(name, email)。仓库级 config 优先，缺项落到全局。

    仓库级恰恰是「我在这个仓里叫什么」的权威答案 —— 本机 81 个启用仓里 5 个有
    自己的署名（rxyy@local、q@haiio.xyz…），其余 76 个走全局。
    """
    p = os.path.normpath(path)
    hit = _identity_cache.get(p)
    if hit is not None:
        return hit
    name = email = ""
    gd = _git_dir(p)
    if gd:
        name, email = _read_config_user(os.path.join(gd, "config"))
    gname, gemail = global_identity()
    pair = (name or gname, email or gemail)
    _identity_cache[p] = pair
    return pair


def repo_identity(path: str) -> str:
    """这个仓里「我」的标识：优先 email，没有就 name（给 --author 用）。"""
    name, email = repo_identity_pair(path)
    return email or name


def identity_set(paths) -> tuple[set[str], set[str]]:
    """这台机器上「我」用过的全部 git 身份：(邮箱小写集合, 姓名集合)。

    按仓一个个比对而不是取一个身份走天下，是因为同一个人在不同仓署名本就不同；
    但**判断某笔提交是不是我的时候要拿整个集合去比**，不能只比这个仓当前配的那
    一个 —— 否则「我上周用另一个邮箱在这个仓提的活」会被自己的过滤器悄悄丢掉，
    那比多算几笔别人的还糟（日报少报了活，人不会发现）。
    """
    emails: set[str] = set()
    names: set[str] = set()
    gname, gemail = global_identity()
    for n, e in [(gname, gemail)] + [repo_identity_pair(p) for p in paths]:
        if e:
            emails.add(e.lower())
        elif n:
            names.add(n)
    return emails, names


def commit_is_mine(commit: dict, emails: set[str], names: set[str],
                   override: str = "") -> bool:
    """这笔提交算不算「我的」。override 走 git 的老口径：在 `姓名 <邮箱>` 里子串匹配。"""
    email = str(commit.get("email") or "")
    name = str(commit.get("author") or "")
    if override:
        return override.lower() in ("%s <%s>" % (name, email)).lower()
    return (email.lower() in emails) or (bool(name) and name in names)


def clear_identity_cache() -> None:
    global _global_user
    _global_user = None
    _identity_cache.clear()


def commits_on_date(path: str, date: str, author: str = "") -> list[dict] | None:
    """某一天（本地时间）该仓库的提交，返回 [{hash,time,author,email,subject}]。

    和 commits_in_range 一样：git 没跑成返回 None，不是 []。
    """
    return commits_in_range(path, date, date, author=author)


def commits_in_range(path: str, date_from: str, date_to: str, author: str = "",
                     all_branches: bool | None = None,
                     with_stats: bool = False,
                     timeout: int = 20) -> list[dict] | None:
    """[date_from, date_to]（含两端，本地时间）范围内该仓库的提交。

    扫全部分支（--branches --remotes）而不是只走 HEAD：这台机器上的分支常常落后于
    已经 fetch 下来的远端，只走 HEAD 会把自己的活漏掉（8-06 的 live_api 就是这么
    丢的）。默认在「有过滤条件」时才开——不过滤还扫全分支等于把同事每条远端分支
    都算进来；调用方自己在 Python 侧按作者分拣时，显式传 all_branches=True。

    每笔带上 author/email：谁提的要能当场看见。以前只把过滤条件交给 git，被滤掉
    的那些就彻底消失了，日报页上看不出「firecrawl 那 8 笔是上游的、已排除」，
    也就分不清「今天没活」和「今天的活被过滤器吃了」。

    with_stats 才加 --shortstat。它要为每笔提交算一遍 diff：90 天窗口在 firecrawl
    这种大仓上能把一次体检拖到分钟级，而改动行数**全仓库没有一处在用**——日报素材
    还专门不喂它（模型爱把数字照抄进正文，违反「不堆细节数字」）。

    **git 没跑成返回 None，不是 []**：超时、仓库损坏、被锁都会让输出变空，而空列表
    的意思是「这段时间真没提交」。混为一谈的后果见 _run_git 上面那段。
    """
    since = f"{date_from} 00:00:00"
    until = f"{date_to} 23:59:59"
    args = ["log", "--no-merges", f"--since={since}", f"--until={until}",
            "--date=format:%Y-%m-%d %H:%M",
            "--pretty=format:%x01%h%x02%ad%x02%ae%x02%an%x02%s"]
    if with_stats:
        args.append("--shortstat")
    if all_branches is None:
        all_branches = bool(author)
    if author:
        # -F：把 --author 当字面量，别让邮箱/姓名里的字符被当成正则
        args[1:1] = ["-F", f"--author={author}"]
    if all_branches:
        args[1:1] = ["--branches", "--remotes"]
    ok, raw = _run_git(path, args, timeout=timeout)
    if not ok:
        return None
    if not raw:
        return []
    commits: list[dict] = []
    cur: dict | None = None
    for line in raw.splitlines():
        if line.startswith("\x01"):
            if cur:
                commits.append(cur)
            parts = line[1:].split("\x02")
            while len(parts) < 5:
                parts.append("")
            cur = {"hash": parts[0], "time": parts[1],
                   "email": parts[2], "author": parts[3],
                   # 主题里真混进 \x02 时不能截断，剩下的全拼回去
                   "subject": "\x02".join(parts[4:])}
            if with_stats:
                cur.update(files=0, insertions=0, deletions=0)
        elif cur and with_stats and ("changed" in line):
            # " 3 files changed, 40 insertions(+), 5 deletions(-)"
            m = re.search(r"(\d+) files? changed", line)
            if m:
                cur["files"] = int(m.group(1))
            m = re.search(r"(\d+) insertions?", line)
            if m:
                cur["insertions"] = int(m.group(1))
            m = re.search(r"(\d+) deletions?", line)
            if m:
                cur["deletions"] = int(m.group(1))
    if cur:
        commits.append(cur)
    return commits


def validate_repo(path: str) -> dict:
    """手动添加时校验路径是否是 git 仓库。"""
    path = os.path.normpath(path)
    if not os.path.isdir(path):
        return {"ok": False, "error": "目录不存在"}
    if not _is_repo(path):
        return {"ok": False, "error": "该目录不是 git 仓库（缺少 .git）"}
    return {"ok": True, "name": os.path.basename(path),
            "branch": _head_branch(path), "last_commit_at": _last_commit_at(path)}
