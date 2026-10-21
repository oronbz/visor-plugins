"""visor Plugin: the latest builds of the Bitrise apps you watch.

Speaks visor's Plugin protocol (JSON-RPC 2.0, one message per line on
stdin/stdout). Standard library only, Python 3.9+.
"""

import json
import os
import sys
import threading
from collections import namedtuple
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
STAMP = "%Y-%m-%dT%H:%M:%SZ"

RUNNING, ON_HOLD = "running", "on hold"
SUCCEEDED, FAILED, ABORTED = "succeeded", "failed", "aborted"
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
SET_TOKEN = "secret:token"

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

Build = namedtuple("Build", "slug app_slug app state data")
Build.__doc__ = "A build as the Widget lists it, with Bitrise's build object as data."

Fetched = namedtuple("Fetched", "apps builds running")
Fetched.__doc__ = "A Widget's apps as (slug, title), the builds it lists and how many run."

widgets = {}
state_lock = threading.Lock()
write_lock = threading.Lock()


class RPCError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Problem(Exception):
    """A Status other than ok: a refresh reports it, an Action answers its message as an error."""

    def __init__(self, message, status, code=None, until=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.until = until


def setup(message, action):
    return Problem(message, {"state": "setup", "message": message, "action": action})


def error(message, reason, hint=None, code=None):
    status = {"state": "error", "message": message, "reason": reason}
    if hint:
        status["hint"] = hint
    return Problem(message, status, code)


def rate_limited(until):
    stamp = until.strftime(STAMP)
    return Problem("Bitrise asked to wait until " + stamp,
                   {"state": "rate_limited", "until": stamp}, 429, until)


def unknown_app(name):
    return error("Bitrise has no app `%s` this token can see" % name, "unknown app",
                 "Check the slugs and titles in this Widget's apps setting")


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


def refused(code, path, retry_after):
    if code in (401, 403):
        problem = setup("Bitrise rejected the token (%d); set a new one" % code, SET_TOKEN)
        problem.code = code
        return problem
    if code == 429:
        return rate_limited(back_off_until(retry_after))
    return error("Bitrise answered %d for %s" % (code, path), "Bitrise %d" % code, code=code)


def send(msg):
    msg["jsonrpc"] = "2.0"
    with write_lock:
        sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def call(token, path, query=None, body=None):
    """Bitrise's answer to a GET of path, or a POST of body to it; raises Problem otherwise."""
    url = API_URL + path + ("?" + urlencode(query) if query else "")
    headers = {"Authorization": token, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=TIMEOUT) as resp:
            return json.load(resp)
    except HTTPError as e:
        raise refused(e.code, path, e.headers.get("Retry-After")) from None
    except (URLError, OSError) as e:
        raise error("Couldn't reach Bitrise: %s" % getattr(e, "reason", e), "offline") from None
    except ValueError:
        raise error("Bitrise's answer for %s isn't JSON" % path, "bad answer") from None


def app_path(slug):
    return "/apps/" + quote(slug, safe="")


def parse_time(s):
    if not s:
        return None
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def state_of(data):
    status = data.get("status")
    if status == 0:
        return ON_HOLD if data.get("is_on_hold") else RUNNING
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


def elapsed(build, now):
    if build.state in (RUNNING, ON_HOLD):
        since = (parse_time(build.data.get("started_on_worker_at"))
                 or parse_time(build.data.get("triggered_at")))
        return duration((now - since).total_seconds()) if since else ""
    finished = parse_time(build.data.get("finished_at"))
    return ago((now - finished).total_seconds()) if finished else ""


def order(build):
    rank = {RUNNING: 0, ON_HOLD: 1}.get(build.state, 2)
    when = build.data.get("finished_at" if rank == 2 else "triggered_at")
    return rank, -(parse_time(when) or EPOCH).timestamp()


def actions(build):
    url = BUILD_URL + build.slug
    out = [{"id": "open", "label": "Open build", "primary": True, "verb": "open", "target": url}]
    pr_url = build.data.get("pull_request_view_url")
    if pr_url:
        out.append({"id": "pr", "key": "p", "label": "Open pull request", "verb": "open",
                    "target": pr_url})
    out.append({"id": "copy", "key": "y", "label": "Copy build URL", "verb": "copy",
                "target": url})
    if build.state in (RUNNING, ON_HOLD):
        out.append({"id": "abort", "key": "x", "label": "Abort build", "confirm": True})
    out.append({"id": "rebuild", "key": "R", "label": "Rebuild", "confirm": True})
    return out


def item(build, now):
    glyph, token = GLYPHS[build.state]
    pr = build.data.get("pull_request_id")
    return {
        "id": build.slug,
        "fields": [
            [{"text": glyph, "token": token}],
            [{"text": build.app, "token": "dim"}],
            [{"text": build.data.get("triggered_workflow") or "", "token": "fg"}],
            [{"text": "#%d" % pr if pr else build.data.get("branch") or "", "token": "fg"}],
            [{"text": elapsed(build, now), "token": "faint"}],
        ],
        "actions": actions(build),
    }


def payload(apps, builds, running):
    now = datetime.now(timezone.utc)
    return {
        "columns": COLUMNS,
        "items": [item(build, now) for build in builds],
        "count": running,
        "empty": "No builds in " + ", ".join(title for _, title in apps),
        "actions": [WATCH],
    }


def title_of(app):
    return app.get("title") or app["slug"]


def list_apps(token):
    apps, query = [], {"limit": 50}
    while True:
        page = call(token, "/apps", query)
        apps += page.get("data") or []
        after = (page.get("paging") or {}).get("next")
        if not after:
            return apps
        query = {"limit": 50, "next": after}


def watched(settings):
    return [name.strip() for name in str(settings.get("apps", "")).split(",") if name.strip()]


def resolve(token, names):
    """The (slug, title) of each app named, by slug or by title in any case."""
    listed = list_apps(token)
    lookup = {(app.get("title") or "").lower(): app for app in listed}
    lookup.update((app["slug"].lower(), app) for app in listed)
    for name in names:
        if name.lower() not in lookup:
            raise unknown_app(name)
    found = [lookup[name.lower()] for name in names]
    return list(dict.fromkeys((app["slug"], title_of(app)) for app in found))


def fetch(token, app, limit):
    slug, title = app
    try:
        listed = call(token, app_path(slug) + "/builds",
                      {"sort_by": "running_first", "limit": limit})["data"]
    except Problem as problem:
        if problem.code == 404:
            raise unknown_app(slug) from None
        raise
    return [Build(data["slug"], slug, title, state_of(data), data) for data in listed]


def token_for(settings):
    token = (os.environ.get("BITRISE_TOKEN") or settings.get("token") or "").strip()
    if not token:
        raise setup("Set a Bitrise personal access token", SET_TOKEN)
    if not (token.isascii() and token.isprintable()):
        raise setup("The Bitrise token has characters no token has; set a new one", SET_TOKEN)
    return token


def limit_of(settings):
    try:
        return max(1, int(settings.get("limit", 10)))
    except (TypeError, ValueError):
        raise error("`limit` must be a whole number, not `%s`" % settings.get("limit"),
                    "bad limit") from None


def load(settings, apps=None):
    """The builds of the apps settings watch, given those apps if they're already resolved."""
    token = token_for(settings)
    names = watched(settings)
    if not names:
        raise setup("Pick the Bitrise apps to watch", WATCH)
    limit = limit_of(settings)
    apps = apps or resolve(token, names)
    with ThreadPoolExecutor(max_workers=len(apps)) as pool:
        lists = list(pool.map(lambda app: fetch(token, app, limit), apps))
    builds = [build for listed in lists for build in listed]
    running = sum(1 for build in builds if build.state == RUNNING)
    return Fetched(apps, sorted(builds, key=order)[:limit], running)


class Widget:
    """A placed Widget: its settings, the builds it lists, and the timers that keep them
    fresh while it's visible. Its fields are guarded by state_lock."""

    def __init__(self, id, settings, visible):
        self.id = id
        self.settings = settings
        self.visible = visible
        self.closed = threading.Event()
        self.sending = threading.Lock()
        self.apps = None
        self.builds = []
        self.running = 0
        self.failed = False
        self.limited = None
        self.ticker = None
        self.poller = None

    def record(self, fetched, problem):
        self.failed = problem is not None
        if fetched:
            self.apps, self.builds, self.running = fetched
        elif problem.status["state"] == "setup":
            self.builds = []
        elif problem.until:
            self.limited = problem

    def waiting(self):
        """The rate_limited Problem while Bitrise's back-off lasts, else None."""
        if self.limited and datetime.now(timezone.utc) >= self.limited.until:
            self.limited = None
        return self.limited

    def ticking(self):
        return (self.visible and not self.failed and not self.closed.is_set()
                and any(build.state == RUNNING for build in self.builds))

    def polling(self):
        return (self.visible and not self.closed.is_set()
                and any(build.state in (RUNNING, ON_HOLD) for build in self.builds))

    def start_timers(self):
        if self.ticking() and self.ticker is None:
            self.ticker = threading.Thread(target=self.tick, daemon=True)
            self.ticker.start()
        if self.polling() and self.poller is None:
            self.poller = threading.Thread(target=self.poll, daemon=True)
            self.poller.start()

    def poll(self):
        while not self.closed.wait(FAST_POLL):
            with state_lock:
                if not self.polling():
                    self.poller = None
                    return
                if self.waiting():
                    continue
            update = refreshed(self)
            self.push(lambda: update if self.visible else None)

    def tick(self):
        while not self.closed.wait(TICK):
            if self.push(self.ticked) is None:
                return

    def ticked(self):
        if not self.ticking():
            self.ticker = None
            return None
        return {"payload": payload(self.apps, self.builds, self.running)}

    def push(self, make):
        """Sends make()'s update, made under state_lock, unless the Widget has closed.
        stdout is written outside state_lock so a slow reader stalls only this Widget's pushes."""
        with self.sending:
            with state_lock:
                update = None if self.closed.is_set() else make()
            if update is not None:
                send({"method": "widget/update", "params": dict(update, widget=self.id)})
            return update


def refreshed(widget):
    """The Widget's update from a fresh fetch, recorded unless it has closed meanwhile."""
    with state_lock:
        apps = widget.apps
    fetched = problem = None
    try:
        fetched = load(widget.settings, apps)
        update = {"payload": payload(*fetched)}
    except Problem as e:
        problem = e
    except Exception as e:
        problem = error("the Plugin failed: %s" % type(e).__name__, "plugin failed")
    if problem:
        fetched, update = None, {"status": problem.status}
    with state_lock:
        if not widget.closed.is_set():
            widget.record(fetched, problem)
            widget.start_timers()
    return update


def widget_of(id):
    with state_lock:
        widget = widgets.get(id)
    if widget is None:
        raise RPCError(-32602, "no Widget `%s`" % id)
    return widget


def refresh(id):
    widget = widget_of(id)
    with state_lock:
        waiting = widget.waiting()
    if waiting:
        return {"status": waiting.status}
    return refreshed(widget)


def watch_choices(widget):
    settings = widget_of(widget).settings
    taken = {name.lower() for name in watched(settings)}
    apps = list_apps(token_for(settings))
    choices = [{"id": app["slug"], "label": title_of(app)} for app in apps
               if app["slug"].lower() not in taken
               and (app.get("title") or "").lower() not in taken]
    return {"choices": sorted(choices, key=lambda choice: choice["label"].lower())}


def watch(widget, slug):
    settings = widget_of(widget).settings
    titles = {app["slug"]: title_of(app) for app in list_apps(token_for(settings))}
    if slug not in titles:
        raise RPCError(-32602, unknown_app(slug).message)
    return {"settings": {"apps": ", ".join(watched(settings) + [slug])},
            "toast": "Watching " + titles[slug]}


def abort(token, build):
    path = "%s/builds/%s/abort" % (app_path(build.app_slug), quote(build.slug, safe=""))
    call(token, path, body={
        "abort_reason": "Aborted from visor",
        "abort_with_success": False,
        "skip_git_status_report": False,
        "skip_notifications": False,
    })
    return {"toast": "Aborted %s on %s" % (build.data.get("triggered_workflow"), build.app)}


def rebuild(token, build):
    params = build.data.get("original_build_params")
    if not params:
        # The build object carries no PR refs, repo URLs or owners, so this is the
        # PR build Bitrise documents: branch, branch_dest, commit_hash and pull_request_id.
        data = build.data
        params = {"branch": data.get("branch"), "workflow_id": data.get("triggered_workflow"),
                  "commit_hash": data.get("commit_hash")}
        if data.get("pull_request_id"):
            params["pull_request_id"] = data["pull_request_id"]
            params["branch_dest"] = data.get("pull_request_target_branch")
        params = {k: v for k, v in params.items() if v}
    call(token, app_path(build.app_slug) + "/builds",
         body={"hook_info": {"type": "bitrise"}, "build_params": params})
    return {"toast": "Rebuilding %s on %s" % (build.data.get("triggered_workflow"), build.app)}


BUILD_ACTIONS = {"abort": abort, "rebuild": rebuild}


def choices(params):
    if params.get("action") != WATCH["id"]:
        raise RPCError(-32602, "no Action `%s`" % params.get("action"))
    return watch_choices(params["widget"])


def invoke(params):
    action = params.get("action")
    if action == WATCH["id"]:
        return watch(params["widget"], params.get("choice") or "")
    if action not in BUILD_ACTIONS:
        raise RPCError(-32602, "no Action `%s`" % action)
    with state_lock:
        widget = widgets.get(params["widget"])
        builds = widget.builds if widget else []
        build = next((b for b in builds if b.slug == params.get("item")), None)
    if build is None:
        raise RPCError(-32602, "no build `%s` on this Widget" % params.get("item"))
    return BUILD_ACTIONS[action](token_for(widget.settings), build)


def open_widget(params):
    with state_lock:
        old = widgets.get(params["widget"])
        if old:
            old.closed.set()
        widgets[params["widget"]] = Widget(params["widget"], params.get("settings") or {},
                                           params.get("visible", True))


def close_widget(params):
    with state_lock:
        widget = widgets.pop(params["widget"], None)
        if widget:
            widget.closed.set()


def frame_widget(params):
    with state_lock:
        widget = widgets.get(params["widget"])
        if widget:
            widget.visible = params.get("visible", True)
            widget.start_timers()


METHODS = {
    "initialize": lambda params: {"protocol": 1},
    "widget/open": open_widget,
    "widget/close": close_widget,
    "widget/frame": frame_widget,
    "widget/refresh": lambda params: refresh(params["widget"]),
    "action/choices": choices,
    "action/invoke": invoke,
}


def handle(method, params):
    if method not in METHODS:
        raise RPCError(-32601, "no method `%s`" % method)
    return METHODS[method](params)


def main():
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        msg = json.loads(line.decode("utf-8"))
        try:
            reply = {"result": handle(msg.get("method"), msg.get("params") or {})}
        except RPCError as e:
            reply = {"error": {"code": e.code, "message": e.message}}
        except Problem as e:
            reply = {"error": {"code": -32000, "message": e.message}}
        except Exception as e:
            reply = {"error": {"code": -32603,
                               "message": "the Plugin failed: %s" % type(e).__name__}}
        if "id" in msg:
            reply["id"] = msg["id"]
            send(reply)


if __name__ == "__main__":
    main()
