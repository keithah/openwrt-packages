# keithah-installer-recovery-v1
set -eu

_keithah_root=${KEITHAH_ROOT:-/}
_keithah_feed_url=https://keithah.github.io/openwrt-packages
_keithah_supported_arch=aarch64_cortex-a53
_keithah_daemons='starwatchd wattlined'
_keithah_feed_name=keithah
_keithah_key_fingerprint=f6c72c675c844b91
_keithah_tmp_feed=
_keithah_tmp_filtered=
_keithah_tmp_key=
_keithah_tmp_expected_key=

_keithah_error() {
	printf '%s\n' "keithah installer recovery: $*" >&2
	exit 1
}

_keithah_cleanup() {
	[ -z "${_keithah_tmp_feed:-}" ] || rm -f "$_keithah_tmp_feed"
	[ -z "${_keithah_tmp_filtered:-}" ] || rm -f "$_keithah_tmp_filtered"
	[ -z "${_keithah_tmp_key:-}" ] || rm -f "$_keithah_tmp_key"
	[ -z "${_keithah_tmp_expected_key:-}" ] || rm -f "$_keithah_tmp_expected_key"
}

trap '_keithah_cleanup' 0
trap '_keithah_cleanup; trap - 0 HUP INT TERM; exit 1' HUP INT TERM

case $_keithah_root in
	/*) ;;
	*) _keithah_error 'preflight failed: KEITHAH_ROOT must be an absolute path' ;;
esac
[ -d "$_keithah_root" ] && [ ! -L "$_keithah_root" ] ||
	_keithah_error 'preflight failed: KEITHAH_ROOT is not a directory'
command -v opkg >/dev/null 2>&1 ||
	_keithah_error 'preflight failed: opkg is required'
command -v wget >/dev/null 2>&1 ||
	_keithah_error 'preflight failed: wget is required'

if ! _keithah_architectures=$(opkg print-architecture); then
	_keithah_error 'architecture check failed: opkg print-architecture failed'
fi
_keithah_arch_ok=
while IFS=' ' read -r _keithah_arch_word _keithah_arch_name _keithah_arch_priority; do
	if [ "$_keithah_arch_word" = arch ] &&
		[ "$_keithah_arch_name" = "$_keithah_supported_arch" ]; then
		_keithah_arch_ok=1
	fi
done <<EOF
$_keithah_architectures
EOF
[ -n "$_keithah_arch_ok" ] ||
	_keithah_error "unsupported architecture: $_keithah_supported_arch is required"

command -v awk >/dev/null 2>&1 ||
	_keithah_error 'preflight failed: awk is required'
command -v cmp >/dev/null 2>&1 ||
	_keithah_error 'preflight failed: cmp is required'
command -v mktemp >/dev/null 2>&1 ||
	_keithah_error 'preflight failed: mktemp is required'

_keithah_base=$_keithah_root
[ "$_keithah_base" != / ] || _keithah_base=
_keithah_opkg_dir=$_keithah_base/etc/opkg
_keithah_key_dir=$_keithah_opkg_dir/keys
_keithah_feed_file=$_keithah_opkg_dir/customfeeds.conf
_keithah_key_file=$_keithah_key_dir/$_keithah_key_fingerprint
[ -d "$_keithah_opkg_dir" ] && [ ! -L "$_keithah_opkg_dir" ] ||
	_keithah_error 'feed configuration failed: unsafe or missing opkg directory'
[ -d "$_keithah_key_dir" ] && [ ! -L "$_keithah_key_dir" ] ||
	_keithah_error 'feed configuration failed: unsafe or missing opkg key directory'
if [ -e "$_keithah_feed_file" ] || [ -L "$_keithah_feed_file" ]; then
	[ -f "$_keithah_feed_file" ] && [ ! -L "$_keithah_feed_file" ] ||
		_keithah_error 'feed configuration failed: unsafe customfeeds.conf'
fi
if [ -e "$_keithah_key_file" ] || [ -L "$_keithah_key_file" ]; then
	[ -f "$_keithah_key_file" ] && [ ! -L "$_keithah_key_file" ] ||
		_keithah_error 'feed configuration failed: unsafe feed key path'
fi

_keithah_tmp_feed=$(mktemp "$_keithah_opkg_dir/.keithah-feed.XXXXXX") ||
	_keithah_error 'feed configuration failed: cannot create feed temporary file'
_keithah_tmp_filtered=$(mktemp "$_keithah_opkg_dir/.keithah-filtered.XXXXXX") ||
	_keithah_error 'feed configuration failed: cannot create filter temporary file'
_keithah_tmp_key=$(mktemp "$_keithah_key_dir/.keithah-key.XXXXXX") ||
	_keithah_error 'feed configuration failed: cannot create key temporary file'
_keithah_tmp_expected_key=$(mktemp "$_keithah_key_dir/.keithah-expected-key.XXXXXX") ||
	_keithah_error 'feed configuration failed: cannot create expected-key temporary file'

if ! wget -qO "$_keithah_tmp_key" "$_keithah_feed_url/keithah-feed.pub"; then
	_keithah_error 'feed configuration failed: public-key download failed'
fi
cat >"$_keithah_tmp_expected_key" <<'EOF'
untrusted comment: Keith OpenWrt package feed
RWT2xyxnXIRLkZzbs1HvD+48GPkSqoNPCZVCOw49GUdTg2O7Cv9LzMtx
EOF
cmp -s "$_keithah_tmp_expected_key" "$_keithah_tmp_key" ||
	_keithah_error 'feed configuration failed: public key does not match pinned key'
chmod 0644 "$_keithah_tmp_key" ||
	_keithah_error 'feed configuration failed: cannot set public-key permissions'

if [ -f "$_keithah_feed_file" ]; then
	cp -p "$_keithah_feed_file" "$_keithah_tmp_feed" ||
		_keithah_error 'feed configuration failed: cannot preserve customfeeds.conf metadata'
else
	chmod 0644 "$_keithah_tmp_feed" ||
		_keithah_error 'feed configuration failed: cannot set customfeeds.conf permissions'
fi
awk '
	($1 == "src" || $1 == "src/gz") &&
	($2 == "starwatch" || $2 == "wattline" || $2 == "keithah") { next }
	{ print }
' "$_keithah_feed_file" >"$_keithah_tmp_filtered" 2>/dev/null ||
	_keithah_error 'feed configuration failed: cannot filter customfeeds.conf'
cat "$_keithah_tmp_filtered" >"$_keithah_tmp_feed" ||
	_keithah_error 'feed configuration failed: cannot write customfeeds.conf'
printf '%s\n' "src/gz $_keithah_feed_name $_keithah_feed_url" >>"$_keithah_tmp_feed" ||
	_keithah_error 'feed configuration failed: cannot append shared feed'

mv -f "$_keithah_tmp_key" "$_keithah_key_file" ||
	_keithah_error 'feed configuration failed: cannot install public key'
_keithah_tmp_key=
mv -f "$_keithah_tmp_feed" "$_keithah_feed_file" ||
	_keithah_error 'feed configuration failed: cannot install customfeeds.conf'
_keithah_tmp_feed=
rm -f "$_keithah_tmp_filtered" "$_keithah_tmp_expected_key"
_keithah_tmp_filtered=
_keithah_tmp_expected_key=

opkg update || _keithah_error 'feed update failed'
for _keithah_daemon in $_keithah_daemons; do
	if opkg status "$_keithah_daemon" >/dev/null 2>&1; then
		if ! opkg install "$_keithah_daemon"; then
			_keithah_error "installed-product repair failed: $_keithah_daemon"
		fi
	fi
done

trap - 0 HUP INT TERM
unset _keithah_root _keithah_feed_url _keithah_supported_arch _keithah_daemons
unset _keithah_feed_name _keithah_key_fingerprint _keithah_architectures
unset _keithah_arch_ok _keithah_arch_word _keithah_arch_name _keithah_arch_priority
unset _keithah_base _keithah_opkg_dir _keithah_key_dir _keithah_feed_file
unset _keithah_key_file _keithah_tmp_feed _keithah_tmp_filtered _keithah_tmp_key
unset _keithah_tmp_expected_key _keithah_daemon
