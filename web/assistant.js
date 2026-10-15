/* VPS Director — the console's command assistant. Pure logic, no DOM, no network: it only SUGGESTS.

   - typo fix for the command name (Damerau-Levenshtein ≤ 2, ≤ 1 for short words) against the curated
     commands.json plus the executables the server found in its PATH; also for a known command's
     subcommand (`systemctl restat` → `restart`);
   - completions for subcommands and flags from commands.json, each with a short explanation, and
     ready one-liners (examples);
   - fish-style autosuggestion from history (this session first, then ~/.bash_history if provided);
   - danger badges for destructive patterns — a warning, never a block.

   Works in the browser (window.Assistant) and in node (module.exports) for the tests in web/tests/. */
(function (root) {
  "use strict";

  var MAX_ITEMS = 8;
  var HISTORY_MAX = 1000;

  // ---------- edit distance ----------

  /* Optimal string alignment distance (Damerau-Levenshtein with adjacent transpositions). Stops early
     and returns limit + 1 when the distance is surely above `limit`. */
  function distance(a, b, limit) {
    if (limit === undefined) limit = Infinity;
    if (Math.abs(a.length - b.length) > limit) return limit + 1;
    var prev2 = null, prev = [], cur, i, j;
    for (j = 0; j <= b.length; j++) prev.push(j);
    for (i = 1; i <= a.length; i++) {
      cur = [i];
      var best = i;
      for (j = 1; j <= b.length; j++) {
        var cost = a.charAt(i - 1) === b.charAt(j - 1) ? 0 : 1;
        var v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
        if (i > 1 && j > 1 && a.charAt(i - 1) === b.charAt(j - 2) && a.charAt(i - 2) === b.charAt(j - 1)) {
          v = Math.min(v, prev2[j - 2] + 1);
        }
        cur.push(v);
        if (v < best) best = v;
      }
      if (best > limit) return limit + 1;
      prev2 = prev;
      prev = cur;
    }
    return prev[b.length];
  }

  function typoLimit(word) { return word.length <= 4 ? 1 : 2; }

  // ---------- parsing ----------

  /* The last simple command of a line (after |, ;, &&, ||) split into words, plus what comes before it.
     Quotes are respected well enough for suggestions; nothing here is ever executed. */
  function lastSegment(line) {
    var cut = 0, quote = "", i;
    for (i = 0; i < line.length; i++) {
      var ch = line.charAt(i);
      if (quote) { if (ch === quote) quote = ""; continue; }
      if (ch === "'" || ch === '"') quote = ch;
      else if (ch === "|" || ch === ";" || (ch === "&" && line.charAt(i + 1) === "&")) {
        cut = i + 1;
        if (ch === "&" || (ch === "|" && line.charAt(i + 1) === "|")) { i++; cut = i + 1; }
      }
    }
    return { before: line.slice(0, cut), text: line.slice(cut) };
  }

  function words(text) {
    var out = [], re = /("[^"]*"?|'[^']*'?|\S)+/g, m;
    while ((m = re.exec(text))) out.push({ word: m[0], start: m.index, end: m.index + m[0].length });
    return out;
  }

  function unquote(w) { return w.replace(/^["']|["']$/g, ""); }

  /* Index of the command word: skips sudo (and its flags), env assignments and `command`/`exec`/`time`. */
  function commandIndex(ws) {
    var i = 0;
    while (i < ws.length) {
      var w = ws[i].word;
      if (w === "sudo" || w === "command" || w === "exec" || w === "time" || w === "nohup") {
        i++;
        while (i < ws.length && ws[i].word.charAt(0) === "-") i += ws[i].word === "-u" ? 2 : 1;
      } else if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(w)) i++;
      else break;
    }
    return i;
  }

  // ---------- danger patterns ----------

  var DANGER = {
    rm_root: ["deletes everything under a system folder or your home — there is no trash bin",
      "удалит всё в системной папке или в домашней — корзины нет"],
    dd_dev: ["writes straight onto a disk device — the data there is overwritten",
      "пишет прямо на диск — данные на нём будут затёрты"],
    mkfs: ["formats a disk or partition — everything on it is lost",
      "форматирует диск или раздел — всё на нём пропадёт"],
    chmod_root: ["changes permissions or owners of system files recursively — can break the server and SSH",
      "рекурсивно меняет права или владельцев системных файлов — сервер и SSH могут сломаться"],
    fork_bomb: ["a fork bomb: spawns processes until the server freezes",
      "fork-бомба: плодит процессы, пока сервер не зависнет"],
    pipe_sh: ["runs a script straight from the internet without reading it",
      "запускает скрипт прямо из интернета, не глядя"],
    fw_flush: ["drops every firewall rule (and can lock you out with a DROP policy)",
      "сбрасывает все правила файрвола (а с политикой DROP — отрежет доступ)"],
    ufw_off: ["turns the firewall off or wipes its rules",
      "выключает файрвол или стирает его правила"],
    power: ["reboots or powers off the server — this console ends too",
      "перезагрузит или выключит сервер — консоль тоже закроется"],
    dev_write: ["redirects output onto a disk device — the data there is overwritten",
      "перенаправляет вывод прямо на диск — данные будут затёрты"],
    volumes: ["deletes Docker volumes — the data in them is gone",
      "удаляет тома Docker — данные в них пропадут"],
    crontab_r: ["removes ALL scheduled jobs of the user, without asking",
      "удалит ВСЕ задачи расписания пользователя, без вопроса"]
  };

  var HOME_TARGETS = /^(~|~\/|~\/\*|\$HOME|\$\{HOME\}|\$HOME\/|\$HOME\/\*|\$\{HOME\}\/\*?)$/;
  var SYSTEM_DIR = /^\/(\*|(bin|boot|dev|etc|home|lib|lib64|opt|proc|root|sbin|srv|sys|usr|var)(\/\*?)?)?$/;

  function isDangerTarget(w) {
    var t = unquote(w);
    return t === "/" || SYSTEM_DIR.test(t) || HOME_TARGETS.test(t) || t === "*" || t === "/*";
  }

  function segmentDangers(seg, found) {
    var ws = words(seg).map(function (x) { return x.word; });
    var ci = commandIndex(words(seg));
    var cmd = ws[ci] || "", args = ws.slice(ci + 1);
    var flags = args.filter(function (a) { return a.charAt(0) === "-"; });
    var targets = args.filter(function (a) { return a.charAt(0) !== "-"; });
    var shortFlags = flags.filter(function (f) { return f.charAt(1) !== "-"; }).join("");
    if (cmd === "rm") {
      var recursive = /[rR]/.test(shortFlags) || flags.indexOf("--recursive") >= 0;
      if (flags.indexOf("--no-preserve-root") >= 0 || (recursive && targets.some(isDangerTarget))) found.rm_root = true;
    }
    if (cmd === "dd" && args.some(function (a) { return /^of=\/dev\/(?!null$|zero$|stdout$|stderr$)/.test(a); })) found.dd_dev = true;
    if (/^mkfs(\.\w+)?$/.test(cmd) || cmd === "wipefs" || (cmd === "mke2fs")) found.mkfs = true;
    if ((cmd === "chmod" || cmd === "chown" || cmd === "chgrp") && (/R/.test(shortFlags) || flags.indexOf("--recursive") >= 0) &&
        targets.slice(1).some(isDangerTarget)) found.chmod_root = true;
    if ((cmd === "iptables" || cmd === "ip6tables") && (/F/.test(shortFlags) || flags.indexOf("--flush") >= 0 ||
        (args.indexOf("-P") >= 0 && args.indexOf("DROP") >= 0))) found.fw_flush = true;
    if (cmd === "nft" && /flush\s+ruleset/.test(args.join(" "))) found.fw_flush = true;
    if (cmd === "ufw" && (targets[0] === "disable" || targets[0] === "reset")) found.ufw_off = true;
    if (/^(shutdown|reboot|poweroff|halt)$/.test(cmd) || (cmd === "init" && /^[06]$/.test(targets[0] || "")) ||
        (cmd === "systemctl" && /^(reboot|poweroff|halt|kexec)$/.test(targets[0] || ""))) found.power = true;
    if (cmd === "docker" && ((targets[0] === "volume" && /^(rm|prune)$/.test(targets[1] || "")) ||
        (targets[0] === "system" && targets[1] === "prune" && flags.indexOf("--volumes") >= 0) ||
        (targets[0] === "compose" && targets[1] === "down" && (flags.indexOf("-v") >= 0 || flags.indexOf("--volumes") >= 0)))) found.volumes = true;
    if (cmd === "docker-compose" && targets[0] === "down" && (flags.indexOf("-v") >= 0 || flags.indexOf("--volumes") >= 0)) found.volumes = true;
    if (cmd === "crontab" && /r/.test(shortFlags)) found.crontab_r = true;
  }

  function dangers(line, lang) {
    var found = {};
    var flat = line.replace(/\s+/g, "");
    if (/(\w+|:)\(\)\{[^}]*\1\|\1&[^}]*\};?\1/.test(flat) || flat.indexOf(":(){:|:&};:") >= 0) found.fork_bomb = true;
    if (/\b(curl|wget|fetch)\b[^|]*\|\s*(sudo\s+(-\S+\s+)*)?(env\s+)?(ba|z|da|k|fi)?sh\b/.test(line) ||
        /\b(ba|z)?sh\s+(-c\s+)?["']?\$\(\s*(curl|wget)\b/.test(line) || /\b(ba|z)?sh\s+<\(\s*(curl|wget)\b/.test(line) ||
        /\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?python3?\b/.test(line)) found.pipe_sh = true;
    if (/>\s*\/dev\/(sd[a-z]|nvme\d|vd[a-z]|xvd[a-z]|hd[a-z]|mmcblk\d|md\d|dm-\d)/.test(line)) found.dev_write = true;
    // every simple command of the line on its own
    var rest = line, seg;
    var parts = [];
    for (var guard = 0; guard < 50 && rest; guard++) {
      seg = lastSegment(rest);
      parts.push(seg.text);
      rest = seg.before.replace(/(\|\||&&|[|;])\s*$/, "");
      if (seg.before === "") break;
    }
    parts.forEach(function (p) { segmentDangers(p, found); });
    var i = lang === "ru" ? 1 : 0;
    return Object.keys(DANGER).filter(function (k) { return found[k]; }).map(function (k) { return { id: k, text: DANGER[k][i] }; });
  }

  // ---------- the assistant ----------

  function hintOf(node, lang) {
    if (!node) return "";
    return (lang === "ru" ? node.ru : node.en) || node.en || "";
  }

  function create(db, opts) {
    opts = opts || {};
    var commands = (db && db.commands && typeof db.commands === "object") ? db.commands : {};
    var curated = Object.keys(commands);
    var executables = [];
    var history = [];          // oldest first; the session's own lines are appended at the end
    var nameSet = {};

    function setExecutables(list) {
      executables = (Array.isArray(list) ? list : []).filter(function (n) { return typeof n === "string" && /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/.test(n); });
      nameSet = {};
      curated.concat(executables).forEach(function (n) { nameSet[n] = true; });
    }
    function setHistory(list) {
      history = [];
      (Array.isArray(list) ? list : []).forEach(function (l) { if (typeof l === "string") remember(l); });
    }
    function remember(line) {
      line = String(line || "").trim();
      if (!line || line.length > 500) return;
      var i = history.indexOf(line);
      if (i >= 0) history.splice(i, 1);
      history.push(line);
      if (history.length > HISTORY_MAX) history.shift();
    }
    function known(name) { return nameSet[name] === true; }

    function typoFor(word, pool) {
      var limit = typoLimit(word), best = [], bestD = limit + 1;
      if (word.length < 2) return [];
      pool.forEach(function (name) {
        if (name === word || Math.abs(name.length - word.length) > limit) return;
        var d = distance(word, name, limit);
        if (d > limit) return;
        if (d < bestD) { bestD = d; best = [name]; } else if (d === bestD && best.indexOf(name) < 0) best.push(name);
      });
      // curated commands first, then shorter / alphabetical
      best.sort(function (a, b) {
        var ca = commands[a] ? 0 : 1, cb = commands[b] ? 0 : 1;
        return ca - cb || a.length - b.length || (a < b ? -1 : 1);
      });
      return best.slice(0, 3);
    }

    function suggest(line, lang) {
      lang = lang === "ru" ? "ru" : "en";
      var out = { primary: null, items: [], danger: dangers(line || "", lang) };
      if (!line || !line.trim()) return out;
      var items = [], seen = {};
      function push(item) {
        if (!item || seen[item.line] || item.line === line) return;
        seen[item.line] = true;
        items.push(item);
      }

      // 1. history (fish-style): the newest line that starts with what is typed
      var ghost = null;
      for (var i = history.length - 1; i >= 0; i--) {
        if (history[i].length > line.length && history[i].indexOf(line) === 0) {
          ghost = { kind: "history", line: history[i], label: history[i], hint: lang === "ru" ? "из истории" : "from history" };
          break;
        }
      }

      // 2. the last simple command: typo, completions, examples
      var seg = lastSegment(line);
      var ws = words(seg.text);
      var trailing = /\s$/.test(seg.text) || ws.length === 0;
      var ci = commandIndex(ws);
      var base = seg.before + seg.text;            // line = base; replacing a word = slice by its offsets
      var offset = seg.before.length;
      var typos = [], completes = [], examples = [];
      var cmdWord = ws[ci];

      function replaceWord(w, by, suffix) {
        return base.slice(0, offset + w.start) + by + (suffix === undefined ? base.slice(offset + w.end) : suffix);
      }

      if (cmdWord && (ci === ws.length - 1 && !trailing)) {
        // typing the command name
        var word = cmdWord.word;
        var pre = curated.concat(executables).filter(function (n, k, arr) { return n !== word && n.indexOf(word) === 0 && arr.indexOf(n) === k; });
        pre.sort(function (a, b) { return (commands[a] ? 0 : 1) - (commands[b] ? 0 : 1) || a.length - b.length || (a < b ? -1 : 1); });
        pre.slice(0, 6).forEach(function (n) { completes.push({ kind: "complete", line: replaceWord(cmdWord, n, " "), label: n, hint: hintOf(commands[n], lang) }); });
        if (!pre.length && !known(word) && word.length >= 3) {
          typoFor(word, curated.concat(executables)).forEach(function (n) {
            typos.push({ kind: "typo", line: replaceWord(cmdWord, n), label: n, hint: hintOf(commands[n], lang) });
          });
        }
      } else if (cmdWord) {
        var name = cmdWord.word;
        if (!known(name) && !commands[name] && /^[A-Za-z][\w.+-]*$/.test(name)) {
          typoFor(name, curated.concat(executables)).forEach(function (n) {
            typos.push({ kind: "typo", line: replaceWord(cmdWord, n), label: n, hint: hintOf(commands[n], lang) });
          });
        }
        var node = commands[name];
        if (node) {
          var args = ws.slice(ci + 1);
          var cur = trailing ? null : args.pop();
          var stopped = false;
          for (var k = 0; k < args.length && !stopped; k++) {
            var a = args[k].word;
            if (a.charAt(0) === "-") continue;
            if (node.sub && node.sub[a]) { node = node.sub[a]; continue; }
            if (node.sub) {
              var fix = typoFor(a, Object.keys(node.sub).filter(function (s) { return s.charAt(0) !== "-"; }));
              if (fix.length === 1) typos.push({ kind: "typo", line: replaceWord(args[k], fix[0]), label: fix[0], hint: hintOf(node.sub[fix[0]], lang) });
            }
            stopped = true; // positional arguments from here on
          }
          if (!stopped) {
            var prefix = cur ? cur.word : "";
            var pool = [];
            if (prefix.charAt(0) !== "-" && node.sub) Object.keys(node.sub).forEach(function (s) { pool.push([s, node.sub[s], true]); });
            if ((prefix.charAt(0) === "-" || !node.sub) && node.flags) Object.keys(node.flags).forEach(function (f) { pool.push([f, node.flags[f], false]); });
            var used = {};
            args.forEach(function (x) { used[x.word] = true; });
            pool.filter(function (p) { return p[0] !== prefix && p[0].indexOf(prefix) === 0 && !used[p[0]]; }).slice(0, MAX_ITEMS)
              .forEach(function (p) {
                var end = /=$/.test(p[0]) ? "" : " ";
                completes.push({ kind: "complete", line: cur ? replaceWord(cur, p[0], end) : base + p[0] + end, label: p[0], hint: hintOf(p[1], lang) });
              });
          }
        }
        var top = commands[name];
        if (top && top.examples) {
          var typed = seg.text.replace(/^\s+/, "");
          top.examples.forEach(function (ex) {
            if (ex.line.length > typed.length && ex.line.indexOf(typed) === 0) {
              examples.push({ kind: "example", line: seg.before + seg.text.slice(0, seg.text.length - typed.length) + ex.line, label: ex.line, hint: hintOf(ex, lang) });
            }
          });
        }
      }

      var primary = ghost || (completes.length === 1 ? completes[0] : null) || (!completes.length && typos.length === 1 ? typos[0] : null) ||
        (!completes.length && !typos.length && examples.length === 1 ? examples[0] : null);
      if (primary) push(primary);
      typos.forEach(push);
      completes.forEach(push);
      examples.slice(0, 3).forEach(push);
      if (ghost) push(ghost);
      out.primary = primary;
      out.items = items.slice(0, MAX_ITEMS);
      return out;
    }

    setExecutables(opts.executables);
    setHistory(opts.history);
    return { suggest: suggest, remember: remember, setExecutables: setExecutables, setHistory: setHistory,
      history: function () { return history.slice(); } };
  }

  var api = { create: create, distance: distance, dangers: dangers, lastSegment: lastSegment, DANGER: DANGER };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Assistant = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
