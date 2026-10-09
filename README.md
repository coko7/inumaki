<div align="center">

# 🗣️  Inumaki (狗巻)

Inumaki allows you to control your Linux desktop with your voice.

</div>

> [!NOTE]
> I was chopping onions and I wanted to use my computer at the same time.
>
> `Onion 🧅 + Computer 🖥️ = Inumaki 🗣️`

## How does it work?

- Inumaki listens to a microphone continuously
- Transcribes what you say locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- Performs actions based on commands defined in `config.toml`

### Supported actions

For now, these are the supported actions:

- move/click the mouse
- type text, press keys
- launch apps, run shell commands
- Hyprland only: switch workspaces

>[!NOTE]
> Everything runs offline on your machine.
> You can even turn off your Internet access if you are not convinced.

```
[12:48:09] heard: "workspace one" (1.4s audio, transcribed in 0.6s, 0 queued)
[12:48:09]   -> "workspace one"
```

## Requirements

- Linux with PipeWire or PulseAudio (`parec` must be available)
- Python 3.11+
- **Optional:** `gtk-launch` (for "open X"), `hyprctl` (for the Hyprland
  workspace commands)

### For Wayland users only

If you want to control your mouse/keyboard, you will need to have
[`ydotool`](https://github.com/ReimuNotMoe/ydotool) with its daemon running:

```bash
# starts the daemon
systemctl --user enable --now ydotool
```

You will also need write access to `/dev/uinput` (usually via adding your current user to the `input` group).

### For X11 users only

If you are using X11, you just need to install `xdotool`.

## Quick start

1. To start using **Inumaki**, clone this repo and open it locally:

    ```bash
    git clone https://github.com/coko7/inumaki && cd inumaki
    ```

2. Then, you will need to pick the microphone you want to use.
You can list all your audio input devices with this:

    ```bash
    ./run.sh --list-devices
    ```

    Take note of the mic your want to use, you will need it after.

3. Start `run.sh` with no argument:

    ```bash
    ./run.sh  # first run creates .venv and config.toml
    ```

    The first run will download a Whisper model (~500 MB for `small.en`).
    Stay quiet for the first second while it measures background noise.

4. Set your mic in `config.toml` (`device = "..."`) or pass it each time:

    ```bash
    ./run.sh -d alsa_input.usb-<vendor>_<model>-00.analog-stereo
    ```

### Options

| Flag | Effect |
| --- | --- |
| `-c, --config PATH` | Use another config file (default: `config.toml`) |
| `-d, --device NAME` | Override the microphone |
| `-m, --model NAME` | Override the Whisper model |
| `--list-devices` | List microphones and exit |
| `--dry-run` | Print actions instead of performing them |

## Default commands

`config.example.toml` ships with:

| Say | Does |
| --- | --- |
| "top left", "bottom right", "center", ... | Move the mouse there |
| "move up/down/left/right" | Nudge the mouse 100 px |
| "click", "double click", "right click" | Click |
| "scroll up/down" | Scroll |
| "open terminal" | Open a terminal |
| "open Firefox", "launch the file manager" | Launch the closest matching installed app |
| "type Hello, world" | Type `Hello, world` |
| "submit", "press enter" | Press Enter |
| "space" | Press Space |
| "workspace 3" | Switch Hyprland workspace (1-10) |

Commands match anywhere in the sentence, so "move the mouse to the top
left" works too, and close mishearings like "top lift" still match.

## Configuration

`config.toml` has a `[settings]` table and any number of `[[command]]`
entries. It is git-ignored; `config.example.toml` is the template.

### Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `device` | `""` | Source name from `--list-devices`; empty = system default |
| `model` | `small.en` | `tiny.en`, `base.en`, `small.en`, `medium.en`, `large-v3`, `distil-large-v3` |
| `compute_type` | `int8` | `int8` for CPU, `float16` for a CUDA GPU |
| `language` | `en` | Transcription language |
| `wake_word` | `""` | If set, only sentences starting with it are acted on |
| `silence_ms` | `700` | Pause length that ends a sentence |
| `energy_threshold` | `0` | Speech loudness threshold; `0` = auto-calibrate |
| `fuzzy_cutoff` | `0.8` | How close a mishearing must be to count (0-1) |
| `screen` | `[1920, 1080]` | Resolution, used for `"50%"` coordinates |
| `launcher` | `gtk-launch {id}` | Command used by `launch`; `{id}` is the desktop file id |
| `beam_size` | `1` | Whisper beam size; higher is slower, slightly more accurate |
| `prompt` | `auto` | Words that bias recognition; `auto` uses your command words |

### Commands

```toml
# Fixed phrases
[[command]]
phrases = ["top left", "mouse top left"]
actions = [{ type = "move", x = "1%", y = "1%" }]

# Regex with a captured placeholder
[[command]]
pattern = '^(?:open|launch|start) (?:the )?(?P<app>.+)$'
actions = [{ type = "launch", app = "{app}" }]

# Several actions in a row
[[command]]
phrases = ["copy that"]
actions = [
  { type = "click", button = "left", count = 2 },
  { type = "key", keys = "29:1 46:1 46:0 29:0" },  # Ctrl+C
]
```

- `phrases` match anywhere in what you said; `pattern` is a Python regex
  on the lowercased, punctuation-free transcript. Add `raw = true` to match
  Whisper's original text instead (keeps casing and punctuation).
- Named groups like `(?P<app>...)` fill `{app}` in the actions.
- Priority: a phrase equal to the whole sentence, then patterns, then
  phrases contained in the sentence, then fuzzy matches.

### Actions

| Type | Fields | Notes |
| --- | --- | --- |
| `move` | `x`, `y` | Absolute; pixels or `"50%"` |
| `move_relative` | `dx`, `dy` | |
| `click` | `button`, `count` | `left`, `right`, `middle` |
| `scroll` | `amount` | Positive scrolls down |
| `type` | `text` | |
| `key` | `keys` | ydotool: `"28:1 28:0"` (keycode:state); xdotool: `"Return"` |
| `shell` | `cmd` | Runs in the background via `sh` |
| `launch` | `app` | Fuzzy-matched against installed `.desktop` files |
| `sleep` | `ms` | |

Key codes for ydotool are Linux input event codes, listed in
`/usr/include/linux/input-event-codes.h`.

An unknown action `type` stops the script at startup with the list of
valid types, so typos in `config.toml` show up immediately.

## Troubleshooting

- **Long delay before commands run.** Check the timing in the log. If
  clips hit `max_utterance_s`, background noise is above the threshold:
  raise `energy_threshold`. If transcription itself is slow, use a smaller
  model (`base.en`).
- **Commands fire during normal conversation.** Set a `wake_word`.
- **Absolute mouse moves land in the wrong place (Wayland).** ydotool
  moves the pointer relatively, so pointer acceleration distorts it. Use a
  flat acceleration profile for the ydotool virtual device.
- **"open X" picks the wrong app or none.** Whisper may mishear unusual
  names; add a `phrases` command with a `shell` action for that app.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how the code is organised.

## License

MIT, see [LICENSE](LICENSE).
