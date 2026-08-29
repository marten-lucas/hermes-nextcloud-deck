# Nextcloud Collaboration & Identity Guardrails

When working through Nextcloud Talk or Deck:

1. Treat the current Hermes session identity as authoritative.
2. Never ask the user to provide their Nextcloud user ID for authorization.
3. Never put X-On-Behalf-Of or X-User-Groups into tool arguments.
4. Downstream authorization (MCP / APIs) is determined purely by the runtime identity propagated by Hermes.
5. Use the current user's permissions rather than the Hermes bot's administrative permissions.
6. When an MCP tool is unavailable due to RBAC, do not attempt to bypass authorization by changing tool arguments or guessing another user.
7. When working with Deck:
   - Identify the current work item/card first.
   - Preserve existing card content.
   - Use explicit status mappings.
   - Do not overwrite unrelated description content.
8. When modifying a Deck card, prefer the smallest possible mutation.
9. When adding a comment, clearly distinguish agent-generated content from user-authored content.
10. For persistent memory:
    - Use Honcho for user-specific preferences and long-lived context.
    - Do not store secrets, credentials, access tokens, or authorization headers.
    - Never use another user's memory to answer the current user's request.
11. If the current identity is ambiguous, do not guess. Ask for clarification.