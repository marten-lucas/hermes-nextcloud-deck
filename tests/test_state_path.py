import sys
import unittest
from pathlib import Path
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

import adapter as adapter_mod
from adapter import NextcloudDeckPlatform


def _make_adapter() -> NextcloudDeckPlatform:
    """Adapter im Minimal-Setup (kein Netzwerk, keine Board-Konfiguration)."""
    config = SimpleNamespace(
        extra={
            "base_url": "https://cloud.example.org",
            "username": "hermes",
            "app_password": "secret",
            "hermes_user_id": "hermes",
        }
    )
    return NextcloudDeckPlatform(config)


class TestStateFileLocation(unittest.TestCase):
    """OPS-001: Die persistente State-Datei liegt OUTSIDE dem
    Plugin-Source-Verzeichnis (home-scoped unter ~/.hermes) und ist damit
    git-ungeprüft und übersteht Plugin-Updates (git pull)."""

    def test_state_file_outside_plugin_dir(self):
        adapter = _make_adapter()
        # Persistenz ist aktiv (kein In-Memory-Modus mit state_file=None).
        state_path = adapter.state._state_file
        if state_path is None:
            self.fail("state_file sollte gesetzt sein (Persistenz aktiv)")
        state_file = Path(state_path).resolve()
        plugin_dir = Path(adapter_mod.__file__).resolve().parent
        # (a) Nicht im Source-Tree des Plugins.
        self.assertNotIn(plugin_dir, state_file.parents)
        # (b) Home-scoped unter ~/.hermes.
        self.assertTrue(
            str(state_file).startswith(str(Path.home() / ".hermes")),
            f"state file {state_file} should be under ~/.hermes",
        )


if __name__ == "__main__":
    unittest.main()
