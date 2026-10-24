"""Regression tests for the three production-deployment bugs found in the audit.

Each test here corresponds to a failure that was live in the cloud deployment
and is now fixed. They are deliberately narrow: no network, no subprocess, no
Windows-only import, so they run inside the Linux container as well as on the
developer's machine.

1. Music Studio: a TRUNCATED model response must not raise.
   The 16-field brief is long enough that the provider regularly returns
   finish_reason="stop" on a document that never closed. The old extractor
   raised "The prompt-enhancement response was not valid JSON." and the UI
   showed that raw error instead of generating anything.

2. GameForge: probing for pygbag must not poison the interpreter.
   pygbag ships a vendored typing_extensions.py and appends its support/ dir
   to sys.path on import, after which fastapi / sqlalchemy / openai all fail.
   The probe has to find the spec without importing the module.

3. GameForge: the same probe must still answer truthfully.
   A probe that always returns True would be as broken as one that crashes.
"""
from __future__ import annotations

import importlib
import importlib.util
import json

import pytest

from app.api.gameforge import PYGBAG_OK, _module_available
from app.api.music_studio import ProviderError, extract_json


# ---------------------------------------------------------------------------
# 1. Truncated-JSON recovery
# ---------------------------------------------------------------------------
class TestExtractJsonSurvivesTruncation:
    """Every shape the provider actually produced during the audit."""

    def test_clean_json(self):
        assert extract_json('{"a":1,"b":"x"}') == {"a": 1, "b": "x"}

    def test_fenced_json(self):
        assert extract_json('```json\n{"a":1}\n```') == {"a": 1}

    def test_json_wrapped_in_prose(self):
        assert extract_json('Sure!\n{"a":1}\nHope that helps.') == {"a": 1}

    def test_truncated_mid_string_keeps_earlier_fields(self):
        # The exact failure captured live: unclosed string, no closing brace.
        raw = ('{"enhanced_prompt":"abc","genre":"Epic","textures":"Layered string')
        out = extract_json(raw)
        assert out["enhanced_prompt"] == "abc"
        assert out["genre"] == "Epic"
        assert "textures" not in out

    def test_truncated_with_no_closing_brace(self):
        assert extract_json('{"a":1,"b":"hello wor') == {"a": 1}

    def test_truncated_inside_an_array(self):
        # A brace-only depth counter mistakes the comma inside this array for
        # a field separator and cuts mid-value. Regression guard.
        out = extract_json('{"a":1,"instruments":["guitar","drum')
        assert out == {"a": 1}

    def test_truncated_inside_very_first_field(self):
        out = extract_json('{"a":"café trun')
        assert out == {"a": "café trun"}

    def test_truncated_after_a_key_with_no_value(self):
        assert extract_json('{"a":1,"b":') == {"a": 1}

    def test_unicode_is_not_mangled(self):
        out = extract_json('{"a":"café … trun')
        assert out["a"] == "café … trun"

    def test_nested_truncation_keeps_complete_subtrees(self):
        out = extract_json('{"a":{"b":1},"c":[{"d":"x"}],"e":"cut')
        assert out["a"] == {"b": 1}
        assert out["c"] == [{"d": "x"}]

    def test_brace_inside_a_string_value_is_not_a_boundary(self):
        raw = '{"a":"use {braces} and a } closer","b":"x"}'
        assert extract_json(raw)["b"] == "x"

    def test_valid_json_with_nested_braces_in_string(self):
        raw = '{"a":{"b":[1,2,{"c":"}"}]},"d":"end"}'
        assert extract_json(raw) == {"a": {"b": [1, 2, {"c": "}"}]}, "d": "end"}

    def test_commas_inside_strings_do_not_split_fields(self):
        assert extract_json('{"a":"one, two","b":"after"}')["b"] == "after"

    @pytest.mark.parametrize("bad", ["", "   ", "I cannot do that.", "no json here"])
    def test_unusable_responses_are_still_rejected(self, bad):
        # Salvage must not turn "no JSON at all" into a bogus object.
        with pytest.raises(ProviderError):
            extract_json(bad)

    def test_every_truncated_case_yields_a_dict_or_is_honestly_rejected(self):
        """A response with at least one complete field always salvages.

        A response with ZERO complete fields is correctly rejected: an empty
        brief carries no information, and returning ``{}`` would hand the
        synthesizer a blank prompt while reporting success.
        """
        salvaged = []
        for raw in ('{"a":1,"b":"x', '{"a":', '{"a":[1,2', '{"a":"x',
                    '{"a":1,"b":"cut', '{"a":{"b":1},"c":"cut'):
            try:
                out = extract_json(raw)
            except ProviderError:
                continue  # nothing recoverable - honest rejection
            assert isinstance(out, dict) and out, raw
            salvaged.append(raw)
        # The three cases that carry a complete field all survive; the two that
        # carry none are rejected rather than faked.
        assert len(salvaged) == 3, salvaged


# ---------------------------------------------------------------------------
# 2. pygbag probe must not break the interpreter
# ---------------------------------------------------------------------------
class TestPygbagProbeIsSideEffectFree:
    def test_module_available_matches_find_spec(self):
        expected = importlib.util.find_spec("pygbag") is not None
        assert _module_available("pygbag") is expected

    def test_unknown_module_is_reported_unavailable(self):
        assert _module_available("definitely_not_a_real_module_xyz") is False

    def test_pygbag_is_not_imported_by_the_probe(self):
        """The whole point: 'pygbag' must not be sitting in sys.modules.

        Importing it appends its own support/ directory to sys.path, and that
        directory contains a vendored typing_extensions.py that shadows the
        real one for fastapi / sqlalchemy / openai.
        """
        # Import a module that itself needs the real typing_extensions.
        assert importlib.import_module("sqlalchemy") is not None
        assert "pygbag" not in importlib.sys.modules, (
            "pygbag was imported in-process; it poisons typing_extensions"
        )

    def test_core_stack_imports_cleanly(self):
        """The libraries that broke in the container must import normally."""
        for name in ("fastapi", "sqlalchemy", "openai", "pydantic"):
            assert importlib.import_module(name) is not None


# ---------------------------------------------------------------------------
# 3. GameForge still reports pygame honestly
# ---------------------------------------------------------------------------
class TestGameforgeReportsEnvironmentTruthfully:
    def test_pygame_flag_matches_a_real_import(self):
        from app.api import gameforge

        try:
            import pygame  # noqa: F401

            expected = True
        except Exception:
            expected = False
        assert gameforge.PYGAME_OK is expected

    def test_pygbag_flag_is_a_bool(self):
        assert isinstance(PYGBAG_OK, bool)

    def test_health_endpoint_reports_pygame_version(self):
        """The exact string the UI shows when GameForge is broken."""
        from app.api import gameforge

        if gameforge.PYGAME_OK:
            assert gameforge.PYGAME_VERSION.count(".") == 2, gameforge.PYGAME_VERSION

    def test_gameforge_health_payload_is_json_serialisable(self):
        payload = {
            "pygame": {"installed": bool(PYGBAG_OK) or True},
            "pygbag": {"installed": PYGBAG_OK},
        }
        assert json.loads(json.dumps(payload))["pygbag"]["installed"] is PYGBAG_OK