"""Tests for TrainingOptions validation and auto-computed max_length."""

from __future__ import annotations

import math
import statistics

import pytest

from omen.errors import TrainingError
from omen.model import MAX_PASSWORD_LENGTH
from omen.train import MIN_SAMPLES_FOR_PERCENTILE, ModelTrainer, TrainingOptions


def test_max_length_std_rejects_non_positive() -> None:
    with pytest.raises(TrainingError, match="max_length_std"):
        TrainingOptions(max_length_std=0.0).validate()
    with pytest.raises(TrainingError, match="max_length_std"):
        TrainingOptions(max_length_std=-1.0).validate()


def test_max_length_percentile_rejects_out_of_range() -> None:
    with pytest.raises(TrainingError, match="max_length_percentile"):
        TrainingOptions(max_length_percentile=0.0).validate()
    with pytest.raises(TrainingError, match="max_length_percentile"):
        TrainingOptions(max_length_percentile=1.5).validate()
    with pytest.raises(TrainingError, match="max_length_percentile"):
        TrainingOptions(max_length_percentile=-0.1).validate()


def test_max_length_percentile_allows_the_boundary_value() -> None:
    TrainingOptions(max_length_percentile=1.0).validate()


def test_min_symbol_slots_rejects_negative() -> None:
    with pytest.raises(TrainingError, match="min_symbol_slots"):
        TrainingOptions(min_symbol_slots=-1).validate()


def test_explicit_max_length_is_never_recomputed() -> None:
    """An explicit max_length is the escape hatch back to the old fixed
    behaviour — it must win regardless of the corpus's own length spread."""
    corpus = ["a" * 3, "a" * 50, "a" * 3, "a" * 3]  # wildly skewed lengths
    model = ModelTrainer(TrainingOptions(ngram=2, alphabet="a", max_length=10)).train(
        lambda: iter(corpus)
    )
    assert model.max_length == 10


def test_auto_max_length_matches_mean_plus_k_std() -> None:
    """Hand-computed ground truth: a small, uniform-alphabet corpus whose
    mean/stdev are easy to verify by hand, checked against the same formula
    the implementation uses (ceil(mean + k*std), population stdev)."""
    lengths = [3, 4, 4, 5, 5, 5, 6, 6, 7]
    corpus = ["a" * n for n in lengths]
    k = 1.5
    expected_raw = statistics.fmean(lengths) + k * statistics.pstdev(lengths)

    model = ModelTrainer(TrainingOptions(ngram=2, alphabet="a", max_length_std=k)).train(
        lambda: iter(corpus)
    )

    assert model.max_length == math.ceil(expected_raw)


def test_auto_max_length_responds_to_the_std_multiplier() -> None:
    lengths = [5, 5, 5, 5, 15]  # one long outlier pulls stdev way up
    corpus = ["a" * n for n in lengths]

    low_k = ModelTrainer(TrainingOptions(ngram=2, alphabet="a", max_length_std=0.1)).train(
        lambda: iter(corpus)
    )
    high_k = ModelTrainer(TrainingOptions(ngram=2, alphabet="a", max_length_std=3.0)).train(
        lambda: iter(corpus)
    )
    assert high_k.max_length > low_k.max_length


def test_auto_max_length_clamped_above_context_length() -> None:
    """A corpus dominated by short passwords computes a raw mean+k*std below
    ctx_len (ngram - 1 = 4 here); it must clamp up to ctx_len to stay
    trainable, rather than producing a max_length shorter than one context."""
    corpus = ["a"] * 10 + ["aaaa"] * 2  # mean/std pulled well below 4 by the 1s
    model = ModelTrainer(TrainingOptions(ngram=5, alphabet="a", max_length_std=1.0)).train(
        lambda: iter(corpus)
    )
    assert model.max_length == 4  # ctx_len = ngram - 1, the clamp floor


def test_auto_max_length_clamped_at_the_hard_maximum() -> None:
    lengths = [MAX_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH - 1]
    corpus = ["a" * n for n in lengths]
    model = ModelTrainer(TrainingOptions(ngram=2, alphabet="a", max_length_std=50.0)).train(
        lambda: iter(corpus)
    )
    assert model.max_length == MAX_PASSWORD_LENGTH


def test_auto_max_length_percentile_covers_a_thin_tail_std_would_miss() -> None:
    """Reproduces the real mismatch the hybrid exists for: a tight,
    low-variance cluster (std of 95 passwords at length 10) plus a thin long
    tail (5 at length 30). mean+1.5*std alone lands at ~17.5 — the tail is
    many stdevs out even though it isn't that much longer in absolute terms
    — while the 99.5th percentile reaches the tail directly."""
    lengths = [10] * 95 + [30] * 5  # n=100 >= MIN_SAMPLES_FOR_PERCENTILE
    corpus = ["a" * n for n in lengths]
    mean = statistics.fmean(lengths)
    stdev = statistics.pstdev(lengths, mean)
    std_based = mean + 1.5 * stdev
    ordered = sorted(lengths)
    percentile_based = ordered[min(len(ordered) - 1, int(0.995 * len(ordered)))]
    assert percentile_based > std_based  # the mismatch actually exists in this fixture

    model = ModelTrainer(TrainingOptions(ngram=2, alphabet="a")).train(lambda: iter(corpus))
    assert model.max_length == math.ceil(max(std_based, percentile_based))
    assert model.max_length == 30  # concretely: the tail is fully covered


def test_auto_max_length_percentile_skipped_below_the_sample_floor() -> None:
    """The same shape as above, but too few samples to trust a percentile —
    must fall back to the pure mean+std estimate, not reach for the tail."""
    lengths = [10] * 25 + [30] * 1  # n=26 < MIN_SAMPLES_FOR_PERCENTILE
    assert len(lengths) < MIN_SAMPLES_FOR_PERCENTILE
    corpus = ["a" * n for n in lengths]
    mean = statistics.fmean(lengths)
    stdev = statistics.pstdev(lengths, mean)
    std_based = mean + 1.5 * stdev

    model = ModelTrainer(TrainingOptions(ngram=2, alphabet="a")).train(lambda: iter(corpus))
    assert model.max_length == math.ceil(std_based)
    assert model.max_length == 17  # concretely: nowhere near the lone-outlier tail (30)


def test_profile_prefers_same_script_remainder_over_louder_foreign_noise() -> None:
    """Reproduces the round-3 incident synthetically: a foreign character
    far more frequent than a legitimate ascii symbol must no longer win the
    one remainder slot over it, now that --profile wires select_alphabet's
    script-aware prefer in automatically."""
    from omen.profiles import floor_chars

    floor_only = "".join(floor_chars("en"))
    corpus = [floor_only] + ["?"] * 3 + ["の"] * 50  # の, intentionally louder
    model = ModelTrainer(
        TrainingOptions(profile="en", alphabet_size=63, min_symbol_slots=1, max_length=62)
    ).train(lambda: iter(corpus))

    assert "?" in model.alphabet.chars
    assert "の" not in model.alphabet.chars


def test_alphabet_selection_is_exposed_after_auto_selection() -> None:
    trainer = ModelTrainer(TrainingOptions(ngram=2, alphabet_size=3, max_length=5))
    trainer.train(lambda: iter(["abc", "aab", "bca"]))
    assert trainer.alphabet_selection is not None
    assert isinstance(trainer.alphabet_selection.excluded, tuple)


def test_alphabet_selection_is_none_for_an_explicit_alphabet() -> None:
    """No select_alphabet call happens at all when --alphabet is explicit —
    nothing to expose, so it must stay None rather than a stale/empty stand-in."""
    trainer = ModelTrainer(TrainingOptions(ngram=2, alphabet="abc", max_length=5))
    trainer.train(lambda: iter(["abc", "aab", "bca"]))
    assert trainer.alphabet_selection is None


def test_profile_floor_auto_raises_the_trained_alphabet_size() -> None:
    """A corpus with >= 14 distinct non-floor characters plus --profile de
    must end up with an alphabet sized to the floor plus the full default
    symbol headroom (84 = 70 + 14), not squeezed into the bare requested
    alphabet_size (72) the floor would otherwise leave almost no room in."""
    from omen.profiles import floor_chars

    symbols = "!@#$%^&*()-_=+"  # 14 distinct non-floor characters
    corpus = [f"passwort{s}" for s in symbols] * 5
    model = ModelTrainer(TrainingOptions(profile="de", max_length=12, alphabet_size=72)).train(
        lambda: iter(corpus)
    )

    floor = floor_chars("de")
    assert floor <= set(model.alphabet.chars)
    assert model.alphabet_size == len(floor) + 14
