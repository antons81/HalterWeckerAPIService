import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from build_stop_packages import load_cities, transit_radar_manifest  # noqa: E402
from external_gtfs import (  # noqa: E402
    build_external_lines,
    build_external_stop_packages,
    load_external_cities,
    load_external_gtfs_sources,
)
from fintraffic_gateway import (  # noqa: E402
    GTFSRealtimeProviderContext,
    GTFSRealtimeTripUpdatesGateway,
    GTFSRealtimeVehiclePositionsGateway,
    PublicGTFSRealtimeHTTPTransport,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def varint(value: int) -> bytes:
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def bytes_field(number: int, value: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(value)) + value


def varint_field(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def fixed32_field(number: int, value: float) -> bytes:
    return varint((number << 3) | 5) + struct.pack("<f", value)


def vehicle_feed() -> bytes:
    header = varint_field(3, 900)
    valid_trip = bytes_field(1, b"trip-b") + bytes_field(5, b"route-b")
    valid_position = fixed32_field(1, -27.47) + fixed32_field(2, 153.03)
    valid_vehicle = (
        bytes_field(1, valid_trip)
        + bytes_field(2, valid_position)
        + varint_field(5, 900)
        + bytes_field(7, b"stop-b")
        + bytes_field(8, bytes_field(1, b"brisbane-vehicle"))
    )
    unknown_trip = bytes_field(1, b"unknown-trip") + bytes_field(5, b"unknown-route")
    unknown_position = fixed32_field(1, -27.47) + fixed32_field(2, 153.03)
    unknown_vehicle = (
        bytes_field(1, unknown_trip)
        + bytes_field(2, unknown_position)
        + varint_field(5, 900)
        + bytes_field(8, bytes_field(1, b"unknown-vehicle"))
    )
    return (
        bytes_field(1, header)
        + bytes_field(2, bytes_field(1, b"valid") + bytes_field(4, valid_vehicle))
        + bytes_field(2, bytes_field(1, b"unknown") + bytes_field(4, unknown_vehicle))
    )


def trip_update_feed() -> bytes:
    header = varint_field(3, 900)
    trip = bytes_field(1, b"trip-b") + bytes_field(5, b"route-b")
    event = varint_field(2, 920) + varint_field(1, 20)
    stop_update = bytes_field(1, event) + varint_field(3, 1) + bytes_field(4, b"stop-b")
    trip_update = bytes_field(1, trip) + bytes_field(2, stop_update)
    entity = bytes_field(1, b"update") + bytes_field(3, trip_update)
    return bytes_field(1, header) + bytes_field(2, entity)


def context(city_id: str) -> GTFSRealtimeProviderContext:
    suffix = "b" if city_id == "brisbane" else "g"
    return GTFSRealtimeProviderContext(
        provider_id="australia-translink-seq",
        identifier_prefix="au-seq:",
        stop_id_prefix="au-seq:",
        trips=frozenset({f"au-seq:trip-{suffix}"}),
        routes=frozenset({f"au-seq:route-{suffix}"}),
        route_by_trip={f"au-seq:trip-{suffix}": f"au-seq:route-{suffix}"},
        stops=frozenset({f"au-seq:stop-{suffix}"}),
        trip_headsign_by_trip={f"au-seq:trip-{suffix}": "Brisbane terminal"},
    )


class AustraliaGTFSConfigurationTests(unittest.TestCase):
    def test_canberra_realtime_transport_uses_basic_auth_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CANBERRA_CLIENT_ID": "client", "CANBERRA_CLIENT_SECRET": "secret"},
            clear=False,
        ):
            transport = PublicGTFSRealtimeHTTPTransport.from_basic_auth_environment(
                "HalteWecker-TransportCanberra-GTFSRT/1.0",
                client_id_env="CANBERRA_CLIENT_ID",
                client_secret_env="CANBERRA_CLIENT_SECRET",
            )
        self.assertIsNotNone(transport)
        self.assertEqual(transport._headers["Authorization"], "Basic Y2xpZW50OnNlY3JldA==")

    def test_registry_and_manifest_keep_australian_providers_distinct(self) -> None:
        sources = {
            str(source["id"]): source
            for source in load_external_gtfs_sources(
                REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
            )
        }
        self.assertEqual(
            {
                "australia-translink-seq",
                "australia-adelaide",
                "australia-translink-cairns",
                "australia-translink-bowen",
                "australia-translink-innisfail",
                "australia-translink-fraser-coast",
                "australia-transperth",
                "australia-tasmania",
                "australia-nt-darwin",
                "australia-nt-alice-springs",
                "australia-transport-nsw",
                "australia-transport-canberra",
            },
            {source_id for source_id in sources if source_id.startswith("australia-")},
        )
        self.assertEqual(
            [city["id"] for city in load_external_cities(sources["australia-translink-seq"], REPOSITORY_ROOT)],
            ["brisbane", "gold-coast", "sunshine-coast"],
        )
        self.assertEqual(
            [city["id"] for city in load_external_cities(sources["australia-adelaide"], REPOSITORY_ROOT)],
            ["adelaide"],
        )
        self.assertEqual(
            [city["id"] for city in load_external_cities(sources["australia-transport-nsw"], REPOSITORY_ROOT)],
            ["sydney", "newcastle", "wollongong", "central-coast"],
        )
        self.assertEqual(
            [city["id"] for city in load_external_cities(sources["australia-transport-canberra"], REPOSITORY_ROOT)],
            ["canberra"],
        )

        manifest = transit_radar_manifest(
            load_cities(REPOSITORY_ROOT / "config" / "australia-cities.json")
        )
        by_city = {str(city["appCityID"]): city for city in manifest["cities"]}
        self.assertEqual(by_city["brisbane"]["cityID"], "brisbane-au")
        self.assertEqual(by_city["gold-coast"]["timeZoneIdentifier"], "Australia/Brisbane")
        self.assertEqual(by_city["adelaide"]["timeZoneIdentifier"], "Australia/Adelaide")
        self.assertEqual(
            by_city["brisbane"]["providers"][0]["providerID"],
            "australia-translink-seq-brisbane",
        )
        self.assertEqual(
            by_city["adelaide"]["providers"][0]["providerID"],
            "australia-adelaide",
        )
        static_only = {
            city_id: by_city[city_id]["providers"][0]
            for city_id in ("perth", "hobart", "launceston", "burnie", "darwin", "alice-springs")
        }
        self.assertTrue(all(provider["staticOnly"] for provider in static_only.values()))
        self.assertTrue(all("realtimeDepartures" not in provider["features"] for provider in static_only.values()))
        self.assertEqual(by_city["cairns"]["providers"][0]["adapter"], "translinkQueensland")
        for city_id in ("sydney", "newcastle", "wollongong", "central-coast"):
            provider = by_city[city_id]["providers"][0]
            self.assertEqual(provider["providerID"], "australia-transport-nsw")
            self.assertEqual(provider["adapter"], "externalGTFS")
            self.assertEqual(by_city[city_id]["timeZoneIdentifier"], "Australia/Sydney")
            self.assertIn("realtimeDepartures", provider["features"])
            self.assertIn("vehiclePositions", provider["features"])
        canberra = by_city["canberra"]["providers"][0]
        self.assertEqual(canberra["providerID"], "australia-transport-canberra")
        self.assertEqual(canberra["adapter"], "externalGTFS")
        self.assertEqual(by_city["canberra"]["timeZoneIdentifier"], "Australia/Sydney")
        self.assertIn("realtimeDepartures", canberra["features"])
        self.assertIn("vehiclePositions", canberra["features"])

    def test_city_stop_sets_are_disjoint_and_shared_route_is_preserved(self) -> None:
        cities = [
            {"id": "brisbane", "name": "Brisbane", "latitude": -27.4698, "longitude": 153.0251, "radiusMeters": 35_000},
            {"id": "gold-coast", "name": "Gold Coast", "latitude": -28.0167, "longitude": 153.4, "radiusMeters": 35_000},
            {"id": "sunshine-coast", "name": "Sunshine Coast", "latitude": -26.65, "longitude": 153.0667, "radiusMeters": 40_000},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "seq.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "stops.txt",
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "b,Brisbane,-27.4698,153.0251\n"
                    "g,Gold Coast,-28.0167,153.4000\n"
                    "s,Sunshine Coast,-26.6500,153.0667\n",
                )
                archive.writestr(
                    "routes.txt",
                    "route_id,route_short_name,route_long_name,route_type\nshared,1,Shared route,3\n",
                )
                archive.writestr(
                    "trips.txt",
                    "route_id,service_id,trip_id,trip_headsign,direction_id\nshared,S1,T1,Terminal,0\n",
                )
                archive.writestr(
                    "stop_times.txt",
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "T1,08:00:00,08:00:00,b,1\n"
                    "T1,08:10:00,08:10:00,g,2\n"
                    "T1,08:20:00,08:20:00,s,3\n",
                )
            with zipfile.ZipFile(archive_path) as archive:
                _entries, packages = build_external_stop_packages(
                    archive,
                    cities,
                    root / "out",
                    namespace="au-seq:",
                )
                lines = build_external_lines(archive, packages, namespace="au-seq:")

            stop_sets = [
                {str(stop["id"]) for stop in packages[city_id]}
                for city_id in ("brisbane", "gold-coast", "sunshine-coast")
            ]
            self.assertTrue(all(not (left & right) for index, left in enumerate(stop_sets) for right in stop_sets[index + 1:]))
            for city_id in ("brisbane", "gold-coast", "sunshine-coast"):
                self.assertIn("au-seq:shared", lines[f"au-seq:{city_id[0]}"])

    def test_nsw_exclusive_partition_keeps_overlapping_radii_disjoint(self) -> None:
        cities = [
            {"id": "sydney", "name": "Sydney", "latitude": -33.8688, "longitude": 151.2093, "radiusMeters": 40_000},
            {"id": "central-coast", "name": "Central Coast", "latitude": -33.4269, "longitude": 151.3419, "radiusMeters": 40_000},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "nsw.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "stops.txt",
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "s,Sydney,-33.8688,151.2093\n"
                    "c,Central Coast,-33.4269,151.3419\n"
                    "m,Midpoint,-33.64,151.28\n",
                )
                archive.writestr("routes.txt", "route_id,route_short_name,route_long_name,route_type\nR,1,Shared,3\n")
                archive.writestr("trips.txt", "route_id,service_id,trip_id,trip_headsign,direction_id\nR,S,T,Shared,0\n")
                archive.writestr(
                    "stop_times.txt",
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "T,08:00:00,08:00:00,s,1\nT,08:10:00,08:10:00,m,2\nT,08:20:00,08:20:00,c,3\n",
                )
            with zipfile.ZipFile(archive_path) as archive:
                _entries, packages = build_external_stop_packages(
                    archive,
                    cities,
                    root / "out",
                    namespace="au-nsw:",
                    exclusive_city_partition=True,
                )
            stop_sets = [
                {str(stop["id"]) for stop in packages[city_id]}
                for city_id in ("sydney", "central-coast")
            ]
            self.assertTrue(stop_sets[0].isdisjoint(stop_sets[1]))
            self.assertEqual(stop_sets[0] | stop_sets[1], {"au-nsw:s", "au-nsw:c", "au-nsw:m"})


class AustraliaGTFSRealtimeGatewayTests(unittest.TestCase):
    def test_strict_join_infers_legacy_static_namespace(self) -> None:
        legacy_context = GTFSRealtimeProviderContext(
            provider_id="australia-translink-seq",
            identifier_prefix="",
            stop_id_prefix="",
            trips=frozenset({"au-seq:trip-b"}),
            routes=frozenset({"au-seq:route-b"}),
            route_by_trip={"au-seq:trip-b": "au-seq:route-b"},
            stops=frozenset({"au-seq:stop-b"}),
            trip_headsign_by_trip={"au-seq:trip-b": "Brisbane terminal"},
        )
        gateway = GTFSRealtimeVehiclePositionsGateway(
            provider_id="australia-translink-seq",
            city_ids={"brisbane"},
            city_regions={
                "brisbane": {
                    "minimumLatitude": -27.8,
                    "maximumLatitude": -27.1,
                    "minimumLongitude": 152.5,
                    "maximumLongitude": 153.5,
                }
            },
            context_registry=lambda _city_id: (legacy_context,),
            transport=lambda _url: vehicle_feed(),
            upstream_url="https://example.invalid/seq/vehicles",
            path="/australia/seq/realtime/vehicle-positions",
            clock=lambda: 900,
            strict_static_join=True,
        )

        response = gateway.handle(
            "/australia/seq/realtime/vehicle-positions",
            {"cityID": ["brisbane"]},
        )

        self.assertEqual(response.payload["vehicleCount"], 1)
        self.assertEqual(response.payload["vehicles"][0]["tripID"], "au-seq:trip-b")
        self.assertEqual(response.payload["vehicles"][0]["routeID"], "au-seq:route-b")
        self.assertEqual(response.payload["vehicles"][0]["stopID"], "au-seq:stop-b")
        self.assertEqual(response.payload["vehicles"][0]["destination"], "Brisbane terminal")

    def test_trip_updates_infer_legacy_static_namespace(self) -> None:
        legacy_context = GTFSRealtimeProviderContext(
            provider_id="australia-translink-seq",
            identifier_prefix="",
            stop_id_prefix="",
            trips=frozenset({"au-seq:trip-b"}),
            routes=frozenset({"au-seq:route-b"}),
            route_by_trip={"au-seq:trip-b": "au-seq:route-b"},
            stops=frozenset({"au-seq:stop-b"}),
        )
        gateway = GTFSRealtimeTripUpdatesGateway(
            provider_id="australia-translink-seq",
            city_ids={"brisbane"},
            context_registry=lambda _city_id: (legacy_context,),
            transport=lambda _url: trip_update_feed(),
            upstream_url="https://example.invalid/seq/trip-updates",
            path="/australia/seq/realtime/trip-updates",
            clock=lambda: 900,
            strict_static_join=True,
        )

        response = gateway.handle(
            "/australia/seq/realtime/trip-updates",
            {"cityID": ["brisbane"], "stopIDs": ["au-seq:stop-b"]},
        )

        self.assertEqual(response.payload["updates"][0]["tripID"], "au-seq:trip-b")
        self.assertEqual(response.payload["updates"][0]["routeID"], "au-seq:route-b")
        self.assertEqual(response.payload["updates"][0]["stopID"], "au-seq:stop-b")

    def test_shared_seq_cache_filters_city_and_rejects_unjoined_vehicles(self) -> None:
        calls = []
        gateway = GTFSRealtimeVehiclePositionsGateway(
            provider_id="australia-translink-seq",
            city_ids={"brisbane", "gold-coast"},
            city_regions={
                "brisbane": {"minimumLatitude": -27.8, "maximumLatitude": -27.1, "minimumLongitude": 152.5, "maximumLongitude": 153.5},
                "gold-coast": {"minimumLatitude": -28.4, "maximumLatitude": -27.75, "minimumLongitude": 152.8, "maximumLongitude": 153.9},
            },
            context_registry=lambda city_id: (context(city_id),),
            transport=lambda _url: calls.append("fetch") or vehicle_feed(),
            upstream_url="https://example.invalid/seq/vehicles",
            path="/australia/seq/realtime/vehicle-positions",
            clock=lambda: 900,
            cache_ttl=30,
            max_stale=180,
            strict_static_join=True,
        )
        brisbane = gateway.handle(
            "/australia/seq/realtime/vehicle-positions",
            {"cityID": ["brisbane"]},
        )
        gold_coast = gateway.handle(
            "/australia/seq/realtime/vehicle-positions",
            {"cityID": ["gold-coast"]},
        )
        self.assertEqual(calls, ["fetch"])
        self.assertEqual(brisbane.payload["vehicleCount"], 1)
        self.assertEqual(brisbane.payload["vehicles"][0]["tripID"], "au-seq:trip-b")
        self.assertEqual(gold_coast.payload["vehicleCount"], 0)

    def test_trip_updates_require_matching_static_trip_route_and_stop(self) -> None:
        gateway = GTFSRealtimeTripUpdatesGateway(
            provider_id="australia-translink-seq",
            city_ids={"brisbane"},
            context_registry=lambda _city_id: (context("brisbane"),),
            transport=lambda _url: trip_update_feed(),
            upstream_url="https://example.invalid/seq/trip-updates",
            path="/australia/seq/realtime/trip-updates",
            clock=lambda: 900,
            cache_ttl=30,
            max_stale=180,
            strict_static_join=True,
        )
        response = gateway.handle(
            "/australia/seq/realtime/trip-updates",
            {"cityID": ["brisbane"], "stopIDs": ["au-seq:stop-b"]},
        )
        self.assertEqual(response.payload["updates"][0]["tripID"], "au-seq:trip-b")
        self.assertEqual(response.payload["updates"][0]["stopID"], "au-seq:stop-b")


if __name__ == "__main__":
    unittest.main()
