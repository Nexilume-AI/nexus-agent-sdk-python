#!/bin/sh
# Persist the large-manifest fix across router container recreation. No-op on
# updated firmware; transform only the exact old parser and retain other code.
set -eu
target=/usr/sbin/nexus-cloud-connectord
grep -Fq 'content="$(cat "$MANIFEST_SNAPSHOT")"' "$target" || exit 0
umask 077
temporary=$(mktemp /tmp/nexus-manifest-parser.XXXXXX)
trap 'rm -f "$temporary"' EXIT
awk '
$0 == "\tcontent=\"$(cat \"$MANIFEST_SNAPSHOT\")\"" {
    if ((getline) <= 0 || $0 != "\tjson_load \"$content\" || return 1") exit 1
    print "\tjson_load_file \"$MANIFEST_SNAPSHOT\" || return 1"
    next
}
{ print }
' "$target" > "$temporary"
sh -n "$temporary"
cat "$temporary" > "$target"
chmod 755 "$target"
