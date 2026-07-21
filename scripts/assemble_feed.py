#!/usr/bin/env python3
"""Validate product IPKs and assemble a deterministic OpenWrt package feed."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
from typing import Iterable


ALLOWED_ARCHITECTURES = frozenset({"aarch64_cortex-a53", "all"})
MAX_IPK_SIZE = 64 * 1024 * 1024
MAX_TAR_SIZE = 128 * 1024 * 1024
MAX_CONTROL_SIZE = 1024 * 1024
MAX_INSTALLER_SIZE = 1024 * 1024
MAX_MEMBERS = 128
FIELD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
PRODUCT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
REPOSITORY = re.compile(r"^keithah/[A-Za-z0-9._-]+$")
SAFE_INSTALLER = re.compile(r"^install-[a-z0-9][a-z0-9-]*\.sh$")
SOURCE_PATH = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
EXPECTED_REPOSITORIES = {
    "starwatch": "keithah/openwrt-starwatch",
    "wattline": "keithah/openwrt-wattline",
    "speedtest": "keithah/openwrt-ookla-speedtest-cli",
}


class FeedError(ValueError):
    """Input is not a safe, valid feed artifact."""


@dataclass(frozen=True)
class SourceSpec:
    product: str
    repository: str
    ipk_pattern: str
    installer: str
    installer_source: str

    @property
    def ipk_regex(self) -> re.Pattern[str]:
        return re.compile(self.ipk_pattern, re.ASCII)


@dataclass(frozen=True)
class PackageRecord:
    package: str
    version: str
    architecture: str
    filename: str
    size: int
    sha256: str
    control: tuple[tuple[str, str], ...]

    def render(self) -> str:
        fields = list(self.control)
        fields.extend((
            ("Filename", self.filename),
            ("Size", str(self.size)),
            ("SHA256sum", self.sha256),
        ))
        return "".join(f"{name}: {value}\n" for name, value in fields) + "\n"


def load_manifest(path: Path) -> list[SourceSpec]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FeedError(f"invalid manifest: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"sources"} or not isinstance(raw["sources"], list):
        raise FeedError("manifest must contain only a sources array")
    required = {"product", "repository", "ipk_pattern", "installer", "installer_source"}
    result = []
    products = set()
    installers = set()
    for item in raw["sources"]:
        if not isinstance(item, dict) or set(item) != required or not all(isinstance(item[k], str) for k in required):
            raise FeedError("manifest source has unexpected or invalid fields")
        spec = SourceSpec(**item)
        if not PRODUCT_NAME.fullmatch(spec.product) or not REPOSITORY.fullmatch(spec.repository):
            raise FeedError("invalid product or repository")
        if not SAFE_INSTALLER.fullmatch(spec.installer) or not SOURCE_PATH.fullmatch(spec.installer_source):
            raise FeedError("invalid installer path")
        try:
            compiled = spec.ipk_regex
        except re.error as exc:
            raise FeedError(f"invalid IPK pattern: {exc}") from exc
        if not spec.ipk_pattern.startswith("^") or not spec.ipk_pattern.endswith("$") or compiled.match(""):
            raise FeedError("IPK pattern must be anchored and nonempty")
        if spec.product in products or spec.installer in installers:
            raise FeedError("duplicate manifest product or installer")
        products.add(spec.product)
        installers.add(spec.installer)
        result.append(spec)
    if not result:
        raise FeedError("manifest has no sources")
    if {spec.product: spec.repository for spec in result} != EXPECTED_REPOSITORIES:
        raise FeedError("manifest must define the three exact product repositories")
    return result


def _bounded_gzip(data: bytes, label: str, limit: int = MAX_TAR_SIZE) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as stream:
            result = stream.read(limit + 1)
            if len(result) > limit:
                raise FeedError(f"{label} exceeds decompressed size limit")
            if stream.read(1):
                raise FeedError(f"{label} exceeds decompressed size limit")
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise FeedError(f"invalid gzip {label}: {exc}") from exc
    return result


def _safe_name(name: str, *, directory: bool = False) -> bool:
    normalized = name[2:] if name.startswith("./") else name
    if directory and normalized in ("", "."):
        return True
    normalized = normalized.rstrip("/") if directory else normalized
    parts = normalized.split("/")
    return bool(normalized) and not normalized.startswith("/") and all(p not in ("", ".", "..") for p in parts)


def _ustar_members(raw: bytes, label: str, *, allow_directories: bool = False) -> list[tuple[tarfile.TarInfo, bytes]]:
    if len(raw) < 1024 or len(raw) % 512:
        raise FeedError(f"malformed {label} ustar archive")
    # Walk physical headers because tarfile intentionally hides pax/GNU
    # extension records from getmembers().
    offset = 0
    headers = []
    while offset + 512 <= len(raw):
        block = raw[offset:offset + 512]
        if block == b"\0" * 512:
            if raw[offset:offset + 1024] != b"\0" * 1024:
                raise FeedError(f"malformed {label} ustar terminator")
            if any(raw[offset + 1024:]):
                raise FeedError(f"nonzero trailing data in {label} ustar archive")
            break
        if block[257:263] != b"ustar\0":
            raise FeedError(f"{label} is not a POSIX ustar archive")
        typeflag = block[156:157]
        allowed_types = (b"", b"\0", b"0", b"5") if allow_directories else (b"", b"\0", b"0")
        if typeflag not in allowed_types:
            raise FeedError(f"unsafe or extended member in {label} ustar archive")
        try:
            size_field = block[124:136].rstrip(b"\0 ") or b"0"
            size = int(size_field, 8)
        except ValueError as exc:
            raise FeedError(f"malformed {label} member size") from exc
        headers.append(offset)
        offset += 512 + ((size + 511) // 512) * 512
        if len(headers) > MAX_MEMBERS:
            raise FeedError(f"too many members in {label}")
    else:
        raise FeedError(f"unterminated {label} ustar archive")
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            infos = archive.getmembers()
            if len(infos) > MAX_MEMBERS:
                raise FeedError(f"too many members in {label}")
            result = []
            for info in infos:
                if info.offset not in headers:
                    raise FeedError(f"{label} is not a POSIX ustar archive")
                if info.isdir() and allow_directories and _safe_name(info.name, directory=True):
                    continue
                if not _safe_name(info.name) or not info.isfile():
                    raise FeedError(f"unsafe member in {label}: {info.name}")
                if info.size > MAX_CONTROL_SIZE and label == "control archive":
                    raise FeedError("control member exceeds size limit")
                extracted = archive.extractfile(info)
                if extracted is None:
                    raise FeedError(f"unreadable member in {label}")
                contents = extracted.read(info.size + 1)
                if len(contents) != info.size:
                    raise FeedError(f"truncated member in {label}")
                result.append((info, contents))
            return result
    except (tarfile.TarError, OSError) as exc:
        raise FeedError(f"malformed {label} ustar archive: {exc}") from exc


def _parse_control(data: bytes) -> tuple[tuple[str, str], ...]:
    if len(data) > MAX_CONTROL_SIZE or b"\0" in data:
        raise FeedError("invalid control record")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FeedError("control record is not UTF-8") from exc
    fields: list[tuple[str, str]] = []
    seen = set()
    for line in text.splitlines():
        if line.startswith((" ", "\t")):
            if not fields:
                raise FeedError("malformed control continuation")
            name, value = fields[-1]
            fields[-1] = (name, value + "\n" + line)
            continue
        if not line or ":" not in line:
            raise FeedError("malformed control field")
        name, value = line.split(":", 1)
        if not FIELD_NAME.fullmatch(name) or name.lower() in seen:
            raise FeedError("invalid or duplicate control field")
        seen.add(name.lower())
        fields.append((name, value[1:] if value.startswith(" ") else value))
    values = {name.lower(): value for name, value in fields}
    for required in ("package", "version", "architecture"):
        if not values.get(required) or "\n" in values[required]:
            raise FeedError(f"control record missing {required}")
    if any(name in values for name in ("filename", "size", "sha256sum")):
        raise FeedError("control record contains generated index fields")
    return tuple(fields)


def _read_ipk(path: Path) -> PackageRecord:
    try:
        stat = path.lstat()
        if not path.is_file() or path.is_symlink() or stat.st_size > MAX_IPK_SIZE:
            raise FeedError(f"invalid IPK file: {path.name}")
        compressed = path.read_bytes()
    except OSError as exc:
        raise FeedError(f"cannot read IPK {path.name}: {exc}") from exc
    outer = _ustar_members(_bounded_gzip(compressed, "IPK"), "IPK")
    members = {(i.name[2:] if i.name.startswith("./") else i.name): data for i, data in outer}
    if set(members) != {"debian-binary", "control.tar.gz", "data.tar.gz"} or members["debian-binary"] != b"2.0\n":
        raise FeedError("IPK outer archive has unexpected members")
    control_members = _ustar_members(
        _bounded_gzip(members["control.tar.gz"], "control archive"),
        "control archive",
        allow_directories=True,
    )
    controls = [data for info, data in control_members if (info.name[2:] if info.name.startswith("./") else info.name) == "control"]
    if len(controls) != 1:
        raise FeedError("control archive must contain exactly one control file")
    fields = _parse_control(controls[0])
    values = {name.lower(): value for name, value in fields}
    return PackageRecord(values["package"], values["version"], values["architecture"],
                         path.name, stat.st_size, hashlib.sha256(compressed).hexdigest(), fields)


def _copy_file(source: Path, destination: Path) -> None:
    shutil.copyfile(source, destination)


def _write_gzip(path: Path, payload: bytes) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(payload)


def _replace_output(staging: Path, output: Path) -> None:
    backup = None
    try:
        if output.exists():
            backup = Path(tempfile.mkdtemp(prefix=f".{output.name}.old-", dir=output.parent))
            backup.rmdir()
            os.replace(output, backup)
        os.replace(staging, output)
    except BaseException:
        if backup is not None and backup.exists() and not output.exists():
            os.replace(backup, output)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def assemble(download_root: Path, output: Path, manifest: Iterable[SourceSpec] | dict) -> list[PackageRecord]:
    download_root = Path(download_root)
    output = Path(output)
    if isinstance(manifest, dict):
        raise FeedError("manifest must be loaded and validated")
    specs = list(manifest)
    try:
        output_resolved = output.resolve()
        download_resolved = download_root.resolve()
        output_in_downloads = output_resolved == download_resolved or download_resolved in output_resolved.parents
    except OSError as exc:
        raise FeedError(f"cannot resolve assembly paths: {exc}") from exc
    if (output == output.parent or output.name in ("", ".", "..") or output.is_symlink()
            or (output.exists() and not output.is_dir()) or output_in_downloads):
        raise FeedError("unsafe output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.new-", dir=output.parent))
    records = []
    filenames = set()
    tuples = set()
    try:
        # A collision is fatal even when the second product's own regex would
        # reject that name; otherwise validation order could mask ambiguity.
        discovered_names = []
        for spec in specs:
            candidate_dir = download_root / spec.product
            if candidate_dir.is_dir() and not candidate_dir.is_symlink():
                discovered_names.extend(p.name for p in candidate_dir.iterdir() if p.suffix == ".ipk")
        if len(discovered_names) != len(set(discovered_names)):
            raise FeedError("duplicate filename across products")
        for spec in specs:
            directory = download_root / spec.product
            if not directory.is_dir() or directory.is_symlink():
                raise FeedError(f"missing product directory: {spec.product}")
            entries = list(directory.iterdir())
            if any(not p.is_file() or p.is_symlink() for p in entries):
                raise FeedError(f"unexpected path for product {spec.product}")
            installers = [p for p in entries if p.name.startswith("install-") and p.suffix == ".sh"]
            if len(installers) != 1 or installers[0].name != spec.installer:
                raise FeedError(f"product {spec.product} must have exactly one expected installer")
            if installers[0].stat().st_size > MAX_INSTALLER_SIZE:
                raise FeedError(f"installer exceeds size limit for {spec.product}")
            ipks = [p for p in entries if p.suffix == ".ipk"]
            unexpected = [p.name for p in entries if p not in installers and p not in ipks]
            if unexpected or not ipks:
                raise FeedError(f"missing IPKs or unexpected filename for {spec.product}")
            for ipk in sorted(ipks):
                if not spec.ipk_regex.fullmatch(ipk.name):
                    raise FeedError(f"unexpected IPK filename: {ipk.name}")
                if ipk.name in filenames:
                    raise FeedError(f"duplicate filename: {ipk.name}")
                record = _read_ipk(ipk)
                if record.architecture not in ALLOWED_ARCHITECTURES:
                    raise FeedError(f"unexpected architecture: {record.architecture}")
                key = (record.package, record.version, record.architecture)
                if key in tuples:
                    raise FeedError(f"duplicate package tuple: {key}")
                filenames.add(ipk.name)
                tuples.add(key)
                records.append(record)
                _copy_file(ipk, staging / ipk.name)
                if not (staging / ipk.name).is_file():
                    raise FeedError(f"indexed file missing from output: {ipk.name}")
                os.chmod(staging / ipk.name, 0o644)
            _copy_file(installers[0], staging / spec.installer)
            os.chmod(staging / spec.installer, 0o755)
        records.sort(key=lambda r: (r.package, r.version, r.architecture, r.filename))
        payload = "".join(record.render() for record in records).encode("utf-8")
        (staging / "Packages").write_bytes(payload)
        os.chmod(staging / "Packages", 0o644)
        _write_gzip(staging / "Packages.gz", payload)
        os.chmod(staging / "Packages.gz", 0o644)
        expected = filenames | {s.installer for s in specs} | {"Packages", "Packages.gz"}
        actual = {p.name for p in staging.iterdir() if p.is_file() and not p.is_symlink()}
        missing = filenames - actual
        if missing:
            raise FeedError(f"indexed file missing from output: {sorted(missing)}")
        if actual != expected or len(list(staging.iterdir())) != len(expected):
            raise FeedError("output inventory does not match allowlist")
        _replace_output(staging, output)
        return records
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("sources.json"))
    parser.add_argument("--downloads", type=Path, default=Path("downloads"))
    parser.add_argument("--output", type=Path, default=Path("pages"))
    args = parser.parse_args()
    assemble(args.downloads, args.output, load_manifest(args.manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
