import argparse
import queue
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import __doc__ as description
from .actions import Executor
from .audio import Audio, capture, list_devices
from .config import Command, Dictate, Settings, load_config
from .matching import Matcher
from .transcribe import Dictation, Transcriber
from .util import log, normalize

REPO_DIR = Path(__file__).resolve().parent.parent
# After a bare "type french", how long the next utterance counts as dictation.
DICTATION_WAIT_S = 15


class Args(argparse.Namespace):
    """Typed view of the parsed command line."""

    config: Path = REPO_DIR / "config.toml"
    device: str | None = None
    model: str | None = None
    list_devices: bool = False
    dry_run: bool = False
    text: bool = False


def parse_args() -> Args:
    ap = argparse.ArgumentParser(prog="inumaki", description=description)
    _ = ap.add_argument("-c", "--config", type=Path)
    _ = ap.add_argument("-d", "--device", help="pactl source name (overrides config)")
    _ = ap.add_argument("-m", "--model", help="whisper model (overrides config)")
    _ = ap.add_argument("--list-devices", action="store_true")
    _ = ap.add_argument("--dry-run", action="store_true", help="print actions only")
    _ = ap.add_argument(
        "-t",
        "--text",
        action="store_true",
        help="read utterances as text lines from stdin instead of the mic",
    )
    return ap.parse_args(namespace=Args())


def resumes_listening(cmd: Command) -> bool:
    return any(
        action["type"] == "listening" and action["enabled"] for action in cmd["actions"]
    )


def is_meta(cmd: Command) -> bool:
    """Commands about inumaki itself, which "repeat" must not re-run."""
    return any(action["type"] in ("repeat", "listening") for action in cmd["actions"])


class Dispatcher:
    """Turns one transcript into actions: wake word, matching, execution.

    Also owns the actions that need state across utterances: "repeat"
    and "listening", and re-transcribing commands with `dictate`.
    """

    def __init__(self, settings: Settings, commands: list[Command], executor: Executor):
        self.executor: Executor = executor
        self.matcher: Matcher = Matcher(commands, settings["fuzzy_cutoff"])
        # While paused, only commands that turn listening back on can match.
        self.resume_matcher: Matcher = Matcher(
            [cmd for cmd in commands if resumes_listening(cmd)],
            settings["fuzzy_cutoff"],
        )
        self.listening: bool = True
        self.wake: str = normalize(settings["wake_word"])
        # Last command run and its placeholder values, for the "repeat" action.
        self.last: tuple[Command, dict[str, str]] | None = None
        # Set in mic mode for `dictate` commands:
        # (audio, trigger words) -> the audio after them, or None if not found
        self.cut_after: Callable[[Audio, str], Audio | None] | None = None
        # (audio, language) -> transcript in that language
        self.dictation: Callable[[Audio, str], str] | None = None
        # A `dictate` command said without text ("type french"), waiting for
        # the next utterance: (command, params, dictate spec, since)
        self.pending: tuple[Command, dict[str, str], Dictate, float] | None = None

        # Same prefix on the raw transcript, tolerating punctuation ("Computer, ...").
        self.wake_re: re.Pattern[str] = re.compile(
            r"^\W*" + r"\W+".join(map(re.escape, self.wake.split())) + r"\W*",
            re.IGNORECASE,
        )

    def __call__(self, raw: str, audio: Audio | None = None) -> None:
        text = normalize(raw)
        if self.wake:
            if not text.startswith(self.wake):
                return

            text = text[len(self.wake) :].strip()
            raw = self.wake_re.sub("", raw, count=1)

        matcher = self.matcher if self.listening else self.resume_matcher
        hit = matcher.match(text, raw)
        if not hit:
            log("  no matching command" if self.listening else "  (paused, ignored)")
            return

        label, cmd, params = hit
        log(f'  -> "{label}" {params or ""}')
        if "dictate" in cmd:
            dictate = cmd["dictate"]
            if not params.get(dictate.get("into", "text")):
                # Paused after the trigger: the text comes as its own utterance.
                self.pending = cmd, params, dictate, time.monotonic()
                log(f'  say the {dictate["language"]} text now ({DICTATION_WAIT_S}s)')
                return
            params = self._dictate(dictate, params, audio)

        self._run(cmd, params)

    def waiting_dictation(self) -> Dictate | None:
        """The dictate spec if the next utterance should be dictated."""
        if self.pending is None:
            return None
        if time.monotonic() - self.pending[3] > DICTATION_WAIT_S:
            log("  (dictation timed out)")
            self.pending = None
            return None
        return self.pending[2]

    def finish_dictation(self, text: str) -> None:
        """Run the pending `dictate` command with `text` as its dictated text."""
        if self.pending is None:
            return
        cmd, params, dictate, _ = self.pending
        self.pending = None

        # Drop the period Whisper adds to sentences, like the "type" pattern does.
        text = text.strip().removesuffix(".")
        if not text:
            log("  nothing dictated")
            return
        self._run(cmd, {**params, dictate.get("into", "text"): text})

    def _run(self, cmd: Command, params: dict[str, str]) -> None:
        if not is_meta(cmd):
            # "again" twice in a row re-runs the same real command, and never
            # re-runs a pause/resume.
            self.last = cmd, params

        for action in cmd["actions"]:
            if action["type"] == "repeat":
                self._repeat()
            elif action["type"] == "listening":
                self._set_listening(action["enabled"])
            else:
                self.executor.run(action, params)

    def _dictate(
        self, dictate: Dictate, params: dict[str, str], audio: Audio | None
    ) -> dict[str, str]:
        """Replace the dictated placeholder with the other-language transcript."""
        if audio is None or self.cut_after is None or self.dictation is None:
            log("  (no audio in --text mode: using the text as given)")
            return params

        started = time.monotonic()
        rest = self.cut_after(audio, dictate["after"])
        text = "" if rest is None else self.dictation(rest, dictate["language"])
        if not text:
            log(
                f'  nothing dictated after "{dictate["after"]}"; '
                + "using the English transcript"
            )
            return params

        # Drop the period Whisper adds to sentences, like the "type" pattern does.
        text = text.removesuffix(".")
        took = time.monotonic() - started
        log(f'  dictated ({dictate["language"]}, {took:.1f}s): "{text}"')
        return {**params, dictate.get("into", "text"): text}

    def _set_listening(self, enabled: bool) -> None:
        self.listening = enabled
        if enabled:
            log("  listening again")
        else:
            log("  paused: only commands that resume listening are accepted")

    def _repeat(self) -> None:
        if self.last is None:
            log("  nothing to repeat yet")
            return

        cmd, params = self.last
        for action in cmd["actions"]:
            self.executor.run(action, params)


def start_capture(
    settings: Settings,
) -> tuple[queue.Queue[Audio | None], threading.Event, list[Exception]]:
    """Run capture in a thread; the queue yields utterances, or None on failure."""

    utterances: queue.Queue[Audio | None] = queue.Queue()
    stop = threading.Event()
    errors: list[Exception] = []

    def run() -> None:
        try:
            capture(settings["device"], settings, utterances, stop)
        except Exception as e:
            errors.append(e)
            utterances.put(None)

    threading.Thread(target=run, daemon=True).start()
    return utterances, stop, errors


def listen_mic(
    settings: Settings, commands: list[Command], dispatch: Dispatcher, backend: str
) -> None:
    transcribe = Transcriber(settings, commands)
    if any("dictate" in cmd for cmd in commands):
        dispatch.cut_after = transcribe.cut_after
        dispatch.dictation = Dictation(settings)

    utterances, stop, errors = start_capture(settings)
    device = settings["device"] or "default source"
    log(f"Listening on {device} (backend: {backend}). Ctrl+C to quit.")

    try:
        while True:
            audio = utterances.get()
            if audio is None:
                sys.exit(f"Capture failed: {errors[0]}")

            dictate = dispatch.waiting_dictation()
            if dictate and dispatch.dictation:
                started = time.monotonic()
                text = dispatch.dictation(audio, dictate["language"])
                took = time.monotonic() - started
                log(f'dictated ({dictate["language"]}, {took:.1f}s): "{text}"')
                dispatch.finish_dictation(text)
                continue

            raw, took, length = transcribe(audio)
            timing = (
                f"{length:.1f}s audio, transcribed in {took:.1f}s, "
                + f"{utterances.qsize()} queued"
            )
            if not normalize(raw):
                log(f"(nothing recognised; {timing})")
                continue

            log(f'heard: "{raw}" ({timing})')
            dispatch(raw, audio)

    finally:
        stop.set()


def read_text(dispatch: Dispatcher, backend: str) -> None:
    """Treat each stdin line as a transcript. Works interactively or piped."""
    interactive = sys.stdin.isatty()
    if interactive:
        log(f"Text mode (backend: {backend}). Type what you'd say; Ctrl+D to quit.")

    while True:
        try:
            raw = input("> " if interactive else "").strip()
        except EOFError:
            return
        if not raw:
            continue

        if not interactive:
            log(f'heard: "{raw}"')
        if dispatch.waiting_dictation():
            dispatch.finish_dictation(raw)
        else:
            dispatch(raw)


def main() -> None:
    args = parse_args()
    if args.list_devices:
        list_devices()
        return

    settings, commands = load_config(args.config)
    if args.device:
        settings["device"] = args.device
    if args.model:
        settings["model"] = args.model

    executor = Executor(settings, dry_run=args.dry_run)
    dispatch = Dispatcher(settings, commands, executor)

    try:
        if args.text:
            read_text(dispatch, executor.backend)
        else:
            listen_mic(settings, commands, dispatch, executor.backend)
    except KeyboardInterrupt:
        pass
