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
- `data/attributions.json`

Each stop has `id`, `name`, `latitude`, `longitude`, and `searchName` fields. The iOS app validates downloaded JSON and later imports it into its local SQLite FTS index.

The pipeline downloads the official BKG VG250 municipality boundaries and assigns every German stop to its municipality. Only municipalities containing at least one stop are published. Stable automatic city IDs contain the municipality's official AGS code. Cities in `config/cities.json` keep their existing IDs, aliases, larger configured radii, and Transit Radar configuration.

City line catalogs are derived from the GTFS relationship `stop_times → trips → routes`. They contain only routes serving at least one stop inside the selected municipality. The iOS app uses these optional catalogs to scope regional realtime vehicle feeds to the selected city and falls back to the unfiltered live feed when no valid catalog is available.

Municipality boundaries are provided by the Bundesamt für Kartographie und Geodäsie under Datenlizenz Deutschland – Namensnennung – Version 2.0. Generated data includes `data/attributions.json` with the required source information.
