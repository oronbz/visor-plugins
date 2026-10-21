import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "test-token-3f9a1c"
WAIT = 10


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(slug, status=1, on_hold=False, workflow="primary", branch="main", pr=None,
          triggered=None, started=None, finished=None):
    b = {
        "slug": slug,
        "status": status,
        "is_on_hold": on_hold,
        "triggered_workflow": workflow,
        "branch": branch,
        "pull_request_id": pr or 0,
        "pull_request_view_url": "https://github.com/o/r/pull/%d" % pr if pr else None,
        "triggered_at": triggered or ago(hours=1),
        "started_on_worker_at": started,
        "finished_at": finished,
    }
    if status != 0 and finished is None:
        b["finished_at"] = ago(minutes=30)
    return b


class FakeBitrise:
    """A stdlib http.server standing in for api.bitrise.io/v0.1."""

    def __init__(self):
        self.apps = {}
        self.fail = {}
        self.retry_after = None
        self.gate = None
        self.page = 50
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                self.record("GET", path=url.path, query=query)
                self.reply(*fake.answer_get(url.path, self.headers.get("Authorization"), query))

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null")
                self.record("POST", path=self.path, body=body)
                self.reply(*fake.answer_post(self.path, self.headers.get("Authorization")))

            def record(self, method, **request):
                fake.requests.append(dict(request, method=method,
                                          authorization=self.headers.get("Authorization")))

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if code == 429 and fake.retry_after:
                    self.send_header("Retry-After", fake.retry_after)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v0.1" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def app(self, slug, title, builds=()):
        self.apps[slug] = {"title": title, "builds": list(builds)}

    def answer_get(self, path, auth, query):
        if path in self.fail:
            return self.fail[path], {"message": "nope"}
        if auth != TOKEN:
            return 401, {"message": "Unauthorized"}
        if path == "/v0.1/apps":
            return 200, self.app_page(query.get("next"))
        parts = path.split("/")
        if len(parts) >= 4 and parts[1] == "v0.1" and parts[2] == "apps":
            app = self.apps.get(parts[3])
            if app is None:
                return 404, {"message": "Not Found"}
            if len(parts) == 5 and parts[4] == "builds":
                if self.gate:
                    try:
                        self.gate.wait()
                    except threading.BrokenBarrierError:
                        return 500, {"message": "builds were fetched one app at a time"}
                return 200, {"data": app["builds"], "paging": {"total_item_count": 0}}
        return 404, {"message": "Not Found"}

    def app_page(self, after):
        slugs = sorted(self.apps)
        start = slugs.index(after) if after in slugs else 0
        page = slugs[start:start + self.page]
        paging = {"total_item_count": len(slugs), "page_item_limit": self.page}
        if start + self.page < len(slugs):
            paging["next"] = slugs[start + self.page]
        data = [{"slug": s, "title": self.apps[s]["title"]} for s in page]
        return {"data": data, "paging": paging}

    def answer_post(self, path, auth):
        if path in self.fail:
            return self.fail[path], {"message": "nope"}
        if auth != TOKEN:
            return 401, {"message": "Unauthorized"}
        parts = path.split("/")
        if (len(parts) < 5 or parts[2] != "apps" or parts[3] not in self.apps
                or parts[4] != "builds"):
            return 404, {"message": "Not Found"}
        if len(parts) == 7 and parts[6] == "abort":
            return 200, {"status": "ok"}
        if len(parts) == 5:
            return 201, {"status": "ok", "message": "webhook processed", "results": [
                {"status": "ok", "build_slug": "new-build", "build_number": 412,
                 "triggered_workflow": "primary"}]}
        return 404, {"message": "Not Found"}

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]


class Plugin:
    """plugin.py as visor runs it: a subprocess speaking JSON-RPC over stdio."""

    def __init__(self, env):
        full = {k: v for k, v in os.environ.items() if k != "BITRISE_TOKEN"}
        full.update(env)
        self.proc = subprocess.Popen(
            [sys.executable, "plugin.py"], cwd=HERE, env=full,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self.lines = []
        self.messages = queue.Queue()
        self.updates = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.stderr = []
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line)
            msg = json.loads(line)
            if msg.get("method") == "widget/update":
                self.updates.put(msg["params"])
            else:
                self.messages.put(msg)

    def _read_stderr(self):
        for line in self.proc.stderr:
            self.stderr.append(line)

    def send(self, msg):
        msg["jsonrpc"] = "2.0"
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def notify(self, method, params):
        self.send({"method": method, "params": params})

    def request(self, method, params):
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        while True:
            msg = self.messages.get(timeout=WAIT)
            if msg.get("id") == self.next_id:
                return msg

    def update(self, timeout=WAIT):
        return self.updates.get(timeout=timeout)

    def update_within(self, seconds):
        """The next update pushed within seconds, or None."""
        try:
            return self.updates.get(timeout=seconds)
        except queue.Empty:
            return None

    def updates_for(self, seconds):
        deadline = time.monotonic() + seconds
        pushed = []
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return pushed
            try:
                pushed.append(self.updates.get(timeout=left))
            except queue.Empty:
                return pushed

    def close(self):
        self.proc.stdin.close()
        code = self.proc.wait(timeout=WAIT)
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code


class BitriseTest(unittest.TestCase):
    def setUp(self):
        self.bitrise = FakeBitrise()
        self.addCleanup(self.bitrise.close)
        self.env = {"BITRISE_API_URL": self.bitrise.url}

    def start(self):
        plugin = Plugin(self.env)
        self.addCleanup(self._stop, plugin)
        result = plugin.request("initialize", {"protocol": 1, "visor": "test"})["result"]
        self.assertEqual(result, {"protocol": 1})
        return plugin

    def _stop(self, plugin):
        if plugin.proc.poll() is None:
            self.assertEqual(plugin.close(), 0)
        for line in plugin.lines + plugin.stderr:
            self.assertNotIn(TOKEN, line)

    def open(self, plugin, widget="b1", visible=True, **settings):
        full = {"apps": "", "limit": 10}
        full.update(settings)
        plugin.notify("widget/open", {
            "widget": widget, "type": "bitrise", "settings": full,
            "size": {"width": 60, "height": 10}, "focused": False, "visible": visible})

    def refresh(self, plugin, widget="b1"):
        return plugin.request("widget/refresh", {"widget": widget})["result"]

    def wait_for(self, condition):
        deadline = time.monotonic() + WAIT
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out waiting")
            time.sleep(0.02)

    def items(self, result):
        self.assertNotIn("status", result)
        return result["payload"]["items"]

    def texts(self, item):
        return ["".join(s.get("text", "") for s in field) for field in item["fields"]]

    def frame(self, plugin, visible, widget="b1"):
        plugin.notify("widget/frame", {
            "widget": widget, "size": {"width": 60, "height": 10}, "focused": False,
            "visible": visible})

    def choices(self, plugin, widget="b1"):
        answer = plugin.request("action/choices", {"widget": widget, "action": "watch"})
        self.assertNotIn("error", answer)
        return answer["result"]["choices"]

    def actions(self, item):
        return {a["id"]: a for a in item.get("actions", [])}

    def invoke(self, plugin, action, item, widget="b1"):
        return plugin.request("action/invoke", {"widget": widget, "action": action, "item": item})

    def rebuild_body(self, b):
        self.bitrise.app("app1", "Rider iOS", [b])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.refresh(plugin)
        result = self.invoke(plugin, "rebuild", b["slug"])["result"]
        self.assertEqual(result, {"toast": "Rebuilding test_pr on Rider iOS"})
        [request] = self.bitrise.posts()
        self.assertEqual(request["path"], "/v0.1/apps/app1/builds")
        self.assertEqual(request["authorization"], TOKEN)
        self.assertEqual(request["body"]["hook_info"], {"type": "bitrise"})
        return request["body"]["build_params"]

    def test_without_a_token_it_asks_for_one(self):
        plugin = self.start()
        self.open(plugin, apps="app1")
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "setup")
        self.assertEqual(status["action"], "secret:token")
        self.assertEqual(self.bitrise.requests, [])

    def test_a_finished_build_shows_its_status_app_workflow_branch_and_age(self):
        self.bitrise.app("app1", "Rider iOS", [
            build("b-1", status=2, workflow="test_pr", branch="feature/x",
                  finished=ago(minutes=5, seconds=10)),
        ])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        [item] = self.items(self.refresh(plugin))
        self.assertEqual(item["id"], "b-1")
        self.assertEqual(self.texts(item), ["✗", "Rider iOS", "test_pr", "feature/x", "5m ago"])
        self.assertEqual(item["fields"][0][0]["token"], "bad")
        self.assertEqual(item["fields"][1][0]["token"], "dim")
        self.assertEqual(item["fields"][4][0]["token"], "faint")
        builds = [r for r in self.bitrise.requests if r["path"] == "/v0.1/apps/app1/builds"]
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["query"], {"sort_by": "running_first", "limit": "10"})
        self.assertTrue(all(r["authorization"] == TOKEN for r in self.bitrise.requests))

    def test_builds_across_apps_run_running_then_on_hold_then_newest_finished_cut_to_limit(self):
        self.bitrise.app("app1", "Rider iOS", [
            build("r-1", status=0, started=ago(minutes=3)),
            build("f-10", status=1, finished=ago(minutes=10)),
            build("f-90", status=1, finished=ago(minutes=90)),
        ])
        self.bitrise.app("app2", "Driver Android", [
            build("h-1", status=0, on_hold=True),
            build("f-2", status=1, finished=ago(minutes=2)),
            build("f-40", status=1, finished=ago(minutes=40)),
        ])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1, app2", limit=4)
        result = self.refresh(plugin)
        self.assertEqual([i["id"] for i in self.items(result)], ["r-1", "h-1", "f-2", "f-10"])
        self.assertEqual(result["payload"]["count"], 1)
        for r in self.bitrise.requests:
            if r["path"].endswith("/builds"):
                self.assertEqual(r["query"]["limit"], "4")

    def test_the_count_is_every_running_build_even_those_past_the_limit(self):
        self.bitrise.app("app1", "Rider iOS", [build("r-%d" % n, status=0) for n in range(3)])
        self.bitrise.app("app2", "Driver Android", [
            build("r-9", status=0),
            build("h-1", status=0, on_hold=True),
        ])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1, app2", limit=2)
        result = self.refresh(plugin)
        self.assertEqual(len(self.items(result)), 2)
        self.assertEqual(result["payload"]["count"], 4)

    def test_each_refresh_asks_bitrise_once_per_watched_app(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        self.bitrise.app("app2", "Driver Android", [build("b-2")])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1, Driver Android")
        self.assertEqual(self.items(self.refresh(plugin))[0]["fields"][1][0]["text"], "Rider iOS")
        self.assertEqual(sorted(r["path"] for r in self.bitrise.requests),
                         ["/v0.1/apps", "/v0.1/apps/app1/builds", "/v0.1/apps/app2/builds"])
        del self.bitrise.requests[:]
        self.refresh(plugin)
        self.assertEqual(sorted(r["path"] for r in self.bitrise.requests),
                         ["/v0.1/apps/app1/builds", "/v0.1/apps/app2/builds"])

    def test_each_status_has_its_glyph_and_colour(self):
        self.bitrise.app("app1", "Rider iOS", [
            build("running", status=0),
            build("held", status=0, on_hold=True),
            build("ok", status=1, finished=ago(minutes=1)),
            build("failed", status=2, finished=ago(minutes=2)),
            build("aborted", status=3, finished=ago(minutes=3)),
            build("aborted-failure", status=4, finished=ago(minutes=4)),
        ])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        glyphs = {i["id"]: (i["fields"][0][0]["text"], i["fields"][0][0]["token"])
                  for i in self.items(self.refresh(plugin))}
        self.assertEqual(glyphs, {
            "running": ("●", "accent"),
            "held": ("◌", "warn"),
            "ok": ("✓", "ok"),
            "failed": ("✗", "bad"),
            "aborted": ("⊘", "faint"),
            "aborted-failure": ("⊘", "faint"),
        })

    def test_a_pr_build_shows_its_pr_number_in_place_of_the_branch(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1", branch="feature/x", pr=4921)])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        [item] = self.items(self.refresh(plugin))
        self.assertEqual(self.texts(item)[3], "#4921")

    def test_a_running_build_shows_how_long_it_has_run(self):
        self.bitrise.app("app1", "Rider iOS", [
            build("started", status=0, triggered=ago(minutes=20),
                  started=ago(minutes=12, seconds=30)),
            build("queued", status=0, triggered=ago(minutes=2, seconds=5)),
            build("long", status=0, started=ago(hours=1, minutes=4, seconds=30)),
        ])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        times = {i["id"]: self.texts(i)[4] for i in self.items(self.refresh(plugin))}
        self.assertRegex(times["started"], r"^12m 3\ds$")
        self.assertRegex(times["queued"], r"^2m 0\ds$")
        self.assertEqual(times["long"], "1h 04m")

    def test_no_builds_shows_an_empty_message(self):
        self.bitrise.app("app1", "Rider iOS", [])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        payload = self.refresh(plugin)["payload"]
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["empty"], "No builds in Rider iOS")

    def test_bitrise_token_in_the_environment_overrides_the_setting(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        self.env["BITRISE_TOKEN"] = TOKEN
        plugin = self.start()
        self.open(plugin, token="a-stale-token", apps="app1")
        self.assertEqual([i["id"] for i in self.items(self.refresh(plugin))], ["b-1"])
        self.assertTrue(all(r["authorization"] == TOKEN for r in self.bitrise.requests))

    def test_a_token_pasted_with_a_line_break_still_works(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        plugin = self.start()
        for token in (TOKEN + "\n", " " + TOKEN + "\r\n"):
            with self.subTest(token=token):
                self.open(plugin, token=token, apps="app1")
                self.assertEqual([i["id"] for i in self.items(self.refresh(plugin))], ["b-1"])
        self.assertTrue(all(r["authorization"] == TOKEN for r in self.bitrise.requests))

    def test_a_token_no_header_can_carry_asks_for_a_new_one_without_echoing_it(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        plugin = self.start()
        self.open(plugin, token=TOKEN + "\nX-Evil: 1", apps="app1")
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "setup")
        self.assertEqual(status["action"], "secret:token")
        answer = plugin.request("action/choices", {"widget": "b1", "action": "watch"})
        self.assertIn("token", answer["error"]["message"])
        self.assertEqual(self.bitrise.requests, [])

    def test_a_rejected_token_asks_for_a_new_one(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        for code in (401, 403):
            with self.subTest(code=code):
                self.bitrise.fail = {"/v0.1/apps/app1/builds": code}
                plugin = self.start()
                self.open(plugin, token=TOKEN, apps="app1")
                status = self.refresh(plugin)["status"]
                self.assertEqual(status["state"], "setup")
                self.assertEqual(status["action"], "secret:token")
                self.assertIn("rejected", status["message"])

    def test_no_watched_apps_needs_setup(self):
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps=" , ")
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "setup")
        self.assertEqual(status["action"],
                         {"id": "watch", "label": "Watch app…", "input": "choice"})
        self.assertEqual(self.bitrise.requests, [])

    def test_the_list_offers_watch_app(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        actions = self.refresh(plugin)["payload"]["actions"]
        self.assertIn({"id": "watch", "label": "Watch app…", "input": "choice"}, actions)

    def test_an_app_bitrise_does_not_know_is_an_error_naming_it(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1,typo-slug")
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "error")
        self.assertIn("typo-slug", status["message"])

    def test_app_titles_watch_their_apps_whatever_their_case(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        self.bitrise.app("app2", "Driver Android", [build("b-2")])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="rider IOS, app2")
        self.assertEqual({i["id"] for i in self.items(self.refresh(plugin))}, {"b-1", "b-2"})
        self.assertIn("/v0.1/apps/app1/builds", [r["path"] for r in self.bitrise.requests])

    def test_a_title_matching_no_app_is_an_error_naming_it(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="Rider iOS, Rider Android")
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "error")
        self.assertIn("Rider Android", status["message"])

    def test_a_bitrise_failure_is_an_error_without_a_payload(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        self.bitrise.fail = {"/v0.1/apps/app1/builds": 500}
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        result = self.refresh(plugin)
        self.assertNotIn("payload", result)
        self.assertEqual(result["status"]["state"], "error")
        self.assertIn("500", result["status"]["message"])

    def test_a_rate_limit_waits_as_long_as_bitrise_asks_or_a_minute(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        self.bitrise.fail = {"/v0.1/apps/app1/builds": 429}
        for retry_after, wait in (("120", 120), (None, 60)):
            with self.subTest(retry_after=retry_after):
                self.bitrise.retry_after = retry_after
                plugin = self.start()
                self.open(plugin, token=TOKEN, apps="app1")
                result = self.refresh(plugin)
                self.assertNotIn("payload", result)
                self.assertEqual(result["status"]["state"], "rate_limited")
                until = datetime.strptime(result["status"]["until"], "%Y-%m-%dT%H:%M:%SZ")
                left = until.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
                self.assertAlmostEqual(left.total_seconds(), wait, delta=3)

    def test_a_rate_limited_widget_asks_bitrise_nothing_until_then(self):
        self.env["BITRISE_FAST_POLL"] = "0.3"
        self.bitrise.app("app1", "Rider iOS", [build("held", status=0, on_hold=True)])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.refresh(plugin)
        self.bitrise.fail = {"/v0.1/apps/app1/builds": 429}
        self.bitrise.retry_after = "60"
        pushed = plugin.update(timeout=2)
        self.assertNotIn("payload", pushed)
        self.assertEqual(pushed["status"]["state"], "rate_limited")
        asked = len(self.bitrise.requests)
        self.assertIsNone(plugin.update_within(1))
        self.assertEqual(self.refresh(plugin)["status"], pushed["status"])
        self.assertEqual(len(self.bitrise.requests), asked)

    def test_a_failed_fetch_while_running_pushes_an_error_and_stops_ticking(self):
        self.env["BITRISE_FAST_POLL"] = "0.3"
        self.bitrise.app("app1", "Rider iOS", [build("run", status=0, started=ago(seconds=5))])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.refresh(plugin)
        self.bitrise.fail = {"/v0.1/apps/app1/builds": 502}
        while True:
            pushed = plugin.update(timeout=2)
            if "status" in pushed:
                break
        self.assertNotIn("payload", pushed)
        self.assertEqual(pushed["status"]["state"], "error")
        for pushed in plugin.updates_for(1.5):
            self.assertNotIn("payload", pushed)

    def test_an_unreachable_bitrise_is_an_error(self):
        self.env["BITRISE_API_URL"] = "http://127.0.0.1:9/v0.1"
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        result = self.refresh(plugin)
        self.assertNotIn("payload", result)
        self.assertEqual(result["status"]["state"], "error")

    def test_the_watched_apps_are_fetched_at_once(self):
        for n in range(3):
            self.bitrise.app("app%d" % n, "App %d" % n, [build("b-%d" % n)])
        self.bitrise.gate = threading.Barrier(3, timeout=3)
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app0,app1,app2")
        self.assertEqual(len(self.items(self.refresh(plugin))), 3)

    def test_a_second_open_replaces_the_widget_settings(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        plugin = self.start()
        self.open(plugin, apps="app1")
        self.open(plugin, token=TOKEN, apps="app1")
        self.assertEqual([i["id"] for i in self.items(self.refresh(plugin))], ["b-1"])

    def test_a_running_build_ticks_every_second_without_asking_bitrise(self):
        self.bitrise.app("app1", "Rider iOS", [
            build("run", status=0, started=ago(seconds=5)),
            build("done", status=1, finished=ago(minutes=5)),
        ])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        first = {i["id"]: self.texts(i) for i in self.items(self.refresh(plugin))}
        asked = len(self.bitrise.requests)
        pushed = plugin.update(timeout=2.5)
        self.assertEqual(pushed["widget"], "b1")
        self.assertNotIn("status", pushed)
        now = {i["id"]: self.texts(i) for i in pushed["payload"]["items"]}
        self.assertEqual(now["done"], first["done"])
        self.assertEqual(now["run"][:4], first["run"][:4])
        self.assertGreater(int(now["run"][4].rstrip("s")), int(first["run"][4].rstrip("s")))
        self.assertEqual(len(self.bitrise.requests), asked)

    def test_a_hidden_widget_stops_ticking_until_shown_again(self):
        self.bitrise.app("app1", "Rider iOS", [build("run", status=0, started=ago(seconds=5))])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.refresh(plugin)
        self.frame(plugin, visible=False)
        self.assertIsNone(plugin.update_within(1.6))
        self.frame(plugin, visible=True)
        self.assertEqual(plugin.update(timeout=2.5)["widget"], "b1")

    def test_a_widget_opened_hidden_does_not_tick(self):
        self.bitrise.app("app1", "Rider iOS", [build("run", status=0, started=ago(seconds=5))])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1", visible=False)
        self.refresh(plugin)
        self.assertIsNone(plugin.update_within(1.6))

    def test_a_closed_widget_stops_ticking(self):
        self.bitrise.app("app1", "Rider iOS", [build("run", status=0, started=ago(seconds=5))])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.open(plugin, widget="b2", token=TOKEN, apps="app1", visible=False)
        self.refresh(plugin)
        plugin.notify("widget/close", {"widget": "b1"})
        self.assertIsNone(plugin.update_within(1.6))

    def test_finished_builds_neither_tick_nor_poll(self):
        self.env["BITRISE_FAST_POLL"] = "0.3"
        self.bitrise.app("app1", "Rider iOS", [build("done", status=1, finished=ago(minutes=5))])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.refresh(plugin)
        asked = len(self.bitrise.requests)
        self.assertIsNone(plugin.update_within(1.6))
        self.assertEqual(len(self.bitrise.requests), asked)

    def test_a_fetch_in_flight_when_the_widget_closes_brings_none_of_its_builds_back(self):
        self.env["BITRISE_FAST_POLL"] = "0.3"
        self.bitrise.app("app1", "Rider iOS", [build("held", status=0, on_hold=True)])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.refresh(plugin)
        held = threading.Event()
        self.addCleanup(held.set)
        self.bitrise.gate = held
        asked = len(self.bitrise.requests)
        self.wait_for(lambda: len(self.bitrise.requests) > asked)
        plugin.notify("widget/close", {"widget": "b1"})
        held.set()
        self.assertIsNone(plugin.update_within(0.6))
        answer = self.invoke(plugin, "abort", "held")
        self.assertIn("held", answer["error"]["message"])
        self.assertEqual(self.bitrise.posts(), [])

    def test_a_fetch_that_trips_the_plugin_is_an_error_and_fetching_goes_on(self):
        self.env["BITRISE_FAST_POLL"] = "0.3"
        self.bitrise.app("app1", "Rider iOS", [build("held", status=0, on_hold=True)])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.refresh(plugin)
        self.bitrise.app("app1", "Rider iOS", [{"status": 0, "is_on_hold": True}])
        pushed = plugin.update(timeout=2)
        self.assertNotIn("payload", pushed)
        self.assertEqual(pushed["status"]["state"], "error")
        self.bitrise.app("app1", "Rider iOS", [build("held", status=1, finished=ago(seconds=1))])
        while "status" in pushed:
            pushed = plugin.update(timeout=2)
        self.assertEqual([i["id"] for i in pushed["payload"]["items"]], ["held"])
        self.assertEqual(plugin.stderr, [])

    def test_a_limit_that_is_not_a_number_is_an_error_naming_it(self):
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1", limit="lots")
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "error")
        self.assertIn("limit", status["message"])
        self.assertEqual(self.bitrise.requests, [])

    def test_a_build_on_hold_is_fetched_again_until_it_finishes(self):
        self.env["BITRISE_FAST_POLL"] = "0.3"
        self.bitrise.app("app1", "Rider iOS", [build("held", status=0, on_hold=True)])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.refresh(plugin)
        self.bitrise.app("app1", "Rider iOS", [build("held", status=1, finished=ago(seconds=1))])
        pushed = plugin.update(timeout=2)
        self.assertEqual(pushed["widget"], "b1")
        [item] = pushed["payload"]["items"]
        self.assertEqual(self.texts(item)[0], "✓")
        asked = len(self.bitrise.requests)
        self.assertIsNone(plugin.update_within(1))
        self.assertEqual(len(self.bitrise.requests), asked)

    def test_two_widgets_keep_their_own_builds_and_timers(self):
        self.bitrise.app("app1", "Rider iOS", [build("r-1", status=0, started=ago(seconds=5))])
        self.bitrise.app("app2", "Driver Android",
                         [build("r-2", status=0, started=ago(seconds=5))])
        plugin = self.start()
        self.open(plugin, widget="b1", token=TOKEN, apps="app1")
        self.open(plugin, widget="b2", token=TOKEN, apps="app2")
        self.refresh(plugin, widget="b1")
        self.refresh(plugin, widget="b2")
        self.frame(plugin, widget="b1", visible=False)
        plugin.updates_for(0.2)
        pushed = plugin.updates_for(1.5)
        self.assertTrue(pushed)
        for update in pushed:
            self.assertEqual(update["widget"], "b2")
            self.assertEqual([i["id"] for i in update["payload"]["items"]], ["r-2"])

    def test_watch_app_offers_the_apps_the_token_sees_but_not_the_watched_ones(self):
        self.bitrise.app("app1", "Rider iOS")
        self.bitrise.app("app2", "Driver Android")
        self.bitrise.app("app3", "Back Office")
        self.bitrise.page = 2
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.assertEqual(self.choices(plugin), [
            {"id": "app3", "label": "Back Office"},
            {"id": "app2", "label": "Driver Android"},
        ])

    def test_watch_app_without_a_token_says_so_without_asking_bitrise(self):
        plugin = self.start()
        self.open(plugin)
        answer = plugin.request("action/choices", {"widget": "b1", "action": "watch"})
        self.assertIn("token", answer["error"]["message"])
        self.assertEqual(self.bitrise.requests, [])

    def test_watch_app_with_a_rejected_token_says_so(self):
        self.bitrise.app("app1", "Rider iOS")
        plugin = self.start()
        self.open(plugin, token="a-stale-token")
        answer = plugin.request("action/choices", {"widget": "b1", "action": "watch"})
        self.assertIn("rejected", answer["error"]["message"])

    def test_picking_an_app_appends_its_slug_to_the_apps_setting(self):
        self.bitrise.app("app1", "Rider iOS")
        self.bitrise.app("app2", "Driver Android")
        plugin = self.start()
        for apps, written in (("app1", "app1, app2"), ("", "app2")):
            with self.subTest(apps=apps):
                self.open(plugin, token=TOKEN, apps=apps)
                [choice] = [c for c in self.choices(plugin) if c["id"] == "app2"]
                answer = plugin.request("action/invoke", {
                    "widget": "b1", "action": "watch", "choice": choice["id"]})
                self.assertEqual(answer["result"]["settings"], {"apps": written})
                self.assertIn("Driver Android", answer["result"]["toast"])

    def test_a_finished_branch_build_opens_copies_and_rebuilds(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1", status=2)])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        [item] = self.items(self.refresh(plugin))
        actions = self.actions(item)
        self.assertEqual(sorted(actions), ["copy", "open", "rebuild"])
        url = "https://app.bitrise.io/build/b-1"
        self.assertEqual(actions["open"], {
            "id": "open", "label": "Open build", "primary": True, "verb": "open", "target": url})
        self.assertEqual(actions["copy"], {
            "id": "copy", "key": "y", "label": "Copy build URL", "verb": "copy", "target": url})
        self.assertEqual(actions["rebuild"], {
            "id": "rebuild", "key": "R", "label": "Rebuild", "confirm": True})

    def test_a_running_pr_build_also_opens_its_pr_and_aborts(self):
        self.bitrise.app("app1", "Rider iOS", [
            build("run", status=0, pr=4921),
            build("held", status=0, on_hold=True),
        ])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        items = {i["id"]: self.actions(i) for i in self.items(self.refresh(plugin))}
        self.assertEqual(sorted(items["run"]), ["abort", "copy", "open", "pr", "rebuild"])
        self.assertEqual(items["run"]["pr"], {
            "id": "pr", "key": "p", "label": "Open pull request", "verb": "open",
            "target": "https://github.com/o/r/pull/4921"})
        self.assertEqual(items["run"]["abort"], {
            "id": "abort", "key": "x", "label": "Abort build", "confirm": True})
        self.assertEqual(sorted(items["held"]), ["abort", "copy", "open", "rebuild"])

    def test_abort_stops_the_build_and_says_so(self):
        self.bitrise.app("app1", "Rider iOS", [build("run", status=0, workflow="test_pr")])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.refresh(plugin)
        result = self.invoke(plugin, "abort", "run")["result"]
        self.assertEqual(self.bitrise.posts(), [{
            "method": "POST",
            "path": "/v0.1/apps/app1/builds/run/abort",
            "body": {"abort_reason": "Aborted from visor", "abort_with_success": False,
                     "skip_git_status_report": False, "skip_notifications": False},
            "authorization": TOKEN,
        }])
        self.assertEqual(result, {"toast": "Aborted test_pr on Rider iOS"})

    def test_rebuild_resends_the_original_build_params(self):
        original = {
            "branch": "feature/x", "commit_hash": "abc123", "workflow_id": "test_pr",
            "pull_request_id": 4921, "branch_dest": "main",
            "pull_request_merge_branch": "pull/4921/merge",
            "environments": [{"mapped_to": "FLAVOR", "value": "qa", "is_expand": True}],
        }
        b = build("b-1", status=2, workflow="test_pr", branch="feature/x", pr=4921)
        b.update({"commit_hash": "abc123", "original_build_params": original})
        self.assertEqual(self.rebuild_body(b), original)

    def test_rebuild_without_original_params_reruns_the_branch_workflow_and_commit(self):
        b = build("b-1", status=2, workflow="test_pr", branch="feature/x")
        b["commit_hash"] = "abc123"
        self.assertEqual(self.rebuild_body(b), {
            "branch": "feature/x", "workflow_id": "test_pr", "commit_hash": "abc123"})

    def test_rebuild_of_a_pr_build_without_original_params_keeps_the_pr(self):
        b = build("b-1", status=2, workflow="test_pr", branch="feature/x", pr=4921)
        b.update({"commit_hash": "abc123", "pull_request_target_branch": "main"})
        self.assertEqual(self.rebuild_body(b), {
            "branch": "feature/x", "workflow_id": "test_pr", "commit_hash": "abc123",
            "pull_request_id": 4921, "branch_dest": "main"})

    def test_an_abort_bitrise_refuses_is_a_jsonrpc_error(self):
        self.bitrise.app("app1", "Rider iOS", [build("run", status=0)])
        self.bitrise.fail = {"/v0.1/apps/app1/builds/run/abort": 400}
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.refresh(plugin)
        answer = self.invoke(plugin, "abort", "run")
        self.assertNotIn("result", answer)
        self.assertIn("400", answer["error"]["message"])

    def test_an_action_on_a_build_not_listed_is_a_jsonrpc_error(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        self.refresh(plugin)
        answer = self.invoke(plugin, "rebuild", "gone")
        self.assertNotIn("result", answer)
        self.assertIn("gone", answer["error"]["message"])
        self.assertEqual(self.bitrise.posts(), [])

    def test_an_unknown_action_is_a_jsonrpc_error(self):
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        answer = plugin.request("action/invoke", {"widget": "b1", "action": "made-up"})
        self.assertNotIn("result", answer)
        self.assertIn("made-up", answer["error"]["message"])


if __name__ == "__main__":
    unittest.main()
