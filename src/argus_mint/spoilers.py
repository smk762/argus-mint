"""The private spoiler list: terms a project's text and images must not give away.

The file lives outside every repository, at the path in ``ARGUS_MINT_SPOILERS`` (keep it
``chmod 600``). One rule per line; blank lines and ``#`` comments are skipped::

    # 1: a name nobody may learn yet
    some name
    re:\\bthe (thing|other thing)\\b

A plain line matches as a whole phrase, ignoring case and runs of spaces; a ``re:`` line is a
regular expression, also ignoring case. A hit reports only the rule's **number** (its place
among the rules, counting from 1), never the term or the text around it, so nothing that
records or shows a hit (a draft, the wire, a log, a fixture) repeats the spoiler.

Pure: reading the file and matching, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["SpoilerError", "SpoilerList", "load_spoilers"]


class SpoilerError(ValueError):
    """The spoiler file can't be read, or a rule in it is invalid (named by number only)."""


@dataclass(frozen=True)
class SpoilerList:
    rules: tuple[re.Pattern[str], ...]

    def hits(self, *texts: str) -> list[int]:
        """The numbers (from 1) of every rule that matches any of ``texts``, in order.

        Each text is searched on its own: ``^``/``$`` anchor to every text, and no match spans two.
        """
        return [n for n, rule in enumerate(self.rules, start=1) if any(rule.search(t) for t in texts)]

    def __len__(self) -> int:
        return len(self.rules)


EMPTY = SpoilerList(())


def _phrase(term: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in term.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


def parse(text: str) -> SpoilerList:
    rules: list[re.Pattern[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            try:
                rules.append(re.compile(line[3:].strip(), re.IGNORECASE))
            except re.error:
                raise SpoilerError(f"spoiler rule {len(rules) + 1} is not a valid regular expression") from None
        else:
            rules.append(_phrase(line))
    return SpoilerList(tuple(rules))


def load_spoilers(path: Path | None) -> SpoilerList:
    """Load the list at ``path``; no path means no rules. A missing or unreadable file is an error."""
    if path is None:
        return EMPTY
    try:
        return parse(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise SpoilerError(f"the spoiler file can't be read ({e.strerror})") from None
