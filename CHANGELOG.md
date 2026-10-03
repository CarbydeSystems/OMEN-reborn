# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.0] - 2026-10-03

### Added

- Script-aware remainder selection. With `--profile` set, `select_alphabet`
  now fills the non-floor alphabet slots from same-script-or-scriptless
  characters first. It only draws from foreign-script characters if that
  pool runs out. Without this, a large foreign-language corpus sample
  could still win a remainder slot over a legitimate same-script symbol —
  the same failure mode the floor (1.1.0) already prevents for letters,
  just one layer further out. Found live: `?` lost to two Japanese
  punctuation marks in a corpus where the floor fix had already recovered
  `Q`/`J`/`X`.
- Near-miss reporting. `alphabet_warnings()` now also names the
  highest-ranked same-script character that was excluded, whenever a
  foreign-script character still made the alphabet. Available from
  `train` and `alphabet`, which have the corpus's character frequencies
  to hand. Not available from `inspect`, since a saved model doesn't keep
  them.

### Changed

- README rewritten in Simplified Technical English (STE100), end to end,
  for clarity.

## [1.2.0] - 2026-10-02

### Added

- `--max-length-std`/`--max-length-percentile` on `omen train`: when
  `--max-length` is not given, it is now computed from the corpus itself as
  the larger of `ceil(mean + N * stdev)` (default `N=1.5`) and the length
  covering a given percentile of the corpus (default: 0.995), instead of a
  fixed cutoff of 20. The percentile estimate catches a long-but-thin tail
  that mean+stdev alone can miss on a low-variance corpus; it's skipped
  under 30 passwords, where it would be noise rather than signal. Pass
  `--max-length` explicitly to keep the old fixed behaviour.
- `--min-symbol-slots` on `omen train` and `omen alphabet`: with `--profile`,
  the alphabet size is now auto-raised above `--alphabet-size`/`--size` when
  needed so the profile's floor never crowds non-floor symbols and
  punctuation down to only a handful of slots (default headroom: 14).
- Corpus ingestion drops any line containing U+FFFD (the Unicode replacement
  character) — whether from an invalid byte sequence in the input or already
  present in it — before it can compete for a real alphabet slot.
- `CHANGELOG.md` (this file).

### Changed

- `omen train`'s summary line now reports `max_length`, and prints a note
  when it was auto-computed or the alphabet size was auto-raised.

## [1.1.0] - 2026-10-01

### Added

- Language alphabet profiles (`--profile`, 15 codes covering 14 languages):
  reserve a floor of a language's own letters so a small or noisy corpus
  can't lose a base letter to a frequency fluke from unrelated foreign-script
  data.
- `alphabet_warnings()`: detect-and-warn when a trained (or hand-built)
  alphabet looks too narrow or script-contaminated for its profile — or, with
  no profile given, looks mixed-script at all.
- `omen inspect` and `omen alphabet --profile` report the profile and any
  warnings.

## [1.0.0] - 2026-10-01

### Added

- `native/omen-rank`: a native C rank estimator, wired into `omen eval
  --rank` automatically when built (`make -C native omen-rank`), with a
  pure-Python fallback when it isn't. Same output either way — this is a
  speed-only change, orders of magnitude faster than the pure-Python path on
  large batches.

[Unreleased]: https://github.com/CarbydeSystems/OMEN-reborn/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/CarbydeSystems/OMEN-reborn/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/CarbydeSystems/OMEN-reborn/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/CarbydeSystems/OMEN-reborn/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/CarbydeSystems/OMEN-reborn/releases/tag/v1.0.0
