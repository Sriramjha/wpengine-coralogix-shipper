# WP Engine → Coralogix log shipper

Small Python utility that **polls the WP Engine logs API** (access / error) and sends events to **Coralogix** using the HTTP **`/logs/v1/singles`** endpoint.

- **No Google Cloud / GCS** required.
- **State file** tracks per–log-type API offsets between runs (safe for cron every 5 minutes).
- **Optional file lock** so overlapping runs exit cleanly.

## Prerequisites

- **Python 3.10+**
- WP Engine **API credentials** and **install name** (per environment).
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
| `WPE_INSTALL_ID` | yes | Install name from WP Engine portal |
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

## Publishing this as a GitHub repo

Maintainer: [Sriramjha](https://github.com/Sriramjha).

### Option A — GitHub website + `git push`

1. Create a **new empty** repository: [github.com/new](https://github.com/new)  
   - **Owner:** Sriramjha  
   - **Repository name:** `wpengine-coralogix-shipper`  
   - Public (or private). **Do not** add a README, `.gitignore`, or license (this repo already has them).
2. From this folder (after `git init` / first commit if needed):

   ```bash
   cd wpengine-coralogix-shipper
   git branch -M main
   git remote add origin https://github.com/Sriramjha/wpengine-coralogix-shipper.git
   git push -u origin main
   ```

   Use SSH instead if you prefer:  
   `git remote add origin git@github.com:Sriramjha/wpengine-coralogix-shipper.git`

### Option B — GitHub CLI (`gh`) + helper script

One-time: `brew install gh` then `gh auth login`.

Then from this folder:

```bash
chmod +x publish_to_github.sh
./publish_to_github.sh
```

This creates `Sriramjha/wpengine-coralogix-shipper` if it does not exist and runs `git push -u origin main`.

Manual equivalent:

```bash
gh repo create Sriramjha/wpengine-coralogix-shipper --public --source=. --remote=origin --push
```

**Do not commit `.env` or `wpengine_coralogix_state.json`** — they are listed in `.gitignore`.

## Troubleshooting

- **401/403 from WP Engine**: verify API access in the WP Engine portal and credentials in `.env`.
- **429**: the script backs off; shorten the cron interval or lower `MAX_RECORDS_PER_RUN` if needed.
- **Coralogix ingest errors**: confirm `CORALOGIX_DOMAIN` matches your Coralogix region and the key is a **Send-Your-Data** key.

## License

Add a `LICENSE` file of your choice when you publish the repository.
