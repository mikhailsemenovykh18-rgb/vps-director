/* Line icons (24×24, stroke = currentColor), built as SVG elements (createElementNS, never HTML strings). */
(function (root) {
  "use strict";
  var C = "M12 3a9 9 0 1 1 0 18a9 9 0 1 1 0-18z";
  var PATHS = {
    server: "M4 4h16v6H4z M4 14h16v6H4z M8 7h.01 M8 17h.01",
    clock: C + " M12 7v5l3 2",
    shield: "M12 3l7 3v6c0 4.5-3 7.5-7 9c-4-1.5-7-4.5-7-9V6z",
    sliders: "M4 6h9 M17 6h3 M15 4v4 M4 12h3 M11 12h9 M9 10v4 M4 18h11 M19 18h1 M17 16v4",
    refresh: "M20 11a8 8 0 0 0-14.9-4 M4 3v4h4 M4 13a8 8 0 0 0 14.9 4 M20 21v-4h-4",
    right: "M9 6l6 6-6 6",
    left: "M15 6l-6 6 6 6",
    ok: C + " M8 12.5l2.5 2.5L16 9.5",
    warn: "M12 3.5L2.5 20h19z M12 10v4 M12 17h.01",
    bad: C + " M9 9l6 6 M15 9l-6 6",
    info: C + " M12 11v5 M12 8h.01",
    terminal: "M3 5h18v14H3z M7 10l3 2-3 2 M12 15h5",
    box: "M12 3l8 4.5v9L12 21l-8-4.5v-9z M4 7.5l8 4.5 8-4.5 M12 12v9",
    restart: "M4 12a8 8 0 1 0 2.4-5.7 M4 3v4h4",
    plus: "M12 5v14 M5 12h14",
    trash: "M4 7h16 M9 7V4h6v3 M6 7l1 13h10l1-13",
    globe: C + " M3 12h18 M12 3c3 3.5 3 14.5 0 18 M12 3c-3 3.5-3 14.5 0 18",
    archive: "M3 4h18v4H3z M5 8v12h14V8 M10 12h4",
    down: "M12 5v14 M6 13l6 6 6-6",
    close: "M6 6l12 12 M18 6L6 18",
    lock: "M6 11h12v9H6z M8 11V8a4 4 0 0 1 8 0v3",
    send: "M21 4L3 11l7 2 2 7z M10 13l11-9",
    bell: "M6 16V11a6 6 0 0 1 12 0v5l2 2H4z M10 20a2 2 0 0 0 4 0",
    sun: "M12 8a4 4 0 1 1 0 8a4 4 0 1 1 0-8z M12 2v2 M12 20v2 M4.9 4.9l1.4 1.4 M17.7 17.7l1.4 1.4 M2 12h2 M20 12h2 M4.9 19.1l1.4-1.4 M17.7 6.3l1.4-1.4",
    chat: "M4 5h16v11H9l-5 4z M8 9h8 M8 12h5",
    copy: "M9 9h11v11H9z M5 15H4V4h11v1",
    link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1 M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
    user: "M12 4a4 4 0 1 1 0 8a4 4 0 1 1 0-8z M4 21c0-4 4-6 8-6s8 2 8 6",
    gear: "M12 9a3 3 0 1 1 0 6a3 3 0 1 1 0-6z M12 2v3 M12 19v3 M2 12h3 M19 12h3 M4.9 4.9l2.1 2.1 M17 17l2.1 2.1 M4.9 19.1L7 17 M17 7l2.1-2.1",
    gauge: "M12 4a9 9 0 1 1 0 18a9 9 0 1 1 0-18z M12 13l4-4 M7.5 16a6 6 0 0 1 9 0",
    more: "M5 12h.01 M12 12h.01 M19 12h.01",
    logout: "M15 4h4v16h-4 M10 8l-4 4 4 4 M6 12h10"
  };
  var NS = "http://www.w3.org/2000/svg";

  function icon(name, label) {
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "24");
    svg.setAttribute("height", "24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    if (label) {
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", label);
    } else {
      svg.setAttribute("aria-hidden", "true");
    }
    svg.setAttribute("focusable", "false");
    var path = document.createElementNS(NS, "path");
    path.setAttribute("d", PATHS[name] || PATHS.info);
    svg.appendChild(path);
    return svg;
  }

  root.Icons = { icon: icon, names: Object.keys(PATHS) };
})(this);
