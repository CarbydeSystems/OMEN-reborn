# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.0] - 2026-10-02

### Added

- `--max-length-std` on `omen train`: when `--max-length` is not given, it is
  now computed from the corpus itself as `ceil(mean + N * stdev)` over the
  lengths of passwords the trained alphabet can represent (default `N=1.5`),
  instead of a fixed cutoff of 20. Pass `--max-length` explicitly to keep the
  old fixed behaviour.
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

[Unreleased]: https://github.com/CarbydeSystems/OMEN-reborn/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/CarbydeSystems/OMEN-reborn/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/CarbydeSystems/OMEN-reborn/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/CarbydeSystems/OMEN-reborn/releases/tag/v1.0.0
