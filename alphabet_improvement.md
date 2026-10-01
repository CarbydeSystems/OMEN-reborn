# Alphabet selection: guarantee full base-charset coverage, add language profiles

## The bug, concretely

`select_alphabet()` (`omen/alphabet.py`) is pure global frequency ranking:
count every character across the training corpus, take the top `size` by
count (ties broken by ascending code point), done. No charset awareness, no
guaranteed floor, no language profile. It will silently drop a whole English
letter if something else in the corpus happens to be marginally more
frequent — and it did.

**Real repro case** (Ashfall engagement session, 2026-09-30, `rjf` campaign):
trained a 72-char model on a corpus of real cracked passwords + an org-term
wordlist + a systematic sample of `crackstation.txt` (every 2800th line
across the full 15.7GB file — a quick way to get a size-bounded spread
without reading the whole thing, see that session's notes if useful context).
`crackstation.txt` is a raw multilingual breach compilation; the sample
picked up a handful of non-English fragments along with everything else.

Resulting alphabet (`omen inspect`):

```
alphabet size    : 72
alphabet coverage: 92.91%
alphabet         : eanirostlcmduh1g.230kb459678pyAvwf ECjDBSIzRN@TOLM-PHGUxqK!FYVW$+Zの()/#_
```

**Missing: uppercase `Q`, `J`, `X`.** Present instead: a Japanese character
(の, "no") that edged out at least one of them by raw frequency count alone.

Consequence, measured directly: ran the operator's native `omen-rank` against
1,284 held-out passwords from the same engagement. Every password containing
a missing letter came back `UNREACHABLE (contains an out-of-alphabet
character)` — instantly, unconditionally, regardless of how plausible the
rest of the password is. Two concrete hits in a ~15-password sample:
`!1Q2s3e4f5t6h7u8k9o`, `!QAZ$RFV1qaz4rfv` (the latter a QWERTY keyboard-walk
pattern — exactly the kind of thing a password-cracking tool should never be
structurally blind to).

This is a correctness bug, not a tuning nit. A tool whose entire job is
"model what passwords look like" should not be able to lose a base Latin
letter to one Unicode outlier. The `92.91%` coverage number was sitting right
there in `omen inspect`'s own output the whole time — it's a real signal,
just not one anything acts on today.

## Why this matters more than it looks

- English passwords draw from a near-complete `A-Za-z0-9` + a handful of
  symbols. Losing even one letter means every password containing it in
  *any* position is unconditionally unrankable and ungeneratable — not
  deprioritized, excluded. For a corpus-driven alphabet with a small budget
  (72, say), this is a real-probability event, not an edge case — rare
  letters (Q, J, X, Z) are exactly the ones a pure-frequency cutoff is most
  likely to bump for a handful of foreign-script or symbol outliers.
- It fails silently. Nothing in `train`'s own output flags "you just lost a
  base letter" — you only find out by noticing `UNREACHABLE` results later
  (as happened here), or by eyeballing `omen inspect`'s alphabet string
  character by character.
- It compounds with `eval --rank`/`omen-rank`'s existing cost: a password
  that's merely low-probability still costs guesses to rank; one that's
  alphabet-excluded costs *zero* guesses and still never gets found. The
  failure mode is worse than "ranked last," not equivalent to it.

## Proposed fix: a reserved floor + frequency-ranked remainder

Change `select_alphabet()`'s contract from "top `size` characters by global
frequency" to:

1. **Reserve a base floor** for the active language profile (see below) —
   e.g. for `en`: all 26 lowercase, all 26 uppercase, all 10 digits = 62
   chars, unconditionally included regardless of their corpus frequency
   (even a count of zero — a letter that never appears in training data
   should still be *generatable*, just assigned appropriately low
   probability by the n-gram tables; it should never be alphabet-excluded).
2. **Fill the remainder from corpus frequency**, same algorithm as today,
   over symbols/punctuation and (if the profile allows it) non-floor
   characters — this is where `!`, `@`, `$`, space, and genuinely
   corpus-specific signal (org-specific accented names, etc.) should still
   compete on frequency, same as now.
3. **Make the floor size vs. frequency-slot split visible and configurable**
   — e.g. `--size 72 --profile en` reserves 62 and ranks the remaining 10;
   `--profile en-strict` could reserve more of the symbol space too
   (`. @ - ! $ + ( ) / # _ %  ^ & * ? : ;` or similar) if the operator wants
   fewer surprises at the cost of less corpus-adaptive symbol selection.
4. **Surface a hard warning (not just the existing coverage %) when a profile
   floor can't fit the requested size** — e.g. `--profile en --size 40`
   should fail loud (floor alone needs 62) rather than silently falling back
   to pure frequency ranking and reintroducing this exact bug by a different
   path.

This is additive, not a breaking change to the model format — `Alphabet` is
just an ordered tuple of characters either way; only `select_alphabet()`'s
internals and CLI surface change.

## Language profiles — the broader ask

Right now there's exactly one implicit profile: "whatever characters are
frequent in whatever corpus you fed it," with no notion of a target
language/locale at all. Worth designing toward real profiles, e.g.:

- `en` — floor as above (`A-Za-z0-9`), remainder frequency-ranked from
  corpus symbols.
- `es` / `fr` / `de` / etc. — same base Latin floor, plus a small reserved
  slice for the language's own common accented/special characters (`ñ, á,
  é, í, ó, ú, ü` for Spanish; `é, è, ê, ç, à, ù` for French; `ä, ö, ü, ß` for
  German) so a Spanish-language org's password corpus doesn't lose `ñ` to a
  frequency fluke the same way this run lost `Q`.
- An **auto-detect** mode: sniff the dominant Unicode script/language in the
  training corpus (even a simple heuristic — ratio of Latin-vs-other
  codepoints, or a cheap per-character script lookup) and auto-select the
  floor profile, falling back to `en` or a configurable default when the
  corpus is already mixed/ambiguous. Explicit `--profile` should always
  override auto-detection.
- A **multi-language floor** for a genuinely mixed-language target org
  (plausible for an international company) — union multiple language
  floors, understanding that an 72-character total budget gets tight fast
  once several languages' floors are unioned; probably needs a bigger
  default alphabet size (or a documented warning) when more than one
  profile is active.

This needs real research before committing to specifics (per your note) —
this doc is meant to hand off the problem cleanly, not pre-decide the
profile taxonomy. Starting points worth checking before designing further:
- Whether `hashcat`'s own charset/hcmask conventions already have a
  reasonable per-language common-symbol taxonomy worth reusing instead of
  inventing one.
- Whether a cheap, dependency-free script-detection heuristic (Unicode
  block ranges) is good enough, or whether this needs a real
  language-detection library — probably the former, given this only needs
  to pick a *profile*, not translate or fully classify text.

## Suggested validation once implemented

- A regression test: train with `--profile en` on a corpus that's
  deliberately skewed to make pure-frequency selection drop a base letter
  (reproduce this exact bug synthetically), assert the resulting alphabet
  still contains all 62 floor characters regardless.
- A CLI-level check: `omen inspect` should flag explicitly when the active
  alphabet is missing any floor character for its stated/detected profile —
  not just report the raw coverage percentage, which (as this run showed, at
  92.91%) can look superficially fine while still hiding a complete letter
  loss.
- Keep the repro case above (`rjf` model, this exact corpus construction, the
  two named `UNREACHABLE` passwords) as a known-bad fixture to validate the
  fix against directly.
