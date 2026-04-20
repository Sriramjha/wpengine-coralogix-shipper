#!/usr/bin/env python3
"""
Poll WP Engine install logs (access/error) and ship to Coralogix via /logs/v1/singles.

Self-contained: no other modules from this repo are required.

State: JSON file with per-log-type offsets (WP Engine API offset pagination).

Environment: see README.md or --help.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

API_BASE = "https://api.wpengineapi.com/v1"


# --- Coralogix singles mapping (same shape as common Coralogix HTTP examples) ---


def iso_to_utc_ms(iso_ts: str) -> float:
    s = iso_ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def _coerce_timestamp_ms(obj: dict[str, Any], *, field: str | None) -> float:
    if field:
        v = obj.get(field)
        if isinstance(v, (int, float)):
            return float(v) if float(v) > 10_000_000_000 else float(v) * 1000.0
        if isinstance(v, str) and v.strip():
            try:
                return iso_to_utc_ms(v)
            except ValueError:
                pass
    for key in ("timestamp", "ts", "time", "created_at", "@timestamp"):
        v = obj.get(key)
        if isinstance(v, (int, float)):
            x = float(v)
            return x if x > 10_000_000_000 else x * 1000.0
        if isinstance(v, str) and v.strip():
            try:
                return iso_to_utc_ms(v)
            except ValueError:
                continue
    return time.time() * 1000.0


def _coerce_severity(obj: dict[str, Any], *, field: str | None) -> int:
    if field:
        v = obj.get(field)
        if isinstance(v, int) and 1 <= v <= 6:
            return v
        if isinstance(v, str) and v.isdigit():
            n = int(v)
            if 1 <= n <= 6:
                return n
    v = obj.get("severity")
    if isinstance(v, int) and 1 <= v <= 6:
        return v
    return 3


def _computer_name(obj: dict[str, Any], *, field: str | None) -> str:
    if field:
        v = obj.get(field)
        if v is not None and str(v).strip():
            return str(v)[:1024]
    for key in ("computerName", "hostname", "host", "device", "host_name", "computer", "Hostname"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v[:1024]
    return "unknown-host"


def record_to_coralogix_single(
    obj: dict[str, Any],
    *,
    application_name: str,
    subsystem_name: str,
    text_field: str | None,
    timestamp_field: str | None,
    severity_field: str | None,
    computer_field: str | None,
) -> dict[str, Any]:
    if text_field:
        raw = obj.get(text_field)
        if raw is None:
            text = json.dumps({"error": "missing_text_field", "field": text_field, "record": obj}, default=str)
        elif isinstance(raw, str):
            text = raw
        else:
            text = json.dumps(raw, separators=(",", ":"), default=str)
    else:
        text = json.dumps(obj, separators=(",", ":"), default=str)

    return {
        "applicationName": application_name,
        "subsystemName": subsystem_name,
        "computerName": _computer_name(obj, field=computer_field),
        "timestamp": _coerce_timestamp_ms(obj, field=timestamp_field),
        "severity": _coerce_severity(obj, field=severity_field),
        "text": text,
    }


def send_coralogix_singles_batch(
    sess: requests.Session,
    ingress_url: str,
    private_key: str,
    batch: list[dict[str, Any]],
) -> None:
    if not batch:
        return
    url = f"{ingress_url.rstrip('/')}/logs/v1/singles"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {private_key}",
    }
    r = sess.post(url, headers=headers, data=json.dumps(batch), timeout=120)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Coralogix ingest failed {r.status_code}: {r.text}")


# --- WP Engine poll + state ---


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return v


def _auth_header(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"last_offsets": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_offsets": {}}


def save_state_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def acquire_lock(lock_path: Path | None) -> object | None:
    if not lock_path:
        return None
    try:
        import fcntl
    except ImportError:
        print("LOCK_PATH set but fcntl not available (non-Unix?)", file=sys.stderr)
        sys.exit(1)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        print("Another run is active (lock held); exiting.", file=sys.stderr)
        sys.exit(0)
    return fh


def release_lock(fh: object | None) -> None:
    if fh is None:
        return
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def fetch_logs_page(
    sess: requests.Session,
    *,
    auth: str,
    install_id: str,
    log_type: str,
    offset: int,
    limit: int,
) -> list[dict[str, Any]]:
    url = f"{API_BASE}/installs/{install_id}/logs"
    headers = {
        "Authorization": auth,
        "Accept": "application/json",
        "User-Agent": "wpengine-coralogix-shipper/1.0",
    }
    backoff = 1.0
    while True:
        r = sess.get(
            url,
            headers=headers,
            params={"type": log_type, "limit": limit, "offset": offset},
            timeout=60,
        )
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", str(int(backoff))))
            time.sleep(wait)
            backoff = min(backoff * 2, 30.0)
            continue
        backoff = 1.0
        if r.status_code != 200:
            raise RuntimeError(f"WP Engine API {r.status_code}: {r.text}")
        data = r.json()
        return list(data.get("results", data.get("data", [])))


def main() -> None:
    p = argparse.ArgumentParser(description="WP Engine logs -> Coralogix (singles API).")
    p.add_argument("--dry-run", action="store_true", help="Fetch only; do not POST to Coralogix.")
    args = p.parse_args()

    wpe_user = _require_env("WPE_API_USER")
    wpe_pass = _require_env("WPE_API_PASSWORD")
    install = _require_env("WPE_INSTALL_ID")
    _require_env("CORALOGIX_PRIVATE_KEY")
    _require_env("CORALOGIX_DOMAIN")

    state_path = Path(os.environ.get("STATE_PATH", "wpengine_coralogix_state.json")).expanduser()
    lock_path_str = os.environ.get("LOCK_PATH", "").strip()
    lock_path = Path(lock_path_str).expanduser() if lock_path_str else None
    lock_fh = acquire_lock(lock_path)

    app_name = os.environ.get("CX_APPLICATION_NAME", "WPENGINE").strip() or "WPENGINE"
    sub_name = os.environ.get("CX_SUBSYSTEM_NAME", install).strip() or install
    types_raw = os.environ.get("WPE_LOG_TYPES", "access,error")
    log_types = [x.strip() for x in types_raw.split(",") if x.strip()]
    max_per_type = int(os.environ.get("MAX_RECORDS_PER_RUN", "5000"))
    page_size = int(os.environ.get("PAGE_SIZE", "100"))
    batch_size = int(os.environ.get("CORALOGIX_BATCH_SIZE", "50"))

    cx_domain = os.environ["CORALOGIX_DOMAIN"].removeprefix("https://").split("/")[0]
    ingress = f"https://ingress.{cx_domain}"
    cx_key = os.environ["CORALOGIX_PRIVATE_KEY"]

    state = load_state(state_path)
    offsets: dict[str, int] = {}
    if isinstance(state.get("last_offsets"), dict):
        for k, v in state["last_offsets"].items():
            try:
                offsets[str(k)] = int(v)
            except (TypeError, ValueError):
                pass

    auth = _auth_header(wpe_user, wpe_pass)
    sess = requests.Session()

    new_offsets = dict(offsets)
    total_shipped = 0

    try:
        for log_type in log_types:
            start = new_offsets.get(log_type, 0)
            collected: list[dict[str, Any]] = []
            cur = start
            while len(collected) < max_per_type:
                need = min(page_size, max_per_type - len(collected))
                page = fetch_logs_page(
                    sess,
                    auth=auth,
                    install_id=install,
                    log_type=log_type,
                    offset=cur,
                    limit=need,
                )
                if not page:
                    break
                for rec in page:
                    if not isinstance(rec, dict):
                        continue
                    row = dict(rec)
                    row["_wpe_log_type"] = log_type
                    collected.append(row)
                cur += len(page)
                if len(page) < need:
                    break

            if not collected:
                print(f"{log_type}: no new records (offset {start})")
                continue

            print(f"{log_type}: fetched {len(collected)} record(s) from offset {start}")

            if args.dry_run:
                new_offsets[log_type] = cur
                continue

            cx_batch: list[dict[str, Any]] = []
            for row in collected:
                cx_batch.append(
                    record_to_coralogix_single(
                        row,
                        application_name=app_name,
                        subsystem_name=sub_name,
                        text_field=None,
                        timestamp_field=None,
                        severity_field=None,
                        computer_field="Hostname",
                    )
                )
                if len(cx_batch) >= batch_size:
                    send_coralogix_singles_batch(sess, ingress, cx_key, cx_batch)
                    total_shipped += len(cx_batch)
                    cx_batch.clear()
            if cx_batch:
                send_coralogix_singles_batch(sess, ingress, cx_key, cx_batch)
                total_shipped += len(cx_batch)

            new_offsets[log_type] = cur

        state_out = {
            "install": install,
            "last_offsets": new_offsets,
            "updated_unix": int(time.time()),
        }
        if not args.dry_run:
            save_state_atomic(state_path, state_out)
    finally:
        release_lock(lock_fh)

    if args.dry_run:
        print("Dry run: state not updated.")
    else:
        print(f"Done. Shipped {total_shipped} log(s). State: {state_path}")


if __name__ == "__main__":
    main()
