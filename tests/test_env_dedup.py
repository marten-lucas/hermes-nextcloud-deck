import os
import sys
import unittest
from types import SimpleNamespace

# Clear any cached gateway imports to ensure adapter uses its fallback classes
for _mod in (
    "gateway",
    "gateway.config",
    "gateway.platforms",
    "gateway.platforms.base",
):
    if _mod in sys.modules:
        del sys.modules[_mod]

import adapter as adapter_mod  # noqa: E402

_ENV_KEYS = (
    "NEXTCLOUD_DECK_MAX_IN_PROGRESS",
    "NEXTCLOUD_DECK_TEMPLATE_LANGUAGE",
    "NEXTCLOUD_DECK_BACKLOG_FORMAT_QUIET_SECONDS",
)


def _config(extra=None):
    return SimpleNamespace(extra=extra if extra is not None else {})


class TestEnvDedup(unittest.TestCase):
    """FUNC-001: Die drei Config-Optionen, deren ``_env(...)``-Fallback-Ketten
    zuvor einen *doppelten* (identischen) Variablennamen enthielten — z. B.
    ``_env("NEXTCLOUD_DECK_MAX_IN_PROGRESS", "NEXTCLOUD_DECK_MAX_IN_PROGRESS")``
    — lösen weiterhin korrekt auf. Der Cleanup (Entfernen der toten Dublette)
    war behavior-neutral: Primärname > Extra > Default bleibt unverändert."""

    def setUp(self):
        self._saved_env = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
        self._saved_dotenv_cache = adapter_mod._DOTENV_CACHE
        adapter_mod._DOTENV_CACHE = None

    def tearDown(self):
        adapter_mod._DOTENV_CACHE = self._saved_dotenv_cache
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_defaults_when_unset(self):
        cfg = adapter_mod._build_runtime_config(_config())
        self.assertEqual(cfg.max_in_progress, 0)
        self.assertEqual(cfg.template_language, "de")
        self.assertEqual(cfg.backlog_format_quiet_seconds, 300.0)

    def test_env_primary_name_resolves(self):
        os.environ["NEXTCLOUD_DECK_MAX_IN_PROGRESS"] = "3"
        os.environ["NEXTCLOUD_DECK_TEMPLATE_LANGUAGE"] = "en"
        os.environ["NEXTCLOUD_DECK_BACKLOG_FORMAT_QUIET_SECONDS"] = "120"
        cfg = adapter_mod._build_runtime_config(_config())
        self.assertEqual(cfg.max_in_progress, 3)
        self.assertEqual(cfg.template_language, "en")
        self.assertEqual(cfg.backlog_format_quiet_seconds, 120.0)

    def test_extra_overrides_env(self):
        os.environ["NEXTCLOUD_DECK_MAX_IN_PROGRESS"] = "9"
        os.environ["NEXTCLOUD_DECK_TEMPLATE_LANGUAGE"] = "en"
        cfg = adapter_mod._build_runtime_config(_config({
            "max_in_progress": 2,
            "template_language": "de",
        }))
        self.assertEqual(cfg.max_in_progress, 2)
        self.assertEqual(cfg.template_language, "de")


if __name__ == "__main__":
    unittest.main()
