"""A small history of CPU / load / memory / disk per server for the Mini App's charts.

Every SAMPLE_EVERY seconds the web API probes the servers anyway (the same read-only probe as
/servers) and keeps one point per server: [time, load average (raw), memory %, disk %, CPU
utilization % or None]. Load average is never turned into a percentage. The newest KEEP points
(24 hours) live in <conf>/metrics.json, so a restart does not wipe the charts.
"""
import json
import os
import threading

FILE = "metrics.json"
SAMPLE_EVERY = 300
KEEP = 288  # 24 h of 5-minute points
MIN_GAP = 240  # a second sample sooner than this (a manual refresh) is not stored


class History:
    def __init__(self, conf_dir):
        self.path = os.path.join(conf_dir, FILE)
        self.lock = threading.Lock()
        self.data = {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):  # points of an older layout (4 fields) are dropped
                self.data = {k: [p for p in v if isinstance(p, list) and len(p) == 5]
                             for k, v in raw.items() if isinstance(v, list)}
        except (OSError, ValueError):
            pass

    def add(self, now, rows, force=False):
        """rows: server_json() rows. Returns True when a point was stored."""
        with self.lock:
            changed = False
            for row in rows:
                if not row.get("online"):
                    continue
                points = self.data.setdefault(row["name"], [])
                if points and not force and now - points[-1][0] < MIN_GAP:
                    continue
                points.append([int(now), round(float(row["load"]), 2), row["mem_pct"], row["disk_pct"], row.get("cpu_pct")])
                del points[:-KEEP]
                changed = True
            names = {r["name"] for r in rows}
            for gone in [k for k in self.data if k not in names]:
                del self.data[gone]
                changed = True
            if changed:
                self.save()
            return changed

    def save(self):
        from vps_director.setup import write_private
        try:
            write_private(self.path, json.dumps(self.data, separators=(",", ":")))
        except OSError:
            pass

    def series(self, name, last=KEEP):
        """{"t", "load", "mem", "disk", "cpu"} — the newest `last` points; "cpu" holds None where it was not measured."""
        with self.lock:
            points = list(self.data.get(name, []))[-last:]
        return {"t": [p[0] for p in points], "load": [p[1] for p in points], "mem": [p[2] for p in points],
                "disk": [p[3] for p in points], "cpu": [p[4] for p in points]}
