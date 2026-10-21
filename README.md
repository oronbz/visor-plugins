# visor-plugins

The official Plugins for [visor](https://github.com/oronbz/visor), the terminal dashboard. Each folder is one Plugin, and each is a single Python file that uses only the standard library.

| Plugin | Shows |
|---|---|
| [`bitrise`](#bitrise) | The latest builds of the Bitrise apps you watch |

Plugins need `python3` (3.9 or later) on your `PATH`. Windows doesn't come with it, so install it from [python.org](https://www.python.org/downloads/) first.

## bitrise

The builds of every Bitrise app you watch, in one list: running builds first, then builds on hold, then the latest finished ones, newest first in each group. Each row has:

- a status glyph: ● running (accent), ◌ on hold (warn), ✓ succeeded (ok), ✗ failed (bad), ⊘ aborted (faint)
- the app, dimmed
- the workflow
- the branch, or `#<number>` for a pull request build
- for a running or on-hold build, how long it's been going; for a finished one, how long ago it ended

The Widget's count is the number of running builds.

| key | action |
|---|---|
| ⏎ | Open the build on Bitrise |
| `p` | Open the pull request (pull request builds only) |
| `y` | Copy the build URL |
| `x` | Abort the build (running or on hold only; asks first) |
| `R` | Rebuild with the original build's parameters, so the same commit runs again, not the branch's latest (asks first) |

Watch app… and Set token are in the Widget's Actions and the `^K` palette:

- **Watch app…** lists the apps your token can see that you don't watch yet, and adds the one you pick to `apps`.
- **Set token** asks for your token in a masked prompt and opens the Bitrise page where you create one.

**Token.** Create a personal access token at [app.bitrise.io/me/account/security](https://app.bitrise.io/me/account/security), then run Set token. visor keeps it in your OS keyring (Keychain, Secret Service or Credential Manager), never in `config.toml`, and every `bitrise` Widget shares it. If `BITRISE_TOKEN` is set in visor's environment, it's used instead; that's the way in on a machine with no keyring. With no token, the Widget asks you to set one. If Bitrise rejects it, the Widget asks for a new one.

**Refresh.** Every 2 minutes. While a build is running or on hold and the Widget is on screen, it checks Bitrise every 15 seconds, and a running build's timer ticks every second without asking Bitrise. Each check makes two requests per watched app. When Bitrise rate-limits it, it waits as long as Bitrise asks, or a minute.

| Setting | Default | |
|---|---|---|
| `apps` | `""` | The apps to watch, by slug or title (any case), comma-separated. Watch app… adds one. |
| `limit` | `10` | The most builds listed |
| `token` | | Your Bitrise personal access token, a secret set through Set token |

```toml
[[widget]]
type = "bitrise"
apps = "Shop iOS, Shop Android"
limit = 15
```

## Install

```sh
visor plugin install bitrise
```

A bare name is a folder of this repo, so that's the same as `visor plugin install oronbz/visor-plugins/bitrise`. Then add a `[[widget]]` block with `type = "bitrise"`.

## Develop

Each Plugin's tests drive it as visor does, over JSON-RPC on stdio, against a fake API:

```sh
python3 -m unittest discover -s bitrise
visor plugin check bitrise
```

## Thanks

Thanks to Shai Mishali ([@freak4pc](https://github.com/freak4pc)) for the first third-party visor Plugins. His collection shaped the format of this one.
