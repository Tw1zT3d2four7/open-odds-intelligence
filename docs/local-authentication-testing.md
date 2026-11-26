# Local authentication testing

This workflow is for an isolated developer workstation only. It does not change the production deployment and must not be used on a public hostname.

## Configure a local development instance

Use a separate `.env` and a separate accounts database. Do not copy the live accounts database, administrator MFA seed, provider credentials, or production session data into the development instance.

Set these values in the local development `.env`:

```dotenv
AUTH_MODE=identity
AUTH_ENVIRONMENT=development
AUTH_IDENTITY_ENABLED=true
AUTH_REGISTRATION_ENABLED=true
AUTH_SUBSCRIBER_LOGIN_ENABLED=true
AUTH_EMAIL_VERIFICATION_REQUIRED=true
AUTH_EMAIL_TRANSPORT=local_outbox
AUTH_PASSWORD_RECOVERY_ENABLED=true
AUTH_ADMIN_MFA_REQUIRED=true
AUTH_SUBSCRIBER_MFA_REQUIRED=false
AUTH_RATE_LIMITING_ENABLED=true
AUTH_CSRF_REQUIRED=true
PUBLIC_BASE_URL=http://localhost:8765
ACCOUNTS_DATABASE_PATH=./data/dev-accounts.sqlite3
```

Keep `MFA_ENCRYPTION_KEY`, `EMAIL_QUEUE_ENCRYPTION_KEY`, and `RATE_LIMIT_HASH_KEY` as distinct, random secrets. The email queue encryption key must be stable while messages remain queued. The rate-limit hash key must remain stable to preserve existing bucket identities.

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
- `disabled`: no email delivery. It cannot be combined with required email verification, so registration/recovery flows that need email cannot work.

Staging and production reject the local outbox and reject attempts to disable identity, required email verification, administrator MFA, rate limiting, or CSRF. Production remains the default environment when no environment is set.

This is a testing transport, not a substitute for staging acceptance. The Admin Console's protected, audited environment editor and the remaining independent feature controls are tracked separately and are not represented as complete by this local CLI.
