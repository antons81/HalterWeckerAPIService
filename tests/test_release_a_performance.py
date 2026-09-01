import json
import statistics
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_german_departure_index import connect
from import_static_departures_database import (
    CityScopedStopIDPrefixes,
    populate_provider_city_memberships,
)
from static_departures_ownership import register_entities
from static_departures_ownership import (
    PROVIDER_ENTITIES_BY_TYPE_KEY_INDEX,
    ensure_provider_entity_lookup_index,
)


class ReleaseAPerformanceTests(unittest.TestCase):
    def test_authoritative_factory_uses_selected_provider_city_universe(self) -> None:
        prefixes = CityScopedStopIDPrefixes.from_authoritative_provider_cities(
            {
                "provider-a": [{"id": "city-a"}, {"id": "city-b"}],
                "provider-b": [{"id": "city-b"}],
            },
            {"provider-a": "static-a:", "provider-b": "namespace-b:"},
        )

        self.assertEqual(
            prefixes.authoritative_prefixes_for_city("city-a"),
            {"provider-a": "static-a:"},
        )
        self.assertEqual(
            prefixes.authoritative_prefixes_for_city("city-b"),
            {
                "provider-a": "static-a:",
                "provider-b": "namespace-b:",
            },
        )
        self.assertIsNone(prefixes.authoritative_prefixes_for_city("city-c"))

    def test_old_and_new_memberships_match_for_critical_shapes(self) -> None:
        cases = {
            "single-provider-city": {
                "cities": {"provider-a": ["city"]},
                "prefixes": {"provider-a": "a:"},
                "entities": {"provider-a": ["a:42"]},
            },
            "multi-provider-city-and-shared-stop": {
                "cities": {
                    "provider-a": ["city"],
                    "provider-b": ["city"],
                },
                "prefixes": {"provider-a": "a:", "provider-b": "b:"},
                "entities": {
                    "provider-a": ["a:42"],
                    "provider-b": ["b:42"],
                },
            },
            "same-raw-id-namespace-collision": {
                "cities": {"provider-a": ["city"]},
                "prefixes": {"provider-a": "a:", "provider-b": "b:"},
                "entities": {
                    "provider-a": ["a:42"],
                    "provider-b": ["42", "b:42"],
                },
            },
            "raw-owner-outside-prefix-map": {
                "cities": {"provider-a": ["city"]},
                "prefixes": {"provider-a": "a:"},
                "entities": {"provider-b": ["42"]},
            },
            "provider-prefix-collision": {
                "cities": {
                    "provider-a": ["city"],
                    "provider-b": ["city"],
                },
                "prefixes": {"provider-a": "same:", "provider-b": "same:"},
                "entities": {
                    "provider-a": ["same:42"],
                    "provider-b": ["same:42"],
                },
            },
            "one-provider-multiple-cities": {
                "cities": {"provider-a": ["city-a", "city-b"]},
                "prefixes": {"provider-a": "a:"},
                "entities": {"provider-a": ["a:city-a-stop", "a:city-b-stop"]},
                "stop_ids": {"city-a": ["city-a-stop"], "city-b": ["city-b-stop"]},
            },
        }

        for name, case in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stop_ids = case.get("stop_ids", {"city": ["42"]})
                stop_data = self._write_stop_data(root / "stop-data", stop_ids)
                prefixes = case["prefixes"]
                entities = case["entities"]
                authoritative = self._authoritative_prefixes(case["cities"], prefixes)
                old_result = self._run_membership(
                    root / "old.sqlite",
                    stop_data,
                    stop_ids,
                    prefixes,
                    entities,
                    None,
                )
                new_result = self._run_membership(
                    root / "new.sqlite",
                    stop_data,
                    stop_ids,
                    prefixes,
                    entities,
                    authoritative,
                )
                self.assertEqual(old_result, new_result)

    def test_raw_owner_recovers_global_prefixed_owner_without_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stop_data = self._write_stop_data(root / "stop-data", {"city": ["42"]})
            prefixes = {"provider-a": "a:", "provider-b": "b:"}
            entities = {
                "provider-a": ["a:42"],
                "provider-b": ["42", "b:42"],
            }
            authoritative = self._authoritative_prefixes(
                {"provider-a": ["city"]}, prefixes
            )
            old_result = self._run_membership(
                root / "old.sqlite",
                stop_data,
                {"city": ["42"]},
                prefixes,
                entities,
                None,
            )
            new_result = self._run_membership(
                root / "new.sqlite",
                stop_data,
                {"city": ["42"]},
                prefixes,
                entities,
                authoritative,
            )
            self.assertEqual(old_result, new_result)
            self.assertEqual(
                new_result[2][1],
                [
                    ("provider-a", "city", "42"),
                    ("provider-a", "city", "a:42"),
                    ("provider-b", "city", "42"),
                    ("provider-b", "city", "b:42"),
                ],
            )

    def test_cross_city_prefix_only_owner_preserves_global_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stop_data = self._write_stop_data(
                root / "stop-data", {"city-a": ["42"], "city-b": ["42"]}
            )
            prefixes = {"provider-a": "a:", "provider-b": "b:"}
            entities = {
                "provider-a": ["a:42"],
                "provider-b": ["b:42"],
            }
            authoritative = self._authoritative_prefixes(
                {"provider-a": ["city-a"], "provider-b": ["city-b"]},
                prefixes,
            )
            old_result = self._run_membership(
                root / "old.sqlite",
                stop_data,
                {"city-a": ["42"], "city-b": ["42"]},
                prefixes,
                entities,
                None,
            )
            new_result = self._run_membership(
                root / "new.sqlite",
                stop_data,
                {"city-a": ["42"], "city-b": ["42"]},
                prefixes,
                entities,
                authoritative,
            )
            self.assertEqual(old_result, new_result)

    def test_manual_partial_missing_and_empty_maps_use_global_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stop_data = self._write_stop_data(root / "stop-data", {"city": ["42"]})
            prefixes = {"provider-a": "a:", "provider-b": "b:"}
            entities = {"provider-a": ["a:42"], "provider-b": ["b:42"]}
            old_result = self._run_membership(
                root / "old.sqlite",
                stop_data,
                {"city": ["42"]},
                prefixes,
                entities,
                None,
            )

            manual_maps = {
                "missing": CityScopedStopIDPrefixes(prefixes_by_city={}),
                "empty-city": CityScopedStopIDPrefixes(prefixes_by_city={"city": {}}),
                "partial-city": CityScopedStopIDPrefixes(
                    prefixes_by_city={"city": {"provider-a": "a:"}}
                ),
            }
            for name, manual_map in manual_maps.items():
                with self.subTest(name=name):
                    result = self._run_membership(
                        root / f"{name}.sqlite",
                        stop_data,
                        {"city": ["42"]},
                        prefixes,
                        entities,
                        manual_map,
                    )
                    self.assertEqual(old_result, result)

    def test_missing_ownership_matches_old_failure_without_memberships(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stop_data = self._write_stop_data(root / "stop-data", {"city": ["unowned"]})
            authoritative = self._authoritative_prefixes(
                {"provider-a": ["city"]}, {"provider-a": "a:"}
            )
            old_result = self._run_membership(
                root / "old.sqlite",
                stop_data,
                {"city": ["unowned"]},
                {"provider-a": "a:"},
                {},
                None,
            )
            new_result = self._run_membership(
                root / "new.sqlite",
                stop_data,
                {"city": ["unowned"]},
                {"provider-a": "a:"},
                {},
                authoritative,
            )
            self.assertEqual(old_result, new_result)
            self.assertEqual(old_result[0], "ValueError")
            self.assertEqual(old_result[2], ([], []))

    def test_deterministic_repeated_authoritative_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stop_data = self._write_stop_data(
                root / "stop-data", {"city": ["42", "43"]}
            )
            prefixes = {"provider-a": "a:", "provider-b": "b:"}
            entities = {
                "provider-a": ["a:42", "a:43"],
                "provider-b": ["b:42", "b:43"],
            }
            authoritative = self._authoritative_prefixes(
                {"provider-a": ["city"], "provider-b": ["city"]},
                prefixes,
            )
            results = [
                self._run_membership(
                    root / f"run-{index}.sqlite",
                    stop_data,
                    {"city": ["42", "43"]},
                    prefixes,
                    entities,
                    authoritative,
                )
                for index in range(3)
            ]
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[1], results[2])

    def test_membership_benchmarks_cover_single_and_multi_provider_shapes(self) -> None:
        for multi_provider in (False, True):
            with (
                self.subTest(multi_provider=multi_provider),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                stop_data, city_ids, prefixes, scoped = self._membership_fixture(
                    root / "stop-data", multi_provider=multi_provider
                )
                old_samples = []
                new_samples = []
                snapshots = []
                for run in range(3):
                    old_connection = connect(root / f"membership-old-{run}.sqlite")
                    self._populate_membership_ownership(old_connection, scoped)
                    started = time.perf_counter()
                    populate_provider_city_memberships(
                        old_connection,
                        stop_data,
                        city_ids,
                        stop_id_prefix_by_provider=prefixes,
                        indexed_ownership_lookup=True,
                    )
                    old_samples.append(time.perf_counter() - started)
                    old_snapshot = self._membership_snapshot(old_connection)
                    old_connection.close()

                    new_connection = connect(root / f"membership-new-{run}.sqlite")
                    self._populate_membership_ownership(new_connection, scoped)
                    started = time.perf_counter()
                    populate_provider_city_memberships(
                        new_connection,
                        stop_data,
                        city_ids,
                        stop_id_prefix_by_provider=prefixes,
                        indexed_ownership_lookup=True,
                        city_scoped_prefixes=scoped,
                    )
                    new_samples.append(time.perf_counter() - started)
                    new_snapshot = self._membership_snapshot(new_connection)
                    new_connection.close()
                    self.assertEqual(old_snapshot, new_snapshot)
                    snapshots.append(new_snapshot)

                self.assertEqual(snapshots[0], snapshots[1])
                self.assertEqual(snapshots[1], snapshots[2])
                old_median = statistics.median(old_samples)
                new_median = statistics.median(new_samples)
                label = (
                    "multi-provider-production-like"
                    if multi_provider
                    else "single-provider-heavy"
                )
                print(
                    f"[Release A] {label} membership benchmark "
                    "(100 cities / 5,000 stops / 98 providers)"
                )
                print(f"  old samples: {[round(value, 4) for value in old_samples]}")
                print(f"  new samples: {[round(value, 4) for value in new_samples]}")
                print(
                    "  fixture | old | new | speedup | output_equal\n"
                    f"  {label} | {old_median:.4f}s | {new_median:.4f}s | "
                    f"{old_median / new_median:.2f}x | True"
                )

    def test_cross_city_collision_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stop_data, city_ids, prefixes, scoped = self._cross_city_fixture(
                root / "stop-data"
            )
            old_samples = []
            new_samples = []
            for run in range(3):
                old_connection = connect(root / f"collision-old-{run}.sqlite")
                self._populate_cross_city_ownership(old_connection, prefixes)
                started = time.perf_counter()
                populate_provider_city_memberships(
                    old_connection,
                    stop_data,
                    city_ids,
                    stop_id_prefix_by_provider=prefixes,
                    indexed_ownership_lookup=True,
                )
                old_samples.append(time.perf_counter() - started)
                old_snapshot = self._membership_snapshot(old_connection)
                old_connection.close()

                new_connection = connect(root / f"collision-new-{run}.sqlite")
                self._populate_cross_city_ownership(new_connection, prefixes)
                started = time.perf_counter()
                populate_provider_city_memberships(
                    new_connection,
                    stop_data,
                    city_ids,
                    stop_id_prefix_by_provider=prefixes,
                    indexed_ownership_lookup=True,
                    city_scoped_prefixes=scoped,
                )
                new_samples.append(time.perf_counter() - started)
                new_snapshot = self._membership_snapshot(new_connection)
                new_connection.close()
                self.assertEqual(old_snapshot, new_snapshot)

            old_median = statistics.median(old_samples)
            new_median = statistics.median(new_samples)
            print("[Release A] cross-city-collision-heavy membership benchmark")
            print(f"  old samples: {[round(value, 4) for value in old_samples]}")
            print(f"  new samples: {[round(value, 4) for value in new_samples]}")
            print(
                "  fixture | old | new | speedup | output_equal\n"
                f"  cross-city-collision-heavy | {old_median:.4f}s | "
                f"{new_median:.4f}s | {old_median / new_median:.2f}x | True"
            )

    def test_lookup_index_is_deferred_and_used_by_membership_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "deferred-index.sqlite"
            connection = connect(database_path)
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name=?",
                    (PROVIDER_ENTITIES_BY_TYPE_KEY_INDEX,),
                ).fetchone()
            )
            register_entities(
                connection,
                "provider-a",
                "raw_stops",
                [("a:42",), ("42",)],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name=?",
                    (PROVIDER_ENTITIES_BY_TYPE_KEY_INDEX,),
                ).fetchone()
            )
            stop_data = self._write_stop_data(root / "stop-data", {"city": ["42"]})
            populate_provider_city_memberships(
                connection,
                stop_data,
                {"city"},
                stop_id_prefix_by_provider={"provider-a": "a:"},
                indexed_ownership_lookup=True,
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name=?",
                    (PROVIDER_ENTITIES_BY_TYPE_KEY_INDEX,),
                ).fetchone()
            )
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT provider_id, key_1
                FROM provider_entities
                WHERE entity_type = ? AND key_1 IN (?, ?)
                """,
                ("raw_stops", "42", "a:42"),
            ).fetchall()
            plan_text = " ".join(str(row[-1]) for row in plan)
            self.assertIn(
                f"USING COVERING INDEX {PROVIDER_ENTITIES_BY_TYPE_KEY_INDEX}",
                plan_text,
            )
            connection.close()

    def test_candidate_join_plan_is_key_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            connection = connect(Path(temporary) / "candidate-plan.sqlite")
            register_entities(
                connection,
                "provider-a",
                "raw_stops",
                ((f"a:{index}",) for index in range(100_000)),
            )
            connection.commit()
            ensure_provider_entity_lookup_index(connection)
            connection.executescript(
                """
                CREATE TEMP TABLE scoped_membership_candidate_stop_ids(
                    stop_id TEXT PRIMARY KEY
                ) WITHOUT ROWID;
                INSERT INTO scoped_membership_candidate_stop_ids VALUES ('a:1');
                INSERT INTO scoped_membership_candidate_stop_ids VALUES ('a:2');
                """
            )
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT entities.provider_id, entities.key_1
                FROM provider_entities AS entities
                WHERE entities.entity_type = 'raw_stops'
                  AND entities.key_1 IN (
                      SELECT stop_id FROM scoped_membership_candidate_stop_ids
                  )
                """
            ).fetchall()
            plan_text = " ".join(str(row[-1]) for row in plan)
            self.assertIn(
                f"USING COVERING INDEX {PROVIDER_ENTITIES_BY_TYPE_KEY_INDEX} "
                "(entity_type=? AND key_1=?)",
                plan_text,
            )
            self.assertNotIn("(entity_type=?)", plan_text)
            connection.close()

    def test_lookup_index_timing_variants(self) -> None:
        variants = ("baseline", "before-inserts", "after-inserts")
        samples = {variant: [] for variant in variants}
        storage = {}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for variant in variants:
                for run in range(3):
                    database_path = root / f"{variant}-{run}.sqlite"
                    connection = connect(database_path)
                    index_build_time = 0.0
                    if variant == "before-inserts":
                        started = time.perf_counter()
                        ensure_provider_entity_lookup_index(connection)
                        connection.commit()
                        index_build_time = time.perf_counter() - started
                    started = time.perf_counter()
                    register_entities(
                        connection,
                        "provider-a",
                        "raw_stops",
                        ((f"stop-{index:06d}",) for index in range(100_000)),
                    )
                    connection.commit()
                    insert_time = time.perf_counter() - started
                    if variant == "after-inserts":
                        started = time.perf_counter()
                        ensure_provider_entity_lookup_index(connection)
                        connection.commit()
                        index_build_time = time.perf_counter() - started
                    started = time.perf_counter()
                    rows = connection.execute(
                        """
                        SELECT provider_id, key_1
                        FROM provider_entities
                        WHERE entity_type = ?
                          AND key_1 IN (?, ?, ?, ?)
                        """,
                        (
                            "raw_stops",
                            "stop-000000",
                            "stop-000001",
                            "missing",
                            "stop-099999",
                        ),
                    ).fetchall()
                    lookup_time = time.perf_counter() - started
                    total_time = insert_time + index_build_time + lookup_time
                    self.assertEqual(len(rows), 3)
                    samples[variant].append(
                        (insert_time, index_build_time, lookup_time, total_time)
                    )
                    connection.close()
                    storage.setdefault(variant, database_path.stat().st_size)

            print("[Release B] ownership index timing benchmark")
            print("  variant | insert | index build | lookup | total")
            for variant in variants:
                median = tuple(
                    statistics.median(sample[index] for sample in samples[variant])
                    for index in range(4)
                )
                print(
                    f"  {variant} | {median[0]:.4f}s | {median[1]:.4f}s | "
                    f"{median[2]:.4f}s | {median[3]:.4f}s"
                )
            print(
                "  storage bytes | "
                + ", ".join(f"{variant}={storage[variant]}" for variant in variants)
            )
            self.assertLess(
                statistics.median(sample[2] for sample in samples["after-inserts"]),
                statistics.median(sample[2] for sample in samples["baseline"]),
            )

    def test_candidate_bounded_lookup_scales_with_unrelated_rows(self) -> None:
        results = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for raw_count in (100_000, 500_000):
                database_path = root / f"scale-{raw_count}.sqlite"
                connection = connect(database_path)
                register_entities(
                    connection,
                    "provider-a",
                    "raw_stops",
                    (
                        (f"a:package-{index:02d}",)
                        if index < 20
                        else (f"unrelated-{index:06d}",)
                        for index in range(raw_count)
                    ),
                )
                connection.commit()
                ensure_provider_entity_lookup_index(connection)
                connection.commit()
                candidate_ids = [f"a:package-{index:02d}" for index in range(20)]
                started = time.perf_counter()
                rows = connection.execute(
                    """
                    SELECT provider_id, key_1
                    FROM provider_entities
                    WHERE entity_type = ?
                      AND key_1 IN (%s)
                    """
                    % ",".join("?" for _ in candidate_ids),
                    ("raw_stops", *candidate_ids),
                ).fetchall()
                discovery_time = time.perf_counter() - started
                stop_data = self._write_stop_data(
                    root / f"stop-data-{raw_count}",
                    {"city": [f"package-{index:02d}" for index in range(20)]},
                )
                tracemalloc.start()
                started = time.perf_counter()
                populate_provider_city_memberships(
                    connection,
                    stop_data,
                    {"city"},
                    stop_id_prefix_by_provider={"provider-a": "a:"},
                    indexed_ownership_lookup=True,
                )
                membership_time = time.perf_counter() - started
                _current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                self.assertEqual(len(rows), 20)
                results.append(
                    (
                        raw_count,
                        discovery_time,
                        membership_time,
                        peak,
                        database_path.stat().st_size,
                    )
                )
                connection.close()

        print("[Release B] unrelated ownership scalability benchmark")
        print("  raw_stops | discovery | membership | peak Python memory | db bytes")
        for raw_count, discovery, membership, peak, db_bytes in results:
            print(
                f"  {raw_count} | {discovery:.4f}s | {membership:.4f}s | "
                f"{peak / 1024 / 1024:.2f} MiB | {db_bytes}"
            )
        self.assertLess(results[1][1], results[0][1] * 5 + 0.05)

    @staticmethod
    def _authoritative_prefixes(cities_by_provider, prefixes):
        return CityScopedStopIDPrefixes.from_authoritative_provider_cities(
            {
                provider: [{"id": city_id} for city_id in city_ids]
                for provider, city_ids in cities_by_provider.items()
            },
            prefixes,
        )

    @staticmethod
    def _run_membership(
        database_path: Path,
        stop_data: Path,
        stop_ids_by_city: dict[str, list[str]],
        prefixes: dict[str, str],
        entities: dict[str, list[str]],
        city_scoped_prefixes: CityScopedStopIDPrefixes | None,
    ):
        connection = connect(database_path)
        try:
            for provider_id, keys in entities.items():
                register_entities(
                    connection,
                    provider_id,
                    "raw_stops",
                    [(key,) for key in keys],
                )
            try:
                populate_provider_city_memberships(
                    connection,
                    stop_data,
                    set(stop_ids_by_city),
                    stop_id_prefix_by_provider=prefixes,
                    indexed_ownership_lookup=True,
                    city_scoped_prefixes=city_scoped_prefixes,
                )
                status = "OK"
                message = ""
            except ValueError as error:
                status = type(error).__name__
                message = str(error)
            return (
                status,
                message,
                ReleaseAPerformanceTests._membership_snapshot(connection),
            )
        finally:
            connection.close()

    @staticmethod
    def _write_stop_data(path: Path, city_stops: dict[str, list[str]]) -> Path:
        (path / "stops").mkdir(parents=True)
        cities = []
        for city_id, stop_ids in sorted(city_stops.items()):
            url = f"stops/{city_id}.json"
            cities.append({"id": city_id, "url": url})
            (path / url).write_text(
                json.dumps([{"id": stop_id} for stop_id in stop_ids]),
                encoding="utf-8",
            )
        (path / "manifest.json").write_text(
            json.dumps({"cities": cities}), encoding="utf-8"
        )
        return path

    @staticmethod
    def _membership_fixture(path: Path, multi_provider: bool):
        path.joinpath("stops").mkdir(parents=True)
        city_ids = {f"city-{index:03d}" for index in range(100)}
        providers = [f"provider-{index:02d}" for index in range(98)]
        prefixes = {provider: f"{provider}:" for provider in providers}
        cities_by_provider = {provider: [] for provider in providers}
        for index, city_id in enumerate(sorted(city_ids)):
            provider_index = index % len(providers)
            selected_providers = [providers[provider_index]]
            if multi_provider and index < 26:
                additional_count = 2 if index < 6 else 1
                selected_providers.extend(
                    providers[(provider_index + offset) % len(providers)]
                    for offset in range(1, additional_count + 1)
                )
            for provider in selected_providers:
                cities_by_provider[provider].append(city_id)
            city_index = int(city_id.removeprefix("city-"))
            (path / f"stops/{city_id}.json").write_text(
                json.dumps(
                    [
                        {"id": f"stop-{city_index:03d}-{stop_index:03d}"}
                        for stop_index in range(50)
                    ]
                ),
                encoding="utf-8",
            )
        (path / "manifest.json").write_text(
            json.dumps(
                {
                    "cities": [
                        {"id": city_id, "url": f"stops/{city_id}.json"}
                        for city_id in sorted(city_ids)
                    ]
                }
            ),
            encoding="utf-8",
        )
        return (
            path,
            city_ids,
            prefixes,
            ReleaseAPerformanceTests._authoritative_prefixes(
                cities_by_provider, prefixes
            ),
        )

    @staticmethod
    def _cross_city_fixture(path: Path):
        path.joinpath("stops").mkdir(parents=True)
        city_ids = {f"city-{index:03d}" for index in range(100)}
        providers = [f"provider-{index:02d}" for index in range(98)]
        prefixes = {provider: f"{provider}:" for provider in providers}
        cities_by_provider = {provider: [] for provider in providers}
        for index, city_id in enumerate(sorted(city_ids)):
            cities_by_provider[providers[index % len(providers)]].append(city_id)
            (path / f"stops/{city_id}.json").write_text(
                json.dumps(
                    [
                        {"id": f"shared-stop-{stop_index:03d}"}
                        for stop_index in range(50)
                    ]
                ),
                encoding="utf-8",
            )
        (path / "manifest.json").write_text(
            json.dumps(
                {
                    "cities": [
                        {"id": city_id, "url": f"stops/{city_id}.json"}
                        for city_id in sorted(city_ids)
                    ]
                }
            ),
            encoding="utf-8",
        )
        return (
            path,
            city_ids,
            prefixes,
            ReleaseAPerformanceTests._authoritative_prefixes(
                cities_by_provider, prefixes
            ),
        )

    @staticmethod
    def _populate_cross_city_ownership(connection, prefixes) -> None:
        for provider, prefix in prefixes.items():
            register_entities(
                connection,
                provider,
                "raw_stops",
                [
                    (f"{prefix}shared-stop-{stop_index:03d}",)
                    for stop_index in range(50)
                ],
            )

    @staticmethod
    def _populate_membership_ownership(connection, scoped) -> None:
        for city_id, provider_map in scoped.prefixes_by_city.items():
            city_index = int(city_id.removeprefix("city-"))
            for provider, prefix in provider_map.items():
                register_entities(
                    connection,
                    provider,
                    "raw_stops",
                    [
                        (f"{prefix}stop-{city_index:03d}-{stop_index:03d}",)
                        for stop_index in range(50)
                    ],
                )

    @staticmethod
    def _membership_snapshot(connection):
        return (
            connection.execute(
                "SELECT city_id, stop_id FROM city_stops ORDER BY city_id, stop_id"
            ).fetchall(),
            connection.execute(
                """
                SELECT provider_id, city_id, stop_id
                FROM provider_city_stops
                ORDER BY provider_id, city_id, stop_id
                """
            ).fetchall(),
        )


if __name__ == "__main__":
    unittest.main()
