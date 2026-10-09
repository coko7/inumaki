import difflib
import re

from .util import normalize


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
            matched = regex.search(raw_text if raw else text)
            if matched:
                return regex.pattern, cmd, matched.groupdict()

        padded = f" {text} "
        contained = [
            (phrase, cmd) for phrase, cmd in self.entries if f" {phrase} " in padded
        ]

        if contained:
            phrase, cmd = max(contained, key=lambda e: len(e[0]))
            return phrase, cmd, {}

        best, best_score = None, 0.0

        for phrase, cmd in self.entries:
            score = difflib.SequenceMatcher(None, phrase, text).ratio()
            if score > best_score:
                best, best_score = (phrase, cmd, {}), score

        return best if best_score >= self.fuzzy_cutoff else None
