import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
WAIT = 10
HOUR = 3600


def chart(symbol, price, previous, closes=(1, 2, 3), name=None, currency="USD", high=None,
          low=None, hint=2, base=None, period="regular", with_previous=True):
    now = int(time.time())
    periods = {p: {"start": now + 10 * HOUR, "end": now + 11 * HOUR}
               for p in ("pre", "regular", "post")}
    if period:
        periods[period] = {"start": now - HOUR, "end": now + HOUR}
    meta = {
        "symbol": symbol,
        "currency": currency,
        "shortName": name or symbol + " Inc.",
        "regularMarketPrice": price,
        "chartPreviousClose": previous if base is None else base,
        "regularMarketDayHigh": high,
        "regularMarketDayLow": low,
        "priceHint": hint,
        "currentTradingPeriod": periods,
    }
    if with_previous:
        meta["previousClose"] = previous
    return {"meta": meta, "timestamp": list(range(len(closes))),
            "indicators": {"quote": [{"close": list(closes)}]}}


class FakeYahoo:
    """A stdlib http.server standing in for query1.finance.yahoo.com/v8/finance/chart."""

    def __init__(self):
        self.charts = {}
        self.fail = {}
        self.retry_after = None
        self.gate = None
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                symbol = unquote(url.path.rsplit("/", 1)[-1])
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                fake.requests.append({"symbol": symbol, "query": query,
                                      "agent": self.headers.get("User-Agent")})
                self.reply(*fake.answer(symbol, query))

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
        self.url = "http://127.0.0.1:%d/v8/finance/chart" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def quote(self, symbol, range_="1d", **kw):
        self.charts[(symbol, range_)] = chart(symbol, **kw)

    def answer(self, symbol, query):
        if symbol in self.fail:
            return self.fail[symbol], {"chart": {"result": None, "error": {"code": "nope"}}}
        if self.gate:
            try:
                self.gate.wait()
            except threading.BrokenBarrierError:
                return 500, {"chart": {"result": None}}
        found = self.charts.get((symbol, query.get("range")))
        if found is None:
            return 404, {"chart": {"result": None, "error": {
                "code": "Not Found", "description": "No data found, symbol may be delisted"}}}
        return 200, {"chart": {"result": [found], "error": None}}

    def asked(self):
        return sorted((r["symbol"], r["query"]["range"]) for r in self.requests)


class Plugin:
    """plugin.py as visor runs it: a subprocess speaking JSON-RPC over stdio."""

    def __init__(self, env):
        full = dict(os.environ)
        full.update(env)
        self.proc = subprocess.Popen(
            [sys.executable, "plugin.py"], cwd=HERE, env=full,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self.messages = queue.Queue()
        self.updates = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            msg = json.loads(line)
            if msg.get("method") == "widget/update":
                self.updates.put(msg["params"])
            else:
                self.messages.put(msg)

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

    def update_within(self, seconds):
        try:
            return self.updates.get(timeout=seconds)
        except queue.Empty:
            return None

    def close(self):
        self.proc.stdin.close()
        code = self.proc.wait(timeout=WAIT)
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code


class StocksTest(unittest.TestCase):
    def setUp(self):
        self.yahoo = FakeYahoo()
        self.addCleanup(self.yahoo.close)
        self.env = {"STOCKS_API_URL": self.yahoo.url}

    def start(self):
        plugin = Plugin(self.env)
        self.addCleanup(self._stop, plugin)
        result = plugin.request("initialize", {"protocol": 1, "visor": "test"})["result"]
        self.assertEqual(result, {"protocol": 1})
        return plugin

    def _stop(self, plugin):
        if plugin.proc.poll() is None:
            self.assertEqual(plugin.close(), 0)

    def open(self, plugin, widget="s1", width=40, **settings):
        full = {"symbols": "", "headline": "", "range": "1d", "chart": 3}
        full.update(settings)
        plugin.notify("widget/open", {
            "widget": widget, "type": "stocks", "settings": full,
            "size": {"width": width, "height": 12}, "focused": False, "visible": True})

    def frame(self, plugin, width, widget="s1"):
        plugin.notify("widget/frame", {
            "widget": widget, "size": {"width": width, "height": 12}, "focused": False,
            "visible": True})

    def refresh(self, plugin, widget="s1"):
        return plugin.request("widget/refresh", {"widget": widget})["result"]

    def payload(self, result):
        self.assertNotIn("status", result)
        return result["payload"]

    def texts(self, item):
        return ["".join(s.get("text", "") for s in field) for field in item["fields"]]

    def text(self, line):
        return "".join(s["text"] for s in line["spans"])

    def actions(self, item):
        return {a["id"]: a for a in item.get("actions", [])}

    def invoke(self, plugin, action, item=None, text=None, widget="s1"):
        params = {"widget": widget, "action": action}
        if item is not None:
            params["item"] = item
        if text is not None:
            params["text"] = text
        return plugin.request("action/invoke", params)

    def test_no_symbols_asks_to_add_one(self):
        plugin = self.start()
        self.open(plugin, symbols=" , ")
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "setup")
        self.assertEqual(status["action"]["id"], "add")
        self.assertEqual(status["action"]["input"], "text")
        self.assertEqual(self.yahoo.requests, [])

    def test_a_symbol_shows_its_state_name_price_currency_and_day_change(self):
        self.yahoo.quote("AAPL", price=1234.5, previous=1200, name="Apple Inc.",
                         high=1240, low=1200)
        plugin = self.start()
        self.open(plugin, symbols="AAPL", chart=0)
        [item] = self.payload(self.refresh(plugin))["items"]
        self.assertEqual(item["id"], "AAPL")
        self.assertEqual(self.texts(item)[:7],
                         ["●", "AAPL", "Apple Inc.", "1,234.50", "USD", "+34.50", "+2.88%"])
        self.assertEqual([f[0]["token"] for f in item["fields"][:7]],
                         ["ok", "bright", "dim", "fg", "faint", "ok", "ok"])
        bar = item["fields"][7][0]["bar"]
        self.assertAlmostEqual(bar["value"], 34.5 / 40)
        self.assertEqual(item["values"], {
            "symbol": "AAPL", "url": "https://finance.yahoo.com/quote/AAPL",
            "name": "Apple Inc.", "price": "1234.50", "currency": "USD", "state": "open",
            "change": "2.88"})
        self.assertEqual(self.yahoo.requests[0]["query"], {"range": "1d", "interval": "5m"})
        self.assertIn("Mozilla", self.yahoo.requests[0]["agent"])

    def test_a_fall_is_bad_and_no_change_is_dim(self):
        self.yahoo.quote("DOWN", price=90, previous=100)
        self.yahoo.quote("FLAT", price=100, previous=100)
        plugin = self.start()
        self.open(plugin, symbols="DOWN, FLAT", chart=0)
        down, flat = self.payload(self.refresh(plugin))["items"]
        self.assertEqual(self.texts(down)[5:7], ["-10.00", "-10.00%"])
        self.assertEqual(down["fields"][6][0]["token"], "bad")
        self.assertEqual(flat["fields"][6][0]["token"], "dim")

    def test_each_trading_period_has_its_glyph(self):
        for period, glyph, token in (("regular", "●", "ok"), ("pre", "◐", "warn"),
                                     ("post", "◐", "warn"), (None, "○", "faint")):
            with self.subTest(period=period):
                self.yahoo.quote("AAPL", price=1, previous=1, period=period)
                plugin = self.start()
                self.open(plugin, symbols="AAPL", chart=0)
                [item] = self.payload(self.refresh(plugin))["items"]
                self.assertEqual(item["fields"][0], [{"text": glyph, "token": token}])

    def test_price_hint_sets_the_decimals(self):
        self.yahoo.quote("PENNY", price=0.123456, previous=0.1, hint=4)
        plugin = self.start()
        self.open(plugin, symbols="PENNY", chart=0)
        [item] = self.payload(self.refresh(plugin))["items"]
        self.assertEqual(self.texts(item)[3], "0.1235")

    def test_symbols_are_split_upper_cased_and_kept_once_in_order(self):
        for s in ("^GSPC", "TEVA.TA", "BTC-USD"):
            self.yahoo.quote(s, price=1, previous=1)
        plugin = self.start()
        self.open(plugin, symbols="^gspc teva.ta,BTC-USD, ^GSPC", chart=0)
        result = self.payload(self.refresh(plugin))
        items = result["items"]
        self.assertEqual([i["id"] for i in items], ["^GSPC", "TEVA.TA", "BTC-USD"])
        self.assertEqual(result["count"], 3)
        self.assertEqual(self.actions(items[0])["open"]["target"],
                         "https://finance.yahoo.com/quote/%5EGSPC")

    def test_a_symbol_yahoo_does_not_know_is_listed_as_unknown(self):
        self.yahoo.quote("AAPL", price=1, previous=1)
        plugin = self.start()
        self.open(plugin, symbols="AAPL, NOPE", chart=0)
        items = self.payload(self.refresh(plugin))["items"]
        self.assertEqual(self.texts(items[1])[1:3], ["NOPE", "unknown symbol"])
        self.assertIn("remove", self.actions(items[1]))

    def test_the_headline_charts_the_first_symbol_over_the_day(self):
        self.yahoo.quote("AAPL", price=110, previous=100, closes=[100, 105, 110], name="Apple")
        self.yahoo.quote("MSFT", price=1, previous=1)
        plugin = self.start()
        self.open(plugin, symbols="AAPL, MSFT", width=30)
        title, graph = self.payload(self.refresh(plugin))["lines"]
        self.assertEqual(self.text(title), "AAPL  Apple  110.00  +10.00  +10.00%  1d")
        self.assertEqual(graph["chart"]["rows"], 3)
        self.assertEqual(graph["chart"]["token"], "ok")
        values = graph["chart"]["values"]
        self.assertEqual(len(values), 60)
        self.assertAlmostEqual(values[0], 0.1)
        self.assertEqual(values[-1], 1)
        self.assertTrue(all(0 <= v <= 1 for v in values))
        self.assertEqual(self.yahoo.asked(), [("AAPL", "1d"), ("MSFT", "1d")])

    def test_a_longer_range_charts_the_headline_again_and_changes_over_it(self):
        self.yahoo.quote("AAPL", price=110, previous=100)
        self.yahoo.quote("AAPL", "1y", price=110, previous=100, base=220,
                         closes=[220, 160, 110], with_previous=False)
        plugin = self.start()
        self.open(plugin, symbols="AAPL", range="1y")
        result = self.payload(self.refresh(plugin))
        title, graph = result["lines"]
        self.assertIn("-110.00  -50.00%  1y", self.text(title))
        self.assertEqual(graph["chart"]["token"], "bad")
        self.assertEqual(self.texts(result["items"][0])[6], "+10.00%")
        self.assertEqual(self.yahoo.asked(), [("AAPL", "1d"), ("AAPL", "1y")])
        query = [r["query"] for r in self.yahoo.requests if r["query"]["range"] == "1y"]
        self.assertEqual(query, [{"range": "1y", "interval": "1d"}])

    def test_a_named_headline_outside_the_list_is_fetched_too(self):
        self.yahoo.quote("AAPL", price=1, previous=1)
        self.yahoo.quote("^GSPC", price=5000, previous=4900, name="S&P 500")
        plugin = self.start()
        self.open(plugin, symbols="AAPL", headline="^gspc")
        result = self.payload(self.refresh(plugin))
        self.assertTrue(self.text(result["lines"][0]).startswith("^GSPC  S&P 500"))
        self.assertEqual([i["id"] for i in result["items"]], ["AAPL"])

    def test_no_chart_rows_leaves_the_headline_out_and_fetches_once_per_symbol(self):
        self.yahoo.quote("AAPL", price=1, previous=1)
        plugin = self.start()
        self.open(plugin, symbols="AAPL", range="5y", chart=0)
        result = self.payload(self.refresh(plugin))
        self.assertNotIn("lines", result)
        self.assertNotIn("headline", self.actions(result["items"][0]))
        self.assertEqual(self.yahoo.asked(), [("AAPL", "1d")])

    def test_an_unknown_headline_leaves_the_headline_out(self):
        self.yahoo.quote("AAPL", price=1, previous=1)
        plugin = self.start()
        self.open(plugin, symbols="AAPL", headline="NOPE")
        self.assertNotIn("lines", self.payload(self.refresh(plugin)))

    def test_a_wider_tile_redraws_the_chart_without_asking_yahoo(self):
        self.yahoo.quote("AAPL", price=1, previous=1, closes=[1, 2])
        plugin = self.start()
        self.open(plugin, symbols="AAPL", width=20)
        self.refresh(plugin)
        asked = len(self.yahoo.requests)
        self.frame(plugin, 20)
        self.assertIsNone(plugin.update_within(0.5))
        self.frame(plugin, 50)
        pushed = plugin.update_within(WAIT)
        self.assertEqual(pushed["widget"], "s1")
        self.assertEqual(len(pushed["payload"]["lines"][1]["chart"]["values"]), 100)
        self.assertEqual(len(self.yahoo.requests), asked)

    def test_the_symbols_are_fetched_at_once(self):
        for s in ("A", "B", "C"):
            self.yahoo.quote(s, price=1, previous=1)
        self.yahoo.gate = threading.Barrier(3, timeout=3)
        plugin = self.start()
        self.open(plugin, symbols="A, B, C", chart=0)
        self.assertEqual(len(self.payload(self.refresh(plugin))["items"]), 3)

    def test_a_yahoo_failure_is_an_error_without_a_payload(self):
        self.yahoo.quote("AAPL", price=1, previous=1)
        self.yahoo.fail = {"AAPL": 500}
        plugin = self.start()
        self.open(plugin, symbols="AAPL")
        result = self.refresh(plugin)
        self.assertNotIn("payload", result)
        self.assertEqual(result["status"]["state"], "error")
        self.assertIn("500", result["status"]["message"])

    def test_an_unreachable_yahoo_is_an_error(self):
        self.env["STOCKS_API_URL"] = "http://127.0.0.1:9/v8/finance/chart"
        plugin = self.start()
        self.open(plugin, symbols="AAPL")
        result = self.refresh(plugin)
        self.assertNotIn("payload", result)
        self.assertEqual(result["status"]["reason"], "offline")

    def test_a_rate_limit_waits_as_long_as_yahoo_asks_and_asks_nothing_meanwhile(self):
        self.yahoo.quote("AAPL", price=1, previous=1)
        self.yahoo.fail = {"AAPL": 429}
        for retry_after, wait in (("120", 120), (None, 60)):
            with self.subTest(retry_after=retry_after):
                self.yahoo.retry_after = retry_after
                plugin = self.start()
                self.open(plugin, symbols="AAPL")
                status = self.refresh(plugin)["status"]
                self.assertEqual(status["state"], "rate_limited")
                until = datetime.strptime(status["until"], "%Y-%m-%dT%H:%M:%SZ")
                left = until.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
                self.assertAlmostEqual(left.total_seconds(), wait, delta=3)
                asked = len(self.yahoo.requests)
                self.assertEqual(self.refresh(plugin)["status"], status)
                self.assertEqual(len(self.yahoo.requests), asked)

    def test_a_bad_chart_setting_is_an_error_naming_it(self):
        plugin = self.start()
        self.open(plugin, symbols="AAPL", chart="lots")
        status = self.refresh(plugin)["status"]
        self.assertEqual(status["state"], "error")
        self.assertIn("lots", status["message"])

    def test_each_symbol_opens_charts_copies_and_removes(self):
        self.yahoo.quote("AAPL", price=1, previous=1)
        self.yahoo.quote("MSFT", price=1, previous=1)
        plugin = self.start()
        self.open(plugin, symbols="AAPL, MSFT")
        first, second = self.payload(self.refresh(plugin))["items"]
        self.assertEqual(sorted(self.actions(first)), ["copy", "open", "remove"])
        actions = self.actions(second)
        self.assertEqual(sorted(actions), ["copy", "headline", "open", "remove"])
        self.assertEqual(actions["headline"], {
            "id": "headline", "key": "c", "label": "Chart in headline"})
        self.assertEqual(actions["open"], {
            "id": "open", "label": "Open on Yahoo Finance", "primary": True, "verb": "open",
            "target": "https://finance.yahoo.com/quote/MSFT"})
        self.assertEqual(actions["copy"], {
            "id": "copy", "key": "y", "label": "Copy symbol", "verb": "copy", "target": "MSFT"})
        self.assertEqual(actions["remove"], {
            "id": "remove", "key": "x", "label": "Remove symbol", "confirm": True})

    def test_add_symbol_checks_each_new_symbol_and_appends_it(self):
        self.yahoo.quote("NVDA", price=1, previous=1)
        self.yahoo.quote("TSLA", price=1, previous=1)
        plugin = self.start()
        self.open(plugin, symbols="AAPL")
        result = self.invoke(plugin, "add", text="nvda, aapl tsla")["result"]
        self.assertEqual(result, {"settings": {"symbols": "AAPL, NVDA, TSLA"},
                                  "toast": "Watching NVDA, TSLA"})
        self.assertEqual(self.yahoo.asked(), [("NVDA", "1d"), ("TSLA", "1d")])

    def test_add_symbol_refuses_one_yahoo_does_not_know(self):
        plugin = self.start()
        self.open(plugin, symbols="")
        answer = self.invoke(plugin, "add", text="NOPE")
        self.assertNotIn("result", answer)
        self.assertIn("NOPE", answer["error"]["message"])

    def test_add_symbol_refuses_nothing_new(self):
        plugin = self.start()
        self.open(plugin, symbols="AAPL")
        answer = self.invoke(plugin, "add", text=" aapl ")
        self.assertNotIn("result", answer)
        self.assertEqual(self.yahoo.requests, [])

    def test_chart_in_headline_sets_the_headline(self):
        plugin = self.start()
        self.open(plugin, symbols="AAPL, MSFT")
        result = self.invoke(plugin, "headline", item="MSFT")["result"]
        self.assertEqual(result, {"settings": {"headline": "MSFT"}, "toast": "Charting MSFT"})

    def test_remove_drops_the_symbol_and_a_headline_naming_it(self):
        plugin = self.start()
        self.open(plugin, symbols="AAPL, MSFT", headline="msft")
        result = self.invoke(plugin, "remove", item="MSFT")["result"]
        self.assertEqual(result, {"settings": {"symbols": "AAPL", "headline": ""},
                                  "toast": "Stopped watching MSFT"})
        result = self.invoke(plugin, "remove", item="AAPL")["result"]
        self.assertEqual(result["settings"], {"symbols": "MSFT"})

    def test_an_action_on_a_symbol_not_watched_is_a_jsonrpc_error(self):
        plugin = self.start()
        self.open(plugin, symbols="AAPL")
        answer = self.invoke(plugin, "remove", item="GONE")
        self.assertIn("GONE", answer["error"]["message"])

    def test_an_unknown_action_is_a_jsonrpc_error(self):
        plugin = self.start()
        self.open(plugin, symbols="AAPL")
        answer = self.invoke(plugin, "made-up")
        self.assertNotIn("result", answer)
        self.assertIn("made-up", answer["error"]["message"])


if __name__ == "__main__":
    unittest.main()
