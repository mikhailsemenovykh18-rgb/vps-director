/* Picks the theme before the first paint (loaded without defer, right after Telegram's script), so the
   app never flashes the wrong one. The choice — "system", "light" or "dark" — is kept on this device;
   "system" follows Telegram's colour scheme inside Telegram and the system's in a normal browser. */
(function (root) {
  "use strict";
  var KEY = "vpsd-theme";
  var tg = root.Telegram && root.Telegram.WebApp;
  var inTg = !!(tg && tg.initData);
  var BG = { light: "#f3f5f9", dark: "#071a2c" };

  function saved() {
    try { var m = root.localStorage.getItem(KEY); return m === "light" || m === "dark" ? m : "system"; }
    catch (e) { return "system"; }
  }
  function system() {
    if (inTg && tg.colorScheme) return tg.colorScheme === "dark" ? "dark" : "light";
    return root.matchMedia && root.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function resolve(mode) { return mode === "light" || mode === "dark" ? mode : system(); }
  function apply(mode) {
    var theme = resolve(mode || saved());
    var html = root.document.documentElement;
    html.setAttribute("data-theme", theme);
    var meta = root.document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", BG[theme]);
    return theme;
  }
  function set(mode) {
    try { if (mode === "system") root.localStorage.removeItem(KEY); else root.localStorage.setItem(KEY, mode); }
    catch (e) { /* private mode: the choice lasts until the app is closed */ }
    return apply(mode);
  }
  apply();
  root.VPSD_THEME = { saved: saved, set: set, apply: apply, current: function () { return resolve(saved()); }, bg: BG };
})(typeof window !== "undefined" ? window : this);
