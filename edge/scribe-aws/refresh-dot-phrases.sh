#!/bin/sh
# Read-only snapshot of booker dot phrases for scribe-aws (booker untouched). Atomic, only when changed.
SRC=/home/deploy/edge/booker-logs/dot-phrases.json
DST=/home/deploy/edge/scribe-aws/dot-phrases/dot-phrases.json
[ -f "$SRC" ] || exit 0
cmp -s "$SRC" "$DST" && exit 0
cp "$SRC" "$DST.tmp" && chmod 644 "$DST.tmp" && mv -f "$DST.tmp" "$DST"
