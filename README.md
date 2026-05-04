# WP Engine -> Coralogix platform-event shipper

Small Python utility that polls **WP Engine's documented Hosting Platform API**
for state changes (backups, domains, SSL certs, install metadata, account
users, SSH keys, usage, API status) and ships one event per change to
**Coralogix** via `/logs/v1/singles`.

- **No GCP / GCS** required.
- **State file** tracks per-resource id -> content-hash so a cron every 5
  minutes is idempotent and only emits *changes*.
- **Optional file lock** so overlapping runs exit cleanly.

## What this is NOT

This is **not** a request-log / error-log shipper. WP Engine's public API
does not expose access.log or error.log lines. Verified live against
`GET https://api.wpengineapi.com/v1/swagger` (Hosting Platform API
v1.11.0): no path in the spec contains "log".

To ship raw nginx access logs or PHP error logs you need one of:

- **SSH/SFTP** from the WP Engine SSH gateway (logs at
  `~/sites/<install>/logs/{access,error}.log`).
- WP Engine's **paid Log Forwarding add-on** writing to S3, then read S3.
- A **WordPress mu-plugin** running inside the install that POSTs to
  Coralogix directly (only sees what WordPress sees).

This repo deliberately covers only what the public API can deliver.
The Google Chronicle "Collect WP Engine logs" doc references a
`GET /installs/{id}/logs` route that is **not present** in the public API
spec; do not waste time chasing it.

## Prerequisites

- **Python 3.10+**
- WP Engine **API credentials** (User Portal -> Profile -> API Access).
- WP Engine **install identifier** -- either the install UUID
  (`id` from `GET https://api.wpengineapi.com/v1/installs`) or the
  install short name (e.g. `mysite`). The shipper auto-resolves UUID -> name.
- Coralogix **Send-Your-Data** API key and **domain** (e.g. `eu2.coralogix.com`).

## Quick start

```bash
git clone https://github.com/Sriramjha/wpengine-coralogix-shipper.git
cd wpengine-coralogix-shipper
chmod +x setup.sh run.sh
./setup.sh
# edit .env with real values
./run.sh --dry-run               # poll + diff only; nothing posted, state unchanged
./run.sh                         # ship to Coralogix and update state
./run.sh --emit-initial          # one-time backfill of current resources on first run
```

### Manual setup (without `setup.sh`)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env
set -a && source .env && set +a
.venv/bin/python wpengine_logs_to_coralogix.py --dry-run
.venv/bin/python wpengine_logs_to_coralogix.py
```

## Event types

Each polled resource is diffed against the state file and emitted as one
Coralogix log event per change. Toggle with `WPE_EVENT_TYPES`
(comma-separated subset; default = all):

| Event type      | Source endpoint                                           | Action values                  |
|-----------------|-----------------------------------------------------------|--------------------------------|
| `install`       | `GET /installs/{id}`                                      | `created` / `updated`          |
| `backups`       | `GET /installs/{id}/backups`                              | `created` / `updated` / `deleted` |
| `domains`       | `GET /installs/{id}/domains`                              | `created` / `updated` / `deleted` |
| `ssl`           | `GET /installs/{id}/ssl_certificates`                     | `created` / `updated` / `deleted` |
| `account_users` | `GET /accounts/{account_id}/account_users`                | `created` / `updated` / `deleted` |
| `ssh_keys`      | `GET /ssh_keys`                                           | `created` / `updated` / `deleted` |
| `usage`         | `GET /installs/{id}/usage`                                | `snapshot` (every run)         |
| `status`        | `GET /status`                                             | `heartbeat` (every run)        |

### Coralogix event shape

Every event becomes one Coralogix `single` whose `text` is JSON of:

```json
{
  "wpe_event_type": "backups",
  "wpe_action": "updated",
  "install_name": "mysite",
  "install_id": "<uuid>",
  "account_id": "<uuid>",
  "resource_id": "<uuid>",
  "before": { ... previous resource snapshot ... },
  "after":  { ... current resource snapshot ... }
}
```

Severity is mapped per event type / action (e.g. failed backups -> 5,
SSL cert expiring within 7 days -> 5, heartbeat -> 1).

### Initial-run behavior

By default, the **first** run for each resource type silently snapshots
current state and emits **no** events (so you don't flood Coralogix with
"created" events for things that already existed when you first deployed).
Subsequent runs emit only deltas.

To backfill everything currently present, run once with `--emit-initial`.

## Environment variables

| Variable                | Required | Description |
|-------------------------|----------|-------------|
| `WPE_API_USER`          | yes      | WP Engine API username |
| `WPE_API_PASSWORD`      | yes      | WP Engine API password |
| `WPE_INSTALL_ID`        | yes      | Install UUID **or** short name |
| `WPE_API_BASE`          | no       | Default `https://api.wpengineapi.com/v1` |
| `CORALOGIX_PRIVATE_KEY` | yes      | Coralogix Send-Your-Data API key |
| `CORALOGIX_DOMAIN`      | yes      | e.g. `eu2.coralogix.com` (no `https://`) |
| `CX_APPLICATION_NAME`   | no       | Default `WPENGINE` |
| `CX_SUBSYSTEM_NAME`     | no       | Defaults to resolved install short name |
| `STATE_PATH`            | no       | Default `./wpengine_coralogix_state.json` |
| `LOCK_PATH`             | no       | If set, non-blocking `flock` (Linux/macOS) |
| `WPE_EVENT_TYPES`       | no       | Comma-separated subset of event types (default: all) |
| `MAX_RECORDS_PER_RUN`   | no       | Per-resource cap, default `5000` |
| `PAGE_SIZE`             | no       | WP Engine list page size, default `100` |
| `CORALOGIX_BATCH_SIZE`  | no       | Singles per HTTP POST, default `50` |

## Schedule every 5 minutes (cron)

```cron
*/5 * * * * cd /opt/wpengine-coralogix-shipper && set -a && . ./.env && set +a && ./run.sh >> /var/log/wpengine-coralogix.log 2>&1
```

Lock the env file: `chmod 600 .env`.

Add a `LICENSE` file of your choice when you publish the repository.
