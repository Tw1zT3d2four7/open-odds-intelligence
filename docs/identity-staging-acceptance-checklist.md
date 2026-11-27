# Identity Mode — Non-Production Acceptance Checklist

**Status:** Preparation checklist only. No deployment, production cutover, DNS change, or production restart is authorized by this document.

## 0. Staging deployment scaffold

The current `main`/`dev` baseline includes a separate Compose file and a non-secret environment template:

- `docker-compose.staging.yml` uses the isolated `admin-staging` GHCR image tag, a unique container name, a separate `./staging-data` bind mount, and host-loopback-only port `127.0.0.1:18765`.
- `.env.staging.example` is a template only. Copy it to the untracked `.env.staging`, fill secrets using a private secret manager, and set `PUBLIC_BASE_URL` to the real HTTPS staging origin. Never commit `.env.staging`.
- The staging Compose project does not reuse the production `data/` directory, production container name, or production port. Staging data is excluded from Docker build context.

After the merged-baseline image publication workflow succeeds and the isolation/configuration checks below are complete, an operator can start the staging container with:

```bash
cp .env.staging.example .env.staging
chmod 600 .env.staging
install -d -m 700 staging-data
docker compose -f docker-compose.staging.yml pull
docker compose -f docker-compose.staging.yml up -d
docker compose -f docker-compose.staging.yml ps
curl --fail http://127.0.0.1:18765/api/health
```

Do not run these commands against production. The port is deliberately bound to loopback; expose staging remotely only through a separately configured HTTPS reverse proxy after the peer topology and trusted-proxy behavior have been verified. Do not enable Stripe Checkout/Portal, external odds polling, Discord, SMS, or real email delivery until their individual staging credentials and acceptance steps are ready.

## 1. Isolation requirements

Before testing, create a genuinely separate staging deployment:

- Use a separate Compose project name, container names, network, ports, and persistent volumes.
- Use a separate identity database and separate staging-only secrets. Never point staging at production SQLite files or volumes.
- Do not copy production subscriber records, password hashes, sessions, MFA seeds, recovery codes, or email challenges into staging.
- Keep production Compose files, environment files, Caddy site configuration, DNS, containers, and volumes unchanged.
- Use a staging hostname or LAN-only port that does not alter the production domain. The Admin Console remains at /admin under the staging origin; it does not need a separate admin domain.
- Use a fake email sender or provider sandbox. Do not send real registration or password-reset messages during initial acceptance.
- Keep Discord and SMS delivery disabled. No optional alert channel should be active in staging until its own provider and consent design is approved.

## 2. Configuration and startup checks

- Confirm the staging environment explicitly selects the intended AUTH_MODE.
- Confirm the default legacy_basic mode still starts without initializing the identity database on ordinary product requests.
- Confirm invalid AUTH_MODE values fail configuration loading.
- Confirm missing or malformed identity secrets fail closed without printing secret values.
- Confirm the staging identity database is created with restrictive permissions and that WAL/SHM sidecars are also restricted.
- Confirm the application container has no host .env bind mount and no Docker socket.
- Confirm the health endpoint remains available to the orchestrator and exposes only the documented minimal payload in identity mode.
- Confirm the application logs contain no API keys, session bearer tokens, passwords, MFA seeds, recovery codes, or challenge tokens.

## 3. TLS and reverse-proxy checks

Reproduce the intended deployment shape without changing production Caddy:

- Browser-to-proxy traffic uses HTTPS.
- Proxy-to-app traffic uses the intended private network path.
- The proxy preserves the Host and Origin behavior expected by same-origin CSRF validation.
- Secure host-only subscriber, administrator, and CSRF cookies are accepted by the browser over HTTPS.
- Confirm the app's observed peer address and forwarded-header behavior before configuring trusted proxy CIDRs.
- Do not trust X-Forwarded-For or similar headers merely because a request contains them. Configure only the exact proxy addresses/CIDRs verified from the staging network.
- Confirm untrusted clients cannot spoof the rate-limit client address through forwarded headers.

Record the observed proxy-to-app topology and actual source address before proposing any trusted proxy configuration.

## 4. Subscriber acceptance

Using disposable staging identities and the fake email sender:

- Register a subscriber and confirm the account remains unverified until the one-time link is consumed.
- Verify the link is single-use, expires, and cannot be replayed.
- Confirm login fails before verification and succeeds after verification.
- Confirm subscriber and administrator cookies are separate and cannot cross-authorize.
- Confirm account and feature-access pages require a subscriber session and return no-store headers.
- Confirm password reset uses generic responses, consumes a one-time challenge, changes the password, and revokes every active session.
- Confirm authenticated password change requires the current password and CSRF, applies rate limits, invalidates outstanding reset challenges, revokes all sessions, and clears local cookies.
- Confirm the account page uses a fresh script nonce and does not render untrusted values as HTML.
- Confirm logout revokes the server-side session and clears cookies.

## 5. Free and premium provider-data acceptance

Use synthetic provider fixtures first. Do not purchase a provider plan or enable live premium polling as part of the initial staging drill.

The staging compose file is isolated on `127.0.0.1:18765`, uses `.env.staging` and `staging-data/`, and pulls the `:admin-staging` image. The feature-branch image workflow now publishes `:admin-staging`; the `:release` alias is reserved for an actual GitHub release event. A prior feature-branch build updated `:release` before this guard was corrected; that was an image publication only, not a deployment. Treat the existing `:release` alias as untrusted until a future official release rebuilds it, and use `:admin-staging` for staging.

- Confirm free-tier operation with no premium provider credentials configured and verify that no premium provider requests are attempted.
- Configure explicit provider account groups with `ODDS_PROVIDER_ACCOUNT_ACCESS_CLASSES` (for example, `the_odds_api|free-account=free,the_odds_api|premium-account=premium`). Keep `ODDS_PREMIUM_PROVIDER_POLLING_ENABLED=false`; verify only free groups can be leased by scheduled and manual refreshes.
- Configure an optional reserve floor with `ODDS_PROVIDER_ACCOUNT_QUOTA_FLOORS` (for example, `the_odds_api|premium-account=credits_remaining:250`). Verify the Admin Overview shows the configured floor without exposing credentials.
- Confirm a free subscriber receives only free-class quotes, sizes, derived market calculations, history, movement, market limits, and CLV. Confirm a paid subscriber with the explicit `premium_data` entitlement receives free plus premium data when premium polling is separately enabled for the controlled test.
- Use fixtures where free and premium account groups quote the same bookmaker/market/selection at different timestamps. Verify free and premium entries remain separately stored, free views remain free-only, and paid views can use the permitted combined dataset without duplicate sportsbook counting.
- Verify complete market calculations never combine opposite outcomes from different account groups. Verify size/limit provenance follows the account that supplied the size data, not automatically the account that supplied the odds.
- Refresh one same-tier account group while another group remains stored; verify only the leased group is replaced and the other group's quote history, movement, and CLV provenance remain intact.
- Drive provider-reported remaining quota to the configured floor. Confirm every key in that group enters cooldown, other groups remain available, a successful response does not clear the cooldown, and the cooldown survives a service restart.
- Confirm cache keys, pagination, event provenance, and provider diagnostics do not reveal premium data or premium provider details to free accounts.
- Confirm downgrade, cancellation, complimentary expiry, and explicit entitlement revocation immediately remove premium access, including history, movement, market limits, and CLV.
- Confirm missing account groups, unlisted groups, and legacy quote/CLV/market-limit rows without trustworthy account provenance remain restricted.
- Confirm global Discord alerts in identity mode never include premium-only data.
- Confirm provider request schedules and quota accounting are platform-controlled rather than triggered by individual subscriber reads.
- Confirm Admin Overview displays account groups, source classes, usage counters, configured quota floors, and reset state without exposing API keys, tokens, or authorization values.

The premium polling switch remains disabled after the test. Record actual provider plan terms, quota metrics, and pricing separately before approving any production premium ingestion. See `docs/provider-data-access-isolation.md` for the detailed provider-tier checklist.

## 6. Administrator acceptance

Using a staging-only administrator provisioned through the private bootstrap process:

- Confirm password plus TOTP is required; verify a single-use recovery code works only once.
- Confirm public registration cannot create or promote an administrator.
- Confirm direct access to /admin without an active MFA-authenticated administrator session is denied.
- Confirm a subscriber cookie cannot open the Admin Console or call any admin API.
- Confirm every mutating admin endpoint requires same-origin CSRF.
- Confirm subscriber suspension/reactivation, individual session revocation, subscriber-wide session revocation, and feature-grant changes require MFA-backed admin authorization and controlled reason codes.
- Confirm the current administrator session cannot be revoked through the session inventory.
- Confirm admin audit history is bounded, filtered, actor-attributed, and free of email addresses, credentials, and bearer tokens.
- Confirm all identity/admin responses are non-cacheable and private pages use a restrictive per-response CSP nonce.

## 7. Route and authorization acceptance

- Verify every registered application and framework route appears in the explicit route-policy inventory.
- Exercise parameterized routes with real-looking disposable IDs: subscriber status, subscriber-wide session revocation, and individual session revocation.
- Confirm those routes reach their administrator dependency in identity mode rather than being treated as unknown paths.
- Confirm the same identity routes are hidden in legacy_basic mode.
- Confirm unknown paths and unsupported methods remain fail-closed.
- Confirm HEAD requests inherit the corresponding GET access policy without creating an authorization bypass.
- Confirm product feature access defaults to deny and a subscriber without the required grant receives 403.
- Confirm storage failures return sanitized 503 responses and do not fall back to permissive access.

## 8. Email queue and rate-limit acceptance

- Confirm transactional email is queued and the HTTP request does not wait for provider delivery.
- Confirm queue payloads and recipient data are encrypted at rest.
- Confirm idempotency keys prevent duplicate sends after retries.
- Confirm transient provider errors retry with bounded backoff; permanent failures stop retrying.
- Confirm the provider circuit breaker opens and half-open concurrency remains bounded.
- Confirm expired or consumed challenges are not delivered by the worker.
- Confirm IP and account/destination rate limits are enforced using the actual staging peer topology.
- Confirm responses remain generic for known and unknown subscriber addresses.

## 9. Backup and restore drill

Use only the isolated staging identity database:

- Create an encrypted backup and verify restrictive file permissions.
- Interrupt a backup publication in a controlled test and confirm no partial final backup remains.
- Verify an existing backup is never overwritten.
- Restore only through the documented offline procedure.
- Confirm an encrypted pre-restore snapshot is created before replacement.
- Verify schema-version validation rejects unsupported or malformed backups.
- Confirm subscriber/admin sessions and one-time challenges behave safely after restore.
- Record the backup, restore, and key-recovery results without exposing encryption keys or backup contents.

Do not run this drill against a production volume.

## 10. Exit criteria

Staging acceptance is complete only when:

- The full automated CI suite is green.
- All subscriber, administrator, CSRF, cookie, route-policy, and failure-path checks above pass.
- The actual TLS/reverse-proxy topology is documented and trusted proxy configuration is justified by observed network facts.
- Backup, restore, and key-recovery procedures have been exercised successfully on staging.
- Logs and responses have been reviewed for secret leakage.
- The operator explicitly reviews the results and separately approves any future production cutover.

Until these criteria are met, AUTH_MODE=identity remains staging-only. No production mode switch, container recreation, data migration, DNS change, or restart is implied.
