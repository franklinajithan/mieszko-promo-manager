# Ubuntu installation and operation

This application owns only its `mieszko-promo` Compose project, dedicated database and volumes. It does not call or share databases with OmniCore or Ordering. Do not reuse an existing application's environment file, database or volumes.

## Before deployment

Confirm the server hostname/IP, SSH account and key access, intended checkout directory, available RAM/disk, unused loopback port, hostname, existing reverse proxy and TLS method. Inspect existing services and ports. No production deployment has been performed by this repository.

Use Ubuntu with Docker Engine and Compose v2 supporting `service_completed_successfully` and `--wait`. A practical starting allocation is 2 CPU cores / 4 GB RAM plus storage sized for original images and backups; benchmark the actual server before a large first import. No paid service is required.

## Guided new installation

For a new checkout with no existing Promo Manager containers, volumes or configuration, the standard-library helper checks Docker/Compose access, available disk space and an unused loopback port. It refuses an existing installation or retained data volumes and never edits the reverse proxy or another application. Run it from the dedicated checkout on Ubuntu:

```bash
python3 scripts/deploy.py check --port 8085
python3 scripts/deploy.py configure --origin https://promo.your-domain.co.uk --port 8085
python3 scripts/deploy.py install --admin yourname
```

Replace the example origin with the agreed hostname before configuring. Configuration creates `.env` with mode 600, two different generated passwords and secure HTTPS cookies. Passwords are never printed. Existing files and symlinks are refused rather than overwritten. Install prompts privately for the administrator password and starts only the explicitly named Promo Manager Compose project. Shell variables from another app cannot override its database/origin configuration.

For initial private testing without a domain, configure `--origin http://127.0.0.1:8085` instead, then forward the server's loopback port from your office computer:

```bash
ssh -N -L 8085:127.0.0.1:8085 backend@192.168.128.139
```

Open `http://127.0.0.1:8085` on that computer while the tunnel is connected. This is a private test path: the helper rejects plain HTTP origins on public or LAN addresses. Before wider staff access, configure the agreed HTTPS hostname/origin and enable secure cookies. No SSH password belongs in the repository or command arguments.

The shared `scripts/compose.sh` wrapper also scopes backups, restores and health checks to this project and its private environment file, ignoring conflicting shell configuration from other applications.

If installation is interrupted after containers are created, do not delete configuration or volumes and do not rerun new-install initialisation. Inspect `./scripts/compose.sh ps` and the app logs; resume the documented startup/admin command with the preserved configuration. Existing installations must use the backup/upgrade procedure below.

## New installation

```bash
git clone https://github.com/franklinajithan/mieszko-promo-manager.git
cd mieszko-promo-manager
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_hex(32))'
# Put that newly generated 64-character hex secret in POSTGRES_PASSWORD.
# Generate a second different secret for PROMO_APP_PASSWORD.
# Set ALLOWED_ORIGIN to the exact HTTPS origin, no trailing slash.
# Choose an unused PROMO_PORT. COOKIE_SECURE must be true with HTTPS.
./scripts/compose.sh config --quiet
./scripts/compose.sh up -d --build --wait --wait-timeout 180
./scripts/compose.sh exec web python -m app.cli create-admin --username yourname
./scripts/check-health.sh
```

The runtime uses a restricted `promo_app` database role; the migration container uses the owner role. The database is not published. The app binds only to `127.0.0.1:8085` by default. Add a new virtual host using `deploy/nginx.conf.example`, adapting the domain, certificate and port. Test `nginx -t` before reloading the existing proxy. Do not replace other sites' configuration. Do not forward public traffic before HTTPS and staff login work. A session is an opaque random token, stored hashed in PostgreSQL, with HttpOnly/Secure/SameSite cookies and CSRF protection.

For a temporary SSH tunnel on a private test deployment only, use `COOKIE_SECURE=false` and set the exact HTTP origin. Return both settings to production values before public access. The app deliberately ignores arbitrary forwarded client IPs; login throttling uses 10 attempts per username/source pair and 100 per source in 15 minutes; source limits are shared behind the proxy. Add correctly scoped proxy rate limits if needed; do not simply trust user-supplied X-Forwarded-For.

## First data load

1. Create staff accounts under User Management.
2. Upload the original workbook under Excel Imports. Its `Product List` sheet uses **A code, B barcode, C brand, D unit, E weight, F description**, without a header row. Cached Excel lookup results are read without evaluating formulas or following external links; uncached identity formulas are rejected. Alternative workbooks may provide recognised headers in any order.
3. Review accepted-row samples and the full CSV report. Missing internal codes are warnings; a usable barcode remains required. Invalid GTIN checksums remain usable identifiers and are printed as Code128. Identity conflicts and duplicate rows are rejected. Import matches barcode first and internal code as a second identity check; conflicting identities never merge automatically.
4. Commit accepted rows. The worker commits in batches of 250. Cancellation preserves already committed batches; retry matches those identities and continues safely. A retry's counts describe that attempt, not a cumulative insert count.
5. Upload images named `BARCODE.jpg`, `.png` or `.webp` to match automatically. Originals are retained and exact duplicate bytes share one asset. Unmatched images remain reusable by asset ID. Product image removal unlinks the image, retaining the original and historical print assets.
6. Upload the official Mieszko logo as an image and select its ID in a saved label template. The repository contains a text wordmark, not an extracted or invented official logo.
7. Select products across catalogue pages and create a campaign. Set each GBP price, optional prior price and common dates. Promotional prices never modify product records.
8. Open Design & print. Review the actual PDF preview, select A4 orientation and save template settings. Generate a print job, download and print at 100% / Actual size. Exactly one product occupies each A4 page. Archived products cannot enter a new campaign.

## Backup and restore

`./scripts/backup.sh` briefly stops **this app's** web and worker, takes a custom-format PostgreSQL dump and a matching archive of all originals, thumbnails, uploads, PDFs and worker state, verifies readability, records checksums and the source commit, and restarts the writers. No other Compose project is stopped. Configure `BACKUP_DIR` on a separate disk. Adapt `deploy/backup.cron.example` for a nightly run. Monitor its exit code/log; configure off-host copying with existing infrastructure. Backups contain business information; restrict permissions and encrypt off-host storage using your existing tooling.

Backups are not automatically deleted. Configure retention only after a successful restore drill. A backup on the same failed disk is insufficient. The CI workflow exercises the scripts on a disposable stack, but the actual Ubuntu backup destination still needs a restore drill.

Restore only a trusted archive created by this application. This replaces the current app database and files. First take a fresh backup and confirm the source commit:

```bash
PROMO_RESTORE_CONFIRMED=RESTORE_MIESZKO_PROMO ./scripts/restore.sh /absolute/backup/directory
./scripts/check-health.sh
```

Do not run `./scripts/compose.sh down -v` on production: it deletes this application's volumes. Do not restore archives from untrusted sources.

## Upgrades

1. Review the PR, migration notes and CI evidence.
2. Run `./scripts/backup.sh`; confirm the archive and adequate free space.
3. `git pull --ff-only` from verified main.
4. `./scripts/compose.sh stop web worker` then `./scripts/compose.sh build`.
5. `./scripts/compose.sh run --rm migrate` and verify success before restarting writers.
6. `./scripts/compose.sh up -d --wait --wait-timeout 180`.
7. `./scripts/check-health.sh`, then verify sign-in, search, image retrieval and a saved PDF in a real browser.

For an existing prototype database volume, the PostgreSQL init script will not run automatically. Before upgrading, create the runtime role and default privileges with `./scripts/compose.sh exec -T db sh /docker-entrypoint-initdb.d/10-promo-app.sh`, then grant it access to pre-existing tables and sequences using the database owner. Do not recreate a volume to trigger initialisation.

Initial migrations preserve the prototype's products, images and promotions. Duplicate nonempty internal codes deliberately stop the upgrade for manual review, with no automatic deletion. PostgreSQL DDL migrations are transactional. SQLite exists for development tests only. Destructive downgrades are disabled: restore the verified matching database/files backup and source commit for rollback.

## Recovery and monitoring

- `/health/live` checks the process; `/health/ready` checks the database and migration table. The worker health check monitors progress/idle heartbeat.
- `./scripts/compose.sh logs --tail=100 web worker` contains structured request/job events, request IDs and job IDs. No passwords or request bodies are logged. Log rotation is enabled.
- The dashboard and job pages display failed/partial jobs. A restarted worker marks interrupted jobs failed; Retry safely is explicit. One PostgreSQL advisory lock prevents multiple workers processing the same queue. Do not run the test worker against production.
- Persistent files are retained for reliable reprints and recovery. Monitor disk capacity and failed backups; set alerting using existing server monitoring. No paid error-monitoring service is introduced.
- Staff can edit products/images/campaigns and operate their print jobs. Administrators manage imports, product archive/restore, users, settings and audit. No permanent product-delete endpoint is exposed. Disabling or resetting a user revokes their sessions.

## Release gates

Before real deployment: confirm GitHub CI success, review benchmark artifacts, supply official logo/source images, compare representative printed pages with the reference, configure HTTPS/origin and backups, run an Ubuntu restore drill, verify staff/admin permissions and run the full browser flow on that server. Layout regression tests and barcode decoding protect template geometry; they do not replace printer verification or business approval of the design.
