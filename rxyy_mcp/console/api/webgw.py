# -*- coding: utf-8 -*-
"""rxyy tools 远程网关：把整个桌面控制台搬进浏览器（局域网 / Tailscale / 公网）。

沿用 rxyy-mcp/gateway.py 已在 38777 上跑了很久的那套做法：读 index.html 原文动态
注入一段 shim，把 `window.pywebview.api.<m>(...)` 映射成 `fetch POST /api/<m>`。
前端一个字都不用改，web/ 后续更新自动跟随，也不必给 190 个方法逐个写路由。

与 gateway.py 的三处关键不同：
- 它只听 127.0.0.1，本网关要对外，所以有令牌鉴权、同源约束，且**不发 CORS 头**
  （这套 API 能开 Pro、能读账号库、能起本机进程，跨站可读一次就够呛）。
- 它只发单个 ui.html，本网关还要发 web/ 下的静态资源。
- 「持久 MCP」页在 app.js 里写死了 http://127.0.0.1:38777，远程浏览器解析不到，
  故把它反代到同源 /mcpgw/ 并在 shim 里改写请求地址（不改 app.js）。
"""
from __future__ import annotations

import gzip
import hmac
import json
import re
import secrets
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 39090
MCP_GATEWAY = "http://127.0.0.1:38777"
MCP_PREFIX = "/mcpgw"


def mcp_proxy_url(path: str, query: str = "") -> str:
    """把 /mcpgw/img?p=... 还原成 38777 上的地址。查询串必须带上——
    08-15 漏了 query，/img 没有 p= 一律 404，控制台用户发的图全看不见。"""
    tail = path[len(MCP_PREFIX):] or "/"
    url = MCP_GATEWAY + tail
    if query:
        url += "?" + query
    return url
# 公网这一跳挂在已有的 example.invalid 上按路径分流（cloudflared ingress 的
# path 规则），不必再申请域名、也不用 Cloudflare 凭据。同一个服务因此要同时接住
# 两种地址：局域网/Tailscale 直连是根路径，隧道进来的带这个前缀。
DEFAULT_PATH_PREFIX = "/rxyy"
COOKIE_NAME = "rxyy_console"
COOKIE_MAX_AGE = 30 * 24 * 3600
MAX_BODY = 64 * 1024 * 1024
GZIP_MIN = 1400
# private 而不是 public：这些文件本身不算机密，但标了 public，Cloudflare 边缘
# 就会替我们缓存并发给没有令牌的人。max-age 定 5 分钟 + ETag：常用期间零请求，
# 过期后六份并发问一轮 304（几百字节），而队友正在改 UI 时刷新也能立刻看到。
_STATIC_CACHE = "private, max-age=300"
# 带 ?v=<指纹> 进来的就是 index 刚发下去的那一版，内容变了指纹也变，
# 不必再回源问。公网那一跳一个来回要 0.9 秒（客户端落阿姆斯特丹、隧道连接器
# 在洛杉矶，一次点击跨两趟洲际），省掉的那一轮 304 就是实打实的开屏时间。
_STATIC_IMMUTABLE = "private, max-age=31536000, immutable"

# 静态资源白名单：web/ 里就这几类，给死名单省得哪天多出个 .db 被顺出去
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

DENY_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>rxyy tools</title></head>
<body style="background:#f4f5f9;color:#6b7280;font:14px 'Microsoft YaHei UI',sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center"><div style="font-size:17px;color:#1f2430">链接无效或已失效</div>
<div style="margin-top:8px">请在控制台重新取一条带令牌的远程链接</div></div></body></html>"""

# 注入 index.html 的 shim。原生 pywebview 桥在这里不存在，全部换成 http 调用。
# __BASE__ 在发页面时替换成本次请求的路径前缀（根路径进来就是空串）。
_SHIM_TMPL = """
<script>
(function () {
  var BASE = '__BASE__';
  var TOKEN = __TOKEN__;
  var MCP_LOCAL = '__MCP_LOCAL__', MCP_PROXY = BASE + '__MCP_PREFIX__';

  function authHeaders(extra) {
    var headers = { 'X-Requested-With': 'rxyy' };
    if (extra && typeof extra.forEach === 'function') {
      extra.forEach(function (v, k) { headers[k] = v; });
    } else if (extra) {
      Object.assign(headers, extra);
    }
    if (TOKEN) headers['X-Console-Token'] = TOKEN;
    return headers;
  }

  // 桌面壳里这些调用是进程内直连，慢也不会断；换成 http 后必须给个上限，
  // 否则后端卡住时页面上的按钮就是永远转圈。日报生成、开 Pro 这类实测都在
  // 一分钟内，给到 3 分钟足够宽。刷 Token 自己就要等验证码 180 秒，再加开窗
  // 和 Turnstile，3 分钟会被 AbortController 掐死（前端只看到 AbortError）。
  var SLOW = { acc_refresh_token_email: 300000, acc_reftoken_start: 600000 };
  function call(method, args) {
    var ctl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var ms = SLOW[method] || 180000;
    var timer = ctl ? setTimeout(function () { ctl.abort(); }, ms) : null;
    return fetch(BASE + '/api/' + method, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      credentials: 'same-origin',
      body: JSON.stringify(args || []),
      signal: ctl ? ctl.signal : undefined
    }).then(function (r) {
      if (timer) clearTimeout(timer);
      if (r.status === 403) { location.href = BASE + '/__denied'; throw new Error('远程会话已失效'); }
      return r.json().then(function (d) {
        // 桥接层的异常要变成 rejected promise，跟原生 pywebview 行为一致，
        // 否则前端 catch 不到，错误会被当成正常返回值渲染出去
        if (!r.ok) throw new Error((d && d.__error) || ('HTTP ' + r.status));
        return d;
      });
    }, function (e) { if (timer) clearTimeout(timer); throw e; });
  }

  window.pywebview = window.pywebview || {};
  window.pywebview.api = new Proxy({}, {
    get: function (_, m) {
      if (typeof m !== 'string') return undefined;
      return function () { return call(m, Array.prototype.slice.call(arguments)); };
    },
    has: function () { return true; }
  });
  window.__rxyyWebgw = true;

  // 「持久 MCP」页写死了本机网关地址，远程浏览器解析不到 127.0.0.1。
  // 在这儿把请求和 iframe 都改道同源反代，app.js 不用动。
  var origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      init = init || {};
      init.headers = authHeaders(init.headers || {});
      init.credentials = init.credentials || 'same-origin';
      if (typeof input === 'string' && input.indexOf(MCP_LOCAL) === 0) {
        input = MCP_PROXY + input.slice(MCP_LOCAL.length);
      }
    } catch (e) { /* 改写失败就按原样发，最多是那一页不可用 */ }
    return origFetch.call(this, input, init);
  };

  function fixFrames() {
    var list = document.querySelectorAll('iframe[src]');
    for (var i = 0; i < list.length; i++) {
      var s = list[i].getAttribute('src') || '';
      if (s.indexOf(MCP_LOCAL) === 0) {
        list[i].setAttribute('src', MCP_PROXY + s.slice(MCP_LOCAL.length));
      }
    }
  }
  try {
    new MutationObserver(fixFrames).observe(document.documentElement, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['src']
    });
  } catch (e) { /* 老浏览器没有就算了 */ }

  // 窄屏把侧栏做成抽屉。按钮和遮罩都在这儿建，remote.css 负责宽屏时藏起来，
  // 这样 index.html / app.js 一个字都不用改。
  function buildDrawer() {
    if (document.querySelector('.remote-navbtn')) return;
    var btn = document.createElement('button');
    btn.className = 'remote-navbtn';
    btn.setAttribute('aria-label', '菜单');
    btn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></svg>';
    var mask = document.createElement('div');
    mask.className = 'remote-backdrop';
    function close() { document.body.classList.remove('remote-nav-open'); }
    btn.onclick = function () { document.body.classList.toggle('remote-nav-open'); };
    mask.onclick = close;
    // 点了导航就该看内容了，抽屉自己让开
    var side = document.getElementById('sidebar');
    if (side) side.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('.nav-item')) close();
    });
    document.body.appendChild(mask);
    document.body.appendChild(btn);
  }

  // Codex 父页 CSP 的 'self' 是 app://，子 iframe 里 <script src>/<link href>
  // 会被当成跨源直接挡掉（fetch 走 connect-src，反而能拿到正文）。embed
  // 模式用带令牌的 fetch 把 CSS/JS 变成内联，再补发 pywebviewready。
  function injectStyle(css) {
    if (!css) return;
    var el = document.createElement('style');
    el.setAttribute('data-rxyy-embed-boot', '1');
    el.textContent = css;
    (document.head || document.documentElement).appendChild(el);
  }
  if (document.documentElement && document.documentElement.classList.contains('rxyy-embed')) {
    injectStyle('html.rxyy-embed #sidebar,html.rxyy-embed #sideGrip,html.rxyy-embed .remote-navbtn,html.rxyy-embed .remote-backdrop{display:none!important}html.rxyy-embed #app{display:block}html.rxyy-embed #content{padding:10px 12px 12px!important;height:100vh;width:100%}html.rxyy-embed .page[data-page="board"] .page-head{display:none}html.rxyy-embed #boardRoot{height:calc(100vh - 20px)}');
  }
  function bootEmbedAssets(done) {
    if (!document.documentElement.classList.contains('rxyy-embed')
        || window.BoardApp || window.__rxyyEmbedBooted) {
      done();
      return;
    }
    window.__rxyyEmbedBooted = true;
    var links = document.querySelectorAll('link[rel="stylesheet"][href]');
    var scripts = document.querySelectorAll('script[src]');
    var cssJobs = [];
    for (var i = 0; i < links.length; i++) {
      cssJobs.push(origFetch.call(window, links[i].href, {
        credentials: 'same-origin',
        headers: authHeaders()
      }).then(function (r) { return r.ok ? r.text() : ''; }).then(injectStyle).catch(function () {}));
    }
    function runScripts(idx) {
      if (idx >= scripts.length) { done(); return; }
      origFetch.call(window, scripts[idx].src, {
        credentials: 'same-origin',
        headers: authHeaders()
      }).then(function (r) {
        if (!r.ok) throw new Error('asset ' + r.status);
        return r.text();
      }).then(function (code) {
        var el = document.createElement('script');
        el.textContent = code;
        (document.body || document.documentElement).appendChild(el);
        runScripts(idx + 1);
      }).catch(function () { runScripts(idx + 1); });
    }
    var go = function () { runScripts(0); };
    if (typeof Promise !== 'undefined' && cssJobs.length) {
      Promise.all(cssJobs).then(go).catch(go);
    } else {
      go();
    }
  }

  // 原生 pywebview 在桥就绪时派发 pywebviewready，这里在 DOM 就绪后补发
  function fireReady() {
    buildDrawer();
    fixFrames();
    bootEmbedAssets(function () {
      window.dispatchEvent(new Event('pywebviewready'));
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', fireReady);
  } else {
    setTimeout(fireReady, 0);
  }
})();
</script>
""".replace("__MCP_LOCAL__", MCP_GATEWAY).replace("__MCP_PREFIX__", MCP_PREFIX)

# 注入被反代的 rxyy-mcp ui.html：它按同源绝对路径请求 /api/、/img，
# 而那个根现在是本网关，必须补上 /mcpgw 前缀才落回 38777。
_MCP_SHIM_TMPL = """
<script>
(function () {
  var P = '__BASE__' + '__MCP_PREFIX__';
  window.__RXYY_MCP_IMG_PREFIX__ = P;
  function fix(u) {
    return (typeof u === 'string' && u.charAt(0) === '/' && u.indexOf(P + '/') !== 0) ? P + u : u;
  }
  var origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      if (typeof input === 'string' && input !== fix(input)) {
        input = fix(input);
        // 改道后这些就成了本网关的同源写请求，得过 CSRF 闸门
        init = init || {};
        init.headers = Object.assign({}, init.headers || {}, { 'X-Requested-With': 'rxyy' });
        init.credentials = 'same-origin';
      }
    } catch (e) {}
    return origFetch.call(this, input, init);
  };
  function fixImgs() {
    var list = document.querySelectorAll('img[src^="/"]');
    for (var i = 0; i < list.length; i++) {
      var s = list[i].getAttribute('src') || '';
      if (s.indexOf(P + '/') !== 0) list[i].setAttribute('src', P + s);
    }
  }
  try {
    new MutationObserver(fixImgs).observe(document.documentElement, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['src']
    });
  } catch (e) {}
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', fixImgs);
  } else {
    setTimeout(fixImgs, 0);
  }
})();
</script>
""".replace("__MCP_PREFIX__", MCP_PREFIX)


# index.html 里对本机 css/js 的引用。排除掉带 : 的绝对地址和已经带 ? 的，
# 只认真正躺在 web/ 下的那几份。
_ASSET_REF = re.compile(
    r"""(<(?:link|script)\b[^>]*?\b(?:href|src)=(["']))([^"'?:#]+\.(?:css|js))\2""",
    re.I)


def _asset_version(path: Path) -> str:
    """内容指纹：mtime+size。改一个字它就变，浏览器立刻取新的。"""
    st = path.stat()
    return "%x-%x" % (st.st_mtime_ns, st.st_size)


def _stamp_assets(html: str, web_root: Path, token: str = "") -> str:
    """给静态引用打指纹，换来「一年不回源」的缓存资格。

    没有指纹就只能给短 max-age——否则改完 UI 的人要等缓存过期才看得到。
    有了指纹两头都要：命中期间零请求，文件一改地址就变。
    embed 模式还会带上 t=：iframe 挂在 app:// 里时 SameSite=Strict cookie
    存不住，静态件也得靠 URL 令牌过闸。
    """
    def sub(m):
        name = m.group(3)
        try:
            ver = _asset_version(web_root / name)
        except OSError:
            return m.group(0)   # 引用了不存在的文件，原样留着，让 404 照常暴露
        extra = ("&t=" + token) if token else ""
        return "%s%s?v=%s%s%s" % (m.group(1), name, ver, extra, m.group(2))

    return _ASSET_REF.sub(sub, html)


def _shim(base: str, token: str = "") -> str:
    return _SHIM_TMPL.replace("__BASE__", base).replace(
        "__TOKEN__", json.dumps(token))


def _mcp_shim(base: str) -> str:
    return _MCP_SHIM_TMPL.replace("__BASE__", base)


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """与 rxyy-mcp/ipc.py 同款加固：Windows 上 SO_REUSEADDR 允许第二个进程绑同一
    端口并劫持流量，改独占绑定让第二个绑定者直接 OSError；backlog 调大扛重连。"""

    allow_reuse_address = False
    request_queue_size = 64
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def ensure_token(kv) -> str:
    """远程令牌：首次启动自动生成 32 位 hex 存进 kv_config。

    刻意不复用 rxyy-mcp 的 share_token——那个只有 12 位、且已经散在同事手里的
    投递链接上；这套 API 能开 Pro、能翻账号库，得有自己的、换了不影响别人的令牌。
    """
    tok = (kv.kv_get("console_remote_token") or "").strip()
    if not tok:
        tok = secrets.token_hex(16)
        kv.kv_set("console_remote_token", tok)
    return tok


def local_bases(port: int) -> list[tuple[str, str]]:
    """列出本机可用的访问基址，开机时打进日志，方便直接复制链接。"""
    lan, tailscale = "", ""
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = res[4][0]
            if ip.startswith("100.") and not tailscale:
                tailscale = ip          # Tailscale 的 CGNAT 段 100.64.0.0/10
            elif ip.startswith(("192.168.", "10.")) and not lan:
                lan = ip
    except OSError:
        pass
    out = []
    if lan:
        out.append(("局域网", "http://%s:%d" % (lan, port)))
    if tailscale:
        out.append(("Tailscale", "http://%s:%d" % (tailscale, port)))
    return out


def _split_targets(raw):
    """推送地址拆多通道，与 rxyy-mcp/hub.py 同一套约定：分号/换行分隔，逐个都发。"""
    import re
    out = []
    for part in re.split(r"[;\n；]+", str(raw or "")):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


def _is_bark(target: str) -> bool:
    """day.app 或裸 KEY 才是 Bark，其余 http(s) 一律按 ntfy 发。

    照抄 hub 的反向判据：自建 ntfy 域名随便起（判「含 ntfy 字样」曾把自建 ntfy
    误当 Bark，按 /标题/正文 拼路径全部 404）。
    """
    if "day.app" in target:
        return True
    return "://" not in target and "ntfy" not in target


def rxyy_mcp_push_config(project_root) -> dict:
    """读 rxyy-mcp 的推送配置（复用它那套已经配好的 Bark/ntfy 地址，不另开一份）。

    config.json 是机器态，build.ps1 故意不把它打进包，所以**不能照着代码目录找**：
    打包版那样找必然读不到，而外层那个推送线程把异常整个吞了，表现就是「不报错、
    也不推」。规则与 console/app.py 的 _ensure_rxyy_mcp_plus 保持一致。
    """
    import sys

    rxyy_mcp_dir = Path(project_root) / "rxyy-mcp"
    if str(rxyy_mcp_dir) not in sys.path:
        sys.path.insert(0, str(rxyy_mcp_dir))
    try:
        from datadir import CONFIG_PATH as path
    except Exception:  # noqa: BLE001  没有 datadir 就是老布局，照代码目录找
        path = rxyy_mcp_dir / "config.json"
    return json.loads(Path(path).read_text(encoding="utf-8"))


def push_links(bark_url: str, links, timeout=8) -> list:
    """把远程入口推到手机并留在通知历史里。

    通知被清掉、令牌换掉之后，这几条链接就只存在于聊天记录里了——所以开机自动
    补一条，Bark 那边带 isArchive 永久保存，锁屏上随时找得到。静音发（Bark
    level=passive / ntfy priority=1），它只是个入口，不该半夜响。
    """
    import urllib.parse
    import urllib.request

    body = "\n".join("%s：%s" % (label, url) for label, url in links)
    sent = []
    for target in _split_targets(bark_url):
        try:
            if _is_bark(target):
                base = (target if target.startswith("http")
                        else "https://api.day.app/" + target).rstrip("/")
                # 一条链接一条通知：Bark 的历史里就是三个能直接点开的入口，
                # 比挤在一条正文里再去长按复制顺手。
                for label, url in links:
                    qs = urllib.parse.urlencode({
                        "group": "rxyy tools", "isArchive": "1", "level": "passive",
                        "url": url,
                    })
                    # safe="" 是关键：默认的 safe="/" 会把正文里 http:// 的斜杠
                    # 原样留下，Bark 按 /标题/正文 拆路径就全乱了（实测发不出去）
                    urllib.request.urlopen("%s/%s/%s?%s" % (
                        base,
                        urllib.parse.quote("rxyy tools · " + label, safe=""),
                        urllib.parse.quote(url, safe=""), qs), timeout=timeout).read()
            else:
                base = (target if target.startswith("http")
                        else "https://ntfy.sh/" + target).rstrip("/")
                root, _, topic = base.rpartition("/")
                payload = {
                    "topic": topic, "title": "rxyy tools 远程入口", "message": body,
                    "priority": 1, "tags": ["link"],
                    "actions": [{"action": "view", "label": label, "url": url,
                                 "clear": False} for label, url in links[:3]],
                }
                req = urllib.request.Request(
                    root, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=timeout).read()
            sent.append(target)
        except Exception:  # noqa: BLE001  推不出去只是少个书签，不该拖累网关启动
            continue
    return sent


def _make_handler(api, web_dir: Path, token: str, path_prefix: str = ""):
    web_root = web_dir.resolve()
    prefix = ("/" + path_prefix.strip("/")) if path_prefix.strip("/") else ""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "rxyy-webgw"

        def log_message(self, *args):
            pass

        def _route(self, path: str) -> tuple[str, str]:
            """拆出 (前缀, 去掉前缀的路径)。

            同一个服务要接住两种地址：局域网/Tailscale 直连打的是根路径，公网经
            cloudflared 按路径分流进来的带 /rxyy。返回的前缀还要回填进 shim 与
            <base>，页面里的相对资源和接口调用才不会掉到 39080 那边去。
            """
            if prefix and (path == prefix or path.startswith(prefix + "/")):
                return prefix, path[len(prefix):] or "/"
            return "", path

        # ---------- 鉴权 ----------
        def _cookie_token(self) -> str:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE_NAME:
                    return v
            return ""

        def _authed(self, query: str = "") -> bool:
            got = ((parse_qs(query).get("t") or [""])[0]
                   or self.headers.get("X-Console-Token", "")
                   or self._cookie_token())
            return bool(got) and hmac.compare_digest(str(got), token)

        def _same_site(self) -> bool:
            """写操作的 CSRF 闸门。

            Cookie 已是 SameSite=Strict，跨站请求本来就带不上；这里再要一个自定义
            头兜底——跨站表单能提交但设不了自定义头，而 fetch 想设它就得先过预检，
            本服务不发 CORS 头，预检必挂。
            """
            return self.headers.get("X-Requested-With") == "rxyy"

        # ---------- 响应 ----------
        def _send(self, code, body, ctype="application/json; charset=utf-8",
                  cache=None, extra=()):
            data = body if isinstance(body, (bytes, bytearray)) else json.dumps(
                body, ensure_ascii=False, default=str).encode("utf-8")
            gz = (len(data) >= GZIP_MIN and not ctype.startswith(("image/", "font/"))
                  and "gzip" in (self.headers.get("Accept-Encoding") or "").lower())
            if gz:
                try:
                    data = gzip.compress(data, 6)
                except Exception:  # noqa: BLE001
                    gz = False
            # 304 按定义就没有响应体，也不该带 Content-Length——带了会让
            # 部分客户端在长连接上多等一个不会到来的 body
            bodyless = code == 304
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if gz and not bodyless:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            if not bodyless:
                self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache or "no-store")
            # 令牌是从 URL 进来的，别让它跟着 Referer 漏给外站
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and not bodyless:
                try:
                    self.wfile.write(data)
                except Exception:  # noqa: BLE001  对端提前断开是常态
                    pass

        def _deny(self, html=True):
            if html:
                self._send(403, DENY_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send(403, {"__error": "远程会话已失效，请重新用带令牌的链接进入"})

        def _send_index(self, base: str, embed_token: str = ""):
            index = web_root / "index.html"
            try:
                html = index.read_text(encoding="utf-8")
            except OSError as exc:
                self._send(500, {"__error": "index.html 读取失败: %r" % exc})
                return
            html = _stamp_assets(html, web_root, token=embed_token)
            try:
                remote_css = "remote.css?v=" + _asset_version(web_root / "remote.css")
            except OSError:
                remote_css = "remote.css"
            if embed_token:
                remote_css += "&t=" + embed_token
                if "rxyy-embed" not in html.split(">", 1)[0]:
                    html = html.replace("<html", '<html class="rxyy-embed"', 1)
                # 外链 JS 被挡时至少先露出看板页，bootEmbedAssets 再把卡片画上
                html = html.replace(
                    '<section class="page active" data-page="accounts">',
                    '<section class="page" data-page="accounts">', 1)
                html = html.replace(
                    '<section class="page" data-page="board">',
                    '<section class="page active" data-page="board">', 1)
            head = '<link rel="stylesheet" href="%s">' % remote_css + _shim(
                base, embed_token)
            if base:
                # styles.css / app.js 这些都是相对引用，带前缀访问时得先把根挪过去
                head = '<base href="%s/">' % base + head
            if "</head>" in html:
                html = html.replace("</head>", head + "</head>", 1)
            else:
                html = head + html
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        def _send_static(self, path: str, query: str = ""):
            try:
                target = (web_root / path.lstrip("/")).resolve()
            except OSError:
                self._send(404, {"__error": "not found"})
                return
            ctype = _STATIC_TYPES.get(target.suffix.lower())
            # resolve 之后必须仍在 web/ 里：挡 ../ 穿越和符号链接绕行
            if not ctype or web_root not in target.parents or not target.is_file():
                self._send(404, {"__error": "not found"})
                return
            try:
                st = target.stat()
                # 内容指纹取 mtime+size 就够，且不必为了发一个头去读整个文件；
                # 前端六份静态件合起来 320KB，公网那一跳每个来回约 1.7 秒
                # （连接器全在洛杉矶，中国客户端来回要跨两次太平洋），
                # 能靠 304 省下的就是实打实的开屏时间。
                etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
                # index 发下去的引用带着当时的指纹。指纹对得上就说明这份内容
                # 永远不会变（变了 index 会给出新地址），可以彻底不回源。
                fresh = (parse_qs(query).get("v") or [""])[0] == etag.strip('"')
                cache = _STATIC_IMMUTABLE if fresh else _STATIC_CACHE
                if self.headers.get("If-None-Match") == etag:
                    self._send(304, b"", ctype, cache=cache, extra=(("ETag", etag),))
                    return
                self._send(200, target.read_bytes(), ctype, cache=cache,
                           extra=(("ETag", etag),))
            except OSError:
                self._send(404, {"__error": "not found"})

        # ---------- 持久 MCP 反代 ----------
        def _proxy_mcp(self, path: str, body: bytes | None, base: str = "",
                       query: str = ""):
            url = mcp_proxy_url(path, query)
            req = urllib.request.Request(
                url, data=body,
                method="POST" if body is not None else "GET",
                headers={"Content-Type": "application/json"} if body is not None else {})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    ctype = resp.headers.get("Content-Type", "application/json")
            except urllib.error.HTTPError as exc:
                self._send(exc.code, {"__error": "rxyy-mcp 网关返回 %d" % exc.code})
                return
            except Exception as exc:  # noqa: BLE001
                self._send(502, {"__error": "rxyy-mcp 网关不可达: %r" % exc})
                return
            if ctype.startswith("text/html"):
                text = raw.decode("utf-8", "replace")
                shim = _mcp_shim(base)
                if "</head>" in text:
                    text = text.replace("</head>", shim + "</head>", 1)
                else:
                    text = shim + text
                raw = text.encode("utf-8")
            self._send(200, raw, ctype)

        # ---------- 路由 ----------
        def do_GET(self):
            parsed = urlparse(self.path)
            base, path = self._route(parsed.path)
            query = parsed.query
            if path == "/__denied":
                self._send(200, DENY_PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if not self._authed(query):
                self._deny(html=True)
                return
            qs = parse_qs(query)
            token_q = (qs.get("t") or [""])[0]
            embed = (qs.get("embed") or [""])[0].lower() in ("1", "true", "yes")
            # 带 ?t= 进来的：把令牌换成 cookie 再跳干净地址，免得它留在
            # 浏览器历史、书签和后续每一条 Referer 里。
            # embed=1 是给 Codex app:// iframe 用的：父页跨站，SameSite=Strict
            # cookie 存不住，302 掉令牌后下一跳就是粉底 403。留下 t=，shim
            # 用 X-Console-Token，静态件 URL 也带令牌。
            if token_q and not embed:
                self.send_response(302)
                self.send_header("Location", base + (path if path != "/" else "/"))
                self.send_header(
                    "Set-Cookie",
                    "%s=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Strict"
                    % (COOKIE_NAME, token, COOKIE_MAX_AGE))
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if path.startswith(MCP_PREFIX):
                self._proxy_mcp(path, None, base, query)
            elif path in ("/", "/index.html"):
                self._send_index(base, embed_token=token_q if embed else "")
            elif path.rstrip("/") == "/jenkins-go":
                dest = (qs.get("dest") or [""])[0]
                try:
                    from api import jenkins_api as jk
                    jbase, juser, jpw, _host = jk._cfg()
                    if not jpw:
                        self._send(400, (
                            "<!doctype html><meta charset=utf-8><p>还没配 Jenkins 密码</p>"
                        ).encode("utf-8"), "text/html; charset=utf-8")
                        return
                    html = jk.login_form_html(dest=dest, base=jbase, user=juser, password=jpw)
                    self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                except Exception as exc:  # noqa: BLE001
                    self._send(500, ("<!doctype html><meta charset=utf-8><p>%s</p>"
                                     % exc).encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send_static(path, query)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            base, path = self._route(urlparse(self.path).path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                # 收不下就别再谈复用这条连接：剩下的字节没人读，
                # 下一个请求会从半截 body 开始解析
                self.close_connection = True
                self._send(413, {"__error": "请求体过大"})
                return
            # 无论后面拒不拒，body 都必须先读干净。HTTP/1.1 长连接下留在缓冲区里的
            # 半截 body 会被当成下一个请求的请求行 —— 表现就是 403 与 501 交替出现，
            # 而 501 那半边根本不是我们发的（08-07 远程页面上实测到这一串）。
            raw = self.rfile.read(length) if length > 0 else b"[]"
            if not self._authed(urlparse(self.path).query):
                self._deny(html=False)
                return
            if not self._same_site():
                self._send(403, {"__error": "缺少同源标记"})
                return
            if path.startswith(MCP_PREFIX):
                self._proxy_mcp(path, raw, base)
                return
            if not path.startswith("/api/"):
                self._send(404, {"__error": "not found"})
                return
            method = path[len("/api/"):]
            # 私有属性一律不暴露：_window 之类拿到就能越过整个 API 面
            if not method or method.startswith("_"):
                self._send(403, {"__error": "forbidden"})
                return
            try:
                args = json.loads(raw.decode("utf-8") or "[]")
            except Exception:  # noqa: BLE001
                args = []
            if not isinstance(args, list):
                args = [args]
            fn = getattr(api, method, None)
            if not callable(fn):
                self._send(404, {"__error": "没有这个方法: " + method})
                return
            try:
                result = fn(*args)
            except Exception as exc:  # noqa: BLE001  后端异常要原样回给前端弹 toast
                self._send(500, {"__error": repr(exc)})
                return
            self._send(200, result)

    return Handler


def start_web_gateway(api, web_dir, token, host="0.0.0.0", port=DEFAULT_PORT,
                      path_prefix=DEFAULT_PATH_PREFIX):
    """后台线程起远程网关，返回 httpd；端口被占用等失败情况由调用方兜底。"""
    handler = _make_handler(api, Path(web_dir), str(token), path_prefix)
    httpd = _ExclusiveHTTPServer((host, int(port)), handler)
    threading.Thread(target=httpd.serve_forever, name="rxyy-webgw", daemon=True).start()
    return httpd
