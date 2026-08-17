"""Read-only benchmark for Kyiv radar topology candidate fan-out."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from kyiv_radar_inference import KyivRadarTopology


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: benchmark_kyiv_radar.py /path/to/radar/kyiv.json")
        return 2
    path = Path(sys.argv[1])
    topology = KyivRadarTopology.from_path(path)
    rows = []
    for route_id in sorted(json.loads(path.read_text(encoding="utf-8")).get("routes", {})):
        candidates = topology.candidates(route_id)
        shapes = sum(len(candidate.shapes) for candidate in candidates)
        segments = sum(len(shape.segments) for candidate in candidates for shape in candidate.shapes)
        rows.append((len(candidates), shapes, segments, route_id))
    print(f"routes={len(rows)} candidates={sum(row[0] for row in rows)} shapes={sum(row[1] for row in rows)} segments={sum(row[2] for row in rows)}")
    for row in sorted(rows, reverse=True)[:10]:
        print(f"max route_id={row[3]} candidates={row[0]} shapes={row[1]} segments={row[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
