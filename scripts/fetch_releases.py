#!/usr/bin/env python3
"""Fetch exact artifacts from each product's latest immutable GitHub release."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts.assemble_feed import FeedError, SourceSpec, _validate_manifest, load_manifest


API_ROOT = "https://api.github.com"
USER_AGENT = "openwrt-packages-feed/1"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 30
MAX_JSON_SIZE = 2 * 1024 * 1024
MAX_ASSET_SIZE = 64 * 1024 * 1024
MAX_INSTALLER_SIZE = 1024 * 1024
MAX_HEADER_SIZE = 64 * 1024
CHUNK_SIZE = 64 * 1024
MAX_TAG_DEPTH = 8
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_DOWNLOAD_TYPES = frozenset({
    "application/octet-stream",
    "application/gzip",
    "application/x-gzip",
    "application/vnd.debian.binary-package",
})


class FetchError(ValueError):
    """A release could not be fetched without violating the feed policy."""


def _duplicate_checked_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise FetchError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _allowed_host(host: str | None) -> bool:
    if host in {"api.github.com", "github.com"}:
        return True
    return bool(host and host.endswith(".githubusercontent.com"))


def _validate_https_url(url: str, *, api_only: bool = False) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise FetchError("malformed GitHub URL") from exc
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or not _allowed_host(parsed.hostname)
            or (api_only and parsed.hostname != "api.github.com")):
        raise FetchError(f"disallowed GitHub URL or redirect host: {url}")


class _GitHubRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        _validate_https_url(newurl)
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(newurl).hostname != "api.github.com":
            redirected.remove_header("Authorization")
            for key in list(redirected.unredirected_hdrs):
                if key.lower() == "authorization":
                    del redirected.unredirected_hdrs[key]
        return redirected


def _request(url: str, token: str | None, *, binary: bool = False) -> Request:
    _validate_https_url(url, api_only=True)
    headers = {
        "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, headers=headers, method="GET")


def _open(opener, request: Request):
    try:
        if hasattr(opener, "open"):
            return opener.open(request, timeout=TIMEOUT_SECONDS)
        return opener(request, timeout=TIMEOUT_SECONDS)
    except FetchError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"network failure fetching {request.full_url}: {exc}") from exc


def _response_metadata(response, requested_url: str, *, limit: int,
                       expected_types: frozenset[str], final_api_only: bool = False) -> int | None:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    if status != 200:
        raise FetchError(f"unexpected HTTP status {status} for {requested_url}")
    final_url = response.geturl()
    _validate_https_url(final_url, api_only=final_api_only)
    try:
        header_items = list(response.headers.items())
    except (AttributeError, TypeError) as exc:
        raise FetchError("malformed response headers") from exc
    header_size = sum(len(str(name)) + len(str(value)) + 4 for name, value in header_items)
    if header_size > MAX_HEADER_SIZE:
        raise FetchError("response header size limit exceeded")
    lowered = {}
    for name, value in header_items:
        normalized = str(name).lower()
        if normalized in lowered and normalized in {"content-length", "content-type"}:
            raise FetchError(f"duplicate response header: {normalized}")
        lowered[normalized] = str(value).strip()
    content_type = lowered.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in expected_types:
        raise FetchError(f"unexpected response content type: {content_type or 'missing'}")
    length = None
    if "content-length" in lowered:
        try:
            length = int(lowered["content-length"], 10)
        except ValueError as exc:
            raise FetchError("malformed Content-Length header") from exc
        if length < 0 or length > limit:
            raise FetchError("response body is too large")
    return length


def _read_bounded(response, limit: int) -> bytes:
    chunks = []
    remaining = limit + 1
    while remaining:
        chunk = response.read(min(CHUNK_SIZE, remaining))
        if not isinstance(chunk, bytes):
            raise FetchError("response returned non-byte content")
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise FetchError("response body is too large")
    return payload


def _get_json(opener, url: str, token: str | None):
    request = _request(url, token)
    try:
        with _open(opener, request) as response:
            content_length = _response_metadata(
                response, url, limit=MAX_JSON_SIZE,
                expected_types=frozenset({"application/json", "application/vnd.github+json"}),
                final_api_only=True,
            )
            body = _read_bounded(response, MAX_JSON_SIZE)
            if content_length is not None and len(body) != content_length:
                raise FetchError("truncated JSON response body")
    except FetchError:
        raise
    except (OSError, ValueError) as exc:
        raise FetchError(f"failed reading GitHub JSON response: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=_duplicate_checked_object)
    except FetchError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FetchError(f"malformed GitHub JSON response: {exc}") from exc


def _safe_filename(name: object) -> str:
    if (not isinstance(name, str) or not name or len(name.encode("utf-8")) > 255
            or "\0" in name or "\\" in name or PurePosixPath(name).name != name
            or name in {".", ".."}
            or any(ord(character) < 32 or ord(character) == 127 for character in name)):
        raise FetchError("unsafe release asset name")
    return name


def _validate_tag(tag: object) -> str:
    if (not isinstance(tag, str) or not tag or len(tag.encode("utf-8")) > 255
            or "\0" in tag or any(ord(char) < 32 or ord(char) == 127 for char in tag)):
        raise FetchError("invalid release tag")
    return tag


def _asset_api_url(spec: SourceSpec, asset: dict) -> tuple[str, str]:
    if not isinstance(asset, dict):
        raise FetchError("malformed release asset")
    asset_id = asset.get("id")
    name = _safe_filename(asset.get("name"))
    url = asset.get("url")
    if isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id <= 0 or not isinstance(url, str):
        raise FetchError("malformed release asset fields")
    expected = f"{API_ROOT}/repos/{spec.repository}/releases/assets/{asset_id}"
    if url != expected:
        raise FetchError("release asset URL is not its exact GitHub API URL")
    return name, url


def _latest_release(opener, spec: SourceSpec, token: str | None) -> tuple[str, list[tuple[str, str]]]:
    url = f"{API_ROOT}/repos/{spec.repository}/releases/latest"
    release = _get_json(opener, url, token)
    if not isinstance(release, dict):
        raise FetchError(f"malformed latest release response for {spec.product}")
    if not all(key in release for key in ("tag_name", "draft", "prerelease", "assets")):
        raise FetchError("latest release is missing required fields")
    if not isinstance(release["draft"], bool) or not isinstance(release["prerelease"], bool):
        raise FetchError("release flags must be booleans")
    if release["draft"] or release["prerelease"]:
        raise FetchError(f"latest release is not stable for {spec.product}")
    # GitHub added this field after the endpoint was established. Accept both
    # mutable and immutable releases for compatibility, but reject malformed
    # values; tag-to-commit pinning and the final movement check are mandatory.
    if "immutable" in release and not isinstance(release["immutable"], bool):
        raise FetchError("latest release immutable flag must be boolean")
    tag = _validate_tag(release["tag_name"])
    raw_assets = release["assets"]
    if not isinstance(raw_assets, list) or len(raw_assets) > 256:
        raise FetchError("malformed release asset list")
    seen_names = set()
    selected_assets = []
    selected_packages = set()
    for raw_asset in raw_assets:
        name, asset_url = _asset_api_url(spec, raw_asset)
        if name in seen_names:
            raise FetchError(f"duplicate release asset name: {name}")
        seen_names.add(name)
        match = spec.ipk_regex.fullmatch(name)
        if match is None:
            raise FetchError(f"unexpected release asset: {name}")
        package = match.group("package")
        if package not in spec.packages or package in selected_packages:
            raise FetchError(f"duplicate or unexpected package release asset: {package}")
        selected_packages.add(package)
        selected_assets.append((name, asset_url))
    if selected_packages != set(spec.packages):
        raise FetchError(f"missing release package assets for {spec.product}")
    return tag, sorted(selected_assets)


def _git_target(spec: SourceSpec, value: object) -> tuple[str, str, str]:
    if not isinstance(value, dict):
        raise FetchError("malformed Git tag object")
    object_type = value.get("type")
    sha = value.get("sha")
    url = value.get("url")
    if object_type not in {"commit", "tag"} or not isinstance(sha, str) or not GIT_SHA.fullmatch(sha):
        raise FetchError("invalid Git tag target object")
    expected = f"{API_ROOT}/repos/{spec.repository}/git/{'commits' if object_type == 'commit' else 'tags'}/{sha}"
    if url != expected:
        raise FetchError("Git tag target URL does not match its object")
    return object_type, sha, url


def _resolve_tag(opener, spec: SourceSpec, tag: str, token: str | None) -> str:
    encoded_tag = quote(tag, safe="")
    ref_url = f"{API_ROOT}/repos/{spec.repository}/git/ref/tags/{encoded_tag}"
    value = _get_json(opener, ref_url, token)
    if (not isinstance(value, dict) or value.get("ref") != f"refs/tags/{tag}"
            or "object" not in value):
        raise FetchError(f"malformed Git tag ref for {spec.product}")
    object_type, sha, object_url = _git_target(spec, value["object"])
    seen = set()
    for _depth in range(MAX_TAG_DEPTH):
        if object_type == "commit":
            return sha
        if sha in seen:
            raise FetchError(f"Git tag cycle detected for {spec.product}")
        seen.add(sha)
        annotated = _get_json(opener, object_url, token)
        if (not isinstance(annotated, dict) or annotated.get("sha") != sha
                or "object" not in annotated):
            raise FetchError(f"malformed annotated Git tag for {spec.product}")
        object_type, sha, object_url = _git_target(spec, annotated["object"])
    raise FetchError(f"Git tag nesting limit exceeded for {spec.product}")


def _download_asset(opener, url: str, output: Path, token: str | None) -> None:
    request = _request(url, token, binary=True)
    temporary = output.with_name(f".{output.name}.part")
    digest = hashlib.sha256()
    count = 0
    try:
        with _open(opener, request) as response:
            content_length = _response_metadata(
                response, url, limit=MAX_ASSET_SIZE, expected_types=ALLOWED_DOWNLOAD_TYPES
            )
            with temporary.open("xb") as stream:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not isinstance(chunk, bytes):
                        raise FetchError("asset response returned non-byte content")
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > MAX_ASSET_SIZE:
                        raise FetchError("release asset is too large")
                    stream.write(chunk)
                    digest.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        if content_length is not None and count != content_length:
            raise FetchError("truncated release asset response")
        if count == 0:
            raise FetchError("release asset is empty")
        if temporary.stat().st_size != count or hashlib.sha256(temporary.read_bytes()).digest() != digest.digest():
            raise FetchError("release asset checksum changed while writing")
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    except FetchError:
        raise
    except OSError as exc:
        raise FetchError(f"failed writing release asset: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _installer(opener, spec: SourceSpec, commit: str, token: str | None) -> bytes:
    encoded_path = "/".join(quote(part, safe="") for part in spec.installer_source.split("/"))
    url = f"{API_ROOT}/repos/{spec.repository}/contents/{encoded_path}?ref={commit}"
    value = _get_json(opener, url, token)
    required = {"type", "name", "path", "encoding", "content", "size", "sha"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise FetchError(f"malformed installer response for {spec.product}")
    expected_name = PurePosixPath(spec.installer_source).name
    if (value["type"] != "file" or value["name"] != expected_name
            or value["path"] != spec.installer_source or value["encoding"] != "base64"
            or not isinstance(value["content"], str) or isinstance(value["size"], bool)
            or not isinstance(value["size"], int) or value["size"] < 0
            or not isinstance(value["sha"], str)):
        raise FetchError(f"invalid installer metadata for {spec.product}")
    if value["size"] > MAX_INSTALLER_SIZE:
        raise FetchError("installer is too large")
    encoded = "".join(value["content"].split())
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise FetchError(f"invalid installer base64 for {spec.product}") from exc
    if len(content) > MAX_INSTALLER_SIZE:
        raise FetchError("installer is too large")
    if len(content) != value["size"] or not content or b"\0" in content:
        raise FetchError(f"installer content metadata mismatch for {spec.product}")
    blob_sha = hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()
    if value["sha"] != blob_sha:
        raise FetchError(f"installer Git blob checksum mismatch for {spec.product}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FetchError(f"installer is not UTF-8 for {spec.product}") from exc
    if not text.startswith("#!/bin/sh\n"):
        raise FetchError(f"installer is not a POSIX shell script for {spec.product}")
    return content


def _replace_destination(staging: Path, destination: Path) -> None:
    backup = None
    try:
        if destination.exists():
            backup = Path(tempfile.mkdtemp(prefix=f".{destination.name}.old-", dir=destination.parent))
            backup.rmdir()
            os.replace(destination, backup)
        os.replace(staging, destination)
    except BaseException:
        if backup is not None and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def fetch_all(manifest: dict, destination: Path, *, opener=None,
              token: str | None = None) -> dict[str, str]:
    """Fetch all manifest products atomically and return product-to-tag mapping."""
    try:
        specs = _validate_manifest(manifest)
    except FeedError as exc:
        raise FetchError(f"invalid manifest: {exc}") from exc
    destination = Path(destination)
    try:
        resolved_parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise FetchError(f"cannot resolve destination parent: {exc}") from exc
    if (destination == destination.parent or destination.name in {"", ".", ".."}
            or destination.is_symlink() or (destination.exists() and not destination.is_dir())):
        raise FetchError("unsafe destination directory")
    try:
        if destination.exists() and destination.resolve(strict=True).parent != resolved_parent:
            raise FetchError("unsafe destination directory")
    except OSError as exc:
        raise FetchError(f"cannot resolve destination: {exc}") from exc
    if opener is None:
        opener = build_opener(_GitHubRedirectHandler())
    if token is None:
        token = os.environ.get("GH_TOKEN") or None
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.new-", dir=resolved_parent))
    tags = {}
    pinned = {}
    try:
        for spec in specs:
            product_dir = staging / spec.product
            product_dir.mkdir(mode=0o755)
            tag, assets = _latest_release(opener, spec, token)
            commit = _resolve_tag(opener, spec, tag, token)
            for name, url in assets:
                _download_asset(opener, url, product_dir / name, token)
            installer = _installer(opener, spec, commit, token)
            installer_path = product_dir / spec.installer
            installer_path.write_bytes(installer)
            os.chmod(installer_path, 0o755)
            expected = {name for name, _ in assets} | {spec.installer}
            entries = list(product_dir.iterdir())
            if ({entry.name for entry in entries} != expected
                    or any(not entry.is_file() or entry.is_symlink() for entry in entries)):
                raise FetchError(f"unexpected staged inventory for {spec.product}")
            tags[spec.product] = tag
            pinned[spec.product] = (spec, tag, commit)
        for product, (spec, tag, commit) in pinned.items():
            if _resolve_tag(opener, spec, tag, token) != commit:
                raise FetchError(f"release tag moved while fetching {product}")
        _replace_destination(staging, destination)
        return tags
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("sources.json"))
    parser.add_argument("--destination", type=Path, default=Path("downloads"))
    args = parser.parse_args()
    fetch_all(load_manifest(args.manifest), args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
