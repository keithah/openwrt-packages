#!/bin/sh
set -eu

pages=${1:-pages}
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
public_key="$root/keithah-feed.pub"
usign=${USIGN:-usign}
signature=
secret_file=
signature_tmp=
public_tmp=
nojekyll_tmp=
complete=false
caught_signal=

cleanup() {
	status=$?
	trap - EXIT HUP INT TERM
	if [ "$complete" != true ]; then
		if [ -n "$signature" ] && { [ ! -d "$signature" ] || [ -L "$signature" ]; }; then
			rm -f -- "$signature" 2>/dev/null || :
		fi
	fi
	for temporary in "$signature_tmp" "$public_tmp" "$nojekyll_tmp" "$secret_file"; do
		[ -z "$temporary" ] || rm -f -- "$temporary" 2>/dev/null || :
	done
	if [ -n "$caught_signal" ]; then
		kill -s "$caught_signal" "$$" 2>/dev/null || exit "$status"
	fi
	exit "$status"
}
trap cleanup EXIT
trap 'caught_signal=HUP; exit 129' HUP
trap 'caught_signal=INT; exit 130' INT
trap 'caught_signal=TERM; exit 143' TERM

# Check every supplied path component lexically. Testing only the final string
# is insufficient because `link/.` and `parent-link/child` dereference a
# symlink before a final `test -L` can observe it. Dot components are harmless;
# parent traversal is rejected as ambiguous for a publication target.
case "$pages" in
	/*) path_cursor=/; path_rest=${pages#/} ;;
	*) path_cursor=; path_rest=$pages ;;
esac
while :; do
	case "$path_rest" in
		*/*) component=${path_rest%%/*}; path_rest=${path_rest#*/}; more=true ;;
		*) component=$path_rest; path_rest=; more=false ;;
	esac
	case "$component" in
		''|.) ;;
		..)
			echo 'Pages path must not contain parent traversal' >&2
			exit 1
			;;
		*)
			case "$path_cursor" in
				/) candidate="/$component" ;;
				'') candidate=$component ;;
				*) candidate="$path_cursor/$component" ;;
			esac
			if [ -L "$candidate" ]; then
				echo "Pages path must not contain symlinks: $candidate" >&2
				exit 1
			fi
			path_cursor=$candidate
			;;
	esac
	[ "$more" = true ] || break
done
if [ ! -d "$pages" ] || [ ! -f "$pages/Packages" ] || [ -L "$pages/Packages" ]; then
	echo 'assembled Pages directory with Packages is required' >&2
	exit 1
fi
pages=$(CDPATH= cd -P -- "$pages" && pwd)
signature="$pages/Packages.sig"
if [ -z "${OPENWRT_FEED_USIGN_PRIVATE_KEY:-}" ]; then
	echo 'OPENWRT_FEED_USIGN_PRIVATE_KEY is required' >&2
	exit 1
fi
for destination in "$signature" "$pages/keithah-feed.pub" "$pages/.nojekyll"; do
	if [ -d "$destination" ] && [ ! -L "$destination" ]; then
		echo "generated destination must not be a directory: $destination" >&2
		exit 1
	fi
done
# Invalidate any previous signature before signing. rm unlinks a symlink
# itself and never follows its target.
rm -f -- "$signature"
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

signature_tmp=$(mktemp "$pages/.Packages.sig.XXXXXX")
public_tmp=$(mktemp "$pages/.keithah-feed.pub.XXXXXX")
nojekyll_tmp=$(mktemp "$pages/..nojekyll.XXXXXX")
"$usign" -S -m "$pages/Packages" -s "$secret_file" -x "$signature_tmp"
if [ ! -s "$signature_tmp" ] || [ -L "$signature_tmp" ]; then
	echo 'usign did not create a valid signature' >&2
	exit 1
fi
cp "$public_key" "$public_tmp"
chmod 644 "$public_tmp" "$signature_tmp"
: >"$nojekyll_tmp"
chmod 644 "$nojekyll_tmp"
"$usign" -V -m "$pages/Packages" -p "$public_tmp" -x "$signature_tmp"

# Regular destinations are replaced atomically. Symlinks are explicitly
# unlinked first so even implementations that treat a symlink-to-directory as
# a directory cannot follow it. The verified signature is published last.
publish() {
	destination=$1
	temporary=$2
	name=$3
	if [ -L "$destination" ]; then
		rm -f -- "$destination"
	fi
	mv -f -- "$temporary" "$destination"
	if [ ! -f "$destination" ] || [ -L "$destination" ]; then
		echo "failed to publish regular artifact: $name" >&2
		exit 1
	fi
}
publish "$pages/keithah-feed.pub" "$public_tmp" keithah-feed.pub
public_tmp=
publish "$pages/.nojekyll" "$nojekyll_tmp" .nojekyll
nojekyll_tmp=
publish "$pages/Packages.sig" "$signature_tmp" Packages.sig
signature_tmp=

complete=true
