(function () {
  "use strict";
  const MCP_GATEWAY = "http://127.0.0.1:38777";
  const items = document.querySelectorAll(".nav-item");
  const pages = document.querySelectorAll(".page");
  const content = document.getElementById("content");
  const LAST_PAGE_KEY = "rxyy.lastPage";

  function activate(page) {
    if (!document.querySelector('.page[data-page="' + page + '"]')) page = "mcp";
    try { localStorage.setItem(LAST_PAGE_KEY, page); } catch (e) {}
    items.forEach((i) => i.classList.toggle("active", i.dataset.page === page));
    pages.forEach((p) => p.classList.toggle("active", p.dataset.page === page));
    if (content) content.classList.toggle("flush", page === "mcp");
    if (page === "mcp") mountMcp();
    if (page === "board" && window.BoardApp) window.BoardApp.enter();
    if (page === "projects") loadProjects();
    if (page === "settings") loadSettings();
  }

  function mountMcp() {
    const mount = document.getElementById("mcp-mount");
    if (!mount) return;
    mount.innerHTML = '<iframe src="' + MCP_GATEWAY + '/ui" style="width:100%;height:100%;border:0"></iframe>';
  }

  async function apiCall(name, args) {
    const api = window.pywebview && window.pywebview.api;
    if (!api || typeof api[name] !== "function") return null;
    return api[name].apply(api, args || []);
  }

  async function loadProjects() {
    const rows = await apiCall("proj_list");
    const body = document.getElementById("projBody");
    if (!body) return;
    if (!rows || !rows.length) {
      body.innerHTML = '<tr><td colspan="4" class="acc-empty">还没有项目</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (p) {
      return "<tr><td>" + escapeHtml(p.name) + "</td><td>" + escapeHtml(p.path) +
        "</td><td>" + escapeHtml(p.branch || "") + "</td><td>" +
        (p.enabled ? "是" : "否") + "</td></tr>";
    }).join("");
  }

  async function loadSettings() {
    const r = await apiCall("settings_get");
    if (!r) return;
    const set = function (id, v) { const el = document.getElementById(id); if (el) el.value = v || ""; };
    set("setKey", r.bailian_api_key);
    set("setBase", r.bailian_base_url || r.bailian_base_url_default);
    set("setModel", r.bailian_model || r.bailian_model_default);
    set("setDesk", r.desktop_root || r.desktop_root_default);
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"]/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c];
    });
  }

  items.forEach(function (btn) {
    btn.addEventListener("click", function () { activate(btn.dataset.page); });
  });

  const scan = document.getElementById("projScan");
  if (scan) scan.onclick = async function () {
    const r = await apiCall("proj_scan");
    const msg = document.getElementById("projMsg");
    if (msg) msg.textContent = r && r.ok ? ("扫描到 " + r.scanned + " 个") : ((r && r.error) || "失败");
    loadProjects();
  };
  const add = document.getElementById("projAdd");
  if (add) add.onclick = async function () {
    const path = window.prompt("仓库路径");
    if (!path) return;
    await apiCall("proj_add", [path]);
    loadProjects();
  };
  const reload = document.getElementById("projReload");
  if (reload) reload.onclick = loadProjects;

  const envRun = document.getElementById("envRun");
  if (envRun) envRun.onclick = async function () {
    const r = await apiCall("env_selfcheck");
    const out = document.getElementById("envOut");
    if (out) out.textContent = JSON.stringify(r, null, 2);
  };

  const save = document.getElementById("setSave");
  if (save) save.onclick = async function () {
    const val = function (id) { const el = document.getElementById(id); return el ? el.value : ""; };
    const r = await apiCall("settings_set", [{
      bailian_api_key: val("setKey"),
      bailian_base_url: val("setBase"),
      bailian_model: val("setModel"),
      desktop_root: val("setDesk"),
    }]);
    const msg = document.getElementById("setMsg");
    if (msg) msg.textContent = r && r.ok ? "已保存" : ((r && r.error) || "失败");
  };

  window.addEventListener("pywebviewready", function () {
    let page = "mcp";
    try { page = localStorage.getItem(LAST_PAGE_KEY) || "mcp"; } catch (e) {}
    activate(page);
  });
  activate("mcp");
})();
