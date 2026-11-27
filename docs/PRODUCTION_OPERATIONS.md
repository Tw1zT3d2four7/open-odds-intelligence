# Production Operations Runbook

This runbook covers safe operation of a **single-owner, self-hosted** Open Odds Intelligence deployment. It does not certify the application as a multi-tenant subscription service. Complete the commercial-readiness gates below before accepting subscribers or exposing the application to the public internet.

## 1. Persistent data

Compose mounts `./data` at `/app/data`. The default SQLite database is `./data/odds_history.sqlite3`; fitted simulator model files and other local state may also live under `./data`.

Keep these items together for recovery:
- The complete `data/` directory, including SQLite and fitted simulator artifacts.
- The private `.env` file, stored separately in an encrypted secret store.
- The exact Compose file and deployed image version/digest.

**Never use `docker compose down -v` as an update or troubleshooting step.** Do not delete or replace `./data` or `.env` during an image update.

## 2. Create a consistent backup

Run from the directory containing `docker-compose.yml`. SQLite's online backup API is safer than copying a live database file while writes may be occurring.

```bash
mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
sqlite3 ./data/odds_history.sqlite3 ".backup 'backups/odds_history-$stamp.sqlite3'"
tar -czf "backups/app-data-$stamp.tar.gz" data
sha256sum "backups/odds_history-$stamp.sqlite3" "backups/app-data-$stamp.tar.gz" > "backups/SHA256SUMS-$stamp"
```

If the `sqlite3` command-line utility is unavailable, use a maintenance window: stop the service, copy the complete `data/` directory, then start it again.

```bash
docker compose stop
tar -czf "backups/app-data-$stamp.tar.gz" data
docker compose start
```

Protect backups as sensitive operational data. They contain odds history and provider-derived records subject to provider terms. Store at least one encrypted copy on a different physical system. A backup that has never been restored is not a verified backup.

Recommended minimum: daily backup, retain 7 daily and 4 weekly copies, and periodically test restoration to a separate directory. Retention is an operator responsibility; this release does not automatically prune backups.

## 3. Verify a backup

```bash
sha256sum -c backups/SHA256SUMS-<timestamp>
sqlite3 backups/odds_history-<timestamp>.sqlite3 'PRAGMA quick_check;'
```

The integrity check should return `ok`. Keep the matching checksum file with the backup.

## 4. Restore data safely

Restoration replaces local database state. Confirm the target directory and preserve the current state before proceeding.

```bash
docker compose stop
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mv data "data.before-restore-$stamp"
mkdir -p data
tar -xzf backups/app-data-<timestamp>.tar.gz
sqlite3 data/odds_history.sqlite3 'PRAGMA quick_check;'
docker compose up -d
docker compose ps
```

If restoring the standalone SQLite backup instead of the full data archive, copy it into the restored `data/` directory as `odds_history.sqlite3`. Restore any simulator model artifact from the same backup set. Do not combine a database from one backup date with model artifacts from another without checking compatibility.

If health checks fail, stop the container and preserve the failed state for diagnosis. Do not repeatedly delete the database or initialize a blank one.

## 5. Safe image updates and rollback

Before every update:
1. Create and verify a backup.
2. Record the currently deployed image digest and application health.
3. Review release notes and confirm the new image is intended for this deployment.
4. Pull and recreate the service without removing volumes.

```bash
docker compose images
docker inspect --format='{{index .RepoDigests 0}}' open-odds-intelligence
docker compose pull
docker compose up -d
docker compose ps
docker logs --since=10m open-odds-intelligence
```

Check the dashboard and `/api/health`, confirm each configured league's feed freshness, and verify history remains present.

For rollback, set the Compose image to the previously recorded immutable image digest (not an unpinned `latest` tag), then run `docker compose up -d`. If the new version performed a database migration that is not backward-compatible, restore the pre-update backup too; do not assume an older image can safely read a newer schema.

## 6. Health and feed checks

A healthy container only means the process responds to its health probe. It does **not** prove that a provider is authorized, that a league has current events, or that every requested market is populated.

After deployment and during normal operations:
- Check `docker compose ps` and recent container logs.
- Check `GET /api/health` for provider status, last refresh, errors, and per-sport freshness.
- Check each configured league separately; a fresh NFL feed does not prove MLB or another league is fresh.
- Compare event counts, start times, sportsbook coverage, market keys, lines, and prices with provider data.
- Monitor provider quota headers and account usage after refreshes.
- Confirm SQLite and backup sizes are plausible and disk space is sufficient.

Do not repeatedly click refresh to clear an error. A 401/403 requires credential or entitlement correction. A 429 means stop and respect provider quota and retry deadlines.

### Optional final-score collection and corrections

Final-score synchronization is disabled unless `SIMULATION_RESULTS_SYNC_ENABLED=true`. It uses TheRundown score-only requests (`affiliate_ids=0`) for events already present in the local odds archive. These requests still consume provider allowance. Keep `SIMULATION_RESULTS_RECHECK_DAYS=0` unless you specifically want recent completed dates checked again for provider corrections. A positive value is capped at seven days and adds score-only date lookups; it does not trigger odds refreshes. A stored score is replaced only when the provider supplies a strictly newer timezone-aware `source_updated_at` value and the event identity, teams, and start time still match the archived quote. Missing or stale timestamps never overwrite a stored settlement.


## 7. Security boundary of the current release

Optional HTTP Basic authentication protects routes with one configured username/password pair. It is useful for a trusted, single-owner deployment, but it is **not** subscriber account management:
- There are no individual subscriber identities, tenant isolation, roles, per-user sessions, or per-user API quotas.
- There is no subscription billing, entitlement enforcement, customer lifecycle, or self-service account recovery.
- A shared Basic-auth credential cannot safely represent multiple paying customers or support revocation for one customer.
- `/api/health` is intentionally unauthenticated for container health checks; restrict exposure at the reverse proxy or network layer if needed.
- Provider credentials and database files belong only on the server. Never expose them in browser code, logs, screenshots, support tickets, or source control.

**Unmerged feature-branch note:** individual subscriber/admin identity, session controls, billing persistence, and guarded Stripe test-mode Checkout/Customer Portal are being developed on `feature/admin-panel-and-subscriptions`. These changes have not been merged or deployed; they do not describe the currently running single-owner release. The feature branch keeps live billing rejected and hosted-billing switches off by default. See [the development roadmap](roadmap.md), [the Stripe test-mode runbook](stripe-test-mode-acceptance.md), and [the subscription policy proposal](subscription-lifecycle-policy-proposal.md). Do not enable the feature branch's billing switches in a production environment.

Before remote access, place the service behind a maintained TLS reverse proxy, use HTTPS, restrict inbound ports, keep the host and Docker patched, and ensure proxy logs do not record Authorization headers. Use a unique long password. Do not expose the application directly to the public internet over plain HTTP.

## 8. Commercial launch gates

Do not enable paid subscribers until all of the following are designed, implemented, tested, and reviewed:
- **Provider licensing:** written confirmation that each odds/results/limits source permits intended commercial display, derived data, caching, and redistribution. A paid API plan alone is not necessarily a display license.
- **Identity and tenancy:** individual accounts, secure password/session handling, account recovery, authorization checks on every private API route, and isolation of customer data and preferences.
- **Billing and entitlements:** payment processor integration, subscription synchronization, plan limits, cancellations, failed-payment handling, refunds, taxes, and auditable entitlement checks. Never store raw payment-card details.
- **Abuse and quota controls:** per-account and global request limits, refresh scheduling, provider quota budgets, backpressure, and protection against one customer exhausting a shared provider allowance.
- **Operational resilience:** automated encrypted off-host backups, tested restore procedures, monitoring/alerting, error reporting without secrets, disk/database growth monitoring, and incident response.
- **Security review:** TLS, secret rotation, dependency and image scanning, least-privilege deployment, CSRF/session protections if cookie auth is introduced, rate limiting, and independent security review.
- **Product validation:** documented market coverage and freshness, clear stale/missing-data states, simulator calibration evidence per league, and accurate uncertainty disclosure. Passing CI is not proof of profitable picks or model accuracy.
- **Legal and customer disclosures:** provider attribution/terms, privacy policy, terms of service, retention policy, refund/cancellation policy, and jurisdiction-specific review.

## 9. Operational incident checklist

When data appears wrong or the application becomes unavailable:
1. Record the time, affected league, dashboard state, and relevant error text. Redact API keys, Authorization headers, and Discord webhook URLs.
2. Check container health, logs, disk space, provider status, and quota before restarting.
3. Determine whether the issue is provider-side, league/market coverage, stale local snapshots, or application failure.
4. Avoid repeated refreshes during authorization, quota, or provider outage errors.
5. Back up the database before schema repair, manual edits, or rollback.
6. Preserve logs and a database copy before destructive recovery.

This runbook is deliberately conservative: preserving quote history and provider quota is more important than forcing a refresh or creating a clean-looking but empty database.
