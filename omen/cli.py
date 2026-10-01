"""Command-line interface.

A thin dispatch layer: each subcommand parses its arguments, constructs the
relevant domain objects (:class:`ModelTrainer`, :class:`PyEnumerator`,
:class:`PasswordScorer`, :class:`ModelInspector`), and delegates.  No business
logic lives here.

Subcommands::

    omen train    -i CORPUS -m MODEL [--ngram N] [--levels NL] ...
    omen generate -m MODEL [--max-guesses N] [--max-level L] ...   # streams to stdout
    omen eval     -m MODEL [-i FILE | PW ...] [--rank]
    omen alphabet -i CORPUS [--size K]
    omen inspect  -m MODEL
    omen spool    --hashcat "hashcat -a 0 -m 1000 {chunk} h.txt" -- <producer argv...>
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from omen.alphabet import select_alphabet
from omen.enumerate import PyEnumerator
from omen.errors import OmenError
from omen.inspect import ModelInspector
from omen.io_utils import ByteSink, read_corpus
from omen.model import NgramModel
from omen.profiles import alphabet_warnings, available_profiles, floor_chars
from omen.score import PasswordScorer
from omen.spool import CHUNK_PLACEHOLDER, SpoolConfig, run_spool
from omen.train import ModelTrainer, TrainingOptions

CorpusFactory = Callable[[], Iterable[str]]


def main(argv: list[str] | None = None) -> int:
    """Program entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except BrokenPipeError:
        # Downstream consumer (e.g. hashcat) closed the pipe: a normal end.
        _silence_broken_pipe()
        return 0
    except OmenError as exc:
        print(f"omen: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


# -- parser ----------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omen",
        description="OMEN — ordered Markov password candidate generator.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_train = sub.add_parser("train", help="train a model from a password corpus")
    p_train.add_argument("-i", "--input", required=True, help="corpus path, or '-' for stdin")
    p_train.add_argument("-m", "--model", required=True, help="output model directory")
    p_train.add_argument("--ngram", type=int, default=3, help="n-gram order (default: 3)")
    p_train.add_argument("--levels", type=int, default=11, help="number of levels (default: 11)")
    p_train.add_argument(
        "--smoothing", type=float, default=0.01, help="add-δ smoothing (default: 0.01)"
    )
    p_train.add_argument(
        "--max-length", type=int, default=20, help="maximum password length (default: 20)"
    )
    p_train.add_argument(
        "--supplement",
        help="extra corpus to mix in (e.g. rockyou.txt); a file path or '-' for stdin",
    )
    p_train.add_argument(
        "--supplement-lines",
        type=int,
        default=500_000,
        help="use only the first N lines of --supplement (0 = all; default: 500000)",
    )
    alpha_group = p_train.add_mutually_exclusive_group()
    alpha_group.add_argument(
        "--alphabet", help="explicit alphabet string (overrides automatic selection)"
    )
    alpha_group.add_argument(
        "--alphabet-size",
        type=int,
        default=72,
        help="number of most-frequent chars to auto-select (default: 72)",
    )
    p_train.add_argument(
        "--profile",
        choices=available_profiles(),
        default=None,
        help="reserve a language's floor characters before frequency-ranking "
        "the remainder (mutually exclusive with --alphabet); see omen alphabet "
        "--profile to preview one",
    )
    p_train.add_argument(
        "--no-ep", action="store_true", help="disable the word-ending (EP) component"
    )
    p_train.set_defaults(handler=_cmd_train)

    p_gen = sub.add_parser("generate", help="stream ranked candidates to stdout")
    p_gen.add_argument("-m", "--model", required=True, help="model directory")
    p_gen.add_argument("--max-guesses", type=int, default=None, help="stop after N candidates")
    p_gen.add_argument("--max-level", type=int, default=None, help="stop after total level L")
    p_gen.add_argument("--min-length", type=int, default=None, help="minimum candidate length")
    p_gen.add_argument("--max-length", type=int, default=None, help="maximum candidate length")
    p_gen.set_defaults(handler=_cmd_generate)

    p_eval = sub.add_parser("eval", help="score passwords against a model")
    p_eval.add_argument("-m", "--model", required=True, help="model directory")
    p_eval.add_argument("-i", "--input", help="file of passwords, or '-' for stdin")
    p_eval.add_argument("passwords", nargs="*", help="passwords to score")
    p_eval.add_argument("--rank", action="store_true", help="also estimate guess rank")
    p_eval.add_argument(
        "--rank-cap", type=int, default=10_000_000, help="cap for rank estimation work"
    )
    p_eval.set_defaults(handler=_cmd_eval)

    p_alpha = sub.add_parser("alphabet", help="select and report an alphabet from a corpus")
    p_alpha.add_argument("-i", "--input", required=True, help="corpus path, or '-' for stdin")
    p_alpha.add_argument("--size", type=int, default=72, help="alphabet size (default: 72)")
    p_alpha.add_argument(
        "--profile",
        choices=available_profiles(),
        default=None,
        help="preview with a language's floor characters reserved first",
    )
    p_alpha.set_defaults(handler=_cmd_alphabet)

    p_inspect = sub.add_parser("inspect", help="print model metadata and histograms")
    p_inspect.add_argument("-m", "--model", required=True, help="model directory")
    p_inspect.set_defaults(handler=_cmd_inspect)

    p_spool = sub.add_parser(
        "spool",
        help="chunk a producer's output into tmpfs files and attack each with hashcat",
    )
    p_spool.add_argument(
        "--hashcat",
        required=True,
        help=f"hashcat command template containing {CHUNK_PLACEHOLDER!r} (shlex-split)",
    )
    p_spool.add_argument(
        "--chunk-mb", type=int, default=512, help="chunk size in MiB (default: 512)"
    )
    p_spool.add_argument(
        "--chunk-lines", type=int, default=0, help="optional per-chunk line cap (0 = none)"
    )
    p_spool.add_argument("--max-guesses", type=int, default=0, help="stop after N candidates")
    p_spool.add_argument(
        "--spool-dir", default=None, help="chunk directory (default: /dev/shm, else temp dir)"
    )
    p_spool.add_argument(
        "producer",
        nargs=argparse.REMAINDER,
        help="producer command after '--' (streams candidates to stdout)",
    )
    p_spool.set_defaults(handler=_cmd_spool)

    return parser


# -- command handlers ------------------------------------------------------


def _cmd_train(args: argparse.Namespace) -> int:
    options = TrainingOptions(
        ngram=args.ngram,
        levels=args.levels,
        smoothing=args.smoothing,
        max_length=args.max_length,
        alphabet=args.alphabet,
        alphabet_size=args.alphabet_size,
        profile=args.profile,
        ep_enabled=not args.no_ep,
    )
    if args.supplement and args.input == "-" and args.supplement == "-":
        raise OmenError("only one of -i/--supplement can read from stdin")
    factory = _corpus_factory(args.input)
    if args.supplement:
        factory = _supplemented_factory(factory, args.supplement, args.supplement_lines)
    model = ModelTrainer(options).train(factory)
    model.save(args.model)
    print(
        f"omen: trained model saved to {args.model} "
        f"(alphabet={model.alphabet_size}, coverage={model.coverage:.1%}, "
        f"lam={model.scale.lam:.4g})",
        file=sys.stderr,
    )
    if model.alphabet_size < args.alphabet_size and args.alphabet is None:
        print(
            f"omen: warning: alphabet has only {model.alphabet_size} characters "
            f"(requested {args.alphabet_size}) — the corpus doesn't contain that "
            "many distinct characters",
            file=sys.stderr,
        )
    for warning in alphabet_warnings(model.alphabet.chars, model.profile):
        print(f"omen: warning: {warning}", file=sys.stderr)
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    model = NgramModel.load(args.model)
    enumerator = PyEnumerator(model)
    with ByteSink(sys.stdout.buffer) as sink:
        enumerator.stream(
            sink,
            max_guesses=args.max_guesses,
            max_level=args.max_level,
            min_length=args.min_length,
            max_length=args.max_length,
        )
    return 0


def _find_omen_rank() -> Path | None:
    """Locate the native rank estimator: next to this repo's native/ dir first
    (the usual case — built via `make -C native omen-rank`), else on PATH."""
    candidate = Path(__file__).resolve().parent.parent / "native" / "omen-rank"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    found = shutil.which("omen-rank")
    return Path(found) if found else None


def _native_ranks(model_dir: str, passwords: list[str], cap: int) -> list[int | None] | None:
    """Estimate every password's rank in one native/omen-rank call, in order.

    Returns ``None`` (never partially) when the native binary is unavailable,
    a password contains a newline (would desync the line-per-password
    framing), or the subprocess itself fails — the caller falls back to the
    pure-Python :meth:`PasswordScorer.estimate_rank` for every password in
    that case, so this is a pure speed optimisation with no behaviour change
    on the fallback path. A ``None`` entry within the returned list means
    that password was UNREACHABLE (the caller already knows this from
    :meth:`PasswordScorer.score` and never consults the entry).
    """
    binary = _find_omen_rank()
    if binary is None or any("\n" in pw or "\r" in pw for pw in passwords):
        return None
    try:
        proc = subprocess.run(
            [str(binary), model_dir, "--rank-cap", str(cap), "-i", "-"],
            input="\n".join(passwords) + "\n",
            capture_output=True,
            text=True,
            timeout=600,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.splitlines()
    if len(lines) != len(passwords):
        return None  # defensive: misaligned output, don't guess which is which
    ranks: list[int | None] = []
    for line in lines:
        if "\trank>=" in line:
            ranks.append(int(line.rsplit("rank>=", 1)[1]))
        else:
            ranks.append(None)
    return ranks


def _cmd_eval(args: argparse.Namespace) -> int:
    model = NgramModel.load(args.model)
    scorer = PasswordScorer(model)
    passwords = _eval_inputs(args)
    native_ranks = _native_ranks(args.model, passwords, args.rank_cap) if args.rank else None
    for i, pw in enumerate(passwords):
        result = scorer.score(pw)
        if not result.in_model:
            print(f"{pw}\tUNREACHABLE\t({result.reason})")
            continue
        line = (
            f"{pw}\tlevel={result.total_level}\tlen={result.length}\t"
            f"ip={result.ip_level} cp={result.cp_sum} ep={result.ep_level} ln={result.ln_level}"
        )
        if args.rank:
            if native_ranks is not None and native_ranks[i] is not None:
                rank = native_ranks[i]
            else:
                rank = scorer.estimate_rank(pw, cap=args.rank_cap)
            line += f"\trank>={rank}"
        print(line)
    return 0


def _cmd_alphabet(args: argparse.Namespace) -> int:
    floor = floor_chars(args.profile) if args.profile else ()
    with read_corpus(args.input) as passwords:
        selection = select_alphabet(passwords, args.size, floor=floor)
    print(f"size      : {selection.alphabet.size}")
    print(f"coverage  : {selection.coverage:.4%}")
    print(f"distinct  : {selection.distinct_chars}")
    print(f"total     : {selection.total_chars}")
    print(f"profile   : {args.profile if args.profile else '(none — pure frequency)'}")
    print(f"alphabet  : {selection.alphabet.as_string()}")
    if selection.alphabet.size < args.size:
        print(
            f"warning   : alphabet has only {selection.alphabet.size} characters "
            f"(requested {args.size}) — the corpus doesn't contain that many "
            "distinct characters"
        )
    for warning in alphabet_warnings(selection.alphabet.chars, args.profile):
        print(f"warning   : {warning}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    model = NgramModel.load(args.model)
    print(ModelInspector(model).report())
    return 0


def _cmd_spool(args: argparse.Namespace) -> int:
    producer = list(args.producer)
    if producer and producer[0] == "--":
        producer = producer[1:]
    if not producer:
        raise OmenError("provide the producer command after '--'")
    template = shlex.split(args.hashcat)
    if CHUNK_PLACEHOLDER not in template:
        raise OmenError(f"--hashcat template must contain {CHUNK_PLACEHOLDER!r}")
    config = SpoolConfig(
        chunk_bytes=args.chunk_mb << 20,
        chunk_lines=args.chunk_lines,
        max_guesses=args.max_guesses,
        spool_dir=args.spool_dir,
    )
    result = run_spool(producer, template, config)
    print(
        f"omen: spool finished — status={result.status} "
        f"chunks={result.chunks} candidates={result.candidates:,}",
        file=sys.stderr,
    )
    return 0


# -- helpers ---------------------------------------------------------------


def _corpus_factory(source: str) -> CorpusFactory:
    """Return a factory yielding a fresh password iterator on each call.

    Training needs up to two passes (alphabet selection, then counting).  For a
    file we re-open on each pass; for stdin (single-shot) we materialise once.
    """
    if source == "-":
        with read_corpus(source) as passwords:
            buffered = list(passwords)
        return lambda: iter(buffered)

    path = Path(source)

    def factory() -> Iterator[str]:
        with read_corpus(path) as passwords:
            yield from passwords

    return factory


def _supplemented_factory(base: CorpusFactory, supplement: str, max_lines: int) -> CorpusFactory:
    """Wrap a corpus factory so each pass also yields the supplement.

    The supplement is capped to its first ``max_lines`` passwords (0 = all).
    Like :func:`_corpus_factory`, the returned factory must produce a *fresh*
    iterator on every call (training passes over the corpus up to twice).
    """
    supp = _corpus_factory(supplement)  # reuses stdin-materialisation logic

    def factory() -> Iterator[str]:
        yield from base()
        for count, pw in enumerate(supp()):
            if max_lines and count >= max_lines:
                break
            yield pw

    return factory


def _eval_inputs(args: argparse.Namespace) -> list[str]:
    if args.input:
        with read_corpus(args.input) as passwords:
            return list(passwords)
    positional: list[str] = list(args.passwords)
    if positional:
        return positional
    raise OmenError("provide passwords as arguments or via -i/--input")


def _silence_broken_pipe() -> None:
    """Redirect stdout to /dev/null so interpreter shutdown won't re-raise.

    Without this, Python flushes stdout at exit, hits the broken pipe again, and
    prints a noisy 'Exception ignored' message.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except OSError:
        pass
