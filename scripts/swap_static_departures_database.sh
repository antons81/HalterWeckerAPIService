#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/srv/haltewecker/data}"
NEXT="$DATA_ROOT/staging/departures-next.sqlite"
RELEASES="$DATA_ROOT/departures/releases"
CURRENT="$DATA_ROOT/departures-current.sqlite"

test -s "$NEXT"
python3 - "$NEXT" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
assert dict(db.execute('select key, value from metadata'))['databaseVersion']
assert db.execute('select count(*) from active_services').fetchone()[0] > 0
PY

mkdir -p "$RELEASES"
VERSION="$(python3 - "$NEXT" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
print(dict(db.execute('select key, value from metadata'))['databaseVersion'])
PY
)"
RELEASE="$RELEASES/departures-$VERSION.sqlite"
mv "$NEXT" "$RELEASE"
LINK_NEXT="$DATA_ROOT/departures-current.next"
ln -s "departures/releases/$(basename "$RELEASE")" "$LINK_NEXT"
mv -Tf "$LINK_NEXT" "$CURRENT"
echo "$VERSION"
