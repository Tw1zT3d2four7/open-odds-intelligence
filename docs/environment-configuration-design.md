# Environment Configuration Manager — Design Draft

Status: Draft for review only. This document does not authorize .env editing, host-level privileges, container recreation, or production restarts.

## 1. Purpose and safety boundary

Provide an MFA-protected Admin Console workflow for managing a deliberately small, reviewed set of environment variables and provider API keys without exposing arbitrary host filesystem or Docker access to the web application.

The manager must distinguish three separate operations:

1. Edit and validate: prepare a proposed configuration version without changing the live host file.
2. Save/apply configuration: publish a validated version to the fixed host .env path through a narrowly scoped host-side mechanism.
3. Restart/reload: perform a separately authorized operation only when required by the changed settings.

A successful save must never imply that the running container has received the new environment. Docker Compose reads .env when a container is created; editing the file alone does not update an already-running process.

## 2. Non-goals

The manager must not provide arbitrary file browsing, upload/download, path selection, shell or arbitrary command execution, user-supplied Compose arguments, Docker socket access, unrestricted environment-variable creation, or direct editing of source/Compose/host configuration. It must not automatically restart, deploy, or roll back production. Secret values must never appear in API responses, browser storage, logs, analytics, or audit metadata.

## 3. Proposed trust boundaries

### Admin Console

- Every page and API is administrator-only, requires an active MFA-authenticated session, and uses same-origin CSRF protection for mutations.
- Require fresh MFA for secret replacement, reveal (if approved), apply, rollback, and restart.
- The browser receives only variable metadata, validation results, masked values, and change status.
- Store staged proposals separately from the live .env file.
- Any staged secret values must be encrypted at rest with a dedicated key stored outside the identity database and outside the managed .env file. Decrypt only in memory for validation/apply; never persist plaintext secret drafts or expose the encryption key through the manager.
- Responses use Cache-Control: no-store; secrets are never placed in HTML, JavaScript, URLs, or client-side persistent storage.

### Application container

- Do not mount the host .env file or /var/run/docker.sock into the application container.
- Do not grant general host filesystem or privileged Docker API access.
- The application may validate a proposal and persist a pending change record, but it must not choose arbitrary host paths or commands.
- Any eventual host integration must use a fixed, narrow protocol and a fixed configuration target.

### Host-side apply mechanism

A host-side helper is the only component permitted to replace the live .env file. Its architecture remains an approval gate.

If selected, it must run outside the application container under a dedicated least-privilege host identity; have one fixed project directory and one fixed .env target; reject symlinks and path traversal; accept only a structured, versioned request containing allowlisted variable names and values; reject unknown operations, variables, paths, service names, and commands; verify a one-time approval/request ID, expected current configuration hash, expiry, and replay state; expose no generic filesystem or Docker API; and return only an operation ID and sanitized status.

Important trade-off: a helper with permission to replace .env or control Docker is a privileged boundary. A compromised application could attempt to abuse any helper it can reach. The final design must decide whether applying a file change requires separate host-operator approval, a local-only socket with additional authorization, or another independently reviewed mechanism. Do not solve this by mounting the Docker socket into the app container.

## 4. Variable allowlist and metadata

Each supported variable needs a reviewed schema entry containing its exact case-sensitive name, purpose/owner, type and validation rules, required/optional status, defaults, length/character constraints, secret status, restart/reload classification, safe provider-test availability, and concurrency behavior.

Do not permit arbitrary variable creation by default. Adding a variable requires a code-reviewed allowlist/schema change.

Provider API keys and other secret values must be preserved byte-for-byte after explicitly defined input normalization. Never lowercase, uppercase, or case-fold secret material. Reject NUL bytes and disallowed line breaks. Do not silently trim meaningful secret characters.

## 5. Edit, validate, and save workflow

1. Show only allowlisted settings, descriptions, configured/unconfigured state, masked secret indicators, and restart requirements.
2. Secrets are write-only by default: replace rather than retrieve. If controlled reveal is retained, require fresh MFA, an explicit reveal action, a short-lived response, no-store headers, and a secret-reveal audit event.
3. Keep edits in a draft outside the live .env file.
4. Validate the complete candidate configuration: dotenv syntax/quoting, duplicate and unknown keys, types/enums/ranges/lengths/URLs, cross-setting constraints, and provider-specific checks where safe.
5. Provider tests use proposed values in memory, have bounded timeouts, and return sanitized result codes. Never expose headers, credentials, raw provider bodies, or exception text.
6. If any validation fails, reject the proposal and leave the live .env and last known-good backup untouched.
7. If validation succeeds, persist a pending version with a hash, actor, timestamp, changed variable names, validation summary, and restart classification. Do not write the live file yet.
8. Before apply, compare the expected live-file hash to the current hash. If it changed since draft creation, stop and require reload/reconciliation rather than overwriting another operator's work.

## 6. Atomic publication and recovery

If the host-side apply mechanism is approved:

- Open the fixed target without following symlinks and verify it is a regular file owned by the expected host identity.
- Create a temporary file in the same directory with restrictive permissions (0600).
- Write the complete validated candidate, flush and fsync it, and verify its hash.
- Create a recoverable backup of the current live file first. Backups contain secrets and must be encrypted at rest with a host-protected key, as well as kept in a protected directory with restrictive permissions.
- Atomically replace the target with os.replace (or equivalent same-filesystem operation), then fsync the parent directory.
- Re-read and verify the published file and hash before reporting success.
- On failure, leave the previous file intact or restore the verified backup.

Rollback is a separate audited operation selecting a known-good version by opaque ID. It must not accept an arbitrary path or upload. Backup retention/deletion requires a separately approved policy; do not silently purge backups.

## 7. Apply, restart, and health verification

- Saving a validated draft and applying it to the host file are separate actions.
- Show changed variable names (never values), affected subsystems, and whether each change requires process restart or container recreation.
- Never restart automatically after save.
- Restart is a separate explicit action with fresh MFA, CSRF, an impact warning, and a reason code.
- The first release should prefer a documented operator-controlled restart rather than granting the application Docker control.
- Any future fixed restart helper must allow only named Compose services and a fixed project directory; no free-form arguments or commands.
- After restart, verify container health and application readiness. If checks fail, report a sanitized failure and offer audited rollback; never loop automatic restarts.
- Do not expose restart controls until the host-side mechanism, authorization boundary, and rollback procedure pass a non-production drill.

## 8. Audit and observability

Record secret-free events for draft creation/discard, validation result, save/apply, secret replacement/reveal (if approved), backup/rollback, restart request/result, actor, timestamp, request/version ID, changed variable names, and result.

Never record values, partial secret values, API-key prefixes, request headers, provider response bodies, or raw dotenv lines. Apply/restart actions require controlled reason codes. Audit retention remains subject to the separately approved audit-retention policy.

## 9. Required tests before implementation

- Unknown variables, duplicate keys, malformed dotenv, invalid types, and cross-setting conflicts are rejected.
- Invalid proposals never modify the live file or backup.
- Case-sensitive secrets are preserved exactly and never appear in responses, HTML, browser storage, logs, audit metadata, or test failure output.
- Symlink targets, path traversal, non-regular files, wrong ownership, and permission errors fail closed.
- Concurrent drafts cannot overwrite a newer live-file hash.
- Interrupted writes cannot leave a partial live file.
- Backup publication is atomic and rollback restores exact previous bytes.
- Provider timeouts and sanitized failures do not leak credentials.
- Save does not restart; restart requires separate fresh authorization.
- Restart failure is detected and recovery is explicit.
- The app container has no Docker socket and no host .env mount.
- The host-helper protocol rejects arbitrary commands, paths, variables, and replayed requests.

## 10. Decisions required before implementation

- Confirm the exact initial variable allowlist and secret variables.
- Confirm write-only secrets versus controlled reveal.
- Choose the host-side apply architecture and whether host-operator approval is required.
- Decide whether the first release stages proposals only or may atomically replace the live .env.
- Decide which services may be restarted and whether restart remains operator-controlled.
- Approve backup location, access, and retention/deletion policy.
- Approve provider-test behavior, timeout limits, and external provider access.
- Complete a non-production apply, restart, health-check, and rollback drill.

Until these decisions are approved, this manager remains design-only. No .env file, host process, container, Docker service, or production setting is changed by this design.
