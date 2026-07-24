# Keith's OpenWrt packages

The public [`keithah/openwrt-packages`](https://github.com/keithah/openwrt-packages)
repository publishes Keith's signed OpenWrt package feed at
<https://keithah.github.io/openwrt-packages>. The feed currently supports only
the OpenWrt `aarch64_cortex-a53` architecture.

## Install

Choose either `wget` or `curl` for the package you want.

Ookla Speedtest CLI:

```sh
wget -qO- https://keithah.github.io/openwrt-packages/install-ookla-speedtest-cli.sh | sh
curl -fsSL https://keithah.github.io/openwrt-packages/install-ookla-speedtest-cli.sh | sh
```

Ookla Speedtest Web:

```sh
wget -qO- https://keithah.github.io/openwrt-packages/install-ookla-speedtest-web.sh | sh
curl -fsSL https://keithah.github.io/openwrt-packages/install-ookla-speedtest-web.sh | sh
```

Starwatch:

```sh
wget -qO- https://keithah.github.io/openwrt-packages/install-starwatch.sh | sh
curl -fsSL https://keithah.github.io/openwrt-packages/install-starwatch.sh | sh
```

Wattline:

```sh
wget -qO- https://keithah.github.io/openwrt-packages/install-wattline.sh | sh
curl -fsSL https://keithah.github.io/openwrt-packages/install-wattline.sh | sh
```

The installers configure the `keithah` opkg feed without disabling signature
checks. Its usign public-key fingerprint is `f6c72c675c844b91`. Every published
installer first repairs any already-present `starwatchd` and `wattlined`
packages, then runs the selected product's installer. This recovery never
installs an absent peer product. Ordinary `opkg update && opkg upgrade` remains
supported after the fixed package versions enter the feed.

## Packages

The feed carries the latest stable release artifacts for:

- Starwatch: `starwatchd`, `luci-app-starwatch`, and `gl-app-starwatch`;
- Wattline: `wattlined`, `wattline-bt`, `wattline-rtl8761b`,
  `luci-app-wattline`, and `gl-app-wattline`; and
- Speedtest: `ookla-speedtest-cli`;
- Speedtest Web: `ookla-speedtest-webd`, `luci-app-ookla-speedtest-web`, and
  `gl-app-ookla-speedtest-web`.

Each product remains independently built, tested, and released in its own
source repository:
[Starwatch](https://github.com/keithah/openwrt-starwatch),
[Wattline](https://github.com/keithah/openwrt-wattline), and
[Ookla Speedtest CLI packaging](https://github.com/keithah/openwrt-ookla-speedtest-cli).
[Ookla Speedtest packaging](https://github.com/keithah/openwrt-ookla-speedtest).
This repository downloads their release assets and installers; it does not
build or combine product source.

The publisher checks for stable upstream releases hourly and can also be run
manually. A missing, malformed, or incomplete upstream release fails the whole
publication, leaving the last verified Pages deployment live. Correct the
upstream release and rerun the publisher manually (or wait for the next hourly
run) to recover.

Pages also enforces a manifest release floor for every source, so it refuses to
publish a mixed-generation feed while a coordinated rollout is staggered. The
floors are raised with each coordinated product rollout. GitHub's optional
`immutable` release boolean is not required for current Ookla releases; the
tag and asset allowlists, canonical installer bytes, hashes, signing, and
deployable inventory remain the integrity boundary.

The Speedtest package downloads Ookla's official Linux binary while the package
is built; no Ookla binary or vendor archive is stored in this repository.
Ookla's software is proprietary, and its EULA/licensing prompt remains the
normal first-run behavior of the Speedtest CLI.
