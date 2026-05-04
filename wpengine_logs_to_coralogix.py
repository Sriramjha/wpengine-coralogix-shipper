#!/usr/bin/env python3
"""
WP Engine -> Coralogix platform-event shipper.

Polls *documented* WP Engine Hosting Platform API endpoints for changes and
ships per-change events to Coralogix via /logs/v1/singles.

This is NOT a request/error log shipper. WP Engine's public API does not
expose access.log / error.log lines (verified against the live OpenAPI spec
at GET /v1/swagger). Use SSH/SFTP, the Log Forwarding add-on, or a
WordPress mu-plugin for those.

Event types covered (toggle with WPE_EVENT_TYPES, comma-separated):
  install        - install metadata changes (php_version, status, env, etc.)
  backups        - new backups, status transitions, deletions
  domains        - domain add/remove/state-change
  ssl            - SSL certificate add/remove/renewal/expiry
  account_users  - account user add/remove/role-change
  ssh_keys       - SSH key add/remove
  usage          - install disk/CDN usage snapshot (per run)
  status         - WP Engine API heartbeat (per run)

State: JSON file with per-resource id->content-hash tracking.

Environment: see README.md or --help.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

DEFAULT_API_BASE = "https://api.wpengineapi.com/v1"
USER_AGENT = "wpengine-coralogix-shipper/2.0"

ALL_EVENT_TYPES = [
    "install",
    "backups",
    "domains",
    "ssl",
    "account_users",
    "ssh_keys",
    "usage",
    "status",
]


# --- Coralogix singles helpers ---


def iso_to_utc_ms(iso_ts: str) -> float:
    s = iso_ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def now_ms() -> float:
    return time.time() * 1000.0


def make_coralogix_single(
    *,
    application_name: str,
    subsystem_name: str,
    computer_name: str,
    severity: int,
    payload: dict[str, Any],
    timestamp_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "applicationName": application_name,
        "subsystemName": subsystem_name,
        "computerName": computer_name[:1024] if computer_name else "wpengine",
        "timestamp": timestamp_ms if timestamp_ms is not None else now_ms(),
        "severity": severity,
        "text": json.dumps(payload, separators=(",", ":"), default=str),
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


# --- WP Engine API helpers ---


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return v


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _looks_like_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s.strip()))


def _auth_header(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def wpe_get(
    sess: requests.Session,
    *,
    auth: str,
    url: str,
    params: dict[str, Any] | None = None,
) -> requests.Response:
    backoff = 1.0
    while True:
        r = sess.get(
            url,
            headers={
                "Authorization": auth,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            params=params,
            timeout=60,
        )
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", str(int(backoff))))
            time.sleep(wait)
            backoff = min(backoff * 2, 30.0)
            continue
        return r


def wpe_get_json(
    sess: requests.Session,
    *,
    auth: str,
    url: str,
    params: dict[str, Any] | None = None,
) -> Any:
    r = wpe_get(sess, auth=auth, url=url, params=params)
    if r.status_code == 404:
        return None
    if r.status_code in (401, 403):
        raise RuntimeError(
            f"WP Engine API {r.status_code} at {url!r}. Check WPE_API_USER / "
            f"WPE_API_PASSWORD and that this account owns the resource. "
            f"Raw response: {r.text}"
        )
    if r.status_code != 200:
        raise RuntimeError(f"WP Engine API {r.status_code} at {url!r}: {r.text}")
    try:
        return r.json()
    except ValueError as e:
        raise RuntimeError(f"WP Engine API non-JSON at {url!r}: {r.text}") from e


def wpe_paginate(
    sess: requests.Session,
    *,
    auth: str,
    url: str,
    page_size: int = 100,
    max_total: int = 5000,
) -> list[dict[str, Any]]:
    """
    Walk a list endpoint that returns {results, next, previous, count}.
    Stops at max_total to bound a single run.
    """
    out: list[dict[str, Any]] = []
    next_url: str | None = url
    params: dict[str, Any] | None = {"limit": page_size, "offset": 0}
    while next_url and len(out) < max_total:
        data = wpe_get_json(sess, auth=auth, url=next_url, params=params)
        if data is None:
            break
        if isinstance(data, list):
            out.extend(x for x in data if isinstance(x, dict))
            break
        if not isinstance(data, dict):
            break
        results = data.get("results")
        if isinstance(results, list):
            out.extend(x for x in results if isinstance(x, dict))
        nxt = data.get("next")
        if isinstance(nxt, str) and nxt and nxt != next_url:
            next_url = nxt
            params = None
        else:
            break
    return out[:max_total]


def resolve_install_name(
    sess: requests.Session,
    *,
    auth: str,
    install_id_or_name: str,
) -> dict[str, Any]:
    """
    Return {"id": uuid, "name": short_name, "account_id": uuid, "raw": {...}}.
    Accepts UUID or short name in install_id_or_name.
    """
    base = os.environ.get("WPE_API_BASE", DEFAULT_API_BASE).rstrip("/")
    url = f"{base}/installs/{install_id_or_name}"
    data = wpe_get_json(sess, auth=auth, url=url)
    if data is None:
        raise RuntimeError(
            f"WP Engine API 404 resolving install at {url!r}. WPE_INSTALL_ID does "
            "not match any install visible to this API user. Verify with: "
            f"curl -u $WPE_API_USER:$WPE_API_PASSWORD {base}/installs"
        )
    install_id = data.get("id")
    install_name = data.get("name")
    account = data.get("account") or {}
    account_id = account.get("id") if isinstance(account, dict) else None
    if not (isinstance(install_id, str) and isinstance(install_name, str)):
        raise RuntimeError(
            f"WP Engine API response missing id/name at {url!r}: {data!r}"
        )
    return {
        "id": install_id,
        "name": install_name,
        "account_id": account_id if isinstance(account_id, str) else "",
        "raw": data,
    }


# --- State + diff ---


def stable_hash(obj: Any) -> str:
    s = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_state_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def diff_collection(
    *,
    state_for_resource: dict[str, Any],
    current_items: Iterable[dict[str, Any]],
    id_field: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Diff a list of resources against last seen state.
    Returns (events, new_state).

    state_for_resource shape: {"items": {<id>: {"hash": "...", "data": {...}}}}
    Each event: {action, resource_id, before, after}.
    """
    prior = state_for_resource.get("items") if isinstance(state_for_resource, dict) else None
    if not isinstance(prior, dict):
        prior = {}

    seen_ids: set[str] = set()
    events: list[dict[str, Any]] = []
    new_items: dict[str, Any] = {}

    for item in current_items:
        rid = item.get(id_field)
        if not isinstance(rid, (str, int)):
            continue
        rid_s = str(rid)
        seen_ids.add(rid_s)
        h = stable_hash(item)
        new_items[rid_s] = {"hash": h, "data": item}
        prev = prior.get(rid_s)
        if not isinstance(prev, dict):
            events.append({"action": "created", "resource_id": rid_s, "before": None, "after": item})
        elif prev.get("hash") != h:
            events.append({
                "action": "updated",
                "resource_id": rid_s,
                "before": prev.get("data"),
                "after": item,
            })

    for rid_s, prev in prior.items():
        if rid_s in seen_ids:
            continue
        events.append({
            "action": "deleted",
            "resource_id": rid_s,
            "before": prev.get("data") if isinstance(prev, dict) else None,
            "after": None,
        })

    return events, {"items": new_items}


def severity_for(event_type: str, action: str, after: dict[str, Any] | None) -> int:
    if event_type == "status":
        return 1
    if event_type == "usage":
        return 2
    if event_type == "backups":
        if action == "updated" and isinstance(after, dict):
            status = str(after.get("status", "")).lower()
            if status in ("failed", "error"):
                return 5
            if status in ("completed", "complete", "success"):
                return 3
        if action == "created":
            return 3
        if action == "deleted":
            return 4
    if event_type == "ssl":
        if isinstance(after, dict):
            expires = after.get("expires_on") or after.get("not_after") or after.get("expires_at")
            if isinstance(expires, str):
                try:
                    days_left = (datetime.fromisoformat(expires.replace("Z", "+00:00"))
                                 - datetime.now(timezone.utc)).days
                    if days_left < 7:
                        return 5
                    if days_left < 30:
                        return 4
                except ValueError:
                    pass
    if action in ("created", "deleted"):
        return 4
    if action == "updated":
        return 3
    return 3


# --- Per-resource pollers ---


def poll_install_meta(
    sess: requests.Session,
    *,
    auth: str,
    api_base: str,
    install_uuid: str,
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = wpe_get_json(sess, auth=auth, url=f"{api_base}/installs/{install_uuid}")
    if data is None:
        return [], state
    h = stable_hash(data)
    prev = state.get("hash") if isinstance(state, dict) else None
    events: list[dict[str, Any]] = []
    if prev != h:
        events.append({
            "action": "updated" if prev is not None else "created",
            "resource_id": install_uuid,
            "before": state.get("data") if isinstance(state, dict) else None,
            "after": data,
        })
    return events, {"hash": h, "data": data}


def poll_collection(
    sess: requests.Session,
    *,
    auth: str,
    url: str,
    state: dict[str, Any],
    id_field: str = "id",
    page_size: int = 100,
    max_total: int = 5000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items = wpe_paginate(sess, auth=auth, url=url, page_size=page_size, max_total=max_total)
    return diff_collection(state_for_resource=state, current_items=items, id_field=id_field)


def poll_status(
    sess: requests.Session,
    *,
    auth: str,
    api_base: str,
) -> dict[str, Any] | None:
    return wpe_get_json(sess, auth=auth, url=f"{api_base}/status")


def poll_usage(
    sess: requests.Session,
    *,
    auth: str,
    api_base: str,
    install_uuid: str,
) -> dict[str, Any] | None:
    return wpe_get_json(sess, auth=auth, url=f"{api_base}/installs/{install_uuid}/usage")


# --- Main ---


def main() -> None:
    p = argparse.ArgumentParser(description="WP Engine platform events -> Coralogix.")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch + diff only; do NOT POST to Coralogix and do NOT update state.")
    p.add_argument("--emit-initial", action="store_true",
                   help="On first run for a resource, emit 'created' events for "
                        "every existing item. Default: silently snapshot.")
    args = p.parse_args()

    wpe_user = _require_env("WPE_API_USER")
    wpe_pass = _require_env("WPE_API_PASSWORD")
    install_id_or_name = _require_env("WPE_INSTALL_ID")
    cx_key = _require_env("CORALOGIX_PRIVATE_KEY")
    cx_domain_raw = _require_env("CORALOGIX_DOMAIN")

    api_base = os.environ.get("WPE_API_BASE", DEFAULT_API_BASE).rstrip("/")
    state_path = Path(os.environ.get("STATE_PATH", "wpengine_coralogix_state.json")).expanduser()
    page_size = int(os.environ.get("PAGE_SIZE", "100"))
    max_per_resource = int(os.environ.get("MAX_RECORDS_PER_RUN", "5000"))
    batch_size = int(os.environ.get("CORALOGIX_BATCH_SIZE", "50"))

    enabled_raw = os.environ.get("WPE_EVENT_TYPES", ",".join(ALL_EVENT_TYPES))
    enabled = {x.strip() for x in enabled_raw.split(",") if x.strip()}
    invalid = enabled - set(ALL_EVENT_TYPES)
    if invalid:
        print(f"Unknown WPE_EVENT_TYPES values: {sorted(invalid)}. "
              f"Valid: {ALL_EVENT_TYPES}", file=sys.stderr)
        sys.exit(2)

    cx_domain = cx_domain_raw.removeprefix("https://").split("/")[0]
    ingress = f"https://ingress.{cx_domain}"

    sess = requests.Session()
    auth = _auth_header(wpe_user, wpe_pass)

    state = load_state(state_path)

    cached = state.get("install_resolution") if isinstance(state, dict) else None
    if (
        isinstance(cached, dict)
        and cached.get("id_or_name") == install_id_or_name
        and isinstance(cached.get("id"), str)
        and isinstance(cached.get("name"), str)
    ):
        install_uuid = cached["id"]
        install_name = cached["name"]
        account_uuid = cached.get("account_id", "")
    else:
        info = resolve_install_name(sess, auth=auth, install_id_or_name=install_id_or_name)
        install_uuid = info["id"]
        install_name = info["name"]
        account_uuid = info["account_id"]
        if install_uuid != install_id_or_name or install_name != install_id_or_name:
            print(
                f"resolved WPE_INSTALL_ID {install_id_or_name!r} -> "
                f"id={install_uuid!r} name={install_name!r} account={account_uuid!r}",
                file=sys.stderr,
            )

    snapshots = state.get("snapshots") if isinstance(state.get("snapshots"), dict) else {}
    is_first_run_for: dict[str, bool] = {}

    app_name = os.environ.get("CX_APPLICATION_NAME", "WPENGINE").strip() or "WPENGINE"
    sub_name = os.environ.get("CX_SUBSYSTEM_NAME", install_name).strip() or install_name
    computer_name = install_name

    all_events: list[tuple[str, dict[str, Any]]] = []  # (event_type, event)
    new_snapshots: dict[str, Any] = dict(snapshots)

    def queue_events(event_type: str, evts: list[dict[str, Any]]) -> None:
        for e in evts:
            all_events.append((event_type, e))

    if "install" in enabled:
        prev = snapshots.get("install", {})
        is_first_run_for["install"] = not bool(prev)
        evts, snap = poll_install_meta(
            sess, auth=auth, api_base=api_base, install_uuid=install_uuid, state=prev,
        )
        new_snapshots["install"] = snap
        if is_first_run_for["install"] and not args.emit_initial:
            evts = []
        queue_events("install", evts)

    collection_endpoints = {
        "backups": f"{api_base}/installs/{install_uuid}/backups",
        "domains": f"{api_base}/installs/{install_uuid}/domains",
        "ssl": f"{api_base}/installs/{install_uuid}/ssl_certificates",
        "ssh_keys": f"{api_base}/ssh_keys",
    }
    if account_uuid:
        collection_endpoints["account_users"] = (
            f"{api_base}/accounts/{account_uuid}/account_users"
        )
    elif "account_users" in enabled:
        print("account_users requested but no account UUID resolved; skipping",
              file=sys.stderr)

    for et, url in collection_endpoints.items():
        if et not in enabled:
            continue
        prev = snapshots.get(et, {})
        is_first_run_for[et] = not bool(prev.get("items") if isinstance(prev, dict) else None)
        try:
            evts, snap = poll_collection(
                sess, auth=auth, url=url, state=prev,
                page_size=page_size, max_total=max_per_resource,
            )
        except RuntimeError as e:
            print(f"{et}: skipped due to API error: {e}", file=sys.stderr)
            continue
        new_snapshots[et] = snap
        if is_first_run_for[et] and not args.emit_initial:
            evts = []
        queue_events(et, evts)

    if "usage" in enabled:
        usage = poll_usage(sess, auth=auth, api_base=api_base, install_uuid=install_uuid)
        if usage is not None:
            queue_events("usage", [{
                "action": "snapshot", "resource_id": install_uuid,
                "before": None, "after": usage,
            }])

    if "status" in enabled:
        status = poll_status(sess, auth=auth, api_base=api_base)
        if status is not None:
            queue_events("status", [{
                "action": "heartbeat", "resource_id": "wpengine-api",
                "before": None, "after": status,
            }])

    print(f"install={install_name!r} uuid={install_uuid!r}: "
          f"{len(all_events)} event(s) across {len([k for k in new_snapshots if k != 'install_resolution'])} resource type(s)")
    for et in sorted({t for t, _ in all_events}):
        n = sum(1 for t, _ in all_events if t == et)
        first = " (initial; suppressed - rerun with --emit-initial to ship them)" \
            if is_first_run_for.get(et) and not args.emit_initial else ""
        print(f"  {et}: {n}{first}")

    if args.dry_run:
        for et, e in all_events[:10]:
            preview = {k: e.get(k) for k in ("action", "resource_id")}
            print(f"  sample[{et}] {preview}")
        if len(all_events) > 10:
            print(f"  ... and {len(all_events) - 10} more")
        print("Dry run: state NOT updated, nothing posted to Coralogix.")
        return

    batch: list[dict[str, Any]] = []
    total_shipped = 0
    for event_type, evt in all_events:
        action = str(evt.get("action", "unknown"))
        after = evt.get("after") if isinstance(evt.get("after"), dict) else None
        sev = severity_for(event_type, action, after)
        ts_ms: float | None = None
        if isinstance(after, dict):
            for k in ("updated_at", "created_at", "timestamp", "created_on"):
                v = after.get(k)
                if isinstance(v, str) and v.strip():
                    try:
                        ts_ms = iso_to_utc_ms(v)
                        break
                    except ValueError:
                        continue
        payload = {
            "wpe_event_type": event_type,
            "wpe_action": action,
            "install_name": install_name,
            "install_id": install_uuid,
            "account_id": account_uuid,
            "resource_id": evt.get("resource_id"),
            "before": evt.get("before"),
            "after": evt.get("after"),
        }
        batch.append(make_coralogix_single(
            application_name=app_name,
            subsystem_name=sub_name,
            computer_name=computer_name,
            severity=sev,
            payload=payload,
            timestamp_ms=ts_ms,
        ))
        if len(batch) >= batch_size:
            send_coralogix_singles_batch(sess, ingress, cx_key, batch)
            total_shipped += len(batch)
            batch.clear()
    if batch:
        send_coralogix_singles_batch(sess, ingress, cx_key, batch)
        total_shipped += len(batch)

    state_out = {
        "install_resolution": {
            "id_or_name": install_id_or_name,
            "id": install_uuid,
            "name": install_name,
            "account_id": account_uuid,
        },
        "snapshots": new_snapshots,
        "updated_unix": int(time.time()),
    }
    save_state_atomic(state_path, state_out)
    print(f"Done. Shipped {total_shipped} event(s) to Coralogix. State: {state_path}")


if __name__ == "__main__":
    main()
