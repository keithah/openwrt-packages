#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
TMP=${TMPDIR:-/tmp}/installer-recovery-test.$$
trap 'rm -rf "$TMP"' 0 HUP INT TERM

fail() {
	printf '%s\n' "installer_recovery_test: $*" >&2
	exit 1
}

mkdir -p "$TMP/bin"

cat >"$TMP/bin/opkg" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >>"$MOCK_LOG"
case $1 in
	print-architecture)
		printf '%s\n' "arch all 1" "arch ${MOCK_ARCH:-aarch64_cortex-a53} 10"
		;;
	update)
		;;
	status)
		case " ${MOCK_INSTALLED:-} " in
			*" $2 "*) printf 'Package: %s\nStatus: install user installed\n' "$2" ;;
			*) exit 1 ;;
		esac
		;;
	install)
		[ "${MOCK_INSTALL_FAIL:-}" != "$2" ]
		;;
	*) exit 2 ;;
esac
EOF
chmod +x "$TMP/bin/opkg"

cat >"$TMP/bin/wget" <<'EOF'
#!/bin/sh
[ "$1" = -qO ] && [ "$3" = https://keithah.github.io/openwrt-packages/keithah-feed.pub ] || exit 2
cat "$MOCK_PUBLIC_KEY" >"$2"
EOF
chmod +x "$TMP/bin/wget"

run_case() {
	_name=$1
	_installed=$2
	_fail_package=$3
	_arch=$4
	_case_dir=$TMP/$_name
	mkdir -p "$_case_dir/root/etc/opkg/keys"
	printf '%s\n' \
		'src/gz unrelated https://example.invalid/packages' \
		'src/gz starwatch https://old.invalid/starwatch' \
		>"$_case_dir/root/etc/opkg/customfeeds.conf"
	cp "$_case_dir/root/etc/opkg/customfeeds.conf" "$_case_dir/customfeeds.before"
	: >"$_case_dir/log"
	cat >"$_case_dir/upstream.sh" <<'EOF'
[ -z "$(trap)" ] || exit 91
[ "${_keithah_root+x}" != x ] || exit 92
[ "${_keithah_tmp_feed+x}" != x ] || exit 93
printf "%s\n" upstream >>"$MOCK_LOG"
EOF
	cat "$ROOT/scripts/installer_recovery.sh" "$_case_dir/upstream.sh" >"$_case_dir/combined.sh"
	if PATH="$TMP/bin:$PATH" \
		KEITHAH_ROOT="$_case_dir/root" \
		MOCK_LOG="$_case_dir/log" \
		MOCK_PUBLIC_KEY="$ROOT/keithah-feed.pub" \
		MOCK_INSTALLED="$_installed" \
		MOCK_INSTALL_FAIL="$_fail_package" \
		MOCK_ARCH="$_arch" \
		sh "$_case_dir/combined.sh" >"$_case_dir/stdout" 2>"$_case_dir/stderr"; then
		_status=0
	else
		_status=$?
	fi
}

assert_log() {
	_expected=$1
	_actual=$2
	[ "$(cat "$_actual")" = "$_expected" ] ||
		fail "unexpected command sequence: $(tr '\n' '|' <"$_actual")"
}

run_migration_case() {
	_case_dir=$TMP/migration
	mkdir -p "$_case_dir/root/etc/opkg/keys"
	printf '%s\n%s\n%s' \
		'src/gz starwatch https://old.invalid/starwatch' \
		'src/gz keithah https://duplicate.invalid/packages' \
		'src/gz unrelated https://example.invalid/packages' \
		>"$_case_dir/root/etc/opkg/customfeeds.conf"
	chmod 0640 "$_case_dir/root/etc/opkg/customfeeds.conf"
	stat -c '%a:%u:%g' "$_case_dir/root/etc/opkg/customfeeds.conf" \
		>"$_case_dir/feed.metadata.expected"
	printf '%s\n' 'obsolete key' \
		>"$_case_dir/root/etc/opkg/keys/f6c72c675c844b91"
	chmod 0600 "$_case_dir/root/etc/opkg/keys/f6c72c675c844b91"
	stat -c '%a:%u:%g' "$_case_dir/root/etc/opkg/keys/f6c72c675c844b91" \
		>"$_case_dir/key.metadata.expected"
	: >"$_case_dir/log"
	cat >"$_case_dir/upstream.sh" <<'EOF'
[ -z "$(trap)" ] || exit 91
printf "%s\n" upstream >>"$MOCK_LOG"
EOF
	cat "$ROOT/scripts/installer_recovery.sh" "$_case_dir/upstream.sh" >"$_case_dir/combined.sh"
	for _run in first second; do
		PATH="$TMP/bin:$PATH" \
		KEITHAH_ROOT="$_case_dir/root" \
		MOCK_LOG="$_case_dir/log" \
		MOCK_PUBLIC_KEY="$ROOT/keithah-feed.pub" \
		MOCK_INSTALLED='' \
		MOCK_INSTALL_FAIL='' \
		MOCK_ARCH=aarch64_cortex-a53 \
		sh "$_case_dir/combined.sh" >"$_case_dir/stdout.$_run" \
		2>"$_case_dir/stderr.$_run" || fail "migration $_run run failed"
		if [ "$_run" = first ]; then
			cp "$_case_dir/root/etc/opkg/customfeeds.conf" "$_case_dir/feed.first"
			cp "$_case_dir/root/etc/opkg/keys/f6c72c675c844b91" "$_case_dir/key.first"
		fi
	done
}

run_case no_peers '' '' aarch64_cortex-a53
[ "$_status" -eq 0 ] || fail "no-peers case failed"
assert_log 'print-architecture
update
status starwatchd
status wattlined
upstream' "$TMP/no_peers/log"

run_case starwatch_peer 'starwatchd' '' aarch64_cortex-a53
[ "$_status" -eq 0 ] || fail "Starwatch-peer case failed"
assert_log 'print-architecture
update
status starwatchd
install starwatchd
status wattlined
upstream' "$TMP/starwatch_peer/log"

run_case both_peers 'starwatchd wattlined' '' aarch64_cortex-a53
[ "$_status" -eq 0 ] || fail "both-peers case failed"
assert_log 'print-architecture
update
status starwatchd
install starwatchd
status wattlined
install wattlined
upstream' "$TMP/both_peers/log"

run_case repair_failure 'starwatchd wattlined' starwatchd aarch64_cortex-a53
[ "$_status" -ne 0 ] || fail "repair failure unexpectedly succeeded"
assert_log 'print-architecture
update
status starwatchd
install starwatchd' "$TMP/repair_failure/log"
grep -F 'keithah installer recovery: installed-product repair failed: starwatchd' \
	"$TMP/repair_failure/stderr" >/dev/null || fail "repair failure message is missing"

run_case bad_arch 'starwatchd wattlined' '' x86_64
[ "$_status" -ne 0 ] || fail "bad architecture unexpectedly succeeded"
assert_log 'print-architecture' "$TMP/bad_arch/log"
cmp "$TMP/bad_arch/customfeeds.before" \
	"$TMP/bad_arch/root/etc/opkg/customfeeds.conf" >/dev/null ||
	fail "bad architecture mutated the feed configuration"
[ ! -e "$TMP/bad_arch/root/etc/opkg/keys/f6c72c675c844b91" ] ||
	fail "bad architecture installed the feed key"

run_migration_case
printf '%s\n%s' \
	'src/gz keithah https://keithah.github.io/openwrt-packages' \
	'src/gz unrelated https://example.invalid/packages' \
	>"$TMP/migration/feed.expected"
cmp "$TMP/migration/feed.expected" \
	"$TMP/migration/root/etc/opkg/customfeeds.conf" >/dev/null ||
	fail "migration did not preserve exact unrelated feed bytes"
stat -c '%a:%u:%g' "$TMP/migration/root/etc/opkg/customfeeds.conf" \
	>"$TMP/migration/feed.metadata.actual"
cmp "$TMP/migration/feed.metadata.expected" \
	"$TMP/migration/feed.metadata.actual" >/dev/null ||
	fail "migration did not preserve feed metadata"
cmp "$ROOT/keithah-feed.pub" \
	"$TMP/migration/root/etc/opkg/keys/f6c72c675c844b91" >/dev/null ||
	fail "migration did not install the exact pinned key"
stat -c '%a:%u:%g' "$TMP/migration/root/etc/opkg/keys/f6c72c675c844b91" \
	>"$TMP/migration/key.metadata.actual"
cmp "$TMP/migration/key.metadata.expected" \
	"$TMP/migration/key.metadata.actual" >/dev/null ||
	fail "migration did not preserve key metadata"
cmp "$TMP/migration/feed.first" \
	"$TMP/migration/root/etc/opkg/customfeeds.conf" >/dev/null ||
	fail "second migration changed feed bytes"
cmp "$TMP/migration/key.first" \
	"$TMP/migration/root/etc/opkg/keys/f6c72c675c844b91" >/dev/null ||
	fail "second migration changed key bytes"

printf '%s\n' 'installer recovery tests passed'
