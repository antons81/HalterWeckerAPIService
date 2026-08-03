# HalterWecker API Service

Static GTFS pipeline for HalteWecker.

## Setup

1. Add a repository variable named `GTFS_URL` in **Settings → Secrets and variables → Actions**. Its value must be the public GTFS ZIP URL.
2. In **Settings → Pages**, choose **GitHub Actions** as the publishing source.
3. Run **Update stop data** manually once.

The workflow runs every day and publishes:

- `data/manifest.json`
- `data/cities.json`
- `data/stops/{cityId}.json`
- `data/transit/city-lines/{cityId}.json` for cities covered by Live Radar
- `data/transit/rnv/network.json` with rnv route and trip metadata
- `data/attributions.json`

Each stop has `id`, `name`, `latitude`, `longitude`, and `searchName` fields. The iOS app validates downloaded JSON and later imports it into its local SQLite FTS index.

## Ireland NTA realtime snapshots

The production stop-data pipeline consumes the atomically published local static feed at
`/srv/haltewecker/data/ireland/static`; the Ireland systemd updater remains the only owner
of static-feed refreshes. The API container exposes the separately refreshed, read-only
snapshots without forwarding NTA credentials:

- `/ireland/realtime/vehicles`
- `/ireland/realtime/trip-updates`

Both endpoints validate the stored JSON and return its original bytes. Missing or malformed
snapshots return `503`; the API container mounts the Ireland `realtime` directory for this
feature and does not mount `/srv/haltewecker/data/ireland/.env`.

## Static departures synchronization

`scripts/run_stop_data_pipeline.sh` publishes `/srv/haltewecker/data/current` atomically. Only after that replacement succeeds, it starts and waits for `haltewecker-static-departures.service`. The stop-data pipeline succeeds only when systemd reports both `Result=success` and `ExecMainStatus=0`; otherwise the stop release remains published but the pipeline exits non-zero and logs that static departures are not synchronized.

The static-departures service remains the only SQLite rebuild path and keeps its `flock` serialization. The nightly `haltewecker-static-departures.timer` at 01:30 remains enabled as a fallback/recovery run. The stop-data pipeline also takes `/run/lock/haltewecker-stop-data.lock`, so overlapping publications cannot request competing rebuilds.

The `deploy` user needs non-interactive permission for `systemctl start` and `systemctl show` on `haltewecker-static-departures.service`. Verify a completed synchronization with:

```bash
systemctl show haltewecker-static-departures.service -p Result -p ExecMainStatus
curl -sS https://api.asoftlabs.app/static-departures/health
```

The expected service state is `Result=success` and `ExecMainStatus=0`; the health response exposes the active database version and generation time.

The pipeline downloads the official BKG VG250 municipality boundaries and assigns every German stop to its municipality. Only municipalities containing at least one stop are published. Stable automatic city IDs contain the municipality's official AGS code. Cities in `config/cities.json` keep their existing IDs, aliases, larger configured radii, and Transit Radar configuration.

## Austria / ÖBB static stops

Austria uses `packageMode: "austrian"` and an independent radius-package builder, so the German BKG municipality logic is unchanged. Set the `AUSTRIAN_GTFS_URL` repository variable to an authorised current GTFS Schedule download from [Mobilitätsdaten Österreich](https://mobilitaetsdaten.gv.at/daten/soll-fahrplandaten-gtfs). The configured Austrian cities then receive stop packages from the official feed on the next stop-data build. The schedule feed is static; it must not be used to manufacture live vehicle positions.

City line catalogs are derived from the GTFS relationship `stop_times → trips → routes`. They contain only routes serving at least one stop inside the selected municipality. The iOS app uses these optional catalogs to scope regional realtime vehicle feeds to the selected city and falls back to the unfiltered live feed when no valid catalog is available.

## External GTFS providers (Sweden-first)

Germany, Switzerland, Austria and the Netherlands keep their existing builders. Additional countries use a **generic external GTFS registry** so new feeds do not need Sweden-specific Python forks.

### Architecture

| Piece | Role |
|-------|------|
| `config/external-gtfs-sources.json` | Source registry (id, cities file, timezone, `identifierPrefix`, `stopIDMode`) |
| `config/sweden-cities.json` | City packages + transit radar for Sweden |
| `scripts/external_gtfs.py` | Validation, authenticated download, stop/route/departure builders |
| `scripts/build_stop_packages.py` | `--external-gtfs-sources`, repeatable `--external-gtfs-url providerID=URL` |
| `scripts/run_stop_data_pipeline.sh` | Maps `SWEDEN_GTFS_URL` → `--external-gtfs-url sweden=...` |

`stopIDMode: "exact"` keeps original GTFS `stop_id` values (including platforms and parents as separate stops). Sweden GTFS-RT uses the same IDs, so they must not be canonicalized to `parent_station`.

Sweden static departures are published as compact city files:

- `stops/stockholm.json`
- `departures/stockholm.json` (`timezone`, exact stop keys, `t/r/h/d/p` rows)
- `routes/stockholm.json`

Sweden is **not** imported into the shared German/Austrian static-departures SQLite in this architecture. Stockholm is excluded from German SQLite membership via `configured_external_city_ids`.

Realtime stays on the existing worker (`/sweden/sl/...`). City config carries adapter `sweden` and operator `sl` only — no embedded realtime URLs.

### Required environment (production)

Add to `/etc/haltewecker-stop-data.env` (operator-managed; never commit secrets):

```bash
SWEDEN_GTFS_URL=<Samtrafiken Sweden 3 static GTFS URL>
SAMTRAFIKEN_STATIC_API_KEY=<secret>
```

The pipeline never logs the API key. Remote Sweden downloads send `Accept-Encoding: gzip` and attach the key as the Trafiklab `key` query parameter.

### Staging dry-run (no publication)

```bash
cd /srv/haltewecker/pipeline/HalterWeckerAPIService
set -a && source /etc/haltewecker-stop-data.env && set +a
rm -rf /tmp/haltewecker-external-dryrun
python3 scripts/build_stop_packages.py \
  --gtfs-url "$GTFS_URL" \
  --swiss-gtfs-url "$SWISS_GTFS_URL" \
  --austrian-gtfs-url "${AUSTRIAN_GTFS_URL:-}" \
  --nl-gtfs-url "$NL_GTFS_URL" \
  --external-gtfs-url "sweden=$SWEDEN_GTFS_URL" \
  --output /tmp/haltewecker-external-dryrun
test -f /tmp/haltewecker-external-dryrun/stops/stockholm.json
test -f /tmp/haltewecker-external-dryrun/departures/stockholm.json
python3 -c 'import json; m=json.load(open("/tmp/haltewecker-external-dryrun/manifest.json")); print(sum(1 for c in m["cities"] if c["id"]=="stockholm"))'
```

### Production publication

```bash
# only after dry-run succeeds; publishes via staging swap + static-departures sync
sudo -u deploy /srv/haltewecker/pipeline/HalterWeckerAPIService/scripts/run_stop_data_pipeline.sh
```

Do not edit `/srv/haltewecker/data/current` by hand.

### Adding another country (no new Python CLI flags)

1. Append a source object to `config/external-gtfs-sources.json`.
2. Add `config/<country>-cities.json` with `packageMode: "external"` and `externalGTFSProvider`.
3. Map `COUNTRY_GTFS_URL` → `--external-gtfs-url "<id>=$COUNTRY_GTFS_URL"` in `run_stop_data_pipeline.sh`.
4. Register auth in `EXTERNAL_SOURCE_AUTH` inside `scripts/external_gtfs.py` if the feed needs a key.
5. Add a realtime adapter only when a worker exists.

## rnv regional Live Radar

The pipeline downloads the official rnv static GTFS feed and automatically adds every municipality containing an rnv stop to the Transit Radar manifest. The provider remains disabled until the repository variable `RNV_GATEWAY_URL` contains the HTTPS base URL of a deployed gateway. Setting that single variable enables all generated rnv municipalities during the next data build.

The current static feed resolves to 25 municipalities, including Mannheim, Heidelberg, Ludwigshafen am Rhein, Weinheim, Viernheim, Bad Dürkheim, Schriesheim and the smaller municipalities served by rnv routes. This list is derived on every build and therefore follows future network changes without an app update.

Run the OAuth2 gateway with:

```bash
RNV_OAUTH_URL="..." \
RNV_CLIENT_ID="..." \
RNV_CLIENT_SECRET="..." \
RNV_RESOURCE_ID="..." \
RNV_GTFS_RT_VEHICLE_POSITIONS_URL="..." \
python services/rnv_gateway.py
```

The gateway keeps `CLIENT_SECRET` on the server, caches OAuth tokens until shortly before expiry, retries once after an upstream `401`, and exposes the protobuf feed at `/rnv/vehicle-positions.pb`. Do not embed the rnv OAuth credentials in the iOS application.

## Nürnberg computed Live Radar

VAG PULS exposes active journeys and realtime stop predictions, but not current vehicle coordinates. `services/vag_gateway.py` fetches the public journey data once for all app users, caches trip details, and interpolates bus positions between consecutive stops. The output is explicitly marked as `scheduleEstimate`; it must not be presented as raw GPS.

Run the gateway with:

```bash
python services/vag_gateway.py
```

The service listens on port `8081` by default and exposes `/vag/vehicles.json`. Set the repository variable `VAG_GATEWAY_URL` to its public HTTPS base URL. The next stop-data build then enables Nürnberg without requiring an app update. Do not point every app installation directly at the VAG detail endpoints; the shared gateway is responsible for request coalescing and upstream load control.

Municipality boundaries are provided by the Bundesamt für Kartographie und Geodäsie under Datenlizenz Deutschland – Namensnennung – Version 2.0. Generated data includes `data/attributions.json` with the required source information.

## Dresden computed Live Radar (VVO)

VVO WebAPI provides realtime departures and delays, but not current vehicle coordinates. `services/vvo_gateway.py` fetches departures from `/dm` endpoint, gets trip details from `/dm/trip` endpoint, caches stop catalog from VVO_STOPS.JSON, and interpolates vehicle positions between consecutive stops. The output is explicitly marked as `scheduleEstimate`; it must not be presented as raw GPS.

Run the gateway with:

```bash
python services/vvo_gateway.py
```

The service listens on port `8082` by default and exposes `/vvo/vehicles.json`. Set the repository variable `VVO_GATEWAY_URL` to its public HTTPS base URL. The gateway:
- Fetches departures from major Dresden stops
- Uses VVO_STOPS.JSON for WGS84 coordinates (VVO WebAPI returns GK4)
- Implements linear interpolation between stops
- Supports realtime delay information
- Caches stop catalog and trip details

Dresden (`dresden-de`) is added to `config/cities.json` with the `vvo` adapter and is enabled when `VVO_GATEWAY_URL` is configured in the publishing workflow.
