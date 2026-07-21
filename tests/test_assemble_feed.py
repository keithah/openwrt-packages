import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest

from scripts.assemble_feed import FeedError, assemble, load_manifest


ROOT = Path(__file__).resolve().parents[1]


def _tar(members, *, fmt=tarfile.USTAR_FORMAT):
    raw = io.BytesIO()
    kwargs = {"pax_headers": {"comment": "must reject"}} if fmt == tarfile.PAX_FORMAT else {}
    with tarfile.open(fileobj=raw, mode="w", format=fmt, **kwargs) as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            if kind == "file":
                archive.addfile(info, io.BytesIO(data))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                info.size = 0
                archive.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "control"
                info.size = 0
                archive.addfile(info)
    return raw.getvalue()


def make_ipk(path, package, version="1.0-1", arch="all", *,
             outer_format=tarfile.USTAR_FORMAT, control_format=tarfile.USTAR_FORMAT,
             control_name="./control", control_override=None):
    control = control_override or (
        f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\n"
        "Description: first line\n second line\n"
    ).encode()
    control_tar = _tar([(control_name, control, "file")], fmt=control_format)
    control_gz = gzip.compress(control_tar, mtime=0)
    data_gz = gzip.compress(_tar([], fmt=tarfile.USTAR_FORMAT), mtime=0)
    outer = _tar([
        ("./debian-binary", b"2.0\n", "file"),
        ("./control.tar.gz", control_gz, "file"),
        ("./data.tar.gz", data_gz, "file"),
    ], fmt=outer_format)
    path.write_bytes(gzip.compress(outer, mtime=0))


class AssembleFeedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.downloads = self.base / "downloads"
        self.output = self.base / "pages"
        self.manifest = load_manifest(ROOT / "sources.json")
        fixtures = {
            "starwatch": ("starwatchd_1.2.3_aarch64_cortex-a53.ipk", "starwatchd", "aarch64_cortex-a53", "install-starwatch.sh"),
            "wattline": ("wattline-bt_2.0.0_all.ipk", "wattline-bt", "all", "install-wattline.sh"),
            "speedtest": ("ookla-speedtest-cli_1.2.0-1_aarch64_cortex-a53.ipk", "ookla-speedtest-cli", "aarch64_cortex-a53", "install-ookla-speedtest-cli.sh"),
        }
        for product, (filename, package, arch, installer) in fixtures.items():
            directory = self.downloads / product
            directory.mkdir(parents=True)
            make_ipk(directory / filename, package, arch=arch)
            (directory / installer).write_text(f"#!/bin/sh\n# {product}\n")

    def test_assembles_deterministic_sorted_index_and_exact_inventory(self):
        records = assemble(self.downloads, self.output, self.manifest)
        self.assertEqual([r.package for r in records], sorted(r.package for r in records))
        packages = (self.output / "Packages").read_bytes()
        first_snapshot = {p.name: p.read_bytes() for p in self.output.iterdir()}
        shutil.rmtree(self.output)
        assemble(self.downloads, self.output, self.manifest)
        self.assertEqual(first_snapshot, {p.name: p.read_bytes() for p in self.output.iterdir()})
        self.assertEqual(gzip.decompress((self.output / "Packages.gz").read_bytes()), packages)
        self.assertEqual((self.output / "Packages.gz").read_bytes()[4:8], b"\0\0\0\0")
        expected = {"Packages", "Packages.gz",
                    "install-starwatch.sh", "install-wattline.sh", "install-ookla-speedtest-cli.sh"}
        expected |= {p.name for p in self.downloads.glob("*/*.ipk")}
        self.assertEqual({p.name for p in self.output.iterdir()}, expected)
        for ipk in self.downloads.glob("*/*.ipk"):
            self.assertIn(f"Filename: {ipk.name}\n", packages.decode())
            self.assertIn(f"Size: {ipk.stat().st_size}\n", packages.decode())
            self.assertIn(f"SHA256sum: {hashlib.sha256(ipk.read_bytes()).hexdigest()}\n", packages.decode())
        self.assertIn("Description: first line\n second line\n", packages.decode())

    def assert_rejected(self, message):
        with self.assertRaisesRegex(FeedError, message):
            assemble(self.downloads, self.output, self.manifest)

    def test_rejects_missing_product_or_installer_and_multiple_installers(self):
        shutil.rmtree(self.downloads / "starwatch")
        self.assert_rejected("starwatch")
        self.setUp()
        (self.downloads / "starwatch" / "install-extra.sh").write_text("x")
        self.assert_rejected("installer")
        (self.downloads / "starwatch" / "install-starwatch.sh").unlink()
        (self.downloads / "starwatch" / "install-extra.sh").unlink()
        self.assert_rejected("installer")

    def test_rejects_unexpected_filename_and_architecture(self):
        source = next((self.downloads / "starwatch").glob("*.ipk"))
        source.rename(source.with_name("foreign_1.0_all.ipk"))
        self.assert_rejected("filename")
        self.setUp()
        source = next((self.downloads / "wattline").glob("*.ipk"))
        make_ipk(source, "wattline-bt", arch="x86_64")
        self.assert_rejected("architecture")

    def test_rejects_duplicates_by_tuple_and_filename(self):
        make_ipk(self.downloads / "starwatch" / "luci-app-starwatch_1.2.3_all.ipk", "wattline-bt", version="1.0-1")
        self.assert_rejected("duplicate package tuple")
        self.setUp()
        duplicate = self.downloads / "wattline" / "starwatchd_1.2.3_aarch64_cortex-a53.ipk"
        shutil.copy2(next((self.downloads / "starwatch").glob("*.ipk")), duplicate)
        self.assert_rejected("duplicate filename")

    def test_rejects_malformed_ar_pax_and_unsafe_archives(self):
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        target.write_bytes(b"!<arch>\ninvalid")
        self.assert_rejected("gzip")
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53", outer_format=tarfile.PAX_FORMAT)
        self.assert_rejected("ustar")
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53", control_format=tarfile.PAX_FORMAT)
        self.assert_rejected("ustar")
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53", control_name="../control")
        self.assert_rejected("control")

    def test_rejects_malformed_control_and_missing_indexed_output(self):
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53", control_override=b"Package: x\n broken\n")
        self.assert_rejected("control")
        self.setUp()
        import scripts.assemble_feed as module
        original = module._copy_file
        self.addCleanup(setattr, module, "_copy_file", original)
        module._copy_file = lambda source, destination: None if source.suffix == ".ipk" else original(source, destination)
        self.assert_rejected("indexed file")

    def test_manifest_is_strict_and_names_exact_repositories(self):
        repos = {item.repository for item in self.manifest}
        self.assertEqual(repos, {"keithah/openwrt-starwatch", "keithah/openwrt-wattline", "keithah/openwrt-ookla-speedtest-cli"})
        bad = self.base / "bad.json"
        bad.write_text(json.dumps({"sources": [], "extra": True}))
        with self.assertRaises(FeedError):
            load_manifest(bad)
        bad.write_text(json.dumps({"sources": [vars(self.manifest[0])]}))
        with self.assertRaisesRegex(FeedError, "three exact"):
            load_manifest(bad)

    def test_accepts_safe_control_directories_but_rejects_links(self):
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        control = b"Package: starwatchd\nVersion: 1.0-1\nArchitecture: aarch64_cortex-a53\n"
        inner = _tar([("./", b"", "dir"), ("./control", control, "file")])
        outer = _tar([
            ("./debian-binary", b"2.0\n", "file"),
            ("./control.tar.gz", gzip.compress(inner, mtime=0), "file"),
            ("./data.tar.gz", gzip.compress(_tar([]), mtime=0), "file"),
        ])
        target.write_bytes(gzip.compress(outer, mtime=0))
        assemble(self.downloads, self.output, self.manifest)
        inner = _tar([("./control", b"", "symlink")])
        outer = _tar([
            ("./debian-binary", b"2.0\n", "file"),
            ("./control.tar.gz", gzip.compress(inner, mtime=0), "file"),
            ("./data.tar.gz", gzip.compress(_tar([]), mtime=0), "file"),
        ])
        target.write_bytes(gzip.compress(outer, mtime=0))
        self.assert_rejected("unsafe")

    def test_atomic_failure_preserves_previous_output(self):
        self.output.mkdir()
        (self.output / "old").write_text("safe")
        next((self.downloads / "starwatch").glob("*.ipk")).write_bytes(b"bad")
        self.assert_rejected("gzip")
        self.assertEqual((self.output / "old").read_text(), "safe")


if __name__ == "__main__":
    unittest.main()
