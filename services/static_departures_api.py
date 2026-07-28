#!/usr/bin/env python3
"""Read-only static GTFS departure API with automatic SQLite inode reopen."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Database:
    def __init__(self, path: str, ttl: float = 2.0) -> None:
        self.path, self.ttl, self.connection, self.identity, self.checked_at = path, ttl, None, None, 0.0
        self.lock = threading.Lock()

    def get(self) -> sqlite3.Connection:
        with self.lock:
            now = time.monotonic()
            identity = (os.stat(self.path).st_dev, os.stat(self.path).st_ino, os.stat(self.path).st_mtime_ns)
            if self.connection is None or (now - self.checked_at >= self.ttl and identity != self.identity):
                if self.connection is not None:
                    self.connection.close()
                self.connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
                self.connection.execute("PRAGMA query_only=ON")
                self.identity = identity
            self.checked_at = now
            return self.connection

    def meta(self) -> dict[str, str]:
        return dict(self.get().execute("SELECT key, value FROM metadata"))


class Handler(BaseHTTPRequestHandler):
    database: Database

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self) -> None:
        parsed, query = urlparse(self.path), parse_qs(urlparse(self.path).query)
        try:
            if parsed.path == "/static-departures/health": return self.send_json(200, {"ok": True, "database": self.database.meta()})
            if parsed.path == "/static-departures/meta": return self.send_json(200, self.database.meta())
            city, stop = query.get("cityID", [None])[0], query.get("stopID", [None])[0]
            if not city or not stop: return self.send_json(400, {"error": "cityID and stopID are required"})
            connection = self.database.get()
            if not connection.execute("SELECT 1 FROM city_stops WHERE city_id=? AND stop_id=?", (city, stop)).fetchone(): return self.send_json(404, {"error": "unknown cityID/stopID"})
            if parsed.path == "/static-departures/lines":
                rows = connection.execute("SELECT DISTINCT t.route_id, COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id), t.direction_id FROM stop_times s JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id JOIN trips t ON t.trip_id=s.trip_id LEFT JOIN routes r ON r.route_id=t.route_id WHERE rs.canonical_stop_id=? ORDER BY 2,1", (stop,)).fetchall()
                return self.send_json(200, {"cityID": city, "stopID": stop, "lines": [{"routeID": a,"line": b,"direction": c or None} for a,b,c in rows]})
            if parsed.path == "/static-departures/board":
                limit = min(max(int(query.get("limit", ["30"])[0]), 1), 100)
                rows = connection.execute("SELECT a.service_date,s.departure_time,t.trip_id,t.route_id,COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),t.headsign,t.direction_id,rs.platform_code FROM stop_times s JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id JOIN trips t ON t.trip_id=s.trip_id JOIN active_services a ON a.service_id=t.service_id LEFT JOIN routes r ON r.route_id=t.route_id WHERE rs.canonical_stop_id=? ORDER BY a.service_date,s.departure_seconds LIMIT ?", (stop, limit)).fetchall()
                return self.send_json(200, {"cityID": city, "stopID": stop, "departures": [{"serviceDate": f"{d[:4]}-{d[4:6]}-{d[6:]}","scheduledTime": tm,"tripID": trip,"routeID": route,"line": line,"destination": headsign,"direction": direction or None,"platform": platform or None,"isRealtime": False} for d,tm,trip,route,line,headsign,direction,platform in rows]})
            return self.send_json(404, {"error": "not found"})
        except Exception as error:
            self.send_json(500, {"error": str(error)})


if __name__ == "__main__":
    database = Database(os.environ.get("DEPARTURES_DATABASE", "/data/departures-current.sqlite"))
    Handler.database = database
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
