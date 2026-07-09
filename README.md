# HalterWecker API Service

Static GTFS pipeline for HalteWecker.

## Setup

1. Add a repository variable named `GTFS_URL` in **Settings → Secrets and variables → Actions**. Its value must be the public GTFS ZIP URL.
2. In **Settings → Pages**, choose **GitHub Actions** as the publishing source.
3. Run **Update stop data** manually once.

The workflow runs on the first day of each month and publishes:

- `data/manifest.json`
- `data/cities.json`
- `data/stops/{cityId}.json`

Each stop has `id`, `name`, `latitude`, `longitude`, and `searchName` fields. The iOS app validates downloaded JSON and later imports it into its local SQLite FTS index.

The initial city assignment uses a radius around each configured city center. Before nationwide release it should be replaced by an administrative-boundary spatial join.
