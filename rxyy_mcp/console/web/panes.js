/* 可拖分隔条。
 *
 * 单独一个文件而不是塞进 app.js：app.js 十三万字节且常有人同时在改，
 * 这点纯布局的事不该去凑那个热闹。它不依赖 app.js 的任何东西，
 * 加载顺序也就无所谓。
 *
 * 手感跟 rxyy-mcp 的三个页面（ui.html / share.html / tasks.html）对齐：
 * 移上去边线变蓝 → 按住拖 → 松手记住（localStorage）→ 双击复位。
 */
(function () {
  'use strict';

  var LS = {
    get: function (k) { try { return parseInt(localStorage.getItem(k) || '', 10) || 0; } catch (e) { return 0; } },
    set: function (k, v) { try { localStorage.setItem(k, String(v)); } catch (e) {} }
  };

  /* 宽度一律写在 :root 上而不是某个元素上：日报和周报是两张 .rep-grid，
     拖一个另一个也该跟着走；Codebrain 那块还会被整段重绘，写元素上会丢。 */
  function setVar(name, px) {
    document.documentElement.style.setProperty(name, px + 'px');
  }

  /**
   * 通用拖拽绑定。
   *
   * @param {HTMLElement} grip  手柄元素
   * @param {object} o
   * @param {function} o.measure 由指针位置算出目标宽度（px）
   * @param {function} o.settle  松手时回读实际宽度，用来落盘（可选）
   * @param {string}   o.varName CSS 自定义属性名
   * @param {string}   o.storeKey localStorage 键
   * @param {number}   o.min, o.max, o.def
   */
  function bindGrip(grip, o) {
    function apply(px, save) {
      var w = Math.round(Math.min(o.max, Math.max(o.min, px)));
      setVar(o.varName, w);
      if (save) LS.set(o.storeKey, w);
      return w;
    }
    var dragging = false, lastW = 0;
    grip.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      dragging = true;
      lastW = 0;
      document.body.classList.add('resizing');
      e.preventDefault();
      // 捕获放最后并兜住异常：它一抛错会把上面两句连坐掉，拖拽当场失灵
      try { grip.setPointerCapture(e.pointerId); } catch (err) {}
    });
    grip.addEventListener('pointermove', function (e) {
      if (dragging) lastW = apply(o.measure(e), false);
    });
    function stop(e) {
      if (!dragging) return;
      dragging = false;
      try { grip.releasePointerCapture(e.pointerId); } catch (err) {}
      document.body.classList.remove('resizing');
      // 落盘用拖到的那个值，不拿 pointerup 的坐标重量一遍：
      // 那个事件的坐标未必可靠，量歪了宽度会在松手瞬间跳一下
      if (o.settle) apply(o.settle(), true);
      else if (lastW) LS.set(o.storeKey, lastW);
    }
    grip.addEventListener('pointerup', stop);
    grip.addEventListener('pointercancel', stop);
    grip.addEventListener('dblclick', function () { apply(o.def, true); });
    var saved = LS.get(o.storeKey);
    if (saved) apply(saved, false);
  }

  /* ---------- 一、左侧导航栏（#sidebar 与 #content 是 flex 兄弟） ---------- */
  (function navRail() {
    var grip = document.getElementById('sideGrip');
    var pane = document.getElementById('sidebar');
    if (!grip || !pane) return;
    bindGrip(grip, {
      varName: '--sidebar-w', storeKey: 'rxyy_sidebar_w',
      min: 150, max: 420, def: 190,
      // 导航栏贴着窗口左缘，指针的 clientX 就是它该有的宽度
      measure: function (e) { return e.clientX; },
      settle: function () { return pane.offsetWidth; }
    });
  })();

  /* ---------- 二、两栏页：把 grid 的列间距变成手柄 ---------- */
  /* side='left'  第一列是被拖的那列，宽度从容器左缘量到指针
     side='right' 第二列是被拖的那列，宽度从指针量到容器右缘 */
  var GRIDS = [
    { sel: '.rep-grid', varName: '--rep-side', storeKey: 'rxyy_rep_side',
      side: 'right', min: 160, max: 460, def: 220 },
    { sel: '.buy-grid', varName: '--buy-left', storeKey: 'rxyy_buy_left',
      side: 'left', min: 380, max: 760, def: 520 },
    // Codebrain 首行是跨整行的「模型目录」，手柄要插在后面那两块之间
    { sel: '.cba-grid', varName: '--cba-left', storeKey: 'rxyy_cba_left',
      side: 'left', min: 300, max: 780, def: 460,
      childSel: '.cba-panel:not(.cba-panel-worker):not(.cba-panel-catalog)' }
  ];

  function equipGrid(box, cfg) {
    if (box.dataset.gripped === '1') return;
    // 手柄要落在第二条网格轨道上，所以得插在「右边那块」前面
    var anchor = cfg.childSel
      ? box.querySelectorAll(cfg.childSel)[1]
      : box.children[1];
    if (!anchor) return;          // 还没渲染出两块来，等下一次扫描
    box.dataset.gripped = '1';
    var grip = document.createElement('div');
    grip.className = 'pane-grip';
    grip.title = '拖动改两栏宽度，双击复位';
    box.insertBefore(grip, anchor);
    bindGrip(grip, {
      varName: cfg.varName, storeKey: cfg.storeKey,
      min: cfg.min, max: cfg.max, def: cfg.def,
      measure: function (e) {
        var r = box.getBoundingClientRect();
        return cfg.side === 'right' ? r.right - e.clientX : e.clientX - r.left;
      }
    });
  }

  function scanGrids() {
    GRIDS.forEach(function (cfg) {
      Array.prototype.forEach.call(document.querySelectorAll(cfg.sel), function (box) {
        equipGrid(box, cfg);
      });
    });
  }

  scanGrids();

  /* Codebrain 那一页是 innerHTML 整段重绘的，重绘一次手柄就没了。
     与其去改 codebrain.js（那是别人的文件），不如在这儿看着 DOM 自己补回来。
     防抖 200ms：控制台本身在轮询，扫描要便宜到可以忽略。 */
  if (window.MutationObserver) {
    var timer = null;
    new MutationObserver(function () {
      clearTimeout(timer);
      timer = setTimeout(scanGrids, 200);
    }).observe(document.body, { childList: true, subtree: true });
  }
})();
