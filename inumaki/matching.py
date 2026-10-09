import difflib
import re

from .config import Command
from .util import normalize

# (label, command, placeholder values from the pattern's named groups)
Match = tuple[str, Command, dict[str, str]]


class Matcher:
    """Whole-utterance phrase > regex pattern > contained phrase > fuzzy phrase.

    match() returns (label, command, params) or None; params holds the
    pattern's named groups.
    """

    def __init__(self, commands: list[Command], fuzzy_cutoff: float):
        self.fuzzy_cutoff: float = fuzzy_cutoff
        # (normalized phrase, command)
        self.entries: list[tuple[str, Command]] = []
        # (compiled regex, command, match raw text?)
        self.patterns: list[tuple[re.Pattern[str], Command, bool]] = []

        for cmd in commands:
            for phrase in cmd.get("phrases", []):
                self.entries.append((normalize(phrase), cmd))

            if "pattern" in cmd:
                raw = cmd.get("raw", False)
                flags = re.IGNORECASE if raw else 0
                self.patterns.append((re.compile(cmd["pattern"], flags), cmd, raw))

    def match(self, text: str, raw_text: str) -> Match | None:
        # A phrase that is the whole utterance always wins ("open terminal").
        for phrase, cmd in self.entries:
            if phrase == text:
                return phrase, cmd, {}

        # Patterns before contained phrases, so "type double click" types.
        for regex, cmd, raw in self.patterns:
            matched = regex.search(raw_text if raw else text)
            if matched:
                return regex.pattern, cmd, matched.groupdict(default="")

        padded = f" {text} "
        contained = [
            (phrase, cmd) for phrase, cmd in self.entries if f" {phrase} " in padded
        ]

        if contained:
            phrase, cmd = max(contained, key=lambda e: len(e[0]))
            return phrase, cmd, {}

        best: Match | None = None
        best_score = 0.0

        for phrase, cmd in self.entries:
            score = difflib.SequenceMatcher(None, phrase, text).ratio()
            if score > best_score:
                best = phrase, cmd, {}
                best_score = score

        return best if best_score >= self.fuzzy_cutoff else None
