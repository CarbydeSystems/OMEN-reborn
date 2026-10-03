"""Tests for alphabet construction and selection."""

from __future__ import annotations

import pytest

from omen.alphabet import Alphabet, effective_alphabet_size, select_alphabet
from omen.errors import ConfigError, TrainingError


def test_from_chars_builds_inverse_index() -> None:
    alpha = Alphabet.from_chars("abc")
    assert alpha.size == 3
    assert alpha.index["a"] == 0
    assert alpha.index["c"] == 2
    assert alpha.encode("cab") == [2, 0, 1]
    assert alpha.decode([2, 0, 1]) == "cab"


def test_encode_returns_none_for_foreign_char() -> None:
    alpha = Alphabet.from_chars("abc")
    assert alpha.encode("axc") is None


def test_duplicate_and_multichar_rejected() -> None:
    with pytest.raises(ConfigError):
        Alphabet.from_chars("aab")
    with pytest.raises(ConfigError):
        Alphabet.from_chars(["ab", "c"])
    with pytest.raises(ConfigError):
        Alphabet.from_chars("")


def test_decode_bounds_checked() -> None:
    alpha = Alphabet.from_chars("abc")
    with pytest.raises(ConfigError):
        alpha.decode([5])


def test_select_alphabet_picks_most_frequent() -> None:
    passwords = ["aaaa", "aaab", "bbcc", "abcd"]
    selection = select_alphabet(passwords, size=2)
    assert set(selection.alphabet.chars) == {"a", "b"}
    assert 0.0 < selection.coverage <= 1.0
    assert selection.distinct_chars == 4


def test_select_alphabet_is_deterministic_on_ties() -> None:
    passwords = ["abcd"]  # all chars tie at frequency 1
    first = select_alphabet(passwords, size=2).alphabet.as_string()
    second = select_alphabet(passwords, size=2).alphabet.as_string()
    assert first == second == "ab"  # ties broken by code point


def test_select_alphabet_rejects_empty_corpus() -> None:
    with pytest.raises(TrainingError):
        select_alphabet([], size=4)


# -- floor parameter (reserved-character profiles) --------------------------


def test_floor_guarantees_inclusion_even_at_zero_count() -> None:
    """A floor character absent from the corpus entirely still gets a slot."""
    selection = select_alphabet(["aaaa", "aaab"], size=3, floor={"z"})
    assert "z" in selection.alphabet.chars
    assert selection.alphabet.size == 3


def test_floor_is_excluded_from_frequency_competition() -> None:
    """A floor character doesn't also consume a frequency-ranked remainder
    slot — the remainder is filled entirely from non-floor characters."""
    # 'a' is both floored and the most frequent char; size=2 leaves exactly
    # one remainder slot, which should go to 'b' (next most frequent), not
    # be wasted re-selecting 'a'.
    selection = select_alphabet(["aaaa", "aaab", "aaac"], size=2, floor={"a"})
    assert set(selection.alphabet.chars) == {"a", "b"}


def test_floor_reproduces_and_fixes_the_real_bug() -> None:
    """Synthetic repro of the actual incident this feature exists for: a rare
    base letter loses to a frequent foreign character under plain frequency
    ranking at a small alphabet size, but survives once it's floored."""
    rare_letter = "q"
    foreign_char = "の"  # の — the real incident's intruder
    corpus = [rare_letter] + [foreign_char] * 5  # foreign clearly outranks rare

    # Without a floor, frequency ranking alone drops the rare letter.
    bare = select_alphabet(corpus, size=1)
    assert rare_letter not in bare.alphabet.chars
    assert foreign_char in bare.alphabet.chars

    # With the rare letter floored, it survives regardless of frequency.
    floored = select_alphabet(corpus, size=2, floor={rare_letter})
    assert rare_letter in floored.alphabet.chars


def test_floor_larger_than_size_raises() -> None:
    with pytest.raises(TrainingError, match="floor needs"):
        select_alphabet(["abcdef"], size=2, floor={"a", "b", "c"})


def test_empty_floor_matches_original_behaviour() -> None:
    """floor=() (the default) must be byte-identical to pre-floor selection."""
    passwords = ["aaaa", "aaab", "bbcc", "abcd"]
    with_default = select_alphabet(passwords, size=3)
    with_explicit_empty = select_alphabet(passwords, size=3, floor=())
    assert with_default.alphabet.as_string() == with_explicit_empty.alphabet.as_string()


# -- effective_alphabet_size (floor/symbol-budget compensation) -------------


def test_effective_size_is_a_true_noop_with_no_floor() -> None:
    """No floor means nothing to compensate for — regression guard: this must
    hold even when requested_size is far smaller than min_symbol_slots."""
    assert effective_alphabet_size(72, ()) == 72
    assert effective_alphabet_size(3, ()) == 3


def test_effective_size_raises_to_cover_floor_plus_headroom() -> None:
    floor = set("abcdefghij")  # 10 chars
    assert effective_alphabet_size(12, floor, min_symbol_slots=14) == 24  # 10 + 14


def test_effective_size_keeps_requested_when_already_large_enough() -> None:
    floor = set("abc")  # 3 chars
    assert effective_alphabet_size(100, floor, min_symbol_slots=14) == 100


def test_effective_size_min_symbol_slots_is_configurable() -> None:
    floor = set("abcdefghij")  # 10 chars
    assert effective_alphabet_size(12, floor, min_symbol_slots=0) == 12
    assert effective_alphabet_size(12, floor, min_symbol_slots=5) == 15


# -- prefer parameter (script-aware remainder selection) ---------------------


def test_prefer_fills_remainder_from_preferred_pool_first() -> None:
    """A preferred candidate must win a remainder slot over a far more
    frequent non-preferred one — the round-3 incident, reproduced directly:
    '?' (sparse) lost to a foreign character (dense) under plain frequency."""
    passwords = ["?" * 3, "の" * 10]  # の = の, intentionally louder
    selection = select_alphabet(passwords, size=1, prefer=lambda ch: ch.isascii())
    assert selection.alphabet.chars == ("?",)


def test_prefer_falls_through_to_the_rest_once_preferred_is_exhausted() -> None:
    passwords = ["ab", "の" * 5]  # only 2 preferred (ascii) chars exist
    selection = select_alphabet(passwords, size=3, prefer=lambda ch: ch.isascii())
    assert set(selection.alphabet.chars) == {"a", "b", "の"}


def test_prefer_none_matches_pre_fix_behaviour() -> None:
    """prefer=None (the default) must be byte-identical to pure frequency."""
    passwords = ["aaaa", "aaab", "bbcc", "abcd"]
    without = select_alphabet(passwords, size=3)
    explicit_none = select_alphabet(passwords, size=3, prefer=None)
    assert without.alphabet.as_string() == explicit_none.alphabet.as_string()


# -- excluded (ranked-but-unchosen candidates) --------------------------


def test_excluded_covers_every_non_floor_candidate_not_chosen() -> None:
    passwords = ["aaaa", "aaab", "bbcc", "abcd"]
    selection = select_alphabet(passwords, size=2)
    chosen = set(selection.alphabet.chars)
    excluded_chars = {ch for ch, _ in selection.excluded}
    assert chosen | excluded_chars == {"a", "b", "c", "d"}
    assert chosen.isdisjoint(excluded_chars)


def test_excluded_reflects_the_prefer_partition_too() -> None:
    """A preferred candidate that still didn't make it must appear in
    excluded alongside non-preferred ones — same accounting either way."""
    passwords = ["a", "b", "c"] + ["の"] * 100  # の far outnumbers any letter
    selection = select_alphabet(passwords, size=1, prefer=lambda ch: ch.isascii())
    assert selection.alphabet.chars == ("a",)  # ties broken by code point
    assert {ch for ch, _ in selection.excluded} == {"b", "c", "の"}
