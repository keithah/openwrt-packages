# Dedicated Feed Publisher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `keithah/openwrt-packages` as a deterministic, fail-closed, signed GitHub Pages aggregator for Starwatch, Wattline, and Speedtest release artifacts.

**Architecture:** A manifest defines allowlisted product release assets and installer paths. Standard-library Python validates IPKs and builds the index; a POSIX fetch script retrieves immutable tagged artifacts; GitHub Actions signs, verifies, and deploys only scheduled/manual runs.

**Tech Stack:** Python 3 standard library, POSIX shell, GitHub CLI/API, OpenWrt usign, GitHub Actions and Pages.

## Global Constraints

- The public repository is exactly `keithah/openwrt-packages`.
- The live URL is exactly `https://keithah.github.io/openwrt-packages`.
- Only `aarch64_cortex-a53` and `all` IPKs are accepted.
- Every product must contribute at least one IPK and exactly one installer.
- Duplicate `(Package, Version, Architecture)` tuples are fatal.
- Scheduled/manual deployment requires `OPENWRT_FEED_USIGN_PRIVATE_KEY`; unsigned output is never deployed.
- `keithah-feed.pub` and fingerprint `f6c72c675c844b91` remain unchanged.

---

### Task 1: Manifest and deterministic feed assembler

**Files:**
- Create: `sources.json`
- Create: `scripts/assemble_feed.py`
- Create: `tests/test_assemble_feed.py`
- Create: `tests/fixtures/` generated at test runtime

**Interfaces:**
- Consumes: `downloads/<product>/*.ipk`, `downloads/<product>/install-*.sh`, and `sources.json`.
- Produces: `assemble(download_root: Path, output: Path, manifest: dict) -> list[PackageRecord]`, plus deterministic `Packages` and `Packages.gz`.

- [ ] Write failing tests that generate minimal gzip-ustar IPKs in temporary directories for Starwatch, Wattline, and Speedtest. Assert deterministic sorted records, exact filename/size/SHA256 fields, gzip `mtime=0`, installer copying, and allowlisted output inventory.
- [ ] Add rejection tests for malformed outer/control archives, pax/ar inputs, absent products, absent/multiple installers, unexpected architecture, unexpected filename pattern, duplicate tuples, duplicate filenames, and indexed files missing from output.
- [ ] Run `python3 -m unittest -v tests.test_assemble_feed`; expect import failure.
- [ ] Implement dataclasses `SourceSpec` and `PackageRecord`, strict JSON loading, ustar control parsing, Debian-control continuation preservation, stable sorting by package/version/architecture/filename, deterministic gzip, and atomic output-directory replacement.
- [ ] Define manifest entries for `keithah/openwrt-starwatch`, `keithah/openwrt-wattline`, and `keithah/openwrt-ookla-speedtest-cli` with exact IPK regexes and installer paths.
- [ ] Run focused/full tests and commit as `feat: assemble shared OpenWrt feed`.

### Task 2: Immutable latest-release fetcher

**Files:**
- Create: `scripts/fetch_releases.py`
- Create: `tests/test_fetch_releases.py`

**Interfaces:**
- Consumes: GitHub REST responses through injectable `urlopen`, repository manifest entries, and `GH_TOKEN`.
- Produces: `fetch_all(manifest, destination) -> dict[str, str]` mapping product to immutable tag.

- [ ] Write failing tests with local JSON responses/assets. Require latest non-draft/non-prerelease release, exact allowlisted asset matches, checksum-preserving downloads, installer retrieval from the same tag's Git tree, no redirects to non-GitHub HTTPS hosts, response-size bounds, and atomic per-product directories.
- [ ] Run focused tests and confirm import failure.
- [ ] Implement standard-library authenticated GitHub API requests with an explicit User-Agent and 30-second timeout. Resolve the immutable release tag first, download only release-asset API URLs, retrieve installer content using that resolved tag as the `ref` query value, and fail on missing/extra matches.
- [ ] Run focused/full tests and commit as `feat: fetch immutable product releases`.

### Task 3: Signing and deployable artifact validation

**Files:**
- Create: `scripts/sign_feed.sh`
- Copy: `keithah-feed.pub` from Starwatch's published key
- Create: `tests/sign_feed_test.sh`
- Create: `tests/inventory_test.py`

**Interfaces:**
- Consumes: assembled Pages directory, `OPENWRT_FEED_USIGN_PRIVATE_KEY`, and `USIGN` command path.
- Produces: verified `Packages.sig`, `keithah-feed.pub`, and `.nojekyll`.

- [ ] Write a shell test with a disposable fake signer/verifier and Python inventory tests. Require missing-key failure, signing `Packages` rather than compressed data, verification before success, no stale signature reuse, and an exact output allowlist.
- [ ] Run tests and confirm failure because signing script is absent.
- [ ] Implement fail-closed signing: write the secret to a `0600` temporary file outside output, remove any old signature, call `usign -S`, copy public key, call `usign -V`, add `.nojekyll`, and remove the secret file through a trap.
- [ ] Run all tests and commit as `feat: sign and verify feed artifacts`.

### Task 4: CI, Pages deployment, and documentation

**Files:**
- Create: `.github/workflows/test.yml`
- Create: `.github/workflows/pages.yml`
- Create: `README.md`
- Create: `.gitignore`

- [ ] Add static policy tests requiring read-only PR/push tests; hourly/manual deploy triggers; `pages: write` and `id-token: write` only on deploy; concurrency; pinned usign source commit `c4c72b1b07945ee192361dc751291a7c98d6adcd`; secret presence check; fetch, assemble, sign, verify, inventory, upload, and deploy ordering.
- [ ] Run the policy test and confirm workflow absence failure.
- [ ] Implement test and Pages workflows. Deploy runs only for schedule or manual dispatch, builds usign from the pinned commit, and never uploads on a pull request or ordinary push.
- [ ] Document package inventory, one-line installers, signature fingerprint, source-repository boundaries, release freshness, and recovery from a failed upstream release.
- [ ] Ignore `downloads/`, `pages/`, Python caches, IPKs, archives, and secret-key extensions.
- [ ] Run full Python/shell tests, parse YAML, inspect tracked files for package/key artifacts, and commit as `ci: publish signed OpenWrt package feed`.
