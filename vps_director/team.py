"""Who the bot answers: admins (OWNER_IDS — everything) and viewers (VIEWER_IDS — read-only).

Both lists live in .env, so a hand edit still works; names and how someone was added are kept in
state["people"]. A person is added three ways:
    by id       — the owner types a Telegram user id (the person must press Start once to get messages)
    by invite   — a one-time t.me/<bot>?start=inv_<code> link for one role, valid INVITE_TTL
    by contact  — the bot sends the owner a "choose a person" keyboard (request_users), Telegram returns the id
Viewers get the read-only commands (/status, /servers, /security, /sites, /backups, /help), the watchdog
alerts and the Mini App without anything that changes something; never Allow buttons, tasks or the console.
"""
import re
import secrets

ROLES = ("admin", "viewer")
INVITE_TTL = 24 * 3600
PICK_TTL = 30 * 60
MAX_PEOPLE = 50
MAX_INVITES = 10
CODE = re.compile(r"inv_([A-Za-z0-9_-]{16,64})")
READONLY_COMMANDS = ("start", "help", "status", "servers", "security", "sites", "backups")
UID = re.compile(r"[1-9][0-9]{0,15}")


class Members:
    """Live view of admins ∪ viewers for `uid in members` (web auth keeps a reference, not a copy)."""

    def __init__(self, cfg):
        self.cfg = cfg

    def __contains__(self, uid):
        return uid in self.cfg.owners or uid in self.cfg.viewers

    def __iter__(self):
        return iter(sorted(self.cfg.owners | self.cfg.viewers))

    def add(self, uid):  # as on a plain set of owners: an admin
        self.cfg.owners.add(uid)

    def discard(self, uid):
        self.cfg.owners.discard(uid)
        self.cfg.viewers.discard(uid)


def role_of(cfg, uid):
    return "admin" if uid in cfg.owners else "viewer" if uid in cfg.viewers else None


def ids_text(ids):
    return ",".join(sorted(ids, key=lambda x: int(x) if x.isdigit() else 0))


def clean_name(value):
    value = value if isinstance(value, str) else ""
    value = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()
    return value[:60]


def person_name(user):
    """"First Last (@username)" from a Telegram user / users_shared entry."""
    if not isinstance(user, dict):
        return ""
    name = " ".join(clean_name(user.get(k)) for k in ("first_name", "last_name") if clean_name(user.get(k)))
    username = clean_name(user.get("username"))
    return (name + (" (@%s)" % username if username else "")).strip()


def new_code():
    return secrets.token_urlsafe(18)


def live(items, now, ttl):
    return {k: v for k, v in (items or {}).items() if isinstance(v, dict) and now - float(v.get("created") or 0) < ttl} \
        if isinstance(items, dict) else {}
