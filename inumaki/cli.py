import argparse
import queue
import re
import sys
import threading
from pathlib import Path

from . import __doc__ as description
from .actions import Executor
from .audio import Audio, capture, list_devices
from .config import Command, Settings, load_config
from .matching import Matcher
from .transcribe import Transcriber
from .util import log, normalize

REPO_DIR = Path(__file__).resolve().parent.parent


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


class Dispatcher:
    """Turns one transcript into actions: wake word, matching, execution."""

    def __init__(self, settings: Settings, commands: list[Command], executor: Executor):
        self.executor: Executor = executor
        self.matcher: Matcher = Matcher(commands, settings["fuzzy_cutoff"])
        self.wake: str = normalize(settings["wake_word"])

        # Same prefix on the raw transcript, tolerating punctuation ("Computer, ...").
        self.wake_re: re.Pattern[str] = re.compile(
            r"^\W*" + r"\W+".join(map(re.escape, self.wake.split())) + r"\W*",
            re.IGNORECASE,
        )

    def __call__(self, raw: str) -> None:
        text = normalize(raw)
        if self.wake:
            if not text.startswith(self.wake):
                return

            text = text[len(self.wake) :].strip()
            raw = self.wake_re.sub("", raw, count=1)

        hit = self.matcher.match(text, raw)
        if not hit:
            log("  no matching command")
            return

        label, cmd, params = hit
        log(f'  -> "{label}" {params or ""}')
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
    utterances, stop, errors = start_capture(settings)
    device = settings["device"] or "default source"
    log(f"Listening on {device} (backend: {backend}). Ctrl+C to quit.")

    try:
        while True:
            audio = utterances.get()
            if audio is None:
                sys.exit(f"Capture failed: {errors[0]}")

            raw, took, length = transcribe(audio)
            timing = (
                f"{length:.1f}s audio, transcribed in {took:.1f}s, "
                + f"{utterances.qsize()} queued"
            )
            if not normalize(raw):
                log(f"(nothing recognised; {timing})")
                continue

            log(f'heard: "{raw}" ({timing})')
            dispatch(raw)

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
