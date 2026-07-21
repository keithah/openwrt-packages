# Signed Feed Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the new signed feed, release migration-enabled product packages, preserve the legacy URL as a static bridge, and retire product-owned active feed deployment without an outage.

**Architecture:** Cutover is gated by signature verification at the new URL. Product releases migrate installed routers; a final legacy snapshot advertises those releases forever; only then is Starwatch's scheduled publisher removed.

**Tech Stack:** Git, GitHub CLI/API, GitHub Actions/Pages, curl, opkg index parsing, usign.

## Global Constraints

- Never cut product installers or package hooks to the new URL before the new signed feed verifies live.
- Never expose or print either private signing key.
- Do not modify the user-owned Wattline checkout at `/home/keith/src/openwrt-wattline`.
- Do not delete or disable the legacy Starwatch Pages site; leave its final migration snapshot online.
- Do not force-push or overwrite an existing release/tag.

---

### Task 1: Publish product code and create Speedtest release

- [ ] Verify all product-repository tests from clean intended commits.
- [ ] Push Speedtest producer commits to `main`; observe its test workflow.
- [ ] Trigger/observe Speedtest release `v1.2.0`; verify exactly `ookla-speedtest-cli_1.2.0-1_aarch64_cortex-a53.ipk` and `install-ookla-speedtest-cli.sh`; independently inspect IPK control/payload and compare embedded binary hash/ELF metadata to the official archive.
- [ ] Push Starwatch and Wattline migration-preparation commits to their `main` branches without tagging them yet.

### Task 2: Create the dedicated repository and configure Pages

- [ ] Confirm `keithah/openwrt-packages` does not already exist, then create it public from `/home/keith/src/openwrt-packages`, push `main`, and configure GitHub Pages for Actions.
- [ ] Verify test workflow success, public visibility, default branch `main`, and no package artifacts in Git history.
- [ ] Ask the user for the local existing usign private-key path, assign that exact path to `OPENWRT_FEED_KEY_FILE`, then run `gh secret set OPENWRT_FEED_USIGN_PRIVATE_KEY -R keithah/openwrt-packages < "$OPENWRT_FEED_KEY_FILE"` without displaying content.
- [ ] Confirm only the secret name/timestamp through `gh secret list`; never attempt to read it.

### Task 3: Deploy and verify the neutral feed before migration releases

- [ ] Manually dispatch the Pages workflow and monitor it to completion.
- [ ] Download live `Packages`, `Packages.gz`, `Packages.sig`, and `keithah-feed.pub`; verify gzip equality and `usign -V` with the published key.
- [ ] Verify all current Starwatch/Wattline IPKs plus Speedtest and the three installers are indexed/present without duplicate tuples.
- [ ] Run each installer in its fake-root integration test with the live URL override; do not install on a real router without explicit authorization.

### Task 4: Publish migration releases and refresh both feeds

- [ ] Create annotated Starwatch tag `v0.1.3` and Wattline tag `v0.1.4` only after confirming those tags/releases are absent; push each tag and monitor release workflows.
- [ ] Inspect release IPKs and verify migration helper inclusion plus version/control consistency.
- [ ] Manually dispatch `openwrt-packages` Pages again; verify the live index now references Starwatch 0.1.3 and Wattline 0.1.4.
- [ ] Manually dispatch Starwatch's legacy Pages workflow one final time; verify its old live index also advertises the migration-enabled releases and its signature verifies with the same public key.

### Task 5: Retire active legacy publishing

- [ ] Remove `.github/workflows/pages-feed.yml` from Starwatch, update README text to identify the old URL as a static migration bridge, run full tests, and commit `ci: retire product-owned feed publisher`.
- [ ] Push Starwatch `main` and verify removing the workflow did not remove the existing Pages deployment.
- [ ] Confirm no Wattline Pages workflow remains active; preserve any existing unrelated legacy Pages content unless explicitly authorized otherwise.
- [ ] Verify final GitHub state: dedicated hourly workflow enabled, old Starwatch workflow absent, new and old Pages URLs available, new signature valid, final old snapshot valid, three neutral installers available, and all product repositories independent.
- [ ] Record exact URLs, release tags, workflow runs, signature fingerprint, and any follow-up operational recommendation in the final handoff.
