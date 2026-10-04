import sys
import unittest
from unittest.mock import MagicMock

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


class TestPlatformKey(unittest.TestCase):
    """ARCH-002: Der Laufzeit-Plattform-Key ist bewusst `deck` und weicht vom
    Manifest-Namen `nextcloud-deck-platform` ab. Das ist die dokumentierte
    Hermes-Plattform-Plugin-Konvention (Kürzel = Plattform-Key, `<kürzel>-platform`
    = Manifest-Name). Dieser Test fixiert den Plattform-Key, damit er nicht
    versehentlich auf den Manifest-Namen „normalisiert" wird — das würde das
    Laufzeit-Verhalten (Config-Key, `<PLATFORM>_HOME_CHANNEL` = `DECK_HOME_CHANNEL`,
    Routing) brechen."""

    def test_register_uses_platform_key_deck(self):
        ctx = MagicMock()
        adapter_mod.register(ctx)
        ctx.register_platform.assert_called_once()
        kwargs = ctx.register_platform.call_args.kwargs
        self.assertEqual(kwargs["name"], "deck")
        self.assertEqual(kwargs["label"], "Nextcloud Deck")


if __name__ == "__main__":
    unittest.main()
