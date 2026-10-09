import time
from typing import TYPE_CHECKING

from .audio import SAMPLE_RATE, Audio
from .config import Command, Settings
from .util import log, normalize

if TYPE_CHECKING:
    from faster_whisper import WhisperModel  # pyright: ignore[reportMissingTypeStubs]


def build_prompt(settings: Settings, commands: list[Command]) -> str:
    """Words that bias recognition toward known commands.

    Unique words only: a long phrase list gets parroted back by Whisper
    ("workspace 1, workspace 9, ...") and decoding that is slow.
    """

    prompt = settings["prompt"]
    if prompt != "auto":
        return prompt

    phrases = [settings["wake_word"]]

    # collect all phrases
    phrases += [p for c in commands for p in c.get("phrases", [])]
    words = dict.fromkeys(
        w for p in phrases for w in normalize(p).split() if not w.isdigit()
    )

    return " ".join(list(words)[: settings["prompt_max_words"]])


class Transcriber:
    def __init__(self, settings: Settings, commands: list[Command]):
        # Slow import, so it only happens once the model is actually needed.
        from faster_whisper import WhisperModel  # pyright: ignore[reportMissingTypeStubs]

        self.settings: Settings = settings
        log(f"Loading model {settings['model']} ({settings['compute_type']})...")
        self.model: WhisperModel = WhisperModel(
            settings["model"], device="auto", compute_type=settings["compute_type"]
        )
        self.prompt: str = build_prompt(settings, commands)
        log(f"Prompt: {self.prompt!r}")

    def __call__(self, audio: Audio) -> tuple[str, float, float]:
        """Return (transcript, seconds taken, audio length in seconds)."""
        started = time.monotonic()
        # faster_whisper types `vad_parameters` as a bare `dict`, hence the ignore.
        segments, _ = self.model.transcribe(  # pyright: ignore[reportUnknownMemberType]
            audio,
            language=self.settings["language"] or None,
            beam_size=self.settings["beam_size"],
            vad_filter=True,
            condition_on_previous_text=False,
            without_timestamps=True,
            max_new_tokens=self.settings["max_new_tokens"],
            initial_prompt=self.prompt or None,
        )
        # transcribe() is lazy; decoding happens while consuming segments.
        kept = [s for s in segments if s.no_speech_prob < 0.6]
        text = " ".join(s.text.strip() for s in kept).strip()
        return text, time.monotonic() - started, len(audio) / SAMPLE_RATE
