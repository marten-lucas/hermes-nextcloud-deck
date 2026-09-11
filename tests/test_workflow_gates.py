import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from workflow import (
    APPROVAL_APPROVED,
    APPROVAL_REQUIRED,
    PHASE_EXECUTE,
    PHASE_PLAN,
    STATUS_BACKLOG,
    STATUS_DONE,
    STATUS_REVIEW,
    STATUS_RUNNING,
    DeckWorkflowContext,
    build_capabilities_prompt,
    check_agent_label_gate,
    check_agent_status_gate,
    check_destructive_gate,
    compile_destructive_patterns,
    extract_hermes_labels,
    is_backlog_stack,
    is_destructive_tool,
    parse_subtasks,
    analyze_board_suitability,
    resolve_stack_id_for_status,
)
from adapter import (
    NextcloudDeckPlatform,
    DECK_CARD_ACTION_SCHEMA,
    _make_deck_card_action_handler,
)


class TestDeckCardActionTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import adapter as adapter_mod
        self._prev_ref = adapter_mod._LIVE_ADAPTER_REF
        self.config = SimpleNamespace(
            extra={
                "base_url": "https://cloud.example.org",
                "username": "hermes",
                "app_password": "secret",
                "hermes_user_id": "hermes",
                "boards": [{"board_id": "7"}],
            }
        )
        self.adapter = NextcloudDeckPlatform(self.config)
        self.adapter.client = MagicMock()
        adapter_mod._LIVE_ADAPTER_REF = self.adapter

    def tearDown(self):
        import adapter as adapter_mod
        adapter_mod._LIVE_ADAPTER_REF = self._prev_ref

    async def test_handler_requires_card_id(self):
        handler = _make_deck_card_action_handler()
        result = await handler({"target_status": "review"})
        self.assertIsInstance(result, str)
        self.assertIn("card_id", result)

    async def test_handler_builds_metadata_and_calls_send(self):
        self.adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))
        handler = _make_deck_card_action_handler()
        result = await handler({
            "card_id": "42",
            "target_status": "review",
            "description": "# Plan",
            "assign_labels": ["hermes/approval:required"],
        })
        self.assertIsInstance(result, str)
        self.assertIn('"success": true', result)
        self.adapter.send.assert_called_once()
        metadata = self.adapter.send.call_args[1]["metadata"]
        self.assertEqual(metadata["target_status"], "review")
        self.assertEqual(metadata["description"], "# Plan")
        self.assertEqual(metadata["assign_labels"], ["hermes/approval:required"])
        # Kein comment übergeben -> content ist leer
        self.assertEqual(self.adapter.send.call_args[1]["content"], "")

    async def test_handler_passes_comment_as_content(self):
        self.adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))
        handler = _make_deck_card_action_handler()
        result = await handler({
            "card_id": "42",
            "target_status": "review",
            "comment": "Plan fertig, bitte abnehmen",
        })
        self.assertIn('"success": true', result)
        self.adapter.send.assert_called_once()
        self.assertEqual(
            self.adapter.send.call_args[1]["content"],
            "Plan fertig, bitte abnehmen",
        )
        self.assertEqual(self.adapter.send.call_args[1]["metadata"]["target_status"], "review")

    def test_schema_declares_expected_fields(self):
        props = DECK_CARD_ACTION_SCHEMA["parameters"]["properties"]
        for field in ("card_id", "target_status", "description", "assign_labels", "remove_labels", "assign_user", "unassign_user", "comment"):
            self.assertIn(field, props)
        self.assertEqual(DECK_CARD_ACTION_SCHEMA["parameters"]["required"], ["card_id"])


class TestSetupTestCard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            extra={
                "base_url": "https://cloud.example.org",
                "username": "hermes",
                "app_password": "secret",
                "hermes_user_id": "hermes",
                "boards": [{"board_id": "7"}],
            }
        )
        self.adapter = NextcloudDeckPlatform(self.config)
        self.adapter.client = MagicMock()

    async def test_setup_test_card_creates_and_assigns(self):
        self.adapter.client.get_stacks = AsyncMock(return_value=[
            {"id": "39", "title": "Todo"},
        ])
        self.adapter.client.create_card = AsyncMock(return_value={"id": "123"})
        self.adapter._apply_label_to_card = AsyncMock(return_value=(True, None))
        self.adapter._assign_user_to_card = AsyncMock(return_value=True)

        card_id = await self.adapter.setup_test_card(
            board_id="7",
            title="Test Karte",
            description="# Objective",
            stack_title="Todo",
            label_titles=["hermes/risk:low"],
            assignee="ki_assistent",
        )
        self.assertEqual(card_id, "123")
        self.adapter.client.create_card.assert_called_once()
        self.adapter._apply_label_to_card.assert_called_once_with("123", "hermes/risk:low")
        self.adapter._assign_user_to_card.assert_called_once_with("123", "ki_assistent")

    async def test_setup_test_card_stack_not_found(self):
        self.adapter.client.get_stacks = AsyncMock(return_value=[
            {"id": "39", "title": "Todo"},
        ])
        card_id = await self.adapter.setup_test_card(
            board_id="7",
            title="Test",
            stack_title="GibtEsNicht",
        )
        self.assertIsNone(card_id)
        self.adapter.client.create_card.assert_not_called()


class TestWorkflowLogic(unittest.TestCase):
    def test_parse_subtasks(self):
        desc = (
            "# Task\n\n"
            "- [ ] 1. Schritt eins\n"
            "- [x] 2. Schritt zwei erledigt\n"
            "- [X] 3. Schritt drei erledigt\n"
            "* [ ] 4. Schritt vier\n"
            "Normaler Text ohne Checkbox"
        )
        progress = parse_subtasks(desc)
        self.assertEqual(progress.total, 4)
        self.assertEqual(progress.completed, 2)
        self.assertEqual(progress.percentage, 50)
        self.assertIn("2/4 Subtasks erledigt (50%)", progress.summary())

    def test_parse_subtasks_empty(self):
        progress = parse_subtasks("")
        self.assertEqual(progress.total, 0)
        self.assertEqual(progress.completed, 0)
        self.assertEqual(progress.summary(), "Keine Subtasks definiert")

    def test_extract_hermes_labels(self):
        card = {
            "labels": [
                {"title": "hermes/phase:plan"},
                {"title": "hermes/type:implementation"},
                {"title": "hermes/risk:high"},
                {"title": "hermes/approval:required"},
                {"title": "normal-label"},
            ]
        }
        extracted = extract_hermes_labels(card)
        self.assertEqual(extracted.get("phase"), "plan")
        self.assertEqual(extracted.get("type"), "implementation")
        self.assertEqual(extracted.get("risk"), "high")
        self.assertEqual(extracted.get("approval"), "required")

    def test_extract_hermes_labels_friendly(self):
        """Friendly-Titel werden auf kanonische Keys gemappt."""
        card = {
            "labels": [
                {"title": "⚠️ Risiko: Hoch"},
                {"title": "⌛ Freigabe nötig"},
                {"title": "🚀 In Umsetzung"},
                {"title": "💡 Planung"},  # letzte Phase gewinnt
            ]
        }
        extracted = extract_hermes_labels(card)
        self.assertEqual(extracted.get("phase"), "plan")
        self.assertEqual(extracted.get("risk"), "high")
        self.assertEqual(extracted.get("approval"), "required")

    def test_canonical_label_key_variants(self):
        """Alle Schreibweisen lösen auf denselben kanonischen Key auf."""
        from workflow import canonical_label_key, friendly_label_title
        for variant in [
            "hermes/approval:required",
            "approval:required",
            "⌛ Freigabe nötig",
            "freigabe nötig",
        ]:
            self.assertEqual(canonical_label_key(variant), "approval:required", variant)
        self.assertEqual(canonical_label_key("💡 Planung"), "phase:plan")
        self.assertEqual(canonical_label_key("Unbekanntes Label"), None)
        self.assertEqual(friendly_label_title("phase:plan"), "💡 Planung")
        self.assertEqual(friendly_label_title("unbekannt"), "unbekannt")

    def test_label_gate_accepts_friendly_and_canonical(self):
        """Label-Gate blockt Selbst-Freigabe in beiden Schreibweisen."""
        for variant in ["hermes/approval:approved", "approval:approved", "✔️ Freigabe erteilt"]:
            allowed, reason = check_agent_label_gate(variant, "plan")
            self.assertFalse(allowed, variant)
            self.assertIn("Gate 1", reason)

    def test_configure_friendly_labels_from_config(self):
        """Label-Mapping wird dynamisch aus der Config geladen und angewendet."""
        import workflow as workflow_mod
        from workflow import canonical_label_key, friendly_label_title

        original = dict(workflow_mod.FRIENDLY_LABELS)
        try:
            workflow_mod.configure_friendly_labels({
                "phase:plan": {"title": "🎯 Eigenes Planung", "color": "ABC123"},
                "risk:high": ["🆘 Kritisch", "FF0000"],  # kompakte Listen-Form
            })
            self.assertEqual(friendly_label_title("phase:plan"), "🎯 Eigenes Planung")
            self.assertEqual(workflow_mod.FRIENDLY_LABELS["phase:plan"][1], "ABC123")
            self.assertEqual(friendly_label_title("risk:high"), "🆘 Kritisch")
            # Nicht genannte Keys behalten das Default-Mapping
            self.assertEqual(friendly_label_title("approval:required"), "⌛ Freigabe nötig")
            # Aliase werden mit neu gebaut
            self.assertEqual(canonical_label_key("🎯 Eigenes Planung"), "phase:plan")
            self.assertEqual(canonical_label_key("🆘 Kritisch"), "risk:high")
            # Ungültige Einträge werden ignoriert
            workflow_mod.configure_friendly_labels({"phase:plan": {"title": "", "color": ""}})
            self.assertEqual(friendly_label_title("phase:plan"), "🎯 Eigenes Planung")
        finally:
            # Original-Mapping wiederherstellen (kein Test-Pollution)
            workflow_mod.FRIENDLY_LABELS.clear()
            workflow_mod.FRIENDLY_LABELS.update(original)
            workflow_mod._rebuild_aliases()

    def test_has_workflow_label_change(self):
        """Workflow-Label-Änderungen triggern; irrelevante Labels nicht."""
        from workflow import has_workflow_label_change, workflow_label_keys
        # Freigabe erteilen → Trigger
        self.assertTrue(has_workflow_label_change(
            ["💡 Planung", "⌛ Freigabe nötig"],
            ["💡 Planung", "✔️ Freigabe erteilt"],
        ))
        # Irrelevantes Label ("Später") → kein Trigger
        self.assertFalse(has_workflow_label_change(
            ["💡 Planung"],
            ["💡 Planung", "Später"],
        ))
        # Unverändert → kein Trigger
        self.assertFalse(has_workflow_label_change(["💡 Planung"], ["💡 Planung"]))
        # workflow_label_keys filtert auf kanonische Keys
        self.assertEqual(workflow_label_keys(["💡 Planung", "Später"]), {"phase:plan"})

    def test_description_matches_template(self):
        """Format-Prüfung erkennt valide vs. unausgefüllte/leere Descriptions."""
        from workflow import description_matches_template, TEMPLATE_DESCRIPTION

        # Roh-Vorlage mit Platzhaltern → NICHT gültig
        self.assertFalse(description_matches_template(TEMPLATE_DESCRIPTION))

        # Leer → nicht gültig
        self.assertFalse(description_matches_template(""))
        self.assertFalse(description_matches_template("  "))

        # Valide ausgefüllte Karte
        valid = (
            "# Objective\nTermin ermitteln\n\n"
            "# Context\nTest\n\n"
            "# Acceptance Criteria\n- [x] Datum ermittelt\n\n"
            "# Plan\n## Subtasks\n- [x] 1. Datum holen\n\n"
            "# Verification\ndate ausgeführt\n\n"
            "# Result\nErledigt"
        )
        self.assertTrue(description_matches_template(valid))

        # Fehlende Kernabschnitte → nicht gültig
        self.assertFalse(description_matches_template("# Objective\nNur Ziel ohne Plan\n- [ ] x"))

    def test_is_backlog_stack_by_title_and_config(self):
        self.assertTrue(is_backlog_stack({"title": "Backlog"}))
        self.assertTrue(is_backlog_stack({"title": "Ideen"}))
        self.assertFalse(is_backlog_stack({"title": "In Bearbeitung"}))

        config = {"stack_mapping": {"backlog": "99"}}
        self.assertTrue(is_backlog_stack({"id": 99, "title": "Custom Stack"}, config))
        self.assertFalse(is_backlog_stack({"id": 100, "title": "Custom Stack"}, config))

    def test_status_gate_done_blocked(self):
        allowed, reason = check_agent_status_gate(STATUS_DONE, PHASE_EXECUTE, APPROVAL_APPROVED)
        self.assertFalse(allowed)
        self.assertIn("Gate 2", reason)

        allowed, reason = check_agent_status_gate("erledigt", PHASE_EXECUTE)
        self.assertFalse(allowed)

    def test_status_gate_backlog_blocked(self):
        allowed, reason = check_agent_status_gate(STATUS_BACKLOG, PHASE_PLAN)
        self.assertFalse(allowed)

    def test_status_gate_plan_to_running_without_approval_blocked(self):
        # Gate 1: Agent in 'plan' darf nicht nach 'running' ohne approval —
        # aber nur bei implementation-Typ (oder risk >= medium)
        allowed, reason = check_agent_status_gate(
            STATUS_RUNNING, PHASE_PLAN, APPROVAL_REQUIRED, task_type="implementation"
        )
        self.assertFalse(allowed)
        self.assertIn("Gate 1", reason)

        # Mit approval erlaubt
        allowed, reason = check_agent_status_gate(
            STATUS_RUNNING, PHASE_PLAN, APPROVAL_APPROVED, task_type="implementation"
        )
        self.assertTrue(allowed)

        # Verschieben nach review ist in der Plan-Phase immer erlaubt (für Approval-Anforderung)
        allowed, reason = check_agent_status_gate(STATUS_REVIEW, PHASE_PLAN, APPROVAL_REQUIRED)
        self.assertTrue(allowed)

    def test_status_gate_progressive_autonomy_low_risk_no_approval(self):
        # documentation + risk:low -> Gate 1 ist NICHT verpflichtend, Agent darf eigenständig
        allowed, reason = check_agent_status_gate(
            STATUS_RUNNING, PHASE_PLAN, APPROVAL_REQUIRED, task_type="documentation", risk="low"
        )
        self.assertTrue(allowed, msg=reason)

        allowed, reason = check_agent_status_gate(
            STATUS_RUNNING, PHASE_PLAN, None, task_type="troubleshooting", risk="low"
        )
        self.assertTrue(allowed, msg=reason)

    def test_status_gate_high_risk_requires_approval_even_low_type(self):
        # risk:high erzwingt Gate 1 unabhängig vom Typ
        allowed, reason = check_agent_status_gate(
            STATUS_RUNNING, PHASE_PLAN, APPROVAL_REQUIRED, task_type="documentation", risk="high"
        )
        self.assertFalse(allowed)
        self.assertIn("Gate 1", reason)

    def test_gate1_is_mandatory(self):
        from workflow import gate1_is_mandatory

        self.assertTrue(gate1_is_mandatory("implementation", "low"))
        self.assertTrue(gate1_is_mandatory("documentation", "high"))
        self.assertTrue(gate1_is_mandatory(None, "medium"))
        self.assertFalse(gate1_is_mandatory("documentation", "low"))
        self.assertFalse(gate1_is_mandatory("troubleshooting", None))
        self.assertFalse(gate1_is_mandatory("research", "low"))

    def test_is_destructive_tool(self):
        patterns = compile_destructive_patterns()
        self.assertTrue(is_destructive_tool("delete_user", patterns))
        self.assertTrue(is_destructive_tool("restart_service", patterns))
        self.assertTrue(is_destructive_tool("auth_reset_password", patterns))
        self.assertFalse(is_destructive_tool("get_user", patterns))
        self.assertFalse(is_destructive_tool("read_logs", patterns))
        self.assertFalse(is_destructive_tool("", patterns))

    def test_destructive_gate_blocks_high_risk(self):
        patterns = compile_destructive_patterns()
        ctx = DeckWorkflowContext(board_id="7", card_id="42", phase=PHASE_EXECUTE, risk="high")
        allowed, reason = check_destructive_gate("delete_user", ctx, patterns)
        self.assertFalse(allowed)
        self.assertIn("Gate 3", reason)

    def test_destructive_gate_allows_low_risk(self):
        patterns = compile_destructive_patterns()
        ctx = DeckWorkflowContext(board_id="7", card_id="42", phase=PHASE_EXECUTE, risk="low")
        allowed, reason = check_destructive_gate("delete_user", ctx, patterns)
        self.assertTrue(allowed, msg=reason)

    def test_destructive_gate_allows_non_destructive_on_high_risk(self):
        patterns = compile_destructive_patterns()
        ctx = DeckWorkflowContext(board_id="7", card_id="42", phase=PHASE_EXECUTE, risk="high")
        allowed, reason = check_destructive_gate("get_user", ctx, patterns)
        self.assertTrue(allowed, msg=reason)

    def test_destructive_gate_no_context(self):
        patterns = compile_destructive_patterns()
        allowed, reason = check_destructive_gate("delete_user", None, patterns)
        self.assertTrue(allowed, msg=reason)

    # --- Board Suitability ---

    def _stacks(self, titles):
        return [{"id": str(i + 1), "title": t} for i, t in enumerate(titles)]

    def test_suitability_exact_canonical_columns(self):
        stacks = self._stacks(["Backlog", "Triage", "Todo", "Ready", "Running", "Review", "Blocked", "Done"])
        result = analyze_board_suitability("12", "Board12", stacks)
        self.assertTrue(result.is_suitable)
        self.assertEqual(result.missing_required, [])

    def test_suitability_missing_blocked_column(self):
        # Board ohne "Blocked" -> Pflicht-Spalte fehlt
        stacks = self._stacks(["Backlog", "To Do", "In Progress", "In Review", "Done"])
        result = analyze_board_suitability("12", "Board12", stacks)
        self.assertFalse(result.is_suitable)
        self.assertIn("blocked", result.missing_required)
        # "To Do" matcht nicht exakt "todo", "In Progress" nicht "running"
        self.assertIn("running", result.missing_required)
        self.assertIn("review", result.missing_required)

    def test_suitability_mapping_resolves_non_canonical_titles(self):
        # Mapping macht nicht-kanonische Spalten-Titel auflösbar
        stacks = self._stacks(["Backlog", "To Do", "In Progress", "In Review", "Blocked", "Done"])
        config = {"stack_mapping": {
            "todo": "To Do",
            "running": "In Progress",
            "review": "In Review",
            "blocked": "Blocked",
            "done": "Done",
        }}
        result = analyze_board_suitability("12", "Board12", stacks, config)
        self.assertTrue(result.is_suitable, msg=result.format_report())

    def test_resolve_stack_id_by_mapping_id(self):
        stacks = [{"id": "99", "title": "Custom Running"}, {"id": "7", "title": "Whatever"}]
        config = {"stack_mapping": {"running": "99"}}
        self.assertEqual(resolve_stack_id_for_status("running", stacks, config), "99")

    def test_resolve_stack_id_by_title_fallback(self):
        stacks = [{"id": "42", "title": "Running"}]
        self.assertEqual(resolve_stack_id_for_status("running", stacks, None), "42")

    def test_label_gate_prevent_self_approval(self):
        allowed, reason = check_agent_label_gate("hermes/approval:approved", PHASE_PLAN)
        self.assertFalse(allowed)
        self.assertIn("Gate 1", reason)

        allowed, reason = check_agent_label_gate(
            "hermes/phase:execute", PHASE_PLAN, approval_status=None, task_type="implementation"
        )
        self.assertFalse(allowed)

        allowed, reason = check_agent_label_gate(
            "hermes/phase:execute", PHASE_PLAN, approval_status=APPROVAL_APPROVED, task_type="implementation"
        )
        self.assertTrue(allowed)

        # progressive Autonomy: documentation risk:low darf selbst auf execute wechseln
        allowed, reason = check_agent_label_gate(
            "hermes/phase:execute", PHASE_PLAN, approval_status=None, task_type="documentation", risk="low"
        )
        self.assertTrue(allowed)

    def test_capabilities_prompt_plan_mode(self):
        prompt = build_capabilities_prompt(
            phase=PHASE_PLAN,
            task_type="implementation",
            risk="high",
            approval=APPROVAL_REQUIRED,
        )
        self.assertIn("MODUS: PLAN / KONZEPTION (READ-ONLY)", prompt)
        self.assertIn("KEINE produktiven Änderungen", prompt)
        self.assertIn("HIGH RISK", prompt)

    def test_capabilities_prompt_execute_mode(self):
        prompt = build_capabilities_prompt(
            phase=PHASE_EXECUTE,
            task_type="documentation",
            risk="low",
            approval=APPROVAL_APPROVED,
        )
        self.assertIn("MODUS: EXECUTE / UMSETZUNG", prompt)
        self.assertIn("PLAN CHANGE REQUESTED", prompt)


class TestAdapterWorkflowIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            extra={
                "base_url": "https://cloud.example.org",
                "username": "hermes",
                "app_password": "secret",
                "hermes_user_id": "hermes",
                "boards": [{"board_id": "7", "stack_mapping": {"todo": "1", "running": "2", "review": "3", "done": "4"}}],
            }
        )
        self.adapter = NextcloudDeckPlatform(self.config)
        self.adapter.client = MagicMock()

    async def test_send_gate_prevents_move_to_done(self):
        self.adapter._locate_card = AsyncMock(return_value=("7", "2"))
        self.adapter.client.get_card = AsyncMock(return_value={"labels": [{"title": "hermes/phase:execute"}]})
        self.adapter.client.add_comment = AsyncMock(return_value={"id": 123})

        result = await self.adapter.send(
            chat_id="deck:board:7:card:42",
            content="",
            metadata={"target_status": "done"},
        )
        self.assertFalse(result.success)
        self.assertIn("Gate 2", result.error)
        self.adapter.client.add_comment.assert_called_once()
        self.assertIn("Gate 2", self.adapter.client.add_comment.call_args[0][1])

    async def test_send_allows_valid_move_to_review(self):
        # _locate_card: 1. Aufruf (vor Move) → Quell-Stack 2; danach (Verifikation) → Ziel-Stack 3
        self.adapter._locate_card = AsyncMock(side_effect=[("7", "2"), ("7", "3")])
        self.adapter.client.get_card = AsyncMock(return_value={"labels": [{"title": "hermes/phase:plan"}]})
        self.adapter._resolve_target_stack_id = AsyncMock(return_value="3")
        self.adapter.client.move_card = AsyncMock(return_value={"id": 42})
        self.adapter.client.add_comment = AsyncMock(return_value={"id": 100})

        result = await self.adapter.send(
            chat_id="deck:board:7:card:42",
            content="Plan fertig",
            metadata={"target_status": "review"},
        )
        self.assertTrue(result.success)
        self.adapter.client.move_card.assert_called_once_with("7", "2", "42", "3")

    async def test_send_move_verification_failure_detected(self):
        """Move-Verifikation: stiller API-Fehlschlag (Karte nicht im Ziel-Stack) wird erkannt."""
        self.adapter._locate_card = AsyncMock(return_value=("7", "2"))  # bleibt immer im Quell-Stack
        self.adapter.client.get_card = AsyncMock(return_value={"labels": [{"title": "hermes/phase:plan"}]})
        self.adapter._resolve_target_stack_id = AsyncMock(return_value="3")
        self.adapter.client.move_card = AsyncMock(return_value={"id": 42})
        self.adapter.client.add_comment = AsyncMock(return_value={"id": 100})

        result = await self.adapter.send(
            chat_id="deck:board:7:card:42",
            content="Plan fertig",
            metadata={"target_status": "review"},
        )
        self.assertFalse(result.success)
        self.assertIn("silent API failure", result.error)

    async def test_send_auto_moves_to_review_on_description_without_target_status(self):
        """b-Fix: Beschreibung ohne target_status → deterministischer Auto-Move nach review."""
        # 1. Aufruf: Auto-Move-Check (Quell-Stack 1); 2.: Move selbst; 3.: Verifikation (Ziel 3);
        # weitere Aufrufe (z. B. Re-Baseline) → Ziel-Stack 3
        self.adapter._locate_card = AsyncMock(side_effect=[("7", "1"), ("7", "1"), ("7", "3"), ("7", "3")])
        # Karte in 'todo' (Stack 1), Phase plan, low risk → Auto-Move erlaubt
        self.adapter.client.get_card = AsyncMock(return_value={
            "labels": [{"title": "hermes/phase:plan"}, {"title": "hermes/risk:low"}]
        })
        self.adapter.client.get_stacks = AsyncMock(return_value=[
            {"id": "1", "title": "Todo"},
            {"id": "3", "title": "Review"},
        ])
        self.adapter._resolve_target_stack_id = AsyncMock(return_value="3")
        self.adapter.client.move_card = AsyncMock(return_value={"id": 42})
        self.adapter.client.update_card = AsyncMock(return_value={"id": 42})
        self.adapter.client.add_comment = AsyncMock(return_value={"id": 100})

        result = await self.adapter.send(
            chat_id="deck:board:7:card:42",
            content="",
            metadata={"description": "# Plan\n- [ ] Schritt 1"},
        )
        self.assertTrue(result.success)
        self.adapter.client.move_card.assert_called_once_with("7", "1", "42", "3")
        self.adapter.client.update_card.assert_called_once()

    async def test_send_no_auto_move_when_card_already_in_review(self):
        """Auto-Move darf nicht greifen, wenn die Karte schon in review/blocked/done liegt."""
        self.adapter._locate_card = AsyncMock(return_value=("7", "3"))
        self.adapter.client.get_card = AsyncMock(return_value={
            "labels": [{"title": "hermes/phase:plan"}]
        })
        self.adapter.client.get_stacks = AsyncMock(return_value=[
            {"id": "3", "title": "Review"},
        ])
        self.adapter.client.update_card = AsyncMock(return_value={"id": 42})
        self.adapter.client.move_card = AsyncMock(return_value={"id": 42})
        self.adapter.client.add_comment = AsyncMock(return_value={"id": 100})

        result = await self.adapter.send(
            chat_id="deck:board:7:card:42",
            content="",
            metadata={"description": "# Plan aktualisiert"},
        )
        self.assertTrue(result.success)
        self.adapter.client.move_card.assert_not_called()
        self.adapter.client.update_card.assert_called_once()


if __name__ == "__main__":
    unittest.main()
