import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Clear any cached gateway imports to ensure adapter uses its fallback classes
for _mod in (
    "gateway",
    "gateway.config",
    "gateway.platforms",
    "gateway.platforms.base",
):
    if _mod in sys.modules:
        del sys.modules[_mod]

import adapter as adapter_mod


class TestParseDotenvLine(unittest.TestCase):
    """SEC-001: Robuster .env-Zeilen-Parser (export-Präfix, Kommentare,
    Anführungszeichen) statt naivem partition/strip."""

    def test_export_prefix(self):
        self.assertEqual(adapter_mod._parse_dotenv_line("export FOO=bar"), ("FOO", "bar"))

    def test_inline_comment_unquoted(self):
        self.assertEqual(adapter_mod._parse_dotenv_line("FOO=bar # comment"), ("FOO", "bar"))

    def test_double_quoted(self):
        self.assertEqual(adapter_mod._parse_dotenv_line('FOO="a b"'), ("FOO", "a b"))

    def test_single_quoted(self):
        self.assertEqual(adapter_mod._parse_dotenv_line("FOO='a b'"), ("FOO", "a b"))

    def test_full_line_comment_skipped(self):
        self.assertIsNone(adapter_mod._parse_dotenv_line("# just a comment"))

    def test_blank_skipped(self):
        self.assertIsNone(adapter_mod._parse_dotenv_line("   "))

    def test_no_equals_skipped(self):
        self.assertIsNone(adapter_mod._parse_dotenv_line("JUSTAKEY"))

    def test_empty_key_skipped(self):
        self.assertIsNone(adapter_mod._parse_dotenv_line("=value"))


class TestDotenvCacheInvalidation(unittest.TestCase):
    """SEC-001: Der .env-Cache wird bei MTime-Änderung neu geladen, statt
    den Long-Running-Prozess-Wert für immer zu liefern."""

    def setUp(self):
        adapter_mod._DOTENV_CACHE = None

    def _env_file(self, tmp: str) -> Path:
        hermes_dir = Path(tmp) / ".hermes"
        hermes_dir.mkdir()
        return hermes_dir / ".env"

    def test_mtime_change_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = self._env_file(tmp)
            env_file.write_text("FOO=one\n", encoding="utf-8")
            os.utime(env_file, (1_700_000_000, 1_700_000_000))
            with mock.patch.object(adapter_mod.Path, "home", return_value=Path(tmp)):
                self.assertEqual(adapter_mod._load_dotenv_fallback(), {"FOO": "one"})
                env_file.write_text("FOO=two\n", encoding="utf-8")
                os.utime(env_file, (1_700_010_000, 1_700_010_000))
                self.assertEqual(adapter_mod._load_dotenv_fallback(), {"FOO": "two"})

    def test_cache_holds_when_mtime_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = self._env_file(tmp)
            env_file.write_text("FOO=one\n", encoding="utf-8")
            fixed_mtime = (1_700_000_000, 1_700_000_000)
            os.utime(env_file, fixed_mtime)
            with mock.patch.object(adapter_mod.Path, "home", return_value=Path(tmp)):
                self.assertEqual(adapter_mod._load_dotenv_fallback(), {"FOO": "one"})
                # Inhalt ändern, aber MTime NICHT ändern → Cache soll halten.
                env_file.write_text("FOO=changed-but-cached\n", encoding="utf-8")
                os.utime(env_file, fixed_mtime)
                self.assertEqual(adapter_mod._load_dotenv_fallback(), {"FOO": "one"})


class TestSessionStoreKeys(unittest.TestCase):
    """API-001: Session-Key-Enumeration bevorzugt eine öffentliche
    Enumerierungs-API und fällt nur bei deren Fehlen auf die private
    Struktur `_entries` zurück."""

    def test_public_keys_method_preferred(self):
        class _Store:
            def keys(self):
                return ["a", "b"]

            @property
            def _entries(self):
                raise AssertionError(
                    "_entries must not be consulted when a public API exists"
                )

        self.assertEqual(adapter_mod._session_store_keys(_Store()), ["a", "b"])

    def test_entries_method_preferred(self):
        class _Store:
            def entries(self):
                return {"p": 1, "q": 2}

        self.assertEqual(sorted(adapter_mod._session_store_keys(_Store()) or []), ["p", "q"])

    def test_falls_back_to_private_entries(self):
        class _Store:
            _entries = {"x": 1, "y": 2}

        self.assertEqual(sorted(adapter_mod._session_store_keys(_Store()) or []), ["x", "y"])

    def test_no_keys_returns_none(self):
        class _Store:
            pass

        self.assertIsNone(adapter_mod._session_store_keys(_Store()))


if __name__ == "__main__":
    unittest.main()
