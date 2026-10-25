(function () {
  var data = null, tab = "SPOT", ctab = "unlocks";
  var KIND = {TA: "Chart breakout", UNLOCK_SHORT: "Unlock short", POST_UNLOCK_LONG: "Post-unlock long", FUND_LONG: "Fundamentals long", ONCHAIN: "On-chain spot", HOT: "24h hot mover"};
  function money(x) { if (x == null) return "–"; return x >= 1e9 ? "$" + (x / 1e9).toFixed(1) + "B" : x >= 1e6 ? "$" + (x / 1e6).toFixed(1) + "M" : "$" + Math.round(x / 1e3) + "k"; }
  function day(ts) { return new Date(ts * 1000).toLocaleDateString(undefined, {month: "short", day: "numeric"}); }
  function inDays(ts) { var d = (ts * 1000 - Date.now()) / 864e5; return d < 1 ? "in " + Math.max(1, Math.round(d * 24)) + " h" : "in " + Math.round(d) + (Math.round(d) === 1 ? " day" : " days"); }
  function tone(r) {
    if (/crowded|exhaustion|^Unlock: .*\(big\)|-\d+% 7d/.test(r)) return "neg";
    if (/with breakout|passed .* held|leading|\+\d+% 7d|GoPlus OK/.test(r)) return "pos";
    return "";
  }
  function why(s) {
    var rs = s.reasons || [];
    if (!rs.length) return "";
    return '<ul class="why">' + rs.map(function (r) { return '<li class="' + tone(r) + '">' + esc(r) + '</li>'; }).join("") + '</ul>';
  }
  function copyBtn(t) { return '<div class="ca"><code>' + esc(t) + '</code><button class="copy" data-copy="' + esc(t) + '">Copy</button></div>'; }
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]; }); }
  function px(p) {
    if (p == null) return "–";
    if (p >= 1000) return p.toFixed(1);
    if (p >= 100) return p.toFixed(2);
    if (p >= 10) return p.toFixed(3);
    if (p >= 1) return p.toFixed(4);
    return Number(p.toPrecision(5)).toString();
  }
  function sg(n, d) { return n == null ? "–" : (n > 0 ? "+" : "") + n.toFixed(d == null ? 1 : d); }
  function cls(n) { return n == null ? "" : n > 0 ? "good" : n < 0 ? "bad" : "mid"; }
  function ago(iso) {
    if (!iso) return "";
    var s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    var m = Math.floor(s / 60); if (m < 60) return m + " min ago";
    var h = Math.floor(m / 60); if (h < 24) return h + " h ago";
    var d = Math.floor(h / 24); return d + (d === 1 ? " day ago" : " days ago");
  }
  function kv(k, v, c) { return '<div class="kv"><span class="k">' + k + '</span><span class="v ' + (c || "") + '">' + v + '</span></div>'; }
  var LABEL = {open: "Open", tp1: "TP1 hit", tp2: "TP2 hit", tp3: "TP3 hit", stopped: "Stopped", stopped_be: "Stopped at entry", expired: "Expired"};
  function chips(s) {
    var mk = s.market === "PERP" ? "Perps" : s.market === "DEX" ? (s.chain || "On-chain") : "Spot";
    return '<span class="chip ' + s.market.toLowerCase() + '">' + esc(mk) + '</span>' +
           '<span class="chip ' + s.side.toLowerCase() + '">' + s.side + '</span>' +
           (s.lev ? '<span class="chip st">' + s.lev + 'x</span>' : "");
  }
  function openCard(s) {
    var tps = "";
    for (var k = 0; k < 3; k++) tps += kv("TP" + (k + 1) + (s.hits > k ? " ✓" : ""), px(s.tps[k]), s.hits > k ? "hit" : "");
    var st = '<span class="chip st ' + (s.hits ? "win" : "") + '">' + LABEL[s.status] + '</span>';
    var extra = s.market === "PERP" && s.funding_pct != null ? kv("Funding", sg(s.funding_pct, 4) + "%", "") : kv("Price", px(s.last), "");
    return '<article class="tok"><div class="row"><span class="sym">' + esc(s.coin) + '</span>' + chips(s) +
      '<span class="when">' + ago(s.opened) + '</span></div>' +
      '<div class="grid g4">' + kv("Entry", px(s.entry)) + kv("Stop" + (s.hits ? " (entry)" : ""), px(s.hits ? s.entry : s.sl0), "bad") + extra +
      kv("Move", (s.move_pct == null ? "–" : sg(s.move_pct) + "%"), cls(s.move_pct)) + '</div>' +
      '<div class="grid">' + tps + '</div>' +
      (s.trade_on ? '<p class="tradeon"><b>Trade on: ' + esc(s.trade_on) + '</b>' + (s.market_reason ? ' <span class="muted">— ' + esc(s.market_reason) + '</span>' : "") + '</p>' : "") +
      (s.chart ? '<a href="' + esc(chartUrl(s.chart)) + '" target="_blank" rel="noopener"><img class="sigchart" loading="lazy" src="' + esc(chartUrl(s.chart)) + '" alt="' + esc(s.coin) + ' signal chart"></a>' : "") +
      why(s) + (s.market === "DEX" && s.token ? copyBtn(s.token) : "") +
      '<div class="bottom">' + st + '<span class="chip kind">' + esc(KIND[s.kind] || "Chart breakout") + '</span>' +
      (s.score != null ? '<span class="score ' + cls(s.score) + '">Catalyst ' + sg(s.score, 0) + '</span>' : "") + '<span class="muted lvl" style="margin-left:auto">' + (s.market === "PERP" ? "Price " + px(s.last) + " · " : "") + 'checked ' + ago(s.last_ts) + '</span></div></article>';
  }
  function closedRow(s) {
    var lev = s.market === "PERP" && s.lev ? ' <span class="muted">(' + sg(s.result_pct * s.lev) + '% at ' + s.lev + 'x)</span>' : "";
    return '<article class="tok"><div class="row"><span class="sym">' + esc(s.coin) + '</span>' + chips(s) +
      '<span class="when">' + ago(s.closed_at) + '</span></div><div class="bottom">' +
      '<span class="chip st ' + (s.result_r > 0 ? "win" : "loss") + '">' + LABEL[s.status] + '</span>' +
      (s.kind && s.kind !== "TA" ? '<span class="chip kind">' + esc(KIND[s.kind] || s.kind) + '</span>' : "") +
      '<span class="paper ' + cls(s.result_pct) + '" style="margin-left:auto">' + sg(s.result_pct, 2) + '%' + lev + ' · ' + sg(s.result_r, 2) + 'R</span></div></article>';
  }
  function row(name, a) {
    if (!a || !a.trades) return '<tr><td>' + name + '</td><td>0</td><td>–</td><td>–</td><td>–</td></tr>';
    return '<tr><td>' + name + '</td><td>' + a.trades + '</td><td>' + a.win_rate + '%</td><td class="' + cls(a.avg_r) + '">' + sg(a.avg_r, 2) + '</td><td class="' + cls(a.total_r) + '">' + sg(a.total_r, 1) + '</td></tr>';
  }
  var HEAD = '<table><tr><th></th><th>Signals</th><th>Win</th><th>Avg R</th><th>Total R</th></tr>';
  function renderOpen() {
    var list = data.open.filter(function (s) { return s.market === tab; });
    $("open").innerHTML = list.length ? list.map(openCard).join("") :
      '<p class="card muted empty">No open ' + (tab === "PERP" ? "perps" : tab === "DEX" ? "on-chain" : "spot") + ' signals right now. New ones appear when a setup forms (checked about every 5 minutes).</p>';
  }
  function renderCat() {
    var c = data.catalysts || {}, h = "";
    if (ctab === "unlocks") {
      var u = c.unlocks || [];
      h = '<p class="bt-note" style="margin:0 0 6px">Token unlocks in the next 30 days (DefiLlama). <span class="big">Big</span> = 2%+ of circulating supply, or $10M+ and 0.5%+. Big unlocks add selling pressure: longs are scored down before them; once one passes and price holds, longs are scored up.</p>';
      h += u.length ? u.map(function (x) {
        return '<div class="urow"><span class="udate">' + day(x.ts) + '</span><span class="ucoin">' + esc(x.coin) + '</span><span class="ubody">' +
          (x.big ? '<span class="big">' + x.pct.toFixed(1) + '% of supply</span>' : x.pct.toFixed(2) + '% of supply') + ' · ' + money(x.usd) + ' · ' + inDays(x.ts) +
          '<br><span class="muted small">' + esc((x.who || []).slice(0, 2).join(", ")) + (x.insider_pct ? ' · ' + x.insider_pct + '% to team/investors' : '') + (x.tracked ? '' : ' · not in the signal list') + '</span></span></div>';
      }).join("") : '<p class="muted">No unlocks due for tracked coins.</p>';
      var r = c.unlocks_recent || [];
      if (r.length) h += '<p class="bt-note">Recently unlocked: ' + r.slice(0, 6).map(function (x) { return esc(x.coin) + ' ' + x.pct.toFixed(1) + '% (' + day(x.ts) + ')'; }).join(", ") + '</p>';
    } else if (ctab === "onchain") {
      var w = c.onchain || [];
      h = '<p class="bt-note" style="margin:0 0 6px">' + esc(c.onchain_rule || "") + ' Checked ' + (c.onchain_checked || 0) + ' trending pools.</p>';
      h += w.length ? w.map(function (x) {
        var st = x.trigger ? '<span class="chip st win">Breakout now</span>' : x.trend_up ? '<span class="chip st">Uptrend</span>' : '<span class="chip st">Watching</span>';
        return '<article class="tok"><div class="row"><span class="sym">' + esc(x.symbol) + '</span><span class="chip dex">' + esc(x.chain) + '</span>' + st +
          '<span class="when">' + sg(x.chg24h, 0) + '% 24h</span></div><div class="grid g4">' + kv("Price", px(x.price)) + kv("Liquidity", money(x.liq)) + kv("Vol 24h", money(x.vol24h)) + kv("Age", Math.round(x.age_d) + "d") + '</div>' +
          '<div class="muted small">Safety: ' + (x.safety === "ok" ? "GoPlus OK" : "no GoPlus data, be careful") + (x.chg7d != null ? ' · ' + sg(x.chg7d, 0) + '% 7d' : '') + (x.rsi ? ' · RSI ' + Math.round(x.rsi) : '') + '</div>' +
          copyBtn(x.token) + '<div class="bottom"><a class="dex" href="' + esc(x.url) + '" target="_blank" rel="noopener">Chart ↗</a></div></article>';
      }).join("") : '<p class="muted">Nothing passes the safety filters right now.</p>';
    } else if (ctab === "sectors") {
      var s = c.sectors || [], mx = Math.max.apply(null, s.map(function (x) { return Math.abs(x.chg7d); }).concat([1]));
      h = '<p class="bt-note" style="margin:0 0 6px">Sector strength, 7 days (CoinGecko, median change of the top 50 coins in each sector). Shown on each signal; not a filter, because there is no free history to test it.</p>';
      h += s.map(function (x) {
        return '<div class="urow"><span class="ubody"><b>' + esc(x.name) + '</b> <span class="' + cls(x.chg7d) + '">' + sg(x.chg7d, 1) + '%</span>' +
          (x.tracked.length ? '<br><span class="muted small">Tracked: ' + esc(x.tracked.join(", ")) + '</span>' : '') +
          '<div class="bar"><i style="width:' + Math.round(Math.abs(x.chg7d) / mx * 100) + '%;background:' + (x.chg7d >= 0 ? "var(--green)" : "var(--red)") + '"></i></div></span></div>';
      }).join("");
    } else if (ctab === "fund") {
      var f = c.fund_movers || [];
      h = '<p class="bt-note" style="margin:0 0 6px">Biggest week-over-week moves in TVL, fees and DEX volume for tracked coins (DefiLlama).</p>';
      h += f.length ? f.map(function (x) {
        return '<div class="urow"><span class="ucoin">' + esc(x.coin) + '</span><span class="ubody">' + esc(x.metric) + ' <span class="' + cls(x.chg7d) + '">' + sg(x.chg7d, 0) + '%</span> 7d · ' + money(x.size) + '</span></div>';
      }).join("") : '<p class="muted">No big moves this week.</p>';
    } else {
      var l = c.listings || [];
      h = '<p class="bt-note" style="margin:0 0 6px">New on OKX in the last 14 days. Watch only: new listings swing wildly in the first hours, and no signal is given until a coin has 60 days of history.</p>';
      h += l.length ? l.map(function (x) {
        return '<div class="urow"><span class="udate">' + day(x.list_ts) + '</span><span class="ucoin">' + esc(x.coin) + '</span><span class="ubody">' + (x.market === "PERP" ? "Perpetual" : "Spot") + (x.list_ts * 1000 > Date.now() ? ' · coming ' + inDays(x.list_ts) : '') + '</span></div>';
      }).join("") : '<p class="muted">No new listings.</p>';
    }
    $("cat").innerHTML = h;
  }
  function catBacktest(b) {
    var x = b.catalyst; if (!x) return "";
    var V = x.variants, D = x.decisions || {};
    function r3(name, h) {
      function c(a) { return a && a.trades ? a.trades + ' · <span class="' + cls(a.avg_r_net) + '">' + sg(a.avg_r_net, 2) + 'R</span>' : "–"; }
      return '<tr><td>' + esc(name) + '</td><td>' + c(h.H1) + '</td><td>' + c(h.H2) + '</td><td>' + c(h.all) + '</td></tr>';
    }
    var t = '<h3 style="font-size:15px;margin:14px 0 6px">Catalyst rules vs chart-only (' + x.coins + ' coins, ' + x.from + ' to ' + x.to + ')</h3>' +
      '<table><tr><th></th><th>First half</th><th>Second half</th><th>All</th></tr>';
    Object.keys(V).forEach(function (k) { t += r3(k, V[k]); });
    Object.keys(x.catalyst).forEach(function (k) { t += r3("Signal: " + (KIND[k] || k), x.catalyst[k]); });
    t += '</table><p class="bt-note">Signals · average result per signal after fees, in R. A rule becomes a filter only if it beats chart-only in both halves. ';
    var dk = Object.keys(D);
    if (dk.length) t += dk.map(function (k) { return '<b>' + esc(k) + ':</b> ' + esc(D[k]); }).join(" ");
    return t + '</p>';
  }
  function render() {
    var a = data.stats.all;
    $("r-wr").textContent = a.trades ? a.win_rate + "%" : "–";
    $("r-r").textContent = a.trades ? sg(a.total_r, 1) : "–";
    $("r-r").className = "n " + cls(a.total_r);
    $("r-open").textContent = data.stats.open;
    $("tg").href = data.telegram;
    renderOpen();
    $("closed").innerHTML = data.closed.length ? data.closed.slice(0, 25).map(closedRow).join("") : '<p class="card muted empty">No closed signals yet. Live tracking started ' + new Date(data.generated).toLocaleDateString() + '.</p>';
    var st = data.stats, h = HEAD + row("All", st.all) + row("Spot", st.SPOT) + row("Perps", st.PERP);
    Object.keys(st.coins || {}).forEach(function (c) { h += row(c, st.coins[c]); });
    $("stats").innerHTML = h + '</table><p class="bt-note">A “win” is any signal that closed in profit, including TP1 then stopped at entry. R = the stop distance (risking 1R per signal).</p>';
    var b = data.backtest;
    if (b) {
      var t = HEAD + row("All", b.all) + row("Spot", b.SPOT) + row("Perps long", b.PERP_LONG) + row("Perps short", b.PERP_SHORT);
      Object.keys(b.coins).forEach(function (c) { t += row(c, b.coins[c]); });
      t += '</table><p class="bt-note">Backtest of these exact rules on ' + b.days + ' days of past 1h candles (' + b.from + ' to ' + b.to + '). ' +
        'Before fees. After typical fees (' + b.fees.SPOT + '% spot, ' + b.fees.PERP + '% perps round trip) the average drops to ' + sg(b.all.avg_r_net, 2) + 'R per signal, close to break-even. ' +
        (b.before ? 'The first version (EMA20 pullback) lost money in the same test (' + sg(b.before.all.avg_r, 2) + 'R per signal before fees), so it was replaced. ' : '') +
        'Past results do not predict future ones.</p>';
      $("bt").innerHTML = v3Backtest(b) + t + catBacktest(b);
    }
    renderCat();
    if (data.coins) $("ncoins").textContent = data.coins.length;
    if (data.caps) { $("maxopen").textContent = data.caps.max_open; $("maxday").textContent = data.caps.max_new_per_day; }
    $("updated").textContent = "Updated " + ago(data.generated);
  }
  document.querySelectorAll(".tab").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".tab").forEach(function (x) { x.classList.toggle("on", x === b); });
      tab = b.getAttribute("data-f"); if (data) renderOpen();
    });
  });
  document.querySelectorAll(".ctab").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".ctab").forEach(function (x) { x.classList.toggle("on", x === b); });
      ctab = b.getAttribute("data-c"); if (data) renderCat();
    });
  });
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-copy]"); if (!b) return;
    var t = b.getAttribute("data-copy");
    (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).then(function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1500); }, function () {});
  });
  function v3Backtest(b) { return btTable(b.v4, "Strategy v4 (chart reading)") + btTable(b.v3, "Strategy v3 (20-hour breakout + filters)"); }
  function btTable(v, title) {
    if (!v) return "";
    function c(a) { return a && a.trades ? '<td>' + a.trades + '</td><td class="' + cls(a.avg_r_net) + '">' + sg(a.avg_r_net, 3) + 'R</td>' : '<td>0</td><td>–</td>'; }
    var h = '<h3>' + title + ' on ' + v.coins + ' coins (' + v.from + ' → ' + v.to + ', split ' + v.mid + ')</h3>' +
      '<table class="bt"><tr><th>Version</th><th>Signals H1</th><th>Net R H1</th><th>Signals H2</th><th>Net R H2</th></tr>';
    Object.keys(v.rows).forEach(function (k) { h += '<tr><td>' + esc(k) + '</td>' + c(v.rows[k].H1) + c(v.rows[k].H2) + '</tr>'; });
    return h + '</table><p class="bt-note">Average result per signal <b>after fees</b> (' + v.fees_pct_round_trip.SPOT + '% spot, ' + v.fees_pct_round_trip.PERP +
      '% perps round trip), in R. H1 = older half, H2 = newer half. A rule is kept only if it helped in both halves' +
      ((v.kept_filters || v.filters) && (v.kept_filters || v.filters).length ? ' (kept: ' + esc((v.kept_filters || v.filters).join(", ")) + ')' : ' (none qualified)') +
      (v.ml_kept === false ? '. The machine-learning score did not beat the rules out-of-sample, so it is off' : "") +
      '. The coin gate row is walk-forward: coins picked on one half, tested on the other. Past results do not predict future ones.</p>';
  }
  // GitHub Pages (Fastly) serves data.json with Cache-Control: max-age=600 and ignores
  // ?t=, so the old fetch stayed up to 10 minutes behind the commit. raw.githubusercontent.com
  // is the same idea with max-age=300, and ?t= does not bust that cache either.
  // The git refs API is public, sends CORS *, and is cached for about 60s. A raw URL pinned
  // to that commit SHA is a cache miss the first time, and the bytes are exactly that commit.
  var REPO = "GoviGovi1/signals";
  var REF_URL = "https://api.github.com/repos/" + REPO + "/git/refs/heads/main";
  var dataSha = "";
  var apiBlockedUntil = 0;
  var loadGen = 0;
  function chartUrl(p) {
    if (!p) return "";
    if (/^https?:\/\//i.test(p)) return p;
    if (!dataSha || /[^A-Za-z0-9._/-]|\.\./.test(p)) return p;
    return "https://raw.githubusercontent.com/" + REPO + "/" + dataSha + "/site/" + String(p).replace(/^\//, "");
  }
  function fetchJson(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) {
        var err = new Error(String(r.status));
        err.status = r.status;
        throw err;
      }
      return r.json();
    });
  }
  function pack(p, sha) {
    return p.then(function (d) { return { data: d, sha: sha || "" }; });
  }
  function loadRaw(ref) {
    return pack(fetchJson("https://raw.githubusercontent.com/" + REPO + "/" + ref + "/site/data.json"), ref);
  }
  function loadPages() {
    return pack(fetchJson("data.json?t=" + Date.now()), "");
  }
  function loadStale() {
    return loadRaw("main").catch(function () { return loadPages(); });
  }
  function loadFresh() {
    if (Date.now() < apiBlockedUntil) return loadStale();
    return fetchJson(REF_URL).then(function (ref) {
      var sha = ref && ref.object && String(ref.object.sha || "");
      if (!/^[0-9a-f]{40}$/.test(sha)) throw new Error("sha");
      return loadRaw(sha);
    }).catch(function (err) {
      if (err && (err.status === 403 || err.status === 429)) apiBlockedUntil = Date.now() + 10 * 60 * 1000;
      return loadStale();
    });
  }
  function load() {
    var gen = ++loadGen;
    loadFresh().then(function (got) {
      if (gen !== loadGen || !got || !got.data) return;
      var same = data && data.generated && got.data.generated === data.generated && got.sha === dataSha;
      dataSha = got.sha || "";
      if (same) return;
      data = got.data;
      render();
    }).catch(function () {
      if (gen === loadGen && !data) $("open").innerHTML = '<p class="muted">Could not load data. Pull to refresh.</p>';
    });
  }
  load();
  setInterval(function () { if (document.visibilityState === "visible") load(); }, 60000);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") load();
  });
})();
