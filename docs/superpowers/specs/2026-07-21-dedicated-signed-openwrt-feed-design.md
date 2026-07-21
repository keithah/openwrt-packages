# Dedicated Signed OpenWrt Feed Design

## Goal

Create `keithah/openwrt-packages` as the dedicated owner of Keith's signed
OpenWrt package feed. It aggregates independently produced Starwatch,
Wattline, and Ookla Speedtest CLI packages, publishes product-specific
installers, signs one package index, and owns the GitHub Pages deployment.

The product repositories remain independent. None builds, checks out, or
contains another product.

## Current State

The logical shared `keithah` feed is currently assembled and deployed by
`keithah/openwrt-starwatch`. That workflow builds Starwatch locally, downloads
Wattline release IPKs, generates `Packages` and `Packages.gz`, signs `Packages`
with the `STARWATCH_USIGN_PRIVATE_KEY` repository secret, and deploys
`https://keithah.github.io/openwrt-starwatch/`.

The intent to move this responsibility into a dedicated
`keithah/openwrt-packages` repository was already documented in Wattline, but
the migration was not executed. This design completes that migration and adds
Speedtest as a third product.

## Scope

The shared feed continues to support the OpenWrt package architecture
`aarch64_cortex-a53`. Speedtest uses Ookla's official `linux-aarch64` archive.
The repository does not publish armhf or armel packages until actual OpenWrt
target architecture identifiers are selected.

The feed contains:

- the latest Starwatch release IPKs;
- the latest Wattline release IPKs;
- the latest `ookla-speedtest-cli` release IPK;
- `install-starwatch.sh`;
- `install-wattline.sh`;
- `install-ookla-speedtest-cli.sh`;
- `Packages`, `Packages.gz`, `Packages.sig`, and `keithah-feed.pub`; and
- `.nojekyll` for GitHub Pages.

## Repository Boundaries

### Product repositories

Each product owns its source, tests, installer, package builder, release
workflow, and release artifacts. Product repositories publish installable
artifacts but do not publish the shared feed and do not refer to one another.

Starwatch and Wattline retain their existing release-IPK workflows. Their
installers and daemon-package upgrade hooks migrate managed feed configuration
to the dedicated feed URL.

`openwrt-ookla-speedtest-cli` gains:

- a deterministic builder whose output follows
  `ookla-speedtest-cli_VERSION-1_aarch64_cortex-a53.ipk`;
- a package-specific installer;
- tests that inspect the IPK control metadata and payload; and
- a release workflow that publishes `v<PKG_VERSION>` after a version update
  reaches `main`.

The Speedtest builder downloads the official archive into temporary build
storage, verifies the pinned SHA-256, reuses the archive and ELF validation in
the updater, and packages only `/usr/bin/speedtest`. No vendor archive or
binary is committed.

### Feed repository

`openwrt-packages` owns only aggregation, validation, signing, compatibility
installers, and Pages deployment. A small manifest defines the three upstream
repositories, allowed asset patterns, and installer source paths. The feed
publisher downloads the latest releases through GitHub's API and never builds
product source.

## Feed Assembly

The publisher runs hourly and through manual dispatch. Pull requests and
ordinary pushes run fixture-backed validation without deploying.

For a deployable run it:

1. Resolves one latest stable release per product.
2. Downloads only allowlisted `.ipk` asset patterns into temporary storage.
3. Fetches the installer corresponding to each product release.
4. Parses every IPK control record.
5. Rejects malformed packages, unexpected architecture values, missing
   installers, missing products, and duplicate
   `(Package, Version, Architecture)` tuples.
6. Generates deterministic `Packages` and `Packages.gz` indexes.
7. Signs `Packages` with `OPENWRT_FEED_USIGN_PRIVATE_KEY`.
8. Copies the existing `keithah-feed.pub` public key and verifies the generated
   signature before upload.
9. Deploys the complete static directory through GitHub Pages.

No partial or unsigned feed is deployed. A missing signing secret is a hard
failure for scheduled/manual deployment.

## Installer Behavior

All three one-line installers use
`https://keithah.github.io/openwrt-packages` and the feed name `keithah`.
They:

- require root, `opkg`, and `wget`;
- require `aarch64_cortex-a53` in `opkg print-architecture`;
- install the existing publisher public key without disabling global signature
  verification;
- preserve unrelated `/etc/opkg/customfeeds.conf` lines byte-for-byte;
- atomically remove managed `starwatch`, `wattline`, and duplicate `keithah`
  entries and append one neutral `keithah` entry;
- run `opkg update`; and
- install only their own product packages.

The Speedtest README exposes this one-liner:

```sh
wget -qO- https://keithah.github.io/openwrt-packages/install-ookla-speedtest-cli.sh | sh
```

The installer does not accept Ookla's EULA. License acceptance remains the
normal first-run behavior of `/usr/bin/speedtest`.

## Existing-Router Migration

Starwatch's `starwatchd` package and Wattline's `wattlined` package gain an
idempotent upgrade hook that migrates only managed feed entries to the neutral
URL and installs the unchanged publisher key. Tests execute these hooks against
fake router roots and verify preservation of unrelated feeds, permissions, and
ownership where supported.

Cutover is ordered to avoid an outage:

1. Create and test `openwrt-packages` without changing live product URLs.
2. Publish and verify the first Speedtest release.
3. Add the existing private signing key to the new repository as
   `OPENWRT_FEED_USIGN_PRIVATE_KEY`.
4. Deploy the new feed and verify its live index and signature.
5. Update the product installers and migration hooks, bump their package
   versions, and publish new releases.
6. Allow the dedicated feed to ingest those releases.
7. Publish one final signed legacy Starwatch feed snapshot containing the
   migration-enabled Starwatch and Wattline releases.
8. Disable Starwatch's feed schedule and Pages deployment while leaving the
   final legacy snapshot online.

An existing router can therefore discover a migration-enabled upgrade from
the old URL even after the old publisher stops changing. After package upgrade,
the router uses the neutral feed for all subsequent updates.

## Testing and Verification

### Speedtest producer

- Build from local fixture archives in tests without network access.
- Inspect the outer and inner IPK archives and reject pax or ar/deb formats.
- Assert package name, version/release, `aarch64_cortex-a53`, proprietary
  license metadata, and executable mode.
- Assert the only installed payload is `/usr/bin/speedtest`.
- Verify a live build downloads the pinned official archive and passes SHA-256,
  architecture, float-ABI, and no-`PT_INTERP` validation.

### Feed aggregator

- Use fixture IPKs to test deterministic indexing and compression.
- Reject duplicate package tuples, unexpected architectures, malformed
  controls, missing product assets, missing installers, and unsigned output.
- Use a disposable test key to sign and verify fixture feeds.
- Confirm the deployable artifact contains only allowlisted files and every
  indexed filename exists.

### Migration and installers

- Run installers and upgrade hooks against isolated fake roots.
- Verify architecture rejection occurs before mutation.
- Verify the existing key and unrelated feeds are preserved.
- Verify legacy managed entries collapse to one neutral entry.
- Verify repeated runs are idempotent.

### Live cutover

- Verify the new Pages URL returns `Packages`, `Packages.gz`, `Packages.sig`,
  public key, three installers, and every indexed IPK.
- Verify `usign -V` succeeds with the published key.
- Confirm the package index contains all expected Starwatch, Wattline, and
  Speedtest records without duplicates.
- Confirm the old URL's final snapshot contains migration-enabled Starwatch and
  Wattline versions.

## Signing-Key Requirement

GitHub does not expose the value of the existing
`STARWATCH_USIGN_PRIVATE_KEY` secret, so automation cannot copy it. Before live
cutover, the same private key must be provided to `keithah/openwrt-packages` as
`OPENWRT_FEED_USIGN_PRIVATE_KEY`. The public key and fingerprint remain
unchanged, preventing trust-key churn on installed routers.

## Success Criteria

- `keithah/openwrt-packages` is public and owns the shared GitHub Pages feed.
- The live feed is signed by the existing publisher identity and verifies
  before deployment.
- Starwatch, Wattline, and Speedtest remain independent product repositories.
- The feed contains the latest valid IPKs and installers for all three.
- A supported router can install Speedtest with the documented one-liner.
- Existing Starwatch/Wattline routers migrate automatically from the old feed
  during package upgrade without losing unrelated feed configuration.
- The legacy feed remains a static migration bridge, while no product
  repository continues active shared-feed publication.
