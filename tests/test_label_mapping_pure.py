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

import workflow  # noqa: E402
from adapter import NextcloudDeckPlatform, validate_deck_config  # noqa: E402

_CUSTOM_TITLE = "ARCH003-CUSTOM-TITLE"


def _config_with_mapping() -> SimpleNamespace:
    """Valide Config mit einem custom label_mapping (eindeutiger Titel)."""
    return SimpleNamespace(
        extra={
            "base_url": "https://cloud.example.org",
            "username": "hermes",
            "app_password": "secret",
            "hermes_user_id": "hermes",
            "label_mapping": {
                "phase:plan": {"title": _CUSTOM_TITLE, "color": "123456"},
            },
        }
    )


class _SaveLabelState:
    """Save/Restore des modul-globalen Friendly-Label-State (FRIENDLY_LABELS,
    LABEL_ALIASES, Signatur-Guard). Das label_mapping ist modul-global und wird
    beim Anwenden mutiert — ohne Save/Restore würden diese Tests andere Tests
    (z. B. test_workflow_gates, die das Default-Mapping erwarten) polluten."""

    def _save_labels(self) -> None:
        self._saved_labels = dict(workflow.FRIENDLY_LABELS)
        self._saved_sig = workflow._last_label_mapping_signature

    def _restore_labels(self) -> None:
        workflow.FRIENDLY_LABELS.clear()
        workflow.FRIENDLY_LABELS.update(self._saved_labels)
        workflow._rebuild_aliases()
        workflow._last_label_mapping_signature = self._saved_sig


class TestValidateConfigIsPure(_SaveLabelState, unittest.TestCase):
    """ARCH-003: validate_deck_config() (Read-Pfad: Gateway-Config-Validierung,
    Dashboard-Status-Polls) darf das globale Friendly-Label-Mapping NICHT
    mutieren. Die Mutation ist an die Adapter-Konstruktion gebunden."""

    def setUp(self):
        self._save_labels()

    def tearDown(self):
        self._restore_labels()

    def test_validate_config_does_not_mutate_global_labels(self):
        before = workflow.FRIENDLY_LABELS.get("phase:plan", (None, None))
        valid = validate_deck_config(_config_with_mapping())
        self.assertTrue(valid, "config mit allen Credentials sollte valide sein")
        after = workflow.FRIENDLY_LABELS.get("phase:plan", (None, None))
        self.assertEqual(
            before,
            after,
            "validate_deck_config darf FRIENDLY_LABELS nicht mutieren",
        )


class TestAdapterInitAppliesMapping(_SaveLabelState, unittest.TestCase):
    """ARCH-003: Der Live-Adapter (Konstruktor) wendet das label_mapping an —
    d. h. die Custom-Titel erscheinen im globalen FRIENDLY_LABELS (kein
    Regress: die Anwendung existiert weiterhin, nur am Live-Pfad)."""

    def setUp(self):
        self._save_labels()

    def tearDown(self):
        self._restore_labels()

    def test_adapter_construction_applies_mapping(self):
        NextcloudDeckPlatform(_config_with_mapping())
        self.assertEqual(
            workflow.FRIENDLY_LABELS["phase:plan"][0],
            _CUSTOM_TITLE,
            "Adapter-Konstruktor soll das label_mapping anwenden",
        )
        self.assertEqual(workflow.FRIENDLY_LABELS["phase:plan"][1], "123456")


if __name__ == "__main__":
    unittest.main()
