import unittest

from state import DeckCardSnapshot


class TestDeckRevisionId(unittest.TestCase):
    def test_revision_id_is_stable_for_duplicate_snapshots(self):
        snapshot = DeckCardSnapshot(
            board_id="1",
            stack_id="10",
            card_id="100",
            title="Test",
            description="Stand A",
            last_comment_id="7",
            last_comment_message="Bitte prüfen",
        )

        self.assertEqual(snapshot.revision_id(), snapshot.revision_id())
        self.assertEqual(snapshot.revision_id(), snapshot.fingerprint())

    def test_new_card_revision_gets_a_new_id(self):
        snapshot = DeckCardSnapshot(
            board_id="1",
            stack_id="10",
            card_id="100",
            title="Test",
            description="Stand A",
            last_comment_id="7",
            last_comment_message="Bitte prüfen",
        )
        revised = DeckCardSnapshot(
            board_id="1",
            stack_id="10",
            card_id="100",
            title="Test",
            description="Stand B",
            last_comment_id="7",
            last_comment_message="Bitte prüfen",
        )

        self.assertNotEqual(snapshot.revision_id(), revised.revision_id())


if __name__ == "__main__":
    unittest.main()
