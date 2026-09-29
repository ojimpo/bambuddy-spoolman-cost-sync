#!/usr/bin/env python3
"""
Spoolman → Bambuddy cost_per_kg sync + MrBambuSpoolPal lot_nr migration.

Two jobs per loop iteration:
1. Migrate Spoolman's native `lot_nr` (where MrBambuSpoolPal v1.1 mistakenly writes
   the Bambu tray_uuid) into `extra.tag`, so Bambuddy's tag-based lookup matches
   pre-registered spools at AMS load time instead of creating duplicates.
2. Back-compute cost_per_kg = price / (initial_weight/1000) and PATCH the matching
   Bambuddy spool. Matching uses Spoolman's extra.tag against Bambuddy's tray_uuid
   (32-char) and tag_uid (16-char).

Idempotent: cost PATCH only fires when the value differs by >= 0.01; lot_nr migration
only fires when extra.tag is empty and lot_nr looks like a 32-char hex tray_uuid.
Runs in a loop with SLEEP_SECONDS interval.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

SPOOLMAN_URL = os.environ.get("SPOOLMAN_URL", "http://spoolman:8000").rstrip("/")
BAMBUDDY_URL = os.environ.get("BAMBUDDY_URL", "http://bambuddy:8000").rstrip("/")
SLEEP_SECONDS = int(os.environ.get("SLEEP_SECONDS", "600"))
# Set COST_SYNC=0 when Bambuddy reads Spoolman prices itself (Spoolman mode) and
# only the lot_nr -> extra.tag migration is still wanted.
COST_SYNC = os.environ.get("COST_SYNC", "1") != "0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cost_sync")


def _http_json(method: str, url: str, body: dict | None = None) -> Any:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status == 204 or not resp.length:
            return None
        return json.loads(resp.read())


def _normalize_uuid(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip('"').upper() or None


def _spoolman_tag(spool: dict) -> str | None:
    extra = spool.get("extra") or {}
    tag = extra.get("tag")
    if not isinstance(tag, str) or not tag:
        return None
    # Spoolman stores extras as JSON-encoded strings (tag is wrapped in quotes).
    return _normalize_uuid(tag)


def _is_tray_uuid(value: str | None) -> bool:
    if not value or len(value) != 32:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def migrate_lot_nr_to_tag(sm_spools: list[dict]) -> int:
    # MrBambuSpoolPal v1.1 writes the Bambu tray_uuid to Spoolman's native `lot_nr`
    # field, but Bambuddy looks up spools via `extra.tag`. Move it over so the
    # next AMS load matches the pre-registered spool instead of duplicating it.
    migrated = 0
    for sm in sm_spools:
        if sm.get("archived"):
            continue
        lot_nr = sm.get("lot_nr")
        if not _is_tray_uuid(lot_nr):
            continue
        if _spoolman_tag(sm):
            continue
        new_extra = dict(sm.get("extra") or {})
        new_extra["tag"] = json.dumps(lot_nr.upper())
        log.info("Migrating lot_nr -> extra.tag on Spoolman id=%s: %s", sm.get("id"), lot_nr)
        try:
            _http_json(
                "PATCH",
                f"{SPOOLMAN_URL}/api/v1/spool/{sm['id']}",
                {"lot_nr": None, "extra": new_extra},
            )
            migrated += 1
        except urllib.error.HTTPError as e:
            log.error("PATCH (lot_nr migration) failed for Spoolman id=%s: HTTP %s %s", sm.get("id"), e.code, e.reason)
        except urllib.error.URLError as e:
            log.error("PATCH (lot_nr migration) failed for Spoolman id=%s: %s", sm.get("id"), e.reason)
    return migrated


def sync_once() -> None:
    sm_spools = _http_json("GET", f"{SPOOLMAN_URL}/api/v1/spool") or []
    migrated = migrate_lot_nr_to_tag(sm_spools)
    if migrated:
        sm_spools = _http_json("GET", f"{SPOOLMAN_URL}/api/v1/spool") or []
    if not COST_SYNC:
        log.info("Sync done: migrated=%d (cost sync disabled, COST_SYNC=0)", migrated)
        return
    bb_spools = _http_json("GET", f"{BAMBUDDY_URL}/api/v1/inventory/spools") or []

    # Index Bambuddy spools by both 32-char tray_uuid and 16-char tag_uid, since
    # Spoolman entries may carry either depending on how the spool was registered.
    bb_by_tag: dict[str, dict] = {}
    for s in bb_spools:
        for key in ("tray_uuid", "tag_uid"):
            normalized = _normalize_uuid(s.get(key))
            if normalized:
                bb_by_tag[normalized] = s

    updated = 0
    skipped_no_price = 0
    skipped_no_tag = 0
    skipped_no_match = 0
    in_sync = 0

    for sm in sm_spools:
        if sm.get("archived"):
            continue
        price = sm.get("price")
        initial_weight = sm.get("initial_weight")
        if not price or price <= 0 or not initial_weight or initial_weight <= 0:
            skipped_no_price += 1
            continue
        tag = _spoolman_tag(sm)
        if not tag:
            skipped_no_tag += 1
            continue

        bb = bb_by_tag.get(tag)
        if not bb:
            log.warning(
                "Spoolman id=%s tag=%s: no matching Bambuddy spool (likely a stand-by spool not yet loaded into AMS)",
                sm.get("id"), tag,
            )
            skipped_no_match += 1
            continue

        target_cost = round(price / (initial_weight / 1000.0), 2)
        current_cost = bb.get("cost_per_kg")

        if current_cost is not None and abs(float(current_cost) - target_cost) < 0.01:
            in_sync += 1
            continue

        log.info(
            "Updating Bambuddy spool id=%s tag=%s: cost_per_kg %s -> %.2f (Spoolman price=%s, initial_weight=%sg)",
            bb.get("id"), tag, current_cost, target_cost, price, initial_weight,
        )
        try:
            _http_json(
                "PATCH",
                f"{BAMBUDDY_URL}/api/v1/inventory/spools/{bb['id']}",
                {"cost_per_kg": target_cost},
            )
            updated += 1
        except urllib.error.HTTPError as e:
            log.error("PATCH failed for Bambuddy id=%s: HTTP %s %s", bb.get("id"), e.code, e.reason)
        except urllib.error.URLError as e:
            log.error("PATCH failed for Bambuddy id=%s: %s", bb.get("id"), e.reason)

    log.info(
        "Sync done: migrated=%d, updated=%d, in_sync=%d, no_price=%d, no_tag=%d, no_match=%d (Spoolman total=%d, Bambuddy total=%d)",
        migrated, updated, in_sync, skipped_no_price, skipped_no_tag, skipped_no_match,
        len(sm_spools), len(bb_spools),
    )


def main() -> None:
    log.info(
        "cost_sync starting (Spoolman=%s, Bambuddy=%s, interval=%ds)",
        SPOOLMAN_URL, BAMBUDDY_URL, SLEEP_SECONDS,
    )
    while True:
        try:
            sync_once()
        except Exception:
            log.exception("Sync iteration failed; will retry next cycle")
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    main()
