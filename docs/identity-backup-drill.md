# Identity Backup and Key-Recovery Drill

**Status:** Non-production runbook. This procedure is not authorization to access, stop, back up, or restore the production identity database.

## Safety rules

- Run this drill only in an isolated staging/non-production deployment.
- Use synthetic subscriber and administrator accounts. Do not copy production identity data into staging.
- Confirm both the identity database path and odds-history database path point to staging storage before running any command.
- Keep the backup encryption key in the staging secret manager. Never paste it into chat, a ticket, shell history, or a command-line argument.
- Create a private backup directory owned by the application operator. Existing directories must not be group/world writable; the tooling will not change their permissions.
- Stop the staging application and all workers before an apply/restore operation. The CLI flag is an explicit operator confirmation, not an automatic process detector.
- Never run the apply steps against a production volume as part of this drill.

## 1. Prepare an isolated staging database

Set the staging environment to use dedicated paths, for example:

- ACCOUNTS_DATABASE_PATH: a staging-only identity database file.
- DATABASE_PATH: a staging-only odds-history database file.
- IDENTITY_BACKUP_ENCRYPTION_KEY: a valid Fernet key held in the staging secret manager.

Generate a key for a disposable staging environment if one is not already provisioned:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Initialize the staging identity database and create a synthetic subscriber/admin fixture through the normal staging setup process. Verify that the identity database file is mode 0600.

## 2. Create and inspect an encrypted backup

Create a private destination directory:

```bash
install -d -m 700 "$STAGING_BACKUP_DIR"
```

Create the encrypted backup:

```bash
python scripts/backup_identity.py --output "$STAGING_BACKUP_DIR/identity-drill.sqlite3.fernet"
```

Inspect it without changing any database:

```bash
python scripts/restore_identity.py "$STAGING_BACKUP_DIR/identity-drill.sqlite3.fernet"
```

Expected results:

- Backup authentication succeeds with the staging key.
- Schema version, required tables/columns, migration history, SQLite integrity, and foreign-key integrity validate.
- The encrypted file is mode 0600.
- No plaintext database snapshot is written to disk.
- The inspection command reports that no restore was performed.

## 3. Test wrong-key recovery behavior

Temporarily configure a deliberately incorrect staging key in the staging process environment and run the inspection command again.

Expected results:

- Inspection fails with an authentication error.
- No database file is replaced.
- No pre-restore snapshot is created.
- Restore must not proceed until the correct key is recovered from the staging secret manager.

Restore the correct staging key before continuing. Never rotate or replace a production key as part of this test.

## 4. Restore into a separate staging target

Use a separate staging target path, not the source database. Initialize a synthetic target identity database containing a different test subscriber so the pre-restore snapshot path is exercised.

First run inspection only, then stop the staging app and all workers. Confirm that no WAL/SHM sidecars remain.

Only after verifying both paths are staging-only and the application is offline, run:

```bash
python scripts/restore_identity.py "$STAGING_BACKUP_DIR/identity-drill.sqlite3.fernet" --apply --confirm-offline
```

Expected results:

- The command creates an encrypted pre-restore snapshot of the existing target.
- The restored target is atomically replaced and remains mode 0600.
- The target parent directory permissions are unchanged.
- The restored database passes schema, migration, SQLite, and foreign-key validation.
- The staging app can start and complete a basic identity login/session check.

## 5. Verify recovery without exposing account data

Use staging-only checks to confirm:

- The synthetic source subscriber exists after restore.
- The pre-restore-only subscriber is absent from the restored target.
- Administrator MFA enrollment and recovery-code records remain present for the synthetic admin fixture.
- Security audit history and subscriber entitlements remain present.
- Session behavior matches the expected restored snapshot; do not assume sessions created after the backup are retained.
- The application can create a new session and revoke it successfully.

Record only pass/fail, schema version, backup file checksum, and sanitized error codes in the drill report. Do not record email addresses, phone numbers, passwords, recovery codes, tokens, or encryption keys.

## 6. Key custody and recovery

- Keep the active backup key and its recovery copy in separate approved secret-manager locations with restricted access.
- Record who can retrieve each copy and how access is audited.
- Test recovery by retrieving the recovery copy in staging and successfully inspecting a backup created with that key.
- Do not delete older backups when rotating a key. They remain dependent on the old key unless they are deliberately re-encrypted and re-verified.
- If a key is lost, the corresponding encrypted backups are not recoverable. Do not imply otherwise.
- Production key rotation requires a separately approved change plan and verified restore of re-encrypted copies.

## Completion record

The drill is complete only after a staging operator records:

- Staging environment and date.
- Backup creation and inspection result.
- Wrong-key rejection and no-mutation result.
- Restore and pre-restore snapshot result.
- Post-restore application/session check.
- Key recovery-copy inspection result.
- Operator and reviewer sign-off.

The automated test suite covers the CLI confirmation and wrong-key no-mutation paths. It does not replace this deployment-specific staging drill.
