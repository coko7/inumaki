#!/usr/bin/env python3
"""Continuous voice control: mic -> faster-whisper -> actions.

Audio is captured with `parec` (PulseAudio/PipeWire) so any source can be
targeted by name. Speech is segmented with a simple energy gate, transcribed
by faster-whisper, then matched against commands in config.toml.
"""

import argparse
import collections
import difflib
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2  # s16le mono

DEFAULT_SETTINGS = {
    "device": "",  # pactl source name, "" = system default
    "model": "small.en",
    "compute_type": "int8",
    "language": "en",
    "wake_word": "",  # if set, utterances must start with it
    "silence_ms": 700,  # trailing silence that ends an utterance
    "min_speech_ms": 250,
    "max_utterance_s": 10,
    "beam_size": 1,  # 1 = greedy (fast); 5 = slightly more accurate, much slower
    "max_new_tokens": 80,  # stops runaway hallucinations from taking forever
    "prompt": "auto",  # "auto" = unique command words, "" = none, or custom text
    "prompt_max_words": 40,
    "energy_threshold": 0,  # 0 = auto-calibrate from background noise
    "fuzzy_cutoff": 0.8,
    "screen": [1920, 1080],  # used for "50%"-style coordinates
    "launcher": "gtk-launch {id}",  # {id} = desktop file id
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #

def list_devices():
    out = subprocess.run(
        ["pactl", "list", "sources", "short"], capture_output=True, text=True
    ).stdout
    print("Available sources (use the 2nd column as `device`):\n")
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and not cols[1].endswith(".monitor"):
            print(f"  {cols[1]}")
    print("\nMonitors (system output loopback) omitted.")


def rms(frame_bytes):
    samples = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


def capture(device, settings, out_q, stop):
    """Read mic, push complete utterances (float32 arrays) onto out_q."""
    cmd = ["parec", "--format=s16le", f"--rate={SAMPLE_RATE}", "--channels=1",
           "--raw", "--latency-msec=30"]
    if device:
        cmd.append(f"--device={device}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    def read_frame():
        data = proc.stdout.read(FRAME_BYTES)
        if not data or len(data) < FRAME_BYTES:
            raise RuntimeError("audio stream ended (bad device name?)")
        return data

    threshold = settings["energy_threshold"]
    if not threshold:
        log("Calibrating noise floor, stay quiet for 1s...")
        levels = [rms(read_frame()) for _ in range(1000 // FRAME_MS)]
        noise = float(np.percentile(levels, 90))
        threshold = max(noise * 3.0, 300.0)
        log(f"Noise floor {noise:.0f}, speech threshold {threshold:.0f}")

    silence_frames = settings["silence_ms"] // FRAME_MS
    min_frames = settings["min_speech_ms"] // FRAME_MS
    max_frames = settings["max_utterance_s"] * 1000 // FRAME_MS
    preroll = collections.deque(maxlen=300 // FRAME_MS)

    voiced, buf, quiet, loud = False, [], 0, 0
    try:
        while not stop.is_set():
            frame = read_frame()
            is_loud = rms(frame) > threshold
            if not voiced:
                preroll.append(frame)
                if is_loud:
                    voiced, buf, quiet, loud = True, list(preroll), 0, 1
                continue
            buf.append(frame)
            loud += is_loud
            quiet = 0 if is_loud else quiet + 1
            if quiet >= silence_frames or len(buf) >= max_frames:
                if loud >= min_frames:
                    if len(buf) >= max_frames:
                        log(f"  utterance hit max_utterance_s; background noise "
                            f"may be above threshold {threshold:.0f}")
                    pcm = np.frombuffer(b"".join(buf), dtype=np.int16)
                    out_q.put(pcm.astype(np.float32) / 32768.0)
                voiced = False
                preroll.clear()
    finally:
        proc.terminate()


# --------------------------------------------------------------------------- #
# Desktop apps
# --------------------------------------------------------------------------- #

def application_dirs():
    data_home = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    # Earlier dirs win, so user entries override system ones.
    return [Path(d) / "applications" for d in [data_home, *data_dirs.split(":")] if d]


def parse_desktop_entry(path):
    entry, in_main = {}, False
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            in_main = line == "[Desktop Entry]"
        elif in_main and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            entry.setdefault(key.strip(), value.strip())
    return entry


class AppIndex:
    """Spoken name -> desktop file id, built from installed .desktop files."""

    def __init__(self):
        self.apps = None  # list of (desktop id, display name, [normalized aliases])

    def refresh(self):
        seen, apps = set(), []
        for d in application_dirs():
            if not d.is_dir():
                continue
            for path in sorted(d.rglob("*.desktop")):
                app_id = str(path.relative_to(d)).replace("/", "-")[:-len(".desktop")]
                if app_id in seen:
                    continue
                seen.add(app_id)
                e = parse_desktop_entry(path)
                if (not e or e.get("Type") != "Application"
                        or e.get("NoDisplay") == "true" or e.get("Hidden") == "true"):
                    continue
                name = e.get("Name", app_id)
                aliases = [name, e.get("GenericName", ""), app_id.split(".")[-1]]
                aliases += e.get("Keywords", "").split(";")
                aliases = [a for a in dict.fromkeys(map(normalize, aliases)) if a]
                apps.append((app_id, name, aliases))
        self.apps = apps

    def find(self, spoken):
        if self.apps is None:
            self.refresh()
        spoken = normalize(spoken)
        squashed = spoken.replace(" ", "")
        # Exact display name, then exact alias (spaces ignored: "libre office").
        for app_id, name, aliases in self.apps:
            if normalize(name).replace(" ", "") == squashed:
                return app_id, name
        for app_id, name, aliases in self.apps:
            if any(a.replace(" ", "") == squashed for a in aliases):
                return app_id, name
        best, best_score = None, 0.0
        for app_id, name, aliases in self.apps:
            for i, alias in enumerate(aliases):
                score = difflib.SequenceMatcher(None, alias, spoken).ratio()
                score -= 0.05 * min(i, 3)  # prefer name over keywords
                if score > best_score:
                    best, best_score = (app_id, name), score
        return best if best_score >= 0.6 else None


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #

class Executor:
    BUTTONS_YDO = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}
    BUTTONS_XDO = {"left": "1", "right": "3", "middle": "2"}

    def __init__(self, settings, dry_run=False):
        self.screen = settings["screen"]
        self.launcher = settings["launcher"]
        self.apps = AppIndex()
        self.dry_run = dry_run
        wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland"
        if wayland or not shutil.which("xdotool"):
            self.backend = "ydotool"
        else:
            self.backend = "xdotool"
        if not shutil.which(self.backend) and not dry_run:
            sys.exit(f"{self.backend} not found in PATH")

    def _run(self, *args, shell=False):
        if self.dry_run:
            log(f"  (dry-run) {args[0] if shell else ' '.join(map(str, args))}")
            return
        if shell:
            subprocess.Popen(args[0], shell=True)
        else:
            subprocess.run([str(a) for a in args], check=False)

    def _coord(self, value, axis):
        if isinstance(value, str) and value.endswith("%"):
            return int(self.screen[axis] * float(value[:-1]) / 100)
        return int(value)

    def launch(self, spoken):
        found = self.apps.find(spoken)
        if not found:
            self.apps.refresh()  # maybe installed since startup
            found = self.apps.find(spoken)
        if not found:
            log(f'  no installed app matches "{spoken}"')
            return
        app_id, name = found
        log(f"  launching {name} ({app_id})")
        cmd = [part.format(id=app_id) for part in shlex.split(self.launcher)]
        if self.dry_run:
            log(f"  (dry-run) {' '.join(cmd)}")
            return
        subprocess.Popen(cmd, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run(self, action, params=None):
        if params:  # fill "{name}" placeholders from regex captures
            action = {k: v.format_map(params) if isinstance(v, str) else v
                      for k, v in action.items()}
        t = action["type"]
        ydo = self.backend == "ydotool"
        if t == "move":
            x, y = self._coord(action["x"], 0), self._coord(action["y"], 1)
            if ydo:
                self._run("ydotool", "mousemove", "--absolute", "-x", x, "-y", y)
            else:
                self._run("xdotool", "mousemove", x, y)
        elif t == "move_relative":
            dx, dy = int(action.get("dx", 0)), int(action.get("dy", 0))
            if ydo:
                self._run("ydotool", "mousemove", "-x", dx, "-y", dy)
            else:
                self._run("xdotool", "mousemove_relative", "--", dx, dy)
        elif t == "click":
            button = action.get("button", "left")
            count = int(action.get("count", 1))
            if ydo:
                self._run("ydotool", "click", "--repeat", count,
                          "--next-delay", 80, self.BUTTONS_YDO[button])
            else:
                self._run("xdotool", "click", "--repeat", count,
                          self.BUTTONS_XDO[button])
        elif t == "scroll":
            amount = int(action.get("amount", 3))  # positive = down
            if ydo:
                self._run("ydotool", "mousemove", "--wheel", "-x", 0, "-y", -amount)
            else:
                btn = "5" if amount > 0 else "4"
                self._run("xdotool", "click", "--repeat", abs(amount), btn)
        elif t == "type":
            text = action["text"]
            if ydo:
                self._run("ydotool", "type", "--", text)
            else:
                self._run("xdotool", "type", "--", text)
        elif t == "key":
            # ydotool: raw "keycode:state" list, e.g. "29:1 46:1 46:0 29:0"
            # xdotool: keysym combo, e.g. "ctrl+c"
            self._run(self.backend, "key", *str(action["keys"]).split())
        elif t == "shell":
            self._run(action["cmd"], shell=True)
        elif t == "launch":
            self.launch(action["app"])
        elif t == "sleep":
            time.sleep(float(action.get("ms", 100)) / 1000)
        else:
            log(f"  unknown action type: {t}")


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def normalize(text):
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^\w\s%]", "", text)
    return " ".join(text.split())


class Matcher:
    """Whole-utterance phrase > regex pattern > contained phrase > fuzzy phrase.

    match() returns (label, command, params) or None; params holds the
    pattern's named groups.
    """

    def __init__(self, commands, fuzzy_cutoff):
        self.fuzzy_cutoff = fuzzy_cutoff
        self.entries = []  # (normalized phrase, command)
        self.patterns = []  # (compiled regex, command, match raw text?)
        for cmd in commands:
            for phrase in cmd.get("phrases", []):
                self.entries.append((normalize(phrase), cmd))
            if "pattern" in cmd:
                raw = cmd.get("raw", False)
                flags = re.IGNORECASE if raw else 0
                self.patterns.append((re.compile(cmd["pattern"], flags), cmd, raw))

    def match(self, text, raw_text):
        # A phrase that is the whole utterance always wins ("open terminal").
        for phrase, cmd in self.entries:
            if phrase == text:
                return phrase, cmd, {}
        # Patterns before contained phrases, so "type double click" types.
        for regex, cmd, raw in self.patterns:
            m = regex.search(raw_text if raw else text)
            if m:
                return regex.pattern, cmd, m.groupdict()
        padded = f" {text} "
        contained = [(p, c) for p, c in self.entries if f" {p} " in padded]
        if contained:
            phrase, cmd = max(contained, key=lambda e: len(e[0]))
            return phrase, cmd, {}
        best, best_score = None, 0.0
        for phrase, cmd in self.entries:
            score = difflib.SequenceMatcher(None, phrase, text).ratio()
            if score > best_score:
                best, best_score = (phrase, cmd, {}), score
        return best if best_score >= self.fuzzy_cutoff else None


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    settings = {**DEFAULT_SETTINGS, **cfg.get("settings", {})}
    commands = cfg.get("command", [])
    if not commands:
        sys.exit(f"No [[command]] entries in {path}")
    return settings, commands


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default=here / "config.toml")
    ap.add_argument("-d", "--device", help="pactl source name (overrides config)")
    ap.add_argument("-m", "--model", help="whisper model (overrides config)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print actions only")
    args = ap.parse_args()

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

    from faster_whisper import WhisperModel  # slow import, after arg parsing

    log(f"Loading model {settings['model']} ({settings['compute_type']})...")
    model = WhisperModel(settings["model"], device="auto",
                         compute_type=settings["compute_type"])
    # Bias recognition toward known command words.
    # Unique words only: a long phrase list gets parroted back by Whisper
    # ("workspace 1, workspace 9, ...") and decoding that is slow.
    prompt = settings["prompt"]
    if prompt == "auto":
        phrases = [settings["wake_word"]]
        phrases += [p for c in commands for p in c.get("phrases", [])]
        words = dict.fromkeys(w for p in phrases for w in normalize(p).split()
                              if not w.isdigit())
        prompt = " ".join(list(words)[:settings["prompt_max_words"]])
    log(f"Prompt: {prompt!r}")

    utterances = queue.Queue()
    stop = threading.Event()
    errors = []

    def capture_thread():
        try:
            capture(settings["device"], settings, utterances, stop)
        except Exception as e:
            errors.append(e)
            utterances.put(None)

    threading.Thread(target=capture_thread, daemon=True).start()
    log(f"Listening on {settings['device'] or 'default source'} "
        f"(backend: {executor.backend}). Ctrl+C to quit.")

    try:
        while True:
            audio = utterances.get()
            if audio is None:
                sys.exit(f"Capture failed: {errors[0]}")
            started = time.monotonic()
            segments, _ = model.transcribe(
                audio,
                language=settings["language"] or None,
                beam_size=settings["beam_size"],
                vad_filter=True,
                condition_on_previous_text=False,
                without_timestamps=True,
                max_new_tokens=settings["max_new_tokens"],
                initial_prompt=prompt or None,
            )
            # transcribe() is lazy; decoding happens while consuming segments.
            segments = [s for s in segments if s.no_speech_prob < 0.6]
            took = time.monotonic() - started
            raw = " ".join(s.text.strip() for s in segments).strip()
            text = normalize(raw)
            timing = (f"{len(audio) / SAMPLE_RATE:.1f}s audio, transcribed in "
                      f"{took:.1f}s, {utterances.qsize()} queued")
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


if __name__ == "__main__":
    main()
