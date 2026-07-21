#!/bin/sh
set -eu

pages=${1:-pages}
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
public_key="$root/keithah-feed.pub"
usign=${USIGN:-usign}
signature="$pages/Packages.sig"
secret_file=
complete=false

cleanup() {
	status=$?
	trap - EXIT HUP INT TERM
	if [ "$complete" != true ]; then
		rm -f -- "$signature"
	fi
	if [ -n "$secret_file" ]; then
		rm -f -- "$secret_file"
	fi
	if [ "$status" -gt 128 ]; then
		kill -"$((status - 128))" "$$" 2>/dev/null || exit "$status"
	fi
	exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Invalidate any previous signature before checking configuration. No failure
# path may leave an old or partially-created signature deployable.
rm -f -- "$signature"

if [ -z "${OPENWRT_FEED_USIGN_PRIVATE_KEY:-}" ]; then
	echo 'OPENWRT_FEED_USIGN_PRIVATE_KEY is required' >&2
	exit 1
fi
if [ ! -d "$pages" ] || [ -L "$pages" ] || [ ! -f "$pages/Packages" ] || [ -L "$pages/Packages" ]; then
	echo 'assembled Pages directory with Packages is required' >&2
	exit 1
fi
if [ ! -f "$public_key" ] || [ -L "$public_key" ]; then
	echo 'pinned public key is missing' >&2
	exit 1
fi
if ! command -v "$usign" >/dev/null 2>&1; then
	echo "usign command not found: $usign" >&2
	exit 1
fi

# mktemp creates the file outside the Pages tree (under the runner's TMPDIR).
# An explicit umask and chmod keep this invariant independent of platform
# defaults. printf avoids interpreting private-key bytes as options or escapes.
umask 077
signing_key=$OPENWRT_FEED_USIGN_PRIVATE_KEY
unset OPENWRT_FEED_USIGN_PRIVATE_KEY
secret_file=$(mktemp "${TMPDIR:-/tmp}/openwrt-feed-signing-key.XXXXXX")
chmod 600 "$secret_file"
pages_real=$(CDPATH= cd -P -- "$pages" && pwd)
secret_real_dir=$(CDPATH= cd -P -- "$(dirname -- "$secret_file")" && pwd)
case "$secret_real_dir/" in
	"$pages_real"|"$pages_real"/*)
		echo 'temporary signing key must be outside Pages' >&2
		exit 1
		;;
esac
printf '%s' "$signing_key" >"$secret_file"
signing_key=

"$usign" -S -m "$pages/Packages" -s "$secret_file" -x "$signature"
if [ ! -s "$signature" ] || [ -L "$signature" ]; then
	echo 'usign did not create a valid signature' >&2
	exit 1
fi
cp "$public_key" "$pages/keithah-feed.pub"
chmod 644 "$pages/keithah-feed.pub" "$signature"
: >"$pages/.nojekyll"
chmod 644 "$pages/.nojekyll"
"$usign" -V -m "$pages/Packages" -p "$pages/keithah-feed.pub" -x "$signature"

complete=true
