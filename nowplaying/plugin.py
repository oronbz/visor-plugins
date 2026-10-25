"""visor Plugin: what the OS is playing, with play/pause, next, previous and seek.

Speaks visor's Plugin protocol (JSON-RPC 2.0, one message per line on
stdin/stdout). Standard library only, Python 3.9+.
"""

import base64
import hashlib
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from collections import namedtuple
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

PLATFORM = os.environ.get("NOWPLAYING_PLATFORM", sys.platform)
FRAME = 0.25
TICK = 1
RETRY = 10
FIRST_RECORD = 3
QUIET = 1
TIMEOUT = 10
KEEP_ARTWORK = 20
DRIFT = 1.5
MAX_ARTWORK = 10 * 1024 * 1024
CELL = 2.1
BESIDE = 24
BANDS = ("alt", "accent", "info")

MEDIA_CONTROL_INSTALL = "brew tap ungive/media-control && brew install media-control"
PLAYERCTL_INSTALL = "https://github.com/altdesktop/playerctl#installing"

Session = namedtuple("Session", "title artist album duration elapsed at rate playing artwork")
Session.__doc__ = ("A media session as the Widget shows it: elapsed seconds as of epoch time at, "
                   "counted forward at rate while playing, and the artwork's path or None.")

widgets = {}
player = None
ratios = {}
state_lock = threading.Lock()
write_lock = threading.Lock()


class RPCError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Problem(Exception):
    """A Status other than ok: the Widget reports it, an Action answers its message as an error."""

    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


def setup(message, action):
    return Problem(message, {"state": "setup", "message": message, "action": action})


def error(message, reason):
    return Problem(message, {"state": "error", "message": message, "reason": reason})


def status_of(problem):
    return problem.status if problem else None


def send(msg):
    msg["jsonrpc"] = "2.0"
    with write_lock:
        sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def cache_dir():
    if os.environ.get("NOWPLAYING_CACHE_DIR"):
        return os.environ["NOWPLAYING_CACHE_DIR"]
    base = os.environ.get("XDG_CACHE_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "visor", "plugins", "nowplaying")


def save_artwork(data, ext):
    """The cached file holding data, written once and named by its hash, since visor
    reads a path again only when its mtime or size changes."""
    folder = cache_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, hashlib.sha256(data).hexdigest()[:24] + ext)
    if not os.path.exists(path):
        partial = path + ".partial"
        with open(partial, "wb") as f:
            f.write(data)
        if not squared(partial, dimensions(data), path):
            os.replace(partial, path)
        prune(folder, path)
    return path


def squared(source, size, path):
    """Whether sips, which macOS ships, wrote source cropped to a centred square at path:
    a web player's artwork is often a 16:9 crop of a square cover."""
    sips = shutil.which("sips")
    if not sips or not size or size[0] == size[1]:
        return False
    side = str(min(size))
    done = subprocess.run([sips, "-c", side, side, source, "--out", path],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=TIMEOUT)
    if done.returncode != 0 or not os.path.exists(path):
        return False
    os.remove(source)
    return True


def dimensions(data):
    """A PNG's or JPEG's (width, height) from its header, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:2] != b"\xff\xd8":
        return None
    at = 2
    while at + 9 <= len(data):
        if data[at] != 0xFF:
            at += 1
            continue
        marker = data[at + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[at + 5:at + 9])
            return width, height
        if marker == 0xFF or marker == 0x01 or 0xD0 <= marker <= 0xD8:
            at += 1 if marker == 0xFF else 2
            continue
        at += 2 + struct.unpack(">H", data[at + 2:at + 4])[0]
    return None


def ratio(path):
    """The artwork at path's width over its height, 1 when it can't be read."""
    if path not in ratios:
        try:
            with open(path, "rb") as f:
                size = dimensions(f.read(65536))
        except OSError:
            size = None
        ratios[path] = size[0] / size[1] if size and size[1] else 1.0
    return ratios[path]


def prune(folder, keep):
    files = [os.path.join(folder, name) for name in os.listdir(folder)
             if not name.endswith(".partial")]
    files.sort(key=os.path.getmtime, reverse=True)
    for path in files[KEEP_ARTWORK:]:
        if path != keep:
            os.remove(path)


def extension(mime):
    return ".png" if "png" in (mime or "") else ".jpg"


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def epoch(stamp):
    """An ISO 8601 time, such as 2026-10-25T08:58:49Z, as epoch seconds."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|([+-])(\d\d):?(\d\d))?$",
                 stamp or "")
    if not m:
        return time.time()
    whole = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    offset = 0
    if m.group(4):
        offset = (int(m.group(5)) * 3600 + int(m.group(6)) * 60) * (1 if m.group(4) == "+" else -1)
    return whole.timestamp() + number(m.group(2)) - offset


def run(argv):
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise error("%s failed: %s" % (argv[0], e), "%s failed" % argv[0]) from None
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise error("%s %s failed%s" % (argv[0], argv[1], ": " + said[-1] if said else ""),
                    "%s failed" % argv[0])


class Backend:
    """How the Plugin reads and drives the OS's media session. sessions() runs the
    tool's follow mode and yields the Session, or None for no session, at each change."""

    tool = ""
    COMMANDS = {}

    def check(self):
        raise NotImplementedError

    def sessions(self, proc):
        raise NotImplementedError

    def follow_argv(self):
        raise NotImplementedError

    def command(self, action):
        run([self.tool, self.COMMANDS[action]])

    def seek(self, seconds):
        raise NotImplementedError


class MediaControl(Backend):
    """macOS, through media-control, which reads MediaRemote by way of /usr/bin/perl."""

    tool = "media-control"
    COMMANDS = {"toggle": "toggle-play-pause", "next": "next-track", "previous": "previous-track"}

    def __init__(self):
        self.artwork = (None, None)

    def check(self):
        missing = setup("Install media-control: " + MEDIA_CONTROL_INSTALL, {
            "id": "install", "label": "Copy install command", "verb": "copy",
            "target": MEDIA_CONTROL_INSTALL})
        if not shutil.which(self.tool):
            raise missing
        try:
            run([self.tool, "test"])
        except Problem:
            raise missing from None

    def follow_argv(self):
        return [self.tool, "stream"]

    def sessions(self, proc):
        state = {}
        for line in proc.stdout:
            record = json.loads(line)
            if record.get("type") != "data":
                continue
            payload = record.get("payload") or {}
            if not record.get("diff") or not payload:
                state = {}
            state.update(payload)
            state = {k: v for k, v in state.items() if v is not None}
            yield self.session(state)

    def session(self, state):
        if not state:
            return None
        playing = bool(state.get("playing"))
        return Session(
            title=state.get("title") or "", artist=state.get("artist") or "",
            album=state.get("album") or "", duration=number(state.get("duration")),
            elapsed=number(state.get("elapsedTime")), at=epoch(state.get("timestamp")),
            rate=number(state.get("playbackRate"), 1.0), playing=playing,
            artwork=self.artwork_path(state.get("artworkData"), state.get("artworkMimeType")))

    def artwork_path(self, data, mime):
        if not data:
            return None
        if self.artwork[0] != data:
            self.artwork = (data, save_artwork(base64.b64decode(data), extension(mime)))
        return self.artwork[1]

    def seek(self, seconds):
        run([self.tool, "seek", seconds_arg(seconds)])


class Playerctl(Backend):
    """Linux, through playerctl, which reads MPRIS and picks the active player itself."""

    tool = "playerctl"
    COMMANDS = {"toggle": "play-pause", "next": "next", "previous": "previous"}
    FIELDS = ["status", "position", "mpris:length", "title", "artist", "album", "mpris:artUrl"]
    SEP = "\x1f"

    def __init__(self):
        self.artwork = {}

    def check(self):
        if not shutil.which(self.tool):
            raise setup("Install playerctl from your package manager, such as "
                        "`sudo apt install playerctl`",
                        {"id": "install", "label": "Open playerctl's install page",
                         "verb": "open", "target": PLAYERCTL_INSTALL})

    def follow_argv(self):
        template = self.SEP.join("{{%s}}" % field for field in self.FIELDS)
        return [self.tool, "--follow", "metadata", "--format", template]

    def sessions(self, proc):
        for line in proc.stdout:
            yield self.session(line.rstrip("\n"))

    def session(self, line):
        values = dict(zip(self.FIELDS, line.split(self.SEP)))
        if values.get("status") not in ("Playing", "Paused"):
            return None
        playing = values["status"] == "Playing"
        return Session(
            title=values.get("title", ""), artist=values.get("artist", ""),
            album=values.get("album", ""), duration=number(values.get("mpris:length")) / 1e6,
            elapsed=number(values.get("position")) / 1e6, at=time.time(),
            rate=1.0, playing=playing, artwork=self.artwork_path(values.get("mpris:artUrl")))

    def artwork_path(self, url):
        if not url:
            return None
        parts = urlsplit(url)
        if parts.scheme == "file":
            return unquote(parts.path)
        if parts.scheme not in ("http", "https"):
            return None
        if url not in self.artwork:
            try:
                with urlopen(url, timeout=TIMEOUT) as resp:
                    data, mime = resp.read(MAX_ARTWORK + 1), resp.headers.get("Content-Type")
                if len(data) > MAX_ARTWORK:
                    return None
                self.artwork[url] = save_artwork(data, extension(mime or url))
            except OSError:
                return None
        return self.artwork[url]

    def seek(self, seconds):
        run([self.tool, "position", seconds_arg(seconds)])


def seconds_arg(seconds):
    return ("%.2f" % seconds).rstrip("0").rstrip(".")


def backend_for(platform):
    if platform == "darwin":
        return MediaControl()
    if platform.startswith("linux"):
        return Playerctl()
    return None


def within(session, seconds):
    if session.duration > 0:
        seconds = min(seconds, session.duration)
    return max(0.0, seconds)


def position(session, now):
    elapsed = session.elapsed
    if session.playing:
        elapsed += (now - session.at) * session.rate
    return within(session, elapsed)


def text_lines(session):
    """The rows a session's text takes: title, artist and album when it has them, progress."""
    return 2 + bool(session.artist) + bool(session.album)


def without_position(session):
    """What of a session a push shows, apart from where playback is."""
    if session is None:
        return None
    return session._replace(elapsed=0, at=0)


class Player:
    """The OS's media session, followed by one tool process shared by every Widget.
    Its fields are guarded by state_lock."""

    def __init__(self, backend):
        self.backend = backend
        self.session = None
        self.problem = None
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.proc = None
        if backend is None:
            self.problem = error("Now Playing works on macOS and Linux, not %s" % PLATFORM,
                                 "unsupported OS")
            self.ready.set()
        else:
            threading.Thread(target=self.follow, daemon=True).start()

    def follow(self):
        while not self.stopped.is_set():
            try:
                self.backend.check()
                self.stream()
                problem = error("%s stopped" % self.backend.tool, "%s stopped" % self.backend.tool)
            except Problem as e:
                problem = e
            except Exception as e:
                problem = error("the Plugin failed: %s" % type(e).__name__, "plugin failed")
            if self.stopped.is_set():
                return
            self.record(None, problem)
            self.stopped.wait(RETRY)

    def stream(self):
        proc = subprocess.Popen(self.backend.follow_argv(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace")
        with state_lock:
            self.proc = proc
        # media-control opens with an empty record before the full one, and playerctl says
        # nothing until a player exists, so no session is only taken as such after a pause.
        quiet = threading.Timer(QUIET, self.ready.set)
        quiet.daemon = True
        quiet.start()
        try:
            for session in self.backend.sessions(proc):
                self.record(session, None)
        finally:
            proc.kill()
            proc.wait()

    def record(self, session, problem):
        with state_lock:
            changed = (status_of(problem) != status_of(self.problem)
                       or without_position(session) != without_position(self.session)
                       or self.drifted(session))
            self.session, self.problem = session, problem
            if session is not None or problem is not None:
                self.ready.set()
            notified = list(widgets.values()) if changed else []
        for widget in notified:
            widget.changed()

    def drifted(self, session):
        if session is None or self.session is None:
            return False
        now = time.time()
        return abs(position(session, now) - position(self.session, now)) > DRIFT

    def command(self, action):
        self.backend.command(action)

    def seek_by(self, seconds):
        with state_lock:
            session = self.session
        if session is None:
            return
        target = within(session, position(session, time.time()) + seconds)
        self.backend.seek(target)
        with state_lock:
            if self.session is session:
                self.session = session._replace(elapsed=target, at=time.time())
            notified = list(widgets.values())
        for widget in notified:
            widget.changed()

    def stop(self):
        self.stopped.set()
        with state_lock:
            proc = self.proc
        if proc:
            proc.kill()


def clock(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
    return "%d:%02d" % (seconds // 60, seconds % 60)


def parse_duration(text):
    """A Go duration such as "10s", "1m30s" or "500ms", in seconds."""
    units = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}
    parts = re.findall(r"([0-9]+(?:\.[0-9]+)?)(ns|us|µs|ms|s|m|h)", text or "")
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(text)
    return sum(float(n) * units[u] for n, u in parts)


SEEKS = {"back": -1, "forward": 1}
COMMANDS = ("toggle", "next", "previous")


def actions(seek):
    return [
        {"id": "toggle", "key": "space", "label": "Play/pause"},
        {"id": "next", "key": "n", "label": "Next track"},
        {"id": "previous", "key": "p", "label": "Previous track"},
        {"id": "back", "key": "[", "label": "Seek back " + seek},
        {"id": "forward", "key": "]", "label": "Seek forward " + seek},
    ]


class Widget:
    """A placed Widget: its settings, size and visibility, and the ticker that animates it
    while it's visible and something plays. Its fields are guarded by state_lock."""

    def __init__(self, id, settings, size, visible):
        self.id = id
        self.seek = str(settings.get("seek", "10s"))
        self.seek_seconds, self.bad_seek = None, None
        try:
            self.seek_seconds = parse_duration(self.seek)
        except ValueError:
            self.bad_seek = error("`seek` must be a duration such as 10s, not `%s`" % self.seek,
                                  "bad seek")
        self.visualizer = settings.get("visualizer", True) is not False
        self.width = self.height = 0
        self.resize(size)
        self.visible = visible
        self.closed = threading.Event()
        self.sending = threading.Lock()
        self.ticker = None
        self.levels = []
        self.refreshed = False
        self.status = None

    def resize(self, size):
        self.width = size.get("width", self.width)
        self.height = size.get("height", self.height)

    def update(self):
        problem = player.problem or self.bad_seek
        if problem:
            return {"status": problem.status}
        return {"payload": self.payload(player.session, time.time())}

    def payload(self, session, now):
        if session is None:
            return {"lines": [{"spans": [{"text": "Nothing playing", "token": "dim"}]}],
                    "center": True, "actions": actions(self.seek)}
        lines = [{"spans": [{"text": session.title, "token": "fg", "bold": True}]}]
        for text, token in ((session.artist, "dim"), (session.album, "faint")):
            if text:
                lines.append({"spans": [{"text": text, "token": token}]})
        lines.append({"spans": self.progress(session, now)})
        if self.visualizer:
            lines += bands(self.bars(session), self.rows(session))
        out = {"lines": lines, "actions": actions(self.seek)}
        if session.artwork:
            out["image"] = {"path": session.artwork, "side": self.side(session)}
        return out

    def side(self, session):
        """left while the text beside the artwork keeps BESIDE cells, else top: visor drops
        a side image that leaves under 20, and a square one is about CELL cells wide a row."""
        beside = self.width - round(self.height * CELL * ratio(session.artwork)) - 1
        return "left" if beside >= BESIDE else "top"

    def progress(self, session, now):
        at = position(session, now)
        spans = [{"text": "▶ " if session.playing else "⏸ ", "token": "accent"},
                 {"text": clock(at), "token": "dim"}]
        if session.duration > 0:
            spans[1]["text"] += " "
            spans += [{"bar": {"value": min(1.0, at / session.duration)}, "token": "accent"},
                      {"text": " " + clock(session.duration), "token": "dim"}]
        return spans

    def rows(self, session):
        """The visualizer's rows: what the text leaves, or a third of it under top artwork."""
        spare = self.height - text_lines(session)
        if session.artwork and self.side(session) == "top":
            return spare // 3
        return spare

    def bars(self, session):
        """Each bar's height in braille dots: pseudo-random hills that jump up and fall
        back slowly while playing, and one flat dot while paused."""
        count = max(2, 2 * self.width)
        dots = 4 * max(1, self.rows(session))
        if not session.playing:
            self.levels = []
            return [1] * count
        old = self.levels[-count:] if len(self.levels) >= count else [0.0] * count
        drawn = [random.random() ** 1.6 * (1 - 0.4 * i / count) for i in range(count)]
        hills = [(drawn[max(0, i - 1)] + 2 * d + drawn[min(count - 1, i + 1)]) / 4
                 for i, d in enumerate(drawn)]
        self.levels = [max(h, level * 0.75) for h, level in zip(hills, old)]
        return [round(level * dots) for level in self.levels]

    def interval(self):
        session = player.session
        animated = self.visualizer and session is not None and self.rows(session) > 0
        return FRAME if animated else TICK

    def ticking(self):
        return (self.visible and not self.closed.is_set() and player.problem is None
                and self.bad_seek is None and player.session is not None and player.session.playing)

    def start_ticker(self):
        if self.ticking() and self.ticker is None:
            self.ticker = threading.Thread(target=self.tick, daemon=True)
            self.ticker.start()

    def tick(self):
        while True:
            with state_lock:
                interval = self.interval()
            if self.closed.wait(interval) or self.push(self.ticked) is None:
                return

    def ticked(self):
        if not self.ticking():
            self.ticker = None
            return None
        return self.update()

    def changed(self):
        def make():
            self.start_ticker()
            return self.update()
        self.push(make)

    def push(self, make):
        """Sends make()'s update, made under state_lock, unless the Widget has closed."""
        with self.sending:
            with state_lock:
                update = None if self.closed.is_set() else make()
                fresh = update is not None and self.fresh(update)
            if fresh:
                send({"method": "widget/update", "params": dict(update, widget=self.id)})
            return update

    def fresh(self, update):
        """Whether visor should get update: not before its first refresh, which answers with
        the same state, nor a status it already has. Notes the status visor then has."""
        if not self.refreshed:
            return False
        status, last = update.get("status"), self.status
        self.status = status
        return status is None or status != last


def bands(bars, rows):
    """The visualizer as up to three charts stacked top to bottom, one colour each, so a
    bar runs up through them: each chart draws the dots of every bar within its rows."""
    if rows <= 0:
        return []
    count = min(len(BANDS), rows)
    sizes = [rows // count] * count
    sizes[0] += rows - sum(sizes)
    out, below = [], 0
    for token, size in zip(BANDS, sizes):
        values = [min(max(dots - 4 * below, 0), 4 * size) / (4 * size) for dots in bars]
        out.insert(0, {"chart": {"values": values, "rows": size, "token": token}})
        below += size
    return out


def widget_of(id):
    with state_lock:
        widget = widgets.get(id)
    if widget is None:
        raise RPCError(-32602, "no Widget `%s`" % id)
    return widget


def refresh(id):
    widget = widget_of(id)
    player.ready.wait(FIRST_RECORD)
    with state_lock:
        widget.start_ticker()
        update = widget.update()
        widget.refreshed, widget.status = True, update.get("status")
        return update


def invoke(params):
    widget = widget_of(params["widget"])
    action = params.get("action")
    if action not in SEEKS and action not in COMMANDS:
        raise RPCError(-32602, "no Action `%s`" % action)
    with state_lock:
        problem = player.problem or (widget.bad_seek if action in SEEKS else None)
    if problem:
        raise problem
    if action in SEEKS:
        player.seek_by(SEEKS[action] * widget.seek_seconds)
    else:
        player.command(action)
    return {}


def open_widget(params):
    global player
    with state_lock:
        if player is None:
            player = Player(backend_for(PLATFORM))
        old = widgets.get(params["widget"])
        if old:
            old.closed.set()
        widgets[params["widget"]] = Widget(params["widget"], params.get("settings") or {},
                                           params.get("size") or {}, params.get("visible", True))


def close_widget(params):
    with state_lock:
        widget = widgets.pop(params["widget"], None)
        if widget:
            widget.closed.set()


def frame_widget(params):
    with state_lock:
        widget = widgets.get(params["widget"])
        if widget:
            widget.resize(params.get("size") or {})
            widget.visible = params.get("visible", True)
            widget.start_ticker()


METHODS = {
    "initialize": lambda params: {"protocol": 1},
    "widget/open": open_widget,
    "widget/close": close_widget,
    "widget/frame": frame_widget,
    "widget/refresh": lambda params: refresh(params["widget"]),
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
    if player:
        player.stop()


if __name__ == "__main__":
    main()
