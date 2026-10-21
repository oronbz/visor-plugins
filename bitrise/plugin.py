"""visor Plugin: the latest builds of the Bitrise apps you watch.

Speaks visor's Plugin protocol (JSON-RPC 2.0, one message per line on
stdin/stdout). Standard library only, Python 3.9+.
"""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_URL = os.environ.get("BITRISE_API_URL", "https://api.bitrise.io/v0.1").rstrip("/")
BUILD_URL = "https://app.bitrise.io/build/"
TIMEOUT = 15
TICK = 1
BACK_OFF = 60
FAST_POLL = float(os.environ.get("BITRISE_FAST_POLL", "15"))

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

WATCH = {"id": "watch", "label": "Watch app…", "input": "choice"}

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

widgets = {}
resolved = {}
shown = {}
lives = {}
live_lock = threading.Lock()
write_lock = threading.Lock()


class RPCError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class BitriseError(Exception):
    def __init__(self, code, path, retry_after=None):
        super().__init__("Bitrise answered %s for %s" % (code, path))
        self.code = code
        self.path = path
        self.retry_after = retry_after


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
        raise BitriseError(e.code, path, e.headers.get("Retry-After")) from None


def post(token, path, body):
    req = Request(API_URL + path, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": token, "Accept": "application/json", "Content-Type": "application/json"})
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


def actions(b):
    url = BUILD_URL + b["slug"]
    out = [{"id": "open", "label": "Open build", "primary": True, "verb": "open", "target": url}]
    if b.get("pull_request_view_url"):
        out.append({"id": "pr", "key": "p", "label": "Open pull request", "verb": "open",
                    "target": b["pull_request_view_url"]})
    out.append({"id": "copy", "key": "y", "label": "Copy build URL", "verb": "copy", "target": url})
    if b["state"] in (RUNNING, ON_HOLD):
        out.append({"id": "abort", "key": "x", "label": "Abort build", "confirm": True})
    out.append({"id": "rebuild", "key": "R", "label": "Rebuild", "confirm": True})
    return out


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
        "actions": actions(b),
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
        b["app_slug"] = slug
        b["state"] = state_of(b)
    return title, builds


def list_apps(token):
    apps, query = [], {"limit": 50}
    while True:
        page = get(token, "/apps", query)
        apps += page.get("data") or []
        after = (page.get("paging") or {}).get("next")
        if not after:
            return apps
        query = {"limit": 50, "next": after}


def watched(settings):
    return [a.strip() for a in str(settings.get("apps", "")).split(",") if a.strip()]


def resolve(token, apps):
    """The slugs apps names, by slug or by title in any case, remembered per apps value so /apps is listed once."""
    key = tuple(apps)
    if key not in resolved:
        listed = list_apps(token)
        lookup = {(a.get("title") or "").lower(): a["slug"] for a in listed}
        lookup.update((a["slug"].lower(), a["slug"]) for a in listed)
        missing = [a for a in apps if a.lower() not in lookup]
        if missing:
            raise UnknownApp(missing[0])
        resolved[key] = list(dict.fromkeys(lookup[a.lower()] for a in apps))
    return resolved[key]


def token_of(settings):
    return os.environ.get("BITRISE_TOKEN") or settings.get("token")


def apps_for_action(token):
    if not token:
        raise RPCError(-32000, "Set a Bitrise token first")
    try:
        return list_apps(token)
    except BitriseError as e:
        if e.code in (401, 403):
            raise RPCError(-32000, "Bitrise rejected the token (%d); set a new one" % e.code) from None
        raise RPCError(-32000, str(e)) from None
    except (URLError, OSError, ValueError) as e:
        raise RPCError(-32000, "Couldn't reach Bitrise: %s" % getattr(e, "reason", e)) from None


def watch_choices(widget):
    settings = widgets.get(widget, {})
    apps = apps_for_action(token_of(settings))
    taken = {a.lower() for a in watched(settings)}
    choices = [{"id": a["slug"], "label": a.get("title") or a["slug"]} for a in apps
               if a["slug"].lower() not in taken and (a.get("title") or "").lower() not in taken]
    return {"choices": sorted(choices, key=lambda c: c["label"].lower())}


def watch(widget, slug):
    settings = widgets.get(widget, {})
    titles = {a["slug"]: a.get("title") or a["slug"] for a in apps_for_action(token_of(settings))}
    if slug not in titles:
        raise RPCError(-32602, "Bitrise has no app `%s` this token can see" % slug)
    return {"settings": {"apps": ", ".join(watched(settings) + [slug])}, "toast": "Watching " + titles[slug]}


def failure(message, reason, hint=None):
    status = {"state": "error", "message": message, "reason": reason}
    if hint:
        status["hint"] = hint
    return {"status": status}


def back_off_until(retry_after):
    now = datetime.now(timezone.utc)
    until = None
    if retry_after:
        try:
            until = now + timedelta(seconds=int(retry_after))
        except ValueError:
            try:
                until = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError):
                pass
    if until is None or until.tzinfo is None:
        until = now + timedelta(seconds=BACK_OFF)
    return until


def rate_limited(until):
    return {"status": {"state": "rate_limited", "until": until.strftime("%Y-%m-%dT%H:%M:%SZ")}}


def setup(message, action=None):
    status = {"state": "setup", "message": message}
    if action:
        status["action"] = action
    return {"status": status}


def load(widget):
    """The Widget's (titles, builds) from Bitrise, or a status-only answer."""
    settings = widgets.get(widget, {})
    token = token_of(settings)
    if not token:
        return setup("Set a Bitrise personal access token", "secret:token")
    apps = watched(settings)
    if not apps:
        return setup("Pick the Bitrise apps to watch", WATCH)
    limit = max(1, int(settings.get("limit", 10)))
    try:
        apps = resolve(token, apps)
        with ThreadPoolExecutor(max_workers=len(apps)) as pool:
            fetched = list(pool.map(lambda slug: fetch(token, slug, limit), apps))
    except UnknownApp as e:
        return failure("Bitrise has no app `%s` this token can see" % e.slug, "unknown app",
                       "Check the slugs and titles in this Widget's apps setting")
    except BitriseError as e:
        if e.code in (401, 403):
            return setup("Bitrise rejected the token (%d); set a new one" % e.code, "secret:token")
        if e.code == 429:
            return rate_limited(back_off_until(e.retry_after))
        return failure(str(e), "Bitrise %d" % e.code)
    except (URLError, OSError, ValueError) as e:
        return failure("Couldn't reach Bitrise: %s" % getattr(e, "reason", e), "offline")
    titles = [title for title, _ in fetched]
    lists = [builds for _, builds in fetched]
    builds = sorted((b for bs in lists for b in bs), key=order)[:limit]
    shown[widget] = {b["slug"]: b for b in builds}
    return titles, builds


def payload(titles, builds):
    now = datetime.now(timezone.utc)
    return {
        "columns": COLUMNS,
        "items": [item(b, now) for b in builds],
        "count": sum(1 for b in builds if b["state"] == RUNNING),
        "empty": "No builds in " + ", ".join(titles),
        "actions": [WATCH],
    }


class Live:
    """A Widget's last builds and the timers that keep them fresh while it's visible."""

    def __init__(self, widget, visible):
        self.widget = widget
        self.visible = visible
        self.closed = threading.Event()
        self.titles = []
        self.builds = []
        self.failed = False
        self.ticker = None
        self.poller = None
        self.limit = None

    def take(self, loaded):
        if isinstance(loaded, dict):
            self.failed = True
            state = loaded["status"]["state"]
            if state == "setup":
                self.builds = []
            if state == "rate_limited":
                self.limit = loaded
            return loaded
        self.failed = False
        self.titles, self.builds = loaded
        return {"payload": payload(self.titles, self.builds)}

    def ticking(self):
        return (self.visible and not self.failed and not self.closed.is_set()
                and any(b["state"] == RUNNING for b in self.builds))

    def limited(self):
        """The rate_limited answer while Bitrise's back-off lasts, else None."""
        if self.limit:
            until = datetime.strptime(self.limit["status"]["until"], "%Y-%m-%dT%H:%M:%SZ")
            if datetime.now(timezone.utc) < until.replace(tzinfo=timezone.utc):
                return self.limit
            self.limit = None
        return None

    def polling(self):
        return (self.visible and not self.closed.is_set()
                and any(b["state"] in (RUNNING, ON_HOLD) for b in self.builds))

    def keep(self):
        if self.ticking() and self.ticker is None:
            self.ticker = threading.Thread(target=self.tick, daemon=True)
            self.ticker.start()
        if self.polling() and self.poller is None:
            self.poller = threading.Thread(target=self.poll, daemon=True)
            self.poller.start()

    def poll(self):
        while not self.closed.wait(FAST_POLL):
            with live_lock:
                if not self.polling():
                    self.poller = None
                    return
                if self.limited():
                    continue
            loaded = load(self.widget)
            with live_lock:
                if self.closed.is_set():
                    return
                update = self.take(loaded)
                if self.visible:
                    self.push(update)
                self.keep()

    def tick(self):
        while not self.closed.wait(TICK):
            with live_lock:
                if not self.ticking():
                    self.ticker = None
                    return
                self.push({"payload": payload(self.titles, self.builds)})

    def push(self, update):
        update["widget"] = self.widget
        send({"method": "widget/update", "params": update})


def refresh(widget):
    with live_lock:
        live = lives.get(widget)
        limit = live and live.limited()
    if limit:
        return limit
    loaded = load(widget)
    with live_lock:
        live = lives.get(widget)
        if live is None:
            return Live(widget, False).take(loaded)
        update = live.take(loaded)
        live.keep()
        return update


def abort(token, b):
    post(token, "/apps/%s/builds/%s/abort" % (quote(b["app_slug"], safe=""), quote(b["slug"], safe="")), {
        "abort_reason": "Aborted from visor",
        "abort_with_success": False,
        "skip_git_status_report": False,
        "skip_notifications": False,
    })
    return {"toast": "Aborted %s on %s" % (b.get("triggered_workflow"), b["app"])}


def rebuild(token, b):
    params = b.get("original_build_params")
    if not params:
        params = {"branch": b.get("branch"), "workflow_id": b.get("triggered_workflow"),
                  "commit_hash": b.get("commit_hash")}
        if b.get("pull_request_id"):
            params["pull_request_id"] = b["pull_request_id"]
            params["branch_dest"] = b.get("pull_request_target_branch")
        params = {k: v for k, v in params.items() if v}
    post(token, "/apps/%s/builds" % quote(b["app_slug"], safe=""),
         {"hook_info": {"type": "bitrise"}, "build_params": params})
    return {"toast": "Rebuilding %s on %s" % (b.get("triggered_workflow"), b["app"])}


INVOKES = {"abort": abort, "rebuild": rebuild}


def invoke(params):
    action = params.get("action")
    if action not in INVOKES:
        raise RPCError(-32602, "no Action `%s`" % action)
    b = shown.get(params["widget"], {}).get(params.get("item"))
    if b is None:
        raise RPCError(-32602, "no build `%s` on this Widget" % params.get("item"))
    token = token_of(widgets.get(params["widget"], {}))
    try:
        return INVOKES[action](token, b)
    except BitriseError as e:
        raise RPCError(-32000, str(e)) from None
    except (URLError, OSError, ValueError) as e:
        raise RPCError(-32000, "Couldn't reach Bitrise: %s" % getattr(e, "reason", e)) from None


def handle(method, params):
    if method == "initialize":
        return {"protocol": 1}
    if method == "widget/open":
        widgets[params["widget"]] = params.get("settings") or {}
        with live_lock:
            old = lives.get(params["widget"])
            if old:
                old.closed.set()
            lives[params["widget"]] = Live(params["widget"], params.get("visible", True))
    elif method == "widget/close":
        widgets.pop(params["widget"], None)
        shown.pop(params["widget"], None)
        with live_lock:
            live = lives.pop(params["widget"], None)
            if live:
                live.closed.set()
    elif method == "widget/frame":
        with live_lock:
            live = lives.get(params["widget"])
            if live:
                live.visible = params.get("visible", True)
                live.keep()
    elif method == "widget/refresh":
        return refresh(params["widget"])
    elif method == "action/choices" and params.get("action") == WATCH["id"]:
        return watch_choices(params["widget"])
    elif method == "action/invoke" and params.get("action") == WATCH["id"]:
        return watch(params["widget"], params.get("choice") or "")
    elif method == "action/invoke":
        return invoke(params)
    elif method == "action/choices":
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
