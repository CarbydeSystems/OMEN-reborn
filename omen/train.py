"""Training: turn a password corpus into an :class:`~omen.model.NgramModel`.

:class:`ModelTrainer` has a single responsibility — count n-grams, smooth the
counts into probabilities, derive one shared :class:`~omen.levels.LevelScale`
from the global probability floor, and discretise the four level tables.  The
resulting :class:`NgramModel` is a clean data + query object with no training
state attached.

Probability model (add-δ smoothing, alphabet size ``A``):

* IP:  ``P(g)        = (ip[g] + δ) / (N + δ·M)``           over ``M = A^(n-1)`` contexts
* CP:  ``P(c | ctx)  = (cnt + δ) / (cont(ctx) + δ·A)``
* EP:  ``P(end | ctx)= (end + δ) / (occ(ctx) + 2δ)``       (binary: end vs. continue)
* LN:  ``P(L)        = (len[L] + δ) / (N + δ·R)``           over ``R`` length bins

where ``N`` is the number of training passwords used, ``cont(ctx)`` the number
of continuations observed from a context, and ``occ(ctx) = cont(ctx) + end(ctx)``.
"""

from __future__ import annotations

import functools
import math
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from omen.alphabet import (
    DEFAULT_MIN_SYMBOL_SLOTS,
    Alphabet,
    AlphabetSelection,
    effective_alphabet_size,
    select_alphabet,
)
from omen.errors import TrainingError
from omen.levels import LevelScale
from omen.model import MAX_NGRAM, MAX_PASSWORD_LENGTH, NgramModel, checked_pow
from omen.profiles import PROFILES, available_profiles, floor_chars, is_expected_script

CorpusFactory = Callable[[], Iterable[str]]

# Below this many representable passwords, a percentile is noise, not
# signal — too few samples to resolve a tail point like p99.5 meaningfully,
# so _resolve_max_length falls back to the mean+std estimate alone.
MIN_SAMPLES_FOR_PERCENTILE = 30

# Defaults for the two auto-max_length estimates (see TrainingOptions) —
# named so cli.py's argparse defaults can reference the same numbers
# instead of duplicating them.
DEFAULT_MAX_LENGTH_STD = 1.5
DEFAULT_MAX_LENGTH_PERCENTILE = 0.995


@dataclass(frozen=True, slots=True)
class TrainingOptions:
    """User-facing knobs for training.

    ``alphabet`` overrides automatic selection entirely; when ``None``, up to
    ``alphabet_size`` characters are chosen from the corpus by frequency,
    reserving a floor for ``profile`` first (see :mod:`omen.profiles`) when
    one is given — ``profile`` and ``alphabet`` are mutually exclusive, since
    an explicit alphabet already fully determines the charset. A profile's
    floor is also guaranteed at least ``min_symbol_slots`` of headroom beyond
    it (see :func:`~omen.alphabet.effective_alphabet_size`), raising the
    effective alphabet size above ``alphabet_size`` rather than letting the
    floor crowd out symbols and punctuation.

    ``max_length`` of ``None`` (the default) computes it from the corpus
    itself at train time, as the larger of two estimates over the lengths of
    passwords the resolved alphabet can represent:

    * ``ceil(mean + max_length_std * stdev)`` — a tight, low-variance corpus
      (small stdev) gets a tight cutoff.
    * the ``max_length_percentile`` length (e.g. 0.995 = the length covering
      99.5% of the corpus) — a long-but-thin tail still gets covered even
      when it isn't far in *stdev* terms, which the mean+std estimate alone
      can miss on a corpus whose lengths cluster very tightly. Skipped on
      fewer than :data:`MIN_SAMPLES_FOR_PERCENTILE` passwords, where a
      percentile would just be reporting sampling noise.

    Both are clamped to ``[ngram - 1, MAX_PASSWORD_LENGTH]``. Pass an
    explicit ``max_length`` to restore the old fixed-cutoff behaviour.
    """

    ngram: int = 3
    levels: int = 11
    smoothing: float = 0.01
    max_length: int | None = None
    max_length_std: float = DEFAULT_MAX_LENGTH_STD
    max_length_percentile: float = DEFAULT_MAX_LENGTH_PERCENTILE
    alphabet: str | None = None
    alphabet_size: int = 72
    min_symbol_slots: int = DEFAULT_MIN_SYMBOL_SLOTS
    profile: str | None = None
    ep_enabled: bool = True

    def validate(self) -> None:
        if not 2 <= self.ngram <= MAX_NGRAM:
            raise TrainingError(f"ngram must be in [2, {MAX_NGRAM}], got {self.ngram}")
        if not 2 <= self.levels <= 256:
            raise TrainingError(f"levels must be in [2, 256], got {self.levels}")
        if not self.smoothing > 0.0:
            raise TrainingError(f"smoothing (δ) must be > 0, got {self.smoothing}")
        if self.max_length is not None and not (
            self.ngram - 1 <= self.max_length <= MAX_PASSWORD_LENGTH
        ):
            raise TrainingError(
                f"max_length must be in [{self.ngram - 1}, {MAX_PASSWORD_LENGTH}], "
                f"got {self.max_length}"
            )
        if not self.max_length_std > 0.0:
            raise TrainingError(f"max_length_std must be > 0, got {self.max_length_std}")
        if not 0.0 < self.max_length_percentile <= 1.0:
            raise TrainingError(
                f"max_length_percentile must be in (0, 1], got {self.max_length_percentile}"
            )
        if self.alphabet_size < 1:
            raise TrainingError(f"alphabet_size must be >= 1, got {self.alphabet_size}")
        if self.min_symbol_slots < 0:
            raise TrainingError(f"min_symbol_slots must be >= 0, got {self.min_symbol_slots}")
        if self.profile is not None:
            if self.alphabet is not None:
                raise TrainingError("profile and alphabet are mutually exclusive")
            if self.profile not in PROFILES:
                raise TrainingError(
                    f"unknown profile {self.profile!r}; available: "
                    f"{', '.join(available_profiles())}"
                )


class ModelTrainer:
    """Builds an :class:`NgramModel` from a corpus according to options."""

    def __init__(self, options: TrainingOptions) -> None:
        options.validate()
        self._opt = options
        self.alphabet_selection: AlphabetSelection | None = None
        """Set by :meth:`train` when the alphabet was auto-selected (i.e.
        ``options.alphabet`` was ``None``) — ``None`` for an explicit
        alphabet, which has no frequency ranking to report. Exposes the
        full :class:`~omen.alphabet.AlphabetSelection`, including
        ``excluded``, for callers that want more than the trained
        :class:`NgramModel` keeps (e.g. :func:`~omen.profiles.alphabet_warnings`'s
        near-miss reporting)."""

    def train(self, corpus_factory: CorpusFactory) -> NgramModel:
        """Train a model.

        ``corpus_factory`` returns a fresh iterator of passwords each time it is
        called.  It is called once for alphabet selection (only when the alphabet
        is auto), once more for length statistics (only when ``max_length`` is
        auto), and once for counting — so a one-shot iterator is never reused.
        """
        alphabet = self._resolve_alphabet(corpus_factory)
        max_length = self._resolve_max_length(alphabet, corpus_factory)
        self._opt = replace(self._opt, max_length=max_length)
        counts = _Counts(alphabet, self._opt.ngram, self._opt.max_length)
        counts.tally(corpus_factory())
        if counts.num_passwords == 0:
            raise TrainingError(
                "no usable passwords in corpus (all empty, too long/short, "
                "or contain out-of-alphabet characters)"
            )
        return self._build_model(alphabet, counts)

    # -- internals ---------------------------------------------------------

    def _resolve_alphabet(self, corpus_factory: CorpusFactory) -> Alphabet:
        if self._opt.alphabet is not None:
            return Alphabet.from_chars(self._opt.alphabet)
        floor = floor_chars(self._opt.profile) if self._opt.profile else ()
        prefer = (
            functools.partial(is_expected_script, profile=self._opt.profile)
            if self._opt.profile is not None
            else None
        )
        size = effective_alphabet_size(
            self._opt.alphabet_size, floor, min_symbol_slots=self._opt.min_symbol_slots
        )
        self.alphabet_selection = select_alphabet(
            corpus_factory(), size, floor=floor, prefer=prefer
        )
        return self.alphabet_selection.alphabet

    def _resolve_max_length(self, alphabet: Alphabet, corpus_factory: CorpusFactory) -> int:
        """Return the configured ``max_length``, or compute it from the corpus.

        Computed over the lengths of passwords the alphabet can represent —
        same ``encode()`` test :class:`_Counts` uses, though without its
        ``ctx_len``/``max_length`` bounds filtering, which would be circular
        here — as the larger of a mean+stdev estimate and a percentile
        estimate (see :class:`TrainingOptions` for why both), then clamped
        to stay trainable. ``statistics.pstdev`` (population, not sample) is
        deliberate: this is a statistic over *this* corpus, not an inference
        about a wider one.
        """
        if self._opt.max_length is not None:
            return self._opt.max_length
        ctx_len = self._opt.ngram - 1
        encode = alphabet.encode
        lengths = [len(codes) for pw in corpus_factory() if (codes := encode(pw)) is not None]
        if not lengths:
            raise TrainingError(
                "no usable passwords in corpus (all empty, too long/short, "
                "or contain out-of-alphabet characters)"
            )
        mean = statistics.fmean(lengths)
        stdev = statistics.pstdev(lengths, mean)
        raw = mean + self._opt.max_length_std * stdev
        if len(lengths) >= MIN_SAMPLES_FOR_PERCENTILE:
            raw = max(raw, self._percentile_length(lengths))
        return max(ctx_len, min(MAX_PASSWORD_LENGTH, math.ceil(raw)))

    def _percentile_length(self, lengths: list[int]) -> int:
        """The length at or below which ``max_length_percentile`` of ``lengths`` fall."""
        ordered = sorted(lengths)
        idx = min(len(ordered) - 1, int(self._opt.max_length_percentile * len(ordered)))
        return ordered[idx]

    def _build_model(self, alphabet: Alphabet, counts: _Counts) -> NgramModel:
        opt = self._opt
        a = alphabet.size
        ctx_len = opt.ngram - 1
        num_contexts = checked_pow(a, ctx_len, "ip/ep table")
        length_bins = opt.max_length - ctx_len + 1

        floor = self._probability_floor(counts, a, num_contexts, length_bins)
        scale = LevelScale.from_floor(opt.levels, floor)

        ip_levels = self._discretise_ip(counts, scale, num_contexts)
        cp_levels = self._discretise_cp(counts, scale, a)
        ep_levels, ep_enabled = self._discretise_ep(counts, scale, num_contexts)
        ln_levels = self._discretise_ln(counts, scale, length_bins)

        return NgramModel(
            alphabet=alphabet,
            scale=scale,
            ngram=opt.ngram,
            smoothing=opt.smoothing,
            max_length=opt.max_length,
            ip_levels=ip_levels,
            cp_levels=cp_levels,
            ep_levels=ep_levels,
            ep_enabled=ep_enabled,
            ln_levels=ln_levels,
            coverage=counts.coverage,
            profile=opt.profile,
        )

    def _probability_floor(
        self, counts: _Counts, a: int, num_contexts: int, length_bins: int
    ) -> float:
        """Smallest probability any table will discretise (maps to worst level)."""
        d = self._opt.smoothing
        candidates: list[float] = []

        # CP: smallest is δ / (max continuation total + δ·A); plus uniform 1/A
        # for any context with no observed continuations.
        if counts.cp_counts:
            max_cont = max(sum(codes.values()) for codes in counts.cp_counts.values())
            candidates.append(d / (max_cont + d * a))
            if len(counts.cp_counts) < num_contexts:
                candidates.append(1.0 / a)

        # IP: smallest numerator over all M contexts (δ if any context is unseen).
        ip_denom = counts.num_passwords + d * num_contexts
        ip_seen_all = len(counts.ip_counts) == num_contexts
        ip_num = (min(counts.ip_counts.values()) + d) if ip_seen_all else d
        candidates.append(ip_num / ip_denom)

        # EP (only if enabled).
        if self._opt.ep_enabled:
            candidates.append(self._ep_floor(counts, num_contexts))

        # LN: smallest numerator over all length bins (δ if any bin is unseen).
        ln_denom = counts.num_passwords + d * length_bins
        ln_seen_all = len(counts.len_counts) == length_bins
        ln_num = (min(counts.len_counts.values()) + d) if ln_seen_all else d
        candidates.append(ln_num / ln_denom)

        floor = min(candidates)
        # Guard against pathological degeneracy; keep strictly inside (0, 1).
        return min(max(floor, 1e-12), 1.0 - 1e-12)

    def _ep_floor(self, counts: _Counts, num_contexts: int) -> float:
        d = self._opt.smoothing
        best = 0.5 if len(counts.occ_contexts()) < num_contexts else 1.0
        for ctx in counts.occ_contexts():
            occ = counts.occurrences(ctx)
            end = counts.end_counts.get(ctx, 0)
            p = (end + d) / (occ + 2 * d)
            best = min(best, p)
        return best

    def _discretise_ip(self, counts: _Counts, scale: LevelScale, num_contexts: int) -> bytes:
        d = self._opt.smoothing
        denom = counts.num_passwords + d * num_contexts
        table = bytearray(num_contexts)
        unseen_level = scale.to_level(d / denom)
        if unseen_level:
            for i in range(num_contexts):
                table[i] = unseen_level
        for ctx, cnt in counts.ip_counts.items():
            table[ctx] = scale.to_level((cnt + d) / denom)
        return bytes(table)

    def _discretise_cp(self, counts: _Counts, scale: LevelScale, a: int) -> bytes:
        d = self._opt.smoothing
        size = checked_pow(a, self._opt.ngram, "cp table")
        table = bytearray(size)
        # Default: a context with no observed continuations is uniform (1/A).
        uniform_level = scale.to_level(1.0 / a)
        if uniform_level:
            for i in range(size):
                table[i] = uniform_level
        for ctx, code_counts in counts.cp_counts.items():
            cont_total = sum(code_counts.values())
            denom = cont_total + d * a
            base = ctx * a
            for code in range(a):
                cnt = code_counts.get(code, 0)
                table[base + code] = scale.to_level((cnt + d) / denom)
        return bytes(table)

    def _discretise_ep(
        self, counts: _Counts, scale: LevelScale, num_contexts: int
    ) -> tuple[bytes, bool]:
        if not self._opt.ep_enabled:
            return bytes(num_contexts), False
        d = self._opt.smoothing
        table = bytearray(num_contexts)
        # Default for an unseen context: no evidence, end-probability 0.5.
        default_level = scale.to_level(0.5)
        if default_level:
            for i in range(num_contexts):
                table[i] = default_level
        for ctx in counts.occ_contexts():
            occ = counts.occurrences(ctx)
            end = counts.end_counts.get(ctx, 0)
            table[ctx] = scale.to_level((end + d) / (occ + 2 * d))
        return bytes(table), True

    def _discretise_ln(self, counts: _Counts, scale: LevelScale, length_bins: int) -> list[int]:
        d = self._opt.smoothing
        ctx_len = self._opt.ngram - 1
        max_length = self._opt.max_length
        denom = counts.num_passwords + d * length_bins
        levels = [scale.max_level] * (max_length + 1)
        for length in range(ctx_len, max_length + 1):
            cnt = counts.len_counts.get(length, 0)
            levels[length] = scale.to_level((cnt + d) / denom)
        return levels


class _Counts:
    """Mutable accumulator of raw n-gram counts during a single corpus pass."""

    __slots__ = (
        "_alphabet",
        "_ctx_len",
        "_drop_mod",
        "_max_length",
        "_size",
        "cp_counts",
        "end_counts",
        "ip_counts",
        "len_counts",
        "num_passwords",
        "retained_chars",
        "total_chars",
    )

    def __init__(self, alphabet: Alphabet, ngram: int, max_length: int) -> None:
        self._alphabet = alphabet
        self._size = alphabet.size
        self._ctx_len = ngram - 1
        self._max_length = max_length
        self._drop_mod = self._size ** (self._ctx_len - 1)
        self.ip_counts: dict[int, int] = {}
        self.cp_counts: dict[int, dict[int, int]] = {}
        self.end_counts: dict[int, int] = {}
        self.len_counts: dict[int, int] = {}
        self.num_passwords = 0
        self.total_chars = 0
        self.retained_chars = 0

    def tally(self, passwords: Iterable[str]) -> None:
        contains = self._alphabet.contains
        encode = self._alphabet.encode
        ctx_len = self._ctx_len
        for pw in passwords:
            # A literal U+FFFD is a decode artifact, not a real password
            # character (see select_alphabet) — excluded before coverage
            # accounting too, same as read_corpus already excludes it on the
            # CLI's own ingestion path.
            if "�" in pw:
                continue
            # Coverage is measured over every character, before any filtering.
            self.total_chars += len(pw)
            self.retained_chars += sum(1 for ch in pw if contains(ch))

            codes = encode(pw)
            if codes is None:
                continue
            length = len(codes)
            if length < ctx_len or length > self._max_length:
                continue
            self._count_codes(codes)

    def _count_codes(self, codes: list[int]) -> None:
        ctx_len = self._ctx_len
        length = len(codes)
        self.len_counts[length] = self.len_counts.get(length, 0) + 1

        ctx = self._context_index(codes[:ctx_len])
        self.ip_counts[ctx] = self.ip_counts.get(ctx, 0) + 1
        for i in range(ctx_len, length):
            code = codes[i]
            bucket = self.cp_counts.get(ctx)
            if bucket is None:
                bucket = {}
                self.cp_counts[ctx] = bucket
            bucket[code] = bucket.get(code, 0) + 1
            ctx = (ctx % self._drop_mod) * self._size + code
        self.end_counts[ctx] = self.end_counts.get(ctx, 0) + 1
        self.num_passwords += 1

    def _context_index(self, codes: list[int]) -> int:
        idx = 0
        for code in codes:
            idx = idx * self._size + code
        return idx

    def occ_contexts(self) -> set[int]:
        """Contexts that occurred at least once (as a continuation or an end)."""
        return set(self.cp_counts) | set(self.end_counts)

    def occurrences(self, ctx: int) -> int:
        """Total occurrences of ``ctx`` = continuations + ends."""
        cont = sum(self.cp_counts[ctx].values()) if ctx in self.cp_counts else 0
        return cont + self.end_counts.get(ctx, 0)

    @property
    def coverage(self) -> float:
        if self.total_chars == 0:
            return 0.0
        return self.retained_chars / self.total_chars
