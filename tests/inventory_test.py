#!/usr/bin/env python3
"""Validate the complete static Pages artifact or run validator self-tests."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
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


def _manifest_installers(manifest_path: Path) -> set[str]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        installers = {source["installer"] for source in manifest["sources"]}
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InventoryError(f"invalid manifest: {exc}") from exc
    if installers != {"install-starwatch.sh", "install-wattline.sh", "install-ookla-speedtest-cli.sh"}:
        raise InventoryError("manifest must name exactly three expected installers")
    return installers


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
    filenames: set[str] = set()
    tuples: set[tuple[str, str, str]] = set()
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
        if not re.fullmatch(r"0|[1-9][0-9]*", record["size"]):
            raise InventoryError("invalid indexed size")
        if not re.fullmatch(r"[0-9a-f]{64}", record["sha256sum"]):
            raise InventoryError("invalid indexed SHA256sum")
        size = int(record["size"])
        data = artifact.read_bytes()
        if size != len(data) or record["sha256sum"] != hashlib.sha256(data).hexdigest():
            raise InventoryError(f"indexed metadata mismatch: {filename}")
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
    installers = _manifest_installers(manifest_path)
    for name in installers:
        if (pages / name).stat().st_mode & 0o777 != 0o755:
            raise InventoryError(f"installer mode is not 0755: {name}")
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
        ipk = self.pages / "example_1-1_all.ipk"
        ipk.write_bytes(b"fixture ipk")
        digest = hashlib.sha256(ipk.read_bytes()).hexdigest()
        packages = (f"Package: example\nVersion: 1-1\nArchitecture: all\nFilename: {ipk.name}\n"
                    f"Size: {ipk.stat().st_size}\nSHA256sum: {digest}\n\n").encode()
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
        (self.pages / "example_1-1_all.ipk").write_bytes(b"tampered")
        with self.assertRaisesRegex(InventoryError, "metadata"):
            validate(self.pages)
        (self.pages / "example_1-1_all.ipk").write_bytes(b"fixture ipk")
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
        payload = (self.pages / "Packages").read_text().replace("Size: 11\n", "Size:  11\n").encode()
        (self.pages / "Packages").write_bytes(payload)
        with (self.pages / "Packages.gz").open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
                stream.write(payload)
        with self.assertRaisesRegex(InventoryError, "size"):
            validate(self.pages)


def main() -> int:
    if len(sys.argv) == 2:
        try:
            validate(Path(sys.argv[1]))
        except (InventoryError, OSError) as exc:
            print(f"inventory validation failed: {exc}", file=sys.stderr)
            return 1
        print("feed inventory is valid")
        return 0
    return 0 if unittest.main(argv=[sys.argv[0]], exit=False).result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
