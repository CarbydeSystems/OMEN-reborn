"""End-to-end CLI tests, including the real pipe / SIGPIPE contract.

These mirror how an external cracking orchestrator drives the generator: launch
``omen generate`` as a subprocess and consume its stdout through a pipe that may
close early.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample_passwords.txt"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from omen.cli import main

    out = tmp_path_factory.mktemp("model")
    rc = main(["train", "-i", str(SAMPLE), "-m", str(out), "--max-length", "16"])
    assert rc == 0
    assert (out / "config.json").is_file()
    return out


def test_generate_max_guesses_count(model_dir: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "omen", "generate", "-m", str(model_dir), "--max-guesses", "1234"],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0
    lines = proc.stdout.decode("utf-8").splitlines()
    assert len(lines) == 1234


def test_generate_pipe_into_head_is_clean(model_dir: Path) -> None:
    """`omen generate | head -5` yields 5 lines and no traceback (SIGPIPE clean)."""
    gen = f"{shlex.quote(sys.executable)} -m omen generate -m {shlex.quote(str(model_dir))}"
    pipeline = f"{gen} | head -n 5"
    proc = subprocess.run(
        ["bash", "-c", pipeline],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=120,
    )
    out_lines = proc.stdout.decode("utf-8").splitlines()
    assert len(out_lines) == 5
    stderr = proc.stderr.decode("utf-8")
    assert "Traceback" not in stderr
    assert "BrokenPipe" not in stderr


def test_eval_cli(model_dir: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "omen", "eval", "-m", str(model_dir), "password", "123456"],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0
    out = proc.stdout.decode("utf-8")
    assert "password\tlevel=" in out
    assert "123456\tlevel=" in out


def test_eval_rank_matches_with_and_without_native(
    model_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`eval --rank` must agree exactly whether or not native/omen-rank is used.

    Runs the same in-process call twice: once with `_find_omen_rank`
    monkeypatched to None (forces the pure-Python fallback), once unpatched
    (uses the native binary when native/omen-rank has been built, same as
    any normal invocation) — proving the native wiring is a speed change
    only, never a behaviour change.
    """
    import omen.cli as cli_module

    passwords = ["password", "123456", "doesnotexistinthissample12345"]
    argv = ["eval", "-m", str(model_dir), "--rank", *passwords]

    monkeypatch.setattr(cli_module, "_find_omen_rank", lambda: None)
    assert cli_module.main(argv) == 0
    fallback_out = capsys.readouterr().out
    monkeypatch.undo()

    assert cli_module.main(argv) == 0
    native_or_fallback_out = capsys.readouterr().out

    # Same rank>=N for every password regardless of which path computed it —
    # the whole point of the native tool being a drop-in, not an approximation.
    # (Equal even when native/omen-rank isn't built: both calls then take the
    # same fallback path, which is still a meaningful — if weaker — check.)
    assert fallback_out == native_or_fallback_out


def test_inspect_cli(model_dir: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "omen", "inspect", "-m", str(model_dir)],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0
    assert "OMEN model" in proc.stdout.decode("utf-8")


def test_train_with_profile_cli(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.txt"
    corpus.write_text("passwort\nschluessel\ngeheim\n" * 5, encoding="utf-8")
    out = tmp_path / "model"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omen",
            "train",
            "-i",
            str(corpus),
            "-m",
            str(out),
            "--max-length",
            "12",
            "--profile",
            "de",
        ],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0
    inspect_proc = subprocess.run(
        [sys.executable, "-m", "omen", "inspect", "-m", str(out)],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=60,
    )
    report = inspect_proc.stdout.decode("utf-8")
    assert "alphabet profile : de" in report
    # German floor letters must be present regardless of corpus frequency.
    assert "ß" in report and "ü" in report


def test_alphabet_cli_with_profile_preview() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omen",
            "alphabet",
            "-i",
            str(SAMPLE),
            "--profile",
            "es",
            "--size",
            "90",
        ],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0
    out = proc.stdout.decode("utf-8")
    assert "profile   : es" in out
    assert "ñ" in out  # Spanish floor letter, guaranteed regardless of corpus


def test_alphabet_cli_profile_prefers_same_script_remainder(tmp_path: Path) -> None:
    """End-to-end round-3 regression: with --profile, a far louder
    foreign-script character must not win the one remainder slot over a
    legitimate scriptless symbol — see omen/alphabet.py's `prefer`."""
    from omen.profiles import floor_chars

    corpus = tmp_path / "corpus.txt"
    floor_only = "".join(floor_chars("en"))
    corpus.write_text(f"{floor_only}\n{'?' * 3}\n{'の' * 50}\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omen",
            "alphabet",
            "-i",
            str(corpus),
            "--profile",
            "en",
            "--size",
            "63",
            "--min-symbol-slots",
            "1",
        ],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0
    out = proc.stdout.decode("utf-8")
    assert "?" in out.rsplit("alphabet  : ", 1)[1].splitlines()[0]
    assert "の" not in out


def test_alphabet_cli_no_profile_near_miss_names_the_excluded_symbol(tmp_path: Path) -> None:
    """End-to-end for the near-miss warning: with no --profile at all,
    there's no script-aware preference to prevent a foreign character from
    winning a slot over a sparser real symbol — the warning must name it."""
    corpus = tmp_path / "corpus.txt"
    corpus.write_text(f"{'abcdefghij' * 20}\n{'の' * 15}\n{'?' * 3}\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "omen", "alphabet", "-i", str(corpus), "--size", "11"],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0
    out = proc.stdout.decode("utf-8")
    assert "の" in out.rsplit("alphabet  : ", 1)[1].splitlines()[0]
    assert "looks mixed-script" in out
    assert "highest-ranked excluded character was '?' (count=3)" in out


def test_train_rejects_profile_and_alphabet_together(tmp_path: Path) -> None:
    out = tmp_path / "model"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omen",
            "train",
            "-i",
            str(SAMPLE),
            "-m",
            str(out),
            "--profile",
            "de",
            "--alphabet",
            "abc",
        ],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "mutually exclusive" in proc.stderr.decode("utf-8")


def test_train_supplement_influences_alphabet(tmp_path: Path) -> None:
    """A supplement file mixes into the corpus and affects auto-alphabet selection."""
    from omen.cli import main
    from omen.model import NgramModel

    # Base corpus uses only [a-c]; supplement introduces distinctive chars.
    base = tmp_path / "base.txt"
    base.write_text("\n".join(["abcabc"] * 50) + "\n", encoding="utf-8")
    supp = tmp_path / "supp.txt"
    supp.write_text("\n".join(["xyzxyz"] * 50) + "\n", encoding="utf-8")

    out = tmp_path / "model"
    rc = main(
        [
            "train",
            "-i",
            str(base),
            "-m",
            str(out),
            "--ngram",
            "2",
            "--alphabet-size",
            "6",
            "--supplement",
            str(supp),
            "--supplement-lines",
            "50",
        ]
    )
    assert rc == 0
    alphabet = NgramModel.load(out).alphabet.as_string()
    assert {"x", "y", "z"} <= set(alphabet), alphabet


def test_train_supplement_lines_caps(tmp_path: Path) -> None:
    """--supplement-lines bounds how much of the supplement is mixed in."""
    from omen.cli import main
    from omen.model import NgramModel

    base = tmp_path / "base.txt"
    base.write_text("\n".join(["abcabc"] * 50) + "\n", encoding="utf-8")
    supp = tmp_path / "supp.txt"
    supp.write_text("\n".join(["xyzxyz"] * 50) + "\n", encoding="utf-8")

    # Cap to 1 supplement line against 50 base lines, with room for only 3 chars:
    # the base characters dominate and the supplement's chars don't make the cut.
    out = tmp_path / "model"
    rc = main(
        [
            "train",
            "-i",
            str(base),
            "-m",
            str(out),
            "--ngram",
            "2",
            "--alphabet-size",
            "3",
            "--supplement",
            str(supp),
            "--supplement-lines",
            "1",
        ]
    )
    assert rc == 0
    alphabet = set(NgramModel.load(out).alphabet.as_string())
    assert alphabet == {"a", "b", "c"}, alphabet


def test_train_dual_stdin_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from omen.cli import main

    rc = main(["train", "-i", "-", "-m", str(tmp_path / "m"), "--supplement", "-"])
    assert rc == 2
    assert "only one of" in capsys.readouterr().err


def test_alphabet_cli() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "omen", "alphabet", "-i", str(SAMPLE), "--size", "20"],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0
    assert "coverage" in proc.stdout.decode("utf-8")


def test_alphabet_cli_warns_when_corpus_has_fewer_distinct_chars_than_requested() -> None:
    """The original incident this whole feature set traces back to: a small
    corpus asked for more alphabet slots than it has distinct characters."""
    proc = subprocess.run(
        [sys.executable, "-m", "omen", "alphabet", "-i", str(SAMPLE), "--size", "200"],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0
    out = proc.stdout.decode("utf-8")
    assert "warning" in out
    assert "requested 200" in out


def test_train_cli_warns_when_corpus_has_fewer_distinct_chars_than_requested(
    tmp_path: Path,
) -> None:
    out = tmp_path / "model"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omen",
            "train",
            "-i",
            str(SAMPLE),
            "-m",
            str(out),
            "--max-length",
            "16",
            "--alphabet-size",
            "200",
        ],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0
    stderr = proc.stderr.decode("utf-8")
    assert "requested 200" in stderr
