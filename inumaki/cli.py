import argparse
import queue
import re
import sys
import threading
from pathlib import Path

from . import __doc__ as description
from .actions import Executor
from .audio import Audio, capture, list_devices
from .config import Settings, load_config
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


def parse_args() -> Args:
    ap = argparse.ArgumentParser(prog="inumaki", description=description)
    _ = ap.add_argument("-c", "--config", type=Path)
    _ = ap.add_argument("-d", "--device", help="pactl source name (overrides config)")
    _ = ap.add_argument("-m", "--model", help="whisper model (overrides config)")
    _ = ap.add_argument("--list-devices", action="store_true")
    _ = ap.add_argument("--dry-run", action="store_true", help="print actions only")
    return ap.parse_args(namespace=Args())


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
    matcher = Matcher(commands, settings["fuzzy_cutoff"])
    wake = normalize(settings["wake_word"])

    # Same prefix on the raw transcript, tolerating punctuation ("Computer, ...").
    wake_re = re.compile(
        r"^\W*" + r"\W+".join(map(re.escape, wake.split())) + r"\W*", re.IGNORECASE
    )
    transcribe = Transcriber(settings, commands)

    utterances, stop, errors = start_capture(settings)
    device = settings["device"] or "default source"
    log(f"Listening on {device} (backend: {executor.backend}). Ctrl+C to quit.")

    try:
        while True:
            audio = utterances.get()
            if audio is None:
                sys.exit(f"Capture failed: {errors[0]}")

            raw, took, length = transcribe(audio)
            text = normalize(raw)
            timing = (
                f"{length:.1f}s audio, transcribed in {took:.1f}s, "
                f"{utterances.qsize()} queued"
            )

            if not text:
                log(f"(nothing recognised; {timing})")
                continue

            log(f'heard: "{raw}" ({timing})')

            if wake:
                if not text.startswith(wake):
                    continue

                text = text[len(wake) :].strip()
                raw = wake_re.sub("", raw, count=1)

            hit = matcher.match(text, raw)
            if not hit:
                log("  no matching command")
                continue

            label, cmd, params = hit
            log(f'  -> "{label}" {params or ""}')
            for action in cmd["actions"]:
                executor.run(action, params)

    except KeyboardInterrupt:
        pass

    finally:
        stop.set()
