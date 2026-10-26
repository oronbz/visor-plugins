"""visor Plugin: stock, index, fund and crypto quotes from Yahoo Finance.

Speaks visor's Plugin protocol (JSON-RPC 2.0, one message per line on
stdin/stdout). Standard library only, Python 3.9+.
"""

import json
import os
import re
import sys
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_URL = os.environ.get("STOCKS_API_URL",
                         "https://query1.finance.yahoo.com/v8/finance/chart").rstrip("/")
QUOTE_URL = "https://finance.yahoo.com/quote/"
USER_AGENT = "Mozilla/5.0 (visor stocks plugin)"
TIMEOUT = 15
BACK_OFF = 60
WORKERS = 8
WIDTH = 40
FLOOR = 0.1
STAMP = "%Y-%m-%dT%H:%M:%SZ"

INTERVALS = {
    "1d": "5m", "5d": "15m", "1mo": "60m", "3mo": "1d", "6mo": "1d",
    "1y": "1d", "2y": "1wk", "5y": "1wk", "ytd": "1d", "max": "1mo",
}
DAY = "1d"

OPEN, PRE, AFTER, CLOSED = "open", "pre-market", "after hours", "closed"
GLYPHS = {
    OPEN: ("●", "ok"),
    PRE: ("◐", "warn"),
    AFTER: ("◐", "warn"),
    CLOSED: ("○", "faint"),
}
PERIODS = {"regular": OPEN, "pre": PRE, "post": AFTER}
COLUMNS = [
    {"priority": 4},
    {"priority": 0},
    {"priority": 3, "flex": True},
    {"priority": 0, "right": True},
    {"priority": 6},
    {"priority": 2, "right": True},
    {"priority": 1, "right": True},
    {"priority": 5},
]
BAR = 8

ADD = {"id": "add", "key": "a", "label": "Add symbol…", "input": "text",
       "prompt": "Symbols as Yahoo Finance writes them, such as AAPL, ^GSPC, TEVA.TA or BTC-USD"}

Quote = namedtuple("Quote", "symbol name currency price previous base high low decimals state closes")
Quote.__doc__ = ("A symbol's quote: previous is the last session's close, base the close before "
                 "the fetched range began, closes the range's closing prices, oldest first.")

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

    def __init__(self, message, status, until=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.until = until


def setup(message, action):
    return Problem(message, {"state": "setup", "message": message, "action": action})


def error(message, reason, hint=None):
    status = {"state": "error", "message": message, "reason": reason}
    if hint:
        status["hint"] = hint
    return Problem(message, status)


def rate_limited(until):
    stamp = until.strftime(STAMP)
    return Problem("Yahoo Finance asked to wait until " + stamp,
                   {"state": "rate_limited", "until": stamp}, until)


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


def send(msg):
    msg["jsonrpc"] = "2.0"
    with write_lock:
        sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def chart(symbol, range_):
    """Yahoo's chart result for symbol over range_, or None when Yahoo has no such symbol."""
    url = "%s/%s?%s" % (API_URL, quote(symbol, safe=""),
                        urlencode({"range": range_, "interval": INTERVALS[range_]}))
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=TIMEOUT) as resp:
            body = json.load(resp)
    except HTTPError as e:
        if e.code == 404:
            return None
        if e.code == 429:
            raise rate_limited(back_off_until(e.headers.get("Retry-After"))) from None
        raise error("Yahoo Finance answered %d for %s" % (e.code, symbol),
                    "Yahoo %d" % e.code) from None
    except (URLError, OSError) as e:
        raise error("Couldn't reach Yahoo Finance: %s" % getattr(e, "reason", e),
                    "offline") from None
    except ValueError:
        raise error("Yahoo Finance's answer for %s isn't JSON" % symbol, "bad answer") from None
    results = (body.get("chart") or {}).get("result") or []
    return results[0] if results else None


def number(value):
    return float(value) if isinstance(value, (int, float)) else None


def state_of(meta, now):
    periods = meta.get("currentTradingPeriod") or {}
    for name, state in PERIODS.items():
        period = periods.get(name) or {}
        if period.get("start", 0) <= now < period.get("end", 0):
            return state
    return CLOSED


def parse(symbol, result, now):
    meta = result.get("meta") or {}
    price = number(meta.get("regularMarketPrice"))
    if price is None:
        return None
    base = number(meta.get("chartPreviousClose"))
    indicators = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = [float(c) for c in indicators.get("close") or [] if isinstance(c, (int, float))]
    hint = meta.get("priceHint")
    return Quote(
        symbol=symbol,
        name=(meta.get("shortName") or meta.get("longName") or "").strip(),
        currency=meta.get("currency") or "",
        price=price,
        previous=number(meta.get("previousClose")) or base,
        base=base,
        high=number(meta.get("regularMarketDayHigh")),
        low=number(meta.get("regularMarketDayLow")),
        decimals=min(max(hint, 0), 6) if isinstance(hint, int) else 2,
        state=state_of(meta, now),
        closes=closes,
    )


def fetch(symbol, range_):
    result = chart(symbol, range_)
    return parse(symbol, result, time.time()) if result else None


def money(value, decimals):
    return "{:,.{}f}".format(value, decimals)


def signed(value, decimals):
    return "{:+,.{}f}".format(value, decimals)


def trend(change):
    return "ok" if change > 0 else "bad" if change < 0 else "dim"


def change_of(price, since):
    """price's change since since, absolute and in percent, or None when since is unknown."""
    if not since:
        return None
    return price - since, (price - since) / since * 100


def resample(values, n):
    """values stretched or squeezed to n samples, so the chart spans the tile's width."""
    if len(values) < 2:
        return values * n
    step = (len(values) - 1) / (n - 1)
    out = []
    for i in range(n):
        at = i * step
        lo = min(int(at), len(values) - 2)
        out.append(values[lo] + (values[lo + 1] - values[lo]) * (at - lo))
    return out


def scaled(values):
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5] * len(values)
    # An area chart draws 0 as nothing, so the low stays a sliver.
    return [min(1.0, FLOOR + (1 - FLOOR) * (v - lo) / (hi - lo)) for v in values]


def item_actions(symbol, headline, charted):
    out = [{"id": "open", "label": "Open on Yahoo Finance", "primary": True, "verb": "open",
            "target": QUOTE_URL + quote(symbol, safe="")}]
    if charted and symbol != headline:
        out.append({"id": "headline", "key": "c", "label": "Chart in headline"})
    out.append({"id": "copy", "key": "y", "label": "Copy symbol", "verb": "copy",
                "target": symbol})
    out.append({"id": "remove", "key": "x", "label": "Remove symbol", "confirm": True})
    return out


def item(symbol, q, headline, charted):
    actions = item_actions(symbol, headline, charted)
    values = {"symbol": symbol, "url": QUOTE_URL + quote(symbol, safe="")}
    if q is None:
        return {
            "id": symbol,
            "fields": [[], [{"text": symbol, "token": "bright", "bold": True}],
                       [{"text": "unknown symbol", "token": "bad"}], [], [], [], [], []],
            "actions": actions,
            "values": values,
        }
    glyph, token = GLYPHS[q.state]
    day = change_of(q.price, q.previous)
    change, percent = [], []
    if day:
        change = [{"text": signed(day[0], q.decimals), "token": trend(day[0])}]
        percent = [{"text": "{:+.2f}%".format(day[1]), "token": trend(day[0])}]
    span = []
    if q.high is not None and q.low is not None and q.high > q.low:
        at = min(max((q.price - q.low) / (q.high - q.low), 0), 1)
        span = [{"token": "accent", "bar": {"value": at, "width": BAR}}]
    values.update(name=q.name, price=money(q.price, q.decimals).replace(",", ""),
                  currency=q.currency, state=q.state)
    if day:
        values["change"] = "{:.2f}".format(day[1])
    return {
        "id": symbol,
        "fields": [
            [{"text": glyph, "token": token}],
            [{"text": symbol, "token": "bright", "bold": True}],
            [{"text": q.name, "token": "dim"}],
            [{"text": money(q.price, q.decimals), "token": "fg"}],
            [{"text": q.currency, "token": "faint"}],
            change,
            percent,
            span,
        ],
        "actions": actions,
        "values": values,
    }


def headline_lines(q, range_, rows, width):
    since = q.previous if range_ == DAY else q.base
    moved = change_of(q.price, since)
    token = trend(moved[0]) if moved else "dim"
    spans = [
        {"text": q.symbol, "token": "bright", "bold": True},
        {"text": "  " + q.name, "token": "dim"},
        {"text": "  " + money(q.price, q.decimals), "token": "fg", "bold": True},
    ]
    if moved:
        spans.append({"text": "  %s  %+.2f%%" % (signed(moved[0], q.decimals), moved[1]),
                      "token": token})
    spans.append({"text": "  " + range_, "token": "faint"})
    lines = [{"spans": spans}]
    if q.closes:
        values = scaled(resample(q.closes, max(2, width * 2)))
        lines.append({"chart": {"values": values, "rows": rows, "token": token}})
    return lines


def payload(widget):
    settings = widget.settings
    rows = chart_rows(settings)
    headline = widget.headline
    out = {}
    if headline is not None and rows:
        out["lines"] = headline_lines(headline, range_of(settings), rows, widget.width)
    named = headline.symbol if headline else None
    out.update({
        "columns": COLUMNS,
        "items": [item(symbol, q, named, rows > 0) for symbol, q in widget.quotes],
        "count": len(widget.quotes),
        "actions": [ADD],
    })
    return out


def symbols_in(text):
    return list(dict.fromkeys(s.upper() for s in re.split(r"[\s,]+", str(text or "")) if s))


def headline_of(settings, symbols):
    named = str(settings.get("headline") or "").strip().upper()
    return named or (symbols[0] if symbols else "")


def range_of(settings):
    range_ = str(settings.get("range") or DAY)
    if range_ not in INTERVALS:
        raise error("`range` must be one of %s, not `%s`" % (", ".join(INTERVALS), range_),
                    "bad range")
    return range_


def chart_rows(settings):
    try:
        return max(0, int(settings.get("chart", 3)))
    except (TypeError, ValueError):
        raise error("`chart` must be a whole number, not `%s`" % settings.get("chart"),
                    "bad chart") from None


def load(settings):
    """The quotes of the symbols settings watch, and the headline's quote over its range."""
    symbols = symbols_in(settings.get("symbols"))
    if not symbols:
        raise setup("Add a symbol to watch", ADD)
    range_ = range_of(settings)
    rows = chart_rows(settings)
    named = headline_of(settings, symbols) if rows else ""
    wanted = [(symbol, DAY) for symbol in symbols]
    if named and (range_ != DAY or named not in symbols):
        wanted.append((named, range_))
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(wanted))) as pool:
        fetched = list(pool.map(lambda want: fetch(*want), wanted))
    quotes = list(zip(symbols, fetched))
    headline = fetched[-1] if len(wanted) > len(symbols) else dict(quotes).get(named)
    return quotes, headline


class Widget:
    """A placed Widget: its settings, width and latest quotes. Fields are guarded by state_lock."""

    def __init__(self, id, settings, width, visible):
        self.id = id
        self.settings = settings
        self.width = width
        self.visible = visible
        self.closed = False
        self.quotes = None
        self.headline = None
        self.limited = None

    def waiting(self):
        """The rate_limited Problem while Yahoo's back-off lasts, else None."""
        if self.limited and datetime.now(timezone.utc) >= self.limited.until:
            self.limited = None
        return self.limited


def width_of(params):
    return (params.get("size") or {}).get("width") or WIDTH


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
        settings = widget.settings
    if waiting:
        return {"status": waiting.status}
    try:
        quotes, headline = load(settings)
    except Problem as problem:
        with state_lock:
            if problem.until:
                widget.limited = problem
            elif problem.status["state"] == "setup":
                widget.quotes = None
        return {"status": problem.status}
    except Exception as e:
        return {"status": error("the Plugin failed: %s" % type(e).__name__,
                                "plugin failed").status}
    with state_lock:
        if not widget.closed:
            widget.quotes, widget.headline = quotes, headline
        return {"payload": payload(widget)}


def add(widget, text):
    settings = widget_of(widget).settings
    watched = symbols_in(settings.get("symbols"))
    new = [s for s in symbols_in(text) if s not in watched]
    if not new:
        raise RPCError(-32602, "Type a symbol not watched yet, such as AAPL")
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(new))) as pool:
        found = list(pool.map(lambda symbol: fetch(symbol, DAY), new))
    unknown = [symbol for symbol, q in zip(new, found) if q is None]
    if unknown:
        raise RPCError(-32602, "Yahoo Finance has no symbol %s" % ", ".join(unknown))
    return {"settings": {"symbols": ", ".join(watched + new)},
            "toast": "Watching " + ", ".join(new)}


def watched_symbol(params):
    settings = widget_of(params["widget"]).settings
    watched = symbols_in(settings.get("symbols"))
    symbol = params.get("item")
    if symbol not in watched:
        raise RPCError(-32602, "no symbol `%s` on this Widget" % symbol)
    return settings, watched, symbol


def remove(params):
    settings, watched, symbol = watched_symbol(params)
    changed = {"symbols": ", ".join(s for s in watched if s != symbol)}
    if str(settings.get("headline") or "").strip().upper() == symbol:
        changed["headline"] = ""
    return {"settings": changed, "toast": "Stopped watching " + symbol}


def chart_headline(params):
    _, _, symbol = watched_symbol(params)
    return {"settings": {"headline": symbol}, "toast": "Charting " + symbol}


def invoke(params):
    action = params.get("action")
    if action == ADD["id"]:
        return add(params["widget"], params.get("text") or "")
    if action == "remove":
        return remove(params)
    if action == "headline":
        return chart_headline(params)
    raise RPCError(-32602, "no Action `%s`" % action)


def open_widget(params):
    with state_lock:
        old = widgets.get(params["widget"])
        if old:
            old.closed = True
        widgets[params["widget"]] = Widget(params["widget"], params.get("settings") or {},
                                           width_of(params), params.get("visible", True))


def close_widget(params):
    with state_lock:
        widget = widgets.pop(params["widget"], None)
        if widget:
            widget.closed = True


def frame_widget(params):
    """Redraws the chart from the quotes it has when the tile's width changes."""
    update = None
    with state_lock:
        widget = widgets.get(params["widget"])
        if widget is None:
            return
        widened = width_of(params) != widget.width
        widget.width = width_of(params)
        widget.visible = params.get("visible", True)
        if widened and widget.visible and widget.quotes is not None and widget.headline:
            update = {"widget": widget.id, "payload": payload(widget)}
    if update:
        send({"method": "widget/update", "params": update})


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


if __name__ == "__main__":
    main()
