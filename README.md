# WP Engine → Coralogix log shipper

Small Python utility that **polls the WP Engine logs API** (access / error) and sends events to **Coralogix** using the HTTP **`/logs/v1/singles`** endpoint.

- **No Google Cloud / GCS** required.
- **State file** tracks per–log-type API offsets between runs (safe for cron every 5 minutes).
- **Optional file lock** so overlapping runs exit cleanly.

## Prerequisites

- **Python 3.10+**
- WP Engine **API credentials** and **install UUID** (`id` from `GET https://api.wpengineapi.com/v1/installs`).
- Coralogix **Send-Your-Data** API key and **domain** (e.g. `eu2.coralogix.com`).

## Quick start (copy-paste)

```bash
git clone https://github.com/Sriramjha/wpengine-coralogix-shipper.git
cd wpengine-coralogix-shipper
chmod +x setup.sh run.sh
./setup.sh
```

Edit `.env` with real values, then:

```bash
./run.sh --dry-run   # WP Engine only; no Coralogix POST, state unchanged
./run.sh             # ship to Coralogix and update state
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

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `WPE_API_USER` | yes | WP Engine API username |
| `WPE_API_PASSWORD` | yes | WP Engine API password |
| `WPE_INSTALL_ID` | yes | Install **UUID** (see `GET /v1/installs`); not the short install name |
| `WPE_API_BASE` | no | Default `https://api.wpengineapi.com/v1` |
| `WPE_LOGS_URL` | no | Override log fetch URL template; may include `{install_id}`, `{log_type}`, `{limit}`, `{offset}`, `{api_base}` |
| `CORALOGIX_PRIVATE_KEY` | yes | Coralogix Send-Your-Data API key |
| `CORALOGIX_DOMAIN` | yes | e.g. `eu2.coralogix.com` (no `https://`) |
| `CX_APPLICATION_NAME` | no | Default `WPENGINE` |
| `CX_SUBSYSTEM_NAME` | no | Defaults to `WPE_INSTALL_ID` |
| `STATE_PATH` | no | Default `./wpengine_coralogix_state.json` |
| `LOCK_PATH` | no | If set, non-blocking `flock` (Linux/macOS) |
| `WPE_LOG_TYPES` | no | Default `access,error` |
| `MAX_RECORDS_PER_RUN` | no | Default `5000` per log type |
| `PAGE_SIZE` | no | Default `100` |
| `CORALOGIX_BATCH_SIZE` | no | Default `50` |

## Schedule every 5 minutes (cron)

Point `STATE_PATH` and `LOCK_PATH` at persistent paths (example):

```cron
*/5 * * * * cd /opt/wpengine-coralogix-shipper && set -a && . ./.env && set +a && ./run.sh >> /var/log/wpengine-coralogix.log 2>&1
```

Ensure `.env` is readable only by the service user (`chmod 600 .env`).

Add a `LICENSE` file of your choice when you publish the repository.
