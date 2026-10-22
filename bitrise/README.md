# bitrise

A [visor](https://github.com/oronbz/visor) Widget with the builds of every Bitrise app you watch, in one list: running builds first, then builds on hold, then the latest finished ones, newest first in each group. Each Item has:

- a status glyph: ● running (accent), ◌ on hold (warn), ✓ succeeded (ok), ✗ failed (bad), ○ aborted (faint)
- the app, dimmed
- the title: the first line of the commit message, which on a pull request build is the pull request's title, or the branch when there's no message
- the workflow, dimmed
- for a running or on-hold build, how long it's been going; for a finished one, how long ago it ended

The title takes the spare width. In a narrow tile the workflow goes first, then the time, then the app.

The Widget's count is the number of running builds.

## Install

```sh
visor plugin install bitrise
```

That's the same as `visor plugin install oronbz/visor-plugins/bitrise`. Install adds a Bitrise Widget to your dashboard. Focus it and press Enter to set your token, then Enter again to pick the apps to watch.

Needs `python3` 3.9 or later on your `PATH`.

## Keys

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

## Token

Create a personal access token at [app.bitrise.io/me/account/security](https://app.bitrise.io/me/account/security), then run Set token. visor keeps it in your OS keyring (Keychain, Secret Service or Credential Manager), never in `config.toml`, and every `bitrise` Widget shares it. If `BITRISE_TOKEN` is set in visor's environment, it's used instead; that's the way in on a machine with no keyring. With no token, the Widget asks you to set one. If Bitrise rejects it, the Widget asks for a new one.

## Refresh

Every 2 minutes. While a build is running or on hold and the Widget is on screen, it checks Bitrise every 15 seconds, and a running build's timer ticks every second without asking Bitrise. Each check makes one request per watched app. When Bitrise rate-limits it, it waits as long as Bitrise asks, or a minute.

## Settings

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
