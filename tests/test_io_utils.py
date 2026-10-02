"""Tests for corpus reading, in particular U+FFFD (replacement character)
corpus hygiene: a line that fails to decode cleanly — or already contains a
literal replacement character — must never reach the alphabet/n-gram counters,
since it's a decode artifact, not a real password character."""

from __future__ import annotations

from pathlib import Path

from omen.io_utils import read_corpus


def test_invalid_utf8_byte_sequence_drops_the_whole_line(tmp_path: Path) -> None:
    """A line with a byte sequence invalid in UTF-8 decodes (via errors=\"replace\")
    to contain U+FFFD, and must be dropped rather than yielded with a placeholder."""
    path = tmp_path / "corpus.txt"
    path.write_bytes(b"good1\nbad\xff\xfeline\ngood2\n")
    with read_corpus(path) as passwords:
        lines = list(passwords)
    assert lines == ["good1", "good2"]


def test_literal_replacement_character_is_also_dropped(tmp_path: Path) -> None:
    """Even a line that decodes cleanly is dropped if it already contains a
    literal U+FFFD — e.g. piped from another tool that already replaced
    invalid bytes upstream."""
    path = tmp_path / "corpus.txt"
    path.write_text("good1\npass�word\ngood2\n", encoding="utf-8")
    with read_corpus(path) as passwords:
        lines = list(passwords)
    assert lines == ["good1", "good2"]


def test_clean_corpus_is_unaffected(tmp_path: Path) -> None:
    path = tmp_path / "corpus.txt"
    path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    with read_corpus(path) as passwords:
        lines = list(passwords)
    assert lines == ["alpha", "beta", "gamma"]


def test_replacement_character_never_wins_an_alphabet_slot() -> None:
    """Regression for the real incident: U+FFFD repeated often enough to
    out-frequency every real character must still never be selected — this
    is the select_alphabet-level half of the fix (defense in depth alongside
    read_corpus, for callers that build a corpus_factory directly)."""
    from omen.train import ModelTrainer, TrainingOptions

    corpus = ["abc"] * 5 + ["�"] * 50
    model = ModelTrainer(TrainingOptions(ngram=2, alphabet_size=4, max_length=5)).train(
        lambda: iter(corpus)
    )
    assert "�" not in model.alphabet.chars
    assert set(model.alphabet.chars) == {"a", "b", "c"}
