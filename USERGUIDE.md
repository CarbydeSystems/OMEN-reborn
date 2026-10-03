# OMEN-reborn User Guide

The [README](README.md) covers install, usage, and build commands. This
guide covers everything else: why OMEN works the way it does, how to tune
it, the on-disk model format, the codebase's module map, and the dev
workflow.

## Why a rewrite

Three problems made the old code hard to build on. The original C
implementation is unmaintained, and it crashes on modern glibc. The old
`py-omen` needs Python 3.6. The algorithm's reference behaviour is hard to
reproduce from either one.

This project does not patch the old C code. It re-derives the algorithm
cleanly instead, with:

- a single, well-defined level model, shared across all tables,
- length as a first-class term in the ordering (common lengths rank earlier —
  not just within one fixed length),
- hardening against untrusted input on every model load, and
- full type hints, `mypy --strict`, `ruff`, and a pytest suite.

## Language profiles

`omen train` picks the alphabet's characters by corpus frequency. On a small
or noisy corpus, this can drop a base letter of the target language. A real
case: a 72-character English alphabet lost uppercase `Q`, `J`, and `X`. One
Japanese character in the training data was slightly more frequent. It took
a letter's place. Every password with that letter became unrankable — not
ranked low, excluded completely.

A language profile stops this. It reserves a floor: a set of characters that
always get a place in the alphabet, no matter how often they appear in the
corpus. Use `--profile` on `train` or `alphabet`:

```bash
omen train -i cracked.txt -m model/ --profile de --alphabet-size 72
omen alphabet -i cracked.txt --profile es   # preview only, no training
```

Every profile's floor starts with the 62-character Latin base (`a-z A-Z
0-9`). Most profiles add a small set of their own letters on top. German
adds `ä ö ü ß` and their capitals. French adds `é è ê ç` and more. 14
languages work this way.

Four profiles are different: `ru`, `bg`, `el`, and `el-polytonic`. Each adds
a full second alphabet — Cyrillic or Greek — instead of a few letters. Their
floor is larger than the default `--alphabet-size` of 72, on purpose. If you
request one of these profiles at the default size, `train` stops with a
clear error. The error tells you the size to use instead. This is correct
behaviour, not a bug to work around: a Cyrillic password needs room for
Cyrillic letters.

A floor can use up most of `--alphabet-size`. The German floor alone is 70
characters. Out of a default size of 72, that leaves only 2 slots for
everything else, punctuation included. `train` and `alphabet` raise the size
automatically when this happens. At least `--min-symbol-slots` (default: 14)
stay free for non-floor characters, on top of the floor:

```
omen: alphabet size auto-raised 72 -> 84 (70 profile floor + 14 symbol headroom)
```

The floor data comes from hashcat's own `charsets/combined/*.hcchr` files.
These character sets are already in wide use across the password-cracking
community. `omen` does not read these files at run time. The letters are
copied once into `omen/profiles.py`. This way, `omen` has no dependency on
hashcat being installed.

### Foreign characters beyond the floor

A floor only protects a profile's own letters. The slots left over are
still filled by plain frequency, and plain frequency has no notion of
script. A large foreign-language sample mixed into the corpus — a
multi-language breach compilation, say — could still win one of those slots
over a legitimate symbol from the profile's own script. This is the same
failure mode a floor exists to prevent, just one layer further out.

A real case found this: a foreign-script letter beat a real symbol for one
of those leftover slots, in a corpus where the floor fix had already
recovered `Q`/`J`/`X`. With `--profile` set, `train` and `alphabet` now fill
the leftover slots from same-script-or-scriptless characters first. They
only draw from foreign-script characters if that pool runs out.

That fix checks a character's Unicode *name* for a script word, like
`HIRAGANA` or `CYRILLIC`. It does not check punctuation the same way. Some
punctuation shares a Unicode *category* with ordinary ASCII punctuation,
even when it is just as locale-bound as a foreign letter. CJK full-width
punctuation is the clearest case: `。` (IDEOGRAPHIC FULL STOP), `、`
(IDEOGRAPHIC COMMA), and `（` (FULLWIDTH LEFT PARENTHESIS) all share a
category with a plain period or comma. Without a separate check, they stay
in the "universal" group. They can still beat a real symbol like `?` for a
slot, the same way a foreign letter once could.

`omen` now also checks a character's East Asian width, a separate Unicode
property. A character marked "Wide" or "Fullwidth" is treated as
locale-bound, even when its category looks universal. This closes the gap
for CJK punctuation. It does not yet cover every other locale-bound
punctuation mark — Arabic punctuation is one known case, not yet covered.
That gap is real, but it is separate, smaller, and left for later work.

### Warnings

`train` and `alphabet` both print a warning when something still looks
wrong, whether or not you gave a profile:

- **Always**: a warning if the corpus has fewer distinct characters than
  `--alphabet-size`/`--size` asked for. Without this warning, the alphabet
  would come out smaller than requested with no explanation.
- **With a profile**: a warning if any floor letter did not make it into the
  alphabet. This should not happen through normal training — it is a safety
  check for an alphabet built another way. Also a warning if a trained
  character falls outside the profile's own script.
- **Without a profile**: `omen` finds the alphabet's own most common script.
  It warns about any character outside that script. This is the check that
  would have caught the real `Q`/`J`/`X` case above, even with no profile
  named at all.
- **Either way**: if a foreign-script character still made the alphabet,
  `omen` also names the highest-ranked same-script character that was
  excluded instead. This gives a concrete "here's what you're missing," not
  just "something's wrong."

`omen inspect` prints the first two checks for any saved model, including
one trained in an earlier session — the model file remembers which profile
(if any) was used. The last check needs the corpus's own character
frequency. A saved model doesn't keep that frequency data. So this check
only runs right after training, or from `omen alphabet` — not from
`inspect`.

### Decode noise

A corpus can also contain a different kind of noise: text that failed to
decode cleanly. `omen` reads training files as UTF-8. A byte sequence that
isn't valid UTF-8 decodes to U+FFFD, the Unicode replacement character. An
alphabet built by raw frequency could seat that replacement character like
any other. `omen` drops any line containing U+FFFD before it counts
characters. This way, a decode error can never win an alphabet slot.

## Password length

`omen train` also has a `--max-length`. A password longer than this is never
representable by the model — not ranked low, excluded completely. This is
the same kind of hard limit an alphabet has, just for length instead of
characters.

By default, `--max-length` is no longer fixed at 20. `omen train` computes
it from the corpus itself, as the larger of two estimates:

- the mean password length, plus `--max-length-std` standard deviations
  (default: 1.5), rounded up;
- the length covering `--max-length-percentile` of the corpus (default:
  0.995, the 99.5th percentile).

The first estimate alone can undershoot a corpus whose lengths cluster very
tightly. A long tail can sit many standard deviations out, even when it is
only a few characters longer in absolute terms. A small `std` multiplier
never reaches a tail like that.

The percentile estimate catches that tail directly. It measures how much of
the corpus is actually covered, not how far a length sits from the mean. On
fewer than 30 passwords, a percentile is noise, not signal — training falls
back to the mean+std estimate alone in that case. Pass `--max-length`
yourself to set a fixed value instead of either estimate.

Raising the limit costs little at training time. The length table is small,
and it grows linearly, not with the alphabet. The real cost shows up later,
at generation time. Every longer candidate takes more steps to produce. Each
one also uses more of a fixed chunk-size budget when feeding a cracker like
hashcat. Re-measure generation speed after a large `--max-length` increase —
the same way you would after changing the alphabet size.

## Model format

A model is a directory:

- `config.json` — version, `ngram`, `levels`, `lam`, smoothing, alphabet,
  length levels, coverage, profile (the language profile used, or `null`
  for none — see [Language profiles](#language-profiles)). **Fully validated
  before any table is allocated.**
- `ip.dat`, `cp.dat`, `ep.dat` — flat one-byte-per-entry level tables, indexed
  by packed alphabet codes (the native enumerator `mmap`s them directly).
- `manifest.bin` — fixed-layout little-endian header (ngram, levels, `lam`,
  alphabet, length levels) for the C enumerator, so it never parses JSON.

Dense tables have `A^n` entries, where `A` is the alphabet size. Memory
grows fast with `n`. At `A≈72`, `n=3` takes ~370 KB and `n=4` takes ~27 MB.
`n=5` would take ~2 GiB, so it is **blocked by default**.

To unlock `n=5`, raise one constant in `omen/model.py`:

```python
MAX_TABLE_ENTRIES = 1 << 31   # was 1 << 28 (256 MiB default)
```

Only do this on a machine with enough RAM, and with a large corpus — 250M+
passwords, for good context coverage. For most use cases, keep the alphabet
reduced (the default auto-selects the 72 most frequent characters) and
prefer `n=3` or `n=4`.

## Architecture

| Module          | Responsibility                                               |
|-----------------|--------------------------------------------------------------|
| `alphabet.py`   | `Alphabet` value object + frequency-based `select_alphabet`. |
| `profiles.py`   | Language floors, script detection, alphabet warnings.        |
| `levels.py`     | `LevelScale` — probability ↔ level mapping.                  |
| `model.py`      | `NgramModel` — tables, queries, validated save/load.         |
| `train.py`      | `ModelTrainer` — corpus → counts → smoothing → tables.       |
| `enumerate.py`  | `Enumerator` Protocol + `PyEnumerator` (ordered stream).     |
| `score.py`      | `PasswordScorer` — level + rank estimation.                  |
| `inspect.py`    | `ModelInspector` — human-readable report.                    |
| `io_utils.py`   | Buffered `ByteSink`, `LineSink` protocol, corpus reader.     |
| `cli.py`        | Thin argparse dispatch over the objects above.               |

## Performance details

The README covers the build and usage commands for all three pieces below.
This section covers how each one works, portability, and tuning.

### Native C enumerator (`native/omen-enum`)

This is a standalone C program. It reads the same model directory — it
`mmap`s the level tables and reads `manifest.bin`. It streams candidates
**byte-for-byte identical** to `PyEnumerator`, **~50–100× faster** (about
5–10M candidates/s, host-dependent). `tests/test_native_parity.py` verifies
the parity.

It mirrors the Python flags: `--max-guesses`, `--max-level`, `--min-length`,
`--max-length`.

**Portability.** `omen-enum` is **POSIX-only** — it uses `mmap` and
`unistd`. It builds on Linux and macOS. On Windows, or on any host without a
C compiler, use the pure-Python enumerator instead: `python -m omen
generate …`. (A bare `omen` shebang script does not run directly on
Windows. The `omen` console script only exists after `pip install`.) Both
enumerators emit byte-identical ordering, so a model trained once works with
either.

### Native C rank estimator (`native/omen-rank`)

`omen eval --rank` finds a password's rank. The rank is the count of more
probable candidates. `PasswordScorer.estimate_rank` finds this count in
Python. This is slow for many passwords.

`native/omen-rank` finds the same count in C. It is 50 to 100 times faster
(`tests/test_native_rank_parity.py` checks that both tools agree).

`eval --rank` uses this tool on its own after you build it. You do not need
an extra flag. If the tool is missing, `eval` uses the Python path instead.
Both paths give the same rank for the same password.

Use `-i -` to read passwords from stdin:

```bash
native/omen-rank model/ -i - < passwords.txt > ranks.txt
```

The tool loads the model one time. It then scores every password in the
same run.

**Portability.** `omen-rank` works only on POSIX systems, the same as
`omen-enum`. Use Linux or macOS. On Windows, `eval --rank` uses the Python
path.

### Chunked spool-and-attack (`omen spool`)

Piping into hashcat over stdin caps throughput. hashcat can't `mmap` a pipe.
It loses its wordlist amplifier. It stalls on backpressure — the "fast
burst, then ~300 MH/s" effect.

A FIFO doesn't help either. hashcat `mmap`s its wordlist, and a FIFO isn't
seekable.

The fix is a **RAM-backed file hashcat *can* `mmap`**. `omen spool` spools
the producer into bounded tmpfs (`/dev/shm`) chunk files, and attacks each
one. It is **double-buffered**: it fills chunk N+1 while attacking chunk N.

**Sizing `--chunk-mb`.** Each chunk starts a *fresh* hashcat process. Device
init and kernel-cache build cost a few seconds per launch. On a fast GPU, a
small chunk drains in ~1–2 s — at that size, the per-chunk startup cost
dominates. So **prefer large chunks** (GB-range `--chunk-mb`), to amortise
that cost.

The bound is RAM. The double-buffer keeps ~2 chunks resident, so budget ~2×
the chunk size. Rule of thumb: size a chunk to give each hashcat invocation
tens of seconds to a few minutes of work.

**Throughput reality check.** A *raw, ruleless* feed against a fast hash
(NTLM) is bound by the candidate rate — each candidate is one hash. The C
enumerator is the lever there, not chunking. The chunked-file win is
largest for two cases instead: **amplified** attacks (a base list times
`-r` rules on the GPU), and **slow hashes**. In both cases, mmap feeding
keeps the GPU saturated, instead of starving it on stdin.

## Development

Runtime is **stdlib-only**. Development needs extra tools:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Run every check before you commit:

```bash
ruff check . && ruff format --check . && mypy && pytest -q
```
