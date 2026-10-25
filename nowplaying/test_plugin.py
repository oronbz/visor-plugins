import base64
import json
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WAIT = 10

FAKE = r'''
import json, os, sys, time

DIR = os.environ["FAKE_DIR"]
TOOL = os.path.basename(sys.argv[0])
args = sys.argv[1:]


def tail(render):
    with open(os.path.join(DIR, "stream"), "a"):
        pass
    with open(os.path.join(DIR, "stream"), "rb") as records:
        while True:
            at = records.tell()
            line = records.readline()
            if not line.endswith(b"\n"):
                records.seek(at)
                time.sleep(0.02)
                continue
            sys.stdout.write(render(json.loads(line)) + "\n")
            sys.stdout.flush()


def playerctl_line(record, template):
    if not record:
        return ""
    out = template
    for key, value in record.items():
        out = out.replace("{{%s}}" % key, str(value))
    while "{{" in out:
        start = out.index("{{")
        out = out[:start] + out[out.index("}}", start) + 2:]
    return out


if TOOL == "media-control" and args == ["test"]:
    path = os.path.join(DIR, "test-exit")
    sys.exit(int(open(path).read()) if os.path.exists(path) else 0)
if TOOL == "media-control" and args[:1] == ["stream"]:
    tail(lambda record: json.dumps(record))
if TOOL == "sips":
    time.sleep(0.2)
    with open(args[3], "rb") as source, open(args[args.index("--out") + 1], "wb") as out:
        out.write(b"square " + source.read())
if TOOL == "playerctl" and "--follow" in args:
    template = args[args.index("--format") + 1]
    tail(lambda record: playerctl_line(record, template))
with open(os.path.join(DIR, "calls"), "a") as calls:
    calls.write(json.dumps(args) + "\n")
'''

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
JPEG = b"\xff\xd8\xff\xe0 not really a jpeg"


def png(width, height):
    """A PNG's signature and header, all the Plugin reads of an image."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + header
            + struct.pack(">I", zlib.crc32(b"IHDR" + header)))


def stamp(seconds_ago=0):
    when = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def track(**kw):
    """A full media-control payload, paused 30s into a 4-minute track."""
    record = {"title": "Teardrop", "artist": "Massive Attack", "album": "Mezzanine",
              "duration": 240.0, "elapsedTime": 30.0, "timestamp": stamp(),
              "playbackRate": 0, "playing": False}
    record.update(kw)
    return record


def mpris(**kw):
    """What playerctl's metadata holds for a paused track 30s in."""
    record = {"status": "Paused", "position": 30000000, "mpris:length": 240000000,
              "title": "Teardrop", "artist": "Massive Attack", "album": "Mezzanine"}
    record.update(kw)
    return record


class Plugin:
    """plugin.py as visor runs it: a subprocess speaking JSON-RPC over stdio."""

    def __init__(self, env):
        self.proc = subprocess.Popen(
            [sys.executable, "plugin.py"], cwd=HERE, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self.messages = queue.Queue()
        self.updates = queue.Queue()
        self.next_id = 0
        self.stderr = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
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

    def drain(self):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and self.update_within(0.3) is not None:
            pass

    def close(self):
        self.proc.stdin.close()
        code = self.proc.wait(timeout=WAIT)
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code


class NowPlayingTest(unittest.TestCase):
    platform = "darwin"
    tools = ("media-control",)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.bin = os.path.join(self.dir, "bin")
        self.fake = os.path.join(self.dir, "fake")
        self.cache = os.path.join(self.dir, "cache")
        os.mkdir(self.bin)
        os.mkdir(self.fake)
        for tool in self.tools:
            self.install(tool)
        self.env = {"PATH": self.bin, "FAKE_DIR": self.fake, "NOWPLAYING_PLATFORM": self.platform,
                    "NOWPLAYING_CACHE_DIR": self.cache, "HOME": self.dir}

    def install(self, tool):
        path = os.path.join(self.bin, tool)
        with open(path, "w") as f:
            f.write("#!" + sys.executable + "\n" + FAKE)
        os.chmod(path, 0o755)

    def start(self):
        plugin = Plugin(self.env)
        self.addCleanup(self._stop, plugin)
        result = plugin.request("initialize", {"protocol": 1, "visor": "test"})["result"]
        self.assertEqual(result, {"protocol": 1})
        return plugin

    def _stop(self, plugin):
        if plugin.proc.poll() is None:
            self.assertEqual(plugin.close(), 0)
        self.assertEqual(plugin.stderr, [])

    def emit(self, record, diff=False):
        """Has the fake tool print one more record on its stream."""
        if self.platform == "darwin":
            record = {"type": "data", "diff": diff, "payload": record}
        with open(os.path.join(self.fake, "stream"), "a") as f:
            f.write(json.dumps(record) + "\n")

    def calls(self):
        path = os.path.join(self.fake, "calls")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f]

    def open(self, plugin, widget="n1", visible=True, width=60, height=8, **settings):
        full = {"seek": "10s", "visualizer": True}
        full.update(settings)
        plugin.notify("widget/open", {
            "widget": widget, "type": "nowplaying", "settings": full,
            "size": {"width": width, "height": height}, "focused": False, "visible": visible})

    def frame(self, plugin, visible, widget="n1", width=60, height=8):
        plugin.notify("widget/frame", {
            "widget": widget, "size": {"width": width, "height": height}, "focused": False,
            "visible": visible})

    def refresh(self, plugin, widget="n1"):
        return plugin.request("widget/refresh", {"widget": widget})["result"]

    def invoke(self, plugin, action, widget="n1"):
        return plugin.request("action/invoke", {"widget": widget, "action": action})

    def wait_for(self, condition):
        deadline = time.monotonic() + WAIT
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out waiting")
            time.sleep(0.02)

    def pushed_until(self, plugin, condition):
        deadline = time.monotonic() + WAIT
        while True:
            update = plugin.update(timeout=max(0.01, deadline - time.monotonic()))
            if "payload" in update and condition(update["payload"]):
                return update["payload"]

    def showing(self, plugin, condition):
        """The Widget's payload once it meets condition, refreshed or pushed."""
        result = self.refresh(plugin)
        if "payload" in result and condition(result["payload"]):
            return result["payload"]
        return self.pushed_until(plugin, condition)

    def texts(self, payload):
        return ["".join(s.get("text", "") for s in line.get("spans", []))
                for line in payload["lines"] if "spans" in line]

    def charts(self, payload):
        return [line["chart"] for line in payload["lines"] if "chart" in line]

    def progress(self, payload):
        """The spans of the line with the ▶ or ⏸, or None on a payload with no track."""
        for line in payload["lines"]:
            spans = line.get("spans", [])
            if spans and spans[0].get("text") in ("▶ ", "⏸ "):
                return spans
        return None

    def glyph(self, payload):
        spans = self.progress(payload)
        return spans and spans[0]["text"]

    def elapsed(self, payload):
        spans = self.progress(payload)
        return spans and spans[1]["text"].strip()


class Shared:
    """What both backends must do, given a NowPlayingTest subclass's tool and records."""

    def test_no_session_shows_a_centred_dim_nothing_playing(self):
        self.paused()
        plugin = self.start()
        self.open(plugin)
        self.showing(plugin, lambda p: self.texts(p)[0] == "Teardrop")
        self.emit({})
        payload = self.pushed_until(plugin, lambda p: "image" not in p and
                                    self.texts(p) == ["Nothing playing"])
        self.assertTrue(payload["center"])
        self.assertEqual(payload["lines"][0]["spans"][0]["token"], "dim")

    def test_a_missing_tool_needs_setup_with_how_to_install_it(self):
        os.remove(os.path.join(self.bin, self.tools[0]))
        plugin = self.start()
        self.open(plugin)
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "setup")
        self.assertIn(self.install_hint, status["message"])
        self.assertEqual(status["action"]["target"], self.install_target)
        self.assertIn(status["action"]["verb"], ("copy", "open"))

    def test_a_missing_tool_is_reported_once_through_the_refresh(self):
        os.remove(os.path.join(self.bin, self.tools[0]))
        plugin = self.start()
        self.open(plugin)
        self.assertIsNone(plugin.update_within(0.5))
        self.assertEqual(self.refresh(plugin)["status"]["state"], "setup")
        self.assertIsNone(plugin.update_within(1))

    def test_each_action_runs_its_command_and_seeks_by_the_seek_setting(self):
        self.paused(at=100)
        plugin = self.start()
        self.open(plugin, seek="15s")
        payload = self.showing(plugin, lambda p: self.elapsed(p) == "1:40")
        keys = {a["id"]: a["key"] for a in payload["actions"]}
        self.assertEqual(keys, {"toggle": "space", "next": "n", "previous": "p",
                                "back": "[", "forward": "]"})
        for action in ("toggle", "next", "previous", "back", "back", "forward"):
            self.assertEqual(self.invoke(plugin, action)["result"], {})
        self.assertEqual(self.calls(), self.commands + [self.seek_to(85), self.seek_to(70),
                                                        self.seek_to(85)])

    def test_a_seek_shows_the_new_position_before_the_player_reports_it(self):
        self.paused(at=100)
        plugin = self.start()
        self.open(plugin)
        self.showing(plugin, lambda p: self.elapsed(p) == "1:40")
        self.invoke(plugin, "forward")
        self.pushed_until(plugin, lambda p: self.elapsed(p) == "1:50")

    def test_seeking_stops_at_the_start_and_the_end_of_the_track(self):
        self.paused(at=5)
        plugin = self.start()
        self.open(plugin, seek="1m")
        self.showing(plugin, lambda p: self.elapsed(p) == "0:05")
        self.invoke(plugin, "back")
        self.invoke(plugin, "forward")
        self.invoke(plugin, "forward")
        self.invoke(plugin, "forward")
        self.invoke(plugin, "forward")
        self.invoke(plugin, "forward")
        self.assertEqual(self.calls()[0], self.seek_to(0))
        self.assertEqual(self.calls()[-1], self.seek_to(240))

    def test_a_paused_widget_pushes_nothing_until_the_track_changes(self):
        self.paused()
        plugin = self.start()
        self.open(plugin)
        self.showing(plugin, lambda p: self.texts(p)[0] == "Teardrop")
        plugin.drain()
        self.assertIsNone(plugin.update_within(1.2))
        self.paused(title="Angel")
        self.pushed_until(plugin, lambda p: self.texts(p)[0] == "Angel")

    def test_the_same_track_reported_again_pushes_nothing(self):
        self.paused()
        plugin = self.start()
        self.open(plugin)
        self.showing(plugin, lambda p: self.texts(p)[0] == "Teardrop")
        plugin.drain()
        self.paused()
        self.assertIsNone(plugin.update_within(1))

    def test_playing_and_visible_it_pushes_about_four_frames_a_second(self):
        self.playing()
        plugin = self.start()
        self.open(plugin)
        payload = self.showing(plugin, lambda p: self.glyph(p) == "▶ ")
        pushed = plugin.updates_for(1.1)
        self.assertGreaterEqual(len(pushed), 3)
        self.assertLessEqual(len(pushed), 6)
        frames = [tuple(self.charts(u["payload"])[-1]["values"]) for u in pushed]
        self.assertGreater(len(set(frames)), 2)
        self.assertEqual(len(frames[0]), 120)
        self.assertGreater(self.progress(pushed[-1]["payload"])[2]["bar"]["value"],
                           self.progress(payload)[2]["bar"]["value"])

    def test_a_hidden_widget_pushes_nothing_until_shown_again(self):
        self.playing()
        plugin = self.start()
        self.open(plugin)
        self.showing(plugin, lambda p: self.glyph(p) == "▶ ")
        self.frame(plugin, visible=False)
        plugin.drain()
        self.assertIsNone(plugin.update_within(1.2))
        self.frame(plugin, visible=True)
        self.assertIsNotNone(plugin.update_within(1))

    def test_a_widget_opened_hidden_does_not_animate(self):
        self.playing()
        plugin = self.start()
        self.open(plugin, visible=False)
        self.showing(plugin, lambda p: self.glyph(p) == "▶ ")
        plugin.drain()
        self.assertIsNone(plugin.update_within(1.2))

    def test_a_closed_widget_stops_pushing(self):
        self.playing()
        plugin = self.start()
        self.open(plugin)
        self.showing(plugin, lambda p: self.glyph(p) == "▶ ")
        plugin.notify("widget/close", {"widget": "n1"})
        plugin.drain()
        self.assertIsNone(plugin.update_within(1))

    def test_a_track_with_only_a_title_animates_the_one_row_it_leaves(self):
        self.playing(artist="", album="")
        plugin = self.start()
        self.open(plugin, height=3)
        self.showing(plugin, lambda p: self.glyph(p) == "▶ ")
        self.assertGreaterEqual(len(plugin.updates_for(1.1)), 3)

    def test_without_the_visualizer_it_has_no_bars_and_ticks_each_second(self):
        self.playing()
        plugin = self.start()
        self.open(plugin, visualizer=False)
        payload = self.showing(plugin, lambda p: self.glyph(p) == "▶ ")
        self.assertEqual(self.charts(payload), [])
        pushed = plugin.updates_for(2.2)
        self.assertGreaterEqual(len(pushed), 1)
        self.assertLessEqual(len(pushed), 3)
        for update in pushed:
            self.assertEqual(self.charts(update["payload"]), [])

    def test_the_visualizer_fills_the_rows_left_in_three_colours(self):
        self.paused()
        plugin = self.start()
        self.open(plugin, height=10)
        payload = self.showing(plugin, lambda p: self.texts(p)[0] == "Teardrop")
        self.assertEqual([(c["token"], c["rows"]) for c in self.charts(payload)],
                         [("info", 2), ("accent", 2), ("alt", 2)])

    def test_a_short_tile_has_fewer_colours_and_one_with_no_room_none(self):
        self.paused()
        plugin = self.start()
        for height, bands in ((6, [("accent", 1), ("alt", 1)]), (4, [])):
            with self.subTest(height=height):
                self.open(plugin, height=height)
                payload = self.showing(plugin, lambda p: self.texts(p)[0] == "Teardrop")
                self.assertEqual([(c["token"], c["rows"]) for c in self.charts(payload)], bands)

    def test_paused_the_visualizer_is_one_flat_still_row_of_dots(self):
        self.paused()
        plugin = self.start()
        self.open(plugin, height=8)
        payload = self.showing(plugin, lambda p: self.texts(p)[0] == "Teardrop")
        *above, bottom = self.charts(payload)
        self.assertEqual(set(bottom["values"]), {1 / 8})
        for chart in above:
            self.assertEqual(set(chart["values"]), {0})

    def test_a_track_with_only_a_title_still_shows_its_progress(self):
        self.paused(artist="", album="")
        plugin = self.start()
        self.open(plugin)
        payload = self.showing(plugin, lambda p: self.texts(p)[0] == "Teardrop")
        self.assertEqual(self.texts(payload), ["Teardrop", "⏸ 0:30  4:00"])

    def test_a_seek_setting_that_is_not_a_duration_is_an_error_naming_it(self):
        self.paused()
        plugin = self.start()
        self.open(plugin, seek="soon")
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "error")
        self.assertIn("seek", status["message"])

    def test_an_unknown_action_is_a_jsonrpc_error(self):
        plugin = self.start()
        self.open(plugin)
        answer = self.invoke(plugin, "made-up")
        self.assertIn("made-up", answer["error"]["message"])


class MediaControlTest(Shared, NowPlayingTest):
    install_hint = "brew tap ungive/media-control && brew install media-control"
    install_target = install_hint
    commands = [["toggle-play-pause"], ["next-track"], ["previous-track"]]

    def seek_to(self, seconds):
        return ["seek", str(seconds)]

    def paused(self, at=30, **kw):
        self.emit(track(elapsedTime=float(at), **kw))

    def playing(self, at=30, **kw):
        self.emit(track(elapsedTime=float(at), playing=True, playbackRate=1, **kw))

    def test_a_full_record_then_diffs_show_the_merged_track(self):
        self.emit(track())
        plugin = self.start()
        self.open(plugin)
        payload = self.refresh(plugin)["payload"]
        self.assertEqual(self.texts(payload)[:3], ["Teardrop", "Massive Attack", "Mezzanine"])
        self.assertEqual(payload["lines"][0]["spans"][0]["bold"], True)
        self.assertEqual(self.progress(payload)[0]["text"], "⏸ ")
        self.assertEqual(self.elapsed(payload), "0:30")
        self.assertEqual(self.progress(payload)[-1]["text"].strip(), "4:00")
        self.assertAlmostEqual(self.progress(payload)[2]["bar"]["value"], 0.125)
        self.emit({"title": "Angel", "elapsedTime": 5.0, "timestamp": stamp()}, diff=True)
        payload = self.pushed_until(plugin, lambda p: self.texts(p)[0] == "Angel")
        self.assertEqual(self.texts(payload)[:3], ["Angel", "Massive Attack", "Mezzanine"])
        self.assertEqual(self.elapsed(payload), "0:05")

    def test_elapsed_counts_forward_from_the_timestamp_at_the_playback_rate(self):
        for rate, playing, least, most in ((1, True, 40, 42), (2, True, 50, 54),
                                           (0, False, 30, 30)):
            with self.subTest(rate=rate):
                stream = os.path.join(self.fake, "stream")
                if os.path.exists(stream):
                    os.remove(stream)
                self.emit(track(elapsedTime=30.0, timestamp=stamp(10), playbackRate=rate,
                                playing=playing))
                plugin = self.start()
                self.open(plugin, visible=False)
                payload = self.refresh(plugin)["payload"]
                minutes, seconds = self.elapsed(payload).split(":")
                self.assertGreaterEqual(int(minutes) * 60 + int(seconds), least)
                self.assertLessEqual(int(minutes) * 60 + int(seconds), most)
                plugin.close()

    def test_a_timestamp_with_an_offset_is_read_in_its_zone(self):
        local = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%dT%H:%M:%S+03:00")
        self.emit(track(timestamp=local, playing=True, playbackRate=1))
        plugin = self.start()
        self.open(plugin, visible=False)
        self.assertIn(self.elapsed(self.refresh(plugin)["payload"]), ("0:30", "0:31", "0:32"))

    def test_an_empty_diff_is_no_session(self):
        self.paused()
        plugin = self.start()
        self.open(plugin)
        self.showing(plugin, lambda p: self.texts(p)[0] == "Teardrop")
        self.emit({}, diff=True)
        self.pushed_until(plugin, lambda p: self.texts(p) == ["Nothing playing"])

    def test_a_failing_media_control_test_needs_setup(self):
        with open(os.path.join(self.fake, "test-exit"), "w") as f:
            f.write("1")
        plugin = self.start()
        self.open(plugin)
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "setup")
        self.assertEqual(status["action"]["target"], self.install_target)

    def test_artwork_is_cached_once_per_image_and_shown_by_path(self):
        cover = base64.b64encode(PNG).decode()
        other = base64.b64encode(JPEG).decode()
        self.emit(track(artworkData=cover, artworkMimeType="image/png"))
        plugin = self.start()
        self.open(plugin)
        image = self.refresh(plugin)["payload"]["image"]
        self.assertEqual(image["side"], "left")
        self.assertTrue(os.path.isabs(image["path"]))
        self.assertTrue(image["path"].startswith(self.cache))
        with open(image["path"], "rb") as f:
            self.assertEqual(f.read(), PNG)
        first = os.stat(image["path"]).st_mtime_ns
        time.sleep(0.05)
        self.emit({"title": "Angel"}, diff=True)
        payload = self.pushed_until(plugin, lambda p: self.texts(p)[0] == "Angel")
        self.assertEqual(payload["image"], image)
        self.emit(track(title="Dissolved Girl", artworkData=other, artworkMimeType="image/jpeg"))
        payload = self.pushed_until(plugin, lambda p: self.texts(p)[0] == "Dissolved Girl")
        self.assertNotEqual(payload["image"]["path"], image["path"])
        with open(payload["image"]["path"], "rb") as f:
            self.assertEqual(f.read(), JPEG)
        self.emit(track(title="Teardrop", artworkData=cover, artworkMimeType="image/png"))
        payload = self.pushed_until(plugin, lambda p: self.texts(p)[0] == "Teardrop")
        self.assertEqual(payload["image"], image)
        self.assertEqual(os.stat(image["path"]).st_mtime_ns, first)
        self.assertEqual(len(os.listdir(self.cache)), 2)

    def test_wide_artwork_is_cropped_to_a_centred_square(self):
        self.install("sips")
        self.emit(track(artworkData=base64.b64encode(png(160, 90)).decode(),
                        artworkMimeType="image/png"))
        plugin = self.start()
        self.open(plugin)
        path = self.refresh(plugin)["payload"]["image"]["path"]
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"square " + png(160, 90))
        [call] = self.calls()
        self.assertEqual(call[:3], ["-c", "90", "90"])
        self.assertEqual(call[4], "--out")
        self.assertEqual(os.listdir(self.cache), [os.path.basename(path)])

    def test_two_visors_cropping_the_same_artwork_both_show_it_square(self):
        self.install("sips")
        self.emit(track(artworkData=base64.b64encode(png(160, 90)).decode(),
                        artworkMimeType="image/png"))
        plugins = [self.start(), self.start()]
        for plugin in plugins:
            self.open(plugin)
        paths = {self.refresh(plugin)["payload"]["image"]["path"] for plugin in plugins}
        self.assertEqual(len(paths), 1)
        with open(paths.pop(), "rb") as f:
            self.assertEqual(f.read(), b"square " + png(160, 90))
        self.assertEqual(len(os.listdir(self.cache)), 1)

    def test_square_artwork_is_kept_as_it_is(self):
        self.install("sips")
        self.emit(track(artworkData=base64.b64encode(png(90, 90)).decode(),
                        artworkMimeType="image/png"))
        plugin = self.start()
        self.open(plugin)
        with open(self.refresh(plugin)["payload"]["image"]["path"], "rb") as f:
            self.assertEqual(f.read(), png(90, 90))
        self.assertEqual(self.calls(), [])

    def test_artwork_goes_on_top_when_the_text_beside_it_would_be_too_narrow(self):
        self.emit(track(artworkData=base64.b64encode(png(90, 90)).decode(),
                        artworkMimeType="image/png"))
        plugin = self.start()
        for width, height, side, rows in ((60, 8, "left", 4), (60, 20, "top", 5),
                                          (40, 10, "top", 2)):
            with self.subTest(width=width, height=height):
                self.open(plugin, width=width, height=height)
                payload = self.refresh(plugin)["payload"]
                self.assertEqual(payload["image"]["side"], side)
                self.assertEqual(sum(c["rows"] for c in self.charts(payload)), rows)

    def test_the_animation_frames_do_not_resend_the_artwork(self):
        self.playing(artworkData=base64.b64encode(PNG).decode(), artworkMimeType="image/png")
        plugin = self.start()
        self.open(plugin)
        self.showing(plugin, lambda p: "image" in p)
        for update in plugin.updates_for(0.6):
            self.assertNotIn("data", update["payload"]["image"])


class PlayerctlTest(Shared, NowPlayingTest):
    platform = "linux"
    tools = ("playerctl",)
    install_hint = "playerctl"
    install_target = "https://github.com/altdesktop/playerctl#installing"
    commands = [["play-pause"], ["next"], ["previous"]]

    def seek_to(self, seconds):
        return ["position", str(seconds)]

    def paused(self, at=30, **kw):
        self.emit(mpris(position=at * 1000000, **kw))

    def playing(self, at=30, **kw):
        self.emit(mpris(position=at * 1000000, status="Playing", **kw))

    def test_metadata_and_status_show_the_track(self):
        self.paused()
        plugin = self.start()
        self.open(plugin)
        payload = self.showing(plugin, lambda p: self.texts(p)[0] == "Teardrop")
        self.assertEqual(self.texts(payload)[:3], ["Teardrop", "Massive Attack", "Mezzanine"])
        self.assertEqual(self.elapsed(payload), "0:30")
        self.assertEqual(self.progress(payload)[-1]["text"].strip(), "4:00")

    def test_a_stopped_player_is_nothing_playing(self):
        self.paused()
        plugin = self.start()
        self.open(plugin)
        self.showing(plugin, lambda p: self.texts(p)[0] == "Teardrop")
        self.paused(status="Stopped")
        self.pushed_until(plugin, lambda p: self.texts(p) == ["Nothing playing"])

    def test_elapsed_counts_forward_from_the_last_position(self):
        self.playing()
        plugin = self.start()
        self.open(plugin, visualizer=False)
        self.showing(plugin, lambda p: self.elapsed(p) == "0:30")
        self.pushed_until(plugin, lambda p: self.elapsed(p) == "0:31")

    def test_a_file_url_artwork_is_shown_from_its_path(self):
        self.paused(**{"mpris:artUrl": "file:///tmp/My%20Covers/teardrop.png"})
        plugin = self.start()
        self.open(plugin)
        payload = self.showing(plugin, lambda p: "image" in p)
        self.assertEqual(payload["image"], {"path": "/tmp/My Covers/teardrop.png", "side": "left"})

    def test_a_web_artwork_is_downloaded_into_the_cache_once(self):
        served = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                served.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(PNG)))
                self.end_headers()
                self.wfile.write(PNG)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/cover" % server.server_address[1]
        self.paused(**{"mpris:artUrl": url})
        plugin = self.start()
        self.open(plugin)
        payload = self.showing(plugin, lambda p: "image" in p)
        self.assertTrue(payload["image"]["path"].startswith(self.cache))
        with open(payload["image"]["path"], "rb") as f:
            self.assertEqual(f.read(), PNG)
        self.paused(title="Angel", **{"mpris:artUrl": url})
        again = self.pushed_until(plugin, lambda p: self.texts(p)[0] == "Angel")
        self.assertEqual(again["image"], payload["image"])
        self.assertEqual(served, ["/cover"])


class UnsupportedTest(NowPlayingTest):
    platform = "win32"
    tools = ()

    def test_another_os_is_an_error_naming_the_ones_supported(self):
        plugin = self.start()
        self.open(plugin)
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "error")
        self.assertIn("macOS and Linux", status["message"])

if __name__ == "__main__":
    unittest.main()
