"""Tests for language alphabet profiles, auto-detection, and warnings."""

from __future__ import annotations

from pathlib import Path

import pytest

from omen.errors import TrainingError
from omen.model import NgramModel
from omen.profiles import (
    PROFILES,
    alphabet_warnings,
    available_profiles,
    detect_dominant_script,
    expected_scripts,
    floor_chars,
)
from omen.train import ModelTrainer, TrainingOptions

_LATIN_BASE_SIZE = 62  # a-z A-Z 0-9


# -- registry -----------------------------------------------------------


def test_fifteen_profiles_registered() -> None:
    # 14 languages, 15 profiles: Spanish ships two hashcat variants (es, es-castilian).
    assert len(PROFILES) == 15


def test_available_profiles_lists_every_code_english_first() -> None:
    codes = available_profiles()
    assert len(codes) == 15
    assert codes[0] == "en"
    assert set(codes) == set(PROFILES)


def test_registry_keys_match_each_profile_own_code() -> None:
    for code, profile in PROFILES.items():
        assert profile.code == code


def test_every_profile_floor_includes_the_latin_base() -> None:
    for code in PROFILES:
        floor = floor_chars(code)
        assert sum(1 for c in floor if c.isascii() and c.isalnum()) >= _LATIN_BASE_SIZE


def test_english_floor_is_exactly_the_latin_base() -> None:
    assert len(floor_chars("en")) == _LATIN_BASE_SIZE


def test_native_script_profiles_are_larger_than_the_default_alphabet_size() -> None:
    """ru/bg/el/el-polytonic union a full native script onto Latin — this is
    *why* they need a bigger --alphabet-size, not something to silently fit."""
    for code in ("ru", "bg", "el", "el-polytonic"):
        assert len(floor_chars(code)) > 72


def test_unknown_profile_rejected_by_training_options() -> None:
    with pytest.raises(TrainingError, match="unknown profile"):
        TrainingOptions(profile="klingon").validate()


def test_profile_and_explicit_alphabet_are_mutually_exclusive() -> None:
    with pytest.raises(TrainingError, match="mutually exclusive"):
        TrainingOptions(profile="de", alphabet="abc").validate()


# -- expected_scripts / detect_dominant_script ---------------------------


def test_expected_scripts_latin_only_profile() -> None:
    assert expected_scripts("en") == frozenset({"LATIN"})
    assert expected_scripts("de") == frozenset({"LATIN"})


def test_expected_scripts_native_script_profile_adds_its_script() -> None:
    assert expected_scripts("ru") == frozenset({"LATIN", "CYRILLIC"})
    assert expected_scripts("el") == frozenset({"LATIN", "GREEK"})


def test_detect_dominant_script_majority_vote() -> None:
    cyrillic_abvg = "абвг"  # "абвг" — escaped: avoids a Latin/Cyrillic
    # look-alike lint warning on a literal Cyrillic string sitting next to ASCII "A"
    assert detect_dominant_script("aaaaB") == "LATIN"
    assert detect_dominant_script(cyrillic_abvg + "A") == "CYRILLIC"


def test_detect_dominant_script_ignores_digits_and_symbols() -> None:
    assert detect_dominant_script("123!@#") == ""
    assert detect_dominant_script("abc123!") == "LATIN"


# -- alphabet_warnings ----------------------------------------------------


def test_no_profile_clean_single_script_alphabet_has_no_warnings() -> None:
    assert alphabet_warnings("abcdefgh12345", None) == []


def test_no_profile_foreign_character_is_flagged() -> None:
    """The real incident, reproduced exactly: no --profile was ever given,
    one foreign character is present among otherwise-Latin characters."""
    warnings = alphabet_warnings("abcdefghの", None)
    assert len(warnings) == 1
    assert "の" in warnings[0]
    assert "LATIN" in warnings[0]


def test_profile_given_missing_floor_character_is_flagged() -> None:
    # 'z' missing from an otherwise-full English floor, via a hand-built
    # alphabet (the defence-in-depth path — select_alphabet itself can't
    # produce this when its own floor parameter is used correctly).
    almost_full_floor = "".join(c for c in floor_chars("en") if c != "z")
    warnings = alphabet_warnings(almost_full_floor, "en")
    assert any("missing" in w and "z" in w for w in warnings)


def test_profile_given_clean_alphabet_has_no_warnings() -> None:
    assert alphabet_warnings(floor_chars("en"), "en") == []


def test_profile_given_foreign_script_character_is_flagged() -> None:
    chars = floor_chars("en") | {"の"}
    warnings = alphabet_warnings(chars, "en")
    assert any("outside profile" in w and "の" in w for w in warnings)


def test_native_script_profile_does_not_flag_its_own_script() -> None:
    """A Cyrillic letter must not be 'foreign' to the ru profile."""
    assert alphabet_warnings(floor_chars("ru"), "ru") == []


# -- end-to-end: train -> save -> load -> inspect round trip ------------


def test_profile_persists_through_save_and_load(tmp_path: Path) -> None:
    model = ModelTrainer(TrainingOptions(profile="de", max_length=10)).train(
        lambda: iter(["passwort", "schluessel", "gehaim"] * 5)
    )
    model.save(tmp_path)
    reloaded = NgramModel.load(tmp_path)
    assert reloaded.profile == "de"


def test_missing_profile_key_in_old_config_defaults_to_none(tmp_path: Path) -> None:
    """A model saved before profile support existed has no 'profile' key at
    all in config.json — must load cleanly with profile=None, not crash."""
    import json

    model = ModelTrainer(TrainingOptions(max_length=10)).train(
        lambda: iter(["password", "letmein"] * 5)
    )
    model.save(tmp_path)
    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    del config["profile"]  # simulate a pre-existing model file
    config_path.write_text(json.dumps(config), encoding="utf-8")

    reloaded = NgramModel.load(tmp_path)
    assert reloaded.profile is None
