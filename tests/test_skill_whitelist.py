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


class TestSkillRegistrationWhitelist(unittest.TestCase):
    """ARCH-001: register() registriert NUR die zum Plugin gehörenden Skills
    (explizite Whitelist `adapter_mod.PLUGIN_SKILLS`) und NICHT das fachlich
    unverwandte 'openwisp-template-update', das ebenfalls im skills/-Verzeichnis
    liegt — sonst koppelt das Plugin den Skill-Namensraum an eine fremde Domäne."""

    def test_only_whitelisted_skills_registered(self):
        ctx = MagicMock()
        adapter_mod.register(ctx)
        registered = [call.args[0] for call in ctx.register_skill.call_args_list]
        self.assertIn("nextcloud-deck", registered)
        self.assertNotIn("openwisp-template-update", registered)
        # Exakt die Whitelist, nichts anderes.
        self.assertEqual(sorted(registered), sorted(adapter_mod.PLUGIN_SKILLS))

    def test_whitelist_contains_only_plugin_skill(self):
        self.assertEqual(adapter_mod.PLUGIN_SKILLS, ("nextcloud-deck",))


if __name__ == "__main__":
    unittest.main()
