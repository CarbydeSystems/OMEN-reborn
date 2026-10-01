"""Parity between the native C rank estimator and the Python reference.

`native/omen-rank` must agree byte-for-byte with `omen.score.PasswordScorer`
(`score()` + `estimate_rank()`), formatted exactly as `omen eval --rank` would
print it — so the two are interchangeable, just orders of magnitude faster.
Skipped when the binary has not been built (keeps CI green without a
compiler); build it with ``make -C native omen-rank``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omen.model import NgramModel
from omen.score import PasswordScorer

REPO_ROOT = Path(__file__).resolve().parent.parent
OMEN_RANK = REPO_ROOT / "native" / "omen-rank"

pytestmark = pytest.mark.skipif(
    not OMEN_RANK.is_file(), reason="native/omen-rank not built (run `make -C native omen-rank`)"
)


def _expected_line(scorer: PasswordScorer, pw: str, cap: int) -> str:
    """The line `omen eval --rank` would print for `pw` (mirrors cli.py's _cmd_eval)."""
    result = scorer.score(pw)
    if not result.in_model:
        return f"{pw}\tUNREACHABLE\t({result.reason})"
    rank = scorer.estimate_rank(pw, cap=cap)
    return (
        f"{pw}\tlevel={result.total_level}\tlen={result.length}\t"
        f"ip={result.ip_level} cp={result.cp_sum} ep={result.ep_level} ln={result.ln_level}"
        f"\trank>={rank}"
    )


def _c_lines(model_dir: Path, passwords: list[str], cap: int) -> list[str]:
    proc = subprocess.run(
        [str(OMEN_RANK), str(model_dir), "--rank-cap", str(cap), *passwords],
        capture_output=True,
        timeout=120,
        check=True,
        encoding="utf-8",
    )
    return proc.stdout.splitlines()


def test_rank_parity_real_passwords(trained_model: NgramModel, tmp_path: Path) -> None:
    """A spread of real sample passwords — in-model, varying level."""
    trained_model.save(tmp_path)
    scorer = PasswordScorer(trained_model)
    cap = 2_000_000
    passwords = ["password", "password123", "qwerty123", "welcome1", "monkey"]
    expected = [_expected_line(scorer, pw, cap) for pw in passwords]
    assert _c_lines(tmp_path, passwords, cap) == expected


def test_rank_parity_out_of_alphabet(trained_model: NgramModel, tmp_path: Path) -> None:
    """A character the trained alphabet never saw (UNREACHABLE, exact reason text)."""
    trained_model.save(tmp_path)
    scorer = PasswordScorer(trained_model)
    cap = 2_000_000
    # U+1F389 PARTY POPPER — not a character any plausible 72-entry password
    # alphabet selects, so this reliably exercises the out-of-alphabet path.
    passwords = ["party\U0001f389time"]
    expected = [_expected_line(scorer, pw, cap) for pw in passwords]
    assert all("UNREACHABLE" in e for e in expected)  # sanity: the fixture still assumes this
    assert _c_lines(tmp_path, passwords, cap) == expected


def test_rank_parity_length_outside_range(trained_model: NgramModel, tmp_path: Path) -> None:
    """Too short (below ctx_len) and too long (above max_length), exact reason text."""
    trained_model.save(tmp_path)
    scorer = PasswordScorer(trained_model)
    cap = 2_000_000
    # trained_model fixture: ngram=3 (ctx_len=2) / max_length=16 (conftest.py).
    passwords = ["a", "a" * 20]
    expected = [_expected_line(scorer, pw, cap) for pw in passwords]
    assert all("UNREACHABLE" in e and "outside [" in e for e in expected)
    assert _c_lines(tmp_path, passwords, cap) == expected


def test_rank_parity_rank_zero() -> None:
    """A tiny, fully-enumerable model gives us a guaranteed rank>=0 password:
    the enumerator's first-emitted candidate is, by construction, at the
    model's *minimum* level — so nothing precedes it and its rank is 0,
    whether that minimum level is itself 0 (the `score == 0` fast path in
    estimate_rank) or not (the general counting path legitimately counting
    zero). Either way this is the one real invariant worth pinning down
    directly, rather than hoping the real-sample fixture happens to produce
    a rank-0 case.
    """
    import tempfile

    from omen.enumerate import PyEnumerator
    from omen.train import ModelTrainer, TrainingOptions

    model = ModelTrainer(TrainingOptions(ngram=2, alphabet="ab", levels=8, max_length=3)).train(
        lambda: iter(["a", "b", "ab", "ba", "aab", "bba", "abb"])
    )
    scorer = PasswordScorer(model)

    class _First:
        item: bytes | None = None

        def write_line_bytes(self, raw: bytes) -> None:
            if self.item is None:
                self.item = raw

    sink = _First()
    PyEnumerator(model).stream(sink, max_guesses=1)
    assert sink.item is not None
    first_pw = sink.item.decode("utf-8")
    assert scorer.estimate_rank(first_pw, cap=2_000_000) == 0

    with tempfile.TemporaryDirectory() as d:
        model.save(d)
        cap = 2_000_000
        expected = [_expected_line(scorer, first_pw, cap)]
        assert expected[0].endswith("rank>=0")
        assert _c_lines(Path(d), [first_pw], cap) == expected
