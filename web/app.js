/* VPS Director Mini App — screens, routing, API client. Vanilla JS, no build step.

   Inside Telegram every API call carries Telegram.WebApp.initData (header X-Init-Data); in a normal
   browser the session cookie from the one-time /dashboard link does the job. Every value that comes
   from the server is put into the page with textContent / attributes — never as HTML. */
(function () {
  "use strict";

  var TG = window.Telegram && window.Telegram.WebApp;
  var IN_TG = !!(TG && TG.initData);
  var app = document.getElementById("app");
  var tabbar = document.getElementById("tabbar");
  var icon = window.Icons.icon;
  var t = window.I18N.make(guessLang());
  var state = { me: null, servers: null, server: {}, security: null, sites: null, backups: null, jobs: null, problems: null };
  var screen = { cleanup: null, key: "" };
  var depth = 0; // screens opened inside the app since start (decides what "back" does)

  function guessLang() {
    var code = (IN_TG && TG.initDataUnsafe && TG.initDataUnsafe.user && TG.initDataUnsafe.user.language_code) ||
      navigator.language || "en";
    return /^ru\b/i.test(code) ? "ru" : "en";
  }

  // ---------- DOM helpers ----------

  function h(tag, props) {
    var el = document.createElement(tag);
    var p = props || {};
    Object.keys(p).forEach(function (k) {
      var v = p[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    });
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) child.forEach(function (c) { add(el, c); });
    else el.appendChild(typeof child === "object" ? child : document.createTextNode(String(child)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

  // ---------- Telegram glue ----------

  function tgAtLeast(v) { return !!(TG && TG.isVersionAtLeast && TG.isVersionAtLeast(v)); }

  function haptic(kind) {
    if (!tgAtLeast("6.1") || !TG.HapticFeedback) return;
    try {
      if (kind === "success" || kind === "error" || kind === "warning") TG.HapticFeedback.notificationOccurred(kind);
      else if (kind === "select") TG.HapticFeedback.selectionChanged();
      else TG.HapticFeedback.impactOccurred(kind || "light");
    } catch (e) { /* older clients */ }
  }

  function confirmDialog(text) {
    return new Promise(function (resolve) {
      if (tgAtLeast("6.2") && TG.showConfirm) {
        try { TG.showConfirm(text, function (ok) { resolve(!!ok); }); return; } catch (e) { /* fall through */ }
      }
      resolve(window.confirm(text));
    });
  }

  // themes: theme.js already set data-theme before the first paint; here the Telegram frame follows it
  var chromeConsole = false;
  function applyTheme() {
    if (window.VPSD_THEME) window.VPSD_THEME.apply();
    setChrome(chromeConsole);
  }

  function setChrome(consoleMode) {
    chromeConsole = !!consoleMode;
    if (!IN_TG || !tgAtLeast("6.1")) return;
    var theme = window.VPSD_THEME ? window.VPSD_THEME.current() : "light";
    var bg = consoleMode ? "#0b1622" : (window.VPSD_THEME ? window.VPSD_THEME.bg[theme] : "#f3f5f9");
    try {
      if (tgAtLeast("6.9")) { TG.setHeaderColor(bg); TG.setBackgroundColor(bg); }
      else { TG.setHeaderColor("secondary_bg_color"); TG.setBackgroundColor("secondary_bg_color"); }
      if (tgAtLeast("7.10") && TG.setBottomBarColor) TG.setBottomBarColor(consoleMode ? "#0b1622" : (theme === "dark" ? "#0c2338" : "#ffffff"));
    } catch (e) { /* not supported */ }
  }
  if (window.matchMedia) {  // "System" in a normal browser follows the system as it changes
    try { window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme); } catch (e) { /* old engines */ }
  }

  var backHandler = null;
  function setBack(show) {
    if (!IN_TG || !tgAtLeast("6.1") || !TG.BackButton) return;
    if (backHandler) TG.BackButton.offClick(backHandler);
    backHandler = null;
    if (show) { backHandler = goBack; TG.BackButton.onClick(backHandler); TG.BackButton.show(); }
    else TG.BackButton.hide();
  }

  // swipe from the left edge to the right = Back, like the native gesture (only where Back exists)
  (function edgeSwipe() {
    var sx = null, sy = 0, dx = 0, EDGE = 28, NEED = 70;
    function target() { return document.querySelector("body.console-mode .console") || document.getElementById("app"); }
    window.addEventListener("touchstart", function (e) {
      sx = null;
      if (e.touches.length !== 1 || !parseRoute().back) return;
      var t0 = e.touches[0];
      if (t0.clientX > EDGE) return;
      sx = t0.clientX; sy = t0.clientY; dx = 0;
    }, { passive: true });
    window.addEventListener("touchmove", function (e) {
      if (sx === null) return;
      var t1 = e.touches[0];
      dx = t1.clientX - sx;
      if (Math.abs(t1.clientY - sy) > 60) { sx = null; reset(); return; }  // a scroll, not a swipe
      var el = target();
      if (el && dx > 0) { el.style.transition = "none"; el.style.transform = "translateX(" + Math.min(dx, 140) / 2.5 + "px)"; }
    }, { passive: true });
    function reset() {
      var el = target();
      if (el) { el.style.transition = "transform .2s ease"; el.style.transform = ""; }
    }
    window.addEventListener("touchend", function () {
      if (sx === null) return;
      sx = null;
      reset();
      if (dx > NEED) goBack();
    });
    window.addEventListener("touchcancel", function () { sx = null; reset(); });
  })();

  var mainHandler = null;
  function setMainButton(text, fn) {
    if (!IN_TG || !TG.MainButton) return false;
    if (mainHandler) TG.MainButton.offClick(mainHandler);
    mainHandler = null;
    if (!text) { TG.MainButton.hide(); return true; }
    mainHandler = function () { haptic("medium"); fn(); };
    TG.MainButton.setText(text);
    TG.MainButton.onClick(mainHandler);
    TG.MainButton.show();
    return true;
  }

  // ---------- API ----------

  function ApiError(status, message) { this.status = status; this.message = message; }

  // Inside Telegram the initData is swapped once for a sliding session (initData itself is only
  // fresh for 10 minutes — without this the Mini App stopped working 10 minutes after opening).
  var session = null, sessionReq = null;

  function ensureSession() {
    if (!IN_TG || session) return Promise.resolve();
    if (!sessionReq) {
      sessionReq = request("POST", "/api/session", {}, { "X-Init-Data": TG.initData }).then(function (data) {
        session = data.session;
      }).then(function () { sessionReq = null; }, function (err) { sessionReq = null; throw err; });
    }
    return sessionReq;
  }

  function api(method, path, body) {
    return ensureSession().then(function () {
      return request(method, path, body, session ? { "X-Vpsd-Session": session } : {});
    }).catch(function (err) {
      // the session expired (an hour idle or 12 hours old): get a new one once, if initData is still fresh
      if (err.status === 401 && session) {
        session = null;
        return ensureSession().then(function () {
          return request(method, path, body, { "X-Vpsd-Session": session });
        });
      }
      throw err;
    }).catch(function (err) {
      if (err.status === 401) showAuth();
      throw err;
    });
  }

  function request(method, path, body, extra) {
    var headers = { "X-Requested-With": "vpsd", "Accept": "application/json" };
    for (var k in extra) headers[k] = extra[k];
    var opts = { method: method, headers: headers, credentials: "same-origin", cache: "no-store" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body || {});
    }
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = ctl && setTimeout(function () { ctl.abort(); }, 45000);
    if (ctl) opts.signal = ctl.signal;
    return fetch(path, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (timer) clearTimeout(timer);
        if (res.status === 401) throw new ApiError(401, "unauthorized");
        if (!res.ok || data.ok === false) {
          var e = new ApiError(res.status, res.status === 429 ? t("too_many") : (data.error || t("error")));
          e.data = data;
          throw e;
        }
        return data;
      });
    }, function () {
      if (timer) clearTimeout(timer);
      throw new ApiError(0, t("net_error"));
    });
  }

  // ---------- small formatting helpers ----------

  function hhmm(sec) {
    try { return new Date(sec * 1000).toLocaleTimeString(t.lang, { hour: "2-digit", minute: "2-digit" }); }
    catch (e) { return ""; }
  }
  function uptime(sec) {
    sec = Number(sec) || 0;
    if (sec >= 86400) return t("up_d", { n: Math.floor(sec / 86400) });
    if (sec >= 3600) return t("up_h", { n: Math.floor(sec / 3600) });
    return t("up_m", { n: Math.max(1, Math.floor(sec / 60)) });
  }
  function level(pct, warnAt) { return pct >= Math.max(95, warnAt + 5) ? "bad" : pct >= warnAt ? "warn" : ""; }
  function bar(pct, lvl, label) {
    var fill = h("i");
    fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
    return h("div", { class: "bar" + (lvl ? " " + lvl : ""), role: "meter", "aria-valuemin": "0", "aria-valuemax": "100",
      "aria-valuenow": String(Math.round(pct)), "aria-label": label }, fill);
  }
  // a sparkline (SVG built with createElementNS — no HTML strings): values 0…max, newest on the right
  var SVGNS = "http://www.w3.org/2000/svg";
  function spark(values, max, lvl, label) {
    if (!values || values.length < 2) return null;
    var w = 100, ht = 30, n = values.length;
    var top = Math.max(max || 0, Math.max.apply(null, values), 1);
    var pts = values.map(function (v, i) { return [(i / (n - 1)) * w, ht - (Math.min(v, top) / top) * (ht - 3) - 1.5]; });
    var line = "M" + pts.map(function (p) { return p[0].toFixed(2) + " " + p[1].toFixed(2); }).join(" L");
    var svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 " + w + " " + ht);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("class", "spark" + (lvl ? " " + lvl : ""));
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label || "");
    var area = document.createElementNS(SVGNS, "path");
    area.setAttribute("d", line + " L" + w + " " + ht + " L0 " + ht + " Z");
    area.setAttribute("class", "spark-area");
    var path = document.createElementNS(SVGNS, "path");
    path.setAttribute("d", line);
    path.setAttribute("class", "spark-line");
    path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(area);
    svg.appendChild(path);
    return svg;
  }

  // the approved VPS Director mark: three servers wired to a central control gauge (theme colours)
  function brandMark(cls) {
    var svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 64 64");
    svg.setAttribute("class", "mark" + (cls ? " " + cls : ""));
    svg.setAttribute("aria-hidden", "true");
    function el(tag, attrs) {
      var e = document.createElementNS(SVGNS, tag);
      Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
      svg.appendChild(e);
      return e;
    }
    var wire = { fill: "none", stroke: "currentColor", "stroke-width": "2.4", "stroke-linecap": "round", "stroke-linejoin": "round" };
    function server(x, y) {
      el("rect", { x: x, y: y, width: 22, height: 14, rx: 3.6, fill: "currentColor" });
      el("rect", { x: x + 3.4, y: y + 3.4, width: 11, height: 2.6, rx: 1.3, class: "slot" });
      el("rect", { x: x + 3.4, y: y + 8, width: 11, height: 2.6, rx: 1.3, class: "slot" });
      el("circle", { cx: x + 18, cy: y + 4.7, r: 1.45, class: "dot" });
      el("circle", { cx: x + 18, cy: y + 9.3, r: 1.45, class: "dot" });
    }
    server(21, 2); server(3, 46); server(39, 46);
    el("path", Object.assign({ d: "M32 16v6.5" }, wire));
    el("path", Object.assign({ d: "M16.7 26.5h6.6 M47.3 26.5h-6.6" }, wire));
    el("circle", Object.assign({ cx: 14, cy: 26.5, r: 2.7 }, wire));
    el("circle", Object.assign({ cx: 50, cy: 26.5, r: 2.7 }, wire));
    el("path", Object.assign({ d: "M14 29.2v16.8 M50 29.2v16.8" }, wire));
    el("path", Object.assign({ d: "M26 40.5l-3.4 5.5 M38 40.5l3.4 5.5" }, wire));
    el("circle", Object.assign({ cx: 32, cy: 32.5, r: 9.6 }, wire));
    el("path", Object.assign({ d: "M26.6 35.2a6 6 0 0 1 3.2-8.3", "stroke-width": "1.9" }, { fill: "none", stroke: "currentColor", "stroke-linecap": "round" }));
    el("path", Object.assign({ d: "M32 32.6l4.3-4.6" }, wire));
    el("circle", { cx: 32, cy: 32.6, r: 2.2, fill: "currentColor" });
    return svg;
  }

  var STATUS_ICON = { ok: "ok", warn: "warn", bad: "bad", info: "info", wait: "clock" };
  function pill(kind, text) { return h("span", { class: "pill " + kind }, icon(STATUS_ICON[kind] || "info"), text); }
  function local(name, text) {
    var p = name + ": ";
    return text.indexOf(p) === 0 ? text.charAt(p.length).toUpperCase() + text.slice(p.length + 1) : text;
  }
  function serverStatus(row) {
    if (!row || !row.online) return ["bad", t("offline")];
    if (row.problems && row.problems.length) return ["warn", t("problems_n", { n: row.problems.length })];
    return ["ok", t("online")];
  }

  // a problem line that opens its page (what it means, what the server shows, how to fix it)
  function issueItem(level, text, key) {
    var cls = level === "bad" ? "bad" : level === "info" ? "info" : "warn";
    var inner = [icon(cls), h("span", { class: "grow", text: text })];
    if (!key) return h("li", { class: cls + "-text" }, inner);
    return h("li", { class: "issue" }, h("button", { type: "button", class: "issue-btn " + cls + "-text",
      onclick: function () { haptic("light"); navigate("#/problem/" + encodeURIComponent(key)); } },
      inner, h("span", { class: "chev" }, icon("right"))));
  }
  function issueList(items) {  // [[level, text, key]]
    var ul = h("ul", { class: "problems" });
    items.forEach(function (it) { ul.appendChild(issueItem(it[0], it[1], it[2])); });
    return ul;
  }

  function openLink(url) {
    if (IN_TG && TG.openLink) { try { TG.openLink(url); return; } catch (e) { /* fall through */ } }
    window.open(url, "_blank", "noopener");
  }

  var toastTimer = null;
  function toast(text) {
    var el = document.getElementById("toast");
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove("show"); }, 2800);
  }

  function topbar(title, opts) {
    opts = opts || {};
    var bar = h("div", { class: "topbar" + (opts.small ? " small" : "") });
    if (opts.back) {
      bar.appendChild(h("button", { class: "icon-btn back-btn", type: "button", "aria-label": t("back"), onclick: goBack }, icon("left")));
    }
    if (opts.brand) bar.appendChild(h("div", { class: "brand" }, brandMark(), h("h1", { text: title })));
    else bar.appendChild(h("h1", { text: title }));
    if (opts.refresh) {
      var btn = h("button", { class: "icon-btn", type: "button", "aria-label": t("refresh"), title: t("refresh"),
        onclick: function () { haptic("light"); opts.refresh(btn); } }, icon("refresh"));
      bar.appendChild(btn);
    }
    return bar;
  }
  function busy(btn, on) { if (btn) btn.setAttribute("aria-busy", on ? "true" : "false"); }
  function skeletons(n) { var out = []; for (var i = 0; i < n; i++) out.push(h("div", { class: "skeleton", "aria-hidden": "true" })); return out; }
  function sectionTitle(text, extra) { return h("h2", { class: "section-title" }, text, extra ? h("span", { class: "count", text: extra }) : null); }
  function errorBox(err, retry) {
    return h("div", { class: "center", role: "alert" }, icon("warn"), h("p", { text: err.message || t("error") }),
      retry ? h("button", { class: "btn secondary", type: "button", onclick: retry }, t("retry")) : null);
  }

  // ---------- routing ----------

  function navigate(hash) { depth++; location.hash = hash; }
  function goBack() {
    haptic("light");
    if (depth > 0) { depth--; history.back(); }
    else location.replace("#/");
  }

  function parseRoute() {
    var parts = (location.hash.replace(/^#\/?/, "") || "").split("/").map(function (p) {
      try { return decodeURIComponent(p); } catch (e) { return ""; }
    });
    var name = parts[0] || "";
    if (name === "server" && parts[1]) return { tab: "servers", key: "server", arg: parts[1], back: true };
    if (name === "console" && parts[1]) return { tab: null, key: "console", arg: parts[1], back: true };
    if (name === "problem" && parts[1]) return { tab: "servers", key: "problem", arg: parts.slice(1).join("/"), back: true };
    if (name === "problems") return { tab: "servers", key: "problems", back: true };
    if (name === "jobs" && parts[1]) return { tab: "jobs", key: "job", arg: parts[1], back: true };
    if (name === "jobs") return { tab: "jobs", key: "jobs" };
    if (name === "health") return { tab: "health", key: "health", arg: parts[1] || "security" };
    if (name === "settings" && parts[1] === "add") return { tab: "settings", key: "teamAdd", back: true };
    if (name === "settings") return { tab: "settings", key: "settings" };
    return { tab: "servers", key: "home" };
  }

  function route() {
    if (!state.me) return;
    var r = parseRoute();
    if (screen.cleanup) { try { screen.cleanup(); } catch (e) { /* ignore */ } }
    screen.cleanup = null;
    setMainButton(null);
    document.body.classList.toggle("no-tabs", !r.tab);
    document.body.classList.remove("console-mode");
    setChrome(false);
    tabbar.hidden = !r.tab;
    Array.prototype.forEach.call(tabbar.querySelectorAll("a"), function (a) {
      if (a.getAttribute("data-tab") === r.tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    setBack(!!r.back);
    clear(app);
    window.scrollTo(0, 0);
    var screens = { home: home, server: serverPage, console: consolePage, jobs: jobsPage, health: healthPage, settings: settingsPage,
      problem: problemPage, problems: problemsPage, teamAdd: teamAddPage, job: jobPage };
    screen.key = r.key;
    screen.cleanup = screens[r.key](r.arg) || null;
  }

  function buildTabs() {
    var spec = { health: ["gauge", "tab_overview"], servers: ["server", "tab_servers"], jobs: ["clock", "tab_jobs"],
      settings: ["more", "tab_more"] };
    Array.prototype.forEach.call(tabbar.querySelectorAll("a"), function (a) {
      var s = spec[a.getAttribute("data-tab")];
      clear(a);
      a.appendChild(icon(s[0]));
      a.appendChild(h("span", { text: t(s[1]) }));
      a.onclick = function () { haptic("select"); depth = 0; };
    });
    tabbar.querySelector("nav").setAttribute("aria-label", t("app"));
  }

  // ---------- pull to refresh ----------

  function pullToRefresh(indicator, onRefresh) {
    var startY = null, dist = 0, label = h("span", { text: t("pull") });
    clear(indicator);
    indicator.appendChild(icon("down"));
    indicator.appendChild(label);
    function down(e) { startY = window.scrollY <= 0 && e.touches.length === 1 ? e.touches[0].clientY : null; dist = 0; }
    function move(e) {
      if (startY === null) return;
      var dy = e.touches[0].clientY - startY;
      dist = dy > 0 ? Math.min(dy * 0.5, 90) : 0;
      indicator.style.height = dist + "px";
      var ready = dist > 60;
      if (ready !== indicator.classList.contains("ready")) haptic(ready ? "light" : "select");
      indicator.classList.toggle("ready", ready);
      label.textContent = t(ready ? "release" : "pull");
    }
    function up() {
      if (startY === null) return;
      startY = null;
      indicator.style.transition = "height .2s ease";
      indicator.style.height = "0px";
      setTimeout(function () { indicator.style.transition = ""; }, 220);
      if (dist > 60) onRefresh();
      dist = 0;
      indicator.classList.remove("ready");
    }
    window.addEventListener("touchstart", down, { passive: true });
    window.addEventListener("touchmove", move, { passive: true });
    window.addEventListener("touchend", up);
    window.addEventListener("touchcancel", up);
    return function () {
      window.removeEventListener("touchstart", down);
      window.removeEventListener("touchmove", move);
      window.removeEventListener("touchend", up);
      window.removeEventListener("touchcancel", up);
    };
  }

  // ---------- Home: server cards ----------

  function home() {
    var alive = true;
    var refreshBtn;
    var sub = h("p", { class: "subtitle" });
    var ptr = h("div", { class: "ptr", "aria-hidden": "true" });
    var body = h("div", { "aria-busy": "true" });
    var failed = false;  // the last refresh failed: the last good data stays, marked as such
    var top = topbar(state.me.bot || t("app"), { refresh: function (btn) { refreshBtn = btn; load(true); }, brand: true });
    add(app, [top, sub, ptr, body]);

    function draw(data) {
      clear(body);
      body.setAttribute("aria-busy", "false");
      var rows = data.servers || [];
      clear(sub);
      var old = Date.now() / 1000 - (data.time || 0) > 600;  // the API answer is older than 10 minutes
      sub.appendChild(h("span", { text: t("servers_n", { n: rows.length }) }));
      sub.appendChild(h("span", { class: old || failed ? "stale" : "", text: "· " + (old || failed
        ? t("data_from", { time: hhmm(data.time) }) + (failed ? " · " + t("not_refreshed") : "")
        : t("updated", { time: hhmm(data.time) })) }));
      var problems = data.problems || [];
      var offline = rows.some(function (r) { return !r.online; });
      if (!problems.length) {
        body.appendChild(h("div", { class: "banner ok", role: "status" }, icon("ok"), h("div", { class: "title", text: t("all_good") })));
      } else {
        var names = rows.filter(function (r) { return !r.online || (r.problems && r.problems.length); })
          .map(function (r) { return r.online ? r.name : r.name + " (" + t("offline").toLowerCase() + ")"; });
        var keys = [];
        rows.forEach(function (r) { (r.issues || []).forEach(function (i) { keys.push(i.key); }); });
        body.appendChild(h("button", { type: "button", class: "banner banner-btn " + (offline ? "bad" : "warn"),
          "aria-label": t("problems_n", { n: problems.length }) + ". " + t("open_details"),
          onclick: function () {
            haptic("light");
            navigate(keys.length === 1 ? "#/problem/" + encodeURIComponent(keys[0]) : "#/problems");
          } }, icon(offline ? "bad" : "warn"),
          h("div", { class: "grow" }, h("div", { class: "title", text: t("problems_n", { n: problems.length }) }),
            h("div", { class: "muted", text: names.join(" · ") })), h("span", { class: "chev" }, icon("right"))));
      }
      if (!rows.length) { body.appendChild(h("div", { class: "card empty", text: t("no_servers") })); return; }
      var grid = h("div", { class: "servers-grid" });
      rows.forEach(function (row) { grid.appendChild(serverCard(row)); });
      body.appendChild(grid);
    }

    function load(refresh) {
      busy(refreshBtn || top.querySelector(".icon-btn"), true);
      return api("GET", "/api/servers" + (refresh ? "?refresh=1" : "")).then(function (data) {
        state.servers = data;
        failed = false;
        if (!alive) return;
        if (refresh) haptic("success");
        draw(data);
      }, function (err) {
        if (!alive || err.status === 401) return;
        failed = true;
        if (state.servers) { toast(err.message); draw(state.servers); }
        else { clear(body); body.appendChild(errorBox(err, function () { load(false); })); }
      }).then(function () { busy(top.querySelector(".icon-btn"), false); });
    }

    if (state.servers) draw(state.servers); else add(body, skeletons(2));
    load(false);
    var stopPtr = pullToRefresh(ptr, function () { load(true); });
    return function () { alive = false; stopPtr(); };
  }

  function metric(label, value, pct, lvl, extra) {
    return h("div", { class: "metric" },
      h("div", { class: "k" }, h("span", { text: label })),
      h("div", { class: "v" }, value, extra ? h("small", { text: " " + extra }) : null), bar(pct, lvl, label + " " + value));
  }

  function num(x, digits) {
    try { return Number(x).toLocaleString(t.lang, { minimumFractionDigits: digits, maximumFractionDigits: digits }); }
    catch (e) { return Number(x).toFixed(digits); }
  }
  function loadText(row) { return t("load_of", { load: num(row.load, 2), n: row.cores }); }

  // a real chart or an honest empty state: CPU % when it was measured, else load average (never as %)
  function trendFor(row, points) {
    var hist = row.history || {};
    var cpu = (hist.cpu || []).filter(function (v) { return typeof v === "number"; });
    if (cpu.length >= 2) {
      return h("div", { class: "trend" }, h("span", { class: "trend-k", text: t("trend_cpu") }),
        spark(cpu.slice(-points), 100, level(cpu[cpu.length - 1], 85), t("trend_cpu")));
    }
    var load = (hist.load || []).filter(function (v) { return typeof v === "number"; });
    if (load.length >= 2) {
      return h("div", { class: "trend" }, h("span", { class: "trend-k", text: t("trend_load") }),
        spark(load.slice(-points), row.cores, row.load > row.cores ? "warn" : "", t("trend_load")));
    }
    return h("div", { class: "trend" }, h("span", { class: "trend-k", text: t("trend_cpu") }),
      h("div", { class: "trend-empty", text: t("trend_none") }));
  }

  function serverCard(row) {
    var st = serverStatus(row);
    var card = h("button", { class: "card server-card st-" + st[0], type: "button",
      "aria-label": row.name + ", " + st[1],
      onclick: function () { haptic("light"); navigate("#/server/" + encodeURIComponent(row.name)); } },
      h("div", { class: "server-head" },
        h("div", { class: "grow" },
          h("div", { class: "name" }, row.name, row.main ? h("span", { class: "tag", text: t("main") }) : null),
          (row.where || row.role) ? h("div", { class: "meta" }, row.where || "",
            row.role ? h("span", { class: "role-inline", text: (row.where ? " · " : "") + row.role }) : null) : null),
        pill(st[0], st[1])));
    if (row.online) {
      var cpuKnown = typeof row.cpu_pct === "number";
      card.appendChild(h("div", { class: "metrics" },
        cpuKnown ? metric(t("cpu"), row.cpu_pct + "%", row.cpu_pct, level(row.cpu_pct, 85))
          : metric(t("load"), num(row.load, 2), row.load / row.cores * 100, row.load > row.cores ? "warn" : "", "· " + t("cores", { n: row.cores })),
        metric(t("memory"), row.mem_pct + "%", row.mem_pct, level(row.mem_pct, 90)),
        metric(t("disk"), row.disk_pct + "%", row.disk_pct, level(row.disk_pct, 85))));
      card.appendChild(trendFor(row, 48));
      var foot = h("div", { class: "card-foot" }, h("span", { title: t("uptime") }, icon("clock"), uptime(row.uptime)));
      if (cpuKnown) foot.appendChild(h("span", { class: "tnum", text: loadText(row) }));
      if (row.containers && row.containers.total) {
        var bad = row.containers.bad && row.containers.bad.length;
        foot.appendChild(h("span", { class: bad ? "warn-text" : "" }, icon(bad ? "warn" : "box"),
          t("containers_ok", { ok: row.containers.ok, total: row.containers.total })));
      }
      if (row.failed && row.failed.length) foot.appendChild(h("span", { class: "bad-text" }, icon("bad"), t("failed_n", { n: row.failed.length })));
      card.appendChild(foot);
    }
    if (row.problems && row.problems.length) {
      var ul = h("ul", { class: "problems" });
      row.problems.forEach(function (p) { ul.appendChild(h("li", { class: row.online ? "warn-text" : "bad-text" }, icon(row.online ? "warn" : "bad"), h("span", { text: local(row.name, p) }))); });
      card.appendChild(ul);
    }
    return card;
  }

  // ---------- Server page ----------

  function serverPage(name) {
    var alive = true, timers = [], restarts = {};
    var cached = state.server[name];
    var sub = h("p", { class: "subtitle" });
    var body = h("div", { "aria-busy": "true" });
    var top = topbar(name, { back: true, small: true, refresh: function () { load(true); } });
    add(app, [top, sub, body]);
    var containersBox = null, secBox = null, data = null;

    function draw(d) {
      data = d;
      clear(body);
      body.setAttribute("aria-busy", "false");
      var row = d.server || { name: name, online: false, problems: [] };
      sub.textContent = [row.where, row.role, row.online ? t("up_for", { t: uptime(row.uptime) }) : ""].filter(Boolean).join(" · ")
        || t("updated", { time: hhmm(d.time) });
      var st = serverStatus(row);
      var banner = h("div", { class: "banner " + st[0], role: "status" }, icon(st[0]),
        h("div", null, h("div", { class: "title", text: st[1] }),
          row.online ? h("div", { class: "muted", text: t("updated", { time: hhmm(d.time) }) }) : null));
      body.appendChild(banner);
      if (row.online) {
        var cards = h("div", { class: "big-metrics" });
        var big = function (k, v, s, pct, lvl, metricKey, seriesKey, top) {
          var series = (row.history && row.history[seriesKey || metricKey] || []).filter(function (x) { return typeof x === "number"; });
          var chart = metricKey ? (series.length >= 2 ? spark(series, top || 100, lvl, k + " · 24h")
            : h("div", { class: "trend-empty", text: t("trend_none") })) : null;
          var kids = [h("div", { class: "k", text: k }), h("div", { class: "v", text: v }),
            pct === null ? null : bar(pct, lvl, k + " " + v), s ? h("div", { class: "s", text: s }) : null, chart];
          if (!metricKey) return h("div", { class: "card" }, kids);
          return h("button", { type: "button", class: "card card-btn", "aria-label": k + " " + v + ". " + t("open_details"),
            onclick: function () { haptic("light"); navigate("#/problem/" + encodeURIComponent("metric:" + name + ":" + metricKey)); } }, kids);
        };
        if (typeof row.cpu_pct === "number") {  // utilization, measured; load average is its own tile, never a %
          cards.appendChild(big(t("cpu"), row.cpu_pct + "%", t("cpu_hint"), row.cpu_pct, level(row.cpu_pct, 85), "load", "cpu"));
        }
        cards.appendChild(big(t("load"), num(row.load, 2), t("cores", { n: row.cores }), row.load / row.cores * 100,
          row.load > row.cores ? "warn" : "", "load", "load", row.cores));
        cards.appendChild(big(t("memory"), row.mem_pct + "%", row.mem_used_gb + " / " + row.mem_total_gb + " GB", row.mem_pct, level(row.mem_pct, 90), "mem"));
        cards.appendChild(big(t("disk"), row.disk_pct + "%", row.disk_used_gb + " / " + row.disk_total_gb + " GB", row.disk_pct, level(row.disk_pct, 85), "disk"));
        if (typeof row.cpu_pct !== "number") cards.appendChild(big(t("uptime"), uptime(row.uptime), null, null));
        body.appendChild(cards);
      }
      // console
      if (state.me.console) {
        var open = function () { navigate("#/console/" + encodeURIComponent(name)); };
        // a console that kept running on this server (VS Code style): the button takes you back into it
        var running = (consoles || []).filter(function (x) { return x.server === name; })[0];
        var label = running ? t("console_back_to") : t("open_console");
        if (!setMainButton(label, open)) {
          body.appendChild(h("button", { class: "btn", type: "button", onclick: open }, icon("terminal"), label));
        }
        if (running) {
          var mins = function (sec) { return Math.max(1, Math.round(sec / 60)) + (t.lang === "ru" ? " мин" : " min"); };
          body.appendChild(h("p", { class: "note" }, icon("terminal"), " ",
            running.attached ? t("console_running", { t: mins(running.age) }) : t("console_waiting", { left: mins(running.left || 0) })));
        }
      } else {
        body.appendChild(h("p", { class: "note" }, icon("lock"), " ", t("console_off")));
      }
      // problems
      body.appendChild(sectionTitle(t("problems")));
      if (row.problems && row.problems.length) {
        var plist = h("div", { class: "list" });
        (row.issues || row.problems.map(function (p) { return { text: p }; })).forEach(function (p) {
          var inner = [h("span", { class: row.online ? "warn-text" : "bad-text" }, icon(row.online ? "warn" : "bad")),
            h("div", { class: "grow", text: local(row.name, p.text) })];
          if (p.key) {
            inner.push(h("span", { class: "chev" }, icon("right")));
            plist.appendChild(h("button", { type: "button", class: "row row-btn",
              onclick: function () { haptic("light"); navigate("#/problem/" + encodeURIComponent(p.key)); } }, inner));
          } else plist.appendChild(h("div", { class: "row" }, inner));
        });
        body.appendChild(plist);
      } else body.appendChild(h("div", { class: "list" }, h("div", { class: "row" }, h("span", { class: "ok-text" }, icon("ok")),
        h("div", { class: "grow muted", text: t("no_problems") }))));
      if (row.failed && row.failed.length) {
        body.appendChild(sectionTitle(t("failed_units"), String(row.failed.length)));
        var flist = h("div", { class: "list" });
        row.failed.forEach(function (u) { flist.appendChild(h("div", { class: "row" }, h("span", { class: "bad-text" }, icon("bad")), h("div", { class: "grow mono", text: u }))); });
        body.appendChild(flist);
      }
      // containers
      var cs = d.containers || [];
      var okN = cs.filter(function (c) { return c.state === "up"; }).length;
      body.appendChild(sectionTitle(t("containers"), cs.length ? t("containers_ok", { ok: okN, total: cs.length }) : ""));
      containersBox = h("div", { class: "list" });
      body.appendChild(containersBox);
      drawContainers();
      // security for this server
      body.appendChild(sectionTitle(t("security")));
      secBox = h("div");
      body.appendChild(secBox);
      drawSecurity();
      // forget this server (never the machine the bot runs on); Undo comes in Telegram
      if (!row.main) {
        body.appendChild(h("button", { class: "btn btn-quiet", type: "button", onclick: removeServer },
          icon("trash"), t("remove_server")));
      }
    }

    function removeServer() {
      confirmDialog(t("remove_server_q", { s: name })).then(function (yes) {
        if (!yes || !alive) return;
        return api("DELETE", "/api/servers/" + encodeURIComponent(name), {}).then(function () {
          haptic("success");
          delete state.server[name];
          state.servers = null; state.security = null;
          toast(t("server_removed", { s: name }));
          goBack();
        }, function (err) { if (err.status !== 401) { haptic("error"); toast(err.message); } });
      });
    }

    function drawContainers() {
      clear(containersBox);
      var cs = (data && data.containers) || [];
      if (!cs.length) { containersBox.appendChild(h("div", { class: "row muted", text: t("no_containers") })); return; }
      cs.forEach(function (c) {
        var r = restarts[c.name];
        var info = h("button", { type: "button", class: "grow plain-btn", "aria-label": c.name + ". " + t("open_details"),
          onclick: function () { haptic("light"); navigate("#/problem/" + encodeURIComponent("ctr:" + name + ":" + c.name)); } },
          h("div", { class: "label mono", text: c.name }), h("div", { class: "sub", text: c.image + " · " + c.status }));
        if (r) {
          var map = { pending: ["wait", "restart_wait"], allowed: ["wait", "restart_allowed"], done: ["ok", "restart_done"],
            failed: ["bad", "restart_failed"], denied: ["info", "restart_denied"], expired: ["info", "restart_expired"] };
          var m = map[r.state] || map.pending;
          info.appendChild(h("div", { class: "status-line " + m[0] + "-text", role: "status" },
            m[0] === "wait" ? h("span", { class: "spinner", "aria-hidden": "true" }) : icon(m[0]),
            t(m[1]) + (r.result && r.state === "failed" ? ": " + r.result : "")));
        }
        var waiting = r && (r.state === "pending" || r.state === "allowed");
        containersBox.appendChild(h("div", { class: "row" }, h("span", { class: "dot " + c.state, "aria-hidden": "true" }), info,
          h("button", { class: "small-btn", type: "button", disabled: waiting, "aria-label": t("restart") + " " + c.name,
            onclick: function () { restart(c.name); } }, icon("restart"), h("span", { text: t("restart") }))));
      });
    }

    function restart(container) {
      confirmDialog(t("restart_q", { c: container, s: name })).then(function (yes) {
        if (!yes || !alive) return;
        return api("POST", "/api/servers/" + encodeURIComponent(name) + "/containers/" + encodeURIComponent(container) + "/restart", {})
          .then(function (res) {
            haptic("medium");
            restarts[container] = { id: res.pending, state: "pending", started: Date.now() };
            drawContainers();
            toast(t("restart_wait"));
            poll(container);
          }, function (err) { if (err.status !== 401) { haptic("error"); toast(err.message); } });
      });
    }

    function poll(container) {
      var r = restarts[container];
      if (!alive || !r) return;
      timers.push(setTimeout(function () {
        api("GET", "/api/restarts/" + encodeURIComponent(r.id)).then(function (res) {
          if (!alive) return;
          var changed = res.state !== r.state;
          r.state = res.state; r.result = res.result;
          if (changed) drawContainers();
          if (res.state === "pending" || res.state === "allowed") { if (Date.now() - r.started < 11 * 60000) poll(container); return; }
          haptic(res.state === "done" ? "success" : res.state === "failed" ? "error" : "warning");
          if (res.state === "done") timers.push(setTimeout(function () { load(true, true); }, 1500));
        }, function () { if (alive) poll(container); });
      }, 2000));
    }

    function drawSecurity() {
      clear(secBox);
      if (!state.security) { secBox.appendChild(h("div", { class: "skeleton", "aria-hidden": "true" })); return; }
      var row = (state.security.servers || []).filter(function (s) { return s.name === name; })[0];
      if (!row) { secBox.appendChild(h("div", { class: "list" }, h("div", { class: "row muted", text: "—" }))); return; }
      secBox.appendChild(securityCard(row, false));
    }

    var consoles = null;
    if (state.me.console && state.me.role !== "viewer") {
      timers.push(setTimeout(function () { loadScripts(["assistant.js", "console.js"]); }, 1200));  // warm the terminal
      api("GET", "/api/console/sessions").then(function (r) {
        consoles = r.sessions || [];
        if (alive && consoles.some(function (x) { return x.server === name; }) && data) draw(data);
      }, function () { /* the list is a convenience */ });
    }

    function load(refresh, quiet) {
      var b = top.querySelector(".icon-btn:not(.back-btn)");
      busy(b, true);
      return api("GET", "/api/servers/" + encodeURIComponent(name) + (refresh ? "?refresh=1" : "")).then(function (d) {
        state.server[name] = d;
        if (!alive) return;
        if (refresh && !quiet) haptic("success");
        draw(d);
      }, function (err) {
        if (!alive || err.status === 401) return;
        if (data) toast(err.message); else { clear(body); body.appendChild(errorBox(err, function () { load(false); })); }
      }).then(function () { busy(b, false); });
    }

    if (cached) draw(cached); else add(body, skeletons(2));
    load(false);
    if (!state.security) {
      api("GET", "/api/security").then(function (d) { state.security = d; if (alive && secBox) drawSecurity(); }, function () {
        if (alive && secBox) { clear(secBox); secBox.appendChild(h("p", { class: "note", text: t("net_error") })); }
      });
    }
    return function () { alive = false; timers.forEach(clearTimeout); };
  }

  // ---------- Problems: the list and one problem with ways to fix it ----------

  var AREA_ICON = { server: "server", security: "shield", site: "globe", backup: "box" };

  function problemsPage() {
    var alive = true;
    var body = h("div", { "aria-busy": "true" });
    var top = topbar(t("problems"), { back: true, refresh: function () { load(true); } });
    add(app, [top, body]);

    function draw(d) {
      clear(body);
      body.setAttribute("aria-busy", "false");
      var items = d.problems || [];
      if (!items.length) {
        body.appendChild(h("div", { class: "banner ok", role: "status" }, icon("ok"), h("div", { class: "title", text: t("all_good") })));
        return;
      }
      var list = h("div", { class: "list" });
      items.forEach(function (p) {
        var lvl = p.level === "bad" ? "bad" : "warn";
        list.appendChild(h("button", { type: "button", class: "row row-btn",
          onclick: function () { haptic("light"); navigate("#/problem/" + encodeURIComponent(p.key)); } },
          h("span", { class: lvl + "-text" }, icon(lvl)),
          h("div", { class: "grow" }, h("div", { class: "label", text: p.text }),
            h("div", { class: "sub" }, icon(AREA_ICON[p.area] || "info"), " " + t("area_" + p.area) + (p.server ? " · " + p.server : ""))),
          h("span", { class: "chev" }, icon("right"))));
      });
      body.appendChild(list);
    }

    function load(refresh) {
      var b = top.querySelector(".icon-btn:not(.back-btn)");
      busy(b, true);
      return api("GET", "/api/problems" + (refresh ? "?refresh=1" : "")).then(function (d) {
        state.problems = d;
        if (!alive) return;
        if (refresh) haptic("success");
        draw(d);
      }, function (err) {
        if (!alive || err.status === 401) return;
        clear(body); body.appendChild(errorBox(err, function () { load(false); }));
      }).then(function () { busy(b, false); });
    }
    if (state.problems) draw(state.problems); else add(body, skeletons(3));
    load(false);
    return function () { alive = false; };
  }

  function problemPage(key) {
    var alive = true, timers = [], runs = {};
    var body = h("div", { "aria-busy": "true" });
    // a card from Health / a metric / a container opens its state page; only real problems are "Problem"
    var info = /^(secinfo|siteinfo|bkinfo|metric|ctr):/.test(key);
    var top = topbar(info ? t("tab_health") : t("problem"), { back: true, refresh: function () { load(true); } });
    add(app, [top, body]);
    var data = null;

    function draw(d) {
      data = d;
      clear(body);
      body.setAttribute("aria-busy", "false");
      if (d.gone) {
        body.appendChild(h("div", { class: "banner ok", role: "status" }, icon("ok"),
          h("div", null, h("div", { class: "title", text: t("problem_gone") }), h("div", { class: "muted", text: t("problem_gone_sub") }))));
        body.appendChild(h("button", { class: "btn secondary", type: "button", onclick: goBack }, t("back")));
        return;
      }
      var lvl = d.level === "bad" ? "bad" : d.level === "warn" ? "warn" : "info";
      body.appendChild(h("div", { class: "card problem-head" },
        h("div", { class: "server-head" }, h("span", { class: lvl + "-text big-icon" }, icon(lvl)),
          h("div", { class: "grow" }, h("div", { class: "name", text: d.server ? local(d.server, d.title) : d.title }),
            h("div", { class: "meta", text: [t("area_" + d.area), d.server].filter(Boolean).join(" · ") })))));
      if (d.related && d.related.length) {
        body.appendChild(sectionTitle(t("problems"), String(d.related.length)));
        var rl = h("div", { class: "card" });
        rl.appendChild(issueList(d.related.map(function (i) { return [i.level, d.server ? local(d.server, i.text) : i.text, i.key]; })));
        body.appendChild(rl);
      }
      body.appendChild(sectionTitle(t("what_it_means")));
      body.appendChild(h("div", { class: "card" }, h("p", { class: "about", text: d.about })));
      if (d.seen && d.seen.length) {
        body.appendChild(sectionTitle(d.seen_title));
        d.seen.forEach(function (text) { body.appendChild(h("pre", { class: "card evidence", tabindex: "0", text: text })); });
      }
      if (d.fixes && d.fixes.length) {
        body.appendChild(sectionTitle(t("how_to_fix")));
        var box = h("div", { class: "fixes" });
        d.fixes.forEach(function (f) { box.appendChild(fixButton(f)); });
        body.appendChild(box);
      }
    }

    function fixButton(f) {
      var id = f.id + ":" + (f.arg || "");
      var r = runs[id];
      var main = f.kind === "allow" || f.kind === "task";
      var ICON = { ask: "chat", console: "terminal", recheck: "refresh", restart: "restart", unit: "restart", updates: "shield",
        f2b: "shield", accept: "ok", site: "globe", server: "server" };
      var btn = h("button", { type: "button", class: "btn" + (main ? "" : " secondary") + (f.id === "ask" ? " accent" : ""),
        disabled: !!(r && (r.state === "pending" || r.state === "allowed")), onclick: function () { run(f, id); } },
        icon(ICON[f.id] || "gear"), h("span", { text: f.label }));
      var wrap = h("div", { class: "fix" }, btn);
      if (f.hint) wrap.appendChild(h("div", { class: "hint", text: f.hint }));
      if (r) {
        var map = { pending: ["wait", "fix_wait"], allowed: ["wait", "fix_running"], done: ["ok", "fix_done"],
          failed: ["bad", "fix_failed"], denied: ["info", "restart_denied"], expired: ["info", "restart_expired"], task: ["ok", "fix_task"] };
        var m = map[r.state] || map.pending;
        wrap.appendChild(h("div", { class: "status-line " + m[0] + "-text", role: "status" },
          m[0] === "wait" ? h("span", { class: "spinner", "aria-hidden": "true" }) : icon(m[0]),
          t(m[1]) + (r.result ? ": " + r.result : "")));
      }
      return wrap;
    }

    function run(f, id) {
      if (f.kind === "nav") {
        haptic("light");
        if (f.id === "console") navigate("#/console/" + encodeURIComponent(f.target));
        else if (f.id === "server") navigate("#/server/" + encodeURIComponent(f.target));
        else if (f.id === "site") openLink(f.target);
        else if (f.id === "recheck") load(true);
        return;
      }
      var ask = f.kind === "allow" ? confirmDialog(f.label + "? " + f.hint) : Promise.resolve(true);
      ask.then(function (yes) {
        if (!yes || !alive) return;
        return api("POST", "/api/fix", { key: key, fix: f.id, arg: f.arg || "" }).then(function (res) {
          haptic("medium");
          if (res.task) { runs[id] = { state: "task" }; toast(t("fix_task")); }
          else { runs[id] = { id: res.pending, state: "pending", started: Date.now() }; toast(t("restart_wait")); poll(id); }
          draw(data);
        }, function (err) { if (err.status !== 401) { haptic("error"); toast(err.message); } });
      });
    }

    function poll(id) {
      var r = runs[id];
      if (!alive || !r || !r.id) return;
      timers.push(setTimeout(function () {
        api("GET", "/api/restarts/" + encodeURIComponent(r.id)).then(function (res) {
          if (!alive) return;
          var changed = res.state !== r.state;
          r.state = res.state; r.result = res.result;
          if (changed) draw(data);
          if (res.state === "pending" || res.state === "allowed") { if (Date.now() - r.started < 16 * 60000) poll(id); return; }
          haptic(res.state === "done" ? "success" : res.state === "failed" ? "error" : "warning");
          if (res.state === "done") { state.servers = null; state.problems = null; state.security = null; }
        }, function () { if (alive) poll(id); });
      }, 2000));
    }

    function load(refresh) {
      var b = top.querySelector(".icon-btn:not(.back-btn)");
      busy(b, true);
      return api("GET", "/api/problem?key=" + encodeURIComponent(key) + (refresh ? "&refresh=1" : "")).then(function (d) {
        if (!alive) return;
        if (refresh) haptic("success");
        draw(d);
      }, function (err) {
        if (!alive || err.status === 401) return;
        if (data) toast(err.message); else { clear(body); body.appendChild(errorBox(err, function () { load(false); })); }
      }).then(function () { busy(b, false); });
    }
    add(body, skeletons(3));
    load(false);
    return function () { alive = false; timers.forEach(clearTimeout); };
  }

  // ---------- Console (console.js does the terminal) ----------

  function consolePage(name) {
    document.body.classList.add("console-mode");
    setChrome(true);
    // the terminal's code (and its assistant) is fetched only when a console is opened, not with the app
    var alive = true, cleanup = null;
    var ctx = { h: h, add: add, clear: clear, t: t, api: api, icon: icon, haptic: haptic, TG: TG, IN_TG: IN_TG,
      goBack: goBack, confirm: confirmDialog, toast: toast, me: state.me, tgAtLeast: tgAtLeast };
    if (!window.ConsoleScreen) app.appendChild(h("div", { class: "center", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" })));
    loadScripts(["assistant.js", "console.js"]).then(function () {
      if (!alive) return;
      clear(app);
      if (!window.ConsoleScreen) { app.appendChild(errorBox({ message: t("net_error") }, function () { route(); })); return; }
      cleanup = window.ConsoleScreen.mount(app, name, ctx);
    });
    return function () { alive = false; if (cleanup) cleanup(); };
  }

  var loaded = {};
  function loadScripts(list) {
    return list.reduce(function (p, src) {
      return p.then(function () {
        if (loaded[src]) return loaded[src];
        loaded[src] = new Promise(function (resolve) {
          var s = document.createElement("script");
          s.src = src;
          s.onload = resolve;
          s.onerror = function () { delete loaded[src]; resolve(); };
          document.head.appendChild(s);
        });
        return loaded[src];
      });
    }, Promise.resolve());
  }

  // ---------- Security / Sites / Backups ----------

  function securityCard(row, withHead) {
    var card = h("div", { class: "card" });
    var findings = row.findings || [];
    var real = findings.filter(function (f) { return f.level !== "info"; });
    var info = findings.filter(function (f) { return f.level === "info"; });
    var st = !row.online ? ["bad", t("offline")] : real.some(function (f) { return f.level === "bad"; }) ? ["bad", t("problems_n", { n: real.length })]
      : real.length ? ["warn", t("problems_n", { n: real.length })] : ["ok", row.first ? t("sec_first") : t("all_good")];
    if (withHead) card.appendChild(h("button", { type: "button", class: "server-head head-btn", "aria-label": row.name + ". " + t("open_details"),
      onclick: function () { haptic("light"); navigate("#/problem/" + encodeURIComponent("secinfo:" + row.name)); } },
      h("div", { class: "grow" }, h("div", { class: "name", text: row.name })), pill(st[0], st[1]), h("span", { class: "chev" }, icon("right"))));
    else card.appendChild(h("div", { class: "server-head" }, h("div", { class: "grow muted", text: real.length ? t("sec_new") : "" }), pill(st[0], st[1])));
    if (!row.online) return card;
    if (real.length) {
      card.appendChild(issueList(real.map(function (f) { return [f.level, local(row.name, f.text), f.key]; })));
    }
    var facts = h("div", { class: "card-foot" });
    var fact = function (label, value, good) {
      if (value === undefined || value === null || value === "") return;
      facts.appendChild(h("span", { class: good === true ? "" : good === false ? "warn-text" : "" },
        icon(good === false ? "warn" : good === true ? "ok" : "info"), label + ": " + value));
    };
    fact(t("ssh_password"), row.ssh_password, row.ssh_password ? row.ssh_password === "no" : undefined);
    fact(t("root_login"), row.root_login, row.root_login ? row.root_login !== "yes" : undefined);
    fact(t("fail2ban"), row.fail2ban, row.fail2ban ? row.fail2ban === "active" : undefined);
    fact(t("auto_updates"), row.auto_updates, row.auto_updates ? row.auto_updates !== "off" : undefined);
    if (row.updates !== undefined && row.updates !== "") {
      var secN = parseInt(row.security_updates, 10) || 0;
      fact(t("updates"), row.updates + (secN ? " (" + secN + " " + t("sec_updates") + ")" : ""), secN ? false : undefined);
    }
    fact(t("ports"), row.ports);
    if (row.logins !== undefined) fact(t("logins"), t("logins_from", { n: row.logins, ips: row.login_ips }));
    card.appendChild(facts);
    if (info.length) {
      var il = h("ul", { class: "problems" });
      info.forEach(function (f) { il.appendChild(h("li", { class: "info-text" }, icon("info"), h("span", { text: local(row.name, f.text) }))); });
      card.appendChild(h("div", { class: "note", text: t("sec_info") }));
      card.appendChild(il);
    }
    return card;
  }

  var HEALTH_GROUPS = ["security", "sites", "backups"];

  function healthPage(tab) {
    var hidden = state.me.health_hidden || [];
    var tabs = HEALTH_GROUPS.filter(function (g) { return hidden.indexOf(g) < 0; });
    if (!tabs.length) tabs = HEALTH_GROUPS.slice();
    if (tabs.indexOf(tab) < 0) tab = tabs[0];
    var alive = true;
    var body = h("div", { "aria-busy": "true" });
    var top = topbar(t("tab_overview"), { refresh: function () { load(true); } });
    if (state.me.role !== "viewer") {
      top.appendChild(h("button", { class: "icon-btn", type: "button", "aria-label": t("health_edit"), title: t("health_edit"),
        onclick: function () { haptic("light"); editGroups(); } }, icon("sliders")));
    }
    var seg = h("div", { class: "segments", role: "tablist", "aria-label": t("tab_health") });
    tabs.forEach(function (name) {
      seg.appendChild(h("button", { type: "button", role: "tab", "aria-selected": name === tab ? "true" : "false",
        onclick: function () { if (name !== tab) { haptic("select"); location.replace("#/health/" + name); } } }, t("seg_" + name)));
    });
    if (tabs.length < 2) seg.hidden = true;
    var overall = h("div");  // the whole picture first: every open problem, from the same rules as the bot
    add(app, [top, overall, seg, body]);
    function drawOverall(d) {
      clear(overall);
      var items = (d && d.problems) || [];
      var bad = items.some(function (p) { return p.level === "bad"; });
      if (!items.length) {
        overall.appendChild(h("div", { class: "banner ok", role: "status" }, icon("ok"),
          h("div", { class: "grow" }, h("div", { class: "title", text: t("all_good") }), h("div", { class: "muted", text: t("updated", { time: hhmm(d.time) }) }))));
        return;
      }
      overall.appendChild(h("button", { type: "button", class: "banner banner-btn " + (bad ? "bad" : "warn"),
        onclick: function () { haptic("light"); navigate("#/problems"); } }, icon(bad ? "bad" : "warn"),
        h("div", { class: "grow" }, h("div", { class: "title", text: t("problems_n", { n: items.length }) }),
          h("div", { class: "muted", text: t("updated", { time: hhmm(d.time) }) })), h("span", { class: "chev" }, icon("right"))));
    }
    if (state.problems) drawOverall(state.problems); else overall.appendChild(h("div", { class: "skeleton slim", "aria-hidden": "true" }));
    api("GET", "/api/problems").then(function (d) { state.problems = d; if (alive) drawOverall(d); }, function (e) {
      if (!alive || e.status === 401) return;
      clear(overall);
      overall.appendChild(h("div", { class: "banner info", role: "status" }, icon("info"), h("div", { class: "grow" },
        h("div", { class: "title", text: t("state_unknown") }), h("div", { class: "muted", text: e.message }))));
    });

    // which groups the tab shows (for everyone using this bot; the watchdog keeps checking all of them)
    function editGroups() {
      clear(body);
      seg.hidden = true;
      var picked = HEALTH_GROUPS.filter(function (g) { return hidden.indexOf(g) < 0; });
      var list = h("div", { class: "card form" });
      HEALTH_GROUPS.forEach(function (g) {
        var sw = h("input", { type: "checkbox", class: "switch", id: "grp-" + g, checked: picked.indexOf(g) >= 0,
          onchange: function () { haptic("select"); } });
        list.appendChild(h("div", { class: "field switch-row" }, h("div", { class: "grow" },
          h("label", { for: "grp-" + g, text: t("seg_" + g) }), h("div", { class: "hint", text: t("grp_" + g + "_hint") })), sw));
      });
      var err = h("div", { class: "form-error", role: "alert" });
      body.appendChild(sectionTitle(t("health_groups")));
      body.appendChild(list);
      body.appendChild(h("p", { class: "note", text: t("health_groups_note") }));
      body.appendChild(err);
      body.appendChild(h("button", { class: "btn spaced", type: "button", onclick: function () {
        var off = HEALTH_GROUPS.filter(function (g) { return !document.getElementById("grp-" + g).checked; });
        if (off.length === HEALTH_GROUPS.length) { err.textContent = t("health_one_left"); haptic("error"); return; }
        api("POST", "/api/health/groups", { hidden: off }).then(function (r) {
          haptic("success");
          state.me.health_hidden = r.hidden;
          route();
        }, function (e) { haptic("error"); err.textContent = e.message; });
      } }, icon("ok"), t("save")));
      body.appendChild(h("button", { class: "btn secondary spaced", type: "button", onclick: function () { route(); } }, t("console_cancel")));
    }

    function draw(d) {
      clear(body);
      body.setAttribute("aria-busy", "false");
      if (tab === "security") (d.servers || []).forEach(function (row) { body.appendChild(securityCard(row, true)); });
      if (tab === "sites") {
        var sites = d.sites || [];
        if (!sites.length) { body.appendChild(h("div", { class: "card empty", text: t("none_sites") })); return; }
        sites.forEach(function (s) {
          var bad = !s.up || (s.problems && s.problems.length);
          var card = h("div", { class: "card" }, h("button", { type: "button", class: "server-head head-btn", "aria-label": s.name + ". " + t("open_details"),
            onclick: function () { haptic("light"); navigate("#/problem/" + encodeURIComponent("siteinfo:" + s.name)); } },
            h("div", { class: "grow" }, h("div", { class: "name", text: s.name }), h("div", { class: "meta", text: s.url })),
            pill(!s.up ? "bad" : bad ? "warn" : "ok", s.up ? (bad ? t("problems_n", { n: s.problems.length }) : t("site_up")) : t("site_down")),
            h("span", { class: "chev" }, icon("right"))));
          var foot = h("div", { class: "card-foot" });
          if (s.status) foot.appendChild(h("span", null, icon("globe"), "HTTP " + s.status + (typeof s.seconds === "number" ? " · " + s.seconds.toFixed(2) + " s" : "")));
          if (s.cert_days !== null && s.cert_days !== undefined) foot.appendChild(h("span", { class: s.cert_days < 14 ? "warn-text" : "" }, icon("lock"), t("cert") + ": " + t("days", { n: s.cert_days })));
          if (s.domain_days !== null && s.domain_days !== undefined) foot.appendChild(h("span", { class: s.domain_days < 30 ? "warn-text" : "" }, icon("clock"), t("domain") + ": " + t("days", { n: s.domain_days })));
          card.appendChild(foot);
          var issues = (s.issues || []).map(function (i) { return [i.level, i.text, i.key]; });
          if (issues.length) card.appendChild(issueList(issues));
          if (s.url) card.appendChild(h("button", { type: "button", class: "link-btn", onclick: function () { openLink(s.url); } },
            icon("globe"), t("open_site")));
          body.appendChild(card);
        });
      }
      if (tab === "backups") {
        var items = d.backups || [];
        if (!items.length) { body.appendChild(h("div", { class: "card empty", text: t("none_backups") })); return; }
        var list = h("div", { class: "list" });
        items.forEach(function (b) {
          var info = h("div", { class: "grow" }, h("div", { class: "label", text: b.name }),
            h("div", { class: "sub", text: [b.type, b.server, b.line].filter(Boolean).join(" · ") }));
          var issues = b.issues || [];
          var target = "bkinfo:" + b.name;
          var kids = [h("span", { class: b.fine ? "ok-text" : "warn-text" }, icon(b.fine ? "ok" : "warn")), info,
            pill(b.fine ? "ok" : "warn", t(b.fine ? "backup_fresh" : "backup_stale"))];
          issues.forEach(function (p) { info.appendChild(h("div", { class: "status-line warn-text" }, icon("warn"), p.text)); });
          if (target) {
            kids.push(h("span", { class: "chev" }, icon("right")));
            list.appendChild(h("button", { type: "button", class: "row row-btn",
              onclick: function () { haptic("light"); navigate("#/problem/" + encodeURIComponent(target)); } }, kids));
          } else list.appendChild(h("div", { class: "row" }, kids));
        });
        body.appendChild(list);
      }
    }

    function load(refresh) {
      var b = top.querySelector(".icon-btn");
      busy(b, true);
      return api("GET", "/api/" + tab + (refresh ? "?refresh=1" : "")).then(function (d) {
        state[tab] = d;
        if (!alive) return;
        if (refresh) haptic("success");
        draw(d);
      }, function (err) {
        if (!alive || err.status === 401) return;
        if (state[tab]) toast(err.message); else { clear(body); body.appendChild(errorBox(err, function () { load(false); })); }
      }).then(function () { busy(b, false); });
    }
    if (state[tab]) draw(state[tab]); else add(body, skeletons(2));
    load(false);
    return function () { alive = false; };
  }

  // ---------- Jobs ----------

  // ---------- "When": a sheet with a calendar, the time and repeats (dates are in the bot's time zone) ----------

  // value: {mode: "in", every: "30m"} | {mode: "once", date: "YYYY-MM-DD", time: "HH:MM"} |
  //        {mode: "daily", time: "HH:MM"} | {mode: "every", every: "2h"}
  function whenPayload(v) {
    if (!v) return null;
    if (v.mode === "in") return { when: v.every, every: false };
    if (v.mode === "once") return { on: v.date, when: v.time, every: false };
    if (v.mode === "daily") return { when: v.time, every: true };
    return { when: v.every, every: true };
  }
  function ymd(y, m, d) { return y + "-" + ("0" + (m + 1)).slice(-2) + "-" + ("0" + d).slice(-2); }
  function parseYmd(s) { var p = s.split("-"); return { y: +p[0], m: +p[1] - 1, d: +p[2] }; }
  function dateLabel(s) {
    var p = parseYmd(s);
    try { return new Date(Date.UTC(p.y, p.m, p.d, 12)).toLocaleDateString(t.lang, { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }); }
    catch (e) { return s; }
  }
  function durationLabel(code) {
    var m = /^(\d+)([mhd])$/.exec(code || "");
    if (!m) return code || "";
    return t("dur_" + m[2], { n: +m[1] });
  }
  function whenLabel(v) {
    if (!v) return t("when_pick");
    if (v.mode === "in") return t("in_x", { x: durationLabel(v.every) });
    if (v.mode === "once") return dateLabel(v.date) + " · " + v.time;
    if (v.mode === "daily") return t("daily_at", { time: v.time });
    return t("every_x", { x: durationLabel(v.every) });
  }

  function pickWhen(initial, ctx) {  // ctx: {today: "YYYY-MM-DD", now: "HH:MM"} from /api/jobs
    return new Promise(function (resolve) {
      var v = initial ? JSON.parse(JSON.stringify(initial)) : null;
      var mode = v && v.mode !== "in" ? v.mode : "once";
      var date = v && v.date || ctx.today;
      var time = v && v.time || (function () { var p = ctx.now.split(":"); var hh = (+p[0] + 1) % 24; return ("0" + hh).slice(-2) + ":00"; })();
      var every = v && (v.mode === "every") ? v.every : "1h";
      var view = parseYmd(date);
      var backdrop = h("div", { class: "sheet-backdrop", onclick: function (e) { if (e.target === backdrop) close(null); } });
      var sheet = h("div", { class: "sheet", role: "dialog", "aria-modal": "true", "aria-label": t("when") });
      backdrop.appendChild(sheet);
      document.body.appendChild(backdrop);
      document.body.classList.add("sheet-open");
      requestAnimationFrame(function () { backdrop.classList.add("open"); });

      function close(result) {
        backdrop.classList.remove("open");
        document.body.classList.remove("sheet-open");
        setTimeout(function () { backdrop.remove(); }, 220);
        resolve(result);
      }

      function stepper(label, value, onStep) {
        return h("div", { class: "stepper" },
          h("button", { type: "button", class: "step", "aria-label": label + " −", onclick: function () { haptic("select"); onStep(-1); } }, "−"),
          h("div", { class: "step-v", "aria-live": "polite", text: value }),
          h("button", { type: "button", class: "step", "aria-label": label + " +", onclick: function () { haptic("select"); onStep(1); } }, "+"));
      }
      function timeBlock() {
        var p = time.split(":"), hh = +p[0], mm = +p[1];
        var set = function (a, b) { time = ("0" + ((a + 24) % 24)).slice(-2) + ":" + ("0" + ((b + 60) % 60)).slice(-2); draw(); };
        return h("div", { class: "time-pick" },
          h("div", { class: "label-like", text: t("time") }),
          h("div", { class: "time-row" },
            stepper(t("hours"), ("0" + hh).slice(-2), function (d) { set(hh + d, mm); }),
            h("span", { class: "colon", text: ":" }),
            stepper(t("minutes"), ("0" + mm).slice(-2), function (d) { set(hh, mm + d * 5 - (mm % 5 && d > 0 ? mm % 5 : 0)); })),
          h("div", { class: "chips" }, ["00", "15", "30", "45"].map(function (x) {
            return h("button", { type: "button", class: "chip-toggle", "aria-pressed": time.slice(3) === x ? "true" : "false",
              onclick: function () { haptic("select"); time = time.slice(0, 3) + x; draw(); } }, ":" + x);
          })));
      }
      function calendar() {
        var first = new Date(Date.UTC(view.y, view.m, 1));
        var days = new Date(Date.UTC(view.y, view.m + 1, 0)).getUTCDate();
        var lead = (first.getUTCDay() + 6) % 7;  // Monday first
        var title;
        try { title = first.toLocaleDateString(t.lang, { month: "long", year: "numeric", timeZone: "UTC" }); } catch (e) { title = view.y + "-" + (view.m + 1); }
        title = title.charAt(0).toUpperCase() + title.slice(1);
        var t0 = parseYmd(ctx.today);
        var canPrev = view.y > t0.y || (view.y === t0.y && view.m > t0.m);
        var grid = h("div", { class: "cal-grid", role: "grid" });
        for (var w = 0; w < 7; w++) {
          var wd; try { wd = new Date(Date.UTC(2024, 0, 1 + w)).toLocaleDateString(t.lang, { weekday: "narrow", timeZone: "UTC" }); } catch (e) { wd = ""; }
          grid.appendChild(h("div", { class: "cal-wd", "aria-hidden": "true", text: wd }));
        }
        for (var i = 0; i < lead; i++) grid.appendChild(h("div", { class: "cal-pad" }));
        for (var d = 1; d <= days; d++) {
          (function (d) {
            var key = ymd(view.y, view.m, d);
            var past = key < ctx.today;
            grid.appendChild(h("button", { type: "button", class: "cal-day" + (key === ctx.today ? " today" : "") + (key === date ? " on" : ""),
              disabled: past, "aria-pressed": key === date ? "true" : "false", "aria-label": dateLabel(key),
              onclick: function () { haptic("select"); date = key; draw(); } }, String(d)));
          })(d);
        }
        return h("div", { class: "cal" },
          h("div", { class: "cal-head" },
            h("button", { type: "button", class: "icon-btn small-nav", disabled: !canPrev, "aria-label": t("prev_month"),
              onclick: function () { view.m -= 1; if (view.m < 0) { view.m = 11; view.y -= 1; } draw(); } }, icon("left")),
            h("div", { class: "cal-title", text: title }),
            h("button", { type: "button", class: "icon-btn small-nav", "aria-label": t("next_month"),
              onclick: function () { view.m += 1; if (view.m > 11) { view.m = 0; view.y += 1; } draw(); } }, icon("right"))),
          grid);
      }
      var err = h("div", { class: "form-error", role: "alert" });
      function draw() {
        clear(sheet);
        sheet.appendChild(h("div", { class: "sheet-grip", "aria-hidden": "true" }));
        sheet.appendChild(h("div", { class: "sheet-title", text: t("when") }));
        var seg = h("div", { class: "segments", role: "tablist" });
        [["once", "when_once"], ["daily", "when_daily"], ["every", "when_every"]].forEach(function (m) {
          seg.appendChild(h("button", { type: "button", role: "tab", "aria-selected": mode === m[0] ? "true" : "false",
            onclick: function () { haptic("select"); mode = m[0]; draw(); } }, t(m[1])));
        });
        sheet.appendChild(seg);
        if (mode === "once") {
          sheet.appendChild(h("div", { class: "chips quick" }, [["30m", "in"], ["2h", "in"]].map(function (q) {
            return h("button", { type: "button", class: "chip-toggle", onclick: function () { haptic("light"); close({ mode: "in", every: q[0] }); } },
              t("in_x", { x: durationLabel(q[0]) }));
          }).concat([h("button", { type: "button", class: "chip-toggle", onclick: function () {
            var p = parseYmd(ctx.today); var nd = new Date(Date.UTC(p.y, p.m, p.d + 1));
            haptic("light"); close({ mode: "once", date: ymd(nd.getUTCFullYear(), nd.getUTCMonth(), nd.getUTCDate()), time: "09:00" });
          } }, t("tomorrow_9"))])));
          sheet.appendChild(calendar());
          sheet.appendChild(timeBlock());
        } else if (mode === "daily") {
          sheet.appendChild(timeBlock());
        } else {
          sheet.appendChild(h("div", { class: "label-like", text: t("repeat_every") }));
          sheet.appendChild(h("div", { class: "chips" }, ["15m", "30m", "1h", "2h", "6h", "12h", "1d", "7d"].map(function (x) {
            return h("button", { type: "button", class: "chip-toggle", "aria-pressed": every === x ? "true" : "false",
              onclick: function () { haptic("select"); every = x; draw(); } }, durationLabel(x));
          })));
        }
        err.textContent = "";
        sheet.appendChild(err);
        sheet.appendChild(h("div", { class: "btn-row sheet-actions" },
          h("button", { type: "button", class: "btn secondary", onclick: function () { close(null); } }, t("console_cancel")),
          h("button", { type: "button", class: "btn", onclick: done }, t("done"))));
      }
      function done() {
        if (mode === "once" && date === ctx.today && time <= ctx.now) { err.textContent = t("time_past"); haptic("error"); return; }
        haptic("success");
        close(mode === "once" ? { mode: "once", date: date, time: time } : mode === "daily" ? { mode: "daily", time: time } : { mode: "every", every: every });
      }
      draw();
    });
  }

  function whenField(id, initial, getCtx, onChange) {
    var value = initial;
    var btn = h("button", { type: "button", class: "when-btn", id: id, "aria-haspopup": "dialog" });
    function paint() { clear(btn); btn.appendChild(icon("clock")); btn.appendChild(h("span", { class: "grow", text: whenLabel(value) })); btn.appendChild(icon("right")); btn.classList.toggle("empty", !value); }
    btn.addEventListener("click", function () {
      haptic("light");
      pickWhen(value, getCtx()).then(function (v) { if (v) { value = v; paint(); if (onChange) onChange(v); } });
    });
    paint();
    return { el: btn, get: function () { return value; }, set: function (v) { value = v; paint(); } };
  }

  function jobsPage() {
    var alive = true;
    var KIND_ICON = { reminder: "bell", task: "gear", command: "terminal", brief: "sun" };
    var tzNote = h("p", { class: "subtitle" });
    var list = h("div", { "aria-busy": "true" });
    var err = h("div", { class: "form-error", role: "alert" });
    var text = h("input", { type: "text", id: "job-text", maxlength: "500", autocomplete: "off", required: true, enterkeyhint: "next" });
    var ctx = { today: new Date().toISOString().slice(0, 10), now: "00:00" };
    var when = whenField("job-when", null, function () { return ctx; });
    var submit = h("button", { class: "btn", type: "submit" }, icon("plus"), t("add"));
    var form = h("form", { class: "card form", novalidate: true, onsubmit: onSubmit },
      h("div", { class: "field" }, h("label", { for: "job-text", text: t("text") }), text),
      h("div", { class: "field" }, h("label", { for: "job-when", text: t("when") }), when.el),
      err, submit);
    add(app, [topbar(t("jobs"), { refresh: function () { load(); } }), tzNote, sectionTitle(t("add_reminder")), form, sectionTitle(t("tab_jobs")), list]);

    function onSubmit(e) {
      e.preventDefault();
      err.textContent = "";
      if (!text.value.trim() || !when.get()) { err.textContent = t("job_need"); haptic("error"); return; }
      submit.disabled = true;
      var body = whenPayload(when.get());
      body.text = text.value.trim();
      api("POST", "/api/jobs", body).then(function () {
        haptic("success");
        text.value = ""; when.set(null);
        if (document.activeElement) document.activeElement.blur();
        return load();
      }, function (e2) { if (e2.status !== 401) { haptic("error"); err.textContent = e2.message; } })
        .then(function () { submit.disabled = false; });
    }

    function draw(d) {
      clear(list);
      list.setAttribute("aria-busy", "false");
      tzNote.textContent = t("times_in", { tz: d.tz || "UTC" });
      if (d.today) { ctx.today = d.today; ctx.now = d.now_time || "00:00"; }
      var jobs = d.jobs || [];
      if (!jobs.length) { list.appendChild(h("div", { class: "card empty", text: t("jobs_empty") })); return; }
      var box = h("div", { class: "list" });
      jobs.forEach(function (j) {
        var kind = KIND_ICON[j.kind] ? j.kind : "task";
        var info = h("div", { class: "grow" }, h("div", { class: "label", text: kind === "brief" ? t("kind_brief") : j.text }),
          h("div", { class: "sub", text: [kind === "brief" ? "" : t("kind_" + kind), j.when, j.next_text ? t("next", { t: j.next_text }) : ""].filter(Boolean).join(" · ") }));
        var edit = h("button", { type: "button", class: "grow plain-btn job-open", "aria-label": t("edit") + ": " + j.text,
          onclick: function () { haptic("light"); navigate("#/jobs/" + encodeURIComponent(j.id)); } },
          h("span", { class: "muted" }, icon(KIND_ICON[kind])), info, h("span", { class: "chev" }, icon("right")));
        box.appendChild(h("div", { class: "row" }, edit,
          h("button", { class: "small-btn danger", type: "button", "aria-label": t("delete") + ": " + j.text,
            onclick: function () { remove(j); } }, icon("trash"))));
      });
      list.appendChild(box);
    }

    function remove(j) {
      var label = j.text.length > 60 ? j.text.slice(0, 57) + "…" : j.text;
      confirmDialog(t("delete_q", { t: label })).then(function (yes) {
        if (!yes) return;
        api("DELETE", "/api/jobs/" + encodeURIComponent(j.id), {}).then(function () { haptic("success"); load(); },
          function (e) { if (e.status !== 401) { haptic("error"); toast(e.message); } });
      });
    }

    function load() {
      return api("GET", "/api/jobs").then(function (d) { state.jobs = d; if (alive) draw(d); }, function (e) {
        if (!alive || e.status === 401) return;
        clear(list); list.appendChild(errorBox(e, load));
      });
    }
    if (state.jobs) draw(state.jobs); else add(list, skeletons(1));
    load();
    return function () { alive = false; };
  }

  function jobPage(id) {
    var alive = true;
    var body = h("div");
    add(app, [topbar(t("edit_job"), { back: true }), body]);
    var KIND_ICON = { reminder: "bell", task: "gear", command: "terminal", brief: "sun" };

    function draw(j) {
      clear(body);
      if (!j) { body.appendChild(h("div", { class: "card empty", text: t("job_gone") })); return; }
      var kind = KIND_ICON[j.kind] ? j.kind : "task";
      body.appendChild(h("div", { class: "card" }, h("div", { class: "server-head" }, h("span", { class: "muted big-icon" }, icon(KIND_ICON[kind])),
        h("div", { class: "grow" }, h("div", { class: "name", text: kind === "brief" ? t("kind_brief") : t("kind_" + kind) }),
          h("div", { class: "meta", text: [j.when, j.next_text ? t("next", { t: j.next_text }) : ""].filter(Boolean).join(" · ") })))));
      if (!j.editable) {
        body.appendChild(h("p", { class: "note", text: t("brief_in_settings") }));
        body.appendChild(h("button", { class: "btn secondary spaced", type: "button", onclick: function () { navigate("#/settings"); } },
          icon("sliders"), t("settings")));
        return;
      }
      var err = h("div", { class: "form-error", role: "alert" });
      var text = h("textarea", { id: "edit-text", maxlength: "500", rows: "4", required: true });
      text.value = j.text;
      var initial = j.daily ? { mode: "daily", time: j.daily } : j.every ? { mode: "every", every: j.when_input }
        : { mode: "once", date: j.next_date, time: j.next_time };
      var changed = false;
      var when = whenField("edit-when", initial, function () { return ctx; }, function () { changed = true; });
      var save = h("button", { class: "btn", type: "submit" }, icon("ok"), t("save"));
      body.appendChild(h("form", { class: "card form", novalidate: true, onsubmit: function (e) {
        e.preventDefault();
        err.textContent = "";
        save.disabled = true;
        // an untouched time is sent as it was, so the bot keeps the run where it is
        var body = changed ? whenPayload(when.get()) : { when: j.when_input, every: !!j.repeat };
        body.text = text.value.trim();
        api("POST", "/api/jobs/" + encodeURIComponent(j.id), body)
          .then(function () { haptic("success"); toast(t("saved_job")); state.jobs = null; goBack(); },
            function (e2) { if (e2.status !== 401) { haptic("error"); err.textContent = e2.message; } })
          .then(function () { save.disabled = false; });
      } },
        h("div", { class: "field" }, h("label", { for: "edit-text", text: t("text") }), text),
        h("div", { class: "field" }, h("label", { for: "edit-when", text: t("when") }), when.el),
        err, save));
      body.appendChild(h("button", { class: "btn btn-quiet", type: "button", onclick: function () {
        confirmDialog(t("delete_q", { t: j.text.length > 60 ? j.text.slice(0, 57) + "…" : j.text })).then(function (yes) {
          if (!yes) return;
          api("DELETE", "/api/jobs/" + encodeURIComponent(j.id), {}).then(function () { haptic("success"); state.jobs = null; goBack(); },
            function (e) { if (e.status !== 401) { haptic("error"); toast(e.message); } });
        });
      } }, icon("trash"), t("delete")));
    }

    var ctx = { today: new Date().toISOString().slice(0, 10), now: "00:00" };
    api("GET", "/api/jobs").then(function (d) {
      if (!alive) return;
      if (d.today) { ctx.today = d.today; ctx.now = d.now_time || "00:00"; }
      draw((d.jobs || []).filter(function (x) { return String(x.id) === String(id); })[0]);
    }, function (e) { if (alive && e.status !== 401) { clear(body); body.appendChild(errorBox(e, function () { route(); })); } });
    add(body, skeletons(2));
    return function () { alive = false; };
  }

  // ---------- Settings ----------

  function settingsPage() {
    var alive = true;
    var me = state.me;
    var admin = me.role !== "viewer";
    var body = h("div", { "aria-busy": "true" });
    var top = topbar(t("settings"), { refresh: function () { load(); } });
    add(app, [top, body]);
    var data = null, teamData = null, dirty = {};

    var GROUPS = [["g_main", ["NAME", "OWNER_NAME", "LANG", "TZ", "BRIEF", "WORKDIR"]],
      ["g_agent", ["AGENT_MODE", "TIMEOUT", "PROGRESS"]],
      ["g_watch", ["WATCH_INTERVAL", "WATCH_DISK_PCT"]],
      ["g_app", ["WEB_CONSOLE", "WEB_CONSOLE_AUTO", "CONSOLE_KEEP", "CONSOLE_TMUX"]]];

    function draw() {
      clear(body);
      body.setAttribute("aria-busy", "false");
      // team
      body.appendChild(sectionTitle(t("team"), teamData ? String(teamData.members.length) : ""));
      var list = h("div", { class: "list" });
      (teamData ? teamData.members : []).forEach(function (m) {
        var kids = [h("span", { class: "avatar", "aria-hidden": "true", text: (m.name || m.uid).charAt(0).toUpperCase() }),
          h("div", { class: "grow" }, h("div", { class: "label", text: (m.name || "id " + m.uid) + (m.me ? " · " + t("you") : "") }),
            h("div", { class: "sub", text: "id " + m.uid + (m.via ? " · " + t("added_" + m.via) : "") })),
          pill(m.role === "admin" ? "info" : "ok", t("role_" + m.role))];
        if (admin) {
          kids.push(h("span", { class: "chev" }, icon("right")));
          list.appendChild(h("button", { type: "button", class: "row row-btn", onclick: function () { memberMenu(m); } }, kids));
        } else list.appendChild(h("div", { class: "row" }, kids));
      });
      if (admin) {
        list.appendChild(h("button", { type: "button", class: "row row-btn accent-text",
          onclick: function () { haptic("light"); navigate("#/settings/add"); } },
          h("span", { class: "avatar add", "aria-hidden": "true" }, icon("plus")), h("div", { class: "grow label", text: t("add_person") })));
      }
      body.appendChild(list);
      if (teamData && teamData.invites.length) body.appendChild(h("p", { class: "note", text: t("invites_open", { n: teamData.invites.length }) }));
      // the look: kept on this device, applied at once (no restart)
      body.appendChild(sectionTitle(t("appearance")));
      var mode = window.VPSD_THEME ? window.VPSD_THEME.saved() : "system";
      var themeSeg = h("div", { class: "segments", role: "radiogroup", "aria-label": t("theme") });
      ["system", "light", "dark"].forEach(function (m) {
        themeSeg.appendChild(h("button", { type: "button", role: "radio", "aria-checked": m === mode ? "true" : "false",
          "aria-selected": m === mode ? "true" : "false", onclick: function () {
            haptic("select");
            if (window.VPSD_THEME) window.VPSD_THEME.set(m);
            applyTheme();
            Array.prototype.forEach.call(themeSeg.children, function (b) {
              var on = b === this; b.setAttribute("aria-selected", on ? "true" : "false"); b.setAttribute("aria-checked", on ? "true" : "false");
            }, this);
          } }, t("theme_" + m)));
      });
      body.appendChild(h("div", { class: "card" }, h("div", { class: "label-like", text: t("theme") }), themeSeg));
      if (!data) return;
      // editable settings
      GROUPS.forEach(function (g) {
        body.appendChild(sectionTitle(t(g[0])));
        var card = h("div", { class: "card form" });
        g[1].forEach(function (key) {
          var f = data.fields.filter(function (x) { return x.key === key; })[0];
          if (f) card.appendChild(field(f));
        });
        body.appendChild(card);
      });
      // fixed ones
      var fixed = Object.keys(data.fixed || {});
      if (fixed.length) {
        body.appendChild(sectionTitle(t("g_fixed")));
        var fl = h("div", { class: "list" });
        fixed.forEach(function (k) {
          fl.appendChild(h("div", { class: "row" }, h("div", { class: "grow" }, h("div", { class: "label mono", text: k }),
            h("div", { class: "sub mono", text: data.fixed[k] }))));
        });
        body.appendChild(fl);
        body.appendChild(h("p", { class: "note", text: t("fixed_hint", { dir: data.conf_dir }) }));
      }
      body.appendChild(h("div", { class: "list spaced" },
        h("div", { class: "row" }, h("div", { class: "grow label", text: t("signed_in") }),
          h("div", { class: "value", text: me.via === "telegram" ? t("via_telegram") : t("via_cookie") }))));
      if (me.via !== "telegram") {
        body.appendChild(h("button", { class: "btn secondary spaced", type: "button", onclick: function () {
          api("POST", "/api/logout", {}).then(function () { state.me = null; showAuth(t("signed_out")); }, function (e) { toast(e.message); });
        } }, icon("logout"), t("sign_out")));
      }
      saveButton();
    }

    function value(key) { return Object.prototype.hasOwnProperty.call(dirty, key) ? dirty[key] : data.values[key]; }
    function change(key, v) {
      if (JSON.stringify(v) === JSON.stringify(data.values[key])) delete dirty[key]; else dirty[key] = v;
      saveButton();
    }

    function field(f) {
      var id = "set-" + f.key;
      var label = h("label", { for: id, text: t("set_" + f.key) });
      var hint = t("set_" + f.key + "_hint");
      var hintEl = hint && hint !== "set_" + f.key + "_hint" ? h("div", { class: "hint", text: hint }) : null;
      var v = value(f.key), input;
      var off = !admin;
      if (f.kind === "bool") {
        input = h("input", { type: "checkbox", id: id, class: "switch", checked: !!v, disabled: off,
          onchange: function () { haptic("select"); change(f.key, input.checked); } });
        return h("div", { class: "field switch-row" }, h("div", { class: "grow" }, label, hintEl), input);
      }
      if (f.kind === "choice") {
        input = h("select", { id: id, disabled: off, onchange: function () {
          var was = value(f.key);
          if (f.key === "AGENT_MODE" && input.value === "full") {  // a real change of trust: asked once more
            confirmDialog(t("agent_full_q")).then(function (yes) {
              if (yes) change(f.key, "full"); else input.value = was;
            });
            return;
          }
          change(f.key, input.value);
        } },
          f.choices.map(function (c) {
            var label = t("opt_" + f.key + "_" + c);
            return h("option", { value: c, selected: c === v, text: label.indexOf("opt_") === 0 ? t("lang_" + c) : label });
          }));
        return h("div", { class: "field" }, label, input, hintEl);
      }
      if (f.kind === "tz") {
        // grouped by the offset from UTC, west to east; the value is the IANA name the bot needs
        var groups = f.groups || [];
        input = h("select", { id: id, disabled: off, onchange: function () { haptic("select"); change(f.key, input.value); } },
          h("option", { value: "", selected: !v, text: t("tz_server") }),
          groups.map(function (g) {
            return h("optgroup", { label: g.offset }, g.zones.map(function (z) {
              var parts = z.split("/");
              var city = parts[parts.length - 1].replace(/_/g, " ");
              return h("option", { value: z, selected: z === v, text: g.offset + " · " + city + (parts.length > 1 ? " (" + parts[0] + ")" : "") });
            }));
          }));
        return h("div", { class: "field" }, label, input, hintEl);
      }
      var numeric = f.kind === "int";
      input = h("input", { type: numeric ? "number" : "text", id: id, value: v === null || v === undefined ? "" : String(v),
        inputmode: numeric ? "numeric" : null, min: numeric ? f.min : null, max: numeric ? f.max : null, disabled: off,
        autocomplete: "off", autocapitalize: "off", spellcheck: "false",
        oninput: function () {
          var raw = input.value;
          change(f.key, numeric ? (raw === "" ? raw : Number(raw)) : raw);
        } });
      var unit = f.unit ? h("span", { class: "unit", text: t("unit_" + f.unit) }) : null;
      return h("div", { class: "field" }, label, unit ? h("div", { class: "with-unit" }, input, unit) : input, hintEl,
        h("div", { class: "form-error", id: id + "-err", role: "alert" }));
    }

    var saveBtn = null;
    function saveButton() {
      var n = Object.keys(dirty).length;
      if (!admin) return;
      var label = n ? t("save_n", { n: n }) : null;
      if (setMainButton(label, save)) return;
      if (!saveBtn) {
        saveBtn = h("button", { class: "btn sticky-save", type: "button", onclick: save });
        document.body.appendChild(saveBtn);
      }
      saveBtn.textContent = label || "";
      saveBtn.hidden = !n;
    }

    function save() {
      var keys = Object.keys(dirty);
      if (!keys.length) return;
      confirmDialog(t("save_q")).then(function (yes) {
        if (!yes || !alive) return;
        Array.prototype.forEach.call(body.querySelectorAll(".form-error"), function (e) { e.textContent = ""; });
        api("POST", "/api/settings", { changes: dirty }).then(function () {
          haptic("success");
          dirty = {};
          setMainButton(null);
          if (saveBtn) saveBtn.hidden = true;
          waitRestart();
        }, function (err) {
          haptic("error");
          var fields = (err.data && err.data.fields) || {};
          Object.keys(fields).forEach(function (k) {
            var el = document.getElementById("set-" + k + "-err");
            if (el) el.textContent = fields[k];
          });
          toast(Object.keys(fields).length ? t("fix_fields") : err.message);
        });
      });
    }

    function waitRestart() {
      clear(body);
      body.appendChild(h("div", { class: "center", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }),
        h("h2", { text: t("restarting") }), h("p", { class: "muted", text: t("restarting_sub") })));
      var tries = 0;
      (function ping() {
        setTimeout(function () {
          if (!alive) return;
          tries++;
          api("GET", "/api/me").then(function (m) {
            state.me = m; me = m;
            if (m.lang && m.lang !== t.lang) t = window.I18N.make(m.lang);
            if (m.bot) document.title = m.bot;
            buildTabs();
            toast(t("saved"));
            load();
          }, function () { if (tries < 200) ping(); });
        }, tries ? 2000 : 3500);
      })();
    }

    function memberMenu(m) {
      haptic("light");
      var other = m.role === "admin" ? "viewer" : "admin";
      var buttons = [{ id: "role", type: "default", text: t(other === "admin" ? "make_admin" : "make_viewer") },
        { id: "del", type: "destructive", text: t("remove_person") }, { type: "cancel" }];
      var act = function (id) {
        if (id === "role") {
          api("POST", "/api/team/" + m.uid, { role: other }).then(function () { haptic("success"); load(); },
            function (e) { haptic("error"); toast(e.message); });
        } else if (id === "del") {
          confirmDialog(t("remove_person_q", { name: m.name || m.uid })).then(function (yes) {
            if (!yes) return;
            api("DELETE", "/api/team/" + m.uid, {}).then(function () { haptic("success"); load(); },
              function (e) { haptic("error"); toast(e.message); });
          });
        }
      };
      if (tgAtLeast("6.2") && TG.showPopup) {
        try { TG.showPopup({ title: m.name || "id " + m.uid, message: t("role_" + m.role), buttons: buttons }, act); return; } catch (e) { /* below */ }
      }
      var choice = window.prompt((m.name || m.uid) + "\n1 — " + buttons[0].text + "\n2 — " + buttons[1].text);
      if (choice === "1") act("role"); else if (choice === "2") act("del");
    }

    function load() {
      var b = top.querySelector(".icon-btn");
      busy(b, true);
      Promise.all([api("GET", "/api/settings"), api("GET", "/api/team")]).then(function (res) {
        if (!alive) return;
        data = res[0]; teamData = res[1]; dirty = {};
        draw();
      }, function (err) {
        if (!alive || err.status === 401) return;
        clear(body); body.appendChild(errorBox(err, load));
      }).then(function () { busy(b, false); });
    }
    add(body, skeletons(3));
    load();
    return function () { alive = false; if (saveBtn) saveBtn.remove(); };
  }

  function teamAddPage() {
    var alive = true;
    var role = "viewer";
    var body = h("div");
    add(app, [topbar(t("add_person"), { back: true }), body]);
    var invite = null, invites = [];

    function draw() {
      clear(body);
      var seg = h("div", { class: "segments", role: "tablist", "aria-label": t("role") });
      ["viewer", "admin"].forEach(function (r) {
        seg.appendChild(h("button", { type: "button", role: "tab", "aria-selected": r === role ? "true" : "false",
          onclick: function () { haptic("select"); role = r; invite = null; draw(); } }, t("role_" + r)));
      });
      body.appendChild(sectionTitle(t("role")));
      body.appendChild(seg);
      body.appendChild(h("p", { class: "note role-note", text: t("role_" + role + "_about") }));

      // 1. invite link
      body.appendChild(sectionTitle(t("way_invite")));
      var c1 = h("div", { class: "card" }, h("p", { class: "about", text: t("way_invite_about") }));
      if (invite) {
        c1.appendChild(h("div", { class: "invite-link mono", tabindex: "0", text: invite.link }));
        c1.appendChild(h("div", { class: "btn-row" },
          h("button", { class: "btn", type: "button", onclick: function () { share(invite.link); } }, icon("send"), t("share")),
          h("button", { class: "btn secondary", type: "button", onclick: function () { copy(invite.link); } }, icon("copy"), t("copy"))));
        c1.appendChild(h("div", { class: "hint", text: t("invite_once") }));
      } else {
        c1.appendChild(h("button", { class: "btn", type: "button", onclick: makeInvite }, icon("link"), t("make_invite")));
      }
      body.appendChild(c1);

      // 2. from contacts
      body.appendChild(sectionTitle(t("way_contact")));
      body.appendChild(h("div", { class: "card" }, h("p", { class: "about", text: t("way_contact_about") }),
        h("button", { class: "btn secondary", type: "button", onclick: pick }, icon("user"), t("choose_contact"))));

      // 3. by id
      body.appendChild(sectionTitle(t("way_id")));
      var uid = h("input", { type: "text", id: "add-uid", inputmode: "numeric", maxlength: "16", autocomplete: "off", placeholder: "123456789" });
      var name = h("input", { type: "text", id: "add-name", maxlength: "60", autocomplete: "off" });
      var err = h("div", { class: "form-error", role: "alert" });
      body.appendChild(h("form", { class: "card", novalidate: true, onsubmit: function (e) {
        e.preventDefault();
        err.textContent = "";
        if (!/^[1-9][0-9]{0,15}$/.test(uid.value.trim())) { err.textContent = t("id_bad"); return; }
        api("POST", "/api/team", { uid: uid.value.trim(), role: role, name: name.value.trim() }).then(function () {
          haptic("success"); toast(t("person_added")); goBack();
        }, function (e2) { haptic("error"); err.textContent = e2.message; });
      } }, h("p", { class: "about", text: t("way_id_about") }),
        h("div", { class: "field" }, h("label", { for: "add-uid", text: t("tg_id") }), uid),
        h("div", { class: "field" }, h("label", { for: "add-name", text: t("name_optional") }), name), err,
        h("button", { class: "btn", type: "submit" }, icon("plus"), t("add"))));

      if (invites.length) {
        body.appendChild(sectionTitle(t("open_invites"), String(invites.length)));
        var l = h("div", { class: "list" });
        invites.forEach(function (inv) {
          l.appendChild(h("div", { class: "row" }, h("div", { class: "grow" }, h("div", { class: "label", text: t("role_" + inv.role) }),
            h("div", { class: "sub", text: t("valid_until", { time: hhmm(inv.expires) }) })),
            h("button", { class: "small-btn danger", type: "button", onclick: function () {
              api("DELETE", "/api/team/invite/" + inv.id, {}).then(function () { haptic("success"); loadInvites(); }, function (e) { toast(e.message); });
            } }, icon("trash"), h("span", { text: t("revoke") }))));
        });
        body.appendChild(l);
      }
    }

    function makeInvite() {
      api("POST", "/api/team/invite", { role: role }).then(function (res) {
        haptic("success");
        invite = res;
        loadInvites();
      }, function (e) { haptic("error"); toast(e.message); });
    }
    function share(link) {
      var url = "https://t.me/share/url?url=" + encodeURIComponent(link) + "&text=" + encodeURIComponent(t("invite_text", { bot: state.me.bot }));
      if (IN_TG && TG.openTelegramLink) { try { TG.openTelegramLink(url); return; } catch (e) { /* below */ } }
      if (navigator.share) { navigator.share({ url: link }).catch(function () {}); return; }
      copy(link);
    }
    function copy(text) {
      var done = function () { haptic("success"); toast(t("copied")); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () { toast(text); });
      else toast(text);
    }
    function pick() {
      api("POST", "/api/team/pick", { role: role }).then(function () {
        haptic("success");
        if (IN_TG) {
          confirmDialog(t("pick_sent_q")).then(function (yes) { if (yes) TG.close(); });
        } else toast(t("pick_sent"));
      }, function (e) { haptic("error"); toast(e.message); });
    }
    function loadInvites() {
      api("GET", "/api/team").then(function (d) { if (!alive) return; invites = d.invites || []; draw(); }, function () { draw(); });
    }
    draw();
    loadInvites();
    return function () { alive = false; };
  }

  // ---------- auth / boot ----------

  function showAuth(extra) {
    if (screen.cleanup) { try { screen.cleanup(); } catch (e) { /* ignore */ } screen.cleanup = null; }
    state.me = null;
    tabbar.hidden = true;
    document.body.classList.add("no-tabs");
    document.body.classList.remove("console-mode");
    setBack(false);
    setMainButton(null);
    clear(app);
    var box = h("div", { class: "center" }, h("span", { class: "big-icon" }, icon("lock")),
      h("h2", { text: t("auth_title") }), extra ? h("p", { text: extra }) : null, h("p", { text: IN_TG ? t("auth_tg") : t("auth_browser") }));
    if (IN_TG) box.appendChild(h("button", { class: "btn", type: "button", onclick: function () { TG.close(); } }, t("close")));
    app.appendChild(box);
  }

  // a language other than English / Russian lives in its own small file, fetched only when chosen
  function loadLang(lang) {
    if (!lang || window.I18N.strings[lang] || window.I18N.known.indexOf(lang) < 0) return Promise.resolve();
    return new Promise(function (resolve) {
      var s = document.createElement("script");
      s.src = "lang/" + lang + ".js";
      s.onload = s.onerror = function () { resolve(); };
      document.head.appendChild(s);
    });
  }

  function boot() {
    clear(app);
    app.appendChild(h("div", { class: "center", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("p", { text: t("loading") })));
    api("GET", "/api/me").then(function (me) {
      state.me = me;
      return loadLang(me.lang).then(function () { return me; });
    }).then(function (me) {
      if (!me) return;
      if (me.lang && me.lang !== t.lang) t = window.I18N.make(me.lang);
      document.documentElement.lang = t.lang;
      if (me.bot) document.title = me.bot;  // the owner's name for the bot, not "VPS Director"
      buildTabs();
      route();
    }, function (err) {
      if (err.status === 401) return;
      clear(app);
      app.appendChild(errorBox(err, boot));
    });
  }

  document.body.classList.toggle("browser", !IN_TG);
  if (TG) {
    try {
      TG.ready();
      TG.expand();
      if (tgAtLeast("7.7") && TG.disableVerticalSwipes) TG.disableVerticalSwipes();
      applyTheme();
      TG.onEvent("themeChanged", applyTheme);
    } catch (e) { /* an old client */ }
  }
  window.addEventListener("hashchange", route);
  boot();
})();
