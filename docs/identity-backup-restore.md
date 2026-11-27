# Identity Database Backup and Restore

**Status:** Encrypted backup/restore tooling implemented; no production backup or restore has been run.  
**Scope:** `accounts.sqlite3` only. This does not back up or modify `odds_history.sqlite3`.

## Required independent keys

The identity database contains password hashes, session records, MFA-encrypted seeds, security audit records, and encrypted pending email payloads. Keep these values in a protected secret manager or offline recovery vault:

- `IDENTITY_BACKUP_ENCRYPTION_KEY` — encrypts the backup file itself.
- `MFA_ENCRYPTION_KEY` — decrypts administrator TOTP seeds.
- `EMAIL_QUEUE_ENCRYPTION_KEY` — decrypts pending verification/recovery email payloads.
- `RATE_LIMIT_HASH_KEY` — preserves stable rate-limit bucket digests.

Do not reuse one key for another purpose. Never store the backup encryption key beside the encrypted backup files. If a key is lost, the corresponding encrypted material may be unrecoverable.

Generate the backup key once in a trusted environment:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add it to the protected host `.env` as `IDENTITY_BACKUP_ENCRYPTION_KEY`. Do not commit the value.

## Create a backup

The script uses SQLite's online backup API to create a consistent in-memory snapshot, validates integrity and schema version, encrypts the snapshot with Fernet, and writes only the encrypted file. It does not create a plaintext database copy on disk.

From the Compose directory:

```bash
docker compose exec -T open-odds-intelligence python -m scripts.backup_identity
```

The default destination is `data/backups/identity-<UTC timestamp>.sqlite3.fernet`. A newly created backup directory is mode 0700 and each backup file is mode 0600. Existing caller-selected directories are not chmodded; they must not be group- or world-writable. For backup confidentiality, use a dedicated mode-0700 directory. Existing backup files are never overwritten. The encrypted file is written to a private temporary file, flushed and fsynced, then atomically published without replacing an existing destination; an interrupted write cannot leave a partial final backup.

To choose a path:

```bash
docker compose exec -T open-odds-intelligence python -m scripts.backup_identity --output /app/data/backups/pre-upgrade.sqlite3.fernet
```

Copy encrypted backups to a separate protected location. Store the four independent keys in a separate recovery vault. Maintain the existing odds-history backup process separately.

## Inspect a backup without restoring

```bash
docker compose exec -T open-odds-intelligence python -m scripts.restore_identity /app/data/backups/identity-YYYYMMDDTHHMMSSZ.sqlite3.fernet
```

This authenticates/decrypts in memory and checks SQLite integrity and schema version. It does not write or replace a database.

## Restore — explicit offline operation only

**Never run restore against a live application.** First schedule a maintenance window and stop the application container:

```bash
docker compose stop open-odds-intelligence
```

Then inspect the selected backup. Confirm the backup key and the original MFA/email queue keys are available. Restore with both explicit confirmation flags:

```bash
docker compose run --rm --no-deps open-odds-intelligence python -m scripts.restore_identity /app/data/backups/identity-YYYYMMDDTHHMMSSZ.sqlite3.fernet --apply --confirm-offline
```

Safety behavior:

- Restore is impossible unless both `--apply` and `--confirm-offline` are supplied.
- The script refuses symlink/non-regular files, overly permissive files, invalid backup authentication, unsupported schemas, and any target matching the odds-history database path.
- It refuses to replace a target while SQLite `-wal` or `-shm` sidecars exist. Do not delete these manually while a process may be using them; investigate and confirm the service is stopped.
- If an identity database already exists, an encrypted pre-restore snapshot is created first.
- The decrypted database is held in memory and a mode-0600 temporary file in the target directory; the target is replaced atomically only after a second integrity/schema check.
- No account rows, odds history, Docker volumes, or provider configuration are deleted by the tooling.

After restore, start the application:

```bash
docker compose start open-odds-intelligence
docker compose logs --tail=100 open-odds-intelligence
```

Verify health, administrator MFA login, subscriber authentication, entitlement checks, pending email queue delivery, and the existing odds dashboard/history. If the restore is not correct, stop the service and restore the encrypted pre-restore snapshot using the same explicit procedure.

## Key rotation

Do not replace `IDENTITY_BACKUP_ENCRYPTION_KEY` while relying on old backups. Retain the key used for each backup generation in the recovery vault. A rotation procedure must decrypt each retained backup with its original key and re-encrypt it with the new key before retiring the old key. The identity database's internal MFA and email-queue encryption keys are separate and must remain available to decrypt those records after restore.

## Automated coverage

CI verifies encrypted backup creation, schema/integrity validation, restore round-trip, pre-restore snapshot creation, restrictive permissions, wrong-key rejection, symlink rejection, odds-database protection, explicit restore flags, and WAL/SHM safety. Tests use temporary databases only.
