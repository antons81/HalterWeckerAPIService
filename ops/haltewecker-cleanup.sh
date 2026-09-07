#!/usr/bin/env bash
set -euo pipefail

SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
FLOCK_BIN="${FLOCK_BIN:-flock}"
DATA_ROOT="${DATA_ROOT:-/srv/haltewecker/data}"
DATA="$(cd "$DATA_ROOT" && pwd -P)"
RELEASES="$DATA/releases"
DEPARTURE_RELEASES="$DATA/departures/releases"
ROLLBACK_ROOT="$DATA/temp/current-rollback"
DRY_RUN="${HALTEWECKER_CLEANUP_DRY_RUN:-0}"
ABANDONED_RELEASE_MAX_AGE_HOURS="${HALTEWECKER_ABANDONED_RELEASE_MAX_AGE_HOURS:-24}"
VALIDATION_MAX_AGE_HOURS="${HALTEWECKER_VALIDATION_MAX_AGE_HOURS:-48}"
STAGING_MAX_AGE_HOURS="${HALTEWECKER_STAGING_MAX_AGE_HOURS:-24}"
LEGACY_EXTRA_BACKUP_MAX_AGE_HOURS="${HALTEWECKER_LEGACY_EXTRA_BACKUP_MAX_AGE_HOURS:-48}"
PIPELINE_REPO="${HALTEWECKER_PIPELINE_REPO:-/srv/haltewecker/pipeline/HalterWeckerAPIService}"
GTFS_CACHE_ROOT="${GTFS_CACHE_ROOT:-/srv/haltewecker/cache/gtfs}"
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

# The full and scoped pipelines share these locks. Holding both prevents a
# release from being staged or activated while retention is being evaluated.
LOCKS="${HALTEWECKER_CLEANUP_LOCKS:-/run/lock/haltewecker-stop-data.lock:/run/lock/haltewecker-static-departures.lock}"
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

protect_reference "$DATA/current-release" "current-release"

while IFS= read -r -d '' symlink_path; do
    case "$symlink_path" in
	"$DATA/current-release"|"$DATA/previous/stop-data") continue ;;
    esac
    protect_reference "$symlink_path" "active-symlink:$symlink_path"
done < <(find "$DATA" -path "$RELEASES" -prune -o -type l -print0)

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
    [[ "$1" =~ ^(scoped-)?[0-9]{8}T[0-9]{6}Z-[[:alnum:]]+$ ]]
}

is_published_release() {
    local release_path="$1"
    [[ -d "$release_path/stop-data" ]] || return 1
    [[ -f "$release_path/departures.sqlite" ]] || return 1
    [[ -f "$release_path/release-metadata.json" ]] || return 1
    return 0
}

ordered_candidates="$(mktemp "${TMPDIR:-/tmp}/haltewecker-release-retention.XXXXXX")"
trap 'rm -f -- "$ordered_candidates"' EXIT

shopt -s nullglob
release_entries=("$RELEASES"/*)
for release_path in "${release_entries[@]}"; do
    [[ -L "$release_path" || -d "$release_path" ]] || continue
    [[ -L "$release_path" ]] && continue
    release_name="${release_path##*/}"
    is_release_name "$release_name" || continue
    is_published_release "$release_path" || continue
    sortable_name="$release_name"
    [[ "$sortable_name" == scoped-* ]] && sortable_name="${sortable_name#scoped-}"
    printf '%s\t%s\n' "${sortable_name%%-*}" "$release_name" >> "$ordered_candidates"
done

ordered_release_names=()
while IFS=$'\t' read -r sortable_timestamp release_name; do
    [[ -n "$release_name" ]] || continue
    ordered_release_names+=("$release_name")
done < <(sort -r -k1,1 -k2,2 "$ordered_candidates")
retention_slot=0
for release_name in "${ordered_release_names[@]}"; do
    (( retention_slot >= retention_count )) && break
    retention_slot=$((retention_slot + 1))
    append_reason "$release_name" "retention-slot=$retention_slot/$retention_count"
done

echo "Release retention: configured=$configured_retention effective=$retention_count"

for release_path in "${release_entries[@]}"; do
    [[ -L "$release_path" || -d "$release_path" ]] || continue
    release_name="${release_path##*/}"

    if [[ -L "$release_path" ]]; then
        echo "KEEP   $release_path reason=release-entry-is-symlink"
        continue
    fi
    if [[ ! -d "$release_path" ]]; then
        continue
    fi
    if ! is_release_name "$release_name"; then
        echo "KEEP   $release_path reason=not-a-published-release-name"
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
    local entry age_seconds ttl_seconds

    if [[ ! -d "$root" ]]; then
        echo "Artifact retention: directory-missing path=$root"
        return 0
    fi

    ttl_seconds="$(ttl_seconds_from_hours "$ttl_hours")"
    echo "Artifact retention: directory=$root max-age-hours=$ttl_hours"
    while IFS= read -r -d '' entry; do
        if [[ -L "$entry" ]]; then
            echo "KEEP   $entry reason=symlink-entry"
            continue
        fi
        if [[ ! -e "$entry" ]]; then
            echo "KEEP   $entry reason=missing-entry"
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
