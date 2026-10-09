import time

from .audio import SAMPLE_RATE
from .util import log, normalize


def build_prompt(settings, commands):
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
    def __init__(self, settings, commands):
        from faster_whisper import WhisperModel  # slow import

        self.settings = settings
        log(f"Loading model {settings['model']} ({settings['compute_type']})...")
        self.model = WhisperModel(
            settings["model"], device="auto", compute_type=settings["compute_type"]
        )
        self.prompt = build_prompt(settings, commands)
        log(f"Prompt: {self.prompt!r}")

    def __call__(self, audio):
        """Return (transcript, seconds taken, audio length in seconds)."""
        started = time.monotonic()
        segments, _ = self.model.transcribe(
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
        segments = [s for s in segments if s.no_speech_prob < 0.6]
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, time.monotonic() - started, len(audio) / SAMPLE_RATE
