"""The unified masking entry (统一脱敏入口).

Every model call goes through this module first: direct identifiers are replaced
with typed placeholders before any text leaves the process.  The detector then
works on masked text only, and quotes stored in the review queue therefore never
contain raw personal data.

The masked text is the *reference* for quote verification: a model quote must be
found verbatim in the masked segment, exactly as the model received it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Iterable, Sequence

from .segments import Segment

TOKEN_TEMPLATE = "[PII:{kind}]"

#: Direct identifiers recognised by the masking gate.  ID comes before BANK so an
#: 18-digit identity number is typed as an ID and not as a card number; phone
#: numbers are the shortest pattern and never overlap the other two.
_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ID", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("BANK", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
)

NAME_KIND = "NAME"


@dataclass(frozen=True)
class MaskReplacement:
    kind: str
    raw_start: int
    raw_end: int
    masked_start: int
    masked_end: int

    @property
    def token(self) -> str:
        return TOKEN_TEMPLATE.format(kind=self.kind)


@dataclass(frozen=True)
class _Piece:
    """A contiguous run of text, either copied or a placeholder."""

    masked_start: int
    masked_end: int
    raw_start: int
    raw_end: int
    placeholder: bool


@dataclass(frozen=True)
class MaskedText:
    raw: str
    text: str
    replacements: tuple[MaskReplacement, ...]
    pieces: tuple[_Piece, ...]

    def raw_span(self, masked_start: int, masked_end: int) -> tuple[int, int] | None:
        """Map a masked span back to raw offsets.

        Returns ``None`` when the span cuts through a placeholder: such a span has
        no exact raw counterpart.
        """

        if masked_start < 0 or masked_end > len(self.text) or masked_end <= masked_start:
            return None
        pieces = [piece for piece in self.pieces if piece.masked_start < masked_end and piece.masked_end > masked_start]
        if not pieces:
            return None
        if any(piece.placeholder and (masked_start != piece.masked_start or masked_end != piece.masked_end) for piece in pieces):
            return None
        first = pieces[0]
        last = pieces[-1]
        if masked_start < first.masked_start or masked_end > last.masked_end:
            return None
        raw_start = first.raw_start + (masked_start - first.masked_start)
        raw_end = last.raw_start + (masked_end - last.masked_start)
        return raw_start, raw_end


def _candidate_matches(text: str, roster: Sequence[str]) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for kind, pattern in _IDENTIFIER_PATTERNS:
        for match in pattern.finditer(text):
            matches.append((match.start(), match.end(), kind))
    names = sorted({name for name in roster if name}, key=len, reverse=True)
    if names:
        name_pattern = re.compile("|".join(re.escape(name) for name in names))
        for match in name_pattern.finditer(text):
            matches.append((match.start(), match.end(), NAME_KIND))
    return matches


def mask_text(text: str, roster: Iterable[str] = ()) -> MaskedText:
    """Replace direct identifiers in ``text`` with typed placeholders."""

    candidates = sorted(_candidate_matches(text, tuple(roster)), key=lambda item: (item[0], -(item[1] - item[0])))
    accepted: list[tuple[int, int, str]] = []
    cursor = 0
    for start, end, kind in candidates:
        if start < cursor:
            continue  # overlapping candidate inside an already-masked span
        accepted.append((start, end, kind))
        cursor = end

    out: list[str] = []
    pieces: list[_Piece] = []
    replacements: list[MaskReplacement] = []
    masked_len = 0
    position = 0
    for start, end, kind in accepted:
        if start > position:
            chunk = text[position:start]
            pieces.append(_Piece(masked_len, masked_len + len(chunk), position, start, False))
            out.append(chunk)
            masked_len += len(chunk)
        token = TOKEN_TEMPLATE.format(kind=kind)
        pieces.append(_Piece(masked_len, masked_len + len(token), start, end, True))
        out.append(token)
        replacements.append(
            MaskReplacement(kind=kind, raw_start=start, raw_end=end, masked_start=masked_len, masked_end=masked_len + len(token))
        )
        masked_len += len(token)
        position = end
    if position < len(text):
        chunk = text[position:]
        pieces.append(_Piece(masked_len, masked_len + len(chunk), position, len(text), False))
        out.append(chunk)

    return MaskedText(
        raw=text,
        text="".join(out),
        replacements=tuple(replacements),
        pieces=tuple(pieces),
    )


@dataclass(frozen=True)
class MaskedDocument:
    """The masked view of a whole draft plus its per-segment span mapping."""

    segments: tuple[Segment, ...]
    per_segment: dict[int, MaskedText]

    @property
    def replacements(self) -> int:
        return sum(len(masked.replacements) for masked in self.per_segment.values())


def mask_segments(segments: Sequence[Segment], roster: Iterable[str] = ()) -> MaskedDocument:
    """Run the masking gate over every segment of a draft."""

    roster_values = tuple(roster)
    masked_segments: list[Segment] = []
    per_segment: dict[int, MaskedText] = {}
    for segment in segments:
        masked = mask_text(segment.text, roster_values)
        per_segment[segment.seq] = masked
        masked_segments.append(Segment(seq=segment.seq, text=masked.text))
    return MaskedDocument(segments=tuple(masked_segments), per_segment=per_segment)


__all__ = [
    "TOKEN_TEMPLATE",
    "MaskReplacement",
    "MaskedDocument",
    "MaskedText",
    "mask_segments",
    "mask_text",
]
