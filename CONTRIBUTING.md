# Contributing

Notes for working on inumaki's code. For installing, configuring and
running it, see the [README](README.md).

## Development setup

```bash
./run.sh --dry-run    # creates .venv and config.toml on first run
```

`run.sh` is a thin wrapper. Once `.venv` exists you can run the package
directly:

```bash
.venv/bin/python -m inumaki --dry-run
```

`--dry-run` prints every action instead of performing it.
`--text` reads utterances from stdin instead of the mic, skipping capture and the Whisper
model entirely.

Together they test matching in under a second,
and they also work piped, which is handy for scripted checks:

```bash
printf 'top left\nopen firefox\nworkspace three\n' \
  | .venv/bin/python -m inumaki --text --dry-run
```

## Project structure

```
inumaki/
├── run.sh                 # creates .venv and config.toml, then runs the package
├── config.example.toml    # template config (config.toml is git-ignored)
├── requirements.txt
└── inumaki/
    ├── __main__.py        # entry point for `python -m inumaki`
    ├── cli.py             # arguments, capture thread, main loop
    ├── config.py          # default settings, config types, loading config.toml
    ├── audio.py           # mic capture with parec, speech segmenting, --list-devices
    ├── transcribe.py      # Whisper model and recognition hint (`prompt`)
    ├── matching.py        # phrase / pattern / fuzzy matching
    ├── actions.py         # mouse, keys, typing, shell, launch
    ├── apps.py            # finding installed apps from .desktop files
    └── util.py            # log(), normalize()
```

How one utterance flows through it:

1. `audio.capture` reads 30 ms frames from `parec` and starts recording
   when a frame is louder than the noise threshold. After `silence_ms` of
   quiet, the recording goes onto a queue.
2. `cli.listen_mic` takes it off the queue and `transcribe.Transcriber`
   turns it into text. In `--text` mode, `cli.read_text` reads the text
   from stdin instead and steps 1–2 are skipped.
3. `cli.Dispatcher` strips the wake word, `matching.Matcher` finds the
   command, then `actions.Executor` runs its actions.

## Adding an action type

1. In `config.py`, add a TypedDict for it with `type: Literal["..."]`.
2. Add it to the `Action` union and to `ACTION_TYPES`.
3. Handle it in `Executor.run` in `actions.py`. Compare `action["type"]`
   directly so the type checker narrows `action` to your TypedDict.
4. Document it in the README's Actions table and in
   `config.example.toml`.

## Type checking

The code is fully type-annotated and passes
[basedpyright](https://docs.basedpyright.com/) in its default mode with
no warnings. Point it at the project venv so numpy and faster-whisper
resolve:

```bash
basedpyright --pythonpath .venv/bin/python inumaki
```
