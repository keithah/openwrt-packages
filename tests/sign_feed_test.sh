#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
pages="$tmp/pages"
key_tmp="$tmp/key-tmp"
mkdir -p "$pages" "$key_tmp"

make_pages() {
	rm -rf "$pages"
	mkdir -p "$pages"
	printf '%s\n' 'Package: test' 'Version: 1-1' 'Architecture: all' >"$pages/Packages"
	python3 - "$pages" <<'PY'
import gzip
import pathlib
import sys
p = pathlib.Path(sys.argv[1])
with (p / "Packages.gz").open("wb") as raw:
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
        stream.write((p / "Packages").read_bytes())
PY
}

cat >"$tmp/fake-usign" <<'SH'
#!/bin/sh
set -eu
mode=$1
shift
message=
secret=
public=
signature=
while [ "$#" -gt 0 ]; do
	case "$1" in
		-m) message=$2; shift 2 ;;
		-s) secret=$2; shift 2 ;;
		-p) public=$2; shift 2 ;;
		-x) signature=$2; shift 2 ;;
		*) exit 90 ;;
	esac
done
printf '%s %s\n' "$mode" "$(basename -- "$message")" >>"$FAKE_LOG"
case "$mode" in
	-S)
		[ -z "${OPENWRT_FEED_USIGN_PRIVATE_KEY:-}" ]
		[ "$message" = "$FAKE_PAGES/Packages" ]
		case "$signature" in "$FAKE_PAGES"/.Packages.sig.*) ;; *) exit 89 ;; esac
		[ -f "$secret" ]
		[ "$(stat -c %a "$secret")" = 600 ]
		case "$secret" in "$FAKE_PAGES"/*) exit 91 ;; esac
		[ "$(cat "$secret")" = "$FAKE_EXPECTED_SECRET" ]
		printf 'new signature\n' >"$signature"
		if [ "${FAKE_SIGNAL_PARENT:-0}" = 1 ]; then
			kill -TERM "$PPID"
			exit 95
		fi
		[ "${FAKE_SIGN_FAIL:-0}" != 1 ] || exit 92
		;;
	-V)
		[ -z "${OPENWRT_FEED_USIGN_PRIVATE_KEY:-}" ]
		[ "$message" = "$FAKE_PAGES/Packages" ]
		[ "$(cat "$public")" = "$(cat "$FAKE_EXPECTED_PUBLIC")" ]
		case "$signature" in "$FAKE_PAGES"/.Packages.sig.*) ;; *) exit 88 ;; esac
		[ "$(cat "$signature")" = 'new signature' ]
		[ "${FAKE_VERIFY_FAIL:-0}" != 1 ] || exit 93
		;;
	*) exit 94 ;;
esac
SH
chmod 755 "$tmp/fake-usign"

export USIGN="$tmp/fake-usign"
export TMPDIR="$key_tmp"
export FAKE_LOG="$tmp/usign.log"
export FAKE_PAGES="$pages"
export FAKE_EXPECTED_SECRET='private secret content'
export FAKE_EXPECTED_PUBLIC="$root/keithah-feed.pub"

# A missing key fails closed and invalidates a stale signature before invoking usign.
make_pages
printf 'stale\n' >"$pages/Packages.sig"
if env -u OPENWRT_FEED_USIGN_PRIVATE_KEY sh "$root/scripts/sign_feed.sh" "$pages" >"$tmp/out" 2>"$tmp/err"; then
	echo 'missing key unexpectedly succeeded' >&2
	exit 1
fi
grep -F 'OPENWRT_FEED_USIGN_PRIVATE_KEY is required' "$tmp/err" >/dev/null
[ ! -e "$pages/Packages.sig" ]

# Even a hostile TMPDIR cannot place private-key material in the publish tree.
make_pages
if TMPDIR="$pages" OPENWRT_FEED_USIGN_PRIVATE_KEY="$FAKE_EXPECTED_SECRET" \
	sh "$root/scripts/sign_feed.sh" "$pages" >"$tmp/out" 2>"$tmp/err"; then
	echo 'Pages-local signing key unexpectedly succeeded' >&2
	exit 1
fi
grep -F 'temporary signing key must be outside Pages' "$tmp/err" >/dev/null
[ ! -e "$pages/Packages.sig" ]
if find "$pages" -name 'openwrt-feed-signing-key.*' -print -quit | grep . >/dev/null; then
	echo 'temporary signing key leaked into Pages' >&2
	exit 1
fi

# A signer failure after writing output cannot leave a deployable signature or secret.
make_pages
: >"$FAKE_LOG"
if OPENWRT_FEED_USIGN_PRIVATE_KEY="$FAKE_EXPECTED_SECRET" FAKE_SIGN_FAIL=1 \
	sh "$root/scripts/sign_feed.sh" "$pages" >"$tmp/out" 2>"$tmp/err"; then
	echo 'failed signer unexpectedly succeeded' >&2
	exit 1
fi
[ ! -e "$pages/Packages.sig" ]
[ -z "$(find "$key_tmp" -mindepth 1 -print -quit)" ]

# A termination signal while signing cleans both partial signature and key.
make_pages
set +e
OPENWRT_FEED_USIGN_PRIVATE_KEY="$FAKE_EXPECTED_SECRET" FAKE_SIGNAL_PARENT=1 \
	sh "$root/scripts/sign_feed.sh" "$pages" >"$tmp/out" 2>"$tmp/err"
signal_status=$?
set -e
[ "$signal_status" -ne 0 ]
[ ! -e "$pages/Packages.sig" ]
[ -z "$(find "$key_tmp" -mindepth 1 -print -quit)" ]

# A verification/public-private mismatch also removes the newly-created signature.
make_pages
: >"$FAKE_LOG"
if OPENWRT_FEED_USIGN_PRIVATE_KEY="$FAKE_EXPECTED_SECRET" FAKE_VERIFY_FAIL=1 \
	sh "$root/scripts/sign_feed.sh" "$pages" >"$tmp/out" 2>"$tmp/err"; then
	echo 'failed verification unexpectedly succeeded' >&2
	exit 1
fi
[ ! -e "$pages/Packages.sig" ]
[ -z "$(find "$key_tmp" -mindepth 1 -print -quit)" ]
grep -F -- '-S Packages' "$FAKE_LOG" >/dev/null
grep -F -- '-V Packages' "$FAKE_LOG" >/dev/null

# Generated destinations never follow attacker-controlled symlinks.
make_pages
printf 'signature target\n' >"$tmp/external-signature"
printf 'public target\n' >"$tmp/external-public"
printf 'jekyll target\n' >"$tmp/external-jekyll"
ln -s "$tmp/external-signature" "$pages/Packages.sig"
ln -s "$tmp/external-public" "$pages/keithah-feed.pub"
ln -s "$tmp/external-jekyll" "$pages/.nojekyll"
OPENWRT_FEED_USIGN_PRIVATE_KEY="$FAKE_EXPECTED_SECRET" \
	sh "$root/scripts/sign_feed.sh" "$pages"
[ "$(cat "$tmp/external-signature")" = 'signature target' ]
[ "$(cat "$tmp/external-public")" = 'public target' ]
[ "$(cat "$tmp/external-jekyll")" = 'jekyll target' ]
for output in Packages.sig keithah-feed.pub .nojekyll; do
	[ -f "$pages/$output" ]
	[ ! -L "$pages/$output" ]
done

# Real directories at generated destinations fail before signing.
make_pages
mkdir "$pages/keithah-feed.pub"
if OPENWRT_FEED_USIGN_PRIVATE_KEY="$FAKE_EXPECTED_SECRET" \
	sh "$root/scripts/sign_feed.sh" "$pages" >"$tmp/out" 2>"$tmp/err"; then
	echo 'directory destination unexpectedly succeeded' >&2
	exit 1
fi
grep -F 'generated destination must not be a directory' "$tmp/err" >/dev/null
[ ! -e "$pages/Packages.sig" ]

# A trailing slash cannot disguise a symlinked Pages root.
make_pages
ln -s "$pages" "$tmp/pages-link"
if OPENWRT_FEED_USIGN_PRIVATE_KEY="$FAKE_EXPECTED_SECRET" \
	sh "$root/scripts/sign_feed.sh" "$tmp/pages-link/" >"$tmp/out" 2>"$tmp/err"; then
	echo 'symlinked Pages root unexpectedly succeeded' >&2
	exit 1
fi
grep -F 'assembled Pages directory with Packages is required' "$tmp/err" >/dev/null
[ ! -e "$pages/Packages.sig" ]

# Success signs the uncompressed index, verifies it, and publishes trust metadata.
make_pages
: >"$FAKE_LOG"
OPENWRT_FEED_USIGN_PRIVATE_KEY="$FAKE_EXPECTED_SECRET" \
	sh "$root/scripts/sign_feed.sh" "$pages"
[ "$(cat "$pages/Packages.sig")" = 'new signature' ]
cmp "$root/keithah-feed.pub" "$pages/keithah-feed.pub"
[ -f "$pages/.nojekyll" ]
[ ! -s "$pages/.nojekyll" ]
[ -z "$(find "$key_tmp" -mindepth 1 -print -quit)" ]
[ "$(sed -n '1p' "$FAKE_LOG")" = '-S Packages' ]
[ "$(sed -n '2p' "$FAKE_LOG")" = '-V Packages' ]
[ "$(wc -l <"$FAKE_LOG" | tr -d ' ')" = 2 ]
! grep -F "$FAKE_EXPECTED_SECRET" "$FAKE_LOG" >/dev/null

echo 'sign feed tests passed'
