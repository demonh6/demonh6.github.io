/* ============================================================
   盘面结构 · 涨停温度
   数据来源：data/dashboard.json 的 structure 字段
   （东财涨停/炸板/跌停/强势池，由 scripts/update_data.py 采集）
   ============================================================ */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var C_UP = "#f04a3d", C_DOWN = "#26a56b", C_GOLD = "#d9b23c";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v, d) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(d === undefined ? 0 : d);
  }

  function renderKpis(s) {
    var sealCls = s.seal_rate == null ? "" : (s.seal_rate >= 60 ? "up" : (s.seal_rate < 45 ? "down" : "gold"));
    var scoreCls = s.score >= 60 ? "gold" : (s.score < 45 ? "down" : "");
    var dtCls = s.dt_count >= 20 ? "down" : "";

    var cards = [
      {
        cls: "up", lbl: "涨停家数",
        val: num(s.zt_count), unit: "家",
        sub: "其中首板 " + num(s.first_board) + " · 连板 " + num(s.continued),
      },
      {
        cls: dtCls, lbl: "跌停家数",
        val: num(s.dt_count), unit: "家",
        sub: s.dt_count >= 20 ? "风险释放中" : "个股风险可控",
      },
      {
        cls: sealCls, lbl: "封板率",
        val: s.seal_rate == null ? "—" : num(s.seal_rate, 1), unit: "%",
        sub: "炸板 " + num(s.zb_count) + " 家 · 自算指标",
      },
      {
        cls: "up", lbl: "最高连板",
        val: num(s.max_streak), unit: "板",
        sub: s.max_streak >= 5 ? "有高度股" : (s.max_streak >= 3 ? "高度一般" : "缺乏高度"),
      },
      {
        cls: scoreCls, lbl: "结构分",
        val: num(s.score, 1), unit: "",
        sub: esc(s.score_label || "") + " · 权重 45/35/20",
      },
    ];

    $("st-kpis").innerHTML = cards.map(function (c) {
      return '<div class="st-kpi ' + c.cls + '">' +
        '<div class="k-lbl">' + c.lbl + '</div>' +
        '<div class="k-val ' + c.cls + '">' + c.val +
          (c.unit ? '<small>' + c.unit + '</small>' : '') + '</div>' +
        '<div class="k-sub">' + c.sub + '</div>' +
        '</div>';
    }).join("");
  }

  function renderLadder(s) {
    var rows = s.ladder || [];
    // 只展示到「最高板 + 1」，避免尾部一排 0 稀释信息
    var keep = Math.max(3, (s.max_streak || 1) + 1);
    rows = rows.filter(function (r) { return r.boards <= keep; });
    var max = Math.max.apply(null, rows.map(function (r) { return r.count; }).concat([1]));
    $("st-ladder").innerHTML = rows.map(function (r) {
      var w = r.count / max * 100;
      var hi = r.boards >= 3 && r.count > 0 ? " hi" : "";
      return '<div class="st-lrow' + hi + '">' +
        '<span class="lb">' + esc(r.label) + '</span>' +
        '<span class="track"><span class="fill" style="width:' + w.toFixed(1) + '%"></span></span>' +
        '<span class="n">' + r.count + '</span>' +
        '</div>';
    }).join("");
  }

  function renderIndustries(s) {
    var rows = (s.industries || []).slice(0, 6);
    if (!rows.length) {
      $("st-ind").innerHTML = '<div class="st-note">今日无涨停行业数据</div>';
      return;
    }
    var max = Math.max.apply(null, rows.map(function (r) { return r.count; }).concat([1]));
    $("st-ind").innerHTML = rows.map(function (r) {
      return '<div class="st-irow">' +
        '<span class="nm" title="' + esc(r.name) + '">' + esc(r.name) + '</span>' +
        '<span class="track"><span class="fill" style="width:' + (r.count / max * 100).toFixed(1) + '%"></span></span>' +
        '<span class="v">' + r.count + '<small>' + num(r.share, 0) + '%</small></span>' +
        '</div>';
    }).join("") +
      '<div class="st-note">占比 = 该行业涨停家数 ÷ 当日涨停总数。占比越高，说明资金主线越集中。</div>';
  }

  function renderLeaders(s) {
    var rows = s.leaders || [];
    if (!rows.length) {
      $("st-leaders").innerHTML = '<div class="st-note">今日无涨停个股</div>';
      return;
    }
    $("st-leaders").innerHTML = rows.map(function (x) {
      var bdCls = x.streak >= 4 ? "" : " weak";
      return '<div class="st-lead">' +
        '<span class="nm"><span>' + esc(x.name) + '</span>' +
          '<span class="cd">' + esc(x.code) + '</span>' +
          '<span class="ind">' + esc(x.industry || "—") + '</span></span>' +
        '<span class="bd' + bdCls + '">' + x.streak + '板</span>' +
        '<span class="fd">封单 ' + (x.fund_yi != null ? x.fund_yi.toFixed(2) + "亿" : "—") + '</span>' +
        '<span class="pc">+' + num(x.change_pct, 2) + '%</span>' +
        '</div>';
    }).join("");
  }

  function renderHistory(s) {
    var h = s.history || [];
    var box = $("st-hist");
    var W = 520, H = 180, L = 34, R = 14, T = 14, B = 26;
    if (!h.length) {
      box.innerHTML = '<text x="' + (W / 2) + '" y="' + (H / 2) + '" text-anchor="middle" ' +
        'font-size="11" fill="#59647a">暂无历史序列</text>';
      return;
    }
    var vals = [];
    h.forEach(function (p) { vals.push(p.zt || 0, p.dt || 0); });
    var maxV = Math.max.apply(null, vals.concat([10]));
    var hi = Math.ceil(maxV * 1.18 / 10) * 10;

    function x(i) { return L + (h.length === 1 ? (W - L - R) / 2 : i * (W - L - R) / (h.length - 1)); }
    function y(v) { return T + (hi - v) / hi * (H - T - B); }

    var g = "";
    [0, 0.5, 1].forEach(function (f) {
      var v = hi * f, yy = y(v);
      g += '<line x1="' + L + '" y1="' + yy.toFixed(1) + '" x2="' + (W - R) + '" y2="' + yy.toFixed(1) +
        '" stroke="rgba(255,255,255,.055)"/>' +
        '<text x="' + (L - 6) + '" y="' + (yy + 3).toFixed(1) + '" text-anchor="end" font-size="9.5" fill="#59647a">' +
        Math.round(v) + '</text>';
    });

    function line(key, color) {
      if (h.length === 1) {
        return '<circle cx="' + x(0).toFixed(1) + '" cy="' + y(h[0][key] || 0).toFixed(1) +
          '" r="3.5" fill="' + color + '"/>';
      }
      var pts = h.map(function (p, i) { return x(i).toFixed(1) + "," + y(p[key] || 0).toFixed(1); });
      var last = h[h.length - 1];
      return '<polyline points="' + pts.join(" ") + '" fill="none" stroke="' + color +
        '" stroke-width="1.9" stroke-linejoin="round" stroke-linecap="round"/>' +
        '<circle cx="' + x(h.length - 1).toFixed(1) + '" cy="' + y(last[key] || 0).toFixed(1) +
        '" r="3.2" fill="' + color + '"/>';
    }

    // x 轴：首/中/末
    var ticks = [];
    if (h.length === 1) ticks = [0];
    else if (h.length === 2) ticks = [0, 1];
    else ticks = [0, Math.floor((h.length - 1) / 2), h.length - 1];
    var xlab = ticks.map(function (i) {
      var d = String(h[i].date || "").slice(5);
      return '<text x="' + x(i).toFixed(1) + '" y="' + (H - 8) + '" text-anchor="middle" font-size="9.5" fill="#59647a">' + d + '</text>';
    }).join("");

    var note = "";
    if (h.length < 2) {
      var y1 = y(h[0].zt || 0), y2 = y(h[0].dt || 0);
      if (Math.abs(y1 - y2) < 13) {                 // 两点过近时上下错开，避免标签重叠
        var mid = (y1 + y2) / 2;
        y1 = mid - 8; y2 = mid + 8;
      }
      note = '<text x="' + (L + 4) + '" y="' + (T + 12) + '" font-size="10" fill="#8b98a9">序列自 ' +
        esc(h[0].date) + ' 起累积，每个交易日追加 1 点</text>' +
        '<text x="' + (x(0) + 10).toFixed(1) + '" y="' + (y1 + 3).toFixed(1) +
        '" font-size="10.5" fill="' + C_UP + '">涨停 ' + (h[0].zt || 0) + '</text>' +
        '<text x="' + (x(0) + 10).toFixed(1) + '" y="' + (y2 + 3).toFixed(1) +
        '" font-size="10.5" fill="' + C_DOWN + '">跌停 ' + (h[0].dt || 0) + '</text>';
    }

    box.setAttribute("viewBox", "0 0 " + W + " " + H);
    box.innerHTML = g + line("zt", C_UP) + line("dt", C_DOWN) + xlab + note;
  }

  function renderInsights(s) {
    var list = s.insights || [];
    if (!list.length) {
      $("st-insights").innerHTML = '<div class="st-ins">数据不足，暂不生成解读。</div>';
      return;
    }
    $("st-insights").innerHTML = list.map(function (t) {
      var i = t.indexOf("：");
      var head = i > 0 ? t.slice(0, i) : "";
      var rest = i > 0 ? t.slice(i + 1) : t;
      return '<div class="st-ins"><span class="b">·</span><span>' +
        (head ? "<b>" + esc(head) + "</b>：" : "") + esc(rest) + '</span></div>';
    }).join("");
  }

  function renderStructure(s) {
    if (!s || (!s.zt_count && !s.dt_count)) {
      $("st-boot").hidden = true;
      $("st-err").textContent = "盘面结构数据暂缺（多为非交易日，或东财涨停池接口临时不可用）。";
      return;
    }
    $("st-boot").hidden = true;
    $("st-body").hidden = false;

    renderKpis(s);
    renderLadder(s);
    renderIndustries(s);
    renderLeaders(s);
    renderHistory(s);
    renderInsights(s);

    var upd = $("st-date");
    if (upd) upd.textContent = s.date || "—";
  }

  window.renderStructure = renderStructure;
})();
