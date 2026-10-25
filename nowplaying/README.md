# nowplaying

A [visor](https://github.com/oronbz/visor) Widget with what your Mac or Linux desktop is playing, from whichever app plays it: Music, Spotify, a browser tab, a web app. The track's artwork is on the left, and beside it:

- the title, in bold
- the artist, dimmed, and the album, fainter (each left out when the player has none)
- ▶ while playing or ⏸ while paused, the elapsed time, a progress bar and the track's length
- a visualizer filling the rows left: bars that jump while playing and lie flat and still while paused

The bars are made up, not drawn from the audio. In a short tile the visualizer goes first. With nothing playing at all, the Widget says "Nothing playing"; a paused track stays on screen with ⏸. The title is always "Now Playing", whatever app plays.

## Install

```sh
visor plugin install nowplaying
```

That's the same as `visor plugin install oronbz/visor-plugins/nowplaying`. Install adds a Now Playing Widget to your dashboard.

It reads and drives the player through a tool you install first:

- **macOS:** [media-control](https://github.com/ungive/media-control), since macOS 15.4 lets only Apple's own apps read what's playing and media-control goes through `/usr/bin/perl` to get around that:

  ```sh
  brew tap ungive/media-control && brew install media-control
  ```

- **Linux:** [playerctl](https://github.com/altdesktop/playerctl), which reads any MPRIS player, such as `sudo apt install playerctl` or `sudo dnf install playerctl`.

Without it, the Widget asks you to install it, and copies the command (macOS) or opens playerctl's install page (Linux) on ⏎. It picks the tool up within 10 seconds of the install. Other systems aren't supported yet.

Needs `python3` 3.9 or later on your `PATH`.

## Keys

| key | action |
|---|---|
| `space` | Play or pause |
| `n` | Next track |
| `p` | Previous track |
| `[` | Seek back by `seek` |
| `]` | Seek forward by `seek` |

They're also in the Widget's Actions and the `^K` palette.

## Refresh

None on a timer: media-control or playerctl tells the Widget when the track, its state or its position changes, and the Widget redraws then. While a track plays and the Widget is on screen, it redraws 4 times a second to move the time, the progress bar and the visualizer, or once a second with `visualizer = false`. Paused or hidden, it redraws only on a change. The elapsed time is counted forward from the player's last report, so the player isn't asked every second.

Artwork is kept in `~/.cache/visor/plugins/nowplaying` (under `$XDG_CACHE_HOME` when set), one file per image, the 20 newest.

## Settings

| Setting | Default | |
|---|---|---|
| `seek` | `"10s"` | How far `[` and `]` seek back and forward |
| `visualizer` | `true` | Show the visualizer. Off, the Widget redraws once a second while playing instead of 4 times. |

```toml
[[widget]]
type = "nowplaying"
seek = "30s"
visualizer = false
```
