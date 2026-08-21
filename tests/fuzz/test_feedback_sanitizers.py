"""Deterministic fuzz/property tests for untrusted feedback text.

No external fuzzer is required here: the important property is that many weird
strings, including control bytes, bidi controls and lone surrogates, always exit
the sanitizers in a form that can be UTF-8 encoded and embedded in the successor
brief.
"""

from __future__ import annotations

import random
import string

from adlc.stages.feedback import clean_inline, clean_text

_DISALLOWED = (
    [chr(i) for i in range(0x20) if i not in (0x09, 0x0A)]
    + [chr(0x7F)]
    + [chr(i) for i in range(0x200B, 0x2010)]
    + [chr(i) for i in range(0x202A, 0x202F)]
    + [chr(i) for i in range(0x2060, 0x2065)]
    + [chr(i) for i in range(0x2066, 0x206A)]
    + ["\ufeff", "\ud800", "\udfff"]
)
_ALPHABET = list(string.ascii_letters + string.digits + " \t\n\r`_-/:.,") + _DISALLOWED


def _weird_text(seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(_ALPHABET) for _ in range(rng.randrange(0, 500)))


def test_clean_text_fuzz_never_emits_unencodable_or_spoofing_characters() -> None:
    for seed in range(300):
        out = clean_text(_weird_text(seed), limit=160)
        out.encode("utf-8")
        assert "\r" not in out
        assert not any("\ud800" <= ch <= "\udfff" for ch in out)
        assert not any("\u200b" <= ch <= "\u200f" for ch in out)
        assert not any("\u202a" <= ch <= "\u202e" for ch in out)
        assert not any("\u2060" <= ch <= "\u2069" for ch in out)
        assert "\ufeff" not in out
        assert all(ch in "\t\n" or ord(ch) >= 0x20 for ch in out)
        assert ord("\x7f") not in [ord(ch) for ch in out]


def test_clean_inline_fuzz_collapses_everything_to_one_prompt_safe_line() -> None:
    for seed in range(300, 600):
        out = clean_inline(_weird_text(seed), limit=96)
        out.encode("utf-8")
        assert "\n" not in out
        assert "\r" not in out
        assert "\t" not in out
        assert "`" not in out
        assert "  " not in out
        assert not any("\ud800" <= ch <= "\udfff" for ch in out)

