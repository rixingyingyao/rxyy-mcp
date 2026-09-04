/* 任务面板（团队版看板）P2 UI。
   契约见 docs/plans/2026-08-13-task-board-design.md。数据层一律走
   window.pywebview.api.board_*（桌面壳与远程网关都通）；window.BOARD_MOCK=true
   或 pywebview 不在（浏览器里直接开测试页）时自动降级到内置假数据。 */
(function () {
  'use strict';

  const root = document.getElementById('boardRoot');
  if (!root) return;

  const $ = (sel, scope) => (scope || root).querySelector(sel);
  const $$ = (sel, scope) => Array.from((scope || root).querySelectorAll(sel));

  const COLUMNS = [
    { key: 'backlog', label: '待批准' },
    { key: 'todo', label: '待认领' },
    { key: 'in_progress', label: '处理中' },
    { key: 'in_review', label: '待验收' },
    { key: 'done', label: '已完成' },
    { key: 'blocked', label: '受阻' },
  ];
  const STATUS_LABEL = COLUMNS.reduce((acc, col) => {
    acc[col.key] = col.label;
    return acc;
  }, {});

  // 契约里优先级就是「高/中/低」三个中文值，但历史数据（taskstage 迁过来的）
  // 用的是 high/normal/low，两套都得认，显示统一成中文。
  const PRIORITY_ALIAS = {
    '高': '高', high: '高', urgent: '高',
    '中': '中', normal: '中', medium: '中', '': '中',
    '低': '低', low: '低',
  };
  const PRIORITY_CLASS = { '高': 'pr-high', '中': 'pr-normal', '低': 'pr-low' };

  const AGENT_META = {
    cursor: { label: 'Cursor', cls: 'ag-cursor' },
    codex: { label: 'Codex', cls: 'ag-codex' },
    '': { label: '未领', cls: 'ag-none' },
  };

  const EVENT_META = {
    create: { icon: '✚', label: '建卡' },
    claim: { icon: '🙋', label: '领取' },
    dispatch: { icon: '📨', label: '派发' },
    move: { icon: '➜', label: '挪列' },
    comment: { icon: '💬', label: '评论' },
    zt: { icon: '⚡', label: '进度' },
    release: { icon: '↩', label: '退回' },
    review: { icon: '✔', label: '验收' },
  };

  const POLL_MS = 10000;

  let apiMode = '';          // '' 未决 / 'live' / 'mock'
  let initialized = false;
  let entering = null;
  let pollTimer = 0;
  // 换卡令牌。board_get / board_list 都是异步的（远程网关上还带着一段真网络），
  // 10s 轮询那拍和人手点的操作会同时在飞，谁先回来不保证。慢的那份回来照写，
  // 串的不只是显示——抽屉里的评论/验收/退回/交付全读 state.detailId，卡号一串，
  // 按钮就打在另一张卡上了。异步回来先验令牌，对不上就整份丢掉。
  let detailSeq = 0;
  let listSeq = 0;

  const state = {
    cards: [],
    projects: [],            // 只增不减：按项目过滤时列表里剩不下别的项目，下拉不能跟着塌掉
    filter: { project: '', archived: false },
    detail: null,            // { card, events }
    detailId: '',
    detailSig: '',           // 详情没变就不重画，免得 10s 轮询把正在输入的评论冲掉
    commentDraft: '',
    loading: false,
    busy: false,
    error: '',
    syncedAt: 0,
    selected: {},           // id -> true，勾选后批量派
    sendIds: [],            // 当前派发弹窗里的那一批
  };

  /* ---------------- 工具 ---------------- */

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, ch => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
  }

  // 后端时间戳可能是秒、毫秒或 ISO 串，三种都得认，认不出就不显示。
  function toDate(value) {
    if (value == null || value === '') return null;
    if (typeof value === 'number' || /^\d+(\.\d+)?$/.test(String(value))) {
      const num = Number(value);
      const date = new Date(num > 1e11 ? num : num * 1000);
      return isNaN(date.getTime()) ? null : date;
    }
    const date = new Date(value);
    return isNaN(date.getTime()) ? null : date;
  }

  function fmtTime(value) {
    const date = toDate(value);
    if (!date) return '';
    const pad = n => String(n).padStart(2, '0');
    return (date.getMonth() + 1) + '-' + pad(date.getDate()) + ' ' +
      pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  function fmtRel(value) {
    const date = toDate(value);
    if (!date) return '';
    const diff = Math.floor((Date.now() - date.getTime()) / 1000);
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 604800) return Math.floor(diff / 86400) + ' 天前';
    return fmtTime(value);
  }

  function priorityOf(card) {
    const raw = String((card && card.priority) || '').trim();
    return PRIORITY_ALIAS[raw] || PRIORITY_ALIAS[raw.toLowerCase()] || '中';
  }

  function agentOf(card) {
    const assignee = (card && card.assignee) || {};
    const type = String(assignee.agent_type || '').toLowerCase();
    const meta = AGENT_META[type] || AGENT_META[''];
    return {
      cls: meta.cls,
      label: meta.label,
      tab: String(assignee.tab_name || '').trim(),
      conversation: String(assignee.conversation_id || '').trim(),
    };
  }

  function toast(msg, kind) {
    let wrap = document.getElementById('bdToastWrap');
    if (!wrap) {
      wrap = document.createElement('div');
      wrap.id = 'bdToastWrap';
      document.body.appendChild(wrap);
    }
    const el = document.createElement('div');
    el.className = 'bd-toast' + (kind ? ' ' + kind : '');
    el.textContent = String(msg == null ? '' : msg);
    wrap.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 220);
    }, 2600);
  }

  /* ---------------- 假数据（mock） ---------------- */

  const MOCK = (function () {
    const now = Math.floor(Date.now() / 1000);
    let seq = 100;

    function card(fields) {
      return Object.assign({
        id: 'BD-' + (++seq),
        title: '',
        desc: '',
        project: 'rxyy tools',
        priority: '中',
        labels: [],
        status: 'todo',
        assignee: { conversation_id: '', agent_type: '', tab_name: '' },
        blocked_by: [],
        version: 1,
        created_at: now - 7200,
        updated_at: now - 3600,
        last_activity: '',
      }, fields);
    }

    const cards = [
      card({
        id: 'BD-101', status: 'backlog', priority: '中',
        title: 'P4 自动化：黑板「收工」信号自动挪待验收',
        desc: 'rxyy-mcp 黑板出现「收工」类别时，把该会话绑定的卡自动挪到 in_review，并把最后一条 zt 进度写进 events。需要先确认黑板信号与卡片的绑定口径。',
        labels: ['自动化', 'P4'],
        last_activity: '等 P1 接口落地后再排期',
      }),
      card({
        id: 'BD-102', status: 'todo', priority: '高', project: 'codex-dream',
        title: 'Codex 原生直连技能 manage-board',
        desc: '仿 dashi manage-taskboard 写一份技能，让 Codex 用 CLI 直打看板 HTTP API，绑自己的 thread-id，不经 MCP。',
        labels: ['P3', 'skill'],
        last_activity: '待认领',
      }),
      card({
        id: 'BD-103', status: 'todo', priority: '低',
        title: '任务安排站存量数据迁入任务面板',
        desc: 'taskstage 现有草稿/已派发记录映射到看板卡片，保留附件与派发历史。',
        labels: ['P5', '迁移'],
        blocked_by: ['BD-105'],
        last_activity: '被 BD-105 卡住',
      }),
      card({
        id: 'BD-104', status: 'in_progress', priority: '高',
        title: 'P2 看板 UI：board.js + board.css',
        desc: '六列看板 + 卡片抽屉 + 项目筛选 + 新建卡 + 10s 轮询，数据层按契约走 board_*，mock 期字段一致。',
        labels: ['P2', '前端'],
        assignee: { conversation_id: 'bc18e44d', agent_type: 'cursor', tab_name: 'rxyy tools·看板UI' },
        version: 3,
        updated_at: now - 240,
        last_activity: '⚡ developing · 画卡片与抽屉',
      }),
      card({
        id: 'BD-105', status: 'in_progress', priority: '高',
        title: 'P1 hub 看板接口与数据模型',
        desc: 'hub tasks 模型加看板字段（status/assignee/conversation_id/events/version），并开 /board 系列端点。',
        labels: ['P1', '后端'],
        assignee: { conversation_id: '7edf659a', agent_type: 'codex', tab_name: 'rxyy tools·任务面板' },
        version: 5,
        updated_at: now - 900,
        last_activity: '⚡ developing · 建卡与领取端点自测中',
      }),
      card({
        id: 'BD-106', status: 'in_review', priority: '高', project: 'codex-dream',
        title: '多账号管理：cc-switch 与控制台 Codex 账号页打通方案',
        desc: '只读盘点两边现状并给出分级打通方案，不改任何文件。',
        labels: ['调研'],
        assignee: { conversation_id: 'bc18e44d', agent_type: 'cursor', tab_name: 'codex-dream·多账号调研' },
        version: 4,
        updated_at: now - 1500,
        last_activity: '调研结论已提交，等验收',
      }),
      card({
        id: 'BD-107', status: 'in_review', priority: '中', project: 'dashi-taskboard',
        title: 'dashi-taskboard 降级为「Codex 专属可选」并写 README',
        desc: '说明 iframe 方案的先天问题与新面板的关系，保留 Codex 侧用法。',
        labels: ['P5', '文档'],
        assignee: { conversation_id: '7edf659a', agent_type: 'codex', tab_name: 'rxyy tools·任务面板' },
        version: 2,
        updated_at: now - 5400,
        last_activity: 'README 草稿已提交',
      }),
      card({
        id: 'BD-108', status: 'done', priority: '中',
        title: '看板方案与 API 契约定稿',
        desc: '分期 P1-P5、Card/Event 字段、乐观锁与验收标准，rxyy 已批。',
        labels: ['设计'],
        assignee: { conversation_id: '7edf659a', agent_type: 'codex', tab_name: 'rxyy tools·任务面板' },
        version: 6,
        updated_at: now - 10800,
        last_activity: 'rxyy 已验收',
      }),
      card({
        id: 'BD-109', status: 'blocked', priority: '中',
        title: 'hub 重启窗口（会短暂断开所有 tab 的 MCP）',
        desc: '接口上线需要重启 hub，重启会断掉所有在线 agent 的连接，得由 rxyy 选时机。',
        labels: ['运维'],
        last_activity: '等 rxyy 指定时间窗',
      }),
      card({
        id: 'BD-110', status: 'todo', priority: '低',
        title: '左侧栏「任务安排站」改名「任务面板」',
        desc: '接线与改名统一由负责人做，本卡只登记。',
        labels: ['P2'],
        last_activity: '待认领',
      }),
    ];

    const events = {
      'BD-104': [
        { ts: now - 4200, kind: 'create', text: '负责人建卡：P2 看板 UI' },
        { ts: now - 3900, kind: 'claim', conversation_id: 'bc18e44d', text: 'Cursor·rxyy tools·看板UI 领取' },
        { ts: now - 3000, kind: 'zt', conversation_id: 'bc18e44d', text: 'analyzing · 读设计文档与 taskstage 结构' },
        { ts: now - 1200, kind: 'zt', conversation_id: 'bc18e44d', text: 'developing · 搭六列骨架与数据层' },
        { ts: now - 600, kind: 'comment', conversation_id: 'bc18e44d', text: '契约更正已收到：数据层改走 pywebview board_*，不打 38777。' },
        { ts: now - 240, kind: 'zt', conversation_id: 'bc18e44d', text: 'developing · 画卡片与抽屉' },
      ],
      'BD-105': [
        { ts: now - 5400, kind: 'create', text: '负责人建卡：P1 hub 接口' },
        { ts: now - 5200, kind: 'claim', conversation_id: '7edf659a', text: 'Codex·任务面板 领取' },
        { ts: now - 900, kind: 'zt', conversation_id: '7edf659a', text: 'developing · 建卡与领取端点自测中' },
      ],
      'BD-106': [
        { ts: now - 9000, kind: 'create', text: '负责人建卡：多账号管理调研' },
        { ts: now - 8600, kind: 'claim', conversation_id: 'bc18e44d', text: 'Cursor·codex-dream·多账号调研 领取' },
        { ts: now - 2000, kind: 'comment', conversation_id: 'bc18e44d', text: '结论：两边已打通一半，缺 provider 列表、健康与成本三块。' },
        { ts: now - 1500, kind: 'move', conversation_id: 'bc18e44d', text: '挪到 待验收' },
      ],
      'BD-108': [
        { ts: now - 20000, kind: 'create', text: '负责人建卡：看板方案定稿' },
        { ts: now - 12000, kind: 'move', conversation_id: '7edf659a', text: '挪到 待验收' },
        { ts: now - 10800, kind: 'review', text: 'rxyy 验收通过' },
      ],
      'BD-109': [
        { ts: now - 14000, kind: 'create', text: '负责人建卡：hub 重启窗口' },
        { ts: now - 13000, kind: 'move', text: '挪到 受阻：等 rxyy 指定时间窗' },
      ],
    };

    function find(id) {
      return cards.find(item => item.id === id) || null;
    }

    function fail(code, error) {
      return { ok: false, code, error };
    }

    function touch(item, kind, text, conversationId) {
      item.version += 1;
      item.updated_at = Math.floor(Date.now() / 1000);
      item.last_activity = text;
      if (!events[item.id]) events[item.id] = [];
      events[item.id].push({
        ts: item.updated_at, kind, text,
        conversation_id: conversationId || '',
      });
    }

    function guard(item, version) {
      if (version == null || version === '') return null;
      if (Number(version) !== Number(item.version)) {
        return fail('VERSION_CONFLICT', '卡片已被其他人更新（当前版本 ' + item.version + '）');
      }
      return null;
    }

    const delay = () => new Promise(resolve => setTimeout(resolve, 120));

    return {
      async board_list(query) {
        await delay();
        const q = query || {};
        const list = cards.filter(item => (
          (q.archived ? true : !item.archived) &&
          (!q.project || item.project === q.project) &&
          (!q.status || item.status === q.status)
        ));
        list.sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0));
        const projects = [];
        cards.forEach(item => {
          const name = String(item.project || '').trim();
          if (name && projects.indexOf(name) < 0) projects.push(name);
        });
        return { ok: true, cards: list.map(item => JSON.parse(JSON.stringify(item))),
          projects };
      },
      async board_get(id) {
        await delay();
        const item = find(id);
        if (!item) return fail('NOT_FOUND', '卡片不存在');
        return {
          ok: true,
          card: JSON.parse(JSON.stringify(item)),
          events: (events[id] || []).slice().sort((a, b) => a.ts - b.ts),
        };
      },
      async board_create(fields) {
        await delay();
        const payload = fields || {};
        const title = String(payload.title || '').trim();
        if (!title) return fail('BAD_REQUEST', '标题不能为空');
        const item = card({
          id: 'BD-' + (++seq),
          title,
          desc: String(payload.desc || ''),
          project: String(payload.project || '') || 'rxyy tools',
          priority: PRIORITY_ALIAS[String(payload.priority || '中')] || '中',
          labels: Array.isArray(payload.labels) ? payload.labels : [],
          blocked_by: Array.isArray(payload.blocked_by) ? payload.blocked_by : [],
          status: (payload.blocked_by || []).length ? 'blocked' : 'todo',
          created_at: Math.floor(Date.now() / 1000),
          updated_at: Math.floor(Date.now() / 1000),
          last_activity: '刚建卡，待认领',
        });
        cards.unshift(item);
        events[item.id] = [{
          ts: item.created_at, kind: 'create', text: '在面板上建卡：' + title,
        }];
        return { ok: true, card: JSON.parse(JSON.stringify(item)) };
      },
      async board_claim(id, payload) {
        await delay();
        const item = find(id);
        if (!item) return fail('NOT_FOUND', '卡片不存在');
        const conflict = guard(item, (payload || {}).version);
        if (conflict) return conflict;
        if (item.assignee && item.assignee.conversation_id) {
          return fail('CLAIMED', '这张卡已经被 ' + item.assignee.tab_name + ' 领走了');
        }
        if ((item.blocked_by || []).length) {
          return fail('BLOCKED', '前置卡未完成：' + item.blocked_by.join('、'));
        }
        item.assignee = {
          conversation_id: String((payload || {}).conversation_id || ''),
          agent_type: String((payload || {}).agent_type || 'cursor'),
          tab_name: String((payload || {}).tab_name || ''),
        };
        item.status = 'in_progress';
        touch(item, 'claim', (item.assignee.tab_name || item.assignee.conversation_id) + ' 领取',
          item.assignee.conversation_id);
        return { ok: true, card: JSON.parse(JSON.stringify(item)) };
      },
      async board_move(id, payload) {
        await delay();
        const item = find(id);
        if (!item) return fail('NOT_FOUND', '卡片不存在');
        const conflict = guard(item, (payload || {}).version);
        if (conflict) return conflict;
        const status = String((payload || {}).status || '');
        if (!STATUS_LABEL[status]) return fail('BAD_REQUEST', '未知的列');
        item.status = status;
        touch(item, 'move', '挪到 ' + STATUS_LABEL[status], (payload || {}).conversation_id);
        return { ok: true, card: JSON.parse(JSON.stringify(item)) };
      },
      async board_comment(id, payload) {
        await delay();
        const item = find(id);
        if (!item) return fail('NOT_FOUND', '卡片不存在');
        const body = String((payload || {}).body || '').trim();
        if (!body) return fail('BAD_REQUEST', '评论不能为空');
        touch(item, 'comment', body, (payload || {}).conversation_id);
        return { ok: true, card: JSON.parse(JSON.stringify(item)) };
      },
      async board_review(id, payload) {
        await delay();
        const item = find(id);
        if (!item) return fail('NOT_FOUND', '卡片不存在');
        const conflict = guard(item, (payload || {}).version);
        if (conflict) return conflict;
        item.status = 'done';
        touch(item, 'review', 'rxyy 验收通过');
        return { ok: true, card: JSON.parse(JSON.stringify(item)) };
      },
      async board_dispatch_sessions() {
        await delay();
        return {
          ok: true,
          sessions: [
            {id: 's1', name: '智慧云广播·播控修复', cwd: 'D:\\work\\playthread-go',
             connected: true, pending: false, queued: 1},
            {id: 's2', name: 'rxyy tools·看板UI', cwd: 'D:\\work\\cursor工作流',
             connected: true, pending: true, queued: 0},
          ],
        };
      },
      async board_dispatch_card(id, args) {
        await delay();
        args = args || {};
        const item = find(id);
        if (!item) return fail('NOT_FOUND', '卡片不存在');
        if (item.status === 'done' || item.archived) {
          return fail('NOT_DISPATCHABLE', item.archived ? '归档的卡先放回面板再派' : '已完成的卡不用再派');
        }
        const tab = args.tab_name || args.session_id;
        // 刻意不改状态、不写 assignee：归属只能由对方自己 claim 产生
        touch(item, 'dispatch', '已派给 ' + tab + '，等它自己领' +
          (args.note ? '：' + args.note : ''), 'console');
        return {ok: true, qid: 'q1', msg: '已派给 ' + tab, sent: [id], skipped: [], cards: [item]};
      },
      async board_dispatch_cards(ids, args) {
        await delay();
        args = args || {};
        const sent = [];
        const skipped = [];
        (ids || []).forEach(id => {
          const item = find(id);
          if (!item) {
            skipped.push({id: id, code: 'NOT_FOUND', error: '卡片不存在'});
            return;
          }
          if (item.status === 'done' || item.archived) {
            skipped.push({id: id, code: 'NOT_DISPATCHABLE',
              error: item.archived ? '归档的卡先放回面板再派' : '已完成的卡不用再派'});
            return;
          }
          const tab = args.tab_name || args.session_id;
          touch(item, 'dispatch', '已派给 ' + tab + '，等它自己领' +
            ((ids || []).length > 1 ? '（批量 ' + ids.length + ' 张）' : '') +
            (args.note ? '：' + args.note : ''), 'console');
          sent.push(id);
        });
        if (!sent.length) {
          const first = skipped[0] || {};
          return fail(first.code || 'INVALID', first.error || '没有能派的卡');
        }
        return {ok: true, qid: 'q1', sent: sent, skipped: skipped,
          msg: '已派 ' + sent.length + ' 张给 ' + (args.tab_name || args.session_id) +
            (sent.length > 1 ? '（合成一条）' : '')};
      },
      async board_archive(id, payload) {
        await delay();
        const item = find(id);
        if (!item) return fail('NOT_FOUND', '卡片不存在');
        const want = (payload || {}).archived !== false;
        if (!!item.archived === want) {
          return {ok: true, changed: false, msg: '本来就是这个状态'};
        }
        item.archived = want;
        touch(item, 'archive', want ? '归档：收起不再占列' : '取消归档：放回面板', 'console');
        return {ok: true, changed: true, msg: want ? '已归档（数据还在）' : '已放回面板'};
      },
      async board_release(id, payload) {
        await delay();
        const item = find(id);
        if (!item) return fail('NOT_FOUND', '卡片不存在');
        const conflict = guard(item, (payload || {}).version);
        if (conflict) return conflict;
        const reason = String((payload || {}).reason || '').trim();
        item.assignee = { conversation_id: '', agent_type: '', tab_name: '' };
        item.status = 'todo';
        touch(item, 'release', '退回待认领' + (reason ? '：' + reason : ''),
          (payload || {}).conversation_id);
        return { ok: true, card: JSON.parse(JSON.stringify(item)) };
      },
    };
  })();

  /* ---------------- 数据层 ---------------- */

  async function resolveApi() {
    if (apiMode) return apiMode;
    if (window.BOARD_MOCK === true) {
      apiMode = 'mock';
      return apiMode;
    }
    if (!(window.pywebview && window.pywebview.api)) {
      // 桌面壳里 pywebview 是异步注入的；浏览器里永远等不到，所以给个上限，
      // 到点还没有就按 mock 跑，测试页才能直接开。
      await new Promise(resolve => {
        let settled = false;
        const finish = () => { if (!settled) { settled = true; resolve(); } };
        window.addEventListener('pywebviewready', finish, { once: true });
        setTimeout(finish, 1500);
      });
    }
    const host = window.pywebview && window.pywebview.api;
    apiMode = host && typeof host.board_list === 'function' ? 'live' : 'mock';
    return apiMode;
  }

  async function call(name, ...args) {
    const mode = await resolveApi();
    const host = mode === 'live' ? window.pywebview.api : MOCK;
    if (typeof host[name] !== 'function') {
      return { ok: false, code: 'NOT_IMPLEMENTED', error: '后端还没提供 ' + name };
    }
    try {
      const result = await host[name](...args);
      if (!result || typeof result !== 'object') {
        return { ok: false, code: 'BAD_RESPONSE', error: name + ' 没有返回数据' };
      }
      return result;
    } catch (err) {
      // 契约要求后端不抛异常；桥断了或旧包缺方法时这里兜住，别让页面白掉。
      return { ok: false, code: 'CALL_FAILED', error: (err && err.message) || (name + ' 调用失败') };
    }
  }

  function handleFail(result, fallback) {
    const msg = (result && result.error) || fallback;
    if (result && result.code === 'VERSION_CONFLICT') {
      toast(msg + '，已重新拉取', 'warn');
      refresh(true);
      if (state.detailId) loadDetail(state.detailId, true);
      return;
    }
    toast(msg, 'err');
  }

  /* ---------------- 骨架 ---------------- */

  function buildLayout() {
    root.innerHTML =
      '<div class="bd-toolbar">' +
        '<div class="bd-toolbar-left">' +
          '<select class="bd-select" id="bdProject" title="按项目筛选"></select>' +
          '<span class="bd-sync" id="bdSync"></span>' +
        '</div>' +
        '<div class="bd-toolbar-right">' +
          '<span class="bd-mode" id="bdMode" hidden></span>' +
          '<label class="bd-archived-toggle" title="归档的卡默认收起来">' +
            '<input type="checkbox" id="bdShowArchived">显示已归档</label>' +
          '<button class="btn" id="bdRefresh" type="button">↻ 刷新</button>' +
          '<button class="btn btn-forge" id="bdNew" type="button">＋ 新建卡</button>' +
        '</div>' +
      '</div>' +
      '<div class="bd-bulk" id="bdBulk" hidden>' +
        '<span id="bdBulkCount">已选 0 张</span>' +
        '<button class="btn btn-forge" id="bdBulkSend" type="button">派给…</button>' +
        '<button class="btn" id="bdBulkClear" type="button">取消选择</button>' +
      '</div>' +
      '<div class="bd-banner" id="bdBanner" hidden></div>' +
      '<div class="bd-cols" id="bdCols"></div>' +
      drawerHtml() +
      createModalHtml() +
      dispatchModalHtml();
  }

  function drawerHtml() {
    return '<div class="bd-drawer-mask" id="bdDrawerMask" hidden></div>' +
      '<aside class="bd-drawer" id="bdDrawer" hidden aria-label="卡片详情">' +
        '<div class="bd-drawer-head">' +
          '<div class="bd-drawer-head-main" id="bdDrawerHead"></div>' +
          '<button class="bd-x" id="bdDrawerX" type="button" aria-label="关闭">×</button>' +
        '</div>' +
        '<div class="bd-drawer-body" id="bdDrawerBody"></div>' +
        '<div class="bd-drawer-foot" id="bdDrawerFoot"></div>' +
      '</aside>';
  }

  function createModalHtml() {
    return '<div class="bd-modal-mask" id="bdNewMask" hidden>' +
      '<div class="bd-modal" role="dialog" aria-modal="true" aria-labelledby="bdNewTitle">' +
        '<div class="bd-modal-head"><b id="bdNewTitle">新建任务卡</b>' +
          '<button class="bd-x" id="bdNewX" type="button" aria-label="关闭">×</button></div>' +
        '<div class="bd-modal-body">' +
          '<label class="bd-fld"><span>标题<i>*</i></span>' +
            '<input class="bd-input" id="bdNewTitleInput" maxlength="120" ' +
              'placeholder="一句话说清要做什么"></label>' +
          '<label class="bd-fld"><span>描述</span>' +
            '<textarea class="bd-textarea" id="bdNewDesc" ' +
              'placeholder="背景、目标、验收要点"></textarea></label>' +
          '<div class="bd-fld-row">' +
            '<label class="bd-fld"><span>项目</span>' +
              '<input class="bd-input" id="bdNewProject" list="bdProjectList" ' +
                'placeholder="如 rxyy tools"><datalist id="bdProjectList"></datalist></label>' +
            '<label class="bd-fld"><span>优先级</span>' +
              '<select class="bd-select" id="bdNewPriority">' +
                '<option value="高">高</option>' +
                '<option value="中" selected>中</option>' +
                '<option value="低">低</option>' +
              '</select></label>' +
          '</div>' +
          '<label class="bd-fld"><span>依赖（选中的卡完成前不能被领取）</span>' +
            '<select class="bd-select bd-multi" id="bdNewBlocked" multiple size="4"></select></label>' +
        '</div>' +
        '<div class="bd-modal-foot">' +
          '<span class="bd-muted">新建的卡默认进「待认领」，选了依赖则进「受阻」。</span>' +
          '<button class="btn" id="bdNewCancel" type="button">取消</button>' +
          '<button class="btn btn-forge" id="bdNewGo" type="button">建卡</button>' +
        '</div>' +
      '</div></div>';
  }

  function dispatchModalHtml() {
    return '<div class="bd-modal-mask" id="bdSendMask" hidden>' +
      '<div class="bd-modal" role="dialog" aria-modal="true" aria-labelledby="bdSendTitle">' +
        '<div class="bd-modal-head"><b id="bdSendTitle">把这张卡派给…</b>' +
          '<button class="bd-x" id="bdSendX" type="button" aria-label="关闭">×</button></div>' +
        '<div class="bd-modal-body" id="bdSendBody"></div>' +
        '<div class="bd-modal-foot">' +
          // 说清楚派发不等于挂上：卡的归属只能由对方自己领出来
          '<span class="bd-muted" id="bdSendHint">派发只是把活递过去，卡还在原列，' +
            '等它自己领了才算它的。多张会合成一条消息。</span>' +
          '<button class="btn" id="bdSendCancel" type="button">取消</button>' +
          '<button class="btn btn-forge" id="bdSendGo" type="button">递过去</button>' +
        '</div>' +
      '</div></div>';
  }

  function bindLayout() {
    $('#bdProject').addEventListener('change', event => {
      state.filter.project = event.target.value;
      refresh();
    });
    $('#bdShowArchived').addEventListener('change', event => {
      state.filter.archived = event.target.checked;
      refresh();
    });
    $('#bdRefresh').addEventListener('click', () => refresh());
    $('#bdNew').addEventListener('click', openCreate);
    $('#bdDrawerX').addEventListener('click', closeDetail);
    $('#bdDrawerMask').addEventListener('click', closeDetail);
    $('#bdNewX').addEventListener('click', closeCreate);
    $('#bdNewCancel').addEventListener('click', closeCreate);
    $('#bdNewGo').addEventListener('click', submitCreate);
    $('#bdSendX').addEventListener('click', closeSend);
    $('#bdSendCancel').addEventListener('click', closeSend);
    $('#bdSendGo').addEventListener('click', submitSend);
    $('#bdBulkSend').addEventListener('click', () => openSend(selectedIds()));
    $('#bdBulkClear').addEventListener('click', clearSelected);
    $('#bdCols').addEventListener('click', event => {
      if (event.target.closest('.bd-pick')) {
        event.stopPropagation();
        return;
      }
      const colPick = event.target.closest('[data-bd-col]');
      if (colPick) {
        event.stopPropagation();
        toggleColPick(colPick.dataset.bdCol);
        return;
      }
      const card = event.target.closest('[data-bd-card]');
      if (card) openDetail(card.dataset.bdCard);
    });
    $('#bdCols').addEventListener('change', event => {
      const box = event.target.closest('[data-bd-pick]');
      if (!box) return;
      if (box.checked) state.selected[box.dataset.bdPick] = true;
      else delete state.selected[box.dataset.bdPick];
      const article = box.closest('.bd-card');
      if (article) article.classList.toggle('is-picked', box.checked);
      renderBulk();
      updateColPickButtons();
    });
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      if (!$('#bdSendMask').hidden) closeSend();
      else if (!$('#bdNewMask').hidden) closeCreate();
      else if (!$('#bdDrawer').hidden) closeDetail();
    });
  }

  /* ---------------- 渲染 ---------------- */

  function renderToolbar() {
    const select = $('#bdProject');
    if (select) {
      // 轮询每 10s 走一次，选项没变就别重建，否则下拉正展开着会被关掉
      const sig = state.projects.join('\u0001');
      if (select.dataset.sig !== sig) {
        select.innerHTML = '<option value="">全部项目</option>' +
          state.projects.map(name =>
            '<option value="' + esc(name) + '">' + esc(name) + '</option>').join('');
        select.dataset.sig = sig;
      }
      select.value = state.filter.project;
    }
    const sync = $('#bdSync');
    if (sync) {
      sync.textContent = state.loading
        ? '加载中…'
        : (state.syncedAt ? '共 ' + state.cards.length + ' 张卡 · ' + fmtRel(state.syncedAt / 1000) + '同步' : '');
    }
    const mode = $('#bdMode');
    if (mode) {
      mode.hidden = apiMode !== 'mock';
      mode.textContent = 'MOCK 假数据';
    }
    const banner = $('#bdBanner');
    if (banner) {
      banner.hidden = !state.error;
      banner.textContent = state.error;
    }
  }

  function renderBoard() {
    const wrap = $('#bdCols');
    if (!wrap) return;
    const grouped = {};
    COLUMNS.forEach(col => { grouped[col.key] = []; });
    state.cards.forEach(card => {
      const key = grouped[card.status] ? card.status : 'backlog';
      grouped[key].push(card);
    });
    wrap.innerHTML = COLUMNS.map(col => {
      const list = grouped[col.key];
      const pickable = list.filter(canDispatchCard);
      const allPicked = pickable.length && pickable.every(c => state.selected[c.id]);
      const body = list.length
        ? list.map(cardHtml).join('')
        : '<div class="bd-col-empty">空</div>';
      return '<section class="bd-col bd-col-' + col.key + '">' +
        '<header class="bd-col-head">' +
          '<span class="bd-col-name">' + esc(col.label) + '</span>' +
          '<span class="bd-col-cnt">' + list.length + '</span>' +
          (pickable.length
            ? '<button type="button" class="bd-col-pick" data-bd-col="' + col.key + '">' +
                (allPicked ? '取消' : '全选') + '</button>'
            : '') +
        '</header>' +
        '<div class="bd-col-body">' + body + '</div>' +
      '</section>';
    }).join('');
    renderBulk();
  }

  function cardHtml(card) {
    const priority = priorityOf(card);
    const agent = agentOf(card);
    const blocked = (card.blocked_by || []).filter(Boolean);
    const labels = (card.labels || []).filter(Boolean).slice(0, 3);
    const activity = String(card.last_activity || '').trim();
    const pickable = canDispatchCard(card);
    const picked = !!state.selected[card.id];
    return '<article class="bd-card' + (picked ? ' is-picked' : '') +
      '" data-bd-card="' + esc(card.id) + '" tabindex="0">' +
      '<div class="bd-card-top">' +
        (pickable
          ? '<label class="bd-pick" title="选中以批量派">' +
              '<input type="checkbox" data-bd-pick="' + esc(card.id) + '"' +
              (picked ? ' checked' : '') + '></label>'
          : '') +
        '<span class="bd-pr ' + PRIORITY_CLASS[priority] + '">' + priority + '</span>' +
        '<span class="bd-card-title">' + esc(card.title) + '</span>' +
      '</div>' +
      '<div class="bd-card-meta">' +
        (card.project ? '<span class="bd-chip">' + esc(card.project) + '</span>' : '') +
        labels.map(label => '<span class="bd-chip bd-chip-soft">' + esc(label) + '</span>').join('') +
        '<span class="bd-agent ' + agent.cls + '" title="' + esc(agent.tab || agent.label) + '">' +
          esc(agent.label) + (agent.tab ? ' · ' + esc(agent.tab) : '') + '</span>' +
      '</div>' +
      (blocked.length
        ? '<div class="bd-card-blocked">⛓ 依赖 ' + esc(blocked.join('、')) + '</div>' : '') +
      reviewBadgeHtml(card.review) +
      '<div class="bd-card-foot">' +
        '<span class="bd-card-act">' + (activity ? esc(activity) : '—') + '</span>' +
        '<span class="bd-card-time">' + esc(fmtRel(card.updated_at)) + '</span>' +
      '</div>' +
    '</article>';
  }

  // 别的 agent 替 rxyy 先验的那一道。他在手机上扫一眼这条就能决定点不点验收，
  // 所以结论和理由都要露在卡面上，别藏进流水里
  function reviewBadgeHtml(review) {
    if (!review || !review.verdict) return '';
    const pass = review.verdict === 'pass';
    const who = String(review.tab_name || review.by || '').trim();
    return '<div class="bd-card-review ' + (pass ? 'is-pass' : 'is-fail') + '">' +
      '<b>' + (pass ? '✔ 复核通过' : '✘ 复核未过') + '</b>' +
      (who ? '<span class="bd-review-by">' + esc(who) + '</span>' : '') +
      (review.text ? '<div class="bd-review-txt">' + esc(review.text) + '</div>' : '') +
    '</div>';
  }

  function renderDetail() {
    const drawer = $('#bdDrawer');
    const mask = $('#bdDrawerMask');
    if (!drawer || !mask) return;
    const detail = state.detail;
    if (!detail || !detail.card) {
      drawer.hidden = true;
      mask.hidden = true;
      return;
    }
    // 轮询每 10s 会重进这里。重建 DOM 会清掉正在写的评论、也会把流水滚回顶部，
    // 所以内容没变就直接返回；真要重画时先把草稿和滚动位置接住。
    const draftBox = $('#bdComment');
    if (draftBox) state.commentDraft = draftBox.value;
    const body = $('#bdDrawerBody');
    const scrollTop = body ? body.scrollTop : 0;
    drawer.hidden = false;
    mask.hidden = false;
    const sig = JSON.stringify([detail.card, detail.events]);
    if (sig && sig === state.detailSig) return;

    const card = detail.card;
    const agent = agentOf(card);
    const priority = priorityOf(card);
    const blocked = (card.blocked_by || []).filter(Boolean);

    $('#bdDrawerHead').innerHTML =
      '<div class="bd-drawer-title">' + esc(card.title) + '</div>' +
      '<div class="bd-drawer-sub">' +
        '<span class="bd-status st-' + esc(card.status) + '">' +
          esc(STATUS_LABEL[card.status] || card.status) + '</span>' +
        '<span class="bd-pr ' + PRIORITY_CLASS[priority] + '">' + priority + '</span>' +
        '<span class="bd-agent ' + agent.cls + '">' + esc(agent.label) +
          (agent.tab ? ' · ' + esc(agent.tab) : '') + '</span>' +
        '<span class="bd-muted">' + esc(card.id) + ' · v' + esc(card.version) + '</span>' +
      '</div>';

    const events = (detail.events || []).slice().sort(
      (a, b) => (toDate(a.ts) || 0) - (toDate(b.ts) || 0));
    $('#bdDrawerBody').innerHTML =
      (card.review
        ? '<div class="bd-sec">' +
            '<div class="bd-sec-t">别的 agent 先验过了</div>' +
            reviewBadgeHtml(card.review) +
            '<div class="bd-muted">这只是判断依据，签字权仍在你手上。</div>' +
          '</div>'
        : '') +
      '<div class="bd-sec">' +
        '<div class="bd-sec-t">描述</div>' +
        '<div class="bd-desc">' + (card.desc ? esc(card.desc) : '<i class="bd-muted">没写描述</i>') + '</div>' +
      '</div>' +
      '<div class="bd-sec bd-grid">' +
        metaRow('项目', card.project || '—') +
        metaRow('负责会话', agent.conversation ? (agent.conversation + (agent.tab ? ' · ' + agent.tab : '')) : '未领取') +
        metaRow('依赖', blocked.length ? blocked.join('、') : '无') +
        metaRow('创建', fmtTime(card.created_at) || '—') +
        metaRow('更新', fmtTime(card.updated_at) || '—') +
      '</div>' +
      '<div class="bd-sec">' +
        '<div class="bd-sec-t">流水（含 zt 进度）</div>' +
        (events.length
          ? '<ol class="bd-events">' + events.map(eventHtml).join('') + '</ol>'
          : '<div class="bd-muted">还没有动态</div>') +
      '</div>' +
      '<div class="bd-sec">' +
        '<div class="bd-sec-t">评论</div>' +
        '<textarea class="bd-textarea" id="bdComment" placeholder="写给执行 agent 的话，会进流水"></textarea>' +
        '<div class="bd-comment-act">' +
          '<button class="btn" id="bdCommentGo" type="button">发送评论</button>' +
        '</div>' +
      '</div>';

    const canReview = card.status === 'in_review';
    const canRelease = card.status === 'in_progress' || card.status === 'in_review' ||
      card.status === 'blocked';
    // 除已完成/已归档外都能拍给 agent；待验收接手后会回到处理中
    const canSend = card.status !== 'done' && !card.archived;
    // 想法池要先批准才轮得到 agent 领；批过的也能再丢回去
    const canApprove = card.status === 'backlog';
    const canPark = card.status === 'todo';
    $('#bdDrawerFoot').innerHTML =
      '<span class="bd-muted">' + (canReview
        ? '可验收，也可再派给 agent 把没做完的做完（它 claim 后卡回处理中）。'
        : (canApprove ? '想法池也能先派去看需求；要动手仍得先批准进「待认领」。'
          : (canSend ? '可以派给在线 agent；派了它也得自己领，卡才算它的。'
            : '已完成的卡不用再派。'))) + '</span>' +
      (canPark
        ? '<button class="btn" id="bdPark" type="button">退回想法池</button>' : '') +
      (canApprove
        ? '<button class="btn btn-forge" id="bdApprove" type="button">✔ 批准开工</button>' : '') +
      (canSend
        ? '<button class="btn" id="bdSend" type="button">派给…</button>' : '') +
      (canRelease
        ? '<button class="btn" id="bdRelease" type="button">退回待认领</button>' : '') +
      (canReview
        ? '<button class="btn btn-forge" id="bdReview" type="button">✔ 验收通过</button>' : '') +
      // 归档=软删除。面板本来连删都没有，误建的卡只能停服务改文件；这里给个收起来的口子
      '<button class="btn" id="bdArchive" type="button" title="' +
        (card.archived ? '放回面板' : '从面板收起来，数据保留，随时能翻回来') + '">' +
        (card.archived ? '取消归档' : '归档') + '</button>';

    const commentBox = $('#bdComment');
    if (commentBox) {
      commentBox.value = state.commentDraft;
      commentBox.addEventListener('input', () => { state.commentDraft = commentBox.value; });
    }
    const newBody = $('#bdDrawerBody');
    if (newBody && scrollTop) newBody.scrollTop = scrollTop;
    state.detailSig = sig;

    const send = $('#bdSend');
    if (send) send.addEventListener('click', () => openSend());

    const commentGo = $('#bdCommentGo');
    if (commentGo) commentGo.addEventListener('click', submitComment);
    const release = $('#bdRelease');
    if (release) release.addEventListener('click', submitRelease);
    const review = $('#bdReview');
    if (review) review.addEventListener('click', submitReview);
    const approve = $('#bdApprove');
    if (approve) approve.addEventListener('click', () => submitShelve('todo', '批准开工'));
    const park = $('#bdPark');
    if (park) park.addEventListener('click', () => submitShelve('backlog', '退回想法池'));
    const archive = $('#bdArchive');
    if (archive) archive.addEventListener('click', submitArchive);
  }

  function submitArchive() {
    const card = state.detail && state.detail.card;
    if (!card) return;
    const want = !card.archived;
    if (want && !window.confirm('把「' + card.title + '」从面板收起来？\n\n' +
      '数据不会删，勾上工具栏的「显示已归档」就能找回来。')) return;
    act(async () => {
      const result = await call('board_archive', card.id,
        { archived: want, version: card.version });
      if (!result.ok) return handleFail(result, want ? '归档失败' : '取消归档失败');
      toast(result.msg || (want ? '已归档' : '已放回面板'), 'ok');
      if (want) closeDetail(); else await loadDetail(card.id, true);
      await refresh(true);
    });
  }

  // 想法池 ↔ 待认领。这是 rxyy 的规划动作，不带会话 ID（后端按控制台身份处理）
  function submitShelve(status, note) {
    const card = state.detail && state.detail.card;
    if (!card) return;
    act(async () => {
      const result = await call('board_move', card.id,
        { status: status, version: card.version, note: note });
      if (!result.ok) return handleFail(result, note + '失败');
      toast(status === 'todo' ? '已批准，agent 可以领了' : '已丢回想法池', 'ok');
      await loadDetail(card.id, true);
      await refresh(true);
    });
  }

  function metaRow(label, value) {
    return '<div class="bd-meta"><span>' + esc(label) + '</span>' +
      '<b title="' + esc(value) + '">' + esc(value) + '</b></div>';
  }

  function eventHtml(event) {
    const meta = EVENT_META[event.kind] || { icon: '•', label: event.kind || '' };
    const who = String(event.conversation_id || '').trim();
    return '<li class="bd-event ev-' + esc(event.kind || '') + '">' +
      '<span class="bd-event-ic" title="' + esc(meta.label) + '">' + meta.icon + '</span>' +
      '<div class="bd-event-main">' +
        '<div class="bd-event-txt">' + esc(event.text || '') + '</div>' +
        '<div class="bd-event-sub">' + esc(fmtTime(event.ts)) +
          (who ? ' · ' + esc(who) : '') + '</div>' +
      '</div>' +
    '</li>';
  }

  /* ---------------- 数据动作 ---------------- */

  async function refresh(silent) {
    const seq = ++listSeq;
    if (!silent) {
      state.loading = true;
      renderToolbar();
    }
    const result = await call('board_list', {
      project: state.filter.project || '',
      status: '',
      archived: !!state.filter.archived,
    });
    // 换过筛选或又刷了一次：这份是上一问的答案。照写就是列表退回上一拍的样子
    // （刚建的卡不见了、刚换的项目又跳回去），要等下一拍才自愈。
    if (seq !== listSeq) return false;
    state.loading = false;
    if (!result.ok) {
      state.error = result.error || '读取看板失败';
      renderToolbar();
      if (!silent) toast(state.error, 'err');
      return false;
    }
    state.error = '';
    state.cards = Array.isArray(result.cards) ? result.cards : [];
    state.syncedAt = Date.now();
    // 已绑 OA 的仓由后端挂进 result.projects，没有卡也要占一项
    const hung = Array.isArray(result.projects) ? result.projects : [];
    hung.concat(state.cards.map(card => card.project)).forEach(raw => {
      const name = String(raw || '').trim();
      if (name && state.projects.indexOf(name) < 0) state.projects.push(name);
    });
    state.projects.sort();
    pruneSelected();
    renderToolbar();
    renderBoard();
    return true;
  }

  async function loadDetail(id, silent) {
    const seq = ++detailSeq;
    const result = await call('board_get', id);
    // 人已经点开别的卡、或者把抽屉关了：这份属于上一张卡。写下去的话，眼睛看到
    // 的是 B、state.detailId 却退回 A，接着点「验收」签的就是 A 那张。报错分支
    // 一并挡掉，否则过期那趟的红字会顶掉当前这张卡。
    if (seq !== detailSeq || id !== state.detailId) return false;
    if (!result.ok) {
      if (!silent) toast(result.error || '读取卡片失败', 'err');
      return false;
    }
    state.detail = { card: result.card, events: result.events || [] };
    state.detailId = (result.card && result.card.id) || id;
    renderDetail();
    return true;
  }

  async function openDetail(id) {
    if (id !== state.detailId) {
      state.commentDraft = '';
      state.detailSig = '';
    }
    state.detailId = id;
    await loadDetail(id);
  }

  function closeDetail() {
    state.detail = null;
    state.detailId = '';
    state.detailSig = '';
    state.commentDraft = '';
    renderDetail();
  }

  function currentVersion() {
    return state.detail && state.detail.card ? state.detail.card.version : undefined;
  }

  async function act(fn) {
    if (state.busy) return;
    state.busy = true;
    try {
      await fn();
    } finally {
      state.busy = false;
    }
  }

  function submitComment() {
    const box = $('#bdComment');
    const body = box ? box.value.trim() : '';
    if (!body) {
      toast('先写点内容', 'warn');
      return;
    }
    act(async () => {
      const result = await call('board_comment', state.detailId, { body, conversation_id: '' });
      if (!result.ok) return handleFail(result, '评论失败');
      if (box) box.value = '';
      state.commentDraft = '';
      toast('评论已发出', 'ok');
      await loadDetail(state.detailId, true);
      await refresh(true);
    });
  }

  function submitReview() {
    act(async () => {
      const result = await call('board_review', state.detailId, { version: currentVersion() });
      if (!result.ok) return handleFail(result, '验收失败');
      toast('已验收，卡片进「已完成」', 'ok');
      await loadDetail(state.detailId, true);
      await refresh(true);
    });
  }

  function submitRelease() {
    const reason = window.prompt('退回原因（可留空）', '');
    if (reason === null) return;
    act(async () => {
      const result = await call('board_release', state.detailId, {
        version: currentVersion(), reason: reason.trim(), conversation_id: '',
      });
      if (!result.ok) return handleFail(result, '退回失败');
      toast('已退回「待认领」', 'ok');
      await loadDetail(state.detailId, true);
      await refresh(true);
    });
  }

  /* ---------------- 建卡 ---------------- */

  function openCreate() {
    const mask = $('#bdNewMask');
    if (!mask) return;
    $('#bdNewTitleInput').value = '';
    $('#bdNewDesc').value = '';
    $('#bdNewProject').value = state.filter.project || '';
    $('#bdNewPriority').value = '中';
    $('#bdProjectList').innerHTML = state.projects
      .map(name => '<option value="' + esc(name) + '"></option>').join('');
    // 已完成的卡当依赖没意义，挑不出来省得误选
    $('#bdNewBlocked').innerHTML = state.cards
      .filter(card => card.status !== 'done')
      .map(card => '<option value="' + esc(card.id) + '">' +
        esc(card.id + ' · ' + card.title) + '</option>').join('');
    mask.hidden = false;
    setTimeout(() => $('#bdNewTitleInput').focus(), 30);
  }

  function closeCreate() {
    const mask = $('#bdNewMask');
    if (mask) mask.hidden = true;
  }

  function submitCreate() {
    const title = $('#bdNewTitleInput').value.trim();
    if (!title) {
      toast('标题不能为空', 'warn');
      return;
    }
    const blocked = Array.from($('#bdNewBlocked').selectedOptions || []).map(opt => opt.value);
    act(async () => {
      const result = await call('board_create', {
        title,
        desc: $('#bdNewDesc').value.trim(),
        project: $('#bdNewProject').value.trim(),
        priority: $('#bdNewPriority').value,
        labels: [],
        blocked_by: blocked,
      });
      if (!result.ok) return handleFail(result, '建卡失败');
      closeCreate();
      toast('已建卡：' + title, 'ok');
      await refresh(true);
      if (result.card && result.card.id) openDetail(result.card.id);
    });
  }

  /* ---------------- 勾选 / 批量派 ---------------- */

  function canDispatchCard(card) {
    return !!(card && card.status !== 'done' && !card.archived);
  }

  function cardById(id) {
    return state.cards.find(c => c.id === id)
      || (state.detail && state.detail.card && state.detail.card.id === id
        ? state.detail.card : null);
  }

  function selectedIds() {
    return Object.keys(state.selected).filter(id => canDispatchCard(cardById(id)));
  }

  function pruneSelected() {
    Object.keys(state.selected).forEach(id => {
      if (!canDispatchCard(cardById(id))) delete state.selected[id];
    });
  }

  function clearSelected() {
    state.selected = {};
    $$('.bd-card.is-picked').forEach(el => el.classList.remove('is-picked'));
    $$('[data-bd-pick]').forEach(el => { el.checked = false; });
    renderBulk();
    updateColPickButtons();
  }

  function toggleColPick(status) {
    const list = state.cards.filter(c => c.status === status && canDispatchCard(c));
    const allOn = list.length && list.every(c => state.selected[c.id]);
    list.forEach(c => {
      if (allOn) delete state.selected[c.id];
      else state.selected[c.id] = true;
    });
    renderBoard();
  }

  function renderBulk() {
    const ids = selectedIds();
    const bar = $('#bdBulk');
    if (!bar) return;
    bar.hidden = ids.length === 0;
    const count = $('#bdBulkCount');
    if (count) count.textContent = '已选 ' + ids.length + ' 张';
  }

  function updateColPickButtons() {
    $$('[data-bd-col]').forEach(btn => {
      const list = state.cards.filter(c => c.status === btn.dataset.bdCol && canDispatchCard(c));
      const allOn = list.length && list.every(c => state.selected[c.id]);
      btn.textContent = allOn ? '取消' : '全选';
    });
  }

  /* ---------------- 派给在线 agent ---------------- */

  async function openSend(ids) {
    if (!ids || !ids.length) {
      const card = state.detail && state.detail.card;
      if (!card || !canDispatchCard(card)) return;
      ids = [card.id];
    }
    ids = ids.filter(id => canDispatchCard(cardById(id)));
    if (!ids.length) {
      toast('没有能派的卡', 'warn');
      return;
    }
    state.sendIds = ids;
    const cards = ids.map(cardById).filter(Boolean);
    const mask = $('#bdSendMask');
    const body = $('#bdSendBody');
    $('#bdSendTitle').textContent = cards.length === 1
      ? '把「' + cards[0].title + '」派给…'
      : '把这 ' + cards.length + ' 张卡派给…';
    const hintEl = $('#bdSendHint');
    if (hintEl) {
      hintEl.textContent = cards.length === 1
        ? '派发只是把活递过去，卡还在原列，等它自己领了才算它的。'
        : '这 ' + cards.length + ' 张会合成一条消息。派了它也得自己一张张 claim。';
    }
    body.innerHTML = '<div class="bd-muted">正在取在线会话…</div>';
    mask.hidden = false;
    const result = await call('board_dispatch_sessions');
    const sessions = (result.ok && result.sessions) || [];
    // 卡上写了项目就把工作目录像的那个排前面，省得在一排 tab 里找
    const hint = String((cards[0] && cards[0].project) || '').toLowerCase();
    sessions.sort((a, b) => {
      const score = s => (hint && String(s.cwd || '').toLowerCase().indexOf(hint) >= 0 ? -1 : 0);
      return score(a) - score(b);
    });
    const list = sessions.length
      ? sessions.map((s, i) =>
        '<label class="bd-sess">' +
          '<input type="radio" name="bdSess" value="' + esc(s.id) + '"' +
          (i === 0 ? ' checked' : '') + '>' +
          '<span class="bd-sess-name">' + esc(s.name || '未命名会话') + '</span>' +
          '<span class="bd-sess-cwd">' + esc(s.cwd || '未知目录') + '</span>' +
          (s.pending ? '<span class="bd-sess-pending">正等回复 · 会当场送达</span>'
            : '<span class="bd-sess-idle">干活中 · 进队列</span>') +
        '</label>').join('')
      : '<div class="bd-sess-empty">' +
        esc(result.error || '当前没有在线会话。先让对应项目的 agent 调一次持久 MCP。') +
        '</div>';
    const preview = cards.length === 1
      ? '<div class="bd-send-card">' +
          '<b>' + esc(cards[0].title) + '</b>' +
          '<span class="bd-muted">' + esc(cards[0].id) +
            (cards[0].project ? ' · ' + esc(cards[0].project) : '') + '</span>' +
        '</div>'
      : '<div class="bd-send-card">' +
          '<b>一次派 ' + cards.length + ' 张</b>' +
          '<div class="bd-send-chips">' + cards.map(c =>
            '<span class="bd-send-chip" title="' + esc(c.title) + '">' +
              esc(STATUS_LABEL[c.status] || c.status) + ' · ' + esc(c.title) +
            '</span>').join('') + '</div>' +
        '</div>';
    body.innerHTML =
      preview +
      '<div class="bd-sess-list">' + list + '</div>' +
      '<label class="bd-fld"><span>补充说明（可选）</span>' +
        '<textarea class="bd-textarea" id="bdSendNote" ' +
          'placeholder="给它的额外交代，会附在派发消息末尾"></textarea></label>';
    $('#bdSendGo').disabled = !sessions.length;
  }

  function closeSend() {
    const mask = $('#bdSendMask');
    if (mask) mask.hidden = true;
    state.sendIds = [];
  }

  function submitSend() {
    const ids = (state.sendIds || []).filter(id => canDispatchCard(cardById(id)));
    const picked = $('input[name="bdSess"]:checked', $('#bdSendBody'));
    if (!ids.length) return;
    if (!picked) { toast('先选一个在线会话', 'warn'); return; }
    const note = ($('#bdSendNote') || { value: '' }).value.trim();
    const label = picked.closest('.bd-sess').querySelector('.bd-sess-name').textContent;
    const args = { session_id: picked.value, tab_name: label, note: note };
    act(async () => {
      let result;
      if (ids.length === 1) {
        result = await call('board_dispatch_card', ids[0], args);
      } else {
        result = await call('board_dispatch_cards', ids, args);
        if (result.code === 'NOT_IMPLEMENTED' || result.code === 'CALL_FAILED') {
          // 旧包还没有批量接口：逐张递，并告诉人换装后会合成一条
          let ok = 0;
          for (let i = 0; i < ids.length; i++) {
            const one = await call('board_dispatch_card', ids[i], args);
            if (one.ok) ok += 1;
            else if (i === 0) result = one;
          }
          if (ok) {
            result = {ok: true, msg: '已逐张递了 ' + ok + ' 张（控制台还是旧包，换装后会合成一条）'};
          }
        }
      }
      if (!result.ok) return handleFail(result, '派发失败');
      ids.forEach(id => delete state.selected[id]);
      closeSend();
      toast(result.msg || '已递过去', 'ok');
      if (state.detailId) await loadDetail(state.detailId, true);
      await refresh(true);
    });
  }

  /* ---------------- 轮询 ---------------- */

  function onScreen() {
    const page = root.closest('.page');
    if (page && !page.classList.contains('active')) return false;
    return !document.hidden;
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(tick, POLL_MS);
  }

  async function tick() {
    if (onScreen() && !state.busy) {
      await refresh(true);
      if (state.detailId) await loadDetail(state.detailId, true);
    }
    schedulePoll();
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(pollTimer);
    else if (initialized) { refresh(true); schedulePoll(); }
  });

  /* ---------------- 入口 ---------------- */

  async function enter() {
    if (entering) return entering;
    entering = (async () => {
      await resolveApi();
      if (!initialized) {
        initialized = true;
        buildLayout();
        bindLayout();
        await refresh();
        schedulePoll();
      } else {
        const creating = document.getElementById('bdNewMask') && !document.getElementById('bdNewMask').hidden;
        if (!creating) await refresh(true);
        schedulePoll();
      }
    })();
    try {
      await entering;
    } finally {
      entering = null;
    }
  }

  window.BoardApp = { enter };
})();
