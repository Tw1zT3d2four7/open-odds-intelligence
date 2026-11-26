# Local authentication testing

This workflow is for an isolated developer workstation only. It does not change the production deployment and must not be used on a public hostname.

## Configure a local development instance

Use a separate `.env.local` and a separate local test data directory. Do not copy the live accounts database, administrator MFA seed, provider credentials, or production session data into the development instance.

The ready-to-run localhost stack below uses `.env.local.example`. Its no-email settings are:

```dotenv
AUTH_MODE=identity
AUTH_ENVIRONMENT=development
AUTH_IDENTITY_ENABLED=true
AUTH_REGISTRATION_ENABLED=true
AUTH_SUBSCRIBER_LOGIN_ENABLED=true
AUTH_EMAIL_VERIFICATION_REQUIRED=false
AUTH_EMAIL_TRANSPORT=disabled
AUTH_PASSWORD_RECOVERY_ENABLED=false
AUTH_ADMIN_MFA_REQUIRED=true
AUTH_SUBSCRIBER_MFA_REQUIRED=false
AUTH_RATE_LIMITING_ENABLED=false
AUTH_CSRF_REQUIRED=true
PUBLIC_BASE_URL=http://localhost:18765
ACCOUNTS_DATABASE_PATH=./data/local-accounts.sqlite3
```

Keep `MFA_ENCRYPTION_KEY` and `RATE_LIMIT_HASH_KEY` as distinct, random secrets. The local disabled-email profile does not need an email queue key. Keep generated keys stable for the life of the local test database.

With this no-email profile, new subscriber accounts are marked verified only by the explicit development policy. Registration does not create a verification challenge or queue an email. Password recovery is unavailable; use a fresh test account if you need to test registration again. The same settings are rejected in staging and production.

The local HTTP origin exception is restricted to `localhost`, `127.0.0.1`, and `::1` in the development profile. It does not permit HTTP on a public hostname. The normal CSRF cookie/header double-submit check remains active. Do not use a public reverse proxy or expose this development instance to the internet.

## Read a verification or recovery message

The application stores the complete email payload encrypted in its existing transactional queue. In local-outbox mode, the external Resend delivery worker is not started. Read messages only from the trusted local terminal:

```bash
docker compose exec -w /app open-odds-intelligence \
  python scripts/read_local_email_outbox.py
```

The script requires both `AUTH_ENVIRONMENT=development` and `AUTH_EMAIL_TRANSPORT=local_outbox`. It decrypts at most the 20 newest pending messages and prints their one-time links to that terminal. It does not create a public route or write tokens to application logs. Treat terminal output as sensitive and do not share it.

## Transport choices

- `resend`: normal encrypted queue plus configured Resend delivery.
- `local_outbox`: development only; encrypted queue is readable through the local CLI above; no external email worker runs.
- `disabled`: no email delivery. In development it may be combined with registration only when email verification and password recovery are both disabled. In that mode, new local subscribers are immediately verified. Staging and production reject this configuration.

Staging and production reject the local outbox and reject attempts to disable identity, required email verification, administrator MFA, rate limiting, or CSRF. Administrator MFA and CSRF remain mandatory even in local development; this no-email shortcut does not bypass administrator MFA. Production remains the default environment when no environment is set.

This is a testing transport, not a substitute for staging acceptance. The Admin Console's protected, audited environment editor and the remaining independent feature controls are tracked separately and are not represented as complete by this local CLI.


## Run the isolated localhost test stack

The repository includes `docker-compose.local.yml` and `.env.local.example`. This stack builds the checked-out feature branch, binds only to host loopback at port `18765`, and uses `./local-test-data` instead of the production data directory. It does not replace or restart the existing service.

From the repository checkout on the server:

```bash
cp .env.local.example .env.local
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Put the first generated value in `MFA_ENCRYPTION_KEY` and the second in `RATE_LIMIT_HASH_KEY` in `.env.local`. Keep these values private and stable for this local test database. Then start the separate test instance:

```bash
docker compose -f docker-compose.local.yml up -d --build
```

Open `http://localhost:18765/register` on the server itself. New local subscriber accounts are immediately verified and can sign in without email delivery. Local registration and login rate limits are disabled, while CSRF remains enabled. Password recovery is disabled.

The local compose file binds to `127.0.0.1` only; it is not available from other LAN devices. Administrator MFA remains required. Bootstrap a separate local administrator with:

```bash
docker compose -f docker-compose.local.yml exec -w /app open-odds-intelligence-local python scripts/bootstrap_admin.py
```

Do not point a public reverse proxy at port 18765. Do not copy the production accounts database or production MFA seed into `local-test-data`. Stop this test stack with:

```bash
docker compose -f docker-compose.local.yml down
```
