# Product Feed Artifacts and Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Speedtest a release-IPK producer and make Starwatch and Wattline automatically migrate installed routers to the dedicated Keith feed.

**Architecture:** Each product continues to own only its package artifacts and installer. Speedtest builds one deterministic `aarch64_cortex-a53` IPK from its pinned official archive; Starwatch and Wattline add an idempotent neutral-feed migration helper to their daemon packages.

**Tech Stack:** Python 3 standard library, POSIX shell, GNU Make/tar, GitHub Actions, opkg/usign.

## Global Constraints

- Product repositories must not check out, build, or name another product.
- The feed URL is exactly `https://keithah.github.io/openwrt-packages` and its opkg name is `keithah`.
- Preserve unrelated `customfeeds.conf` entries byte-for-byte.
- Keep global opkg signature checking enabled and retain public-key fingerprint `f6c72c675c844b91`.
- Speedtest supports `aarch64_cortex-a53` in the shared feed and uses Ookla's `linux-aarch64` binary.
- No Ookla archive or binary may be committed.
- The local Wattline checkout at `/home/keith/src/openwrt-wattline` is user-owned and must not be modified; use an isolated clean checkout of `origin/main`.

---

### Task 1: Deterministic Speedtest IPK builder

**Repository:** `/home/keith/src/openwrt-ookla-speedtest`

**Files:**
- Create: `scripts/build_ipk.py`
- Create: `tests/test_build_ipk.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `Makefile`, official or fixture archive bytes, and existing `validate_archive()`/`archive_sha256()`.
- Produces: `build_ipk(makefile: Path, output_dir: Path, archive: bytes | None = None) -> Path` and `ookla-speedtest-cli_VERSION-1_aarch64_cortex-a53.ipk`.

- [ ] Write failing tests that build from an in-memory aarch64 fixture archive and inspect the IPK as nested gzip-compressed ustar archives. Assert outer members `./debian-binary`, `./control.tar.gz`, and `./data.tar.gz`; exact control fields `Package: ookla-speedtest-cli`, `Version: 1.2.0-1`, `Architecture: aarch64_cortex-a53`, `License: Proprietary`; and one executable payload `./usr/bin/speedtest` with mode `0755`.
- [ ] Add rejection tests for a checksum mismatch, non-aarch64 ELF, `PT_INTERP`, pax members, and unexpected source-archive members replacing `speedtest`.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_build_ipk` and confirm failure because `scripts.build_ipk` is absent.
- [ ] Implement deterministic gzip streams with `mtime=0` and `tarfile.USTAR_FORMAT`. Parse `PKG_VERSION`, `PKG_RELEASE`, and `OOKLA_HASH_aarch64` exactly once from `Makefile`; download through the existing identified `_download()` only when archive bytes are not injected; require the pinned checksum and `validate_archive("aarch64", archive)` before reading `speedtest`.
- [ ] Ensure temporary staging uses `tempfile.TemporaryDirectory`, output replacement is atomic, and no archive is written under the repository.
- [ ] Run the focused test and `python3 -m unittest discover -s tests -v`; expect all tests to pass with pristine output.
- [ ] Commit `scripts/build_ipk.py`, `tests/test_build_ipk.py`, and `.gitignore` as `feat: build Speedtest OpenWrt release package`.

### Task 2: Speedtest signed-feed installer

**Repository:** `/home/keith/src/openwrt-ookla-speedtest`

**Files:**
- Create: `scripts/install.sh`
- Create: `tests/test_install.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: root environment variables `OOKLA_ROOT` and optional `OOKLA_FEED_URL` for isolated tests.
- Produces: idempotent feed/key setup followed by `opkg update` and `opkg install ookla-speedtest-cli`.

- [ ] Write a shell test harness with fake `id`, `opkg`, and `wget` commands plus a fake root. Assert architecture rejection precedes mutation; managed `starwatch`, `wattline`, and duplicate `keithah` entries collapse to one neutral entry; unrelated lines and mode survive; the exact public key is installed; repeated execution is idempotent; and only `ookla-speedtest-cli` is installed.
- [ ] Run `sh tests/test_install.sh`; expect failure because `scripts/install.sh` is absent.
- [ ] Implement the installer using the proven atomic `mktemp`/`awk`/`mv` pattern from the product installers, but keep all names and output Speedtest-specific. Do not accept the Ookla EULA.
- [ ] Add the exact one-liner `wget -qO- https://keithah.github.io/openwrt-packages/install-ookla-speedtest-cli.sh | sh` to `README.md`, explain `aarch64_cortex-a53`, signature verification, and first-run EULA behavior.
- [ ] Run `sh tests/test_install.sh` and the full Python suite; expect all tests to pass.
- [ ] Commit as `feat: add signed-feed Speedtest installer`.

### Task 3: Speedtest automated release assets

**Repository:** `/home/keith/src/openwrt-ookla-speedtest`

**Files:**
- Create: `.github/workflows/release.yml`
- Modify: `tests/test_recipe.py`

**Interfaces:**
- Consumes: pushes to `main` that change `Makefile` or the package builder and manual dispatch.
- Produces: GitHub release `vPKG_VERSION` containing exactly the IPK and `install-ookla-speedtest-cli.sh`.

- [ ] Add a failing static workflow-policy test requiring `contents: write`, full tests before build, live builder invocation, exact inventory validation, idempotent existing-release handling, and no archive upload.
- [ ] Run `python3 -m unittest -v tests.test_recipe`; expect the workflow assertion to fail.
- [ ] Create the workflow. Derive `VERSION` from the one anchored Makefile assignment, build into `dist/`, copy the installer to `dist/install-ookla-speedtest-cli.sh`, require exactly two files, and create `v$VERSION` only when absent. If the release already exists, verify both expected assets rather than overwriting it.
- [ ] Run the focused and full suites and validate YAML parsing with Ruby's standard YAML parser or an available preinstalled parser without adding a dependency.
- [ ] Commit as `ci: publish Speedtest package releases`.

### Task 4: Reusable neutral-feed migration contract

**Repositories:** clean Starwatch `main` and an isolated Wattline checkout of `origin/main`.

**Files in each repository:**
- Create: `package/keithah-feed-migrate.sh`
- Create or modify: package migration tests
- Modify: `package/install.sh`
- Modify: daemon `CONTROL/postinst`
- Modify: `package/Makefile`

**Interfaces:**
- Consumes: `KEITHAH_ROOT` defaulting to `/` for tests and the unchanged public key.
- Produces: `/usr/libexec/keithah-feed-migrate`, which performs idempotent atomic migration and exits nonzero before mutation on an unsupported architecture.

- [ ] In each repository, write failing fake-root tests for architecture rejection, unrelated-line preservation, legacy-entry collapse, existing key replacement, file-mode preservation, and idempotence. Add a postinst contract assertion proving upgrades invoke the installed helper.
- [ ] Run each existing package test suite and confirm only the new migration assertions fail.
- [ ] Implement the same publisher-neutral helper independently in each product repository. Install it at `/usr/libexec/keithah-feed-migrate` from the daemon package. `starwatchd` and `wattlined` postinst scripts invoke it only on the live root before normal service initialization.
- [ ] Change installer defaults from `https://keithah.github.io/openwrt-starwatch` to `https://keithah.github.io/openwrt-packages`; retain product-specific override variables and package choices.
- [ ] Run all Starwatch package tests and all Wattline package tests in their respective clean checkouts.
- [ ] Commit Starwatch as `feat(package): migrate routers to dedicated feed` and Wattline independently with the same subject.

### Task 5: Product release readiness

**Repositories:** Starwatch and the isolated Wattline checkout.

**Files:**
- Modify: Starwatch `package/Makefile`, `.github/workflows/pages-feed.yml`, and `README.md`.
- Modify: Wattline `package/Makefile`, `.github/workflows/release.yml`, and `README.md`.
- Create: Starwatch `.github/workflows/release.yml` if no release-only workflow exists.

- [ ] Bump Starwatch package version from `0.1.2` to `0.1.3` and Wattline from `0.1.3` to `0.1.4`; ensure filename and control metadata use the override version consistently.
- [ ] Make each tagged release attach its product IPKs and no shared `Packages` index. Installer source remains addressable at the matching tag for the aggregator.
- [ ] Keep Starwatch's existing Pages workflow temporarily intact for the final legacy snapshot; do not disable it in this task.
- [ ] Update product READMEs to the neutral installer URL and describe automatic upgrade migration.
- [ ] Run full tests, build release inventories for `0.1.3` and `0.1.4`, and inspect every IPK control record.
- [ ] Commit each repository independently as `release: prepare dedicated feed migration`.

