import collections
import subprocess

import numpy as np

from .util import log

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2  # s16le mono


def list_devices():
    out = subprocess.run(
        ["pactl", "list", "sources", "short"], capture_output=True, text=True
    ).stdout

    print("Available sources:\n")
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

    cmd = [
        "parec",
        "--format=s16le",
        f"--rate={SAMPLE_RATE}",
        "--channels=1",
        "--raw",
        "--latency-msec=30",
    ]

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
                        log(
                            f"  utterance hit max_utterance_s; background noise "
                            f"may be above threshold {threshold:.0f}"
                        )
                    pcm = np.frombuffer(b"".join(buf), dtype=np.int16)
                    out_q.put(pcm.astype(np.float32) / 32768.0)
                voiced = False
                preroll.clear()
    finally:
        proc.terminate()
