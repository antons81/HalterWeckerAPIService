"""Conservative direction inference for Kyiv surface VehiclePositions."""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

EARTH_RADIUS_METERS = 6_371_000.0
MIN_PROGRESS_DELTA_METERS = 25.0
MAX_SAMPLE_GAP_SECONDS = 300
MAX_SPEED_METERS_PER_SECOND = 45.0
MAX_SHAPE_DISTANCE_METERS = 1_000.0
MAX_CONFIDENT_SHAPE_DISTANCE_METERS = 100.0
MIN_DISTANCE_MARGIN_METERS = 20.0
MAX_BEARING_DEVIATION_DEGREES = 35.0
SHAPE_DISTANCE_SCALE_METERS = 500.0
HISTORY_TTL_SECONDS = 600.0
HYSTERESIS_RETENTION_SECONDS = 180.0
MAX_HISTORY_VEHICLES = 10_000
MAX_SAMPLES_PER_VEHICLE = 5
SHAPE_GRID_SIZE_DEGREES = 0.01
SHAPE_GRID_RADIUS_CELLS = 2
MAX_INDEXED_CELLS_PER_SEGMENT = 256
MIN_CONFIDENCE = 0.58
MIN_MARGIN = 0.20
STRONG_SWITCH_CONFIDENCE = 0.78
STRONG_SWITCH_MARGIN = 0.25
REQUIRED_SWITCH_SAMPLES = 2


@dataclass(frozen=True)
class KyivRadarShape:
    shape_id: str
    points: tuple[tuple[float, float, float], ...]
    length_meters: float
    minimum_latitude: float
    maximum_latitude: float
    minimum_longitude: float
    maximum_longitude: float
    segments: tuple[tuple[float, float, float, float, float, float], ...]
    segment_grid: dict[tuple[int, int], tuple[int, ...]]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> KyivRadarShape | None:
        shape_id = str(payload.get("shapeID", "")).strip()
        raw_points = payload.get("points")
        if not shape_id or not isinstance(raw_points, list):
            return None
        points: list[tuple[float, float, float]] = []
        for raw_point in raw_points:
            if not isinstance(raw_point, list) or len(raw_point) < 3:
                continue
            try:
                latitude, longitude, progress = map(float, raw_point[:3])
            except (TypeError, ValueError):
                continue
            if not (
                math.isfinite(latitude)
                and math.isfinite(longitude)
                and math.isfinite(progress)
                and -90 <= latitude <= 90
                and -180 <= longitude <= 180
            ):
                continue
            points.append((latitude, longitude, max(0.0, progress)))
        if len(points) < 2:
            return None
        progress = max(point[2] for point in points)
        segments = tuple(
            (
                first[0],
                first[1],
                first[2],
                second[0],
                second[1],
                second[2],
            )
            for first, second in pairwise(points)
        )
        grid: dict[tuple[int, int], list[int]] = {}
        for index, segment in enumerate(segments):
            minimum_latitude = math.floor(
                min(segment[0], segment[3]) / SHAPE_GRID_SIZE_DEGREES
            )
            maximum_latitude = math.floor(
                max(segment[0], segment[3]) / SHAPE_GRID_SIZE_DEGREES
            )
            minimum_longitude = math.floor(
                min(segment[1], segment[4]) / SHAPE_GRID_SIZE_DEGREES
            )
            maximum_longitude = math.floor(
                max(segment[1], segment[4]) / SHAPE_GRID_SIZE_DEGREES
            )
            cell_count = (maximum_latitude - minimum_latitude + 1) * (
                maximum_longitude - minimum_longitude + 1
            )
            if cell_count > MAX_INDEXED_CELLS_PER_SEGMENT:
                continue
            for latitude_cell in range(minimum_latitude, maximum_latitude + 1):
                for longitude_cell in range(minimum_longitude, maximum_longitude + 1):
                    grid.setdefault((latitude_cell, longitude_cell), []).append(index)
        return cls(
            shape_id=shape_id,
            points=tuple(points),
            length_meters=max(float(payload.get("lengthMeters", 0.0) or 0.0), progress),
            minimum_latitude=min(point[0] for point in points),
            maximum_latitude=max(point[0] for point in points),
            minimum_longitude=min(point[1] for point in points),
            maximum_longitude=max(point[1] for point in points),
            segments=segments,
            segment_grid={key: tuple(value) for key, value in grid.items()},
        )


@dataclass(frozen=True)
class KyivRadarCandidate:
    variant_id: str
    direction_id: str
    destination: str | None
    terminal_stop_id: str | None
    trip_ids: frozenset[str]
    stop_ids: tuple[str, ...]
    stop_sequences: tuple[int, ...]
    shapes: tuple[KyivRadarShape, ...]


class KyivRadarTopology:
    def __init__(self, routes: Mapping[str, tuple[KyivRadarCandidate, ...]]) -> None:
        self._routes = dict(routes)

    @classmethod
    def empty(cls) -> KyivRadarTopology:
        return cls({})

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> KyivRadarTopology:
        raw_routes = payload.get("routes")
        if not isinstance(raw_routes, dict):
            return cls.empty()

        routes: dict[str, tuple[KyivRadarCandidate, ...]] = {}
        for raw_route_id, raw_route in raw_routes.items():
            if not isinstance(raw_route, dict):
                continue
            raw_directions = raw_route.get("directions")
            if not isinstance(raw_directions, list):
                continue
            candidates: list[KyivRadarCandidate] = []
            for raw_direction in raw_directions:
                if not isinstance(raw_direction, dict):
                    continue
                direction_id = str(raw_direction.get("directionID", "")).strip()
                raw_shapes = raw_direction.get("shapes")
                if not direction_id or not isinstance(raw_shapes, list):
                    continue
                shapes = tuple(
                    shape
                    for raw_shape in raw_shapes
                    if isinstance(raw_shape, dict)
                    for shape in [KyivRadarShape.from_payload(raw_shape)]
                    if shape is not None
                )
                if not shapes:
                    continue
                destination = str(raw_direction.get("destination", "")).strip() or None
                raw_trip_ids = raw_direction.get("tripIDs")
                trip_ids = frozenset(
                    str(item).strip() for item in raw_trip_ids if str(item).strip()
                ) if isinstance(raw_trip_ids, list) else frozenset()
                raw_stop_ids = raw_direction.get("stopIDs")
                stop_ids = tuple(
                    str(item).strip() for item in raw_stop_ids if str(item).strip()
                ) if isinstance(raw_stop_ids, list) else ()
                raw_stop_sequences = raw_direction.get("stopSequences")
                stop_sequences = tuple(
                    int(item) for item in raw_stop_sequences
                    if isinstance(item, (int, float, str)) and str(item).strip()
                ) if isinstance(raw_stop_sequences, list) else ()
                terminal_stop_id = str(raw_direction.get("terminalStopID", "")).strip() or None
                shape_ids = ",".join(shape.shape_id for shape in shapes)
                variant_id = str(raw_direction.get("variantID", "")).strip() or (
                    f"{raw_route_id}:{direction_id}:{shape_ids}:{destination or ''}"
                )
                candidates.append(
                    KyivRadarCandidate(
                        variant_id,
                        direction_id,
                        destination,
                        terminal_stop_id,
                        trip_ids,
                        stop_ids,
                        stop_sequences,
                        shapes,
                    )
                )
            if candidates:
                routes[str(raw_route_id)] = tuple(candidates)
        return cls(routes)

    @classmethod
    def from_path(cls, path: str | Path | None) -> KyivRadarTopology:
        if path is None:
            return cls.empty()
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return cls.empty()
        return cls.from_payload(payload) if isinstance(payload, dict) else cls.empty()

    def candidates(self, route_id: str) -> tuple[KyivRadarCandidate, ...]:
        return self._routes.get(route_id, ())

    def route_count(self) -> int:
        return len(self._routes)


@dataclass(frozen=True)
class KyivVehicleSample:
    timestamp: int
    latitude: float
    longitude: float
    trip_id: str | None = None
    stop_id: str | None = None
    stop_sequence: int | None = None
    bearing: float | None = None


@dataclass(frozen=True)
class KyivDirectionDecision:
    direction_id: str | None
    destination: str | None
    confidence: float | None
    margin: float | None
    reason: str
    variant_id: str | None = None
    evidence_source: str = "unknown"


@dataclass(frozen=True)
class _Projection:
    distance_meters: float
    progress_meters: float
    bearing_degrees: float | None = None


@dataclass(frozen=True)
class _CandidateScore:
    candidate: KyivRadarCandidate
    score: float
    support: float
    contrary: float
    usable_shapes: int
    best_distance_meters: float


@dataclass(frozen=True)
class KyivDestinationFamily:
    route_id: str
    terminal_stop_id: str | None
    destination: str | None
    variants: tuple[KyivRadarCandidate, ...]

    @property
    def family_id(self) -> str:
        identity = self.terminal_stop_id or self.destination or "unknown"
        return f"{self.route_id}:{identity}"


@dataclass(frozen=True)
class _FamilyScore:
    family: KyivDestinationFamily
    best_variant: _CandidateScore


@dataclass
class _VehicleState:
    route_id: str
    samples: deque[KyivVehicleSample] = field(
        default_factory=lambda: deque(maxlen=MAX_SAMPLES_PER_VEHICLE)
    )
    confirmed_direction_id: str | None = None
    confirmed_variant_id: str | None = None
    confirmed_family_id: str | None = None
    confirmed_destination: str | None = None
    confirmed_confidence: float | None = None
    last_confident_at: float | None = None
    pending_direction_id: str | None = None
    pending_variant_id: str | None = None
    pending_family_id: str | None = None
    pending_destination: str | None = None
    pending_count: int = 0
    last_seen_at: float = 0.0


class KyivDirectionInference:
    """Thread-safe, non-persistent inference state keyed by realtime vehicle ID."""

    def __init__(
        self,
        topology: KyivRadarTopology,
        *,
        clock: Callable[[], float] = time.time,
        history_ttl: float = HISTORY_TTL_SECONDS,
        max_history_vehicles: int = MAX_HISTORY_VEHICLES,
    ) -> None:
        self._topology = topology
        self._clock = clock
        self._history_ttl = history_ttl
        self._max_history_vehicles = max(1, max_history_vehicles)
        self._states: dict[str, _VehicleState] = {}
        self._lock = threading.Lock()

    def observe(
        self,
        vehicle_id: str,
        route_id: str,
        sample: KyivVehicleSample,
    ) -> KyivDirectionDecision:
        now = self._clock()
        with self._lock:
            self._purge(now)
            state = self._states.get(vehicle_id)
            if state is None or state.route_id != route_id:
                state = _VehicleState(route_id=route_id, last_seen_at=now)
                self._states[vehicle_id] = state
            state.last_seen_at = now

            identity_candidates = self._identity_candidates(state.route_id, sample)
            if len(identity_candidates) == 1:
                candidate = identity_candidates[0]
                family_id = self._family_id(state.route_id, candidate)
                state.samples.append(sample)
                state.confirmed_direction_id = candidate.direction_id
                state.confirmed_variant_id = candidate.variant_id
                state.confirmed_family_id = family_id
                state.confirmed_destination = candidate.destination
                state.confirmed_confidence = 1.0
                state.last_confident_at = now
                return KyivDirectionDecision(
                    candidate.direction_id,
                    candidate.destination,
                    1.0,
                    1.0,
                    "identity",
                    candidate.variant_id,
                    "trip-id" if sample.trip_id and sample.trip_id in candidate.trip_ids else "stop-id",
                )

            previous = state.samples[-1] if state.samples else None
            if previous is None:
                state.samples.append(sample)
                return self._decision_from_state(state, "first-sample")
            if sample.timestamp <= previous.timestamp:
                return self._decision_from_state(state, "non-monotonic-sample")

            elapsed = sample.timestamp - previous.timestamp
            if elapsed > MAX_SAMPLE_GAP_SECONDS:
                self._reset_direction(state)
                state.samples.clear()
                state.samples.append(sample)
                return self._decision_from_state(state, "sample-gap-reset")

            state.samples.append(sample)
            scores = self._score(state.route_id, previous, sample)
            if scores is None:
                return self._retain_or_unknown(state, now, "ambiguous-or-stationary")
            return self._apply_scores(state, scores, now)

    def history_size(self) -> int:
        with self._lock:
            self._purge(self._clock())
            return len(self._states)

    def _purge(self, now: float) -> None:
        expired = [
            vehicle_id
            for vehicle_id, state in self._states.items()
            if now - state.last_seen_at > self._history_ttl
        ]
        for vehicle_id in expired:
            self._states.pop(vehicle_id, None)
        overflow = len(self._states) - self._max_history_vehicles
        if overflow <= 0:
            return
        oldest = sorted(self._states.items(), key=lambda item: item[1].last_seen_at)
        for vehicle_id, _ in oldest[:overflow]:
            self._states.pop(vehicle_id, None)

    @staticmethod
    def _reset_direction(state: _VehicleState) -> None:
        state.confirmed_direction_id = None
        state.confirmed_variant_id = None
        state.confirmed_family_id = None
        state.confirmed_destination = None
        state.confirmed_confidence = None
        state.last_confident_at = None
        state.pending_direction_id = None
        state.pending_variant_id = None
        state.pending_family_id = None
        state.pending_destination = None
        state.pending_count = 0

    @staticmethod
    def _reset_pending(state: _VehicleState) -> None:
        state.pending_direction_id = None
        state.pending_variant_id = None
        state.pending_family_id = None
        state.pending_destination = None
        state.pending_count = 0

    @staticmethod
    def _decision_from_state(state: _VehicleState, reason: str) -> KyivDirectionDecision:
        return KyivDirectionDecision(
            state.confirmed_direction_id,
            state.confirmed_destination,
            state.confirmed_confidence,
            None,
            reason,
            state.confirmed_variant_id,
            "history" if state.confirmed_variant_id else reason,
        )

    def _identity_candidates(
        self,
        route_id: str,
        sample: KyivVehicleSample,
    ) -> tuple[KyivRadarCandidate, ...]:
        candidates = self._topology.candidates(route_id)
        if sample.trip_id:
            matched = tuple(candidate for candidate in candidates if sample.trip_id in candidate.trip_ids)
            if matched:
                return matched
        if sample.stop_id:
            matched = tuple(
                candidate for candidate in candidates
                if sample.stop_id in candidate.stop_ids
                and (
                    sample.stop_sequence is None
                    or not candidate.stop_sequences
                    or sample.stop_sequence in candidate.stop_sequences
                )
            )
            if matched:
                return matched
        return ()

    def _retain_or_unknown(
        self,
        state: _VehicleState,
        now: float,
        reason: str,
    ) -> KyivDirectionDecision:
        self._reset_pending(state)
        if (
            state.confirmed_direction_id is not None
            and state.last_confident_at is not None
            and now - state.last_confident_at <= HYSTERESIS_RETENTION_SECONDS
        ):
            return KyivDirectionDecision(
                None,
                None,
                state.confirmed_confidence,
                None,
                f"retained-{reason}",
                None,
                "history",
            )
        self._reset_direction(state)
        return self._decision_from_state(state, reason)

    def _score(
        self,
        route_id: str,
        previous: KyivVehicleSample,
        current: KyivVehicleSample,
    ) -> tuple[_CandidateScore, ...] | None:
        candidates = self._topology.candidates(route_id)
        if not candidates:
            return None
        elapsed = current.timestamp - previous.timestamp
        scored: list[_CandidateScore] = []
        for candidate in candidates:
            support = contrary = 0.0
            usable_shapes = 0
            best_distance = float("inf")
            for shape in candidate.shapes:
                before = _project(shape, previous.latitude, previous.longitude)
                after = _project(shape, current.latitude, current.longitude)
                if max(before.distance_meters, after.distance_meters) > MAX_SHAPE_DISTANCE_METERS:
                    continue
                delta = after.progress_meters - before.progress_meters
                if abs(delta) < MIN_PROGRESS_DELTA_METERS:
                    continue
                if abs(delta) / elapsed > MAX_SPEED_METERS_PER_SECOND:
                    continue
                bearing_conflict = (
                    current.bearing is not None
                    and after.bearing_degrees is not None
                    and _bearing_difference(current.bearing, after.bearing_degrees)
                    > MAX_BEARING_DEVIATION_DEGREES
                )
                weight = math.exp(
                    -((before.distance_meters + after.distance_meters) / 2.0)
                    / SHAPE_DISTANCE_SCALE_METERS
                )
                usable_shapes += 1
                best_distance = min(best_distance, after.distance_meters)
                if delta > 0:
                    support += weight
                else:
                    contrary += weight
                if bearing_conflict:
                    contrary += weight
            total = support + contrary
            if usable_shapes == 0 or total <= 0:
                continue
            scored.append(
                _CandidateScore(
                    candidate=candidate,
                    score=max(0.0, (support - contrary) / total),
                    support=support,
                    contrary=contrary,
                    usable_shapes=usable_shapes,
                    best_distance_meters=best_distance,
                )
            )
        if not scored:
            return None
        return tuple(scored)

    def _destination_families(
        self,
        route_id: str,
        scores: tuple[_CandidateScore, ...],
    ) -> tuple[_FamilyScore, ...]:
        families: dict[tuple[str | None, str | None], list[_CandidateScore]] = {}
        candidates_by_identity: dict[
            tuple[str | None, str | None], list[KyivRadarCandidate]
        ] = {}
        for score in scores:
            identity = (score.candidate.terminal_stop_id, score.candidate.destination)
            families.setdefault(identity, []).append(score)
            candidates_by_identity.setdefault(identity, []).append(score.candidate)

        family_scores: list[_FamilyScore] = []
        for identity, family_scores_for_variants in families.items():
            best_variant = max(
                family_scores_for_variants,
                key=lambda item: (item.score, item.support, -item.best_distance_meters),
            )
            terminal_stop_id, destination = identity
            family_scores.append(
                _FamilyScore(
                    KyivDestinationFamily(
                        route_id,
                        terminal_stop_id,
                        destination,
                        tuple(candidates_by_identity[identity]),
                    ),
                    best_variant,
                )
            )
        return tuple(family_scores)

    @staticmethod
    def _family_id(route_id: str, candidate: KyivRadarCandidate) -> str:
        identity = candidate.terminal_stop_id or candidate.destination or "unknown"
        return f"{route_id}:{identity}"

    def _apply_scores(
        self,
        state: _VehicleState,
        scores: tuple[_CandidateScore, ...],
        now: float,
    ) -> KyivDirectionDecision:
        eligible = tuple(
            score for score in scores
            if score.best_distance_meters <= MAX_CONFIDENT_SHAPE_DISTANCE_METERS
        )
        if not eligible:
            return self._retain_or_unknown(state, now, "distance")
        family_scores = self._destination_families(state.route_id, eligible)
        ordered = sorted(
            family_scores,
            key=lambda item: (
                -item.best_variant.score,
                item.best_variant.best_distance_meters,
            ),
        )
        best_family = ordered[0]
        best = best_family.best_variant
        second_family = ordered[1] if len(ordered) > 1 else None
        second = second_family.best_variant if second_family else None
        margin = best.score - second.score if second else 1.0
        distance_margin = (
            second.best_distance_meters - best.best_distance_meters
            if second else float("inf")
        )
        closest_distance_by_family = {
            family_score.family.family_id: min(
                item.best_distance_meters
                for item in eligible
                if (
                    item.candidate.terminal_stop_id,
                    item.candidate.destination,
                ) == (
                    family_score.family.terminal_stop_id,
                    family_score.family.destination,
                )
            )
            for family_score in family_scores
        }
        closest_family_id = min(
            closest_distance_by_family,
            key=closest_distance_by_family.get,
        )
        best_is_spatially_dominated = (
            best.best_distance_meters
            - closest_distance_by_family[best_family.family.family_id]
            >= MIN_DISTANCE_MARGIN_METERS
            and closest_family_id != best_family.family.family_id
        )
        same_direction_competition = (
            second is not None
            and second.candidate.direction_id == best.candidate.direction_id
        )
        confident = (
            best.score >= MIN_CONFIDENCE
            and margin >= MIN_MARGIN
            and (
                not same_direction_competition
                or distance_margin >= MIN_DISTANCE_MARGIN_METERS
            )
            and not best_is_spatially_dominated
        )
        if not confident:
            return self._retain_or_unknown(state, now, "low-confidence")

        direction_id = best.candidate.direction_id
        destination = best_family.family.destination
        family_id = best_family.family.family_id
        public_variant_id = (
            best.candidate.variant_id
            if len(best_family.family.variants) == 1
            else None
        )
        if state.confirmed_family_id is None:
            state.confirmed_direction_id = direction_id
            state.confirmed_variant_id = best.candidate.variant_id
            state.confirmed_family_id = family_id
            state.confirmed_destination = destination
            state.confirmed_confidence = best.score
            state.last_confident_at = now
            return KyivDirectionDecision(direction_id, destination, best.score, margin, "confident", public_variant_id, "geometry")

        if state.confirmed_family_id == family_id:
            state.confirmed_destination = destination
            state.confirmed_confidence = best.score
            state.last_confident_at = now
            self._reset_pending(state)
            return KyivDirectionDecision(direction_id, destination, best.score, margin, "stable", public_variant_id, "geometry")

        if best.score >= STRONG_SWITCH_CONFIDENCE and margin >= STRONG_SWITCH_MARGIN:
            if state.pending_family_id == family_id:
                state.pending_count += 1
            else:
                state.pending_direction_id = direction_id
                state.pending_variant_id = best.candidate.variant_id
                state.pending_family_id = family_id
                state.pending_destination = destination
                state.pending_count = 1
            if state.pending_count >= REQUIRED_SWITCH_SAMPLES:
                state.confirmed_direction_id = direction_id
                state.confirmed_variant_id = best.candidate.variant_id
                state.confirmed_family_id = family_id
                state.confirmed_destination = destination
                state.confirmed_confidence = best.score
                state.last_confident_at = now
                self._reset_pending(state)
                return KyivDirectionDecision(direction_id, destination, best.score, margin, "switched", public_variant_id, "geometry")
            return KyivDirectionDecision(
                None,
                None,
                best.score,
                margin,
                "pending-switch",
                None,
                "geometry",
            )
        self._reset_pending(state)
        return self._retain_or_unknown(state, now, "opposite-evidence")

    @staticmethod
    def _unique_destination(
        candidates: Iterable[KyivRadarCandidate],
        direction_id: str,
    ) -> str | None:
        destinations = {
            candidate.destination
            for candidate in candidates
            if candidate.direction_id == direction_id and candidate.destination
        }
        if len(destinations) == 1:
            return next(iter(destinations))
        return None


def _project(shape: KyivRadarShape, latitude: float, longitude: float) -> _Projection:
    best: _Projection | None = None
    reference_latitude = math.radians(latitude)
    cos_latitude = max(0.2, math.cos(reference_latitude))
    scale = EARTH_RADIUS_METERS

    def point(value_latitude: float, value_longitude: float) -> tuple[float, float]:
        return (
            math.radians(value_longitude - longitude) * cos_latitude * scale,
            math.radians(value_latitude - latitude) * scale,
        )

    latitude_cell = math.floor(latitude / SHAPE_GRID_SIZE_DEGREES)
    longitude_cell = math.floor(longitude / SHAPE_GRID_SIZE_DEGREES)
    segment_indexes = {
        index
        for latitude_offset in range(-SHAPE_GRID_RADIUS_CELLS, SHAPE_GRID_RADIUS_CELLS + 1)
        for longitude_offset in range(-SHAPE_GRID_RADIUS_CELLS, SHAPE_GRID_RADIUS_CELLS + 1)
        for index in shape.segment_grid.get(
            (latitude_cell + latitude_offset, longitude_cell + longitude_offset),
            (),
        )
    }
    segments = (
        (shape.segments[index] for index in sorted(segment_indexes))
        if segment_indexes
        else iter(shape.segments)
    )
    for segment in segments:
        first = segment[:3]
        second = segment[3:]
        first_xy = point(first[0], first[1])
        second_xy = point(second[0], second[1])
        dx = second_xy[0] - first_xy[0]
        dy = second_xy[1] - first_xy[1]
        denominator = dx * dx + dy * dy
        if denominator <= 0:
            fraction = 0.0
        else:
            fraction = min(1.0, max(0.0, -(first_xy[0] * dx + first_xy[1] * dy) / denominator))
        projected_x = first_xy[0] + fraction * dx
        projected_y = first_xy[1] + fraction * dy
        distance = math.hypot(projected_x, projected_y)
        progress = first[2] + fraction * (second[2] - first[2])
        candidate = _Projection(distance, progress, _segment_bearing(first, second))
        if best is None or candidate.distance_meters < best.distance_meters:
            best = candidate
    return best or _Projection(float("inf"), 0.0, None)


def _segment_bearing(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> float:
    latitude = math.radians((first[0] + second[0]) / 2.0)
    x = math.radians(second[1] - first[1]) * math.cos(latitude)
    y = math.radians(second[0] - first[0])
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _bearing_difference(first: float, second: float) -> float:
    difference = abs((first - second) % 360.0)
    return min(difference, 360.0 - difference)
