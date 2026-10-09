import argparse
import queue
import re
import sys
import threading
from pathlib import Path

from . import __doc__ as description
from .actions import Executor
from .audio import capture, list_devices
from .config import load_config
from .matching import Matcher
from .transcribe import Transcriber
from .util import log, normalize

REPO_DIR = Path(__file__).resolve().parent.parent


def parse_args():
    ap = argparse.ArgumentParser(prog="inumaki", description=description)
    ap.add_argument("-c", "--config", default=REPO_DIR / "config.toml")
    ap.add_argument("-d", "--device", help="pactl source name (overrides config)")
    ap.add_argument("-m", "--model", help="whisper model (overrides config)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print actions only")
    return ap.parse_args()


def start_capture(settings):
    """Run capture in a thread; the queue yields utterances, or None on failure."""
    utterances = queue.Queue()
    stop = threading.Event()
    errors = []

    def run():
        try:
            capture(settings["device"], settings, utterances, stop)
        except Exception as e:
            errors.append(e)
            utterances.put(None)

    threading.Thread(target=run, daemon=True).start()
    return utterances, stop, errors


def main():
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
    wake_re = re.compile(r"^\W*" + r"\W+".join(map(re.escape, wake.split())) + r"\W*",
                         re.IGNORECASE)
    transcribe = Transcriber(settings, commands)

    utterances, stop, errors = start_capture(settings)
    log(f"Listening on {settings['device'] or 'default source'} "
        f"(backend: {executor.backend}). Ctrl+C to quit.")

    try:
        while True:
            audio = utterances.get()
            if audio is None:
                sys.exit(f"Capture failed: {errors[0]}")
            raw, took, length = transcribe(audio)
            text = normalize(raw)
            timing = (f"{length:.1f}s audio, transcribed in {took:.1f}s, "
                      f"{utterances.qsize()} queued")
            if not text:
                log(f"(nothing recognised; {timing})")
                continue
            log(f'heard: "{raw}" ({timing})')

            if wake:
                if not text.startswith(wake):
                    continue
                text = text[len(wake):].strip()
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
