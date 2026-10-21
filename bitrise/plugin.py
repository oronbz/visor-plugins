"""visor Plugin: the latest builds of the Bitrise apps you watch.

Speaks visor's Plugin protocol (JSON-RPC 2.0, one message per line on
stdin/stdout). Standard library only, Python 3.9+.
"""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_URL = os.environ.get("BITRISE_API_URL", "https://api.bitrise.io/v0.1").rstrip("/")
TIMEOUT = 15

RUNNING, ON_HOLD, SUCCEEDED, FAILED, ABORTED = "running", "on hold", "succeeded", "failed", "aborted"
GLYPHS = {
    RUNNING: ("●", "accent"),
    ON_HOLD: ("◌", "warn"),
    SUCCEEDED: ("✓", "ok"),
    FAILED: ("✗", "bad"),
    ABORTED: ("⊘", "faint"),
}
COLUMNS = [
    {"priority": 0},
    {"priority": 1},
    {"priority": 2},
    {"priority": 3, "flex": True},
    {"priority": 4, "right": True},
]

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

widgets = {}
write_lock = threading.Lock()


class RPCError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class BitriseError(Exception):
    def __init__(self, code, path):
        super().__init__("Bitrise answered %s for %s" % (code, path))
        self.code = code
        self.path = path


class UnknownApp(Exception):
    def __init__(self, slug):
        super().__init__(slug)
        self.slug = slug


def send(msg):
    msg["jsonrpc"] = "2.0"
    with write_lock:
        sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def get(token, path, query=None):
    url = API_URL + path
    if query:
        url += "?" + urlencode(query)
    req = Request(url, headers={"Authorization": token, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except HTTPError as e:
        raise BitriseError(e.code, path) from None


def parse_time(s):
    if not s:
        return None
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def state_of(b):
    status = b.get("status")
    if status == 0:
        return ON_HOLD if b.get("is_on_hold") else RUNNING
    return {1: SUCCEEDED, 2: FAILED}.get(status, ABORTED)


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds ago" % seconds
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)


def duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm %02ds" % (seconds // 60, seconds % 60)
    return "%dh %02dm" % (seconds // 3600, seconds % 3600 // 60)


def elapsed(b, now):
    if b["state"] in (RUNNING, ON_HOLD):
        since = parse_time(b.get("started_on_worker_at")) or parse_time(b.get("triggered_at"))
        return duration((now - since).total_seconds()) if since else ""
    finished = parse_time(b.get("finished_at"))
    return ago((now - finished).total_seconds()) if finished else ""


def order(b):
    rank = {RUNNING: 0, ON_HOLD: 1}.get(b["state"], 2)
    when = b.get("finished_at") if rank == 2 else b.get("triggered_at")
    return rank, -(parse_time(when) or EPOCH).timestamp()


def item(b, now):
    glyph, token = GLYPHS[b["state"]]
    pr = b.get("pull_request_id")
    return {
        "id": b["slug"],
        "fields": [
            [{"text": glyph, "token": token}],
            [{"text": b["app"], "token": "dim"}],
            [{"text": b.get("triggered_workflow") or "", "token": "fg"}],
            [{"text": "#%d" % pr if pr else b.get("branch") or "", "token": "fg"}],
            [{"text": elapsed(b, now), "token": "faint"}],
        ],
    }


def fetch(token, slug, limit):
    path = "/apps/%s" % quote(slug, safe="")
    try:
        title = get(token, path)["data"].get("title") or slug
    except BitriseError as e:
        if e.code == 404:
            raise UnknownApp(slug) from None
        raise
    builds = get(token, path + "/builds", {"sort_by": "running_first", "limit": limit})["data"]
    for b in builds:
        b["app"] = title
        b["state"] = state_of(b)
    return title, builds


def failure(message, reason, hint=None):
    status = {"state": "error", "message": message, "reason": reason}
    if hint:
        status["hint"] = hint
    return {"status": status}


def setup(message, action=None):
    status = {"state": "setup", "message": message}
    if action:
        status["action"] = action
    return {"status": status}


def refresh(widget):
    settings = widgets.get(widget, {})
    token = os.environ.get("BITRISE_TOKEN") or settings.get("token")
    if not token:
        return setup("Set a Bitrise personal access token", "secret:token")
    apps = [a.strip() for a in str(settings.get("apps", "")).split(",") if a.strip()]
    if not apps:
        return setup("Add the Bitrise app slugs to watch to this Widget's apps setting")
    limit = max(1, int(settings.get("limit", 10)))
    try:
        with ThreadPoolExecutor(max_workers=len(apps)) as pool:
            fetched = list(pool.map(lambda slug: fetch(token, slug, limit), apps))
    except UnknownApp as e:
        return failure("Bitrise has no app `%s` this token can see" % e.slug, "unknown app",
                       "Check the slugs in this Widget's apps setting")
    except BitriseError as e:
        if e.code in (401, 403):
            return setup("Bitrise rejected the token (%d); set a new one" % e.code, "secret:token")
        return failure(str(e), "Bitrise %d" % e.code)
    except (URLError, OSError, ValueError) as e:
        return failure("Couldn't reach Bitrise: %s" % getattr(e, "reason", e), "offline")
    titles = [title for title, _ in fetched]
    lists = [builds for _, builds in fetched]
    builds = sorted((b for bs in lists for b in bs), key=order)[:limit]
    now = datetime.now(timezone.utc)
    return {"payload": {
        "columns": COLUMNS,
        "items": [item(b, now) for b in builds],
        "count": sum(1 for b in builds if b["state"] == RUNNING),
        "empty": "No builds in " + ", ".join(titles),
    }}


def handle(method, params):
    if method == "initialize":
        return {"protocol": 1}
    if method == "widget/open":
        widgets[params["widget"]] = params.get("settings") or {}
    elif method == "widget/close":
        widgets.pop(params["widget"], None)
    elif method == "widget/frame":
        pass
    elif method == "widget/refresh":
        return refresh(params["widget"])
    elif method in ("action/invoke", "action/choices"):
        raise RPCError(-32602, "no Action `%s`" % params.get("action"))
    else:
        raise RPCError(-32601, "no method `%s`" % method)
    return None


def main():
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        msg = json.loads(line.decode("utf-8"))
        try:
            reply = {"result": handle(msg.get("method"), msg.get("params") or {})}
        except RPCError as e:
            reply = {"error": {"code": e.code, "message": e.message}}
        except Exception as e:
            reply = {"error": {"code": -32603, "message": "the Plugin failed: %s" % type(e).__name__}}
        if "id" in msg:
            reply["id"] = msg["id"]
            send(reply)


if __name__ == "__main__":
    main()
