import json
import os
import queue
import subprocess
import sys
import threading
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
        self.gate = None
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                fake.requests.append({
                    "method": "GET",
                    "path": url.path,
                    "query": {k: v[0] for k, v in parse_qs(url.query).items()},
                    "authorization": self.headers.get("Authorization"),
                })
                code, body = fake.answer(url.path, self.headers.get("Authorization"))
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v0.1" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def app(self, slug, title, builds=()):
        self.apps[slug] = {"title": title, "builds": list(builds)}

    def answer(self, path, auth):
        if path in self.fail:
            return self.fail[path], {"message": "nope"}
        if auth != TOKEN:
            return 401, {"message": "Unauthorized"}
        parts = path.split("/")
        if len(parts) >= 4 and parts[1] == "v0.1" and parts[2] == "apps":
            app = self.apps.get(parts[3])
            if app is None:
                return 404, {"message": "Not Found"}
            if len(parts) == 4:
                return 200, {"data": {"slug": parts[3], "title": app["title"]}}
            if len(parts) == 5 and parts[4] == "builds":
                if self.gate:
                    try:
                        self.gate.wait()
                    except threading.BrokenBarrierError:
                        return 500, {"message": "builds were fetched one app at a time"}
                return 200, {"data": app["builds"], "paging": {"total_item_count": 0}}
        return 404, {"message": "Not Found"}


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
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.stderr = []
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line)
            self.messages.put(json.loads(line))

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

    def open(self, plugin, widget="b1", **settings):
        full = {"apps": "", "limit": 10}
        full.update(settings)
        plugin.notify("widget/open", {
            "widget": widget, "type": "bitrise", "settings": full,
            "size": {"width": 60, "height": 10}, "focused": False, "visible": True})

    def refresh(self, plugin, widget="b1"):
        return plugin.request("widget/refresh", {"widget": widget})["result"]

    def test_without_a_token_it_asks_for_one(self):
        plugin = self.start()
        self.open(plugin, apps="app1")
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "setup")
        self.assertEqual(status["action"], "secret:token")
        self.assertEqual(self.bitrise.requests, [])

    def items(self, result):
        self.assertNotIn("status", result)
        return result["payload"]["items"]

    def texts(self, item):
        return ["".join(s.get("text", "") for s in field) for field in item["fields"]]

    def test_a_finished_build_shows_its_status_app_workflow_branch_and_age(self):
        self.bitrise.app("app1", "Rider iOS", [
            build("b-1", status=2, workflow="test_pr", branch="feature/x", finished=ago(minutes=5, seconds=10)),
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
            build("started", status=0, triggered=ago(minutes=20), started=ago(minutes=12, seconds=30)),
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
        self.assertIn("apps", status["message"])
        self.assertEqual(self.bitrise.requests, [])

    def test_an_app_bitrise_does_not_know_is_an_error_naming_it(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1,typo-slug")
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "error")
        self.assertIn("typo-slug", status["message"])

    def test_a_bitrise_failure_is_an_error_without_a_payload(self):
        self.bitrise.app("app1", "Rider iOS", [build("b-1")])
        self.bitrise.fail = {"/v0.1/apps/app1/builds": 500}
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        result = self.refresh(plugin)
        self.assertNotIn("payload", result)
        self.assertEqual(result["status"]["state"], "error")
        self.assertIn("500", result["status"]["message"])

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

    def test_an_unknown_action_is_a_jsonrpc_error(self):
        plugin = self.start()
        self.open(plugin, token=TOKEN, apps="app1")
        answer = plugin.request("action/invoke", {"widget": "b1", "action": "made-up"})
        self.assertNotIn("result", answer)
        self.assertIn("made-up", answer["error"]["message"])


if __name__ == "__main__":
    unittest.main()
