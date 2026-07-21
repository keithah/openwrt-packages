#!/usr/bin/env python3
"""Validate the complete static Pages artifact or run validator self-tests."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.assemble_feed import FeedError, MAX_INSTALLER_SIZE, _read_ipk, load_manifest  # noqa: E402
from tests.test_assemble_feed import make_ipk  # noqa: E402

EXPECTED_FINGERPRINT = "f6c72c675c844b91"
GENERATED = {"Packages", "Packages.gz", "Packages.sig", "keithah-feed.pub", ".nojekyll"}
REQUIRED_FIELDS = {"package", "version", "architecture", "filename", "size", "sha256sum"}


class InventoryError(ValueError):
    pass


def _records(payload: bytes) -> list[dict[str, str]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InventoryError("Packages is not UTF-8") from exc
    paragraphs = text.rstrip("\n").split("\n\n") if text else []
    if not paragraphs or not text.endswith("\n\n"):
        raise InventoryError("Packages must contain terminated records")
    records = []
    for paragraph in paragraphs:
        fields: dict[str, str] = {}
        previous = None
        for line in paragraph.splitlines():
            if line.startswith((" ", "\t")):
                if previous is None:
                    raise InventoryError("invalid continuation")
                fields[previous] += "\n" + line
                continue
            if ":" not in line:
                raise InventoryError("invalid Packages field")
            name, value = line.split(":", 1)
            key = name.lower()
            if not name or key in fields:
                raise InventoryError("duplicate Packages field")
            fields[key] = value[1:] if value.startswith(" ") else value
            previous = key
        if not REQUIRED_FIELDS <= fields.keys():
            raise InventoryError("Packages record is missing required fields")
        records.append(fields)
    return records


def _manifest_sources(manifest_path: Path) -> list[dict]:
    try:
        manifest = load_manifest(manifest_path)
        sources = manifest["sources"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, FeedError) as exc:
        raise InventoryError(f"invalid manifest: {exc}") from exc
    installers = {source["installer"] for source in sources}
    if installers != {"install-starwatch.sh", "install-wattline.sh", "install-ookla-speedtest-cli.sh"}:
        raise InventoryError("manifest must name exactly three expected installers")
    return sources


def validate(pages: Path, manifest_path: Path = ROOT / "sources.json",
             public_key_path: Path = ROOT / "keithah-feed.pub") -> None:
    pages = Path(pages)
    if pages.is_symlink() or not pages.is_dir():
        raise InventoryError("Pages path must be a real directory")
    entries = list(pages.iterdir())
    for entry in entries:
        mode = entry.lstat().st_mode
        if not stat.S_ISREG(mode) or entry.is_symlink():
            raise InventoryError(f"unsafe artifact type: {entry.name}")
    packages = (pages / "Packages").read_bytes()
    records = _records(packages)
    sources = _manifest_sources(manifest_path)
    installers = {source["installer"] for source in sources}
    product_counts = {source["product"]: 0 for source in sources}
    filenames: set[str] = set()
    tuples: set[tuple[str, str, str]] = set()
    actual_records = []
    for record in records:
        filename = record["filename"]
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._~:-]*\.ipk", filename)
                or Path(filename).name != filename):
            raise InventoryError(f"unsafe indexed filename: {filename}")
        if (not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", record["package"])
                or not re.fullmatch(r"[A-Za-z0-9.+~:-]+", record["version"])):
            raise InventoryError("invalid indexed package metadata")
        if record["architecture"] not in {"all", "aarch64_cortex-a53"}:
            raise InventoryError("unexpected indexed architecture")
        key = (record["package"], record["version"], record["architecture"])
        if filename in filenames or key in tuples:
            raise InventoryError("duplicate indexed package")
        filenames.add(filename)
        tuples.add(key)
        artifact = pages / filename
        matches = [(source, re.fullmatch(source["ipk_pattern"], filename)) for source in sources]
        matches = [(source, match) for source, match in matches if match is not None]
        if len(matches) != 1:
            raise InventoryError(f"indexed filename is outside manifest allowlist: {filename}")
        source, match = matches[0]
        if (record["package"] not in source["packages"]
                or record["package"] != match.group("package")
                or record["version"] != match.group("version")
                or record["architecture"] != match.group("architecture")):
            raise InventoryError(f"indexed metadata violates manifest for {filename}")
        product_counts[source["product"]] += 1
        if not re.fullmatch(r"0|[1-9][0-9]*", record["size"]):
            raise InventoryError("invalid indexed size")
        if not re.fullmatch(r"[0-9a-f]{64}", record["sha256sum"]):
            raise InventoryError("invalid indexed SHA256sum")
        size = int(record["size"])
        try:
            actual = _read_ipk(artifact)
        except FeedError as exc:
            raise InventoryError(f"invalid indexed IPK {filename}: {exc}") from exc
        if ((actual.package, actual.version, actual.architecture, actual.filename,
             actual.size, actual.sha256)
                != (record["package"], record["version"], record["architecture"],
                    filename, size, record["sha256sum"])):
            raise InventoryError(f"IPK control tuple differs from Packages: {filename}")
        generated = {"filename", "size", "sha256sum"}
        indexed_control = {name: value for name, value in record.items() if name not in generated}
        actual_control = {name.lower(): value for name, value in actual.control}
        if indexed_control != actual_control:
            raise InventoryError(f"IPK control fields differ from Packages: {filename}")
        actual_records.append(actual)
    missing_products = sorted(product for product, count in product_counts.items() if count == 0)
    if missing_products:
        raise InventoryError(f"missing product packages: {', '.join(missing_products)}")
    sorted_records = sorted(actual_records, key=lambda record: (
        record.package, record.version, record.architecture, record.filename))
    if actual_records != sorted_records:
        raise InventoryError("Packages records are not canonically sorted")
    canonical_packages = "".join(record.render() for record in actual_records).encode("utf-8")
    if packages != canonical_packages:
        raise InventoryError("Packages fields are not canonical")
    compressed = (pages / "Packages.gz").read_bytes()
    if len(compressed) < 10 or compressed[4:8] != b"\0\0\0\0":
        raise InventoryError("Packages.gz is not deterministic")
    try:
        if gzip.decompress(compressed) != packages:
            raise InventoryError("Packages.gz does not match Packages")
    except (gzip.BadGzipFile, EOFError) as exc:
        raise InventoryError("Packages.gz is invalid") from exc
    expected_gzip = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=expected_gzip, mtime=0) as stream:
        stream.write(packages)
    if compressed != expected_gzip.getvalue():
        raise InventoryError("Packages.gz bytes are not canonical")
    for name in installers:
        installer = pages / name
        installer_stat = installer.stat()
        if stat.S_IMODE(installer_stat.st_mode) != 0o755:
            raise InventoryError(f"installer mode is not 0755: {name}")
        if not 0 < installer_stat.st_size <= MAX_INSTALLER_SIZE:
            raise InventoryError(f"installer size is invalid: {name}")
    expected_public = public_key_path.read_bytes()
    published_public = (pages / "keithah-feed.pub").read_bytes()
    if published_public != expected_public:
        raise InventoryError("published public key differs from pinned key")
    try:
        lines = published_public.decode("ascii").splitlines()
        decoded = base64.b64decode(lines[1], validate=True)
    except (UnicodeError, IndexError, ValueError) as exc:
        raise InventoryError("invalid published public key") from exc
    if len(lines) != 2 or len(decoded) != 42 or decoded[:2] != b"Ed" or decoded[2:10].hex() != EXPECTED_FINGERPRINT:
        raise InventoryError("unexpected public key fingerprint")
    if not (pages / "Packages.sig").read_bytes():
        raise InventoryError("empty package signature")
    if (pages / ".nojekyll").read_bytes():
        raise InventoryError(".nojekyll must be empty")
    expected = GENERATED | installers | filenames
    actual = {entry.name for entry in entries}
    if actual != expected:
        raise InventoryError(f"artifact inventory differs: expected {sorted(expected)}, got {sorted(actual)}")


class InventoryValidatorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pages = Path(self.temp.name) / "pages"
        self.pages.mkdir()
        installers = ("install-starwatch.sh", "install-wattline.sh", "install-ookla-speedtest-cli.sh")
        for name in installers:
            path = self.pages / name
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o755)
        package_records = []
        fixtures = (
            ("starwatchd_1.2.3_aarch64_cortex-a53.ipk", "starwatchd", "1.2.3", "aarch64_cortex-a53"),
            ("wattline-bt_2.0.0_all.ipk", "wattline-bt", "2.0.0", "all"),
            ("ookla-speedtest-cli_1.2.0-1_aarch64_cortex-a53.ipk", "ookla-speedtest-cli", "1.2.0-1", "aarch64_cortex-a53"),
        )
        for filename, package, version, arch in fixtures:
            ipk = self.pages / filename
            make_ipk(ipk, package, version=version, arch=arch)
            package_records.append(_read_ipk(ipk))
        package_records.sort(key=lambda record: (
            record.package, record.version, record.architecture, record.filename))
        packages = "".join(record.render() for record in package_records).encode()
        (self.pages / "Packages").write_bytes(packages)
        with (self.pages / "Packages.gz").open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
                stream.write(packages)
        (self.pages / "Packages.sig").write_bytes(b"signature\n")
        (self.pages / "keithah-feed.pub").write_bytes((ROOT / "keithah-feed.pub").read_bytes())
        (self.pages / ".nojekyll").touch()

    def test_accepts_exact_deployable_inventory(self):
        validate(self.pages)

    def test_rejects_extra_missing_tampered_and_unsafe_artifacts(self):
        (self.pages / "secret.key").write_text("secret")
        with self.assertRaisesRegex(InventoryError, "inventory"):
            validate(self.pages)
        (self.pages / "secret.key").unlink()
        target = self.pages / "starwatchd_1.2.3_aarch64_cortex-a53.ipk"
        original = target.read_bytes()
        target.write_bytes(b"tampered")
        with self.assertRaisesRegex(InventoryError, "metadata|invalid indexed IPK"):
            validate(self.pages)
        target.write_bytes(original)
        (self.pages / "install-starwatch.sh").chmod(0o644)
        with self.assertRaisesRegex(InventoryError, "0755"):
            validate(self.pages)
        (self.pages / "install-starwatch.sh").chmod(0o755)
        (self.pages / "directory").mkdir()
        with self.assertRaisesRegex(InventoryError, "unsafe artifact"):
            validate(self.pages)

    def test_rejects_noncanonical_gzip_and_wrong_public_key(self):
        payload = (self.pages / "Packages").read_bytes()
        (self.pages / "Packages.gz").write_bytes(gzip.compress(payload, mtime=1))
        with self.assertRaisesRegex(InventoryError, "deterministic"):
            validate(self.pages)
        with (self.pages / "Packages.gz").open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
                stream.write(payload)
        (self.pages / "keithah-feed.pub").write_text("wrong\n")
        with self.assertRaisesRegex(InventoryError, "pinned"):
            validate(self.pages)

    def test_rejects_noncanonical_generated_index_fields(self):
        text = (self.pages / "Packages").read_text()
        payload = text.replace("Size: ", "Size:  ", 1).encode()
        (self.pages / "Packages").write_bytes(payload)
        with (self.pages / "Packages.gz").open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
                stream.write(payload)
        with self.assertRaisesRegex(InventoryError, "size"):
            validate(self.pages)

    def test_rejects_noncanonical_record_order(self):
        paragraphs = [p + b"\n\n" for p in (self.pages / "Packages").read_bytes().split(b"\n\n") if p]
        self._rewrite_index(list(reversed(paragraphs)))
        with self.assertRaisesRegex(InventoryError, "canonical|sorted"):
            validate(self.pages)

    def _rewrite_index(self, records: list[bytes]) -> None:
        payload = b"".join(records)
        (self.pages / "Packages").write_bytes(payload)
        with (self.pages / "Packages.gz").open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
                stream.write(payload)

    def test_rejects_manifest_violations_malformed_ipks_and_missing_products(self):
        evil = self.pages / "evil_1-1_all.ipk"
        make_ipk(evil, "evil", version="1-1", arch="all")
        self._rewrite_index([(self.pages / "Packages").read_bytes(), _read_ipk(evil).render().encode()])
        with self.assertRaisesRegex(InventoryError, "manifest|allowlist|filename"):
            validate(self.pages)

        evil.unlink()
        speedtest = self.pages / "ookla-speedtest-cli_1.2.0-1_aarch64_cortex-a53.ipk"
        speedtest.write_bytes(b"not an IPK")
        paragraphs = (self.pages / "Packages").read_bytes().split(b"\n\n")
        kept = [p + b"\n\n" for p in paragraphs if p and b"Filename: evil_" not in p]
        speedtest_index = next(i for i, paragraph in enumerate(kept)
                               if b"Filename: ookla-speedtest-cli_" in paragraph)
        malformed = kept[speedtest_index].decode()
        malformed = re.sub(r"Size: [0-9]+", f"Size: {speedtest.stat().st_size}", malformed)
        malformed = re.sub(r"SHA256sum: [0-9a-f]+", f"SHA256sum: {hashlib.sha256(speedtest.read_bytes()).hexdigest()}", malformed)
        kept[speedtest_index] = malformed.encode()
        self._rewrite_index(kept)
        with self.assertRaisesRegex(InventoryError, "gzip|IPK|archive"):
            validate(self.pages)

        # Restore a valid feed, then remove every Speedtest record and artifact.
        make_ipk(speedtest, "ookla-speedtest-cli", version="1.2.0-1", arch="aarch64_cortex-a53")
        kept = [p + b"\n\n" for p in (self.pages / "Packages").read_bytes().split(b"\n\n")
                if p and b"Package: ookla-speedtest-cli\n" not in p]
        self._rewrite_index(kept)
        speedtest.unlink()
        with self.assertRaisesRegex(InventoryError, "missing product|speedtest"):
            validate(self.pages)

    def test_rejects_empty_oversize_or_special_mode_installer_and_fifo(self):
        installer = self.pages / "install-starwatch.sh"
        installer.write_bytes(b"")
        with self.assertRaisesRegex(InventoryError, "installer"):
            validate(self.pages)
        installer.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(InventoryError, "installer"):
            validate(self.pages)
        installer.write_text("#!/bin/sh\n")
        installer.chmod(0o4755)
        with self.assertRaisesRegex(InventoryError, "0755"):
            validate(self.pages)
        installer.chmod(0o755)
        fifo = self.pages / "device"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(InventoryError, "unsafe artifact"):
            validate(self.pages)


def main() -> int:
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser()
        parser.add_argument("pages", type=Path)
        parser.add_argument("--manifest", type=Path, default=ROOT / "sources.json")
        parser.add_argument("--public-key", type=Path, default=ROOT / "keithah-feed.pub")
        args = parser.parse_args()
        try:
            validate(args.pages, args.manifest, args.public_key)
        except (InventoryError, OSError) as exc:
            print(f"inventory validation failed: {exc}", file=sys.stderr)
            return 1
        print("feed inventory is valid")
        return 0
    return 0 if unittest.main(argv=[sys.argv[0]], exit=False).result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
