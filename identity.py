from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Lazy-Import der ContextVars aus hermes-x-on-behalf (optional installiert)
_xonbehalf_vars: Optional[tuple] = None


def _get_xonbehalf_vars() -> Optional[tuple]:
    """Lädt (current_user_id, current_user_groups) aus hermes-x-on-behalf, falls verfügbar."""
    global _xonbehalf_vars
    if _xonbehalf_vars is not None:
        return _xonbehalf_vars
    try:
        from hermes_x_on_behalf.plugin import current_user_id, current_user_groups
        _xonbehalf_vars = (current_user_id, current_user_groups)
    except Exception:
        try:
            # Fallback: Plugin-Verzeichnis liegt als Schwesterprojekt im Workspace
            import importlib.util, sys, types

            plugin_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "hermes-x-on-behalf",
            )
            if os.path.isdir(plugin_path):
                pkg = types.ModuleType("hermes_x_on_behalf")
                pkg.__path__ = [plugin_path]
                sys.modules.setdefault("hermes_x_on_behalf", pkg)
                plugin_mod = importlib.import_module("hermes_x_on_behalf.plugin")
                _xonbehalf_vars = (plugin_mod.current_user_id, plugin_mod.current_user_groups)
            else:
                _xonbehalf_vars = (None, None)
        except Exception as exc:
            logger.debug(f"hermes-x-on-behalf ContextVars nicht verfügbar: {exc}")
            _xonbehalf_vars = (None, None)
    return _xonbehalf_vars


def _uid_from_assignee(value: Any) -> str:
    if isinstance(value, dict):
        participant = value.get("participant")
        if isinstance(participant, dict):
            user = participant.get("user")
            if isinstance(user, dict) and user.get("uid"):
                return str(user["uid"]).strip()
        for key in ("uid", "user", "userId", "primaryKey"):
            if value.get(key):
                return str(value[key]).strip()
    return str(value).strip() if value else ""


class DeckIdentityResolver:
    """Resolve the actor used for Hermes' execution context."""

    def __init__(self, bot_user_id: str, client: Any = None, cache_ttl_seconds: int = 120):
        self.bot_user_id = str(bot_user_id or "").strip()
        self.client = client
        self.cache_ttl_seconds = cache_ttl_seconds
        self._group_cache: Dict[str, tuple[float, Set[str]]] = {}

    def assigned_uids(self, card_data: Dict[str, Any]) -> List[str]:
        raw = card_data.get("assignedUsers") or card_data.get("assignees") or []
        if not isinstance(raw, list):
            return []
        return [uid for uid in (_uid_from_assignee(v) for v in raw) if uid]

    async def get_user_groups(self, user_id: str) -> Set[str]:
        """Ruft Nextcloud-Gruppen des Users ab (Provisioning API v1, TTL-Cache, graceful fallback)."""
        if not user_id or user_id == self.bot_user_id or self.client is None:
            return set()

        now = time.time()
        if user_id in self._group_cache:
            timestamp, groups = self._group_cache[user_id]
            if now - timestamp < self.cache_ttl_seconds:
                return groups

        try:
            if hasattr(self.client, "cloud_ocs_get"):
                data = await self.client.cloud_ocs_get(f"users/{user_id}/groups")
                groups_list = (
                    data.get("groups", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                )
                groups = set(str(g).strip() for g in groups_list if str(g).strip()) if isinstance(groups_list, (list, set)) else set()
            else:
                groups = set()

            self._group_cache[user_id] = (now, groups)
            return groups

        except Exception as e:
            err_str = str(e)
            status_code = getattr(e, "status_code", None)
            if "998" in err_str or status_code == 998:
                logger.debug(f"User '{user_id}' ist kein regulärer Nextcloud-User (OCS 998).")
                groups = set()
                self._group_cache[user_id] = (now, groups)
                return groups

            logger.warning(f"Konnte Gruppen für User {user_id} nicht abfragen: {e}")
            return set()

    async def resolve_card_actor(
        self,
        card_data: Dict[str, Any],
        comment_author: Optional[str] = None,
    ) -> Tuple[str, List[str]]:
        """Ermittelt den Actor im Namen dessen Hermes handelt.

        Priorität: Kommentar-Autor (echter Mensch) → Fallback-User.
        Ist der Bot selbst letzter Autor, wird der Fallback-User verwendet,
        damit Hermes nie "als sich selbst" handelt.
        """
        fallback = (
            os.getenv("MCP_IDENTITY_FALLBACK_USER", "").strip()
            or os.getenv("NEXTCLOUD_DECK_USERNAME", "").strip()
            or "system"
        )

        author = str(comment_author).strip() if comment_author else ""
        bot_ids = {self.bot_user_id.lower(), "ki_assistent", "ki gerda"}
        if not author or author.lower() in bot_ids:
            return fallback, []

        groups = await self.get_user_groups(author)
        return author, sorted(groups)

    @staticmethod
    def set_contextvars_identity(user_id: str, groups: Iterable[str] = ()) -> None:
        """Setzt die ContextVars von hermes-x-on-behalf für die HTTP-Header-Injektion."""
        vars_pair = _get_xonbehalf_vars()
        if vars_pair is None or vars_pair[0] is None:
            return
        current_user_id, current_user_groups = vars_pair
        try:
            current_user_id.set(str(user_id) if user_id else None)
            current_user_groups.set(",".join(sorted(str(g) for g in groups if str(g).strip())) or None)
        except Exception as exc:
            logger.debug(f"Konnte Identity-ContextVars nicht setzen: {exc}")

    @staticmethod
    def clear_contextvars_identity() -> None:
        """Räumt die Identity-ContextVars auf."""
        vars_pair = _get_xonbehalf_vars()
        if vars_pair is None or vars_pair[0] is None:
            return
        current_user_id, current_user_groups = vars_pair
        try:
            current_user_id.set(None)
            current_user_groups.set(None)
        except Exception as exc:
            logger.debug(f"Konnte Identity-ContextVars nicht leeren: {exc}")
