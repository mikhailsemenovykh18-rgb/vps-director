// Unit tests for web/assistant.js — run: node --test web/tests/
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const A = require("../assistant.js");

const DB = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "commands.json"), "utf8"));
const make = (opts) => A.create(DB, Object.assign({ executables: ["ls", "systemctl", "docker", "dokku", "htop", "python3"] }, opts));
const lines = (s) => s.items.map((i) => i.line);
const ids = (line) => A.dangers(line, "en").map((d) => d.id);

test("distance: Damerau-Levenshtein with transpositions and an early stop", () => {
  assert.equal(A.distance("sytemctl", "systemctl"), 1);
  assert.equal(A.distance("dokcer", "docker"), 1);   // transposition counts as one
  assert.equal(A.distance("abc", "abc"), 0);
  assert.equal(A.distance("", "abc"), 3);
  assert.equal(A.distance("journalclt", "journalctl"), 1);
  assert.equal(A.distance("kitten", "sitting"), 3);
  assert.equal(A.distance("kitten", "sitting", 1), 2);  // limit + 1
});

test("typos of the command name", () => {
  const a = make();
  for (const [typed, want] of [["sytemctl", "systemctl"], ["dokcer", "docker"], ["journalclt", "journalctl"], ["sytemctl status nginx", "systemctl status nginx"]]) {
    const s = a.suggest(typed, "en");
    assert.ok(lines(s).includes(want), typed + " → " + JSON.stringify(lines(s)));
    assert.equal(s.items[0].kind, "typo");
  }
  assert.equal(make().suggest("dcker ps", "en").primary.line, "docker ps");
  // a known name is never "corrected", short words need distance 1
  assert.equal(make().suggest("ls -la ", "en").items.filter((i) => i.kind === "typo").length, 0);
  assert.equal(make().suggest("xyzzy", "en").items.length, 0);
});

test("executables from the server count as known and as typo targets", () => {
  assert.equal(make().suggest("dokku apps", "en").items.filter((i) => i.kind === "typo").length, 0);
  assert.ok(lines(make().suggest("dokuk ", "en")).includes("dokku "));
});

test("typo of a subcommand", () => {
  assert.ok(lines(make().suggest("systemctl restat nginx", "en")).includes("systemctl restart nginx"));
});

test("command name completion with explanations", () => {
  const s = make().suggest("journ", "en");
  assert.equal(s.primary.line, "journalctl ");
  assert.match(s.primary.hint, /log/);
  assert.match(make().suggest("journ", "ru").primary.hint, /журнал/);
});

test("subcommands and flags from commands.json", () => {
  const a = make();
  assert.ok(lines(a.suggest("systemctl ", "en")).includes("systemctl status "));
  assert.equal(a.suggest("systemctl resta", "en").primary.line, "systemctl restart ");
  assert.ok(lines(a.suggest("docker compose ", "en")).includes("docker compose up "));
  assert.equal(a.suggest("docker compose up -", "en").items.some((i) => i.label === "-d"), true);
  assert.ok(lines(a.suggest("journalctl -", "en")).includes("journalctl -f "));
  assert.equal(a.suggest("journalctl --vacuum-s", "en").primary.line, "journalctl --vacuum-size=");
  assert.ok(lines(a.suggest("sudo systemctl rel", "en")).includes("sudo systemctl reload "));
  assert.ok(lines(a.suggest("df -h && docker ", "en")).includes("df -h && docker ps "));
  // flags already used are not offered again
  assert.equal(lines(a.suggest("ss -t -", "en")).includes("ss -t -t "), false);
});

test("examples (common one-liners)", () => {
  const s = make().suggest("ss -", "en");
  assert.ok(lines(s).includes("ss -tulpn"), JSON.stringify(lines(s)));
  assert.ok(make().suggest("du -xh", "en").items.some((i) => i.kind === "example"));
});

test("fish-style history: session first, then the file", () => {
  const a = make({ history: ["docker logs -f shop", "git status"] });
  assert.equal(a.suggest("docker l", "en").primary.line, "docker logs -f shop");
  a.remember("docker logs --tail 50 worker");
  assert.equal(a.suggest("docker l", "en").primary.line, "docker logs --tail 50 worker");
  a.remember("git status");
  assert.deepEqual(a.history().slice(-2), ["docker logs --tail 50 worker", "git status"]);
  assert.equal(a.suggest("git status", "en").primary, null);  // nothing to add
});

test("history is deduplicated and bounded", () => {
  const a = make();
  for (let i = 0; i < 1200; i++) a.remember("echo " + (i % 1100));
  assert.equal(a.history().length, 1000);
  assert.equal(new Set(a.history()).size, 1000);
});

test("danger badges for destructive commands", () => {
  const cases = {
    "rm -rf /": "rm_root", "sudo rm -rf / --no-preserve-root": "rm_root", "rm -rf ~": "rm_root", "rm -fr $HOME": "rm_root",
    "rm -r -f /*": "rm_root", 'rm -rf "$HOME/"': "rm_root", "rm -rf /etc": "rm_root",
    "dd if=image.iso of=/dev/sda bs=4M": "dd_dev", "mkfs.ext4 /dev/vdb1": "mkfs", "sudo mkfs -t xfs /dev/sdb": "mkfs",
    "chmod -R 777 /": "chmod_root", "chown -R www-data /": "chmod_root",
    ":(){ :|:& };:": "fork_bomb", "bomb(){ bomb|bomb& };bomb": "fork_bomb",
    "curl -fsSL https://get.example.com | sh": "pipe_sh", "wget -qO- https://x.example | sudo bash": "pipe_sh",
    'bash -c "$(curl -fsSL https://x.example/install)"': "pipe_sh",
    "iptables -F": "fw_flush", "iptables --flush INPUT": "fw_flush", "nft flush ruleset": "fw_flush",
    "ufw disable": "ufw_off", "sudo ufw reset": "ufw_off",
    "shutdown -r now": "power", "reboot": "power", "sudo systemctl poweroff": "power", "init 6": "power",
    "cat x > /dev/sda": "dev_write", "docker compose down -v": "volumes", "docker volume prune": "volumes", "crontab -r": "crontab_r",
    "cd /tmp && rm -rf /": "rm_root", "ls; reboot": "power"
  };
  for (const [line, id] of Object.entries(cases)) assert.ok(ids(line).includes(id), line + " → " + JSON.stringify(ids(line)));
});

test("no false alarms on everyday commands", () => {
  for (const line of ["rm -rf ./build", "rm -rf /tmp/cache", "rm file.txt", "dd if=/dev/zero of=./disk.img bs=1M count=10",
    "dd if=/dev/sda of=/dev/null", "chmod 600 ~/.ssh/id_ed25519", "chmod -R 755 /srv/site/public", "curl -fsSL https://example.com -o get.sh",
    "ufw allow 443/tcp", "iptables -L -n", "systemctl restart nginx", "docker compose down", "echo reboot", "grep -r mkfs docs",
    "crontab -l", "ls /dev/sda"]) {
    assert.deepEqual(ids(line), [], line);
  }
});

test("danger texts in both languages; a warning never hides suggestions", () => {
  const s = make().suggest("rm -rf / ", "ru");
  assert.match(s.danger[0].text, /корзины нет/);
  for (const k of Object.keys(A.DANGER)) assert.equal(A.DANGER[k].length, 2);
  assert.ok(Array.isArray(make().suggest("reboot", "en").items));
});

test("empty input and odd input do not throw", () => {
  const a = make();
  for (const line of ["", "   ", "|", "&&", "'unterminated", "\"x", "sudo", "sudo -u", "A=1 B=2", "a | | b", "x".repeat(2000)]) {
    const s = a.suggest(line, "en");
    assert.ok(Array.isArray(s.items) && Array.isArray(s.danger));
    assert.ok(s.items.length <= 8);
  }
  assert.deepEqual(A.create(null).suggest("sys", "en").items, []);
});

test("lastSegment splits on pipes and command separators", () => {
  assert.equal(A.lastSegment("a | b").text, " b");
  assert.equal(A.lastSegment("a && b || c; d").text, " d");
  assert.equal(A.lastSegment("echo 'a|b' x").text, "echo 'a|b' x");
});
