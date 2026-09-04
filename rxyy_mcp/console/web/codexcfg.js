// Codex 配置中心：主题 / 宠物 / 灵动岛 / 看板，四摊配置收进一页。
// 后端 console/api/codexcfg_api.py。跟 jira.js、messenger.js 一样用全局点击代理自挂，
// 不改 app.js（那份文件常年有别人在动）。
(function () {
  "use strict";

  const api = () => (window.pywebview && window.pywebview.api) || null;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const attr = (s) => esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  let mounted = false;
  let busy = false;
  let state = null;
  let editor = null;

  const COLOR_FIELDS = [
    ["background", "整体背景"], ["panel", "面板"], ["panelAlt", "次级面板"],
    ["accent", "强调色"], ["accentAlt", "次强调色"], ["secondary", "辅助色"],
    ["highlight", "高亮色"], ["text", "正文"], ["muted", "弱化文字"],
    ["line", "分隔线 / 描边"],
  ];
  const TEXT_FIELDS = [
    ["brandSubtitle", "品牌副标题"], ["tagline", "标语"],
    ["projectPrefix", "项目名前缀"], ["projectLabel", "项目选择提示"],
    ["statusText", "状态文字"], ["quote", "引语"],
    ["promoTitle", "推广标题"], ["promoSub", "推广副标题"],
    ["promoUrl", "推广链接"],
  ];
  const PART_LABELS = {
    root: "整体根区域", sidebar: "左侧栏", main: "主内容区", header: "顶部栏",
    home: "首页", "home-hero": "首页主卡片", "project-list": "项目列表",
    thread: "任务对话区", message: "消息", composer: "输入框",
    "composer-toolbar": "输入框工具栏",
    "home-utility": "首页项目帽", "composer-shell": "输入框外壳",
    dialog: "弹窗",
  };
  const STATE_LABELS = { "": "默认", hover: "鼠标悬停", "focus-visible": "键盘焦点" };
  const PROPERTY_LABELS = {
    "background-color": "背景色", color: "文字色", "border-color": "四边描边色",
    "border-width": "四边描边宽度", "border-style": "四边描边样式",
    "border-top-color": "上描边色", "border-right-color": "右描边色",
    "border-bottom-color": "下描边色", "border-left-color": "左描边色",
    "border-top-width": "上描边宽度", "border-right-width": "右描边宽度",
    "border-bottom-width": "下描边宽度", "border-left-width": "左描边宽度",
    "border-top-style": "上描边样式", "border-right-style": "右描边样式",
    "border-bottom-style": "下描边样式", "border-left-style": "左描边样式",
    "border-radius": "圆角", "box-shadow": "阴影", opacity: "不透明度",
    "border-top-left-radius": "左上圆角", "border-top-right-radius": "右上圆角",
    "border-bottom-right-radius": "右下圆角", "border-bottom-left-radius": "左下圆角",
    "backdrop-filter": "背景模糊/饱和度", gap: "间距", "row-gap": "行间距",
    "column-gap": "列间距", "font-family": "字体族", "font-size": "字号",
    "font-weight": "字重", "line-height": "行高", "letter-spacing": "字距",
    "transition-duration": "过渡时长", "transition-property": "过渡属性",
  };

  function fmtTime(seconds) {
    if (!seconds) return "—";
    const d = new Date(Number(seconds) * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return (d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
      pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function shell() {
    return "" +
      '<div class="acc-toolbar acc-toolbar-wrap">' +
      '  <span id="cfgState" class="jira-state">读取中…</span>' +
      '  <div class="acc-toolbar-actions">' +
      '    <button class="btn btn-ghost" id="cfgReload">刷新</button>' +
      "  </div>" +
      "</div>" +
      '<div id="cfgMsg" class="acc-msg"></div>' +
      '<div id="cfgBody" class="cfg-grid"></div>';
  }

  function msg(text, ok) {
    const m = $("cfgMsg");
    if (!m) return;
    if (!text) { m.className = "acc-msg"; m.textContent = ""; return; }
    m.className = "acc-msg " + (ok ? "ok" : "err");
    m.textContent = text;
  }

  function card(title, sub, body, foot) {
    return '<section class="cfg-card">' +
      '<div class="cfg-card-head"><b>' + esc(title) + "</b>" +
      (sub ? '<span class="cfg-sub">' + esc(sub) + "</span>" : "") + "</div>" +
      '<div class="cfg-card-body">' + body + "</div>" +
      (foot ? '<div class="cfg-card-foot">' + foot + "</div>" : "") +
      "</section>";
  }

  function normalizeEditor(payload) {
    const defaults = payload.defaults || {};
    const theme = Object.assign({}, payload.theme || {});
    theme.schemaVersion = 1;
    theme.appearance = theme.appearance || defaults.appearance || "auto";
    theme.art = Object.assign({ focusX: 0.5, focusY: 0.5, safeArea: "none", taskMode: "ambient" },
      defaults.art || {}, theme.art || {});
    theme.colors = Object.assign({}, defaults.colors || {}, theme.colors || {});
    COLOR_FIELDS.forEach(([key]) => { if (!theme.colors[key]) theme.colors[key] = "#808080"; });
    TEXT_FIELDS.forEach(([key]) => { if (theme[key] == null) theme[key] = ""; });
    payload.theme = theme;
    payload.rules = Array.isArray(payload.rules) ? payload.rules : [];
    payload.pendingBackground = "";
    payload.dirty = false;
    return payload;
  }

  function option(value, label, selected) {
    return '<option value="' + attr(value) + '"' + (value === selected ? " selected" : "") + '>' +
      esc(label) + "</option>";
  }

  function field(path, label, value, type, extra) {
    return '<label class="cfg-field"><span>' + esc(label) + '</span><input type="' +
      (type || "text") + '" data-theme-field="' + attr(path) + '" value="' + attr(value) + '" ' +
      (extra || "") + '></label>';
  }

  function selectField(path, label, selected, choices) {
    return '<label class="cfg-field"><span>' + esc(label) + '</span><select data-theme-field="' +
      attr(path) + '">' + choices.map(([value, text]) => option(value, text, selected)).join("") +
      "</select></label>";
  }

  function pickerColor(value) {
    const v = String(value || "");
    if (/^#[0-9a-f]{6}$/i.test(v)) return v;
    if (/^#[0-9a-f]{8}$/i.test(v)) return v.slice(0, 7);
    if (/^#[0-9a-f]{3}$/i.test(v)) return "#" + v.slice(1).split("").map((x) => x + x).join("");
    return "#808080";
  }

  function colorField(key, label, value) {
    return '<label class="cfg-color-field"><span>' + esc(label) + '</span><div>' +
      '<input type="color" data-theme-color-picker="' + attr(key) + '" value="' +
      attr(pickerColor(value)) + '"><input type="text" data-theme-field="colors.' + attr(key) +
      '" value="' + attr(value) + '" spellcheck="false"></div></label>';
  }

  function styleRule(part, stateName, create) {
    let rule = editor.rules.find((item) => item.part === part && (item.state || "") === stateName);
    if (!rule && create) {
      rule = { part, state: stateName, declarations: [] };
      editor.rules.push(rule);
    }
    return rule || null;
  }

  function defaultStyleValue(property) {
    if (property.includes("color")) {
      if (property === "background-color") return "var(--ds-theme-color-panel)";
      if (property === "color") return "var(--ds-theme-color-text)";
      return "var(--ds-theme-color-line)";
    }
    if (property.includes("width")) return "1px";
    if (property.includes("style")) return "solid";
    if (property.includes("radius")) return property === "border-radius"
      ? "var(--ds-theme-surface-radius)" : "12px";
    if (property === "box-shadow") return "none";
    if (property === "opacity") return "1";
    if (property === "backdrop-filter") return "blur(var(--ds-theme-surface-blur))";
    if (property === "font-family") return "system-ui";
    if (property === "font-size") return "14px";
    if (property === "font-weight") return "500";
    if (property === "line-height") return "1.4";
    if (property === "letter-spacing") return "0";
    if (property.includes("gap")) return "8px";
    if (property === "transition-duration") return "180ms";
    if (property === "transition-property") return "background-color";
    return "initial";
  }

  function styleState(part, stateName, properties) {
    const rule = styleRule(part, stateName, false);
    const declarations = (rule && rule.declarations) || [];
    const used = new Set(declarations.map((item) => item.property));
    const available = properties.filter((property) => !used.has(property));
    const rows = declarations.length ? declarations.map((item) =>
      '<div class="cfg-style-row"><span title="' + attr(item.property) + '">' +
        esc(PROPERTY_LABELS[item.property] || item.property) + '</span><input type="text" ' +
        'data-style-value data-part="' + attr(part) + '" data-state="' + attr(stateName) +
        '" data-property="' + attr(item.property) + '" value="' + attr(item.value) +
        '" spellcheck="false"><button class="op-btn cfg-remove" data-style-remove ' +
        'data-part="' + attr(part) + '" data-state="' + attr(stateName) + '" data-property="' +
        attr(item.property) + '">×</button></div>').join("")
      : '<div class="cfg-style-empty">这个状态暂未覆盖 Codex 默认样式</div>';
    const add = available.length
      ? '<div class="cfg-style-add"><select>' + available.map((property) =>
          option(property, PROPERTY_LABELS[property] || property, "")).join("") +
        '</select><button class="op-btn" data-style-add data-part="' + attr(part) +
        '" data-state="' + attr(stateName) + '">添加属性</button></div>'
      : '<span class="cfg-style-empty">这个状态已用了全部白名单属性</span>';
    return '<div class="cfg-style-state"><b>' + esc(STATE_LABELS[stateName] || stateName) +
      '</b>' + rows + add + "</div>";
  }

  function stylePart(part, properties, states) {
    const count = editor.rules.filter((item) => item.part === part).reduce(
      (sum, item) => sum + (item.declarations || []).length, 0);
    return '<details class="cfg-style-part" data-style-part="' + attr(part) + '"><summary><b>' +
      esc(PART_LABELS[part] || part) + '</b><span>' + esc(part) + ' · ' + count +
      ' 项</span></summary><div class="cfg-style-part-body">' +
      ["", ...states].map((stateName) => styleState(part, stateName, properties)).join("") +
      "</div></details>";
  }

  function themeEditorCard() {
    const e = editor;
    const theme = e.theme;
    const art = theme.art || {};
    const policy = e.policy || { parts: [], states: [], properties: [], variables: [] };
    const background = e.background || {};
    const preview = background.preview
      ? '<img id="cfgThemeEditorPreview" alt="背景预览" src="' + attr(background.preview) + '">'
      : '<div class="cfg-empty">背景图无法生成预览</div>';
    const basics = field("name", "主题名称", theme.name || "", "text", 'maxlength="80"') +
      '<label class="cfg-field"><span>主题 ID</span><input value="' + attr(theme.id || "") +
      '" readonly></label>' +
      selectField("appearance", "明暗模式", theme.appearance, [
        ["auto", "跟随 Codex"], ["light", "浅色"], ["dark", "深色"],
      ]) +
      field("art.focusX", "画面焦点 X", art.focusX, "number", 'min="0" max="1" step="0.01"') +
      field("art.focusY", "画面焦点 Y", art.focusY, "number", 'min="0" max="1" step="0.01"') +
      selectField("art.safeArea", "安全留白", art.safeArea, [
        ["none", "无"], ["left", "左侧留白"], ["right", "右侧留白"],
      ]) +
      selectField("art.taskMode", "任务页背景", art.taskMode, [
        ["ambient", "柔和显示"], ["full", "完整显示"], ["off", "任务页关闭"],
      ]);
    const colors = COLOR_FIELDS.map(([key, label]) =>
      colorField(key, label, theme.colors[key] || "")).join("");
    const texts = TEXT_FIELDS.map(([key, label]) =>
      field(key, label, theme[key] || "", key === "promoUrl" ? "url" : "text",
        'maxlength="' + (key === "promoUrl" ? "512" : "120") + '"')).join("");
    const parts = (policy.parts || []).map((part) =>
      stylePart(part, policy.properties || [], policy.states || [])).join("");
    return '<section class="cfg-card cfg-theme-editor" id="cfgThemeEditor">' +
      '<div class="cfg-card-head"><b>主题调节 · ' + esc(theme.name || e.id) +
      '</b><span class="cfg-sub">Dream Skin 全部安全可调项</span></div>' +
      '<div class="cfg-card-body cfg-editor-body">' +
      '<div class="cfg-editor-actions"><span id="cfgEditorDirty" class="cfg-sub">' +
        (e.dirty ? "有未保存修改" : "已读取主题源文件") + '</span><div>' +
        '<button class="btn btn-ghost" id="cfgEditorUndo"' +
          ((e.history && e.history.count) ? "" : " disabled") + '>恢复上一版</button>' +
        '<button class="btn btn-ghost" id="cfgEditorClose">关闭</button>' +
        '<button class="btn btn-ghost" id="cfgEditorSave">只保存</button>' +
        '<button class="btn btn-forge" id="cfgEditorApply">保存并应用</button>' +
      '</div></div>' +
      '<details class="cfg-editor-section" open><summary>背景与画面</summary>' +
        '<div class="cfg-background-editor"><div class="cfg-background-preview">' + preview +
        '</div><div><div class="cfg-kv"><span>主题图片</span><b id="cfgThemeImageName">' +
        esc(background.name || "—") + '</b></div><div class="cfg-kv"><span>待替换</span><b ' +
        'id="cfgThemeImagePending">' + esc(e.pendingBackground || "未选择") + '</b></div>' +
        '<button class="op-btn" id="cfgPickThemeBackground">更换背景图…</button></div></div>' +
        '<div class="cfg-form-grid">' + basics + '</div></details>' +
      '<details class="cfg-editor-section" open><summary>官方 10 色</summary>' +
        '<div class="cfg-color-grid">' + colors + '</div></details>' +
      '<details class="cfg-editor-section"><summary>装饰文案（可留空）</summary>' +
        '<p class="cfg-help">Dream Skin 引擎支持这些文字；当前雪白主题把部分装饰隐藏了，' +
        '保留字段是为了换主题或以后重新显示时可直接使用。</p><div class="cfg-form-grid">' +
        texts + '</div></details>' +
      '<details class="cfg-editor-section" open><summary>' +
        (policy.parts || []).length + ' 个界面部件样式</summary>' +
        '<p class="cfg-help">每个部件都能分别设置默认、悬停和键盘焦点状态。属性和值会在保存前交给 ' +
        'Dream Skin 官方 Safe CSS 校验器；不允许任意选择器、图片 URL、脚本或 @import。</p>' +
        '<div class="cfg-style-parts">' + parts + '</div></details>' +
      '<details class="cfg-editor-section"><summary>白名单能力清单</summary>' +
        '<p class="cfg-help">部件：' + esc((policy.parts || []).join("、")) + '</p>' +
        '<p class="cfg-help">状态：默认、' + esc((policy.states || []).join("、")) + '</p>' +
        '<p class="cfg-help">属性：' + esc((policy.properties || []).join("、")) + '</p>' +
        '<p class="cfg-help">可引用变量：' + esc((policy.variables || []).join("、")) + '</p>' +
      '</details></div>' +
      '<div class="cfg-card-foot">保存前自动留一份源主题快照；“保存并应用”仍调用主题目录脚本和 ' +
      'Dream Skin 官方启动/画面验证链路。</div></section>';
  }

  function serializeRules() {
    return editor.rules.filter((rule) => (rule.declarations || []).length).map((rule) => {
      const selector = '[data-ds-part="' + rule.part + '"]' +
        (rule.state ? ":" + rule.state : "");
      const body = rule.declarations.map((item) =>
        "  " + item.property + ": " + item.value + ";").join("\n");
      return selector + " {\n" + body + "\n}";
    }).join("\n\n") + "\n";
  }

  function compatLine(c) {
    if (!c || c.skipped) return "";
    const ok = !!c.ok;
    const label = ok
      ? (c.auto_repaired ? "升级后已自动重打" : "26.803 兼容已打")
      : "缺失（升级后可能回粉）";
    return '<div class="cfg-kv"><span>皮肤锚点</span><b class="' +
      (ok ? "cfg-ok" : "cfg-warn") + '">' +
      label +
      ' <button class="op-btn" id="cfgSkinCompat">重打补丁</button></b></div>';
  }

  function themeCard(t) {
    const active = t.active || {};
    const runtime = active.runtime || {};
    const runtimeActive = !!runtime.active;
    const lib = t.library || [];
    const rows = lib.length
      ? lib.map((it) => {
        const selected = active.name && it.name === active.name;
        const live = selected && runtimeActive;
        return '<div class="cfg-row' + (live ? " is-on" : "") + '" data-row="' + esc(it.id) + '">' +
          '<span class="cfg-row-name">' + esc(it.name) +
            (live ? " · 当前" : (selected ? " · 已选" : "")) + "</span>" +
          '<span class="cfg-row-path" title="' + esc(it.dir) + '">' + esc(it.dir) + "</span>" +
          (it.preview ? '<button class="op-btn" data-peek="' + esc(it.id) + '">预览</button>' : "") +
          '<button class="op-btn" data-edit-theme="' + attr(it.id) + '">调节</button>' +
          (live ? '<span class="cfg-tag">生效中</span>'
            : '<button class="op-btn op-switch2" data-theme="' + esc(it.id) + '">' +
              (selected ? "启动并应用" : "应用") + "</button>") +
          "</div>";
      }).join("")
      : '<div class="cfg-empty">没扫到可一键应用的主题目录（要同时有 theme.json 和 apply-art-theme.ps1）</div>';
    const engineNext = runtimeActive
      ? "应用会调用 Dream Skin 官方脚本；必要时关闭并重新打开 Codex，验证画面真的生效后才报成功。"
      : "「已选 / 上次应用」只表示主题文件已经写上，不等于皮肤正在 Codex 画面上。"
        + "引擎没跑时换肤看不见。点「启动并应用」会按官方脚本拉起注入器，必要时关掉再打开 Codex。";
    return card("主题 · Dream Skin",
      active.engine_version ? "引擎 v" + active.engine_version : "",
      '<div class="cfg-kv"><span>' + (runtimeActive ? "当前主题" : "已选主题") + '</span><b>' +
        esc(active.name || "（读不到）") + "</b></div>" +
      '<div class="cfg-kv"><span>主题引擎</span><b class="' +
        (runtimeActive ? "cfg-ok" : "cfg-warn") + '">' +
        (runtimeActive ? "运行中（端口 " + esc(runtime.port || "—") + "）" :
          "未运行 · " + esc(runtime.reason || "未通过运行态验证")) + "</b></div>" +
      (runtimeActive ? "" :
        '<div class="cfg-kv"><span>下一步</span><b class="cfg-warn">点该主题的「启动并应用」拉起引擎；不要只看「上次应用」时间</b></div>') +
      '<div class="cfg-kv"><span>上次应用</span><b>' + esc(fmtTime(active.applied_at)) + "</b></div>" +
      compatLine(t.compat) +
      rows,
      engineNext);
  }

  function petCard(p) {
    const list = p.list || [];
    const rows = list.length
      ? list.map((it) => {
        const on = it.id === p.island_pet;
        return '<div class="cfg-row' + (on ? " is-on" : "") + '">' +
          '<span class="cfg-row-name">' + esc(it.name) + "</span>" +
          '<span class="cfg-row-path">' + esc(it.author ? "@" + it.author : "") + "</span>" +
          (on ? '<span class="cfg-tag">灵动岛在用</span>'
            : '<button class="op-btn" data-pet="' + esc(it.id) + '">用它</button>') +
          "</div>";
      }).join("")
      : '<div class="cfg-empty">~/.codex/pets 下没有宠物</div>';
    return card("宠物", list.length + " 个已装", rows, esc(p.note || ""));
  }

  function islandCard(i) {
    const auto = i.autostart;
    const autoText = auto === "on" ? "已开" : (auto === "off" ? "已关" : "没装快捷方式");
    const btn = auto === "missing" ? ""
      : '<button class="op-btn" data-auto="' + (auto === "on" ? "0" : "1") + '">' +
        (auto === "on" ? "关掉自启" : "打开自启") + "</button>";
    return card("灵动岛 · Codex Island",
      i.running ? "运行中" : "没在跑",
      '<div class="cfg-kv"><span>进程</span><b class="' + (i.running ? "cfg-ok" : "cfg-warn") + '">' +
        (i.running ? "运行中" : "没在跑") + "</b></div>" +
      '<div class="cfg-kv"><span>开机自启</span><b>' + autoText + " " + btn + "</b></div>" +
      '<div class="cfg-kv"><span>屏幕位置</span><b>' +
        (i.config && i.config.left != null
          ? "左 " + Math.round(i.config.left) + " · 上 " + Math.round(i.config.top)
          : "—") + "</b></div>" +
      '<div class="cfg-kv"><span>最近日志</span><b>' + esc(fmtTime(i.log_at)) + "</b></div>",
      "位置是拖出来的，记在它自己的配置里；这里只读不改，免得跟正在跑的它抢。");
  }

  function boardCard(b) {
    const side = b.sidebar || {};
    const sideText = side.msg || "还没探测。要 Dream Skin 把 Codex 调试口打开。";
    return card("看板", "2026-08-13 起换成原生任务面板",
      '<div class="cfg-kv"><span>任务面板</span><b class="cfg-ok">已内置（左侧栏点开整页）</b></div>' +
      '<div class="cfg-kv"><span>旧 dashi 看板</span><b class="' +
        (b.dashi_running ? "cfg-warn" : "") + '">' +
        (b.dashi_running ? "还在跑（47823）" : "已停") + "</b></div>" +
      '<div class="cfg-kv"><span>它的开机自启</span><b>' +
        (b.dashi_autostart === "on" ? "还开着" :
          (b.dashi_autostart === "off" ? "已关（快捷方式改名保留）" : "没装")) + "</b></div>" +
      '<div class="cfg-kv"><span>Codex 侧栏</span><b class="' +
        (side.ok ? "cfg-ok" : "") + '">' + esc(sideText) + "</b></div>",
      '<button class="btn" id="cfgSideProbe" type="button">探测侧栏</button> ' +
      '<button class="btn btn-forge" id="cfgSideInject" type="button">注入整页看板</button> ' +
      '<button class="btn" id="cfgSideRemove" type="button">卸下</button>' +
      '<div class="cfg-note">走 Dream Skin 已开的 CDP，把本机任务面板嵌进 Codex 窗口，不打开外置浏览器。</div>');
  }

  function backupCard(b) {
    const items = (b && b.items) || [];
    const rows = items.length
      ? items.map((it) =>
        '<div class="cfg-row">' +
          '<span class="cfg-row-name">' + esc(it.name.replace("codex-dress-", "")) + "</span>" +
          '<span class="cfg-row-path">' + (it.size / 1048576).toFixed(1) + " MB</span>" +
          '<button class="op-btn" data-restore="' + esc(it.path) + '">恢复</button>' +
        "</div>").join("")
      : '<div class="cfg-empty">还没有备份</div>';
    return card("备份 / 恢复", items.length + " 份",
      '<div class="cfg-kv"><span>包含</span><b>当前生效的主题文件 + 灵动岛配置</b></div>' + rows,
      '<button class="btn btn-forge" id="cfgBackup">立即备份</button>' +
      "　恢复时主题按名字回到主题库那套重新应用（不硬塞文件，免得跟插件版本对不上）。");
  }

  function render() {
    const box = $("cfgBody");
    if (!box || !state) return;
    box.innerHTML = themeCard(state.theme || {}) + (editor ? themeEditorCard() : "") +
      petCard(state.pets || {}) +
      islandCard(state.island || {}) + boardCard(state.board || {}) +
      backupCard(state.backups || {});
    box.querySelectorAll("[data-theme]").forEach((el) => {
      el.onclick = () => applyTheme(el.dataset.theme, el.closest(".cfg-row"));
    });
    box.querySelectorAll("[data-peek]").forEach((el) => {
      el.onclick = () => peekTheme(el.dataset.peek, el);
    });
    box.querySelectorAll("[data-edit-theme]").forEach((el) => {
      el.onclick = () => openThemeEditor(el.dataset.editTheme);
    });
    box.querySelectorAll("[data-restore]").forEach((el) => {
      el.onclick = () => restore(el.dataset.restore);
    });
    const backup = $("cfgBackup");
    if (backup) backup.onclick = doBackup;
    const compatBtn = $("cfgSkinCompat");
    if (compatBtn) compatBtn.onclick = repairSkinCompat;
    box.querySelectorAll("[data-pet]").forEach((el) => {
      el.onclick = () => setPet(el.dataset.pet);
    });
    box.querySelectorAll("[data-auto]").forEach((el) => {
      el.onclick = () => setAutostart(el.dataset.auto === "1");
    });
    const probe = $("cfgSideProbe");
    if (probe) probe.onclick = () => sidebarAct("codexcfg_sidebar_probe");
    const inject = $("cfgSideInject");
    if (inject) inject.onclick = () => sidebarAct("codexcfg_sidebar_inject");
    const remove = $("cfgSideRemove");
    if (remove) remove.onclick = () => sidebarAct("codexcfg_sidebar_remove");
    if (editor) bindThemeEditor();
  }

  function markEditorDirty() {
    if (!editor) return;
    editor.dirty = true;
    const label = $("cfgEditorDirty");
    if (label) label.textContent = "有未保存修改";
  }

  function setThemeField(path, value) {
    const bits = path.split(".");
    let target = editor.theme;
    for (let index = 0; index < bits.length - 1; index += 1) {
      if (!target[bits[index]] || typeof target[bits[index]] !== "object") target[bits[index]] = {};
      target = target[bits[index]];
    }
    if (path === "art.focusX" || path === "art.focusY") {
      target[bits[bits.length - 1]] = Number(value);
    } else {
      target[bits[bits.length - 1]] = value;
    }
    markEditorDirty();
  }

  function rerenderEditor(openPart) {
    render();
    if (openPart) {
      const details = document.querySelector('.cfg-style-part[data-style-part="' + openPart + '"]');
      if (details) details.open = true;
    }
  }

  function bindThemeEditor() {
    const host = $("cfgThemeEditor");
    if (!host) return;
    host.querySelectorAll("[data-theme-field]").forEach((input) => {
      const update = () => setThemeField(input.dataset.themeField, input.value);
      input.addEventListener(input.tagName === "SELECT" ? "change" : "input", update);
    });
    host.querySelectorAll("[data-theme-color-picker]").forEach((picker) => {
      picker.addEventListener("input", () => {
        const key = picker.dataset.themeColorPicker;
        editor.theme.colors[key] = picker.value;
        const textInput = Array.from(host.querySelectorAll("[data-theme-field]")).find(
          (item) => item.dataset.themeField === "colors." + key);
        if (textInput) textInput.value = picker.value;
        markEditorDirty();
      });
    });
    host.querySelectorAll("[data-style-value]").forEach((input) => {
      input.addEventListener("input", () => {
        const rule = styleRule(input.dataset.part, input.dataset.state || "", false);
        const declaration = rule && rule.declarations.find(
          (item) => item.property === input.dataset.property);
        if (declaration) declaration.value = input.value;
        markEditorDirty();
      });
    });
    host.querySelectorAll("[data-style-add]").forEach((button) => {
      button.onclick = () => {
        const select = button.parentElement.querySelector("select");
        if (!select || !select.value) return;
        const rule = styleRule(button.dataset.part, button.dataset.state || "", true);
        if (!rule.declarations.some((item) => item.property === select.value)) {
          rule.declarations.push({ property: select.value, value: defaultStyleValue(select.value) });
          markEditorDirty();
          rerenderEditor(button.dataset.part);
        }
      };
    });
    host.querySelectorAll("[data-style-remove]").forEach((button) => {
      button.onclick = () => {
        const stateName = button.dataset.state || "";
        const rule = styleRule(button.dataset.part, stateName, false);
        if (!rule) return;
        rule.declarations = rule.declarations.filter(
          (item) => item.property !== button.dataset.property);
        if (!rule.declarations.length) {
          editor.rules = editor.rules.filter((item) => item !== rule);
        }
        markEditorDirty();
        rerenderEditor(button.dataset.part);
      };
    });
    const picker = $("cfgPickThemeBackground");
    if (picker) picker.onclick = pickThemeBackground;
    const close = $("cfgEditorClose");
    if (close) close.onclick = () => {
      if (editor.dirty && !window.confirm("放弃尚未保存的主题修改？")) return;
      editor = null;
      render();
    };
    const save = $("cfgEditorSave");
    if (save) save.onclick = () => saveThemeEditor(false);
    const apply = $("cfgEditorApply");
    if (apply) apply.onclick = () => saveThemeEditor(true);
    const undo = $("cfgEditorUndo");
    if (undo) undo.onclick = () => restoreThemeEditor(false);
  }

  async function openThemeEditor(id) {
    if (busy || !id) return;
    if (editor && editor.dirty && editor.id !== id &&
        !window.confirm("放弃当前主题尚未保存的修改，改调另一套主题？")) return;
    busy = true;
    msg("正在读取主题的全部可调项…", true);
    let r;
    try { r = await api().codexcfg_theme_editor(id); }
    catch (e) { r = { ok: false, error: String(e) }; }
    busy = false;
    if (!r || !r.ok) { msg((r && r.error) || "主题编辑器读取失败", false); return; }
    editor = normalizeEditor(r);
    render();
    msg("已读取「" + (editor.theme.name || id) + "」的全部可调项", true);
    setTimeout(() => {
      const host = $("cfgThemeEditor");
      if (host) host.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  }

  async function pickThemeBackground() {
    if (busy || !editor) return;
    busy = true;
    let r;
    try { r = await api().codexcfg_pick_theme_background(editor.id); }
    catch (e) { r = { ok: false, error: String(e) }; }
    busy = false;
    if (!r || !r.ok) { msg((r && r.error) || "背景图选择失败", false); return; }
    if (r.cancelled) return;
    editor.pendingBackground = r.path || "";
    if (r.preview) editor.background.preview = r.preview;
    markEditorDirty();
    const pending = $("cfgThemeImagePending");
    if (pending) pending.textContent = r.name || r.path || "已选择";
    const preview = $("cfgThemeEditorPreview");
    if (preview && r.preview) preview.src = r.preview;
  }

  async function saveThemeEditor(applyNow) {
    if (busy || !editor) return;
    if (applyNow && !window.confirm(
      "保存主题并立即应用到 Codex？\n\nCodex 可能会关闭并重新打开；未发送的输入请先保存。")) return;
    busy = true;
    msg(applyNow ? "正在保存、应用并验证主题；Codex 可能会重启…" :
      "正在校验并保存主题…", true);
    let r;
    try {
      r = await api().codexcfg_save_theme(
        editor.id, editor.theme, serializeRules(), editor.revision,
        editor.pendingBackground || "", !!applyNow);
    } catch (e) { r = { ok: false, error: String(e) }; }
    busy = false;
    if (!r || !r.ok) {
      if (r && r.saved) {
        try {
          const fresh = await api().codexcfg_theme_editor(editor.id);
          if (fresh && fresh.ok) editor = normalizeEditor(fresh);
        } catch (e) { /* 保存已经完成，刷新失败留给用户手动点刷新 */ }
        await load(true);
      }
      msg((r && r.error) || "主题保存失败", false);
      return;
    }
    if (r.editor && r.editor.ok) editor = normalizeEditor(r.editor);
    await load(true);
    msg(r.msg || (applyNow ? "已保存并应用" : "已保存"), true);
  }

  async function restoreThemeEditor(applyNow) {
    if (busy || !editor) return;
    if (!window.confirm("把主题源文件恢复到上一个保存版本？\n\n当前版本也会先留档，操作可再恢复。")) return;
    busy = true;
    msg("正在恢复主题上一版…", true);
    let r;
    try { r = await api().codexcfg_restore_theme_edit(editor.id, !!applyNow, editor.revision); }
    catch (e) { r = { ok: false, error: String(e) }; }
    busy = false;
    if (!r || !r.ok) { msg((r && r.error) || "主题历史恢复失败", false); return; }
    try {
      const fresh = await api().codexcfg_theme_editor(editor.id);
      if (fresh && fresh.ok) editor = normalizeEditor(fresh);
    } catch (e) { /* 后端已经恢复，页面仍可手动刷新 */ }
    await load(true);
    msg(r.msg || "已恢复上一版", true);
  }

  async function load(silent) {
    if (!silent) $("cfgState").textContent = "读取中…";
    let r;
    try { r = await api().codexcfg_status(); }
    catch (e) { r = { ok: false, error: String(e) }; }
    if (!r || !r.ok) {
      $("cfgState").textContent = "读不到配置";
      $("cfgState").className = "jira-state jira-bad";
      msg((r && r.error) || "读取失败", false);
      return;
    }
    state = r;
    try {
      state.backups = await api().codexcfg_backups();
    } catch (e) { state.backups = { items: [] }; }
    const t = (r.theme && r.theme.active && r.theme.active.name) || "未知主题";
    const themeLive = !!(r.theme && r.theme.active && r.theme.active.runtime &&
      r.theme.active.runtime.active);
    $("cfgState").className = "jira-state " + (themeLive ? "jira-good" : "cfg-warn");
    $("cfgState").textContent = "主题：" + t + (themeLive ? "（生效）" : "（已选，引擎未跑）") + " · 宠物 " +
      ((r.pets && r.pets.island_pet) || "—").split("--")[0] +
      " · 灵动岛" + (r.island && r.island.running ? "运行中" : "没在跑");
    render();
  }

  async function sidebarAct(name) {
    if (busy) return;
    if (!api() || typeof api()[name] !== "function") {
      msg("控制台还是旧包，换装后才有侧栏探测", false);
      return;
    }
    busy = true;
    msg(name.indexOf("inject") >= 0 ? "正在注入…" : "正在探测…", true);
    let r;
    try { r = await api()[name](); }
    catch (e) { r = { ok: false, error: String(e) }; }
    busy = false;
    if (!state.board) state.board = {};
    state.board.sidebar = r || {};
    render();
    msg((r && (r.msg || r.error)) || "没回音", !!(r && r.ok));
  }

  async function repairSkinCompat() {
    if (busy) return;
    busy = true;
    msg("正在重打 26.803 皮肤锚点补丁…", true);
    let r;
    try { r = await api().codexcfg_skin_compat(true); }
    catch (e) { r = { ok: false, error: String(e) }; }
    busy = false;
    if (!r || !r.ok) { msg((r && r.error) || "补丁失败", false); return; }
    msg(r.msg || "补丁已打", true);
    await load(true);
  }

  async function peekTheme(id, btn) {
    const row = btn.closest(".cfg-row");
    const shown = row.parentNode.querySelector('.cfg-peek[data-for="' + id + '"]');
    if (shown) { shown.remove(); btn.textContent = "预览"; return; }
    btn.textContent = "取消";
    let r;
    try { r = await api().codexcfg_theme_preview(id); }
    catch (e) { r = { ok: false, error: String(e) }; }
    if (!r || !r.ok) { btn.textContent = "预览"; msg((r && r.error) || "预览失败", false); return; }
    const box = document.createElement("div");
    box.className = "cfg-peek";
    box.dataset.for = id;
    box.innerHTML = '<img alt="主题预览" src="' + r.preview + '">';
    row.insertAdjacentElement("afterend", box);
  }

  async function doBackup() {
    if (busy) return;
    busy = true;
    msg("正在打包…", true);
    let r;
    try { r = await api().codexcfg_backup(); }
    catch (e) { r = { ok: false, error: String(e) }; }
    busy = false;
    msg((r && r.ok) ? (r.msg || "已备份") : ((r && r.error) || "备份失败"), !!(r && r.ok));
    await load(true);
  }

  async function restore(path) {
    if (busy) return;
    if (!window.confirm("用这份备份覆盖当前装扮？\n\n灵动岛配置会写回；主题会重新应用，必要时关闭并重新打开 Codex。")) return;
    busy = true;
    msg("正在恢复…", true);
    let r;
    try { r = await api().codexcfg_restore(path); }
    catch (e) { r = { ok: false, error: String(e) }; }
    busy = false;
    msg((r && r.ok) ? (r.msg || "已恢复") : ((r && r.error) || "恢复失败"), !!(r && r.ok));
    await load(true);
  }

  async function applyTheme(id, row) {
    if (busy || !id) return;
    const name = ((state.theme.library || []).find((x) => x.id === id) || {}).name || id;
    if (!window.confirm("把 Codex 主题换成「" + name + "」？\n\n" +
      "会调用 Dream Skin 官方脚本；若主题引擎未运行，会关闭并重新打开 Codex。\n" +
      "未发送的输入请先保存，画面通过运行态验证后才会报成功。")) return;
    busy = true;
    if (row) row.classList.add("is-busy");
    msg("正在应用并验证主题；Codex 可能会重启…", true);
    let r;
    try { r = await api().codexcfg_apply_theme(id); }
    catch (e) { r = { ok: false, error: String(e) }; }
    busy = false;
    if (!r || !r.ok) { msg((r && r.error) || "应用失败", false); await load(true); return; }
    msg(r.msg || "已应用", true);
    await load(true);
  }

  async function setPet(id) {
    if (busy || !id) return;
    busy = true;
    let r;
    try { r = await api().codexcfg_set_island_pet(id); }
    catch (e) { r = { ok: false, error: String(e) }; }
    busy = false;
    if (!r || !r.ok) { msg((r && r.error) || "切换失败", false); return; }
    msg(r.msg || "已切换", true);
    await load(true);
  }

  async function setAutostart(on) {
    if (busy) return;
    busy = true;
    let r;
    try { r = await api().codexcfg_set_island_autostart(on); }
    catch (e) { r = { ok: false, error: String(e) }; }
    busy = false;
    if (!r || !r.ok) { msg((r && r.error) || "改自启失败", false); return; }
    msg(r.msg || "已改", true);
    await load(true);
  }

  function mount() {
    if (mounted) return;
    const host = $("codexcfg-mount");
    if (!host) return;
    host.innerHTML = shell();
    mounted = true;
    $("cfgReload").onclick = () => { editor = null; load(); };
    load();
  }

  document.addEventListener("click", (e) => {
    const nav = e.target.closest && e.target.closest('.nav-item[data-page="codexcfg"]');
    if (nav) setTimeout(mount, 0);
  });

  window.CodexCfgApp = { mount, load };
})();
