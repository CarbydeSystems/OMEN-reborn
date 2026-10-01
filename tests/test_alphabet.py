"""Tests for alphabet construction and selection."""

from __future__ import annotations

import pytest

from omen.alphabet import Alphabet, select_alphabet
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
