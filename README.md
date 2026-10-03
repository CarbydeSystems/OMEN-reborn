# OMEN-reborn

OMEN-reborn is a clean-room **Ordered Markov ENumerator**. It generates
password candidates, ranked by probability. It is modern, dependency-free
Python.

OMEN builds an order-`n` Markov model from a corpus of cracked passwords. It
streams candidate guesses in approximately descending probability order. That
ordering is the whole point: a back-end like hashcat tries the most likely
passwords first. It cracks more passwords for the same number of guesses.

This is an **independent implementation, written from the published
algorithm** (Dürmuth et al., *OMEN: Faster Password Guessing Using an Ordered
Markov Enumerator*, ESSoS 2015). No code from the original RUB-SysSec OMEN is
used.

## Benchmark: OMEN vs. PRINCE

Trained on 90% of `rockyou` and evaluated on the **disjoint** held-out 10%
(split by `md5(password) % 10`, so no password is in both sets). The chart shows
the cumulative share of the 1,456,297 held-out passwords cracked as a function
of the number of guesses.

![Crack rate vs. guesses](docs/img/crackrate_vs_guesses.png)

| Method | Crack rate @ 20M guesses |
|--------|-------------------------:|
| **OMEN n=4** | **12.3%** |
| OMEN n=3 | 9.3% |
| PRINCE (train words) | 3.2% |
| PRINCE (top-100k) | 1.7% |
| rockyou wordlist (replay) | 0.0% |

OMEN cracks **~4× more** than the best PRINCE configuration, at the same
guess budget. The reason: OMEN generates new character sequences from a
Markov model. PRINCE only recombines existing words.

The wordlist baseline sits at exactly **0.0%**. Replaying training words
cracks nothing on a disjoint test set. This confirms two things: there is no
train/test leakage, and every OMEN crack is genuine generalisation.

PRINCE still has real strengths — no training needed, direct hashcat
pipelining, no alphabet or length limits. Those strengths are about
operation, not about guess ordering.

Full methodology and a one-command reproduction are in
[`benchmarks/`](benchmarks/README.md).

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

## Install

Runtime is **stdlib-only**. For development:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Without installing, run as a module from the repo root: `python -m omen ...`.

## Usage

```bash
# 1. Train a model from a password list (max length is auto-computed from
#    the corpus's own length spread — pass --max-length to fix it yourself)
omen train -i cracked.txt -m model/ --ngram 3 --levels 11

# 1b. Sparse org corpus? Supplement it with the first 500K rockyou passwords
#     so the n-gram model isn't starved on a handful of cracks (0 = whole file)
omen train -i cracked.txt -m model/ --supplement rockyou.txt --supplement-lines 500000

# 2. Stream ranked candidates (this is what you pipe into a cracker)
omen generate -m model/ --max-guesses 5000000 | hashcat -a 0 -m 1000 hashes.txt

# 3. Score individual passwords (clean replacement for evalPW)
#    --rank uses native/omen-rank on its own once you build it (see below)
omen eval -m model/ Password1 hunter2 --rank

# 4. Inspect a trained model
omen inspect -m model/

# 5. Choose / preview an alphabet from a corpus
#    --profile reserves a language's floor characters first (see below)
omen alphabet -i cracked.txt --size 72 --profile de
```

### Commands

| Command    | Purpose                                                        |
|------------|---------------------------------------------------------------|
| `train`    | Corpus → model directory.                                     |
| `generate` | Stream ranked candidates to **stdout** (SIGPIPE-clean).       |
| `eval`     | Per-password total level, component breakdown, optional rank. |
| `alphabet` | Select the most frequent characters and report coverage.      |
| `inspect`  | Model metadata, table sizes, per-table level histograms.      |
| `spool`    | Chunk a producer's output into tmpfs files; attack each with hashcat. |

## How it works

OMEN turns each conditional probability into an integer **level**. Level 0 is
the most probable; level `NL-1` is the least. The formula is
`level(p) = round(-ln p / lam)`.

A candidate's **total level** is the sum of its components:

```
total = IP(initial n-1 gram) + Σ CP(transition) + EP(ending) + LN(length)
```

The enumerator walks `total = 0, 1, 2, …`. At each total, it emits *every*
candidate with that total before moving to the next. This makes the output
stream globally non-decreasing in level.

One `LevelScale` is shared by all tables. This keeps every component
additive and comparable. `omen eval` recomputes the same total-level value,
so a password's score always equals the level the generator would emit it
at.

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

A floor only protects a profile's own letters. The slots left over are
still filled by plain frequency, and plain frequency has no notion of
script. A large foreign-language sample mixed into the corpus — a
multi-language breach compilation, say — could still win one of those slots
over a legitimate symbol from the profile's own script. This is the same
failure mode a floor exists to prevent, just one layer further out.

A real case found this: `?` lost out to two Japanese punctuation marks this
way. This happened in a corpus where the floor fix alone had already
recovered `Q`/`J`/`X`. With `--profile` set, `train` and `alphabet` now fill
the leftover slots from same-script-or-scriptless characters first. They
only draw from foreign-script characters if that pool runs out.

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

## Performance: native enumerator + chunked feeding

The pure-Python `PyEnumerator` emits ~10⁵ candidates/s. That's fine for
analysis, but too slow to feed a fast GPU back-end. Two pieces close the
gap:

### Native C enumerator (`native/omen-enum`)

This is a standalone C program. It reads the same model directory — it
`mmap`s the level tables and reads `manifest.bin`. It streams candidates
**byte-for-byte identical** to `PyEnumerator`, **~50–100× faster** (about
5–10M candidates/s, host-dependent). `tests/test_native_parity.py` verifies
the parity. Build it and use it as a drop-in producer:

```bash
make -C native                      # produces native/omen-enum
native/omen-enum model/ --max-guesses 50000000 | hashcat -a 0 -m 1000 hashes.txt
```

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
(`tests/test_native_rank_parity.py` checks that both tools agree). Build the
tool once:

```bash
make -C native omen-rank
```

This command makes the file `native/omen-rank`.

`eval --rank` uses this tool on its own after you build it. You do not need
an extra flag. If the tool is missing, `eval` uses the Python path instead.
Both paths give the same rank for the same password.

You can also run the tool directly:

```bash
native/omen-rank model/ --rank-cap 10000000 Password1 hunter2
```

Use `-i FILE` to score many passwords in one run:

```bash
native/omen-rank model/ -i passwords.txt > ranks.txt
```

Use `-i -` to read passwords from stdin. The tool loads the model one time.
It then scores every password in the same run.

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

```bash
omen spool --hashcat "hashcat -a 0 -m 1000 {chunk} hashes.txt" --chunk-mb 512 \
           -- native/omen-enum model/
```

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

```bash
ruff check . && ruff format --check . && mypy && pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
