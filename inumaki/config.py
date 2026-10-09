import sys
import tomllib

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


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    settings = {**DEFAULT_SETTINGS, **cfg.get("settings", {})}
    commands = cfg.get("command", [])
    if not commands:
        sys.exit(f"No [[command]] entries in {path}")
    return settings, commands
