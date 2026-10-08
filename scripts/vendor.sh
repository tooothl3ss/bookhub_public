#!/bin/bash
# Pinned copies of the page's libraries: report newer releases, or fetch them.
#   scripts/vendor.sh check    # one line per library; never fails the caller
#   scripts/vendor.sh update   # download the newest release of the same major line and repoint the page
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=myshelf/static/vendor
PAGE=myshelf/static/index.html
MODE=${1:-check}

# The CDN addresses the page used to load redirect to the newest release of the pinned major line.
newest() {
    case "$1" in
        alpine) curl -fsSI -m 8 -o /dev/null -w '%{redirect_url}' 'https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js' | sed -nE 's#.*alpinejs@([0-9.]+)/.*#\1#p' ;;
        tailwind) curl -fsSI -m 8 -o /dev/null -w '%{redirect_url}' 'https://cdn.tailwindcss.com' | sed -nE 's#.*/([0-9]+\.[0-9.]+)$#\1#p' ;;
    esac
}
source_url() {
    case "$1" in
        alpine) echo "https://unpkg.com/alpinejs@$2/dist/cdn.min.js" ;;
        tailwind) echo "https://cdn.tailwindcss.com/$2" ;;
    esac
}
file_name() {
    case "$1" in
        alpine) echo "alpine-$2.min.js" ;;
        tailwind) echo "tailwind-$2.js" ;;
    esac
}
pinned() {
    local found
    found=$(ls "$DIR" 2>/dev/null | sed -nE "s#^$1-([0-9][0-9.]*[0-9])(\.min)?\.js\$#\1#p" | sort -V | tail -1)
    echo "$found"
}

for lib in alpine tailwind; do
    have=$(pinned "$lib")
    if [ -z "$have" ]; then echo "Vendor: $lib is not pinned in $DIR"; continue; fi
    latest=$(newest "$lib" || true)
    if [ -z "$latest" ]; then echo "Vendor: $lib $have, newest release unknown (no network)"; continue; fi
    if [ "$have" = "$latest" ]; then echo "Vendor: $lib $have is current"; continue; fi
    if [ "$MODE" != update ]; then
        echo "Vendor: $lib $have -> $latest available; run scripts/vendor.sh update, then deploy"
        continue
    fi
    old=$(file_name "$lib" "$have"); new=$(file_name "$lib" "$latest")
    curl -fsSL -m 60 -o "$DIR/$new.part" "$(source_url "$lib" "$latest")"
    [ -s "$DIR/$new.part" ] || { rm -f "$DIR/$new.part"; echo "Vendor: $lib $latest download failed" >&2; exit 1; }
    mv "$DIR/$new.part" "$DIR/$new"
    grep -q "vendor/$old" "$PAGE" || { echo "Vendor: $PAGE does not reference $old; nothing repointed" >&2; exit 1; }
    sed -i "s#vendor/$old#vendor/$new#g" "$PAGE"
    rm -f "$DIR/$old"
    echo "Vendor: $lib $have -> $latest; check the page, then deploy"
done
