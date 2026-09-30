#!/usr/bin/env bash
set -euo pipefail

SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
FLOCK_BIN="${FLOCK_BIN:-flock}"
LSOF_BIN="${LSOF_BIN:-lsof}"
DATA_ROOT="${DATA_ROOT:-/srv/haltewecker/data}"
DATA="$(cd "$DATA_ROOT" && pwd -P)"
RELEASES="$DATA/releases"
DEPARTURE_RELEASES="$DATA/departures/releases"
ROLLBACK_ROOT="$DATA/temp/current-rollback"
DRY_RUN="${HALTEWECKER_CLEANUP_DRY_RUN:-0}"
ABANDONED_RELEASE_MAX_AGE_HOURS="${HALTEWECKER_ABANDONED_RELEASE_MAX_AGE_HOURS:-12}"
VALIDATION_MAX_AGE_HOURS="${HALTEWECKER_VALIDATION_MAX_AGE_HOURS:-12}"
STAGING_MAX_AGE_HOURS="${HALTEWECKER_STAGING_MAX_AGE_HOURS:-6}"
LEGACY_EXTRA_BACKUP_MAX_AGE_HOURS="${HALTEWECKER_LEGACY_EXTRA_BACKUP_MAX_AGE_HOURS:-12}"
PIPELINE_REPO="${HALTEWECKER_PIPELINE_REPO:-/srv/haltewecker/pipeline/HalterWeckerAPIService}"
GTFS_CACHE_ROOT="${GTFS_CACHE_ROOT:-$DATA/cache/gtfs}"
GTFS_ORPHAN_TEMP_MAX_AGE_HOURS="${HALTEWECKER_GTFS_ORPHAN_TEMP_MAX_AGE_HOURS:-24}"
RECLAIMABLE_BYTES=0

ttl_seconds_from_hours() {
    local value="$1"
    awk -v hours="$value" 'BEGIN {
        if (hours !~ /^[0-9]+([.][0-9]+)?$/ || hours < 0) exit 1
        printf "%.0f\n", hours * 3600
    }'
}

format_age_hours() {
    local age_seconds="$1"
    awk -v seconds="$age_seconds" 'BEGIN { printf "%.1f", seconds / 3600 }'
}

format_size() {
    local bytes="$1"
    numfmt --to=iec --suffix=B "$bytes" 2>/dev/null || awk -v bytes="$bytes" 'BEGIN {
        split("B KiB MiB GiB TiB", units)
        index = 1
        while (bytes >= 1024 && index < 5) {
            bytes /= 1024
            index++
        }
        printf "%.1f%s", bytes, units[index]
    }'
}

path_size_bytes() {
    local path="$1"
    du -sb -- "$path" 2>/dev/null | awk '{ print $1 }'
}

path_is_strictly_inside() {
    local path="$1"
    local root="$2"
    local canonical
    canonical="$(readlink -f -- "$path" 2>/dev/null || true)"
    [[ -n "$canonical" && "$canonical" != "$root" && "$canonical" == "$root/"* ]]
}

artifact_age_seconds() {
    local path="$1"
    local mtime now
    mtime="$(stat -c '%Y' -- "$path" 2>/dev/null || true)"
    [[ "$mtime" =~ ^[0-9]+$ ]] || return 1
    now="$(date +%s)"
    (( now >= mtime )) || return 1
    printf '%s\n' "$((now - mtime))"
}

for ttl_name in ABANDONED_RELEASE_MAX_AGE_HOURS VALIDATION_MAX_AGE_HOURS STAGING_MAX_AGE_HOURS LEGACY_EXTRA_BACKUP_MAX_AGE_HOURS; do
    ttl_value="${!ttl_name}"
    if ! ttl_seconds_from_hours "$ttl_value" >/dev/null; then
        echo "Invalid TTL: ${ttl_name}=${ttl_value}" >&2
        exit 1
    fi
done

emit_would_delete() {
    local path="$1"
    local reason="$2"
    local age_seconds="$3"
    local bytes
    bytes="$(path_size_bytes "$path")"
    [[ "$bytes" =~ ^[0-9]+$ ]] || bytes=0
    RECLAIMABLE_BYTES=$((RECLAIMABLE_BYTES + bytes))
    echo "WOULD_DELETE $path reason=$reason age=$(format_age_hours "$age_seconds")h size=$(format_size "$bytes")"
}


if "$SYSTEMCTL_BIN" is-active --quiet haltewecker-stop-data.service ||
   "$SYSTEMCTL_BIN" is-active --quiet haltewecker-static-departures.service; then
    echo "CLEANUP_SKIPPED reason=pipeline-service-active"
    exit 0
fi

# The full, scoped, and VBB pipelines share these locks. Holding all of them
# prevents a release from being staged or activated while retention is evaluated.
LOCKS="${HALTEWECKER_CLEANUP_LOCKS:-/run/lock/haltewecker-stop-data.lock:/run/lock/haltewecker-static-departures.lock:/run/lock/haltewecker-vbb-refresh.lock}"
declare -a LOCK_FDS=()
if [[ -n "$LOCKS" ]]; then
    IFS=: read -r -a LOCK_PATHS <<< "$LOCKS"
    lock_index=0
    for lock_path in "${LOCK_PATHS[@]}"; do
        [[ -n "$lock_path" ]] || continue
        mkdir -p "$(dirname "$lock_path")"
        case "$lock_index" in
            0) exec 8<"$lock_path"; lock_fd=8 ;;
            1) exec 9<"$lock_path"; lock_fd=9 ;;
            2) exec 10<"$lock_path"; lock_fd=10 ;;
            *)
                echo "CLEANUP_SKIPPED reason=too-many-pipeline-locks"
                exit 0
                ;;
        esac
        if ! "$FLOCK_BIN" -n "$lock_fd"; then
            echo "CLEANUP_SKIPPED reason=pipeline-lock-held lock=$lock_path"
            exit 0
        fi
        LOCK_FDS+=("$lock_fd")
        lock_index=$((lock_index + 1))
    done
fi

if [[ ! -d "$RELEASES" ]]; then
    echo "CLEANUP_SKIPPED reason=releases-directory-missing path=$RELEASES"
    exit 0
fi

configured_retention="${HALTEWECKER_RELEASE_RETENTION_COUNT:-${RELEASE_RETENTION_COUNT:-1}}"
if [[ ! "$configured_retention" =~ ^[0-9]+$ ]]; then
    echo "Invalid release retention count: $configured_retention" >&2
    exit 1
fi
retention_count="$configured_retention"
(( retention_count < 1 )) && retention_count=1

declare -a KEEP_NAMES=()
declare -a KEEP_REASONS=()
declare -a PROTECTED_DEPENDENCY_PATHS=()
declare -a PROTECTED_DEPENDENCY_REASONS=()
declare -a PROTECTED_DEPENDENCY_SEEN=()
declare -a PROTECTED_DEPENDENCY_QUEUE=()
declare -a PROTECTED_DEPENDENCY_QUEUE_REASONS=()
DEPENDENCY_GRAPH_UNRESOLVED=0

append_reason() {
    local release_name="$1"
    local reason="$2"
    local index
    for index in "${!KEEP_NAMES[@]}"; do
        if [[ "${KEEP_NAMES[$index]}" == "$release_name" ]]; then
            KEEP_REASONS[$index]+=";$reason"
            return 0
        fi
    done
    KEEP_NAMES+=("$release_name")
    KEEP_REASONS+=("$reason")
}

reason_for() {
    local release_name="$1"
    local index
    for index in "${!KEEP_NAMES[@]}"; do
        if [[ "${KEEP_NAMES[$index]}" == "$release_name" ]]; then
            printf '%s\n' "${KEEP_REASONS[$index]}"
            return 0
        fi
    done
    return 1
}

release_name_for_path() {
    local path="$1"
    local resolved="$(readlink -f -- "$path" 2>/dev/null || true)"
    local relative release_name
    case "$resolved" in
        "$RELEASES"/*)
            relative="${resolved#"$RELEASES/"}"
            release_name="${relative%%/*}"
            [[ -n "$release_name" && -d "$RELEASES/$release_name" ]] || return 0
            printf '%s\n' "$release_name"
            ;;
    esac
}

protect_reference() {
    local path="$1"
    local reason="$2"
    local release_name
    release_name="$(release_name_for_path "$path")"
    [[ -n "$release_name" ]] || return 0
    append_reason "$release_name" "$reason"
}

dependency_mark_path() {
    local path="$1"
    local reason="$2"
    local index
    [[ -n "$path" ]] || return 0
    for index in "${!PROTECTED_DEPENDENCY_PATHS[@]}"; do
        [[ "${PROTECTED_DEPENDENCY_PATHS[$index]}" == "$path" ]] && return 0
    done
    PROTECTED_DEPENDENCY_PATHS+=("$path")
    PROTECTED_DEPENDENCY_REASONS+=("$reason")
}

dependency_reason_for_path() {
    local candidate="$1"
    local index protected reason
    candidate="$(readlink -m -- "$candidate" 2>/dev/null || true)"
    [[ -n "$candidate" ]] || return 1
    for index in "${!PROTECTED_DEPENDENCY_PATHS[@]}"; do
        protected="${PROTECTED_DEPENDENCY_PATHS[$index]}"
        if [[ "$protected" == "$candidate" || "$protected" == "$candidate/"* ]]; then
            reason="${PROTECTED_DEPENDENCY_REASONS[$index]}"
            printf '%s\n' "$reason"
            return 0
        fi
    done
    return 1
}

dependency_enqueue() {
    local path="$1"
    local reason="$2"
    local target raw_target lexical_target

    if [[ -L "$path" ]]; then
        raw_target="$(readlink -- "$path" 2>/dev/null || true)"
        target="$(readlink -f -- "$path" 2>/dev/null || true)"
        if [[ -z "$target" ]]; then
            lexical_target="$(readlink -m -- "$(dirname "$path")/$raw_target" 2>/dev/null || true)"
            DEPENDENCY_GRAPH_UNRESOLVED=1
            dependency_mark_path "$lexical_target" "$reason;unresolved-dependency"
            echo "KEEP   $lexical_target reason=$reason;unresolved-dependency"
            return 0
        fi
    else
        target="$(readlink -f -- "$path" 2>/dev/null || true)"
        if [[ -z "$target" ]]; then
            DEPENDENCY_GRAPH_UNRESOLVED=1
            dependency_mark_path "$(readlink -m -- "$path" 2>/dev/null || true)" "$reason;unresolved-dependency"
            echo "KEEP   $path reason=$reason;unresolved-dependency"
            return 0
        fi
    fi

    dependency_mark_path "$target" "$reason"
    case "$target" in
        "$DATA"|"$DATA/"*) ;;
        *) return 0 ;;
    esac
    for seen in "${PROTECTED_DEPENDENCY_SEEN[@]-}"; do
        [[ "$seen" == "$target" ]] && return 0
    done
    PROTECTED_DEPENDENCY_SEEN+=("$target")
    PROTECTED_DEPENDENCY_QUEUE+=("$target")
    PROTECTED_DEPENDENCY_QUEUE_REASONS+=("$reason")
}

manifest_dependency_paths() {
    local manifest="$1"
    python3 - "$manifest" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    raise SystemExit(2)

def walk(value):
    if isinstance(value, str) and value.startswith("/"):
        print(value)
    elif isinstance(value, dict):
        for item in value.values():
            walk(item)
    elif isinstance(value, list):
        for item in value:
            walk(item)

walk(payload)
PY
}

dependency_build_graph() {
    local queue_index=0
    local entry reason link manifest dependency_paths dependency_path

    if [[ -e "$DATA/current-release" || -L "$DATA/current-release" ]]; then
        dependency_enqueue "$DATA/current-release" "referenced-by-current-release"
    fi
    if [[ -e "$DATA/rollback" || -L "$DATA/rollback" ]]; then
        dependency_enqueue "$DATA/rollback" "referenced-by-rollback"
    fi
    if [[ -e "$DATA/pilot-current" || -L "$DATA/pilot-current" ]]; then
        dependency_enqueue "$DATA/pilot-current" "referenced-by-pilot-current"
    fi

    while (( queue_index < ${#PROTECTED_DEPENDENCY_QUEUE[@]} )); do
        entry="${PROTECTED_DEPENDENCY_QUEUE[$queue_index]}"
        reason="${PROTECTED_DEPENDENCY_QUEUE_REASONS[$queue_index]}"
        queue_index=$((queue_index + 1))
        [[ -d "$entry" ]] || continue

        while IFS= read -r -d '' link; do
            dependency_enqueue "$link" "$reason"
        done < <(find "$entry" -xdev -type l -print0 2>/dev/null)

        while IFS= read -r -d '' manifest; do
            if ! dependency_paths="$(manifest_dependency_paths "$manifest")"; then
                DEPENDENCY_GRAPH_UNRESOLVED=1
                echo "KEEP   $manifest reason=unresolved-dependency"
                continue
            fi
            while IFS= read -r dependency_path; do
                [[ -n "$dependency_path" ]] || continue
                dependency_enqueue "$dependency_path" "$reason;manifest-dependency:$manifest"
            done <<< "$dependency_paths"
        done < <(
            find "$entry" -xdev -type f \
                \( -iname 'release*.json' -o -iname '*manifest*.json' \
                -o -iname '*artifact*.json' -o -iname '*state*.json' \
                -o -iname '*provenance*.json' \) -print0 2>/dev/null
        )
    done
}

emit_unresolved_dependency_keeps() {
    local index protected relative release_name release_path reason
    for index in "${!PROTECTED_DEPENDENCY_PATHS[@]}"; do
        protected="${PROTECTED_DEPENDENCY_PATHS[$index]}"
        case "$protected" in
            "$RELEASES"/*)
                relative="${protected#"$RELEASES/"}"
                release_name="${relative%%/*}"
                release_path="$RELEASES/$release_name"
                reason="${PROTECTED_DEPENDENCY_REASONS[$index]}"
                echo "KEEP   $release_path reason=$reason"
                ;;
        esac
    done
}

dependency_build_graph
if (( DEPENDENCY_GRAPH_UNRESOLVED == 1 )); then
    emit_unresolved_dependency_keeps
    echo "CLEANUP_SKIPPED reason=dependency-graph-unresolved"
    exit 0
fi

protect_reference "$DATA/current-release" "current-release"

while IFS= read -r -d '' symlink_path; do
    case "$symlink_path" in
	"$DATA/current-release"|"$DATA/previous/stop-data") continue ;;
    esac
    protect_reference "$symlink_path" "active-symlink:$symlink_path"
done < <(find "$DATA" -path "$RELEASES" -prune -o -type l -print0)

while IFS= read -r -d '' symlink_path; do
    if [[ "$symlink_path" == "$RELEASES/pilot-current" ]]; then
        protect_reference "$symlink_path" "protected-by-pilot-current"
    else
        protect_reference "$symlink_path" "protected-pointer:$symlink_path"
    fi
done < <(find "$RELEASES" -mindepth 1 -maxdepth 1 -type l -print0)

rollback_release_is_referenced() {
    local release_name="$1"
    local index
    for index in "${!KEEP_NAMES[@]}"; do
        if [[ "${KEEP_NAMES[$index]}" == "$release_name" ]]; then
            return 0
        fi
    done
    return 1
}

rollback_marker_metadata_state() {
    local rollback_path="$1"
    local metadata contents
    local saw_metadata=0
    local saw_completed=0

    while IFS= read -r -d '' metadata; do
        saw_metadata=1
        if ! contents="$(head -c 4096 -- "$metadata" 2>/dev/null | tr '[:upper:]' '[:lower:]')"; then
            printf '%s\n' "unknown"
            return 0
        fi
        if printf '%s' "$contents" | grep -Eiq '(^|[^[:alpha:]])(active|prepared|staged|pending|running|in-progress|incomplete|rollback-required)([^[:alpha:]]|$)'; then
            printf '%s\n' "unfinished"
            return 0
        fi
        if printf '%s' "$contents" | grep -Eiq '(^|[^[:alpha:]])(completed|finalized|success|succeeded|committed)([^[:alpha:]]|$)'; then
            saw_completed=1
            continue
        fi
        printf '%s\n' "unknown"
        return 0
    done < <(
        find "$rollback_path" -mindepth 1 -maxdepth 1 -type f \
            \( -iname '*rollback*' -o -iname '*transaction*' -o -iname '*state*' \) -print0
    )

    if (( saw_metadata == 0 )); then
        printf '%s\n' "none"
    elif (( saw_completed == 1 )); then
        printf '%s\n' "completed"
    else
        printf '%s\n' "unknown"
    fi
}

remove_stale_rollback_marker() {
    local rollback_path="$1"
    local age_seconds

    if [[ "$DRY_RUN" == "1" ]]; then
        if age_seconds="$(artifact_age_seconds "$rollback_path")"; then
            emit_would_delete "$rollback_path" "stale-rollback-marker" "$age_seconds"
        else
            echo "WOULD_DELETE $rollback_path reason=stale-rollback-marker"
        fi
        return 0
    fi

    echo "DELETE $rollback_path reason=stale-rollback-marker"
    rm -rf -- "$rollback_path"
}

for rollback_path in "$ROLLBACK_ROOT"/*; do
    [[ -d "$rollback_path" && ! -L "$rollback_path" ]] || continue
    [[ "$(dirname "$rollback_path")" == "$ROLLBACK_ROOT" ]] || {
        echo "KEEP   $rollback_path reason=rollback-path-check"
        continue
    }
    if ! path_is_strictly_inside "$rollback_path" "$ROLLBACK_ROOT"; then
        echo "KEEP   $rollback_path reason=rollback-path-check"
        continue
    fi

    rollback_name="${rollback_path##*/}"
    metadata_state="$(rollback_marker_metadata_state "$rollback_path")"
    if [[ -d "$RELEASES/$rollback_name" ]] && rollback_release_is_referenced "$rollback_name"; then
        echo "KEEP   $rollback_path reason=active-release-reference"
        continue
    fi
    if [[ "$metadata_state" == "unfinished" || "$metadata_state" == "unknown" ]]; then
        if [[ -d "$RELEASES/$rollback_name" ]]; then
            append_reason "$rollback_name" "rollback-release:$rollback_path"
        fi
        echo "KEEP   $rollback_path reason=unfinished-or-unknown-rollback-state"
        continue
    fi

    remove_stale_rollback_marker "$rollback_path"
done

is_release_name() {
    [[ "$1" =~ ^((scoped-)?[0-9]{8}T[0-9]{6}Z-[[:alnum:]]+|vbb-refresh-[0-9]{8}T[0-9]{6}Z-[[:alnum:]]+-[[:alnum:]]+)$ ]]
}

is_published_release() {
    local release_path="$1"
    [[ -d "$release_path/stop-data" ]] || return 1
    [[ -f "$release_path/departures.sqlite" ]] || return 1
    [[ -f "$release_path/release-metadata.json" ]] || return 1
    return 0
}

is_known_abandoned_build_name() {
    case "$1" in
        build-*|candidate-*|staging-*|rehearsal-*|verification-*|incremental|incremental-*)
            return 0
            ;;
    esac
    return 1
}

build_state_for() {
    local build_path="$1"
    local metadata contents
    local saw_metadata=0
    local saw_incomplete=0
    local saw_success=0

    while IFS= read -r -d '' metadata; do
        saw_metadata=1
        contents="$(head -c 65536 -- "$metadata" 2>/dev/null | tr '[:upper:]' '[:lower:]')"
        if printf '%s' "$contents" | grep -Eiq '(^|[^[:alpha:]])(failed|failure|incomplete|partial|aborted|cancelled|canceled|interrupted)([^[:alpha:]]|$)'; then
            saw_incomplete=1
        fi
        if printf '%s' "$contents" | grep -Eiq '(^|[^[:alpha:]])(published|success|succeeded|completed|finalized|committed|ready)([^[:alpha:]]|$)'; then
            saw_success=1
        fi
    done < <(
        find "$build_path" -type f \
            \( -iname '*manifest*' -o -iname '*metadata*' -o -iname '*state*' -o -iname '*status*' -o -iname '*result*' \) -print0
    )

    if is_published_release "$build_path" || (( saw_success == 1 )); then
        printf '%s\n' "published"
    elif (( saw_incomplete == 1 )); then
        printf '%s\n' "incomplete"
    elif (( saw_metadata == 0 )); then
        printf '%s\n' "absent"
    else
        printf '%s\n' "unknown"
    fi
}

open_files_for() {
    local build_path="$1"
    command -v "$LSOF_BIN" >/dev/null 2>&1 || return 2
    "$LSOF_BIN" -nP -w +D -- "$build_path" 2>/dev/null || true
}

classify_nonstandard_release() {
    local release_path="$1"
    local release_name="${release_path##*/}"
    local age_seconds build_state open_files reason bytes

    if reason="$(dependency_reason_for_path "$release_path" 2>/dev/null)"; then
        echo "KEEP   $release_path reason=$reason"
        return 0
    fi

    reason="$(reason_for "$release_name" 2>/dev/null || true)"
    if [[ -n "$reason" ]]; then
        echo "KEEP   $release_path reason=$reason"
        return 0
    fi
    if ! is_known_abandoned_build_name "$release_name"; then
        echo "KEEP   $release_path reason=unknown-state"
        return 0
    fi
    if ! path_is_strictly_inside "$release_path" "$RELEASES"; then
        echo "KEEP   $release_path reason=unknown-state"
        return 0
    fi
    if ! age_seconds="$(artifact_age_seconds "$release_path")"; then
        echo "KEEP   $release_path reason=unknown-state"
        return 0
    fi
    if (( age_seconds < $(ttl_seconds_from_hours "$ABANDONED_RELEASE_MAX_AGE_HOURS") )); then
        echo "KEEP   $release_path reason=recent-abandoned-build age=$(format_age_hours "$age_seconds")h"
        return 0
    fi

    build_state="$(build_state_for "$release_path")"
    if [[ "$build_state" == "published" ]]; then
        echo "KEEP   $release_path reason=published-state"
        return 0
    fi
    if [[ "$build_state" == "unknown" ]]; then
        echo "KEEP   $release_path reason=unknown-state"
        return 0
    fi

    if ! command -v "$LSOF_BIN" >/dev/null 2>&1; then
        echo "KEEP   $release_path reason=unknown-state"
        return 0
    fi
    open_files="$(open_files_for "$release_path")"
    if [[ -n "$open_files" ]]; then
        echo "KEEP   $release_path reason=open-by-process"
        return 0
    fi

    if [[ "$DRY_RUN" == "1" ]]; then
        bytes="$(path_size_bytes "$release_path")"
        [[ "$bytes" =~ ^[0-9]+$ ]] || bytes=0
        RECLAIMABLE_BYTES=$((RECLAIMABLE_BYTES + bytes))
        echo "DELETE-CANDIDATE $release_path reason=abandoned-build age=$(format_age_hours "$age_seconds")h size=$(format_size "$bytes")"
    else
        echo "DELETE $release_path reason=abandoned-build age=$(format_age_hours "$age_seconds")h"
        rm -rf -- "$release_path"
    fi
}

ordered_candidates="$(mktemp "${TMPDIR:-/tmp}/haltewecker-release-retention.XXXXXX")"
trap 'rm -f -- "$ordered_candidates"' EXIT

shopt -s nullglob
release_entries=("$RELEASES"/*)
for release_path in "${release_entries[@]-}"; do
    [[ -L "$release_path" || -d "$release_path" ]] || continue
    [[ -L "$release_path" ]] && continue
    release_name="${release_path##*/}"
    is_release_name "$release_name" || continue
    is_published_release "$release_path" || continue
    sortable_name="$release_name"
    case "$sortable_name" in
        scoped-*) sortable_name="${sortable_name#scoped-}" ;;
        vbb-refresh-*) sortable_name="${sortable_name#vbb-refresh-}" ;;
    esac
    printf '%s\t%s\n' "${sortable_name%%-*}" "$release_name" >> "$ordered_candidates"
done

ordered_release_names=()
while IFS=$'\t' read -r sortable_timestamp release_name; do
    [[ -n "$release_name" ]] || continue
    ordered_release_names+=("$release_name")
done < <(sort -r -k1,1 -k2,2 "$ordered_candidates")
retention_slot=0
for release_name in "${ordered_release_names[@]-}"; do
    (( retention_slot >= retention_count )) && break
    retention_slot=$((retention_slot + 1))
    append_reason "$release_name" "retention-slot=$retention_slot/$retention_count"
done

echo "Release retention: configured=$configured_retention effective=$retention_count"

for release_path in "${release_entries[@]-}"; do
    [[ -L "$release_path" || -d "$release_path" ]] || continue
    release_name="${release_path##*/}"

    if [[ -L "$release_path" ]]; then
        echo "KEEP   $release_path reason=protected-pointer/symlink"
        continue
    fi
    if [[ ! -d "$release_path" ]]; then
        continue
    fi
    if reason="$(dependency_reason_for_path "$release_path" 2>/dev/null)"; then
        echo "KEEP   $release_path reason=$reason"
        continue
    fi
    reason="$(reason_for "$release_name" 2>/dev/null || true)"
    if [[ -n "$reason" ]]; then
        echo "KEEP   $release_path reason=$reason"
        continue
    fi
    if ! is_release_name "$release_name"; then
        classify_nonstandard_release "$release_path"
        continue
    fi
    if reason="$(reason_for "$release_name")"; then
        if [[ "$reason" == *"current-release"* ]]; then
            echo "CURRENT RELEASE $release_path reason=$reason"
        elif [[ "$reason" == *"previous-release"* ]]; then
            echo "PREVIOUS RELEASE $release_path reason=$reason"
        else
            echo "KEEP   $release_path reason=$reason"
        fi
        continue
    fi
    if ! is_published_release "$release_path"; then
        if ! path_is_strictly_inside "$release_path" "$RELEASES"; then
            echo "KEEP   $release_path reason=outside-root-or-symlink-escape"
            continue
        fi
        if ! age_seconds="$(artifact_age_seconds "$release_path")"; then
            echo "KEEP   $release_path reason=unreliable-mtime"
            continue
        fi
        if (( age_seconds < $(ttl_seconds_from_hours "$ABANDONED_RELEASE_MAX_AGE_HOURS") )); then
            echo "KEEP   $release_path reason=recent-unpublished age=$(format_age_hours "$age_seconds")h"
            continue
        fi
        if [[ "$DRY_RUN" == "1" ]]; then
            emit_would_delete "$release_path" "abandoned-unpublished" "$age_seconds"
        else
            echo "DELETE $release_path reason=abandoned-unpublished age=$(format_age_hours "$age_seconds")h"
            rm -rf -- "$release_path"
        fi
        continue
    fi
    if [[ "$(dirname "$release_path")" != "$RELEASES" ]]; then
        echo "KEEP   $release_path reason=safety-path-check"
        continue
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "WOULD_DELETE $release_path reason=outside-retention-and-unreferenced"
    else
        echo "DELETE $release_path reason=outside-retention-and-unreferenced"
        rm -rf -- "$release_path"
    fi
done

cleanup_expired_artifacts() {
    local root="$1"
    local ttl_hours="$2"
    local reason="$3"
    local entry age_seconds ttl_seconds state open_files active_validation

    if [[ ! -d "$root" ]]; then
        echo "Artifact retention: directory-missing path=$root"
        return 0
    fi

    ttl_seconds="$(ttl_seconds_from_hours "$ttl_hours")"
    echo "Artifact retention: directory=$root max-age-hours=$ttl_hours"
    while IFS= read -r -d '' entry; do
        if dependency_reason="$(dependency_reason_for_path "$entry" 2>/dev/null)"; then
            echo "KEEP   $entry reason=$dependency_reason"
            continue
        fi
        if [[ -L "$entry" ]]; then
            echo "KEEP   $entry reason=symlink-entry"
            continue
        fi
        if [[ ! -e "$entry" ]]; then
            echo "KEEP   $entry reason=missing-entry"
            continue
        fi
        if [[ -d "$entry" ]]; then
            if ! command -v "$LSOF_BIN" >/dev/null 2>&1; then
                echo "KEEP   $entry reason=open-process-check-unavailable"
                continue
            fi
            open_files="$(open_files_for "$entry" 2>/dev/null || true)"
        else
            if ! command -v "$LSOF_BIN" >/dev/null 2>&1; then
                echo "KEEP   $entry reason=open-process-check-unavailable"
                continue
            fi
            open_files="$("$LSOF_BIN" -nP -w -- "$entry" 2>/dev/null || true)"
        fi
        if [[ -n "$open_files" ]]; then
            echo "KEEP   $entry reason=open-by-process"
            continue
        fi
        if ! path_is_strictly_inside "$entry" "$root"; then
            echo "KEEP   $entry reason=outside-root-or-symlink-escape"
            continue
        fi
        if ! age_seconds="$(artifact_age_seconds "$entry")"; then
            echo "KEEP   $entry reason=unreliable-mtime"
            continue
        fi
        if [[ "$reason" == "validation" ]]; then
            active_validation="$(active_release_validation_pass_matches "$entry" || true)"
            if [[ "$active_validation" == "1" ]]; then
                ttl_seconds=0
                echo "Validation retention: active release PASS receipt is cleanup-eligible path=$entry"
            fi
        elif [[ "$reason" == "staging" ]]; then
            state="$(build_state_for "$entry")"
            case "$state" in
                published)
                    ttl_seconds=0
                    echo "Staging retention: successful orphan is cleanup-eligible path=$entry"
                    ;;
                incomplete|absent)
                    ttl_seconds="$(ttl_seconds_from_hours "$STAGING_MAX_AGE_HOURS")"
                    ;;
                *)
                    echo "KEEP   $entry reason=unknown-staging-state"
                    continue
                    ;;
            esac
        fi
        if (( age_seconds < ttl_seconds )); then
            echo "KEEP   $entry reason=recent-$reason age=$(format_age_hours "$age_seconds")h"
            continue
        fi
        if [[ "$DRY_RUN" == "1" ]]; then
            emit_would_delete "$entry" "expired-$reason" "$age_seconds"
        else
            echo "DELETE $entry reason=expired-$reason age=$(format_age_hours "$age_seconds")h"
            rm -rf -- "$entry"
        fi
    done < <(find "$root" -mindepth 1 -maxdepth 1 -print0)
}

active_release_id() {
    local current
    current="$(readlink -f -- "$DATA/current-release" 2>/dev/null || true)"
    [[ -n "$current" ]] || return 1
    basename -- "$current"
}

active_release_validation_pass_matches() {
    local entry="$1"
    local release_id
    release_id="$(active_release_id || true)"
    [[ -n "$release_id" ]] || return 1
    python3 - "$entry" "$release_id" <<'PY'
import json
import sys
from pathlib import Path

entry = Path(sys.argv[1])
release_id = sys.argv[2]
candidates = [entry] if entry.is_file() else list(entry.rglob("validation-receipt.json")) if entry.is_dir() else []
for candidate in candidates:
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        continue
    if payload.get("result") == "PASS" and payload.get("releaseID") == release_id:
        print("1")
        raise SystemExit(0)
print("0")
PY
}

cleanup_expired_artifacts "$DATA/validation" "$VALIDATION_MAX_AGE_HOURS" "validation"
cleanup_expired_artifacts "$DATA/staging" "$STAGING_MAX_AGE_HOURS" "staging"


staging_has_supported_resume() {
    local staging_path="$1"
    local release_name marker marker_contents
    release_name="$(basename "$(dirname "$staging_path")")"
    for marker in "$staging_path.resume" "$staging_path.resume.json"; do
        [[ -f "$marker" ]] || continue
        marker_contents="$(head -c 4096 -- "$marker" 2>/dev/null || true)"
        if [[ "$marker_contents" == *"$release_name"* ]] &&
           printf '%s' "$marker_contents" | rg -q -i '"resumeSupported"[[:space:]]*:[[:space:]]*true|resume[-_ ]supported[[:space:]]*=[[:space:]]*true'; then
            printf '%s\n' "$marker"
            return 0
        fi
    done
    return 1
}

staging_reference_for() {
    local staging_path="$1"
    local references
    references="$(
        rg -l --hidden \
            --glob '*.yml' --glob '*.yaml' --glob '*.env' \
            --glob '*.sh' --glob '*.service' \
            -F -- "$staging_path" \
            /etc/systemd/system /etc/haltewecker-stop-data.env /srv/haltewecker/pipeline \
            2>/dev/null || true
    )"
    [[ -n "$references" ]] || return 1
    printf '%s\n' "$references"
}

cleanup_release_scoped_staging() {
    local staging_path release_name open_pids resume_marker references bytes
    [[ -d "$RELEASES" ]] || return 0

    while IFS= read -r -d '' staging_path; do
        [[ -f "$staging_path" && ! -L "$staging_path" ]] || {
            echo "ACTIVE STAGING $staging_path status=KEEP reason=non-regular-entry"
            continue
        }
        if ! path_is_strictly_inside "$staging_path" "$RELEASES"; then
            echo "ACTIVE STAGING $staging_path status=KEEP reason=outside-root-or-symlink-escape"
            continue
        fi

        release_name="$(basename "$(dirname "$staging_path")")"
        if ! is_release_name "$release_name"; then
            echo "ACTIVE STAGING $staging_path status=KEEP reason=unrecognized-release"
            continue
        fi

        open_pids="$(lsof -nP -w -t -- "$staging_path" 2>/dev/null || true)"
        resume_marker="$(staging_has_supported_resume "$staging_path" || true)"
        references="$(staging_reference_for "$staging_path" || true)"
        if [[ -n "$open_pids" ]]; then
            echo "ACTIVE STAGING $staging_path status=KEEP reason=open-by-process pid=$open_pids"
            continue
        fi
        if [[ -n "$resume_marker" ]]; then
            echo "ACTIVE STAGING $staging_path status=KEEP reason=supported-resume-marker:$resume_marker"
            continue
        fi
        if [[ -n "$references" ]]; then
            echo "ACTIVE STAGING $staging_path status=KEEP reason=referenced:$references"
            continue
        fi

        if ! age_seconds="$(artifact_age_seconds "$staging_path")"; then
            echo "ACTIVE STAGING $staging_path status=KEEP reason=unreliable-mtime"
            continue
        fi
        if (( age_seconds < $(ttl_seconds_from_hours "$STAGING_MAX_AGE_HOURS") )); then
            echo "ACTIVE STAGING $staging_path status=KEEP reason=inside-failed-staging-window age=$(format_age_hours "$age_seconds")h"
            continue
        fi

        echo "ABANDONED STAGING $staging_path status=RECLAIMABLE reason=failed-or-interrupted-without-resume"
        bytes="$(path_size_bytes "$staging_path")"
        if [[ "$DRY_RUN" == "1" ]]; then
            age_seconds="$(artifact_age_seconds "$staging_path" || printf '0')"
            emit_would_delete "$staging_path" "abandoned-static-departures-staging" "$age_seconds"
        else
            echo "DELETE $staging_path size=$bytes reason=abandoned-static-departures-staging"
            rm -f -- "$staging_path"
        fi
    done < <(find "$RELEASES" -mindepth 2 -maxdepth 2 -type f -name 'departures-next.sqlite' -print0)
}

cleanup_release_scoped_staging

declare -a KEEP_DEPARTURE_PATHS=()
declare -a KEEP_DEPARTURE_REASONS=()

append_departure_reason() {
    local departure_path="$1"
    local reason="$2"
    local index
    for index in "${!KEEP_DEPARTURE_PATHS[@]}"; do
        if [[ "${KEEP_DEPARTURE_PATHS[$index]}" == "$departure_path" ]]; then
            KEEP_DEPARTURE_REASONS[$index]+=";$reason"
            return 0
        fi
    done
    KEEP_DEPARTURE_PATHS+=("$departure_path")
    KEEP_DEPARTURE_REASONS+=("$reason")
}

departure_reason_for() {
    local departure_path="$1"
    local index
    for index in "${!KEEP_DEPARTURE_PATHS[@]}"; do
        if [[ "${KEEP_DEPARTURE_PATHS[$index]}" == "$departure_path" ]]; then
            printf '%s\n' "${KEEP_DEPARTURE_REASONS[$index]}"
            return 0
        fi
    done
    return 1
}

protect_departure_reference() {
    local path="$1"
    local resolved
    resolved="$(readlink -f -- "$path" 2>/dev/null || true)"
    case "$resolved" in
        "$DEPARTURE_RELEASES"/*.sqlite)
            append_departure_reason "$resolved" "active-reference:$path"
            ;;
    esac
}

departure_is_open() {
    local departure_path="$1"
    local open_pids
    open_pids="$(lsof -nP -w -t -- "$departure_path" 2>/dev/null || true)"
    [[ -n "$open_pids" ]]
}

departure_is_valid() {
    local departure_path="$1"
    [[ -f "$departure_path" &&
       ! -L "$departure_path" &&
       "$departure_path" == "$DEPARTURE_RELEASES"/*.sqlite &&
       -s "$departure_path" ]]
}

if [[ -d "$DEPARTURE_RELEASES" ]]; then
    protect_departure_reference "$DATA/departures-current.sqlite"

    while IFS= read -r -d '' reference_path; do
        protect_departure_reference "$reference_path"
    done < <(find "$DATA" -path "$DEPARTURE_RELEASES" -prune -o -type l -print0)

    while IFS= read -r -d '' configured_path; do
        [[ -e "$configured_path" || -L "$configured_path" ]] || continue
        protect_departure_reference "$configured_path"
    done < <(
        rg -l --glob '*.yml' --glob '*.yaml' --glob '*.env' --glob '*.sh' \
            'departures-current\.sqlite|current-release/departures\.sqlite|current-rollback|DEPARTURES_DATABASE' \
            "$DATA" /etc/systemd/system /srv/haltewecker/pipeline 2>/dev/null || true
    )

    departure_entries=("$DEPARTURE_RELEASES"/*.sqlite)
    echo "Legacy standalone departure retention: directory=$DEPARTURE_RELEASES max-age-hours=$LEGACY_EXTRA_BACKUP_MAX_AGE_HOURS"
    for departure_path in "${departure_entries[@]}"; do
        [[ -e "$departure_path" || -L "$departure_path" ]] || continue

        if reason="$(departure_reason_for "$departure_path")"; then
            echo "LEGACY EXTRA BACKUP $departure_path status=KEEP reason=$reason"
            continue
        fi
        if departure_is_open "$departure_path"; then
            append_departure_reason "$departure_path" "open-by-process"
            echo "LEGACY EXTRA BACKUP $departure_path status=KEEP reason=open-by-process"
            continue
        fi
        if ! departure_is_valid "$departure_path"; then
            echo "LEGACY EXTRA BACKUP $departure_path status=KEEP reason=invalid-or-nonregular"
            continue
        fi
        if ! departure_age_seconds="$(artifact_age_seconds "$departure_path")"; then
            echo "LEGACY EXTRA BACKUP $departure_path status=KEEP reason=unreliable-mtime"
            continue
        fi
        if (( departure_age_seconds < $(ttl_seconds_from_hours "$LEGACY_EXTRA_BACKUP_MAX_AGE_HOURS") )); then
            echo "LEGACY EXTRA BACKUP $departure_path status=KEEP reason=inside-recovery-window age=$(format_age_hours "$departure_age_seconds")h"
            continue
        fi

        echo "LEGACY EXTRA BACKUP $departure_path status=RECLAIMABLE reason=obsolete-unbound-departures-backup age=$(format_age_hours "$departure_age_seconds")h"
        bytes="$(path_size_bytes "$departure_path")"
        if [[ "$DRY_RUN" == "1" ]]; then
            emit_would_delete "$departure_path" "obsolete-unbound-departures-backup" "$departure_age_seconds"
        else
            echo "DELETE $departure_path size=$bytes reason=obsolete-unbound-departures-backup"
            rm -f -- "$departure_path"
        fi
    done
else
    echo "Departure retention: directory-missing path=$DEPARTURE_RELEASES"
fi

cleanup_gtfs_cache() {
    local helper="$PIPELINE_REPO/scripts/gtfs_source_cache.py"
    local output reclaimed
    if [[ ! -f "$helper" ]]; then
        echo "GTFS cache cleanup skipped reason=helper-missing path=$helper"
        return 0
    fi

    echo "GTFS cache cleanup root=$GTFS_CACHE_ROOT"

    local -a command=(
        python3 "$helper" cleanup
        --cache-root "$GTFS_CACHE_ROOT"
        --orphan-temp-max-age-hours "$GTFS_ORPHAN_TEMP_MAX_AGE_HOURS"
    )
    if [[ "$DRY_RUN" == "1" ]]; then
        command+=(--dry-run)
    fi
    if ! output="$("${command[@]}")"; then
        echo "GTFS cache cleanup skipped reason=helper-failed"
        return 0
    fi
    printf '%s\n' "$output"
    reclaimed="$(printf '%s\n' "$output" | sed -n 's/.*reclaimed_bytes=\([0-9][0-9]*\).*/\1/p' | tail -n 1)"
    if [[ "$reclaimed" =~ ^[0-9]+$ ]]; then
        RECLAIMABLE_BYTES=$((RECLAIMABLE_BYTES + reclaimed))
    fi
}

cleanup_gtfs_cache

echo "Cleanup reclaimable size: $(format_size "$RECLAIMABLE_BYTES") ($RECLAIMABLE_BYTES bytes)"

echo
echo "Temporary artifacts older than 2 days:"
find /tmp -maxdepth 1 \
    -type d \
    \( -name 'haltewecker-*' -o -name 'sweden-*' -o -name 'production-candidate*' -o -name 'stockholm-production-*' \) \
    -mtime +2 \
    -print

echo
df -h /
