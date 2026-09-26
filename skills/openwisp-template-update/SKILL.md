---
name: openwisp-template-update
description: Assign, reorder, or replace OpenWISP configuration templates on a device and verify the auto-deployed result.
---

# OpenWISP Template-Update

Use this skill whenever a task requires changing which configuration templates
are assigned to an OpenWISP device (e.g. modularizing firewall rules on a
router), or verifying what OpenWISP knows about a device after a change.

## Core principle

OpenWISP **renders and deploys** the merged configuration itself. The agent
does **not** SSH to the device and does **not** apply anything manually.
The only thing the agent must do is **assign the templates**; deployment is
automatic. Verification happens by reading back OpenWISP's *view* of the
device, not by querying the live device directly.

The trustworthy source of truth is the OpenWISP controller (via the
`openwisp_*` MCP tools), never the raw device.

## Recipe (assign / reorder / replace templates)

1. **Identify the device** — `openwisp_list_devices` (filter by
   `organization`, `search`) or `openwisp_get_device`. Note the device UUID and
   its current `config.templates` list + `config.status`.

2. **Resolve the target templates** — `openwisp_list_templates` /
   `openwisp_get_template` to obtain the template UUIDs. You want the FULL
   ordered list that should be assigned.

3. **Assign / reorder** — `openwisp_update_device` with `templates` equal to the
   **complete ordered list** of template UUIDs. Order is significant (render /
   merge order). Passing the full list replaces the current assignment, so this
   is how you both unassign (drop a UUID) and reorder (change sequence).

4. **Deployment is automatic** — do not try to SSH or run an apply command.
   OpenWISP triggers rendering and deployment to the device on its own after
   the assignment change.

5. **Verify via the controller** —
   - `openwisp_get_device` → field `config.status` should become `applied`
     (or `modified`/`error` if something is wrong).
   - `openwisp_list_devices` with `status` filter to poll.
   - Trust that OpenWISP deployed correctly *if* `config.status == applied`.

## Reading the merged config (for a snapshot / diff baseline)

- `openwisp_get_device_config` returns a **tar.gz** of the rendered
  configuration. **Extract it** (e.g. with `tar -xzf`) to read the actual
  rendered files — this is how you diff against a previous state.
- Do **not** treat the tarball as unreadable or binary: it is a normal archive
  and must be unpacked before inspection.
- `openwisp_get_template_configuration` gives the rendered config of a single
  template (same idea).

## Execution caveats

- `openwisp_execute_device_command` exists (custom commands via
  `/controller/device/{id}/command/`), but whether a specific device allows
  command execution is decided by the OpenWISP controller configuration, not by
  the MCP. If commands are disabled, fall back to
  `openwisp_get_device_config` + extraction for read-only verification instead
  of treating it as a hard blocker.
- There is no separate "apply/trigger_change" tool; assignment triggers
  deployment automatically. If you need an explicit re-trigger, re-verify via
  `config.status` first before assuming a new tool is required.