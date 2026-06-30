"""
Tests for the pure parsing functions in scraper.py.
No browser, no network, no Telegram needed.
"""

import sys
import os

# Allow importing from the project root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Patch out config so tests don't need a real .env
import types
_fake_config = types.ModuleType("config")
_fake_config.LOGIN_URL      = ""
_fake_config.TICKET_URL     = ""
_fake_config.WEB_USER       = ""
_fake_config.WEB_PASS       = ""
_fake_config.SEDE_IDX       = 1
_fake_config.SERVICIO_IDX   = 2
_fake_config.MAX_CONCURRENT = 2
_fake_config.HEADLESS       = True
sys.modules["config"] = _fake_config

from scraper import parse_message, parse_button


# ── parse_message ─────────────────────────────────────────────────────────────

class TestParseMessage:
    def test_only_code(self):
        assert parse_message("385") == ("385", None, None)

    def test_leading_trailing_spaces(self):
        assert parse_message("  385  ") == ("385", None, None)

    def test_nivel_turno_long_form(self):
        assert parse_message("385 nivel1 turno2") == ("385", 1, 2)

    def test_nivel_turno_short_form(self):
        assert parse_message("385 n2 t1") == ("385", 2, 1)

    def test_nivel_turno_with_spaces(self):
        assert parse_message("385 nivel 1 turno 2") == ("385", 1, 2)

    def test_case_insensitive(self):
        assert parse_message("385 NIVEL1 TURNO2") == ("385", 1, 2)

    def test_invalid_returns_none_tuple(self):
        assert parse_message("hola") == (None, None, None)

    def test_empty_string(self):
        assert parse_message("") == (None, None, None)

    def test_partial_format_no_turno(self):
        # "385 nivel1" without turno is invalid
        assert parse_message("385 nivel1") == (None, None, None)


# ── parse_button ──────────────────────────────────────────────────────────────

class TestParseButton:
    def test_standard_format(self):
        result = parse_button("Nivel 2 - Turno 1 (8 disponibles)")
        assert result == {"nivel": 2, "turno": 1, "disponibles": 8}

    def test_short_labels(self):
        result = parse_button("N2 T1 Cant:8")
        assert result["nivel"] == 2
        assert result["turno"] == 1
        assert result["disponibles"] == 8

    def test_colon_format(self):
        result = parse_button("Nivel:2 Turno:1 Disp:8")
        assert result["nivel"] == 2
        assert result["turno"] == 1
        assert result["disponibles"] == 8

    def test_zero_disponibles(self):
        result = parse_button("Nivel 1 - Turno 3 (0 disponibles)")
        assert result["disponibles"] == 0

    def test_cupos_keyword(self):
        result = parse_button("Nivel 1 Turno 2 - 5 cupos")
        assert result["disponibles"] == 5

    def test_missing_disponibles_falls_back_to_last_number(self):
        result = parse_button("Nivel 1 Turno 2")
        # No availability number → fallback extracts last digit found
        assert result["disponibles"] == 2  # last number in text
