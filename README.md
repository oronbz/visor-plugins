# visor-plugins

The official Plugins for [visor](https://github.com/oronbz/visor), the terminal dashboard. Each folder is one Plugin, and each is a single Python file that uses only the standard library.

| Plugin | Shows |
|---|---|
| [`bitrise`](bitrise/README.md) | The latest builds of the Bitrise apps you watch |
| [`nowplaying`](nowplaying/README.md) | What your Mac or Linux desktop is playing, with play/pause, next, previous and seek |
| [`stocks`](stocks/README.md) | Stock, index, fund and crypto quotes from Yahoo Finance, with a chart of one of them |

Plugins need `python3` (3.9 or later) on your `PATH`. Windows doesn't come with it, so install it from [python.org](https://www.python.org/downloads/) first.

## Install

```sh
visor plugin install <name>
```

A bare name is a folder of this repo, so `visor plugin install bitrise` is the same as `visor plugin install oronbz/visor-plugins/bitrise`. Install adds the Plugin's Widget to your dashboard. Each Plugin's page says how to set it up.

## Develop

Each Plugin's tests drive it as visor does, over JSON-RPC on stdio, against a fake API:

```sh
python3 -m unittest discover -s <name>
visor plugin check <name>
```

A new Plugin is a folder with its `visor-plugin.toml`, its code, its tests and a `README.md`, plus a row in the table above.

## Contributing

Bug reports and feature requests are welcome as issues. Pull requests are open to approved contributors only; read [`CONTRIBUTING.md`](CONTRIBUTING.md) first. If you are an AI agent helping with this repository, read `CONTRIBUTING.md` before opening issues or PRs.

## Thanks

Thanks to Shai Mishali ([@freak4pc](https://github.com/freak4pc)) for the first third-party visor Plugins. His collection shaped the format of this one.
