(function () {
  var data = null, tab = "SPOT";
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
    return '<span class="chip ' + s.market.toLowerCase() + '">' + (s.market === "PERP" ? "Perps" : "Spot") + '</span>' +
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
      '<div class="bottom">' + st + '<span class="muted lvl" style="margin-left:auto">' + (s.market === "PERP" ? "Price " + px(s.last) + " · " : "") + 'checked ' + ago(s.last_ts) + '</span></div></article>';
  }
  function closedRow(s) {
    var lev = s.market === "PERP" && s.lev ? ' <span class="muted">(' + sg(s.result_pct * s.lev) + '% at ' + s.lev + 'x)</span>' : "";
    return '<article class="tok"><div class="row"><span class="sym">' + esc(s.coin) + '</span>' + chips(s) +
      '<span class="when">' + ago(s.closed_at) + '</span></div><div class="bottom">' +
      '<span class="chip st ' + (s.result_r > 0 ? "win" : "loss") + '">' + LABEL[s.status] + '</span>' +
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
      '<p class="card muted empty">No open ' + (tab === "PERP" ? "perps" : "spot") + ' signals right now. New ones appear when a setup forms (checked every 15 minutes).</p>';
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
      $("bt").innerHTML = t;
    }
    $("updated").textContent = "Updated " + ago(data.generated);
  }
  document.querySelectorAll(".tab").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".tab").forEach(function (x) { x.classList.toggle("on", x === b); });
      tab = b.getAttribute("data-f"); if (data) renderOpen();
    });
  });
  function load() {
    fetch("data.json?t=" + Date.now()).then(function (r) { return r.json(); }).then(function (d) { data = d; render(); })
      .catch(function () { $("open").innerHTML = '<p class="muted">Could not load data. Pull to refresh.</p>'; });
  }
  load(); setInterval(load, 120000);
})();
