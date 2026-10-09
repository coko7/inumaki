import sys
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

import tomllib


class Settings(TypedDict):
    device: str
    model: str
    compute_type: str
    language: str
    wake_word: str
    silence_ms: int
    min_speech_ms: int
    max_utterance_s: int
    beam_size: int
    max_new_tokens: int
    prompt: str
    prompt_max_words: int
    energy_threshold: float
    fuzzy_cutoff: float
    screen: list[int]
    launcher: str


DEFAULT_SETTINGS: Settings = {
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


# Coordinates are pixels or a percentage of the screen, e.g. "50%".
Coord = int | str


class MoveAction(TypedDict):
    type: Literal["move"]
    x: Coord
    y: Coord


class MoveRelativeAction(TypedDict):
    type: Literal["move_relative"]
    dx: NotRequired[int]
    dy: NotRequired[int]


class ClickAction(TypedDict):
    type: Literal["click"]
    button: NotRequired[str]
    count: NotRequired[int]


class ScrollAction(TypedDict):
    type: Literal["scroll"]
    amount: NotRequired[int]


class TypeAction(TypedDict):
    type: Literal["type"]
    text: str


class KeyAction(TypedDict):
    type: Literal["key"]
    keys: str


class ShellAction(TypedDict):
    type: Literal["shell"]
    cmd: str


class LaunchAction(TypedDict):
    type: Literal["launch"]
    app: str


class SleepAction(TypedDict):
    type: Literal["sleep"]
    ms: NotRequired[float]


Action = (
    MoveAction
    | MoveRelativeAction
    | ClickAction
    | ScrollAction
    | TypeAction
    | KeyAction
    | ShellAction
    | LaunchAction
    | SleepAction
)

# Keep in sync with the `type` literals above.
ACTION_TYPES: frozenset[str] = frozenset(
    {
        "move",
        "move_relative",
        "click",
        "scroll",
        "type",
        "key",
        "shell",
        "launch",
        "sleep",
    }
)


class Command(TypedDict):
    phrases: NotRequired[list[str]]
    pattern: NotRequired[str]
    raw: NotRequired[bool]
    actions: list[Action]


def load_config(path: Path | str) -> tuple[Settings, list[Command]]:
    with open(path, "rb") as f:
        cfg: dict[str, object] = tomllib.load(f)

    # The TOML is trusted to match the types above; apart from action types
    # it isn't validated. Casting via `object` tells the checker that's on us.
    overrides = cast(dict[str, object], cfg.get("settings", {}))
    merged: dict[str, object] = {**DEFAULT_SETTINGS, **overrides}
    settings = cast(Settings, cast(object, merged))
    commands = cast(list[Command], cfg.get("command", []))

    if not commands:
        sys.exit(f"No [[command]] entries in {path}")

    for command in commands:
        for action in command["actions"]:
            if action["type"] not in ACTION_TYPES:
                sys.exit(
                    f"Unknown action type {action['type']!r} in {path}; "
                    + f"expected one of: {', '.join(sorted(ACTION_TYPES))}"
                )

    return settings, commands
