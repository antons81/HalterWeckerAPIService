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

The pipeline downloads the official BKG VG250 municipality boundaries and assigns every German stop to its municipality. Only municipalities containing at least one stop are published. Stable automatic city IDs contain the municipality's official AGS code. Cities in `config/cities.json` keep their existing IDs, aliases, larger configured radii, and Transit Radar configuration.

City line catalogs are derived from the GTFS relationship `stop_times → trips → routes`. They contain only routes serving at least one stop inside the selected municipality. The iOS app uses these optional catalogs to scope regional realtime vehicle feeds to the selected city and falls back to the unfiltered live feed when no valid catalog is available.

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
