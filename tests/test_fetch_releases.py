import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.error import URLError

from scripts.assemble_feed import load_manifest
from scripts.fetch_releases import FetchError, _GitHubRedirectHandler, fetch_all


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, body, url, *, content_type="application/json", status=200,
                 content_length=True, headers=None):
        self.body = body
        self.offset = 0
        self.url = url
        self.status = status
        values = {"Content-Type": content_type}
        if content_length:
            values["Content-Length"] = str(len(body))
        if headers:
            values.update(headers)
        self.headers = values

    def read(self, amount=-1):
        if amount < 0:
            amount = len(self.body) - self.offset
        data = self.body[self.offset:self.offset + amount]
        self.offset += len(data)
        return data

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeOpener:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        url = request.full_url
        response = self.routes.get(url)
        if response is None:
            raise AssertionError(f"unexpected URL: {url}")
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(request)
        return response


def _json_response(value, url, **kwargs):
    return FakeResponse(json.dumps(value).encode(), url, **kwargs)


class FetchReleasesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.destination = self.base / "downloads"
        self.manifest = load_manifest(ROOT / "sources.json")
        self.routes = {}
        self.payloads = {}
        self.tags = {}
        asset_id = 100
        for source in self.manifest["sources"]:
            repository = source["repository"]
            product = source["product"]
            tag = f"v1.0.0-{product}"
            self.tags[product] = tag
            assets = []
            for package in source["packages"]:
                architecture = "aarch64_cortex-a53" if package in {
                    "starwatchd", "wattlined", "ookla-speedtest-cli"
                } else "all"
                name = f"{package}_1.0.0-1_{architecture}.ipk"
                api_url = f"https://api.github.com/repos/{repository}/releases/assets/{asset_id}"
                final_url = f"https://objects.githubusercontent.com/release/{asset_id}/{name}"
                payload = f"payload:{product}:{name}".encode()
                assets.append({"id": asset_id, "name": name, "url": api_url})
                self.payloads[(product, name)] = payload
                self.routes[api_url] = FakeResponse(
                    payload, final_url, content_type="application/octet-stream"
                )
                asset_id += 1
            releases_url = f"https://api.github.com/repos/{repository}/releases?per_page=100"
            self.routes[releases_url] = _json_response([
                {"tag_name": "ignored-draft", "draft": True, "prerelease": False, "assets": []},
                {"tag_name": "ignored-prerelease", "draft": False, "prerelease": True, "assets": []},
                {"tag_name": tag, "draft": False, "prerelease": False, "assets": assets},
            ], releases_url)
            source_path = "/".join(part.replace(" ", "%20") for part in source["installer_source"].split("/"))
            encoded_tag = tag.replace("+", "%2B").replace("/", "%2F")
            contents_url = (
                f"https://api.github.com/repos/{repository}/contents/{source_path}?ref={encoded_tag}"
            )
            installer = f"#!/bin/sh\n# {product} {tag}\n".encode()
            self.payloads[(product, source["installer"])] = installer
            contents = {
                "type": "file", "name": Path(source["installer_source"]).name,
                "path": source["installer_source"], "encoding": "base64",
                "content": base64.b64encode(installer).decode(),
                "size": len(installer), "sha": hashlib.sha1(b"blob " + str(len(installer)).encode() + b"\0" + installer).hexdigest(),
            }
            self.routes[contents_url] = _json_response(contents, contents_url)
        self.opener = FakeOpener(self.routes)

    def test_fetches_latest_stable_release_assets_and_same_tag_installer(self):
        tags = fetch_all(self.manifest, self.destination, opener=self.opener, token="secret")
        self.assertEqual(tags, self.tags)
        for source in self.manifest["sources"]:
            product = source["product"]
            actual = {path.name: path.read_bytes() for path in (self.destination / product).iterdir()}
            expected = {name: body for (owner, name), body in self.payloads.items() if owner == product}
            self.assertEqual(actual, expected)
            self.assertTrue(all(not path.is_symlink() for path in (self.destination / product).iterdir()))
        self.assertTrue(all(timeout == 30 for _, timeout in self.opener.requests))
        for request, _ in self.opener.requests:
            self.assertEqual(request.get_header("User-agent"), "openwrt-packages-feed/1")
            self.assertEqual(request.get_header("X-github-api-version"), "2022-11-28")
            if request.host == "api.github.com":
                self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        # Bytes on disk are the exact streamed bytes, not a transformed form.
        target = next((self.destination / "speedtest").glob("*.ipk"))
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),
                         hashlib.sha256(self.payloads[("speedtest", target.name)]).hexdigest())

    def test_percent_encodes_tag_as_one_exact_ref(self):
        source = self.manifest["sources"][2]
        repository = source["repository"]
        releases_url = f"https://api.github.com/repos/{repository}/releases?per_page=100"
        release = json.loads(self.routes[releases_url].body)[2]
        release["tag_name"] = "v1.0+build/one"
        self.routes[releases_url] = _json_response([release], releases_url)
        old_contents = next(url for url in self.routes if f"repos/{repository}/contents/" in url)
        response = self.routes.pop(old_contents)
        expected = old_contents.split("?", 1)[0] + "?ref=v1.0%2Bbuild%2Fone"
        self.routes[expected] = FakeResponse(response.body, expected)
        fetch_all(self.manifest, self.destination, opener=self.opener)
        self.assertIn(expected, [request.full_url for request, _ in self.opener.requests])

    def test_rejects_missing_duplicate_or_unexpected_package_assets(self):
        source = self.manifest["sources"][2]
        url = f"https://api.github.com/repos/{source['repository']}/releases?per_page=100"
        release = json.loads(self.routes[url].body)[2]
        cases = [
            [],
            release["assets"] + [dict(release["assets"][0], id=999)],
            release["assets"] + [{"id": 999, "name": "evil_1_all.ipk",
                                   "url": f"https://api.github.com/repos/{source['repository']}/releases/assets/999"}],
        ]
        for assets in cases:
            with self.subTest(assets=assets):
                self.setUp()
                self.routes[url] = _json_response([dict(release, assets=assets)], url)
                with self.assertRaises(FetchError):
                    fetch_all(self.manifest, self.destination, opener=self.opener)

    def test_rejects_malformed_duplicate_json_fields_and_unsafe_names(self):
        source = self.manifest["sources"][0]
        url = f"https://api.github.com/repos/{source['repository']}/releases?per_page=100"
        bad_bodies = [
            b'{"not":"a list"}',
            b'[{"tag_name":"v1","tag_name":"v2","draft":false,"prerelease":false,"assets":[]}]',
            b'[{"tag_name":"v1","draft":false,"prerelease":false,"assets":'
            b'[{"id":1,"name":"../escape.ipk","url":"https://api.github.com/repos/keithah/openwrt-starwatch/releases/assets/1"}]}]',
        ]
        for body in bad_bodies:
            with self.subTest(body=body):
                self.setUp()
                self.routes[url] = FakeResponse(body, url)
                with self.assertRaises(FetchError):
                    fetch_all(self.manifest, self.destination, opener=self.opener)

    def test_rejects_control_characters_even_in_ignored_asset_names(self):
        source = self.manifest["sources"][0]
        url = f"https://api.github.com/repos/{source['repository']}/releases?per_page=100"
        release = json.loads(self.routes[url].body)[2]
        ancillary = {
            "id": 999, "name": "notes\n.txt",
            "url": f"https://api.github.com/repos/{source['repository']}/releases/assets/999",
        }
        self.routes[url] = _json_response([dict(release, assets=[*release["assets"], ancillary])], url)
        with self.assertRaisesRegex(FetchError, "unsafe"):
            fetch_all(self.manifest, self.destination, opener=self.opener)

    def test_default_redirect_policy_strips_auth_and_rejects_foreign_hosts(self):
        from urllib.request import Request
        handler = _GitHubRedirectHandler()
        original = Request(
            "https://api.github.com/repos/keithah/repo/releases/assets/1",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = handler.redirect_request(
            original, None, 302, "Found", {},
            "https://objects.githubusercontent.com/release/1/package.ipk",
        )
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaisesRegex(FetchError, "host"):
            handler.redirect_request(
                original, None, 302, "Found", {}, "https://evil.example/package.ipk"
            )

    def test_rejects_off_github_final_urls_and_never_sends_token_there(self):
        asset_url = next(url for url in self.routes if "/releases/assets/" in url)
        self.routes[asset_url] = FakeResponse(
            b"stolen", "https://evil.example/stolen", content_type="application/octet-stream"
        )
        with self.assertRaisesRegex(FetchError, "host|redirect"):
            fetch_all(self.manifest, self.destination, opener=self.opener, token="secret")
        self.assertTrue(all(request.host == "api.github.com" for request, _ in self.opener.requests))

    def test_rejects_wrong_asset_api_origin_path_and_status_or_content_type(self):
        source = self.manifest["sources"][0]
        url = f"https://api.github.com/repos/{source['repository']}/releases?per_page=100"
        release = json.loads(self.routes[url].body)[2]
        invalid = dict(release["assets"][0], url="https://github.com/keithah/file.ipk")
        self.routes[url] = _json_response([dict(release, assets=[invalid, *release["assets"][1:]])], url)
        with self.assertRaises(FetchError):
            fetch_all(self.manifest, self.destination, opener=self.opener)
        self.setUp()
        self.routes[url] = FakeResponse(b"{}", url, status=500)
        with self.assertRaisesRegex(FetchError, "status"):
            fetch_all(self.manifest, self.destination, opener=self.opener)
        self.setUp()
        self.routes[url] = FakeResponse(b"[]", url, content_type="text/html")
        with self.assertRaisesRegex(FetchError, "content type"):
            fetch_all(self.manifest, self.destination, opener=self.opener)

    def test_bounds_json_headers_downloads_and_decoded_installer(self):
        source = self.manifest["sources"][0]
        url = f"https://api.github.com/repos/{source['repository']}/releases?per_page=100"
        cases = [
            FakeResponse(b"[]", url, headers={"X-Huge": "x" * 70_000}),
            FakeResponse(b"[" + b" " * (2 * 1024 * 1024) + b"]", url, content_length=False),
        ]
        for response in cases:
            with self.subTest(response=response):
                self.setUp()
                self.routes[url] = response
                with self.assertRaisesRegex(FetchError, "limit|header|large"):
                    fetch_all(self.manifest, self.destination, opener=self.opener)
        self.setUp()
        asset_url = next(url for url in self.routes if "/releases/assets/" in url)
        self.routes[asset_url] = FakeResponse(
            b"x" * (64 * 1024 * 1024 + 1), asset_url,
            content_type="application/octet-stream", content_length=False
        )
        with self.assertRaisesRegex(FetchError, "large"):
            fetch_all(self.manifest, self.destination, opener=self.opener)
        self.setUp()
        contents_url = next(url for url in self.routes if "/contents/" in url)
        contents = json.loads(self.routes[contents_url].body)
        contents["content"] = base64.b64encode(b"x" * (1024 * 1024 + 1)).decode()
        contents["size"] = 1024 * 1024 + 1
        self.routes[contents_url] = _json_response(contents, contents_url)
        with self.assertRaisesRegex(FetchError, "installer.*large"):
            fetch_all(self.manifest, self.destination, opener=self.opener)

    def test_timeout_and_partial_failure_preserve_existing_destination(self):
        self.destination.mkdir()
        (self.destination / "old").write_text("preserve")
        failing_url = next(url for url in self.routes if "/contents/" in url and "wattline" in url)
        self.routes[failing_url] = URLError("timed out")
        with self.assertRaisesRegex(FetchError, "network"):
            fetch_all(self.manifest, self.destination, opener=self.opener)
        self.assertEqual({path.name for path in self.destination.iterdir()}, {"old"})
        self.assertEqual((self.destination / "old").read_text(), "preserve")
        self.assertEqual(list(self.base.glob(".downloads.new-*")), [])

    def test_rejects_symlink_or_unsafe_destination_without_mutation(self):
        target = self.base / "target"
        target.mkdir()
        self.destination.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(FetchError, "destination"):
            fetch_all(self.manifest, self.destination, opener=self.opener)
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
