# Health Endpoint Security and Compatibility Design

**Status:** Design only — no route, middleware, response, Docker, or deployment changes are made by this document.

## Why this review is required

The current `GET /api/health` handler returns `{"status": "ok"}` plus the complete `service.status()` and market-limits monitor status. The service status includes substantially more than process liveness:

- Active provider names, configured provider state, and provider usage/coverage.
- Per-provider key-pool status objects.
- Configured sports, regions, and bookmaker selections.
- Snapshot freshness, counts, and last-update timestamps.
- Provider, refresh, simulation, player-prop, and alert error/status details.
- Alert enablement, delivery counts, and last-send/check timestamps.
- Refresh scheduling and interval information.

The current middleware deliberately exempts this endpoint from Basic Auth so Docker/orchestrator health checks continue to work. Existing tests also rely on this endpoint remaining available when legacy Basic Auth is partially configured. Removing or renaming it without a compatibility plan could mark a healthy container unhealthy or lock out operator diagnostics.

## Target contract

### Public liveness: keep `GET /api/health`

The public endpoint should eventually return a deliberately small, stable response:

```json
{"status": "ok"}
```

Requirements:

- No provider names, key-pool status, provider usage, configuration, exception text, event data, account data, or delivery diagnostics.
- No secret values, credential fragments, or internal paths.
- No external provider calls and no expensive scans.
- A successful response means the web process can serve requests. It must not imply that every odds provider is configured or that market data is current.
- Preserve the `status` field and HTTP success semantics expected by the existing health probe.

Whether this is pure process liveness or includes a minimal local dependency check must be decided from the actual container health-check contract before implementation. A provider outage must not cause a restart loop.

### Protected operational diagnostics

After administrator authentication and authorization are wired and proven, expose detailed diagnostics through a separately classified administrator-only route (proposed: `GET /api/admin/diagnostics`). This is a proposal, not a route addition.

The protected response may include operational fields needed by the existing dashboard/operator workflow, subject to a field-by-field review:

- Provider availability and freshness summaries.
- Refresh and simulation subsystem state.
- Market-limits monitor status.
- Alert delivery health and recent timestamps.
- Sanitized provider errors and usage counters.

It must never return raw API keys, authorization headers, webhook URLs/tokens, MFA material, session tokens, or secret-bearing environment values. Provider key-pool status must be reviewed field by field rather than assumed safe. Error messages must be sanitized before exposure.

### Optional readiness signal

If orchestration later needs readiness distinct from liveness, introduce a separate minimal readiness contract only after inspecting the Compose health check and deployment topology. Readiness must be local and bounded, must not call external providers, and must not expose detailed diagnostics. It must not cause a provider quota outage to restart the container.

## Compatibility and migration gates

1. **Compose contract inspected:** `docker-compose.yml` runs Python `urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=5).read()`. The probe requires a successful HTTP response; it does not parse or depend on the JSON body. Keep the route and successful status code stable. Inspect any external monitors separately before implementation.
2. Add contract tests for the public liveness payload and the protected diagnostics response before changing the handler.
3. Preserve the current detailed `/api/health` behavior while the legacy Basic Auth mode remains active, unless a separately reviewed security emergency requires an explicit operator migration.
4. When identity mode is ready, switch the public endpoint to the minimal response and move detailed diagnostics behind administrator authentication in the same reviewed cutover.
5. Update `app/route_policy.py`, `docs/route-access-matrix.md`, and tests together if a diagnostics route is introduced. Unknown routes must remain unclassified by default.
6. Verify the container health probe, dashboard diagnostics, Basic Auth compatibility, and administrator-only denial/allow behavior in CI before deployment.
7. Do not change production Compose, restart containers, or alter persistent data as part of this design work.

## Explicit non-goals

- No immediate change to `GET /api/health`.
- No new diagnostics route or UI.
- No Basic Auth or identity middleware changes.
- No provider refresh, odds database, market-limits, or alert behavior changes.
- No production configuration or deployment changes.
