"""Character alphabet handling.

The model operates over a fixed, ordered alphabet of single characters.  The
alphabet bounds the size of the dense n-gram tables (``A^n`` entries), so it is
typically *reduced* to the most frequent characters in the training corpus via
:func:`select_alphabet`.

:class:`Alphabet` is an immutable value object.  Every character-to-index
lookup is bounds-checked so that neither a hostile corpus nor a hostile model
file can drive an out-of-range table access downstream.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from omen.errors import ConfigError, TrainingError

# Hard cap on alphabet size.  Dense tables are ``A^n``; this keeps a crafted or
# accidental value from requesting an enormous allocation.  256 comfortably
# covers printable ASCII plus common Latin-1 symbols.
MAX_ALPHABET_SIZE = 256

# Default minimum non-floor slots guaranteed by effective_alphabet_size(). A
# large profile floor can otherwise leave almost nothing for symbols and
# punctuation; 14 is a reasonable starting point, not a measured optimum —
# override via TrainingOptions.min_symbol_slots / --min-symbol-slots.
DEFAULT_MIN_SYMBOL_SLOTS = 14


@dataclass(frozen=True, slots=True)
class Alphabet:
    """An immutable, ordered set of single-character symbols.

    ``chars[i]`` is the character with index ``i``; ``index[c]`` is the inverse.
    Construct via :meth:`from_chars` so the index map is built and validated.
    """

    chars: tuple[str, ...]
    index: Mapping[str, int]

    @classmethod
    def from_chars(cls, chars: Iterable[str]) -> Alphabet:
        """Build an alphabet from an ordered iterable of distinct characters."""
        ordered = tuple(chars)
        if not ordered:
            raise ConfigError("alphabet must not be empty")
        if len(ordered) > MAX_ALPHABET_SIZE:
            raise ConfigError(f"alphabet size {len(ordered)} exceeds maximum {MAX_ALPHABET_SIZE}")
        for ch in ordered:
            if len(ch) != 1:
                raise ConfigError(f"alphabet entries must be single characters, got {ch!r}")
        mapping = {ch: i for i, ch in enumerate(ordered)}
        if len(mapping) != len(ordered):
            raise ConfigError("alphabet contains duplicate characters")
        return cls(ordered, MappingProxyType(mapping))

    @property
    def size(self) -> int:
        """Number of symbols in the alphabet (``A``)."""
        return len(self.chars)

    def contains(self, ch: str) -> bool:
        """Return whether ``ch`` is part of this alphabet."""
        return ch in self.index

    def encode(self, text: str) -> list[int] | None:
        """Map ``text`` to indices, or return ``None`` if any char is foreign.

        Returning ``None`` lets callers skip whole passwords containing
        out-of-alphabet characters rather than fabricating misleading n-grams
        from the surviving fragments.
        """
        out: list[int] = []
        idx = self.index
        for ch in text:
            code = idx.get(ch)
            if code is None:
                return None
            out.append(code)
        return out

    def decode(self, codes: Iterable[int]) -> str:
        """Map indices back to a string, bounds-checking each index."""
        chars = self.chars
        size = len(chars)
        out: list[str] = []
        for code in codes:
            if not 0 <= code < size:
                raise ModelIndexError(code, size)
            out.append(chars[code])
        return "".join(out)

    def as_string(self) -> str:
        """Serialise the alphabet to a plain string for persistence."""
        return "".join(self.chars)


class ModelIndexError(ConfigError):
    """Raised when an alphabet index is outside the valid range."""

    def __init__(self, code: int, size: int) -> None:
        super().__init__(f"alphabet index {code} out of range [0, {size})")


@dataclass(frozen=True, slots=True)
class AlphabetSelection:
    """Result of :func:`select_alphabet`: the alphabet plus coverage stats."""

    alphabet: Alphabet
    coverage: float
    """Fraction of corpus characters retained by the chosen alphabet (0..1)."""
    total_chars: int
    distinct_chars: int
    excluded: tuple[tuple[str, int], ...] = ()
    """Non-floor candidates ranked but not chosen, i.e. ``(char, count)`` pairs
    for every corpus character that didn't get a slot. Most frequent first
    within each ``prefer`` partition (see :func:`select_alphabet`) — used by
    :func:`~omen.profiles.alphabet_warnings` to name the strongest candidate
    a foreign character crowded out."""


def effective_alphabet_size(
    requested_size: int,
    floor: Iterable[str],
    *,
    min_symbol_slots: int = DEFAULT_MIN_SYMBOL_SLOTS,
) -> int:
    """Raise ``requested_size`` so a profile floor never crowds out symbols.

    A floor (see :mod:`omen.profiles`) guarantees its own characters a slot
    inside a fixed-size alphabet; left uncompensated, a large floor can leave
    only a handful of slots for everything else — punctuation, symbols, any
    non-floor character the corpus actually uses. This guarantees at least
    ``min_symbol_slots`` beyond the floor instead of silently shrinking the
    remainder. Always a no-op (returns ``requested_size`` unchanged) for
    ``floor=()`` — with nothing reserved, there is no budget to compensate
    for — and whenever the floor is already small relative to
    ``requested_size``.
    """
    floor_size = len(frozenset(floor))
    if floor_size == 0:
        return requested_size
    return max(requested_size, floor_size + min_symbol_slots)


def select_alphabet(
    passwords: Iterable[str],
    size: int,
    *,
    floor: Iterable[str] = (),
    prefer: Callable[[str], bool] | None = None,
) -> AlphabetSelection:
    """Choose ``size`` characters across ``passwords``: a reserved floor first,
    the remainder by frequency.

    ``floor`` (typically a language profile's :func:`~omen.profiles.floor_chars`)
    is included unconditionally — even a character with zero occurrences in the
    corpus still gets a slot, so it remains representable rather than silently
    excluded. The remaining ``size - len(floor)`` slots are filled by frequency
    from the corpus characters *not* already in the floor, same tie-break
    (descending count, then ascending code point) as when ``floor`` is empty —
    which is the original, unconstrained "top `size` by global frequency"
    behaviour this parameter is additive to.

    ``prefer``, when given, splits that remainder ranking into two pools
    before filling slots: characters where ``prefer(ch)`` is true, and
    everything else. Every preferred candidate outranks every non-preferred
    one regardless of relative frequency — the remainder is filled from the
    preferred pool first, only drawing from the rest if the preferred pool
    can't cover the budget. Typically a profile's expected-script check (see
    :func:`~omen.profiles.is_expected_script`): the floor already guarantees
    a profile's own letters a slot, but without ``prefer`` the *remainder*
    slots are still pure frequency, so a large foreign-script corpus sample
    (e.g. a multi-language breach compilation) can still crowd out a
    legitimate same-script symbol there — the same failure mode ``floor``
    exists to prevent, just one layer further out.

    Candidates that ranked but didn't make the cut are returned too, as
    :attr:`AlphabetSelection.excluded` — e.g. for
    :func:`~omen.profiles.alphabet_warnings` to name one.

    Raises :class:`TrainingError` if ``floor`` alone has more distinct
    characters than ``size`` — raise ``size`` rather than silently dropping
    floor characters, which would reintroduce the exact bug a floor exists to
    prevent.
    """
    if size < 1:
        raise TrainingError("alphabet size must be at least 1")
    if size > MAX_ALPHABET_SIZE:
        raise TrainingError(f"alphabet size {size} exceeds maximum {MAX_ALPHABET_SIZE}")

    floor_set = frozenset(floor)
    if len(floor_set) > size:
        raise TrainingError(
            f"profile floor needs {len(floor_set)} characters but size is only "
            f"{size} — raise --alphabet-size to at least {len(floor_set)}"
        )

    counts: Counter[str] = Counter()
    for pw in passwords:
        # A literal U+FFFD (replacement character) is a decode artifact, never
        # a real password character — never let it compete for a slot, even
        # when the caller bypassed read_corpus's own filtering (see io_utils).
        if "�" in pw:
            continue
        counts.update(pw)

    total = sum(counts.values())
    if total == 0:
        raise TrainingError("corpus contains no characters to build an alphabet from")

    remaining_slots = size - len(floor_set)
    candidates = [(ch, n) for ch, n in counts.items() if ch not in floor_set]
    ranked = _rank_candidates(candidates, prefer)
    chosen = sorted(floor_set) + [ch for ch, _ in ranked[:remaining_slots]]
    alphabet = Alphabet.from_chars(chosen)
    retained = sum(counts.get(ch, 0) for ch in chosen)
    return AlphabetSelection(
        alphabet=alphabet,
        coverage=retained / total,
        total_chars=total,
        distinct_chars=len(counts),
        excluded=tuple(ranked[remaining_slots:]),
    )


def _rank_candidates(
    candidates: list[tuple[str, int]], prefer: Callable[[str], bool] | None
) -> list[tuple[str, int]]:
    """Frequency-rank ``candidates`` (descending count, then code point).

    Partitioned by ``prefer`` first when given: every preferred candidate
    outranks every non-preferred one, so filling slots from the front of the
    result exhausts the preferred pool before touching the rest.
    """
    if prefer is None:
        return sorted(candidates, key=lambda kv: (-kv[1], kv[0]))
    preferred = sorted((kv for kv in candidates if prefer(kv[0])), key=lambda kv: (-kv[1], kv[0]))
    rest = sorted((kv for kv in candidates if not prefer(kv[0])), key=lambda kv: (-kv[1], kv[0]))
    return preferred + rest
