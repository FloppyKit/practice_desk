"""OSS defaults must not ship live clinic letterhead."""
from __future__ import annotations

from app import practice

_BLANK_FIELDS = (
    "legal_name",
    "clinician_name",
    "clinician_title",
    "address",
    "phone",
    "fax",
    "email",
    "website",
    "meet",
    "ein",
    "npi",
)

_FORBIDDEN_FRAGMENTS = (
    "1881910081",
    "036.132941",
    "TW-00062",
    "86-1977816",
    "773-312",
    "drmattbrown",
    "psycharts",
    "matt@",
    "meet.drmattbrown",
)


def test_practice_defaults_are_blank() -> None:
    assert practice.DEFAULTS["display_name"] == "Example Practice"
    for key in _BLANK_FIELDS:
        assert practice.DEFAULTS[key] == "", f"{key} should be blank in DEFAULTS"
    assert practice.DEFAULT_LICENSES == []
    blob = str(practice.DEFAULTS) + str(practice.DEFAULT_LICENSES)
    for frag in _FORBIDDEN_FRAGMENTS:
        assert frag not in blob, f"forbidden fragment in shipped defaults: {frag}"
