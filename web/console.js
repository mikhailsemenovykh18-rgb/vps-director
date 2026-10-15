/* VPS Director Mini App — the web console screen (xterm.js ↔ /ws/console) and the command assistant bar.

   Flow (see vps_director/console.py): POST /api/console {server} → the bot asks "Allow?" in Telegram →
   poll GET /api/console/<id> until "allowed" → open the WebSocket with the one-time ticket within 60 s.
   Keystrokes go out as {"t":"in","d":…}, the window size as {"t":"resize","cols","rows"}; output comes
   back as binary frames. The assistant (assistant.js) only suggests: nothing is ever sent without a
   key press or a tap by the user. */
(function () {
  "use strict";

  var VENDOR = "vendor/xterm/";
  var loading = null;

  function loadXterm() {
    if (window.Terminal && window.FitAddon) return Promise.resolve();
    if (loading) return loading;
    function script(src) {
      return new Promise(function (resolve, reject) {
        var s = document.createElement("script");
        s.src = src;
        s.onload = resolve;
        s.onerror = function () { reject(new Error("cannot load " + src)); };
        document.head.appendChild(s);
      });
    }
    var css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = VENDOR + "xterm.css";
    document.head.appendChild(css);
    loading = script(VENDOR + "xterm.js").then(function () {
      return Promise.all([script(VENDOR + "addon-fit.js"), script(VENDOR + "addon-canvas.js")]);
    });
    loading.catch(function () { loading = null; });
    return loading;
  }

  var CLOSE_TEXT = { 4000: "console_closed_idle", 4001: "console_closed_limit", 1000: "console_closed_exit", 1001: "console_closed_bot",
    4003: "console_closed_away" };
  // codes after which the console is over; anything else (1006: the network, Telegram put away) → reconnect
  var FINAL = { 1000: 1, 1001: 1, 4000: 1, 4001: 1, 4003: 1 };
  var RESUME_KEY = "vpsd-console-resume";
  function resumeGet(server) {
    try { return (JSON.parse(localStorage.getItem(RESUME_KEY) || "{}") || {})[server] || null; } catch (e) { return null; }
  }
  function resumeSet(server, value) {
    try {
      var all = JSON.parse(localStorage.getItem(RESUME_KEY) || "{}") || {};
      if (value) all[server] = value; else delete all[server];
      localStorage.setItem(RESUME_KEY, JSON.stringify(all));
    } catch (e) { /* private mode: no resume after closing the app, reconnects still work while it is open */ }
  }
  // Termux's default extra-keys layout, two rows of seven; null = a modifier that sticks for one key
  var KEYS = [
    [["Esc", "\x1b"], ["/", "/"], ["-", "-"], ["Home", "\x1b[H"], ["↑", "\x1b[A"], ["End", "\x1b[F"], ["PgUp", "\x1b[5~"]],
    [["Tab", "\t"], ["Ctrl", null], ["Alt", null], ["←", "\x1b[D"], ["↓", "\x1b[B"], ["→", "\x1b[C"], ["PgDn", "\x1b[6~"]]
  ];
  var FONT_MIN = 9, FONT_MAX = 22, FONT_KEY = "vpsd-term-font";

  // The line being typed, as far as we can follow it from the keystrokes alone. Anything we cannot
  // follow (arrows, shell Tab completion, Ctrl+R …) makes it "unknown" until the next Enter, and the
  // assistant goes quiet rather than guess.
  function LineModel() { this.text = ""; this.known = true; }
  LineModel.prototype.feed = function (data, onCommit) {
    if (data.charAt(0) === "\x1b") { if (data !== "\x1b") this.known = false; return; }
    for (var i = 0; i < data.length; i++) {
      var ch = data.charAt(i), code = data.charCodeAt(i);
      if (ch === "\r" || ch === "\n") {
        if (this.known && this.text.trim() && onCommit) onCommit(this.text.trim());
        this.text = ""; this.known = true;
      } else if (code === 0x7f || code === 0x08) {
        this.text = this.text.slice(0, -1);
      } else if (code === 0x15 || code === 0x03) {   // Ctrl+U, Ctrl+C
        this.text = ""; this.known = true;
      } else if (code === 0x17) {                    // Ctrl+W
        this.text = this.text.replace(/\S+\s*$/, "");
      } else if (code < 0x20) {
        this.known = false;                          // Tab completion, Ctrl+R, Ctrl+A …
      } else {
        this.text += ch;
      }
    }
  };

  function mount(app, name, c) {
    var h = c.h, t = c.t, icon = c.icon;
    var alive = true, timers = [], ws = null, term = null, fit = null, pending = null, ctrl = false, alt = false;
    var resumeInfo = resumeGet(name), retry = 0, retryTimer = null, ending = false;
    var model = new LineModel();
    var assistant = null, current = null;
    var disposers = [];

    var stateDot = h("span", { class: "dot", "aria-hidden": "true" });
    var stateText = h("span", { text: t("console_connecting") });
    var top = h("div", { class: "console-top" },
      h("button", { class: "icon-btn", type: "button", "aria-label": t("back"), onclick: c.goBack }, icon("left")),
      h("div", { class: "title" }, t("console") + " · " + name),
      h("div", { class: "console-state", role: "status" }, stateDot, stateText),
      h("div", { class: "font-btns" },
        h("button", { class: "icon-btn small", type: "button", "aria-label": t("font_smaller"), onclick: function () { zoom(-1); } }, "A−"),
        h("button", { class: "icon-btn small", type: "button", "aria-label": t("font_bigger"), onclick: function () { zoom(1); } }, "A+"),
        h("button", { class: "icon-btn small", type: "button", "aria-label": t("console_end"), title: t("console_end"), onclick: endConsole }, icon("close"))));
    var termEl = h("div", { class: "term", "aria-label": t("console") + " " + name });
    var overlay = h("div", { class: "console-overlay" });
    var wrap = h("div", { class: "term-wrap" }, termEl, overlay);
    var assist = h("div", { class: "assist", role: "toolbar", "aria-label": "Assistant", "data-hint": t("accept_hint") });
    var keys = h("div", { class: "keys", role: "toolbar", "aria-label": "Keys" });
    var root = h("div", { class: "console" }, top, wrap, assist, keys);
    app.appendChild(root);

    // -- the visible height follows the on-screen keyboard --
    function viewport() {
      var vv = window.visualViewport;
      var height = vv ? vv.height : window.innerHeight;
      root.style.setProperty("--vvh", height + "px");
      var kbOpen = vv && window.innerHeight - vv.height > 120;
      root.style.setProperty("--kb-safe", kbOpen ? "0px" : "var(--safe-bottom)");
      refit();
    }
    function on(target, ev, fn) { if (target) { target.addEventListener(ev, fn); disposers.push(function () { target.removeEventListener(ev, fn); }); } }
    on(window.visualViewport, "resize", viewport);
    on(window, "resize", viewport);
    if (c.IN_TG && c.TG.onEvent) {
      c.TG.onEvent("viewportChanged", viewport);
      disposers.push(function () { c.TG.offEvent("viewportChanged", viewport); });
    }
    viewport();

    function setState(kind, text) {
      stateDot.className = "dot " + (kind === "ok" ? "up" : kind === "bad" ? "bad" : "");
      stateText.textContent = text;
    }

    function showOverlay(opts) {
      c.clear(overlay);
      overlay.hidden = false;
      overlay.className = "console-overlay" + (opts.bar ? " bar" : "");
      var inner = h("div", { class: "inner" });
      if (opts.spinner) inner.appendChild(h("span", { class: "spinner", "aria-hidden": "true" }));
      if (opts.icon) inner.appendChild(h("span", { class: "big-icon " + (opts.iconClass || "") }, icon(opts.icon)));
      if (opts.title) inner.appendChild(h("h2", { text: opts.title }));
      if (opts.text) inner.appendChild(h("p", { text: opts.text }));
      (opts.buttons || []).forEach(function (b) {
        inner.appendChild(h("button", { class: "btn" + (b.secondary ? " secondary" : ""), type: "button", onclick: b.fn }, b.text));
      });
      overlay.appendChild(inner);
    }
    function hideOverlay() { overlay.hidden = true; c.clear(overlay); }

    var againBtn = function () { return { text: t("console_again"), fn: function () { c.haptic("medium"); start(); } }; };
    var backBtn = function () { return { text: t("back"), secondary: true, fn: c.goBack }; };

    // -- back into a console that kept running (no Allow: it was allowed when it opened) --
    function resume(auto) {
      if (!resumeInfo) { start(); return; }
      setState("wait", t(auto ? "console_reconnecting" : "console_connecting"));
      if (!auto) showOverlay({ spinner: true, title: t("console_connecting") });
      c.api("POST", "/api/console/" + encodeURIComponent(resumeInfo.id) + "/resume", { token: resumeInfo.resume }).then(function (res) {
        if (!alive) return;
        retry = 0;
        connect(res.ws);
      }, function (err) {
        if (!alive || err.status === 401) return;
        if (err.status === 404 || err.status === 403) {  // it ended (or was taken by a newer token): a new one
          resumeInfo = null; resumeSet(name, null);
          if (auto) {
            setState("bad", t("console_closed"));
            showOverlay({ icon: "info", title: t("console_closed"), text: t("console_closed_gone"), buttons: [againBtn(), backBtn()] });
          } else start();
          return;
        }
        scheduleReconnect();  // no network yet: try again
      });
    }

    function scheduleReconnect() {
      if (!alive || retryTimer || ending) return;
      var wait = Math.min(10000, 1000 * Math.pow(2, retry++));
      setState("wait", t("console_reconnecting"));
      showOverlay({ bar: true, spinner: true, title: t("console_reconnecting"), text: t("console_reconnect_hint") });
      retryTimer = setTimeout(function () { retryTimer = null; resume(true); }, wait);
    }
    function reconnectNow() {
      if (!alive || ws || !resumeInfo || ending) return;
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
      resume(true);
    }
    on(window, "online", reconnectNow);
    on(document, "visibilitychange", function () { if (!document.hidden) reconnectNow(); });

    function endConsole() {
      c.confirm(t("console_end_q")).then(function (yes) {
        if (!yes) return;
        ending = true;
        sendJson({ t: "close" });
        resumeSet(name, null); resumeInfo = null;
        if (!ws) c.goBack();
      });
    }

    // -- step 1 and 2: ask, wait for Allow --
    function start() {
      closeSocket(1000);
      setState("wait", t("console_ask"));
      showOverlay({ spinner: true, title: t("console_ask"), text: t("console_ask_hint"),
        buttons: [{ text: t("console_cancel"), secondary: true, fn: c.goBack }] });
      loadXterm().catch(function () { /* reported when connecting */ });
      c.api("POST", "/api/console", { server: name }).then(function (res) {
        if (!alive) return;
        pending = res.pending;
        c.haptic("medium");
        poll(Date.now());
      }, function (err) {
        if (!alive || err.status === 401) return;
        c.haptic("error");
        setState("bad", t("console_closed"));
        showOverlay({ icon: err.status === 403 ? "lock" : "warn", title: t("console_closed"), text: err.message, buttons: [backBtn()] });
      });
    }

    function poll(since) {
      timers.push(setTimeout(function () {
        if (!alive || !pending) return;
        c.api("GET", "/api/console/" + encodeURIComponent(pending)).then(function (res) {
          if (!alive) return;
          if (res.state === "allowed" && res.ws) { pending = null; connect(res.ws); return; }
          if (res.state === "pending" && Date.now() - since < 130000) { poll(since); return; }
          pending = null;
          c.haptic("warning");
          setState("bad", t("console_closed"));
          showOverlay({ icon: "info", title: t("console_closed"),
            text: t(res.state === "denied" ? "console_denied" : "console_expired"), buttons: [againBtn(), backBtn()] });
        }, function (err) { if (alive && err.status !== 401) poll(since); });
      }, 1500));
    }

    // -- step 3: the terminal --
    function ensureTerm() {
      if (term) return;
      var narrow = window.innerWidth < 420;
      term = new window.Terminal({
        cursorBlink: true, fontSize: savedFont() || (narrow ? 13 : 14), lineHeight: 1.15, scrollback: 5000,
        fontFamily: 'ui-monospace, "SF Mono", "Cascadia Mono", "Roboto Mono", Menlo, Consolas, monospace',
        theme: { background: "#0d1117", foreground: "#e6edf3", cursor: "#58a6ff", selectionBackground: "#264f78",
          black: "#484f58", red: "#ff7b72", green: "#3fb950", yellow: "#d29922", blue: "#58a6ff", magenta: "#bc8cff",
          cyan: "#39c5cf", white: "#b1bac4", brightBlack: "#6e7681", brightRed: "#ffa198", brightGreen: "#56d364",
          brightYellow: "#e3b341", brightBlue: "#79c0ff", brightMagenta: "#d2a8ff", brightCyan: "#56d4dd", brightWhite: "#ffffff" }
      });
      fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
      // before open(): the canvas renderer takes over from the start, so the DOM renderer never tries to
      // inject the <style> elements our CSP refuses
      try { term.loadAddon(new window.CanvasAddon.CanvasAddon()); } catch (e) { /* DOM renderer as a fallback */ }
      term.open(termEl);
      term.onData(function (d) {
        if (ctrl && d.length === 1) {
          var code = d.toUpperCase().charCodeAt(0);
          if (code >= 64 && code <= 95) d = String.fromCharCode(code - 64);
          setMod("Ctrl", false);
        }
        if (alt && d.length === 1) {  // Alt+key = Esc then the key, as Termux and xterm send it
          d = "\x1b" + d;
          setMod("Alt", false);
        }
        send(d);
      });
      term.attachCustomKeyEventHandler(function (e) {
        if (e.type !== "keydown" || e.altKey || e.ctrlKey || e.metaKey) return true;
        var tab = e.key === "Tab" && !e.shiftKey, right = e.key === "ArrowRight";
        if ((tab || right) && current && current.primary && model.known && model.text) {
          e.preventDefault();
          accept(current.primary);
          return false;
        }
        return true;
      });
      term.onResize(function (size) { sendJson({ t: "resize", cols: size.cols, rows: size.rows }); });
    }

    function refit() {
      if (!fit || !term) return;
      try { fit.fit(); } catch (e) { /* hidden */ }
    }

    function connect(path) {
      setState("wait", t("console_connecting"));
      showOverlay({ spinner: true, title: t("console_connecting") });
      loadXterm().then(function () {
        if (!alive) return;
        ensureTerm();
        hideOverlay();
        refit();
        var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;
        ws = new WebSocket(url);
        ws.binaryType = "arraybuffer";
        ws.onopen = function () {
          if (!alive) { ws.close(1000); return; }
          retry = 0;
          hideOverlay();
          c.haptic("success");
          setState("ok", t("online"));
          sendJson({ t: "resize", cols: term.cols, rows: term.rows });
          term.focus();
        };
        ws.onmessage = function (ev) {
          if (typeof ev.data !== "string") { term.write(new Uint8Array(ev.data)); return; }
          var msg = null;
          try { msg = JSON.parse(ev.data); } catch (e) { return; }
          if (msg.t === "session") {  // the key to come back to this console (rotated on every connect)
            resumeInfo = { id: msg.id, resume: msg.resume };
            resumeSet(name, resumeInfo);
          } else if (msg.t === "replay") {
            term.reset();  // what follows is the screen as the server kept it
          }
        };
        ws.onclose = function (ev) {
          ws = null;
          if (!alive) return;
          if (ev.code === 4002) {  // opened in another window
            setState("bad", t("console_elsewhere"));
            showOverlay({ bar: true, icon: "info", title: t("console_elsewhere"), buttons: [
              { text: t("console_take_back"), fn: function () { resumeInfo = resumeGet(name) || resumeInfo; resume(false); } }, backBtn()] });
            return;
          }
          if (!FINAL[ev.code] && resumeInfo && !ending) { scheduleReconnect(); return; }
          resumeSet(name, null); resumeInfo = null;
          if (ending) { c.goBack(); return; }
          c.haptic(ev.code === 1000 ? "light" : "warning");
          setState("bad", t("console_closed"));
          showOverlay({ bar: true, icon: "info", title: t("console_closed"), text: t(CLOSE_TEXT[ev.code] || "console_closed_net"),
            buttons: [againBtn(), backBtn()] });
          showSuggestions(null);
        };
      }, function (err) {
        if (!alive) return;
        setState("bad", t("console_closed"));
        showOverlay({ icon: "warn", title: t("error"), text: err.message, buttons: [againBtn(), backBtn()] });
      });
    }

    function sendJson(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
    function send(data) {
      if (!ws || ws.readyState !== 1) return;
      sendJson({ t: "in", d: data });
      model.feed(data, function (line) { if (assistant) assistant.remember(line); });
      update();
    }
    function closeSocket(code) {
      if (ws) { try { ws.close(code || 1000); } catch (e) { /* closed */ } ws = null; }
    }

    // -- extra keys (Termux style) --
    function setMod(key, onOff) {
      if (key === "Ctrl") ctrl = onOff; else alt = onOff;
      var b = keys.querySelector("[data-key=" + key + "]");
      if (b) b.setAttribute("aria-pressed", onOff ? "true" : "false");
    }
    KEYS.forEach(function (row) {
      var r = h("div", { class: "keys-row" });
      row.forEach(function (k) {
        var b = h("button", { type: "button", "data-key": k[0], "aria-label": k[0], "aria-pressed": k[1] === null ? "false" : null }, k[0]);
        b.addEventListener("pointerdown", function (e) { e.preventDefault(); });  // keeps the keyboard open
        b.addEventListener("click", function () {
          c.haptic("light");
          if (k[1] === null) setMod(k[0], !(k[0] === "Ctrl" ? ctrl : alt)); else send(k[1]);
          if (term) term.focus();
        });
        r.appendChild(b);
      });
      keys.appendChild(r);
    });

    // -- font size: A−/A+ and a two-finger pinch on the terminal, remembered on this device --
    function savedFont() {
      try { var n = parseInt(localStorage.getItem(FONT_KEY), 10); return n >= FONT_MIN && n <= FONT_MAX ? n : 0; }
      catch (e) { return 0; }
    }
    function zoom(step) {
      if (!term) return;
      var size = Math.max(FONT_MIN, Math.min(FONT_MAX, term.options.fontSize + step));
      if (size === term.options.fontSize) { term.focus(); return; }
      term.options.fontSize = size;
      try { localStorage.setItem(FONT_KEY, String(size)); } catch (e) { /* private mode */ }
      c.haptic("light");
      refit();
      term.focus();  // typing keeps going to the shell, not to the A−/A+ button
    }
    var pinch = null;
    function span(e) { var a = e.touches[0], b = e.touches[1]; return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY); }
    on(wrap, "touchstart", function (e) { pinch = e.touches.length === 2 ? span(e) : null; });
    on(wrap, "touchmove", function (e) {
      if (pinch === null || e.touches.length !== 2) return;
      var now = span(e);
      if (Math.abs(now - pinch) > 28) { zoom(now > pinch ? 1 : -1); pinch = now; }
    });
    on(wrap, "touchend", function () { pinch = null; });

    // -- the assistant bar --
    function update() {
      if (!assistant || !model.known || !model.text) { showSuggestions(null); return; }
      showSuggestions(assistant.suggest(model.text, t.lang));
    }

    function accept(item) {
      c.haptic("select");
      var now = model.text, line = item.line;
      if (line.indexOf(now) === 0) send(line.slice(now.length));
      else send(new Array(now.length + 1).join("\x7f") + line);
      if (term) term.focus();
    }

    function showSuggestions(s) {
      current = s;
      c.clear(assist);
      if (!s) return;
      (s.danger || []).forEach(function (d) {
        assist.appendChild(h("span", { class: "chip danger", role: "note" }, "⚠️ ", h("b", { text: t("danger") }), h("small", { text: d.text })));
      });
      (s.items || []).forEach(function (item) {
        var chip = h("button", { type: "button", class: "chip " + (item === s.primary ? "primary" : "") + (item.kind === "typo" ? " typo" : ""),
          "aria-label": item.line + (item.hint ? " — " + item.hint : "") },
          item.kind === "typo" ? h("small", { text: t("did_you_mean") }) : null,
          h("b", { text: item.label }), item.hint ? h("small", { text: item.hint }) : null,
          item === s.primary ? h("span", { class: "kbd", "aria-hidden": "true", text: "Tab" }) : null);
        chip.addEventListener("pointerdown", function (e) { e.preventDefault(); });
        chip.addEventListener("click", function () { accept(item); });
        assist.appendChild(chip);
      });
    }

    if (window.Assistant) {
      Promise.all([
        fetch("commands.json", { cache: "no-cache" }).then(function (r) { return r.json(); }).catch(function () { return {}; }),
        // names and history of the server this console is on, not of the machine the bot runs on
        c.api("GET", "/api/console/commands?server=" + encodeURIComponent(name)).then(function (r) { return r.commands; }, function () { return []; }),
        c.api("GET", "/api/console/history?server=" + encodeURIComponent(name)).then(function (r) { return r.history; }, function () { return []; })
      ]).then(function (res) {
        if (!alive) return;
        assistant = window.Assistant.create(res[0], { executables: res[1], history: res[2] });
        update();
      });
    } else {
      assist.hidden = true;
    }

    resume(false);

    return function () {
      alive = false;
      pending = null;
      timers.forEach(clearTimeout);
      disposers.forEach(function (d) { d(); });
      if (retryTimer) clearTimeout(retryTimer);
      closeSocket(1000);  // leaving the screen does not end the console: it waits for us (see Back on the server page)
      if (term) { try { term.dispose(); } catch (e) { /* gone */ } }
    };
  }

  window.ConsoleScreen = { mount: mount, LineModel: LineModel };
})();
