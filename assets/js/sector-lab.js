/* ============================================================
   行业估值分位 · 板块轮动观察
   纯原生 SVG，无第三方依赖
   数据层：
     data/sector_lab.json       日频全量（估值分位 / 轨迹 / 动量）
     data/sector_lab_live.json  小时级行情兜底快照
     东财行情 JSONP             浏览器直连，盘中 60 秒实时刷新
   ============================================================ */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var D = null, LIVE = null, liveTimer = null;
  var state = {
    range: "3y", scale: "log", group: "all", clockGroups: null,
    hidden: {}, focus: null, pin: null, coord: "valuation",
    sortKey: "rs_3m", sortDesc: true
  };

  var COLORS = {
    coal: "#d9b23c", oil: "#e8a34a", nonferrous: "#c8a13a", steel: "#f0cb72",
    bank: "#4f9cf9", broker: "#83b8ff",
    semi: "#a77bf3", telecom: "#c79bf0", software: "#7f8ff0",
    liquor: "#43d69c", pharma: "#74d2b0", power: "#9fdcc6"
  };
  var GROUP_COLOR = { cyclical: "#d9b23c", financial: "#4f9cf9", growth: "#a77bf3", defensive: "#43d69c" };
  var CLOCK_GROUPS = {
    "衰退": ["growth", "defensive"], "复苏": ["cyclical", "financial", "growth"],
    "过热": ["cyclical", "financial"], "滞胀": ["defensive", "cyclical"]
  };
  var INDEX_SECIDS = [
    ["1.000001", "上证指数"], ["0.399001", "深证成指"],
    ["0.399006", "创业板指"], ["1.000300", "沪深300"]
  ];

  /* PE×PB / PB×动量 两套坐标系的象限判读规则 */
  var QUAD_RULES = {
    valuation: [
      { t: "PB高 · PE低", s: "周期盈利峰值", c: "rgba(217,178,60,.055)",
        rule: "利润处在周期高位，把 PE 的分母撑大、读数压低，但资产端已被给到高 PB。此时「PE 便宜」往往是错觉：一旦盈利回落，PE 会被动抬升。对资源型板块应以 PB、商品价格与产能周期为主锚。" },
      { t: "双高", s: "预期 / 热度高位", c: "rgba(240,74,61,.055)",
        rule: "盈利预期与资产定价同时处于历史偏高区。对成长股，高 PE 通常就对应高估位置，只能靠高增速消化；对周期股，双高多出现在景气与情绪共振的后段。关键看盈利兑现速度能否追上估值扩张。" },
      { t: "双低", s: "相对冷门", c: "rgba(38,165,107,.055)",
        rule: "估值与资产定价同时处在历史偏低区，通常是需求、政策或资金长期冷落的结果。低估不等于补涨：需要分辨是被错杀，还是盈利中枢已系统性下移（价值陷阱）。" },
      { t: "PB低 · PE高", s: "周期盈利谷底", c: "rgba(79,156,249,.06)",
        rule: "盈利被压缩甚至出现亏损，PE 被动抬高（有时根本失效），而 PB 仍在低位。对资源型周期股，这反而常是历史低位区——正是「高 PE 时处于低估位置」的典型几何形态。此时应看 PB、亏损面与商品价格，而不是被高 PE 吓退。" }
    ],
    momentum: [
      { t: "低估值 · 强超额", s: "修复进行中", c: "rgba(67,214,156,.06)",
        rule: "PB 分位偏低但近 3 月跑赢沪深300，说明低估开始被资金定价。这是「估值修复」最常见的起点形态，需要确认超额是来自基本面拐点还是短期轮动。" },
      { t: "高估值 · 强超额", s: "趋势与拥挤并存", c: "rgba(240,74,61,.055)",
        rule: "估值已在历史偏高区、价格仍强势，属于趋势占优但赔率下降的组合。要留意成交额占比与一致预期是否过于拥挤，以及盈利能否继续上修。" },
      { t: "低估值 · 弱超额", s: "价值陷阱待验证", c: "rgba(79,156,249,.06)",
        rule: "便宜但还在跑输，说明市场尚未认可，或基本面仍在恶化。这类位置最需要基本面证据（盈利拐点、供需改善），否则低分位可以长期维持。" },
      { t: "高估值 · 弱超额", s: "高位消化中", c: "rgba(217,178,60,.055)",
        rule: "估值偏高而价格动能转弱，通常是预期见顶后以时间或价格消化估值的阶段。观察分位斜率是否持续下行，以及盈利增速是否同步回落。" }
    ]
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function f1(v) { return v == null ? "—" : Number(v).toFixed(1); }
  function f2(v) { return v == null ? "—" : Number(v).toFixed(2); }
  function sign(v, unit) { return v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(1) + (unit || ""); }
  function color(id) { return COLORS[id] || "#8b98a9"; }
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

  function pctColor(p) {
    if (p == null) return "#59647a";
    var stops = [[0, [79, 156, 249]], [30, [67, 214, 156]], [55, [217, 178, 60]], [100, [240, 94, 70]]];
    for (var i = 0; i < stops.length - 1; i++) {
      if (p <= stops[i + 1][0]) {
        var a = stops[i], b = stops[i + 1], t = (p - a[0]) / (b[0] - a[0]);
        var c = a[1].map(function (v, j) { return Math.round(v + (b[1][j] - v) * t); });
        return "rgb(" + c.join(",") + ")";
      }
    }
    return "#f05e46";
  }
  function retColor(v, bg) {
    if (v == null) return bg ? "transparent" : "var(--text-faint)";
    var a = Math.min(1, Math.abs(v) / 30);
    return bg ? (v >= 0 ? "rgba(240,74,61," + (0.07 + a * 0.42) + ")" : "rgba(38,165,107," + (0.07 + a * 0.42) + ")")
              : (v >= 0 ? "#f06e5e" : "#2fc48c");
  }
  function groupOf(gid) {
    for (var i = 0; i < D.groups.length; i++) if (D.groups[i].id === gid) return D.groups[i];
    return { name: gid, color: "#8b98a9", logic: "", checks: [] };
  }
  function sectorById(id) {
    for (var i = 0; i < D.sectors.length; i++) if (D.sectors[i].id === id) return D.sectors[i];
    return null;
  }
  function visible(s) {
    if (state.hidden[s.id]) return false;
    if (state.clockGroups && state.clockGroups.indexOf(s.group) < 0) return false;
    if (state.group !== "all" && s.group !== state.group) return false;
    return true;
  }
  function avgPct(s) {
    var a = [];
    if (s.pe_pct != null) a.push(s.pe_pct);
    if (s.pb_pct != null) a.push(s.pb_pct);
    return a.length ? a.reduce(function (x, y) { return x + y; }, 0) / a.length : -1;
  }

  /* 北京时间（不依赖用户本地时区） */
  function bjParts() {
    var d = new Date(Date.now() + 8 * 3600 * 1000);
    return { h: d.getUTCHours(), m: d.getUTCMinutes(), s: d.getUTCSeconds(),
             day: d.getUTCDay(), date: d.toISOString().slice(0, 10) };
  }
  function bjStamp(ts) {
    if (!ts) return "";
    var d = new Date((ts > 1e12 ? ts : ts * 1000) + 8 * 3600 * 1000);
    return d.toISOString().slice(0, 10) + " " + d.toISOString().slice(11, 19);
  }
  function sessionOf(tradeDate) {
    var b = bjParts(), hm = b.h * 60 + b.m;
    if (b.day === 0 || b.day === 6 || (tradeDate && tradeDate !== b.date)) return "holiday";
    if ((hm >= 555 && hm < 690) || (hm >= 780 && hm < 905)) return "open";
    if (hm >= 690 && hm < 780) return "lunch";
    return "closed";
  }
  var SESSION_TEXT = { open: "交易中", lunch: "午间休市", closed: "已收盘", holiday: "非交易时段" };

  /* ============================================================ 实时行情层 */
  function jsonpQuotes(secids) {
    return new Promise(function (resolve, reject) {
      var cbName = "__slQuoteCb" + Date.now();
      var script = document.createElement("script");
      var done = false;
      var timer = setTimeout(function () { finish(new Error("行情接口超时")); }, 6500);
      function finish(err, data) {
        if (done) return;
        done = true; clearTimeout(timer);
        try { delete window[cbName]; } catch (e) { window[cbName] = undefined; }
        if (script.parentNode) script.parentNode.removeChild(script);
        err ? reject(err) : resolve(data);
      }
      window[cbName] = function (j) {
        if (j && j.data && j.data.diff) finish(null, j.data.diff);
        else finish(new Error("行情返回格式异常"));
      };
      script.src = "https://push2.eastmoney.com/api/qt/ulist.np/get?cb=" + cbName +
        "&fltt=2&invt=2&fields=f1,f2,f3,f4,f6,f12,f13,f14,f124&secids=" + encodeURIComponent(secids.join(","));
      script.onerror = function () { finish(new Error("行情脚本被拦截")); };
      document.head.appendChild(script);
    });
  }

  function normalizeLive(diff, source) {
    var byCode = {};
    diff.forEach(function (x) { byCode[String(x.f12)] = x; });
    var quotes = {}, maxTs = 0;
    D.sectors.forEach(function (s) {
      var x = byCode[s.code.slice(2)];
      if (!x) return;
      quotes[s.id] = { price: x.f2, pct: x.f3, chg: x.f4, amount: x.f6, ts: x.f124 };
      if (x.f124 > maxTs) maxTs = x.f124;
    });
    var indices = INDEX_SECIDS.map(function (it) {
      var x = byCode[it[0].split(".")[1]];
      return x ? { label: it[1], price: x.f2, pct: x.f3, ts: x.f124 } : null;
    }).filter(Boolean);
    indices.forEach(function (x) { if (x.ts > maxTs) maxTs = x.ts; });
    var b = bjParts();
    var tradeDate = maxTs ? bjStamp(maxTs).slice(0, 10) : null;
    return {
      source: source, quotes: quotes, indices: indices,
      trade_date: tradeDate, session: sessionOf(tradeDate),
      at: ("0" + b.h).slice(-2) + ":" + ("0" + b.m).slice(-2) + ":" + ("0" + b.s).slice(-2),
      quote_at: maxTs ? bjStamp(maxTs).slice(11, 19) : ""
    };
  }

  function normalizeSnapshot(j) {
    var quotes = j.quotes || {}, maxTs = 0;
    Object.keys(quotes).forEach(function (k) { if (quotes[k].ts > maxTs) maxTs = quotes[k].ts; });
    return {
      source: "snapshot", quotes: quotes,
      indices: (j.indices || []).map(function (x) {
        return { label: x.label || x.name, price: x.price, pct: x.pct, ts: x.ts };
      }),
      trade_date: j.trade_date, session: j.session || sessionOf(j.trade_date),
      at: (j.generated_at || "").match(/\d{2}:\d{2}:\d{2}/) ? (j.generated_at.match(/\d{2}:\d{2}:\d{2}/)[0]) : "—",
      quote_at: maxTs ? bjStamp(maxTs).slice(11, 19) : ""
    };
  }

  function loadLive(manual) {
    var secids = D.sectors.map(function (s) {
      return (s.code.indexOf("sh") === 0 ? "1." : "0.") + s.code.slice(2);
    }).concat(INDEX_SECIDS.map(function (x) { return x[0]; }));
    jsonpQuotes(secids)
      .then(function (diff) { setLive(normalizeLive(diff, "realtime")); })
      .catch(function () {
        return fetch("data/sector_lab_live.json?t=" + Date.now(), { cache: "no-store" })
          .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
          .then(function (j) { setLive(normalizeSnapshot(j)); })
          .catch(function () { setLive(null); });
      })
      .then(function () { if (manual) flashStatus(); });
  }

  function setLive(l) {
    LIVE = l;
    renderStatus();
    renderLiveStrip();
    renderMatrix();
  }
  function flashStatus() {
    var el = $("sl-status");
    if (!el) return;
    el.classList.add("flash");
    setTimeout(function () { el.classList.remove("flash"); }, 700);
  }

  function renderStatus() {
    var el = $("sl-status");
    if (!el) return;
    var mode = !LIVE ? "unavailable" : LIVE.source;
    var label = { realtime: "浏览器实时", snapshot: "小时级快照", unavailable: "通道不可用" }[mode];
    var live = LIVE && LIVE.quotes && Object.keys(LIVE.quotes).length;
    var html =
      '<div class="sl-st-item"><span class="sl-st-dot ' + (mode === "realtime" ? "live" : mode === "snapshot" ? "warm" : "off") + '"></span>' +
        '<b>估值分位</b><span>日频 · ' + esc(D.as_of) + '</span></div>' +
      '<div class="sl-st-item"><b>板块行情</b><span>' + label +
        (live ? " · " + esc(LIVE.at) + " 刷新 · " + SESSION_TEXT[LIVE.session] : "") + '</span></div>' +
      '<div class="sl-st-item"><b>周线走势</b><span>' + esc(D.price_as_of || "—") + ' 收盘</span></div>' +
      '<button class="sl-refresh" id="sl-refresh" title="立即刷新行情">↻ 刷新</button>';
    el.innerHTML = html;
    $("sl-refresh").onclick = function () { loadLive(true); };
  }

  function renderLiveStrip() {
    var el = $("sl-live");
    if (!el) return;
    if (!LIVE) {
      el.innerHTML = '<div class="sl-live-empty">实时行情通道不可用，估值分位与走势仍为最近交易日数据（' + esc(D.as_of) + '）。</div>';
      return;
    }
    var idx = LIVE.indices.map(function (x) {
      return '<span class="sl-idx"><span>' + esc(x.label) + '</span><b style="color:' + retColor(x.pct) + '">' +
        (x.price == null ? "—" : Number(x.price).toFixed(2)) + '</b><i style="color:' + retColor(x.pct) + '">' + sign(x.pct, "%") + '</i></span>';
    }).join("");
    var tiles = D.sectors.map(function (s) {
      var q = LIVE.quotes[s.id];
      var cls = q && q.pct != null ? (q.pct > 0 ? "up" : q.pct < 0 ? "down" : "flat") : "flat";
      return '<span class="sl-tile ' + cls + '" data-id="' + esc(s.id) + '" title="' + esc(s.proxy) + '">' +
        '<span class="n"><i style="background:' + color(s.id) + '"></i>' + esc(s.name) + '</span>' +
        '<b>' + (q && q.pct != null ? sign(q.pct, "%") : "—") + '</b></span>';
    }).join("");
    el.innerHTML = '<div class="sl-live-row">' + idx + '</div><div class="sl-live-tiles">' + tiles + '</div>' +
      '<div class="sl-live-note">红涨绿跌 · ' + (LIVE.source === "realtime" ? "直连行情接口，盘中每 60 秒自动刷新" : "来自小时级兜底快照") +
      (LIVE.trade_date ? " · 行情时间 " + esc(LIVE.trade_date) + " " + esc(LIVE.quote_at) : "") + '</div>';
    Array.prototype.forEach.call(el.querySelectorAll(".sl-tile"), function (t) {
      t.onmouseenter = function () { setFocus(t.getAttribute("data-id")); };
      t.onmouseleave = function () { setFocus(null); };
      t.onclick = function () { state.pin = t.getAttribute("data-id"); lastInterpKey = ""; renderInterp(state.pin, null); };
    });
  }

  /* ============================================================ 概览 KPI */
  function tempText(t) {
    if (t == null) return "—";
    return t < 30 ? "偏冷" : t < 50 ? "中性偏冷" : t < 70 ? "中性偏热" : "偏热";
  }
  function renderKpis() {
    var m = D.market || {};
    var valid = D.sectors.filter(function (s) { return s.pb_pct != null; }).slice()
      .sort(function (a, b) { return a.pb_pct - b.pb_pct; });
    var cold = valid[0], hot = valid[valid.length - 1];
    var items = [
      { k: "估值分位日期", v: esc(D.as_of || "—"), d: "日频更新 · " + D.lookback_years + " 年滚动窗口" },
      { k: "全市场估值温度", v: f1(m.temp) + '<span class="u">%</span>', d: "PB 分位中位数 · " + tempText(m.temp) +
          (m.temp_1y != null ? " · 一年前 " + f1(m.temp_1y) + "%" : ""), bar: m.temp },
      { k: "PB 分位最低", v: cold ? esc(cold.name) : "—", d: cold ? "PB " + f1(cold.pb_pct) + "% · PE " + f1(cold.pe_pct) + "%" : "—" },
      { k: "PB 分位最高", v: hot ? esc(hot.name) : "—", d: hot ? "PB " + f1(hot.pb_pct) + "% · PE " + f1(hot.pe_pct) + "%" : "—" }
    ];
    $("sl-kpis").innerHTML = items.map(function (x) {
      return '<div class="sl-kpi"><div class="k">' + x.k + '</div><div class="v num">' + x.v + '</div>' +
        (x.bar != null ? '<div class="sl-temp"><i style="width:' + clamp(x.bar, 0, 100) + '%;background:' + pctColor(x.bar) + '"></i></div>' : '') +
        '<div class="d">' + x.d + '</div></div>';
    }).join("");
    var gap = m.max_gap;
    $("sl-breadth").innerHTML =
      '<span>低分位 (&lt;30%) <b>' + (m.low_count == null ? "—" : m.low_count) + '</b> 个</span>' +
      '<span>中性 <b>' + (m.mid_count == null ? "—" : m.mid_count) + '</b> 个</span>' +
      '<span>高分位 (&gt;70%) <b>' + (m.high_count == null ? "—" : m.high_count) + '</b> 个</span>' +
      (gap ? '<span>PE/PB 分位背离最大 <b style="color:' + color(gap.id) + '">' + esc(gap.name) + ' ' + sign(gap.gap, "pct") + '</b></span>' : "") +
      '<span class="sl-breadth-hint">背离越大，越不能只看 PE</span>';
  }

  /* ============================================================ 四象限散点 */
  var QUAD = { W: 560, H: 430, L: 46, R: 18, T: 18, B: 46 };
  function axisCfg() {
    if (state.coord === "valuation") {
      return { xk: "pe_pct", yk: "pb_pct", xmin: 0, xmax: 100, ymin: 0, ymax: 100,
        xmid: 50, ymid: 50, xt: [0, 25, 50, 75, 100], yt: [0, 25, 50, 75, 100],
        xlabel: "PE 历史分位 (%) →", ylabel: "PB 历史分位 (%) →",
        quads: QUAD_RULES.valuation };
    }
    return { xk: "pb_pct", yk: "rs_3m", xmin: 0, xmax: 100, ymin: -40, ymax: 40,
      xmid: 50, ymid: 0, xt: [0, 25, 50, 75, 100], yt: [-40, -20, 0, 20, 40],
      xlabel: "PB 历史分位 (%) →", ylabel: "近 3 月相对沪深300超额 (%) →",
      quads: QUAD_RULES.momentum };
  }
  function qx(v) {
    var a = axisCfg();
    return QUAD.L + (clamp(v, a.xmin, a.xmax) - a.xmin) / (a.xmax - a.xmin) * (QUAD.W - QUAD.L - QUAD.R);
  }
  function qy(v) {
    var a = axisCfg();
    return QUAD.T + (a.ymax - clamp(v, a.ymin, a.ymax)) / (a.ymax - a.ymin) * (QUAD.H - QUAD.T - QUAD.B);
  }
  function quadIndex(s) {
    var a = axisCfg(), x = s[a.xk], y = s[a.yk];
    if (x == null || y == null) return -1;
    return (x < a.xmid ? 0 : 1) + (y < a.ymid ? 2 : 0);
  }

  function renderCoordTabs() {
    Array.prototype.forEach.call($("sl-coord-tabs").querySelectorAll(".sl-ctab"), function (el) {
      el.className = "sl-ctab" + (el.getAttribute("data-c") === state.coord ? " on" : "");
      el.onclick = function () {
        state.coord = el.getAttribute("data-c");
        lastInterpKey = "";
        renderAll();
      };
    });
    $("sl-quad-hint").textContent = state.coord === "valuation"
      ? "越靠右 PE 分位越高，越靠上 PB 分位越高 · 悬停空白处看象限判读"
      : "横轴 PB 分位（便宜程度），纵轴近 3 月相对沪深300超额（动量） · 两轴交叉可分辨修复 vs 价值陷阱";
  }

  function renderQuadrant() {
    var svg = $("sl-quad"), o = [], a = axisCfg();
    var W = QUAD.W, H = QUAD.H, L = QUAD.L, T = QUAD.T;
    var pw = W - L - QUAD.R, ph = H - T - QUAD.B;
    var mx = qx(a.xmid), my = qy(a.ymid);

    var quads = [
      { x: L, y: T, w: mx - L, h: my - T, tx: L + 8, ty: T + 15, anchor: "", q: a.quads[0] },
      { x: mx, y: T, w: W - QUAD.R - mx, h: my - T, tx: W - QUAD.R - 8, ty: T + 15, anchor: "end", q: a.quads[1] },
      { x: L, y: my, w: mx - L, h: H - QUAD.B - my, tx: L + 8, ty: H - QUAD.B - 10, anchor: "", q: a.quads[2] },
      { x: mx, y: my, w: W - QUAD.R - mx, h: H - QUAD.B - my, tx: W - QUAD.R - 8, ty: H - QUAD.B - 10, anchor: "end", q: a.quads[3] }
    ];
    quads.forEach(function (q) {
      o.push('<rect x="' + q.x + '" y="' + q.y + '" width="' + q.w + '" height="' + q.h + '" fill="' + q.q.c + '"/>');
      o.push('<text class="q-label" x="' + q.tx + '" y="' + q.ty + '"' +
        (q.anchor ? ' text-anchor="' + q.anchor + '"' : '') + '>' + q.q.t + ' · ' + q.q.s + '</text>');
    });
    a.xt.forEach(function (v) {
      o.push('<line class="q-axis" x1="' + qx(v) + '" y1="' + T + '" x2="' + qx(v) + '" y2="' + (H - QUAD.B) + '"/>');
      o.push('<text class="q-tick" x="' + qx(v) + '" y="' + (H - QUAD.B + 14) + '" text-anchor="middle">' + v + '</text>');
    });
    a.yt.forEach(function (v) {
      o.push('<line class="q-axis" x1="' + L + '" y1="' + qy(v) + '" x2="' + (W - QUAD.R) + '" y2="' + qy(v) + '"/>');
      o.push('<text class="q-tick" x="' + (L - 7) + '" y="' + (qy(v) + 3) + '" text-anchor="end">' + v + '</text>');
    });
    o.push('<line class="q-mid" x1="' + mx + '" y1="' + T + '" x2="' + mx + '" y2="' + (H - QUAD.B) + '"/>');
    o.push('<line class="q-mid" x1="' + L + '" y1="' + my + '" x2="' + (W - QUAD.R) + '" y2="' + my + '"/>');
    o.push('<text class="q-label" x="' + (L + pw / 2) + '" y="' + (H - 8) + '" text-anchor="middle">' + a.xlabel + '</text>');
    o.push('<text class="q-label" transform="translate(12,' + (T + ph / 2) + ') rotate(-90)" text-anchor="middle">' + a.ylabel + '</text>');
    o.push('<text class="q-tick" x="' + (mx + 4) + '" y="' + (T + 10) + '">' +
      (state.coord === "valuation" ? "50% 分位线" : "超额 = 0") + '</text>');

    var pts = D.sectors.filter(function (s) { return s[a.xk] != null && s[a.yk] != null && visible(s); })
      .map(function (s) { return { s: s, x: qx(s[a.xk]), y: qy(s[a.yk]) }; });
    pts.sort(function (p, q) { return p.y - q.y; });
    var placed = [];
    /* 上下各留出一条「象限标题带」，避免高分位/低分位散点的标签压住角标 */
    var bandTop = T + 32, bandBottom = H - QUAD.B - 30;
    pts.forEach(function (p) {
      var growUp = p.y > H - QUAD.B - 48;   // 靠近底部象限角标时，标签向上排
      var ly = p.y + 3.5;
      if (p.y < T + 42) ly = bandTop;
      else if (growUp) ly = bandBottom;
      var tries = 0;
      while (tries < 12 && placed.some(function (q) { return Math.abs(q.y - ly) < 12 && Math.abs(q.x - p.x) < 66; })) {
        ly += growUp ? -12 : 12; tries++;
      }
      placed.push({ x: p.x, y: ly });
      var c = color(p.s.id);
      o.push('<g class="sl-dot' + (state.pin === p.s.id ? " pinned" : "") + '" data-id="' + esc(p.s.id) + '">' +
        '<circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) + '" r="6" fill="' + c + '"/>' +
        '<text x="' + (p.x + 10).toFixed(1) + '" y="' + ly.toFixed(1) + '">' + esc(p.s.name) + '</text></g>');
    });
    svg.innerHTML = o.join("");

    var tip = $("sl-quad-tip"), wrap = $("sl-quad-wrap"), lastTipId = null;
    svg.onmousemove = function (e) {
      var g = e.target.closest ? e.target.closest(".sl-dot") : null;
      if (g) {
        var id = g.getAttribute("data-id"), s = sectorById(id);
        if (!s) return;
        setFocus(id, null);
        if (lastTipId !== id) { tip.innerHTML = tipHtml(s); lastTipId = id; }
        tip.className = "sl-tip on";
        var box = wrap.getBoundingClientRect();
        var lx = e.clientX - box.left + 14, ly = e.clientY - box.top + 12;
        if (lx + 262 > box.width) lx = Math.max(4, lx - 280);
        tip.style.left = lx + "px";
        tip.style.top = Math.min(ly, box.height - 175) + "px";
        return;
      }
      tip.className = "sl-tip"; lastTipId = null;
      var r = svg.getBoundingClientRect();
      var vx = (e.clientX - r.left) * QUAD.W / r.width;
      var vy = (e.clientY - r.top) * QUAD.H / r.height;
      var qi = (vx < mx ? 0 : 1) + (vy < my ? 2 : 0);
      var inPlot = vx >= L && vx <= W - QUAD.R && vy >= T && vy <= H - QUAD.B;
      setFocus(null, inPlot ? qi : null);
    };
    svg.onmouseleave = function () { tip.className = "sl-tip"; lastTipId = null; setFocus(null, null); };
    svg.onclick = function (e) {
      var g = e.target.closest ? e.target.closest(".sl-dot") : null;
      if (!g) return;
      var id = g.getAttribute("data-id");
      state.pin = (state.pin === id) ? null : id;
      lastInterpKey = "";
      renderAll();
    };
  }

  function tipHtml(s) {
    var g = groupOf(s.group);
    var h = '<div class="t" style="color:' + color(s.id) + '">' + esc(s.name) +
      ' <span style="font-size:10px;color:var(--text-faint);font-weight:400">' + esc(g.name) + '</span></div>' +
      '<div class="r">估值行业 <b>' + esc(s.board) + '</b> · 走势 <b>' + esc(s.proxy) + '</b></div><table>' +
      '<tr><td>PE(TTM)</td><td class="v">' + f2(s.pe) + '</td><td>历史分位</td><td class="v">' + f1(s.pe_pct) + '%</td></tr>' +
      '<tr><td>PB(MRQ)</td><td class="v">' + f2(s.pb) + '</td><td>历史分位</td><td class="v">' + f1(s.pb_pct) + '%</td></tr>' +
      '<tr><td>PEG</td><td class="v">' + f2(s.peg) + '</td><td>亏损面</td><td class="v">' + f1(s.loss_ratio) + '%</td></tr>' +
      '<tr><td>近3月超额</td><td class="v" style="color:' + retColor(s.rs_3m) + '">' + sign(s.rs_3m, "%") +
      '</td><td>PE/PB背离</td><td class="v">' + sign(s.pe_pb_gap, "pct") + '</td></tr>';
    if (s.pe_stat && s.pb_stat) {
      h += '<tr><td>PE 低/中/高</td><td class="v" colspan="3">' + s.pe_stat.min + ' / ' + s.pe_stat.median + ' / ' + s.pe_stat.max + '</td></tr>' +
        '<tr><td>PB 低/中/高</td><td class="v" colspan="3">' + s.pb_stat.min + ' / ' + s.pb_stat.median + ' / ' + s.pb_stat.max + '</td></tr>';
    }
    h += '</table><div class="logic">' + esc(g.logic) + '</div>' +
      '<div class="logic" style="border:0;padding-top:2px;margin-top:2px">点击散点可固定下方解读；图例 / 分位条点击可隐藏走势</div>';
    return h;
  }

  /* ============================================================ 历史分位条 */
  function sparkline(trail, p) {
    var pts = (trail || []).filter(function (t) { return t[1] != null; });
    if (pts.length < 2) return '<span class="sl-spark-empty">—</span>';
    var n = pts.length;
    var xy = pts.map(function (t, i) {
      return (i / (n - 1) * 46).toFixed(1) + "," + (16 - clamp(t[1], 0, 100) / 100 * 14).toFixed(1);
    }).join(" ");
    return '<svg class="sl-spark" viewBox="0 0 46 16" preserveAspectRatio="none" aria-label="近一年PB分位轨迹">' +
      '<line x1="0" y1="9" x2="46" y2="9" stroke="rgba(255,255,255,.18)" stroke-width=".7" stroke-dasharray="2 2"/>' +
      '<polyline points="' + xy + '" fill="none" stroke="' + pctColor(p) + '" stroke-width="1.4" vector-effect="non-scaling-stroke"/></svg>';
  }

  function renderRows() {
    var order = ["cyclical", "financial", "growth", "defensive"];
    var list = D.sectors.slice().sort(function (a, b) {
      if (a.group !== b.group) return order.indexOf(a.group) - order.indexOf(b.group);
      return avgPct(b) - avgPct(a);
    });
    $("sl-rows").innerHTML = list.map(function (s) {
      var pe = s.pe_pct, pb = s.pb_pct;
      var d = (pb != null && s.pb_pct_1y != null) ? pb - s.pb_pct_1y : null;
      var d20 = (pb != null && s.pb_pct_20d != null) ? pb - s.pb_pct_20d : null;
      var dt = d == null ? "" : '<span class="delta" style="color:' + (d >= 0 ? "#f06e5e" : "#2fc48c") + '">' +
        (d >= 0 ? "↑" : "↓") + Math.abs(d).toFixed(0) + '</span>';
      var d20t = d20 == null ? "" : '<span class="d20" style="color:' + (d20 >= 0 ? "#f06e5e" : "#2fc48c") + '">' +
        (d20 >= 0 ? "+" : "") + d20.toFixed(0) + '</span>';
      return '<div class="sl-row" data-id="' + esc(s.id) + '">' +
        '<span class="sl-name"><i style="background:' + color(s.id) + '"></i>' + esc(s.name) + '</span>' +
        '<span class="sl-bars">' +
          '<span class="sl-bar" title="PB 分位"><span style="width:' + (pb == null ? 0 : pb) + '%;background:' + pctColor(pb) + '"></span><i class="mid"></i></span>' +
          '<span class="sl-bar" title="PE 分位"><span style="width:' + (pe == null ? 0 : pe) + '%;background:' + pctColor(pe) + ';opacity:.62"></span><i class="mid"></i></span>' +
        '</span>' +
        '<span class="sl-spark-cell">' + sparkline(s.pb_trail, pb) + '</span>' +
        '<span class="sl-val num"><b>' + f1(pb) + '</b>%' + dt + d20t + '</span>' +
      '</div>';
    }).join("");
    Array.prototype.forEach.call($("sl-rows").querySelectorAll(".sl-row"), function (row) {
      row.onmouseenter = function () { setFocus(row.getAttribute("data-id"), null); showRowTip(row); };
      row.onmouseleave = function () { setFocus(null, null); $("sl-row-tip").className = "sl-tip"; };
      row.onclick = function () {
        var id = row.getAttribute("data-id");
        state.hidden[id] = !state.hidden[id];
        renderAll();
      };
    });
  }

  function showRowTip(row) {
    var s = sectorById(row.getAttribute("data-id"));
    if (!s) return;
    var tip = $("sl-row-tip");
    tip.innerHTML = tipHtml(s);
    tip.className = "sl-tip on";
    var r = row.getBoundingClientRect(), box = $("sl-rows-wrap").getBoundingClientRect();
    tip.style.left = Math.min(r.left - box.left + 70, Math.max(4, box.width - 262)) + "px";
    tip.style.top = (r.top - box.top + 22) + "px";
  }

  /* ============================================================ 归一化走势 */
  var TR = { W: 1080, H: 300, L: 46, R: 78, T: 14, B: 28 };
  var trendCache = null;

  function normOf(arr, start) {
    var seg = (arr || []).slice(start), base = null;
    for (var i = 0; i < seg.length; i++) if (seg[i] != null) { base = seg[i]; break; }
    return seg.map(function (v) { return (v != null && base) ? v / base * 100 : null; });
  }
  function buildTrend() {
    var dates = D.price.dates, n = dates.length;
    var cnt = { "1y": 52, "3y": 156, "5y": 260, "max": n }[state.range] || 156;
    var start = Math.max(0, n - Math.min(cnt, n));
    var ds = dates.slice(start), vis = D.sectors.filter(visible), norm = {};
    vis.forEach(function (s) { norm[s.id] = normOf(D.price.series[s.id], start); });
    var bench = D.price.benchmark ? normOf(D.price.benchmark.series, start) : null;
    var allv = [100];
    Object.keys(norm).forEach(function (id) { norm[id].forEach(function (v) { if (v != null) allv.push(v); }); });
    if (bench) bench.forEach(function (v) { if (v != null) allv.push(v); });
    var lo = Math.min.apply(null, allv), hi = Math.max.apply(null, allv);
    var pad = Math.max(6, (hi - lo) * 0.08);
    lo = Math.max(0, Math.floor((lo - pad) / 10) * 10);
    hi = Math.ceil((hi + pad) / 10) * 10;
    if (hi - lo < 30) hi = lo + 30;
    trendCache = { dates: ds, norm: norm, bench: bench, vis: vis, lo: lo, hi: hi, start: start };
    return trendCache;
  }
  function tx(i, k) { return TR.L + i * (TR.W - TR.L - TR.R) / Math.max(1, k - 1); }
  function ty(v) {
    var c = trendCache, plot = TR.H - TR.T - TR.B;
    if (state.scale === "log") {
      var lo = Math.log(Math.max(1, c.lo)), hi = Math.log(Math.max(1, c.hi));
      return TR.T + (hi - Math.log(Math.max(1, v))) / Math.max(0.0001, hi - lo) * plot;
    }
    return TR.T + (c.hi - v) / Math.max(0.0001, c.hi - c.lo) * plot;
  }
  function pathOf(arr, k) {
    var d = "", pen = false, lastV = null, lastI = -1;
    for (var i = 0; i < k; i++) {
      var v = arr[i];
      if (v == null) { pen = false; continue; }
      d += (pen ? "L" : "M") + tx(i, k).toFixed(1) + "," + ty(v).toFixed(1) + " ";
      pen = true; lastV = v; lastI = i;
    }
    return { d: d, lastV: lastV, lastI: lastI };
  }

  function renderTrend() {
    var c = buildTrend(), k = c.dates.length, o = [], W = TR.W, H = TR.H;
    var ticks = [];
    if (state.scale === "log") {
      [30, 40, 50, 60, 70, 80, 90, 100, 120, 150, 200, 250, 300, 400, 500, 700, 1000]
        .forEach(function (v) { if (v >= c.lo && v <= c.hi) ticks.push(v); });
      if (ticks.indexOf(100) < 0 && 100 >= c.lo && 100 <= c.hi) ticks.push(100);
      ticks.sort(function (a, b) { return a - b; });
    } else {
      for (var j = 0; j <= 5; j++) ticks.push(c.lo + (c.hi - c.lo) * j / 5);
    }
    ticks.forEach(function (v) {
      var y = ty(v);
      o.push('<line class="sl-grid-line" x1="' + TR.L + '" y1="' + y.toFixed(1) + '" x2="' + (W - TR.R) + '" y2="' + y.toFixed(1) + '"/>');
      o.push('<text class="sl-axis-txt" x="' + (TR.L - 7) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end">' + Math.round(v) + '</text>');
    });
    o.push('<line class="sl-baseline" x1="' + TR.L + '" y1="' + ty(100).toFixed(1) + '" x2="' + (W - TR.R) + '" y2="' + ty(100).toFixed(1) + '"/>');
    o.push('<text class="sl-axis-txt" x="' + (W - TR.R + 5) + '" y="' + (ty(100) + 3).toFixed(1) + '">100</text>');
    var nx = Math.min(6, k);
    for (var q = 0; q < nx; q++) {
      var i = Math.round(q * (k - 1) / Math.max(1, nx - 1));
      o.push('<text class="sl-axis-txt" x="' + tx(i, k).toFixed(1) + '" y="' + (H - 8) + '" text-anchor="middle">' +
        esc(String(c.dates[i]).slice(0, 7)) + '</text>');
    }
    /* 沪深300 基准线（灰白虚线，先画在底层） */
    if (c.bench) {
      var bp = pathOf(c.bench, k);
      if (bp.d) {
        o.push('<path d="' + bp.d + '" fill="none" stroke="rgba(230,238,248,.55)" stroke-width="1.6" ' +
          'stroke-dasharray="6 4" data-bench="1"/>');
      }
    }
    var labels = [];
    c.vis.forEach(function (s) {
      var p = pathOf(c.norm[s.id] || [], k);
      if (!p.d) return;
      var col = color(s.id), sw = (state.focus === s.id || state.pin === s.id) ? 2.8 : 1.6;
      var op = (state.focus || state.pin) && state.focus !== s.id && state.pin !== s.id ? 0.22 : 0.92;
      o.push('<path d="' + p.d + '" fill="none" stroke="' + col + '" stroke-width="' + sw + '" opacity="' + op +
        '" stroke-linejoin="round" stroke-linecap="round" data-line="' + esc(s.id) + '"/>');
      if (p.lastI >= 0) {
        o.push('<circle cx="' + tx(p.lastI, k).toFixed(1) + '" cy="' + ty(p.lastV).toFixed(1) + '" r="2.6" fill="' + col + '" opacity="' + op + '"/>');
        labels.push({ name: s.name, v: p.lastV, col: col, y: ty(p.lastV), x: tx(p.lastI, k) + 6 });
      }
    });
    /* 基准标签并入末端标签列 */
    if (c.bench) {
      var bb = pathOf(c.bench, k);
      if (bb.d && bb.lastI >= 0) labels.push({ name: "沪深300", v: bb.lastV, col: "rgba(230,238,248,.75)", y: ty(bb.lastV), x: tx(bb.lastI, k) + 6 });
    }
    labels.sort(function (a, b) { return a.y - b.y; });
    var minGap = 14, top = TR.T + 6, bottom = TR.H - TR.B - 3;
    var ys = labels.map(function (l) { return Math.max(top, Math.min(bottom, l.y + 3)); });
    for (var m = 1; m < ys.length; m++) if (ys[m] < ys[m - 1] + minGap) ys[m] = ys[m - 1] + minGap;
    if (ys.length && ys[ys.length - 1] > bottom) {
      var shift = ys[ys.length - 1] - bottom;
      for (var q2 = 0; q2 < ys.length; q2++) ys[q2] -= shift;
      for (var q3 = 1; q3 < ys.length; q3++) if (ys[q3] < ys[q3 - 1] + minGap) ys[q3] = ys[q3 - 1] + minGap;
    }
    labels.forEach(function (l, i2) {
      var ly = ys[i2];
      o.push('<path d="M' + l.x.toFixed(1) + ',' + l.y.toFixed(1) + ' L' + (W - TR.R + 2) + ',' + ly.toFixed(1) +
        '" fill="none" stroke="' + l.col + '" stroke-width="0.8" opacity=".38"/>');
      o.push('<text class="sl-endlabel" x="' + (W - TR.R + 6) + '" y="' + (ly + 3).toFixed(1) + '" fill="' + l.col + '">' + esc(l.name) + '</text>');
    });
    o.push('<line id="sl-crossline" class="sl-cross" x1="0" y1="' + TR.T + '" x2="0" y2="' + (H - TR.B) + '" style="display:none"/>');
    $("sl-trend").innerHTML = o.join("");
    bindTrendHover();
  }

  function bindTrendHover() {
    var svg = $("sl-trend"), wrap = $("sl-trend-wrap"), tip = $("sl-trend-tip");
    svg.onmousemove = function (e) {
      var box = svg.getBoundingClientRect(), scale = TR.W / box.width;
      var mx = (e.clientX - box.left) * scale, k = trendCache.dates.length;
      var idx = Math.round((mx - TR.L) / ((TR.W - TR.L - TR.R) / Math.max(1, k - 1)));
      idx = Math.max(0, Math.min(k - 1, idx));
      var line = $("sl-crossline");
      if (line) { line.setAttribute("x1", tx(idx, k)); line.setAttribute("x2", tx(idx, k)); line.style.display = ""; }
      var rows = trendCache.vis.map(function (s) { return { s: s, v: (trendCache.norm[s.id] || [])[idx] }; })
        .filter(function (r) { return r.v != null; }).sort(function (a, b) { return b.v - a.v; });
      var html = '<div class="t">' + esc(trendCache.dates[idx]) + ' · 归一化（区间起点=100）</div><table>';
      rows.forEach(function (r) {
        html += '<tr><td><span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:' +
          color(r.s.id) + ';margin-right:5px"></span>' + esc(r.s.name) + '</td><td class="v">' + r.v.toFixed(1) + '</td></tr>';
      });
      if (trendCache.bench && trendCache.bench[idx] != null) {
        html += '<tr><td><span style="display:inline-block;width:7px;height:2px;background:rgba(230,238,248,.7);margin-right:5px"></span>沪深300</td><td class="v">' + trendCache.bench[idx].toFixed(1) + '</td></tr>';
      }
      html += '</table>';
      tip.innerHTML = html; tip.className = "sl-tip on";
      var wbox = wrap.getBoundingClientRect();
      var lx = e.clientX - wbox.left + 14, ly = e.clientY - wbox.top + 12;
      if (lx + 200 > wbox.width) lx = Math.max(4, lx - 214);
      tip.style.left = lx + "px"; tip.style.top = Math.min(ly, wbox.height - 170) + "px";
    };
    svg.onmouseleave = function () {
      tip.className = "sl-tip";
      var line = $("sl-crossline"); if (line) line.style.display = "none";
    };
  }

  /* ============================================================ 强弱矩阵 */
  var MATRIX_COLS = [
    { k: "name", t: "板块", txt: true },
    { k: "live", t: "当日", tip: "实时/最近交易日涨跌" },
    { k: "ret_1m", t: "近1月" }, { k: "ret_3m", t: "近3月" },
    { k: "ret_6m", t: "近6月" }, { k: "ret_1y", t: "近1年" },
    { k: "rs_3m", t: "3月超额", tip: "相对沪深300ETF" },
    { k: "pb_pct", t: "PB分位" },
    { k: "pb20", t: "20日分位变化", tip: "当前 PB 分位 − 20 交易日前" }
  ];
  function matVal(s, k) {
    if (k === "name") return s.name;
    if (k === "live") return LIVE && LIVE.quotes[s.id] ? LIVE.quotes[s.id].pct : null;
    if (k === "pb20") return (s.pb_pct != null && s.pb_pct_20d != null) ? s.pb_pct - s.pb_pct_20d : null;
    return s[k];
  }
  function renderMatrix() {
    var el = $("sl-matrix");
    if (!el) return;
    var key = state.sortKey, desc = state.sortDesc;
    var list = D.sectors.slice().sort(function (a, b) {
      var x = matVal(a, key), y = matVal(b, key);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      if (key === "name") return desc ? String(y).localeCompare(String(x), "zh") : String(x).localeCompare(String(y), "zh");
      return desc ? y - x : x - y;
    });
    var h = '<thead><tr>' + MATRIX_COLS.map(function (c) {
      var on = key === c.k;
      return '<th class="' + (c.txt ? "txt" : "") + (on ? " sorted" : "") + '" data-k="' + c.k + '"' +
        (c.tip ? ' title="' + esc(c.tip) + '"' : '') + '>' + c.t + (on ? (desc ? " ↓" : " ↑") : "") + '</th>';
    }).join("") + '</tr></thead><tbody>';
    list.forEach(function (s) {
      h += '<tr data-id="' + esc(s.id) + '"><td class="txt"><i style="background:' + color(s.id) + '"></i>' + esc(s.name) + '</td>';
      MATRIX_COLS.slice(1).forEach(function (c) {
        var v = matVal(s, c.k);
        if (c.k === "pb_pct") {
          h += '<td style="color:' + pctColor(v) + '">' + f1(v) + (v == null ? "" : "%") + '</td>';
        } else {
          h += '<td style="background:' + retColor(v, true) + ';color:' + retColor(v) + '">' +
            (v == null ? "—" : sign(v, c.k === "pb20" ? "" : "%")) + '</td>';
        }
      });
      h += '</tr>';
    });
    var b = D.benchmark_ret || {};
    h += '<tr class="sl-mat-bench"><td class="txt">沪深300</td>';
    MATRIX_COLS.slice(1).forEach(function (c) {
      var map = { live: null, ret_1m: b.ret_1m, ret_3m: b.ret_3m, ret_6m: b.ret_6m,
                  ret_1y: b.ret_1y, rs_3m: 0, pb_pct: null, pb20: null };
      var v = map[c.k];
      h += '<td style="color:' + retColor(v) + '">' + (v == null ? "—" : sign(v, c.k === "rs_3m" ? "%" : "%")) + '</td>';
    });
    h += '</tr></tbody>';
    el.innerHTML = h;
    Array.prototype.forEach.call(el.querySelectorAll("th[data-k]"), function (th) {
      th.onclick = function () {
        var k = th.getAttribute("data-k");
        if (state.sortKey === k) state.sortDesc = !state.sortDesc;
        else { state.sortKey = k; state.sortDesc = k !== "name"; }
        renderMatrix();
      };
    });
    Array.prototype.forEach.call(el.querySelectorAll("tbody tr[data-id]"), function (tr) {
      tr.onmouseenter = function () { setFocus(tr.getAttribute("data-id"), null); };
      tr.onmouseleave = function () { setFocus(null, null); };
      tr.onclick = function () { state.pin = tr.getAttribute("data-id"); lastInterpKey = ""; renderInterp(state.pin, null); };
    });
  }

  /* ============================================================ 动态解读 */
  var lastInterpKey = "";
  function deltaWord(v, unit) {
    if (v == null) return "缺少历史对照";
    if (v > 5) return "明显抬升（+" + v.toFixed(0) + (unit || "pct") + "）";
    if (v > 1) return "小幅抬升（+" + v.toFixed(0) + (unit || "pct") + "）";
    if (v < -5) return "明显消化（" + v.toFixed(0) + (unit || "pct") + "）";
    if (v < -1) return "小幅消化（" + v.toFixed(0) + (unit || "pct") + "）";
    return "基本走平（" + (v >= 0 ? "+" : "") + v.toFixed(0) + (unit || "pct") + "）";
  }
  function quadOf(s, mode) {
    var q;
    if (mode === "momentum") q = (s.pb_pct < 50 ? 0 : 1) + (s.rs_3m < 0 ? 2 : 0);
    else q = (s.pe_pct < 50 ? 0 : 1) + (s.pb_pct < 50 ? 2 : 0);
    return { i: q, r: QUAD_RULES[mode][q] };
  }
  function interpDefault(quadIdx) {
    var rules = QUAD_RULES[state.coord];
    var cards = rules.map(function (r, i) {
      var members = D.sectors.filter(function (s) { return quadOf(s, state.coord).i === i; });
      return '<div class="sl-icard' + (quadIdx === i ? " active" : "") + '">' +
        '<div class="h"><b>' + esc(r.t) + '</b><span>' + esc(r.s) + '</span></div>' +
        '<p>' + esc(r.rule) + '</p>' +
        '<div class="mem">' + (members.length
          ? members.map(function (s) { return '<span style="color:' + color(s.id) + '">' + esc(s.name) + '</span>'; }).join("")
          : '<span style="color:var(--text-faint)">当前无板块</span>') + '</div></div>';
    }).join("");
    return '<div class="sl-interp-head"><h4>四象限怎么读 · 动态解读</h4>' +
      '<span class="sl-interp-hint">把鼠标移到散点 / 分位条 / 行情块 / 矩阵行上，或点击散点固定，即可看到该板块逐项解读</span></div>' +
      '<div class="sl-interp-note">' + (state.coord === "valuation"
        ? '横轴 PE 分位、纵轴 PB 分位，金色虚线为 50%。<b>为什么周期股要反着看</b>：PE = 价格 / 盈利，周期股盈利在谷底时 E 被压缩甚至为负，PE 被动抬高甚至失效；PB 的分母是净资产，波动小得多。所以「右下（PB低 / PE高）」往往是资源股的低位区，而「左上（PB高 / PE低）」常是盈利峰值。'
        : '横轴 PB 分位代表便宜程度，纵轴为近 3 月相对沪深300ETF 的超额收益。把「估值」与「价格是否已经开始反应」放在一起，可以区分「低估且正在修复」与「低估但没人要」——这两者在只估值坐标里看起来几乎一样。') +
      '</div><div class="sl-icards">' + cards + '</div>';
  }

  function interpSector(s) {
    var g = groupOf(s.group), qv = quadOf(s, "valuation"), qm = quadOf(s, "momentum");
    var gap = s.pe_pb_gap, m = D.market || {}, b = D.benchmark_ret || {};
    var d20 = (s.pb_pct != null && s.pb_pct_20d != null) ? s.pb_pct - s.pb_pct_20d : null;
    var d60 = (s.pb_pct != null && s.pb_pct_60d != null) ? s.pb_pct - s.pb_pct_60d : null;
    var d1y = (s.pb_pct != null && s.pb_pct_1y != null) ? s.pb_pct - s.pb_pct_1y : null;
    var peMed = s.pe_stat ? s.pe_stat.median : null, pbMed = s.pb_stat ? s.pb_stat.median : null;
    var peDev = (s.pe && peMed) ? (s.pe / peMed - 1) * 100 : null;
    var pbDev = (s.pb && pbMed) ? (s.pb / pbMed - 1) * 100 : null;

    /* ① 位置 */
    var sec = '<div class="sl-isec"><span class="no">01</span><div class="bd"><h5>它现在站在哪里</h5>' +
      '<p><b style="color:' + color(s.id) + '">' + esc(s.name) + '</b> 在 PE×PB 坐标中位于 <b>【' + esc(qv.r.t) + ' · ' + esc(qv.r.s) + '】</b>：' +
      'PE 历史分位 <b>' + f1(s.pe_pct) + '%</b>、PB 历史分位 <b>' + f1(s.pb_pct) + '%</b>' +
      (m.temp != null ? '，全市场 PB 分位中位数为 ' + f1(m.temp) + '%（' + tempText(m.temp) + '）' : '') + '。' +
      '换成估值×动量坐标，它落在 <b>【' + esc(qm.r.t) + ' · ' + esc(qm.r.s) + '】</b>。</p></div></div>';

    /* ② 象限规则 + 属性修正 */
    sec += '<div class="sl-isec"><span class="no">02</span><div class="bd"><h5>这个位置通常意味着什么</h5>' +
      '<p>' + esc(qv.r.rule) + '</p>' +
      '<p class="sl-igroup" style="border-left-color:' + g.color + '"><b style="color:' + g.color + '">' + esc(g.name) + '属性修正：</b>' + esc(g.logic) + '</p>' +
      '<p class="sl-igroup" style="border-left-color:#8b98a9"><b>动量坐标补充：</b>' + esc(qm.r.rule) + '</p></div></div>';

    /* ③ 背离与盈利状态 */
    var gapTxt;
    if (gap == null) gapTxt = "缺少有效的 PE / PB 分位对照。";
    else if (gap >= 25) gapTxt = 'PB 分位比 PE 分位高 <b>' + gap.toFixed(0) + ' 个百分点</b>：资产端定价明显贵于盈利端读数，常见解释是盈利处在高位把 PE 压低（周期峰值），或账面资产被重估。此时「PE 看着便宜」最需要警惕。';
    else if (gap <= -25) gapTxt = 'PE 分位比 PB 分位高 <b>' + Math.abs(gap).toFixed(0) + ' 个百分点</b>：盈利端读数贵、资产端仍在低位，典型成因是盈利被压缩或亏损使 PE 被动抬高。对周期股这常指向盈利谷底；对成长股则要先排除轻资产导致的 PB 天然偏高。';
    else gapTxt = 'PE 与 PB 分位方向基本一致（背离 ' + sign(gap, "pct") + '），两套估值口径没有明显冲突，分位读数的可信度相对更高。';
    var lossTxt = s.loss_ratio == null ? "" :
      '该申万行业 <b>' + (s.members == null ? "—" : s.members) + '</b> 家成分中 <b>' + f1(s.loss_ratio) + '%</b> 处于亏损' +
      (s.loss_ratio >= 25 ? '，比例偏高，PE 的代表性被削弱，应更多参考 PB / PS / 现金流估值。' : '，PE 仍具备一定代表性。');
    var trendTxt = s.earnings_trend == null ? "" :
      ' PE(TTM) 相对静态 PE ' + (s.earnings_trend < 0 ? '低 ' + Math.abs(s.earnings_trend).toFixed(1) + '%，说明近 12 个月盈利较上年改善' :
        '高 ' + s.earnings_trend.toFixed(1) + '%，说明近 12 个月盈利较上年走弱') + '。';
    sec += '<div class="sl-isec"><span class="no">03</span><div class="bd"><h5>PE 与 PB 为什么不一致 · 盈利状态</h5>' +
      '<p>' + gapTxt + '</p><p>' + lossTxt + trendTxt + '</p></div></div>';

    /* ④ 分位动量 */
    sec += '<div class="sl-isec"><span class="no">04</span><div class="bd"><h5>这个位置是怎么来的 · 分位轨迹</h5>' +
      '<div class="sl-itrend">' + sparkline(s.pb_trail, s.pb_pct) + '<span>近 1 年 PB 分位轨迹（虚线 = 50%）</span></div>' +
      '<p>近 20 交易日 PB 分位 <b>' + deltaWord(d20) + '</b>，近 60 交易日 <b>' + deltaWord(d60) + '</b>，近 1 年 <b>' + deltaWord(d1y) + '</b>。' +
      '分位下行可能是价格下跌，也可能是盈利 / 净资产上升把估值消化掉，两者含义完全不同。</p></div></div>';

    /* ⑤ 价格动量 */
    var rsTxt = s.rs_3m == null ? "缺少相对收益数据。" :
      (s.rs_3m > 0
        ? '近 3 月跑赢基准 <b style="color:#f06e5e">' + Math.abs(s.rs_3m).toFixed(1) + 'pct</b>，' +
          (s.pb_pct < 50 ? '属于「低估值 + 强超额」，是估值修复的常见起点。' : '属于「高估值 + 强超额」，趋势占优但赔率下降。')
        : '近 3 月跑输基准 <b style="color:#2fc48c">' + Math.abs(s.rs_3m).toFixed(1) + 'pct</b>，' +
          (s.pb_pct < 50 ? '属于「低估值 + 弱超额」，便宜但市场尚未认可，需要基本面拐点证据。' : '属于「高估值 + 弱超额」，多为预期见顶后的估值消化阶段。'));
    sec += '<div class="sl-isec"><span class="no">05</span><div class="bd"><h5>价格有没有开始反应</h5>' +
      '<p>近 1 月 <b style="color:' + retColor(s.ret_1m) + '">' + sign(s.ret_1m, "%") + '</b>、近 3 月 <b style="color:' + retColor(s.ret_3m) + '">' + sign(s.ret_3m, "%") +
      '</b>、近 6 月 <b style="color:' + retColor(s.ret_6m) + '">' + sign(s.ret_6m, "%") + '</b>、近 1 年 <b style="color:' + retColor(s.ret_1y) + '">' + sign(s.ret_1y, "%") + '</b>；' +
      '同期沪深300ETF 近 3 月 ' + sign(b.ret_3m, "%") + '。' + rsTxt + '</p></div></div>';

    /* ⑥ 数值锚 */
    sec += '<div class="sl-isec"><span class="no">06</span><div class="bd"><h5>把分位还原成绝对数值</h5>' +
      '<div class="sl-ifacts">' +
      '<span>PE(TTM) <b>' + f2(s.pe) + '</b><i>历史中位 ' + f2(peMed) + (peDev == null ? "" : ' · 偏离 ' + sign(peDev, "%")) + '</i></span>' +
      '<span>PB(MRQ) <b>' + f2(s.pb) + '</b><i>历史中位 ' + f2(pbMed) + (pbDev == null ? "" : ' · 偏离 ' + sign(pbDev, "%")) + '</i></span>' +
      '<span>PEG <b>' + f2(s.peg) + '</b><i>数据源原始值</i></span>' +
      '<span>PS(TTM) <b>' + f2(s.ps) + '</b><i>市销率</i></span>' +
      '<span>PCF <b>' + f2(s.pcf) + '</b><i>市现率</i></span>' +
      '<span>总市值 <b>' + (s.cap_yi == null ? "—" : Math.round(s.cap_yi).toLocaleString() + " 亿") + '</b><i>' + (s.members == null ? "—" : s.members + " 家成分") + '</i></span>' +
      '</div><p class="sl-ifoot">估值样本 ' + (s.samples || 0) + ' 个交易日，窗口自 ' + esc(s.window_start || "—") +
      '；估值口径「' + esc(s.board) + '」，走势口径「' + esc(s.proxy) + '」，两者不是同一批资产。</p></div></div>';

    /* ⑦ 验证清单 */
    var checks = (g.checks || []).map(function (c) { return '<li>' + esc(c) + '</li>'; }).join("");
    sec += '<div class="sl-isec"><span class="no">07</span><div class="bd"><h5>要证实 / 证伪，接下来该看什么</h5>' +
      '<ul class="sl-ichecks">' + checks + '</ul>' +
      '<p class="sl-iwarn">分位是「相对自己历史」的统计量，不预测涨跌：低估不等于会补涨，高估也不等于会下跌；样本窗口、成分变更、会计口径变化都会影响读数。本段文字由当日数据自动生成，仅供研究参考，<b>不构成投资建议</b>。</p></div></div>';

    return '<div class="sl-interp-head"><h4><span style="color:' + color(s.id) + '">●</span> ' + esc(s.name) +
      ' <span class="sl-ipill" style="border-color:' + g.color + '66;color:' + g.color + '">' + esc(g.name) + '</span></h4>' +
      '<span class="sl-interp-hint">坐标 ' + (state.coord === "valuation" ? "PE分位 × PB分位" : "PB分位 × 3月超额") +
      ' · 数据 ' + esc(s.as_of || D.as_of) + '</span>' +
      (state.pin === s.id ? '<button class="sl-unpin" id="sl-unpin">取消固定 ✕</button>' : '<span class="sl-interp-hint">点击散点可固定</span>') +
      '</div><div class="sl-ibody">' + sec + '</div>';
  }

  function renderInterp(id, quad) {
    var el = $("sl-interp");
    if (!el || !D) return;
    var key = (id || "") + "|" + (quad == null ? "" : quad) + "|" + state.coord + "|" + (state.pin || "");
    if (key === lastInterpKey) return;
    lastInterpKey = key;
    if (id) {
      var s = sectorById(id);
      el.innerHTML = s ? interpSector(s) : interpDefault(quad);
    } else {
      el.innerHTML = interpDefault(quad);
    }
    var btn = $("sl-unpin");
    if (btn) btn.onclick = function () { state.pin = null; lastInterpKey = ""; renderAll(); };
  }

  /* ============================================================ 图例 / 控件 */
  function renderSeriesLegend() {
    $("sl-slegend").innerHTML = D.sectors.map(function (s) {
      return '<span class="sl-sitem' + (state.hidden[s.id] ? " off" : "") + '" data-id="' + esc(s.id) + '">' +
        '<i style="background:' + color(s.id) + '"></i>' + esc(s.name) + '</span>';
    }).join("") + '<span class="sl-sitem sl-bench-key"><i style="background:rgba(230,238,248,.7)"></i>沪深300（虚线）</span>';
    Array.prototype.forEach.call($("sl-slegend").querySelectorAll(".sl-sitem[data-id]"), function (el) {
      el.onmouseenter = function () { setFocus(el.getAttribute("data-id"), null); };
      el.onmouseleave = function () { setFocus(null, null); };
      el.onclick = function () {
        var id = el.getAttribute("data-id");
        state.hidden[id] = !state.hidden[id];
        renderAll();
      };
    });
  }
  function renderGroupChips() {
    var chips = [{ id: "all", name: "全部", color: "#8b98a9" }].concat(D.groups.map(function (g) {
      return { id: g.id, name: g.name, color: g.color };
    }));
    $("sl-gchips").innerHTML = chips.map(function (g) {
      var on = state.group === g.id;
      return '<button class="sl-gchip' + (on ? " on" : "") + '" data-g="' + g.id + '"' +
        (on && g.id !== "all" ? ' style="background:' + g.color + ';border-color:' + g.color + '"' : "") +
        (on && g.id === "all" ? ' style="background:rgba(255,255,255,.14);border-color:rgba(255,255,255,.2);color:var(--text)"' : "") +
        '>' + esc(g.name) + '</button>';
    }).join("");
    Array.prototype.forEach.call($("sl-gchips").querySelectorAll(".sl-gchip"), function (el) {
      el.onclick = function () {
        state.group = (state.group === el.getAttribute("data-g")) ? "all" : el.getAttribute("data-g");
        state.clockGroups = null; renderAll();
      };
    });
  }
  function renderTabs() {
    Array.prototype.forEach.call($("sl-tabs").querySelectorAll(".sl-tab"), function (el) {
      el.className = "sl-tab" + (el.getAttribute("data-r") === state.range ? " on" : "");
      el.onclick = function () { state.range = el.getAttribute("data-r"); renderAll(); };
    });
    Array.prototype.forEach.call($("sl-scale-tabs").querySelectorAll(".sl-tab"), function (el) {
      el.className = "sl-tab" + (el.getAttribute("data-s") === state.scale ? " on" : "");
      el.onclick = function () { state.scale = el.getAttribute("data-s"); renderAll(); };
    });
  }

  /* ============================================================ 美林时钟 */
  function renderClock() {
    var svg = $("sl-clock"), cx = 100, cy = 92, r = 68, o = [];
    var Q = [
      { name: "过热", a: 0, b: 90, col: "rgba(240,74,61,.13)", tx: 142, ty: 56, groups: CLOCK_GROUPS["过热"] },
      { name: "滞胀", a: 90, b: 180, col: "rgba(217,178,60,.13)", tx: 58, ty: 56, groups: CLOCK_GROUPS["滞胀"] },
      { name: "衰退", a: 180, b: 270, col: "rgba(79,156,249,.13)", tx: 58, ty: 132, groups: CLOCK_GROUPS["衰退"] },
      { name: "复苏", a: 270, b: 360, col: "rgba(67,214,156,.13)", tx: 142, ty: 132, groups: CLOCK_GROUPS["复苏"] }
    ];
    function arc(a0, a1) {
      var p0 = [cx + r * Math.cos(a0 * Math.PI / 180), cy - r * Math.sin(a0 * Math.PI / 180)];
      var p1 = [cx + r * Math.cos(a1 * Math.PI / 180), cy - r * Math.sin(a1 * Math.PI / 180)];
      return "M" + cx + "," + cy + " L" + p0[0].toFixed(1) + "," + p0[1].toFixed(1) +
        " A" + r + "," + r + " 0 0 0 " + p1[0].toFixed(1) + "," + p1[1].toFixed(1) + " Z";
    }
    Q.forEach(function (q) {
      var dim = state.clockGroups && q.groups.join(",") !== state.clockGroups.join(",");
      o.push('<g class="sl-clock-q' + (dim ? " dim" : "") + '" data-clock="' + q.name + '">' +
        '<path d="' + arc(q.a, q.b) + '" fill="' + q.col + '" stroke="rgba(255,255,255,.12)" stroke-width="1"/>' +
        '<text x="' + q.tx + '" y="' + q.ty + '" text-anchor="middle" font-weight="700">' + q.name + '</text></g>');
    });
    o.push('<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="rgba(255,255,255,.18)"/>');
    o.push('<line x1="' + (cx - r) + '" y1="' + cy + '" x2="' + (cx + r) + '" y2="' + cy + '" stroke="rgba(255,255,255,.18)"/>');
    o.push('<line x1="' + cx + '" y1="' + (cy - r) + '" x2="' + cx + '" y2="' + (cy + r) + '" stroke="rgba(255,255,255,.18)"/>');
    o.push('<text x="' + cx + '" y="' + (cy - r - 9) + '" text-anchor="middle" font-size="9" fill="#59647a">通胀 ↑</text>');
    o.push('<text x="' + (cx + r + 8) + '" y="' + (cy + 3) + '" text-anchor="start" font-size="9" fill="#59647a">增长 →</text>');
    o.push('<text x="' + cx + '" y="182" text-anchor="middle" font-size="9" fill="#59647a">点击象限筛选对应板块</text>');
    svg.innerHTML = o.join("");
    Array.prototype.forEach.call(svg.querySelectorAll(".sl-clock-q"), function (g) {
      g.onclick = function () {
        var key = g.getAttribute("data-clock"), groups = CLOCK_GROUPS[key];
        state.clockGroups = (state.clockGroups && state.clockGroups.join(",") === groups.join(",")) ? null : groups;
        state.group = "all"; renderAll();
      };
    });
  }

  /* ============================================================ 读图说明 */
  function renderRead() {
    var rules = QUAD_RULES.valuation;
    var qs = ["左上", "右上", "左下", "右下"].map(function (pos, i) {
      return '<div class="sl-read-item"><span class="q">' + pos + '</span><b>' + esc(rules[i].t) + ' · ' + esc(rules[i].s) + '</b>：' + esc(rules[i].rule) + '</div>';
    }).join("");
    $("sl-read-grid").innerHTML = qs + D.groups.map(function (g) {
      return '<div class="sl-read-item"><span class="q" style="color:' + g.color + ';border-color:' + g.color + '55">' +
        esc(g.name) + '</span>' + esc(g.logic) + '</div>';
    }).join("");
    var r = D.refresh || {};
    $("sl-disc").innerHTML = esc(D.disclaimer) +
      '<div class="sl-refresh-note"><b>更新机制</b>：' + esc(r.valuation || "估值分位日更") + '；' + esc(r.quote || "行情盘中刷新") +
      '。估值分位是日频报表，盘中不会变化，因此不做无意义的重复重算；价格与涨跌幅才是需要高频更新的部分。</div>' +
      '<br>口径：' + esc(D.method) +
      '<br>数据源：' + esc(D.valuation_source) + '；' + esc(D.price_source) + '；' + esc(D.quote_source || "") +
      ' · 生成于 ' + esc(D.generated_at);
  }

  /* ============================================================ 联动高亮 */
  function applyHighlight() {
    var id = state.focus || state.pin;
    Array.prototype.forEach.call(document.querySelectorAll("#sl-quad .sl-dot"), function (g) {
      g.classList.toggle("hot", id && g.getAttribute("data-id") === id);
      g.classList.toggle("dim", !!id && g.getAttribute("data-id") !== id);
    });
    Array.prototype.forEach.call(document.querySelectorAll("#sl-rows .sl-row"), function (r) {
      r.classList.toggle("dim", !!id && r.getAttribute("data-id") !== id);
    });
    Array.prototype.forEach.call(document.querySelectorAll("#sl-trend [data-line]"), function (p) {
      var on = p.getAttribute("data-line") === id;
      p.setAttribute("stroke-width", id ? (on ? 2.8 : 1.2) : 1.6);
      p.setAttribute("opacity", id ? (on ? 1 : 0.18) : 0.92);
    });
    Array.prototype.forEach.call(document.querySelectorAll("#sl-live .sl-tile"), function (t) {
      t.classList.toggle("dim", !!id && t.getAttribute("data-id") !== id);
    });
    Array.prototype.forEach.call(document.querySelectorAll("#sl-matrix tbody tr[data-id]"), function (t) {
      t.classList.toggle("dim", !!id && t.getAttribute("data-id") !== id);
    });
  }
  function setFocus(id, quad) {
    if (state.focus !== id) {
      state.focus = id;
      applyHighlight();
    }
    renderInterp(id || state.pin, quad);
  }

  /* ============================================================ 细分行业全览 */
  /* 用户搜的是「机器人」这类赛道词，而申万二级只有「自动化设备」这种学名。
     不做别名映射，搜索会直接落空 —— 所以这里把常见赛道词反查到承载它的二级行业。 */
  var BD_ALIAS = {
    "机器人": ["自动化设备", "通用设备"],
    "人形机器人": ["自动化设备", "通用设备"],
    "减速器": ["通用设备"],
    "丝杠": ["通用设备"],
    "灵巧手": ["自动化设备"],
    "工业母机": ["通用设备"],
    "算力": ["通信设备", "计算机设备"],
    "光模块": ["通信设备"],
    "液冷": ["通信设备"],
    "芯片": ["半导体"],
    "存储": ["半导体"],
    "半导体设备": ["专用设备"],
    "锂电": ["电池"],
    "固态电池": ["电池"],
    "储能": ["电池", "其他电源设备Ⅱ"],
    "光伏": ["光伏设备"],
    "风电": ["风电设备"],
    "特高压": ["电网设备"],
    "核电": ["电力", "其他电源设备Ⅱ"],
    "军工": ["航空装备Ⅱ", "航天装备Ⅱ", "军工电子Ⅱ", "地面兵装Ⅱ", "航海装备Ⅱ"],
    "白酒": ["白酒Ⅱ"],
    "券商": ["证券Ⅱ"],
    "保险": ["保险Ⅱ"],
    "银行": ["银行Ⅱ"],
    "创新药": ["化学制药", "生物制品"],
    "中药": ["中药Ⅱ"],
    "CXO": ["医疗服务"],
    "医美": ["医疗美容"],
    "宠物": ["动物保健Ⅱ"],
    "稀土": ["小金属"],
    "黄金": ["贵金属"],
    "铜": ["工业金属"],
    "锂矿": ["能源金属"],
    "煤炭": ["煤炭开采", "焦炭Ⅱ"],
    "石油": ["油气开采Ⅱ", "炼化及贸易"],
    "地产": ["房地产开发"],
    "基建": ["基础建设"],
    "游戏": ["游戏Ⅱ"],
    "影视": ["影视院线"],
    "短剧": ["影视院线", "数字媒体"],
    "消费电子": ["消费电子"],
    "家电": ["白色家电", "黑色家电", "小家电"],
    "汽车": ["乘用车", "商用车", "汽车零部件"],
    "白酒Ⅱ": ["白酒Ⅱ"]
  };

  var BD_ALIAS_OF = (function () {
    var m = {};
    for (var k in BD_ALIAS) {
      if (!Object.prototype.hasOwnProperty.call(BD_ALIAS, k)) continue;
      BD_ALIAS[k].forEach(function (n) {
        if (!m[n]) m[n] = [];
        if (m[n].indexOf(k) < 0) m[n].push(k);
      });
    }
    return m;
  })();

  var BD = { q: "", style: "", sort: "pb_pct", dir: 1 };

  function bdChg20(b) {
    if (b.pb_pct == null || b.pb_pct_20d == null) return null;
    return Math.round((b.pb_pct - b.pb_pct_20d) * 10) / 10;
  }

  function bdPctColor(p) {
    // 估值分位用「青蓝→灰→橙」色阶，刻意避开红绿，避免被读成涨跌
    if (p == null) return "var(--text-faint)";
    if (p >= 90) return "#e8622c";
    if (p >= 70) return "#e8a33d";
    if (p <= 10) return "#3f9edb";
    if (p <= 30) return "#5ea8c9";
    return "var(--text-dim)";
  }

  function bdCap(v) {
    if (v == null) return "—";
    if (v >= 10000) return (v / 10000).toFixed(1) + "万亿";
    return Math.round(v).toLocaleString("zh-CN");
  }

  function bdRows() {
    var list = (D && D.boards) || [];
    var q = BD.q.trim().toLowerCase();
    var out = [];
    for (var i = 0; i < list.length; i++) {
      var b = list[i];
      if (BD.style && b.style !== BD.style) continue;
      if (q) {
        var hay = b.name + " " + (b.sw1 || "") + " " +
                  ((BD_ALIAS_OF[b.name] || []).join(" "));
        if (hay.toLowerCase().indexOf(q) < 0) continue;
      }
      out.push(b);
    }
    var k = BD.sort, dir = BD.dir;
    out.sort(function (a, b) {
      var va, vb;
      if (k === "chg20") { va = bdChg20(a); vb = bdChg20(b); }
      else if (k === "name") { va = a.name; vb = b.name; }
      else { va = a[k]; vb = b[k]; }
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "string") return va.localeCompare(vb) * dir;
      return (va - vb) * dir;
    });
    return out;
  }

  function renderBoards() {
    var tbl = $("bd-table");
    if (!tbl) return;
    var all = (D && D.boards) || [];

    if (!all.length) {
      tbl.innerHTML = '<tbody><tr><td class="bd-empty">细分行业数据未生成 · 运行 ' +
        'scripts/update_sector_lab.py 后刷新</td></tr></tbody>';
      if ($("bd-count")) $("bd-count").textContent = "数据待生成";
      return;
    }
    if ($("bd-count")) {
      $("bd-count").textContent = all.length + " 个二级行业";
    }

    var rows = bdRows();
    var cols = [
      ["name", "板块 / 一级行业", "txt"],
      ["pe", "PE", ""],
      ["pe_pct", "PE分位", ""],
      ["pb", "PB", ""],
      ["pb_pct", "PB分位", ""],
      ["chg20", "20日分位变动", ""],
      ["loss_ratio", "亏损占比", ""],
      ["cap_yi", "市值", ""]
    ];
    var head = "<thead><tr>";
    cols.forEach(function (c) {
      head += '<th class="' + c[2] + (BD.sort === c[0] ? " sorted" : "") +
              '" data-k="' + c[0] + '">' + c[1] + "</th>";
    });
    head += "</tr></thead>";

    var body = "<tbody>";
    if (!rows.length) {
      body += '<tr><td class="bd-empty" colspan="8">没有匹配「' +
              esc(BD.q) + "」的板块，试试：机器人 / 光模块 / 白酒 / 储能</td></tr>";
    }
    for (var i = 0; i < rows.length; i++) {
      var b = rows[i];
      var c20 = bdChg20(b);
      var c20txt = c20 == null ? "—" : (c20 > 0 ? "+" : "") + c20.toFixed(1);
      var c20col = c20 == null ? "var(--text-faint)"
                 : (c20 > 0 ? "#e8a33d" : c20 < 0 ? "#5ea8c9" : "var(--text-faint)");
      var alias = (BD_ALIAS_OF[b.name] || []).slice(0, 2).join(" / ");
      body += "<tr>" +
        '<td class="txt bd-name"><b>' + esc(b.name) + "</b> " +
          '<span class="bd-sw1">' + esc(b.sw1 || "") +
          (alias ? " · " + esc(alias) : "") + "</span></td>" +
        "<td>" + f2(b.pe) + "</td>" +
        '<td><span class="bd-bar"><i style="width:' + clamp(b.pe_pct || 0, 0, 100) +
          "%;background:" + bdPctColor(b.pe_pct) + '"></i></span>' +
          '<span style="color:' + bdPctColor(b.pe_pct) + '">' +
          (b.pe_pct == null ? "—" : f1(b.pe_pct)) + "</span></td>" +
        "<td>" + f2(b.pb) + "</td>" +
        '<td><span class="bd-bar"><i style="width:' + clamp(b.pb_pct || 0, 0, 100) +
          "%;background:" + bdPctColor(b.pb_pct) + '"></i></span>' +
          '<span style="color:' + bdPctColor(b.pb_pct) + '">' +
          (b.pb_pct == null ? "—" : f1(b.pb_pct)) + "</span></td>" +
        '<td style="color:' + c20col + '">' + c20txt + "</td>" +
        '<td style="color:' + (b.loss_ratio >= 30 ? "#e8a33d" : "var(--text-dim)") + '">' +
          (b.loss_ratio == null ? "—" : f1(b.loss_ratio) + "%") + "</td>" +
        "<td>" + bdCap(b.cap_yi) + "</td>" +
        "</tr>";
    }
    body += "</tbody>";
    tbl.innerHTML = head + body;

    tbl.onclick = function (e) {
      var th = e.target;
      while (th && th.tagName !== "TH") th = th.parentNode;
      if (!th || !th.getAttribute("data-k")) return;
      var k = th.getAttribute("data-k");
      if (BD.sort === k) BD.dir = -BD.dir;
      else { BD.sort = k; BD.dir = 1; }
      renderBoards();
    };
  }

  function bindBoardsUI() {
    var s = $("bd-search");
    if (s && !s.dataset.bound) {
      s.dataset.bound = "1";
      s.addEventListener("input", function () { BD.q = this.value || ""; renderBoards(); });
    }
    var st = $("bd-style-tabs");
    if (st && !st.dataset.bound) {
      st.dataset.bound = "1";
      st.addEventListener("click", function (e) {
        var b = e.target;
        if (b.tagName !== "BUTTON") return;
        BD.style = b.getAttribute("data-s") || "";
        var kids = st.querySelectorAll(".sl-tab");
        for (var i = 0; i < kids.length; i++) kids[i].classList.remove("on");
        b.classList.add("on");
        renderBoards();
      });
    }
  }

  function renderAll() {
    lastInterpKey = "";
    renderTabs(); renderCoordTabs(); renderGroupChips();
    renderQuadrant(); renderRows(); renderTrend(); renderSeriesLegend(); renderClock();
    renderMatrix(); renderStatus(); renderLiveStrip();
    bindBoardsUI(); renderBoards();
    renderInterp(state.focus || state.pin, null);
    applyHighlight();
  }

  /* ============================================================ 启动 */
  fetch("data/sector_lab.json?t=" + Date.now(), { cache: "no-store" })
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(function (d) {
      D = d;
      if (!D.sectors || !D.sectors.length || !D.price || !D.price.dates) throw new Error("数据格式不完整");
      $("sl-boot").hidden = true;
      $("sl-content").hidden = false;
      renderKpis();
      renderRead();
      renderAll();
      loadLive(false);
      liveTimer = setInterval(function () {
        if (!document.hidden) loadLive(false);
      }, 60000);
      document.addEventListener("visibilitychange", function () {
        if (!document.hidden && LIVE && LIVE.source !== "realtime") loadLive(false);
      });
    })
    .catch(function (e) {
      $("sl-boot").hidden = true;
      $("sl-err").textContent = "行业估值数据加载失败：" + e.message +
        "（请确认 data/sector_lab.json 已生成，或运行 python scripts/update_sector_lab.py）";
    });
})();
