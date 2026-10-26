# stocks

A [visor](https://github.com/oronbz/visor) Widget with the stocks, indices, funds and crypto pairs you watch, from Yahoo Finance, under a chart of one of them. Each Item has:

- a trading glyph: ● the market is open (ok), ◐ pre-market or after hours (warn), ○ closed (faint)
- the symbol, in bold
- the name, dimmed
- the price, and its currency, fainter
- the day's change, absolute and in percent: green up, red down
- a bar of where the price sits between the day's low and high

The name takes the spare width. In a narrow tile the currency goes first, then the bar, the glyph, the name and the absolute change.

Above the list, the headline shows one symbol's price and change with a chart of `range` under it, green when it rose over the range and red when it fell. It's the first symbol unless `headline` names another, which needn't be in the list. Past `1d`, the headline's change is over the whole range, while the list keeps the day's.

## Install

```sh
visor plugin install stocks
```

That's the same as `visor plugin install oronbz/visor-plugins/stocks`. Install adds a Stocks Widget to your dashboard, watching the S&P 500, the Nasdaq Composite, Apple, Microsoft and Nvidia. Press `a` to add a symbol.

No account or key is needed. Needs `python3` 3.9 or later on your `PATH`.

## Symbols

Write them the way [Yahoo Finance](https://finance.yahoo.com) does: `AAPL`, `^GSPC` for an index, a suffix for an exchange outside the US (`TEVA.TA` in Tel Aviv, `VOD.L` in London), `BTC-USD` for a crypto pair, `EURUSD=X` for a currency. Case doesn't matter. A symbol Yahoo doesn't know stays in the list as "unknown symbol", so you can remove it.

Prices are in the exchange's currency, which Yahoo sometimes gives in its minor unit: Tel Aviv prices are in agorot (`ILA`) and London ones in pence (`GBp`).

## Keys

| key | action |
|---|---|
| ⏎ | Open the symbol on Yahoo Finance |
| `c` | Chart the symbol in the headline |
| `y` | Copy the symbol |
| `x` | Stop watching the symbol (asks first) |
| `a` | Add symbols: type one or more, separated by commas or spaces |

They're also in the Widget's Actions and the `^K` palette. Add, chart and remove write `symbols` and `headline` into the Widget's block in `config.toml`.

Each Item carries `symbol`, `name`, `price`, `change` (the day's, in percent), `currency`, `state` and `url` for Actions of your own:

```toml
[[widget]]
type = "stocks"
actions = [{ label = "Open on TradingView", key = "t", run = "open https://www.tradingview.com/symbols/{{symbol}}/" }]
```

## Refresh

Every minute, 15 seconds at the shortest. Each refresh makes one request per symbol, all at once, plus one for the headline when `range` isn't `1d` or the headline isn't in the list. When Yahoo rate-limits it, it waits as long as Yahoo asks, or a minute.

Yahoo Finance's chart API is free and unofficial: quotes can lag the market by a few minutes, and Yahoo can change or close it without notice.

## Settings

| Setting | Default | |
|---|---|---|
| `symbols` | `"^GSPC, ^IXIC, AAPL, MSFT, NVDA"` | The symbols to watch, comma-separated |
| `headline` | `""` | The symbol charted above the list; empty charts the first one |
| `range` | `"1d"` | How far back the chart goes: `1d`, `5d`, `1mo`, `3mo`, `6mo`, `1y`, `2y`, `5y`, `ytd` or `max` |
| `chart` | `3` | Rows of the headline chart; `0` leaves the headline out |

```toml
[[widget]]
type = "stocks"
symbols = "^TA125.TA, TEVA.TA, NICE.TA, AAPL"
headline = "^TA125.TA"
range = "5d"
```
