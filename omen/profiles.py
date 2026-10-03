"""Language alphabet profiles — a reserved character floor per locale.

:func:`~omen.alphabet.select_alphabet` picks the ``size`` most frequent
characters in a corpus with no notion of language at all. On a small or
noisy corpus this can silently drop a base letter of the target language to
a frequency fluke elsewhere in the data (a real incident: a 72-char English
alphabet trained on a crackstation sample lost uppercase ``Q``, ``J``, ``X``
to a single stray Japanese character that happened to be marginally more
frequent). A password containing a dropped letter becomes permanently
unrepresentable: not deprioritised, excluded.

A :class:`LanguageProfile` names a *floor*: characters a profile guarantees
a place in the alphabet regardless of their corpus frequency, even a count
of zero. :func:`floor_chars` always includes the 62-character Latin/digit
base (``a-z A-Z 0-9``) plus, for non-English profiles, the language's own
accented or native-script letters.

Floor data provenance
----------------------
Every non-English profile's extra characters are transcribed from hashcat's
own ``charsets/combined/<Language>.hcchr`` files (hashcat 7.1.2) — a
community-vetted per-language taxonomy already in wide password-cracking
use, rather than a hand-guessed list. Each file is a legacy single-byte
codepage; the comment on each profile below names the source file and the
encoding it was decoded with. Decoded text was filtered to
``str.isalpha()`` characters only (dropping incidental currency/punctuation
symbols that share the same codepage) and deduplicated. The files are not
parsed at runtime — this keeps the package dependency-free and the floor
auditable as plain literal strings.

Two profiles (``el`` and ``el-polytonic``) currently resolve to the same
character set: hashcat's ``GreekPolytonic.hcchr`` decodes identically to
``Greek.hcchr`` once filtered to single-byte-representable letters — full
polytonic diacritics live in Unicode's Greek Extended block, which no
legacy codepage covers. Kept as distinct profile codes (rather than
collapsed to one) so a future enrichment of ``el-polytonic`` with explicit
Unicode polytonic letters is a data-only change.

Script-based profiles (``bg``, ``ru``, ``el``, ``el-polytonic``) union their
native-script letters *onto* the Latin base rather than replacing it —
transliterated/Latin-script passwords are common even for speakers whose
profile is Cyrillic or Greek. This makes their floor alone (62 Latin +
60-75 native-script letters) exceed the default ``alphabet_size`` of 72;
:func:`~omen.alphabet.select_alphabet` raises :class:`~omen.errors.TrainingError`
rather than silently truncating when a profile's floor cannot fit the
requested size — the operator raises ``--alphabet-size`` instead, which is
the correct fix, not a bug to work around.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_LATIN_BASE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# Unicode general categories with no script of their own — digits,
# punctuation, currency/math/other symbols, separators. A character in one
# of these is never "foreign" to any profile; it just isn't evidence for or
# against a script either way.
_SCRIPTLESS_CATEGORIES = frozenset(
    {"Nd", "Nl", "No", "Pc", "Pd", "Pe", "Pf", "Pi", "Po", "Ps", "Sc", "Sk", "Sm", "So", "Zs"}
)

# Every profile's floor is Latin-based except the native-script ones, which
# union a second script onto it (see the module docstring). Scripts here are
# the first word of the Unicode character name (see _script), not ISO script
# codes — good enough to tell "expected" from "foreign", not a full script
# classifier.
_EXTRA_SCRIPTS: dict[str, frozenset[str]] = {
    "ru": frozenset({"CYRILLIC"}),
    "bg": frozenset({"CYRILLIC"}),
    "el": frozenset({"GREEK"}),
    "el-polytonic": frozenset({"GREEK"}),
}


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    """A named floor of characters guaranteed a place in the alphabet.

    ``extra`` is this language's *addition* to (English profiles) or
    *union with* (native-script profiles) :data:`_LATIN_BASE` — never the
    full floor by itself; use :func:`floor_chars` to get the complete set.
    """

    code: str
    name: str
    extra: str
    source: str


PROFILES: dict[str, LanguageProfile] = {
    p.code: p
    for p in (
        LanguageProfile(
            "en", "English", "", "combined/English.hcchr (cp1252) — empty: ASCII is the floor"
        ),
        LanguageProfile("de", "German", "ºßÄäöÖÜü", "combined/German.hcchr (cp1252)"),
        LanguageProfile(
            "fr", "French", "ªàÀâÂæÆçÇèÈéÉêÊëËîÎÏïôÔÙùÛûÜüÿŸœŒ", "combined/French.hcchr (cp1252)"
        ),
        LanguageProfile(
            "es", "Spanish", "ªºàÀáÁçÇèÈéÉÍíÏïñÑòÒóÓúÚÜü", "combined/Spanish.hcchr (cp1252)"
        ),
        LanguageProfile(
            "es-castilian",
            "Spanish (Castilian)",
            "ªºáÁéÉíÍñÑóÓÚúÜü",
            "combined/Castilian.hcchr (cp1252)",
        ),
        LanguageProfile(
            "ca", "Catalan", "ªºàÀçÇèÈéÉÍíÏïòÒóÓúÚÜü", "combined/Catalan.hcchr (cp1252)"
        ),
        LanguageProfile(
            "it", "Italian", "ªºàÀèÈéÉÌìÍíîÎòÒóÓÙùúÚ", "combined/Italian.hcchr (cp1252)"
        ),
        LanguageProfile(
            "pt",
            "Portuguese",
            "ªºàÀáÁâÂÃãçÇéÉêÊÍíóÓôÔÕõúÚÜü",
            "combined/Portuguese.hcchr (cp1252)",
        ),
        LanguageProfile(
            "pl", "Polish", "óÓĄąćĆĘęĽłŁńŃśŚŹźżŻˇ", "combined/Polish.hcchr (cp1250)"
        ),
        LanguageProfile(
            "sk",
            "Slovak",
            "µáÁäÄéÉÍíóÓôÔúÚýÝĄąČčĎďĹĺĽľŇňŔŕŠšŤťŽžˇ",
            "combined/Slovak.hcchr (cp1250)",
        ),
        LanguageProfile(
            "lt",
            "Lithuanian",
            "ĄąČčēĒėĖĘęĢģĮįłŁŠšŪūųŲŹźŽž",
            "combined/Lithuanian.hcchr (ISO-8859-13)",
        ),
        LanguageProfile(
            "bg",
            "Bulgarian",
            "µаАБбвВГгДдЕежЖЗзИиЙйкКлЛмМНнОопПРрсСТтУуФфХхЦцЧчШшщЩЪъЫыЬьэЭЮюяЯёєЅѕІіїјґ",
            "combined/Bulgarian.hcchr (cp1251) — full Cyrillic + extras, unioned onto Latin",
        ),
        LanguageProfile(
            "ru",
            "Russian",
            "µАабБВвгГдДеЕжЖЗзИиЙйкКлЛмМнНоОпПРрСсТтуУфФХхцЦЧчшШщЩЪъЫыьЬэЭЮюяЯёЁєѕЅІіїЈјЎґ",
            "combined/Russian.hcchr (cp1251) — full Cyrillic + extras, unioned onto Latin",
        ),
        LanguageProfile(
            "el",
            "Greek",
            "ΐάΆΈέΉήΊίΰαΑβΒγΓδΔΕεΖζΗηΘθΙικΚλΛμΜΝνΞξοΟπΠΡρςσΣτΤυΥφΦχΧΨψωΩϊΪϋΫόΌύΎΏώ",
            "combined/Greek.hcchr (cp1253) — full Greek, unioned onto Latin",
        ),
        LanguageProfile(
            "el-polytonic",
            "Greek (Polytonic)",
            "ΐάΆΈέΉήΊίΰαΑβΒγΓδΔΕεΖζΗηΘθΙικΚλΛμΜΝνΞξοΟπΠΡρςσΣτΤυΥφΦχΧΨψωΩϊΪϋΫόΌύΎΏώ",
            "combined/GreekPolytonic.hcchr (cp1253) — see module docstring: "
            "identical to 'el' at the single-byte-codepage level",
        ),
    )
}


def floor_chars(code: str) -> frozenset[str]:
    """Return the complete guaranteed-floor character set for a profile code.

    Always includes the 62-character Latin/digit base. Raises
    :class:`KeyError` for an unknown code — callers that take a profile code
    from user input should catch this and report :func:`available_profiles`.
    """
    profile = PROFILES[code]
    return frozenset(_LATIN_BASE) | frozenset(profile.extra)


def available_profiles() -> list[str]:
    """Profile codes in a stable, display-friendly order (English first)."""
    return ["en", *sorted(c for c in PROFILES if c != "en")]


def expected_scripts(code: str) -> frozenset[str]:
    """Unicode scripts a profile's floor legitimately draws from.

    Every profile includes ``LATIN`` (the base, including bare ASCII digits'
    neighbouring punctuation); native-script profiles additionally include
    their own script per :data:`_EXTRA_SCRIPTS`.
    """
    return frozenset({"LATIN"}) | _EXTRA_SCRIPTS.get(code, frozenset())


def is_expected_script(ch: str, profile: str) -> bool:
    """Whether ``ch`` is scriptless, or its script is one of ``profile``'s
    own :func:`expected_scripts` — the same yardstick :func:`alphabet_warnings`
    uses to flag a foreign character after the fact, exposed here so
    :func:`~omen.alphabet.select_alphabet` can prefer expected-script
    candidates *during* selection instead of only checking afterwards.
    """
    script = _script(ch)
    return script == "" or script in expected_scripts(profile)


def _script(ch: str) -> str:
    """Best-effort Unicode script name for one character.

    Uses the first word of the character's canonical Unicode name — e.g.
    ``LATIN SMALL LETTER A`` -> ``LATIN``, ``HIRAGANA LETTER NO`` ->
    ``HIRAGANA``, ``CYRILLIC SMALL LETTER SHORT I`` -> ``CYRILLIC``. No
    per-script lookup table to maintain: the stdlib already encodes this in
    every character's name. Digits, punctuation, and symbols
    (:data:`_SCRIPTLESS_CATEGORIES`) and unassigned/unnamed code points
    return ``""`` — scriptless, never "foreign" to anything.
    """
    if unicodedata.category(ch) in _SCRIPTLESS_CATEGORIES:
        return ""
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return ""
    return name.split(" ", 1)[0]


def detect_dominant_script(chars: Iterable[str]) -> str:
    """The most common Unicode script among ``chars``, by majority vote.

    Scriptless characters (digits, punctuation, symbols) don't vote. Returns
    ``""`` when no character has a detectable script at all (e.g. an
    all-digit alphabet) — callers should treat that as "nothing to check
    against", not as a script named "" to flag everything else against.
    """
    counts = Counter(s for ch in chars if (s := _script(ch)))
    return counts.most_common(1)[0][0] if counts else ""


def _best_excluded_match(
    excluded: Iterable[tuple[str, int]], is_expected: Callable[[str], bool]
) -> tuple[str, int] | None:
    """Highest-count ``(char, count)`` in ``excluded`` matching ``is_expected``.

    ``None`` if nothing matches. A foreign character inside the alphabet and
    an expected-script character excluded from it are two symptoms of the
    same contaminated-corpus cause, not necessarily a direct one-for-one
    trade — so this names the single strongest excluded candidate rather
    than pairing one with the other.
    """
    matches = [(ch, n) for ch, n in excluded if is_expected(ch)]
    return max(matches, key=lambda kv: kv[1], default=None)


def _append_near_miss_warning(
    warnings: list[str],
    excluded: list[tuple[str, int]],
    is_expected: Callable[[str], bool],
    explanation: str,
) -> None:
    """Append a near-miss warning naming the best excluded match, if any."""
    best = _best_excluded_match(excluded, is_expected)
    if best is not None:
        ch, n = best
        warnings.append(
            f"the highest-ranked excluded character was {ch!r} (count={n}) — {explanation}"
        )


def alphabet_warnings(
    chars: Iterable[str],
    profile: str | None,
    excluded: Iterable[tuple[str, int]] = (),
) -> list[str]:
    """Human-readable warnings for a trained alphabet, or ``[]`` if none.

    Two independent checks, chosen by whether a profile was used:

    * **A profile was given or detected**: flags any of the profile's floor
      characters missing from ``chars`` (should be structurally impossible
      when the alphabet came from :func:`~omen.alphabet.select_alphabet`
      with this same profile's floor — kept as a defence-in-depth check for
      an alphabet assembled another way, e.g. an explicit ``--alphabet``
      string that happens to claim a profile), plus any character whose
      script falls outside what that profile expects (:func:`expected_scripts`).
    * **No profile at all**: the real incident this module exists for — a
      plain, profile-less frequency run can still pick up a stray foreign
      character (the original repro: one Japanese character in an otherwise
      all-Latin 72-char English alphabet, with no profile ever named). Takes
      the alphabet's own dominant script as the implied baseline via
      :func:`detect_dominant_script` and flags anything outside it.

    ``excluded`` — typically :attr:`~omen.alphabet.AlphabetSelection.excluded`
    — lets either check additionally name the highest-ranked expected-script
    character that was crowded out, whenever a foreign character made the
    alphabet despite one being available. Omit it (the default) when none is
    available — e.g. :class:`~omen.inspect.ModelInspector` reporting on an
    already-saved model, which keeps no corpus frequency data to show.
    """
    chars = list(chars)
    excluded = list(excluded)
    warnings: list[str] = []

    if profile is not None:
        floor = floor_chars(profile)
        missing = sorted(floor - set(chars))
        if missing:
            warnings.append(
                f"alphabet is missing {len(missing)} floor character(s) for "
                f"profile {profile!r}: {''.join(missing)}"
            )
        # A floor character is never "foreign" to its own profile, whatever a
        # generic script heuristic makes of it — e.g. hashcat's own Russian/
        # Bulgarian/Slovak charset data includes U+00B5 MICRO SIGN (picked up
        # from the legacy codepage it was transcribed from), which isn't
        # really Cyrillic *or* Latin by Unicode name, but is deliberately
        # part of those floors and must not warn every time they're used.
        allowed = expected_scripts(profile)
        foreign = sorted(
            ch for ch in chars if ch not in floor and not is_expected_script(ch, profile)
        )
        if foreign:
            warnings.append(
                f"alphabet has {len(foreign)} character(s) outside profile "
                f"{profile!r}'s expected script(s) ({'/'.join(sorted(allowed))}): "
                f"{''.join(foreign)}"
            )
            _append_near_miss_warning(
                warnings,
                excluded,
                lambda ch: is_expected_script(ch, profile),
                "an expected-script candidate was excluded while a "
                "foreign-script one was kept; select_alphabet's own "
                "script-aware remainder selection prevents this when it ran, "
                "so this alphabet likely wasn't built through it",
            )
        return warnings

    dominant = detect_dominant_script(chars)
    if dominant:
        foreign = sorted(ch for ch in chars if _script(ch) not in ("", dominant))
        if foreign:
            warnings.append(
                f"alphabet looks mixed-script (dominant: {dominant}, no --profile "
                f"given); {len(foreign)} character(s) look foreign: {''.join(foreign)}"
            )
            _append_near_miss_warning(
                warnings,
                excluded,
                lambda ch: _script(ch) in (dominant, ""),
                "training with --profile enables script-aware selection, which may recover it",
            )
    return warnings
