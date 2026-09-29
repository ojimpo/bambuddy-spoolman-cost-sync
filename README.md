# bambuddy-spoolman-cost-sync

A small sidecar that copies per-spool prices from [Spoolman](https://github.com/Donkie/Spoolman) into [Bambuddy](https://github.com/MaziGGy/Bambuddy)'s local `cost_per_kg` field, so that Bambuddy's per-print cost calculation reflects what you actually paid for each spool.

> **Status (2026-09-29): probably no longer needed on current Bambuddy.**
> Bambuddy 1.2.6b1 in Spoolman mode computes per-print cost from Spoolman's `price` directly
> (`_spool_cost_per_gram` in `services/spoolman_tracking.py`: the spool's `price` wins over `filament.price`,
> divided by `filament.weight`). The archive costs of spools that this sidecar *skipped* ("no matching
> Bambuddy spool") matched their Spoolman prices exactly, so the sync was not what produced them.
> The `cost_per_kg` sync below only touches Bambuddy's **built-in** inventory table, which Spoolman mode
> does not use. Set `COST_SYNC=0` to turn it off; the `lot_nr` -> `extra.tag` migration keeps running.
> Verify against your own archives before relying on this.

## Why this exists

When Bambuddy is integrated with Spoolman, the two systems each track filament inventory but they don't share cost information:

- **Spoolman** stores per-spool `price` (purchase total) and `initial_weight` natively.
- **Bambuddy** computes per-print cost from its own `Spool.cost_per_kg` field, which has no automatic input path from Spoolman.

If you also enable Bambuddy's "use Spoolman for filament management" setting (where Bambuddy's filament page renders Spoolman in an iframe), there is no longer a UI route to Bambuddy's native per-spool cost editor. Bambuddy's cost calculation falls back to its global `default_filament_cost` setting, losing per-spool accuracy.

This sidecar bridges that gap. It is read-only against Spoolman and only writes `cost_per_kg` to Bambuddy.

## What it does

Every `SLEEP_SECONDS` (default 600s = 10 min):

1. `GET /api/v1/spool` from Spoolman — full spool list.
2. `GET /api/v1/inventory/spools` from Bambuddy — full spool list.
3. For each Spoolman spool that has a `price` and a tag identifier (stored in Spoolman as `extra.tag`), find the Bambuddy spool with the same `tray_uuid` (32-char Bambu spool UUID) or `tag_uid` (16-char RFID chip UID), and compute:

   ```text
   cost_per_kg = price / (initial_weight / 1000)
   ```

4. If Bambuddy's current `cost_per_kg` differs by ≥ 0.01, `PATCH /api/v1/inventory/spools/{id}` to update it. Otherwise skip (idempotent).

Each cycle ends with a one-line summary:

```text
2026-05-08 14:00:02 INFO Sync done: updated=1, in_sync=5, no_price=0, no_tag=2, no_match=0 (Spoolman total=8, Bambuddy total=8)
```

Each individual update logs the before/after value and the source data:

```text
2026-05-08 14:00:02 INFO Updating Bambuddy spool id=1 tag=0D4B...9CA3: cost_per_kg None -> 6560.00 (Spoolman price=3280.0, initial_weight=500.0g)
```

## Limitations

- **RFID-linked spools only.** Matching uses `tray_uuid` or `tag_uid`, which only Bambu Lab RFID spools and Spoolman entries created from RFID scans (e.g. via [MrBambuSpoolPal](https://github.com/MrBambuSpoolPal/MrBambuSpoolPal-BambuSpoolPal_AndroidApp)) carry. Non-RFID spools that you manually link via Bambuddy's "Link to Spoolman" button do not have either identifier and will be skipped — set their `cost_per_kg` once via Bambuddy's API directly:

  ```sh
  curl -X PATCH http://<bambuddy>/api/v1/inventory/spools/<id> \
       -H 'Content-Type: application/json' \
       -d '{"cost_per_kg": 3300}'
  ```

- **Spoolman price is the source of truth.** If you have manually set a different `cost_per_kg` in Bambuddy and Spoolman has a price for the same spool, the sidecar will overwrite Bambuddy's value with the Spoolman-derived value on the next cycle. Decide which side you want to manage cost from and stick to one.

- **No deletes.** The sidecar only updates `cost_per_kg`. It never archives, deletes, or modifies any other field.

## Quick start (Docker Compose)

Place `cost_sync.py` somewhere your compose file can bind-mount it. Add a service like:

```yaml
services:
  cost-sync:
    image: python:3.13-alpine
    container_name: spoolman-cost-sync
    restart: unless-stopped
    networks:
      - filament-net
    volumes:
      - ./cost-sync/cost_sync.py:/app/cost_sync.py:ro
    environment:
      - SPOOLMAN_URL=http://spoolman:8000
      - BAMBUDDY_URL=http://bambuddy:8000
      - SLEEP_SECONDS=600
      # - COST_SYNC=0   # skip the cost_per_kg sync, keep only the lot_nr -> extra.tag migration
      - PYTHONUNBUFFERED=1
      - TZ=Asia/Tokyo
    command: python /app/cost_sync.py
    labels:
      - "com.centurylinklabs.watchtower.enable=false"

networks:
  filament-net:
    external: true
```

Both Spoolman and Bambuddy must be reachable on the same Docker network (here `filament-net`). The sidecar uses HTTP against internal container hostnames so no TLS configuration is needed.

Bring it up and tail the logs:

```sh
docker compose up -d cost-sync
docker logs -f spoolman-cost-sync
```

The first sync runs immediately, then every `SLEEP_SECONDS`.

A complete example compose file is in [`docker-compose.example.yml`](docker-compose.example.yml).

## Configuration

| Env var | Default | Description |
| --- | --- | --- |
| `SPOOLMAN_URL` | `http://spoolman:8000` | Spoolman base URL. Trailing slash optional. |
| `BAMBUDDY_URL` | `http://bambuddy:8000` | Bambuddy base URL. |
| `SLEEP_SECONDS` | `600` | Interval between sync cycles. |

The script uses Python stdlib only (no `pip install` required). It runs on any `python:3` container.

## How matching works

Spoolman stores RFID tag identifiers in the `extra.tag` field as a JSON-encoded string (so the value looks like `"\"22B5E9CB...\""`). Bambuddy stores the same identifier in its native `tray_uuid` column (32-char Bambu spool UUID, used by Bambuddy's RFID auto-create) or `tag_uid` column (16-char RFID chip UID, sometimes written by MrBambuSpoolPal). The sidecar indexes Bambuddy spools by both keys and normalizes all values with `.strip('"').upper()` before comparison, matching Bambuddy's own internal logic in `find_spool_by_tag`.

## License

MIT — see [LICENSE](LICENSE).
