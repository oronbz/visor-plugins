# visor-plugins

The official Plugins for [visor](https://github.com/oronbz/visor), the terminal dashboard. One folder per Plugin.

| Plugin | Shows |
|---|---|
| [`bitrise`](bitrise) | The latest builds of the Bitrise apps you watch |

Install one with `visor plugin install oronbz/visor-plugins/<name>`. The Plugins here are single-file Python, standard library only, so they need `python3` (3.9 or later) on your `PATH`; Windows doesn't come with it.

## bitrise

Running builds first, then builds on hold, then the latest finished ones, across every app you watch, each with its status, app, workflow, branch or PR, and how long it's been running or how long ago it finished.

While a build is running or on hold and the Widget is on screen, it checks Bitrise every 15 seconds and a running build's timer ticks every second; otherwise it refreshes every 2 minutes. When Bitrise rate-limits it, it waits as long as Bitrise asks, or a minute.

| Setting | Default | |
|---|---|---|
| `apps` | `""` | Bitrise app slugs to watch, comma-separated |
| `limit` | `10` | The most builds listed |
| `token` | | Your Bitrise [personal access token](https://app.bitrise.io/me/account/security), set through the Widget's "Set token" Action and kept in your OS keyring. `BITRISE_TOKEN` in the environment takes its place. |

## Developing

Each Plugin's tests drive it as visor does, over JSON-RPC on stdio, against a fake API:

```sh
python3 -m unittest discover -s bitrise
visor plugin check bitrise
```
