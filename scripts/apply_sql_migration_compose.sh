#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 path/to/migration.sql" >&2
  exit 2
fi

migration=$1
mysql_container=${MYSQL_CONTAINER:-jobbridge-mysql}
remote="/tmp/$(basename -- "$migration")"

cleanup() {
  docker exec "$mysql_container" rm -f "$remote" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker cp "$migration" "$mysql_container:$remote"
docker exec "$mysql_container" sh -c '
  mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" -D"$MYSQL_DATABASE" < "$1"
' sh "$remote"
