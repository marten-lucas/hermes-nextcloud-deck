from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Lazy-Import des hermes-x-on-behalf-Pakets (optional installiert)
_xonbehalf = None


def _get_xonbehalf():
    """Lädt das hermes-x-on-behalf-Paket, falls verfügbar (optional dependency)."""
    global _xonbehalf
    if _xonbehalf is not None:
        return _xonbehalf
    try:
        import hermes_x_on_behalf

        _xonbehalf = hermes_x_on_behalf
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
                _xonbehalf = importlib.import_module("hermes_x_on_behalf")
            else:
                return None
        except Exception as exc:
            logger.debug(f"hermes-x-on-behalf nicht verfügbar: {exc}")
            return None
    return _xonbehalf


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

    def __init__(
        self,
        bot_user_id: str,
        client: Any = None,
        cache_ttl_seconds: int = 120,
        bot_aliases: Iterable[str] = (),
    ):
        self.bot_user_id = str(bot_user_id or "").strip()
        self.bot_aliases = {str(a).strip().lower() for a in bot_aliases if str(a).strip()}
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
    ) -> Tuple[str, List[str], bool]:
        """Ermittelt den Actor im Namen dessen Hermes handelt.

        Priorität: Kommentar-Autor (echter Mensch) → Fallback-User.
        Ist der Bot selbst letzter Autor, wird der Fallback-User verwendet,
        damit Hermes nie "als sich selbst" handelt.

        Returns (actor_id, groups, is_fallback): is_fallback=True markiert
        System-/Fallback-Actors — diese werden zu kind=system-Principals
        (niemals Personal-/Team-Memory).
        """
        fallback = (
            os.getenv("MCP_IDENTITY_FALLBACK_USER", "").strip()
            or os.getenv("NEXTCLOUD_DECK_USERNAME", "").strip()
            or "system"
        )

        author = str(comment_author).strip() if comment_author else ""
        bot_ids = {self.bot_user_id.lower(), *self.bot_aliases}
        if not author or author.lower() in bot_ids:
            return fallback, [], True

        groups = await self.get_user_groups(author)
        return author, sorted(groups), False

    @staticmethod
    def build_principal(
        user_id: str,
        groups: Iterable[str] = (),
        board_id: Optional[str] = None,
        card_id: Optional[str] = None,
        is_fallback: bool = False,
    ):
        """Baut einen PrincipalContext für ein Deck-Event (Kommentar-Autor als Actor).

        Fallback-/System-Actors (is_fallback=True) werden zu kind=system-
        Principals — sie erhalten niemals Personal- oder Team-Memory.

        Deck-Routing läuft über die explizite conversation_scopes-Liste in der
        Hermes-Konfiguration — Board-Titel werden bewusst nicht für Memory-
        Tags geparst (Titel gehören dem User).
        """
        xob = _get_xonbehalf()
        if xob is None or not user_id:
            return None
        try:
            if is_fallback:
                return xob.PrincipalContext.system(str(user_id))
            conversation_id = None
            if board_id and card_id:
                conversation_id = f"deck:board:{board_id}:card:{card_id}"
            elif board_id:
                conversation_id = f"deck:board:{board_id}"
            return xob.PrincipalContext.interactive(
                user_id=str(user_id),
                groups=groups,
                conversation_id=conversation_id,
                channel="nextcloud-deck",
            )
        except Exception as exc:
            logger.debug(f"Konnte PrincipalContext nicht bauen: {exc}")
            return None

    @staticmethod
    def principal_context(principal):
        """Context-Manager mit Token-basiertem Set/Reset (leak-proof)."""
        xob = _get_xonbehalf()
        return xob.principal_context(principal)

    @staticmethod
    def principal_headers(principal) -> Dict[str, str]:
        """Leitet die Propagation-Header aus dem PrincipalContext ab."""
        xob = _get_xonbehalf()
        if xob is None or principal is None:
            return {}
        try:
            return xob.principal_to_headers(principal)
        except Exception:
            return {}
