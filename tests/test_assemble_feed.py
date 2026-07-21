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
             control_name="./control", control_override=None,
             data_override=None, outer_extra=(), control_extra=()):
    control = control_override or (
        f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\n"
        "Description: first line\n second line\n"
    ).encode()
    control_tar = _tar([(control_name, control, "file"), *control_extra], fmt=control_format)
    control_gz = gzip.compress(control_tar, mtime=0)
    data_gz = data_override if data_override is not None else gzip.compress(_tar([], fmt=tarfile.USTAR_FORMAT), mtime=0)
    outer = _tar([
        ("./debian-binary", b"2.0\n", "file"),
        ("./control.tar.gz", control_gz, "file"),
        ("./data.tar.gz", data_gz, "file"),
        *outer_extra,
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
            version = filename.removeprefix(package + "_").removesuffix("_" + arch + ".ipk")
            make_ipk(directory / filename, package, version=version, arch=arch)
            (directory / installer).write_text(f"#!/bin/sh\n# {product}\n")
            (directory / installer).chmod(0o600)

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
        for installer in ("install-starwatch.sh", "install-wattline.sh", "install-ookla-speedtest-cli.sh"):
            product = installer.removeprefix("install-").removesuffix(".sh")
            if product == "ookla-speedtest-cli":
                product = "speedtest"
            copied = self.output / installer
            self.assertEqual(copied.read_bytes(), (self.downloads / product / installer).read_bytes())
            self.assertEqual(copied.stat().st_mode & 0o777, 0o755)
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
        make_ipk(self.downloads / "starwatch" / "luci-app-starwatch_1.2.3_all.ipk", "wattline-bt", version="2.0.0")
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
        repos = {item["repository"] for item in self.manifest["sources"]}
        self.assertEqual(repos, {"keithah/openwrt-starwatch", "keithah/openwrt-wattline", "keithah/openwrt-ookla-speedtest-cli"})
        bad = self.base / "bad.json"
        bad.write_text(json.dumps({"sources": [], "extra": True}))
        with self.assertRaises(FeedError):
            load_manifest(bad)
        bad.write_text(json.dumps({"sources": [self.manifest["sources"][0]]}))
        with self.assertRaisesRegex(FeedError, "three exact"):
            load_manifest(bad)

    def test_rejects_duplicate_json_keys_at_root_and_source_levels(self):
        bad = self.base / "bad.json"
        bad.write_text('{"sources": [], "sources": []}')
        with self.assertRaisesRegex(FeedError, "duplicate JSON key"):
            load_manifest(bad)
        source = self.manifest["sources"][0]
        encoded = json.dumps(source)[1:-1]
        bad.write_text('{"sources": [{' + encoded + ', "product": "evil"}]}')
        with self.assertRaisesRegex(FeedError, "duplicate JSON key"):
            load_manifest(bad)

    def test_assemble_revalidates_dict_and_rejects_manifest_subset(self):
        subset = {"sources": self.manifest["sources"][:2]}
        with self.assertRaisesRegex(FeedError, "three exact"):
            assemble(self.downloads, self.output, subset)
        with self.assertRaisesRegex(FeedError, "manifest"):
            assemble(self.downloads, self.output, list(self.manifest["sources"]))
        changed = json.loads(json.dumps(self.manifest))
        changed["sources"][0]["installer"] = "install-other.sh"
        with self.assertRaisesRegex(FeedError, "exact product"):
            assemble(self.downloads, self.output, changed)
        changed = json.loads(json.dumps(self.manifest))
        changed["sources"][0]["ipk_pattern"] = (
            "^(?P<package>starwatchd)_(?P<version>.+)_(?P<architecture>all)\\.ipk$"
        )
        with self.assertRaisesRegex(FeedError, "exact product"):
            assemble(self.downloads, self.output, changed)

    def test_rejects_control_metadata_that_disagrees_with_filename(self):
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        make_ipk(target, "luci-app-starwatch", version="1.2.3", arch="aarch64_cortex-a53")
        self.assert_rejected("Package.*filename")
        self.setUp()
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        make_ipk(target, "starwatchd", version="9.9-1", arch="aarch64_cortex-a53")
        self.assert_rejected("Version.*filename")
        self.setUp()
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        make_ipk(target, "starwatchd", version="1.2.3", arch="all")
        self.assert_rejected("Architecture.*filename")

    def test_rejects_duplicate_outer_and_control_members(self):
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53",
                 outer_extra=(("./control.tar.gz", b"duplicate", "file"),))
        self.assert_rejected("once each")
        self.setUp()
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        control = b"Package: starwatchd\nVersion: 1.2.3\nArchitecture: aarch64_cortex-a53\n"
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53",
                 control_extra=(("control", control, "file"),))
        self.assert_rejected("exactly one control")

    def test_rejects_malformed_pax_and_unsafe_data_archives(self):
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53", data_override=b"not-gzip")
        self.assert_rejected("data archive")
        self.setUp()
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        pax = gzip.compress(_tar([("./usr/bin/x", b"x", "file")], fmt=tarfile.PAX_FORMAT), mtime=0)
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53", data_override=pax)
        self.assert_rejected("data archive.*ustar")
        self.setUp()
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        traversal = gzip.compress(_tar([("../escape", b"x", "file")]), mtime=0)
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53", data_override=traversal)
        self.assert_rejected("unsafe member.*data archive")
        self.setUp()
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        link = gzip.compress(_tar([("./usr/bin/x", b"", "symlink")]), mtime=0)
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53", data_override=link)
        self.assert_rejected("extended member.*data archive")

    def test_rejects_data_archive_member_count_and_member_size_abuse(self):
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        many = [(f"./usr/share/{i}", b"", "file") for i in range(129)]
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53",
                 data_override=gzip.compress(_tar(many), mtime=0))
        self.assert_rejected("too many members.*data archive")
        self.setUp()
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        # Patch the declared size without allocating a giant fixture; tarfile
        # must still be forced through the bounded member-size check.
        raw = bytearray(_tar([("./usr/bin/x", b"x", "file")]))
        raw[124:136] = b"400000001\0\0\0"  # 64 MiB + 1, octal
        raw[148:156] = b"        "
        checksum = sum(raw[:512])
        raw[148:156] = f"{checksum:06o}\0 ".encode()
        make_ipk(target, "starwatchd", arch="aarch64_cortex-a53",
                 data_override=gzip.compress(bytes(raw), mtime=0))
        self.assert_rejected("member size.*data archive")

    def test_accepts_data_member_larger_than_real_starwatch_binary(self):
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        payload = b"\0" * 18_809_017
        data = gzip.compress(_tar([("./usr/bin/starwatchd", payload, "file")]), mtime=0)
        make_ipk(target, "starwatchd", version="1.2.3", arch="aarch64_cortex-a53",
                 data_override=data)
        records = assemble(self.downloads, self.output, self.manifest)
        self.assertIn("starwatchd", {record.package for record in records})

    def test_accepts_safe_control_directories_but_rejects_links(self):
        target = next((self.downloads / "starwatch").glob("*.ipk"))
        control = b"Package: starwatchd\nVersion: 1.2.3\nArchitecture: aarch64_cortex-a53\n"
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
