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
        self.commits = {}
        asset_id = 100
        for source in self.manifest["sources"]:
            repository = source["repository"]
            product = source["product"]
            tag = f"v1.0.0-{product}"
            commit = f"{asset_id:040x}"
            self.tags[product] = tag
            self.commits[product] = commit
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
            releases_url = f"https://api.github.com/repos/{repository}/releases/latest"
            release = {
                "tag_name": tag, "draft": False, "prerelease": False,
                "immutable": False, "assets": assets,
            }
            self.routes[releases_url] = _json_response(release, releases_url)
            encoded_tag = tag.replace("+", "%2B").replace("/", "%2F")
            ref_url = f"https://api.github.com/repos/{repository}/git/ref/tags/{encoded_tag}"
            ref_value = {
                "ref": f"refs/tags/{tag}",
                "object": {
                    "type": "commit", "sha": commit,
                    "url": f"https://api.github.com/repos/{repository}/git/commits/{commit}",
                },
            }
            self.routes[ref_url] = lambda _request, value=ref_value, url=ref_url: _json_response(value, url)
            source_path = "/".join(part.replace(" ", "%20") for part in source["installer_source"].split("/"))
            contents_url = (
                f"https://api.github.com/repos/{repository}/contents/{source_path}?ref={commit}"
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

    def test_fetches_documented_latest_release_and_commit_pinned_installer(self):
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
        requested = [request.full_url for request, _ in self.opener.requests]
        self.assertFalse(any("?per_page=" in url for url in requested))
        self.assertTrue(all(requested.count(
            f"https://api.github.com/repos/{source['repository']}/git/ref/tags/{self.tags[source['product']]}"
        ) == 2 for source in self.manifest["sources"]))

    def test_percent_encodes_tag_as_one_exact_ref(self):
        source = self.manifest["sources"][2]
        repository = source["repository"]
        releases_url = f"https://api.github.com/repos/{repository}/releases/latest"
        release = json.loads(self.routes[releases_url].body)
        release["tag_name"] = "v1.0+build/one"
        self.routes[releases_url] = _json_response(release, releases_url)
        old_ref = next(url for url in self.routes if f"repos/{repository}/git/ref/" in url)
        response = self.routes.pop(old_ref)
        expected = old_ref.rsplit("/", 1)[0] + "/v1.0%2Bbuild%2Fone"
        ref_value = response(None)
        ref_json = json.loads(ref_value.body)
        ref_json["ref"] = "refs/tags/v1.0+build/one"
        self.routes[expected] = lambda _request, value=ref_json, url=expected: _json_response(value, url)
        fetch_all(self.manifest, self.destination, opener=self.opener)
        self.assertIn(expected, [request.full_url for request, _ in self.opener.requests])
        contents = [url for url in (request.full_url for request, _ in self.opener.requests)
                    if f"repos/{repository}/contents/" in url]
        self.assertEqual(contents[0].split("?ref=", 1)[1], self.commits["speedtest"])

    def test_rejects_missing_duplicate_or_unexpected_package_assets(self):
        source = self.manifest["sources"][2]
        url = f"https://api.github.com/repos/{source['repository']}/releases/latest"
        release = json.loads(self.routes[url].body)
        cases = [
            [],
            release["assets"] + [dict(
                release["assets"][0], id=999,
                url=f"https://api.github.com/repos/{source['repository']}/releases/assets/999",
            )],
            release["assets"] + [{"id": 999, "name": "evil_1_all.ipk",
                                   "url": f"https://api.github.com/repos/{source['repository']}/releases/assets/999"}],
            release["assets"] + [{"id": 999, "name": "release-notes.txt",
                                   "url": f"https://api.github.com/repos/{source['repository']}/releases/assets/999"}],
        ]
        for assets in cases:
            with self.subTest(assets=assets):
                self.setUp()
                self.routes[url] = _json_response(dict(release, assets=assets), url)
                with self.assertRaises(FetchError):
                    fetch_all(self.manifest, self.destination, opener=self.opener)

    def test_latest_endpoint_rejects_draft_prerelease_and_malformed_immutable_flag(self):
        source = self.manifest["sources"][0]
        url = f"https://api.github.com/repos/{source['repository']}/releases/latest"
        release = json.loads(self.routes[url].body)
        for changes in ({"draft": True}, {"prerelease": True}, {"immutable": "false"}):
            with self.subTest(changes=changes):
                self.setUp()
                self.routes[url] = _json_response(dict(release, **changes), url)
                with self.assertRaisesRegex(FetchError, "stable|immutable"):
                    fetch_all(self.manifest, self.destination, opener=self.opener)

    def test_latest_endpoint_is_independent_of_more_than_100_release_history(self):
        source = self.manifest["sources"][0]
        listing_url = f"https://api.github.com/repos/{source['repository']}/releases?per_page=100"
        historical = [
            {"tag_name": f"v0.{index}", "draft": False, "prerelease": True, "assets": []}
            for index in range(100)
        ]
        historical.append({
            "tag_name": "would-be-page-two", "draft": False,
            "prerelease": False, "assets": [],
        })
        self.routes[listing_url] = _json_response(historical, listing_url)
        fetch_all(self.manifest, self.destination, opener=self.opener)
        self.assertNotIn(listing_url, [request.full_url for request, _ in self.opener.requests])

    def test_resolves_lightweight_and_nested_annotated_tags_to_commit(self):
        source = self.manifest["sources"][0]
        repository = source["repository"]
        tag = self.tags["starwatch"]
        ref_url = f"https://api.github.com/repos/{repository}/git/ref/tags/{tag}"
        first_tag = "a" * 40
        second_tag = "b" * 40
        commit = "c" * 40
        ref_value = {
            "ref": f"refs/tags/{tag}", "object": {
                "type": "tag", "sha": first_tag,
                "url": f"https://api.github.com/repos/{repository}/git/tags/{first_tag}",
            },
        }
        self.routes[ref_url] = lambda _request: _json_response(ref_value, ref_url)
        first_url = f"https://api.github.com/repos/{repository}/git/tags/{first_tag}"
        second_url = f"https://api.github.com/repos/{repository}/git/tags/{second_tag}"
        first_value = {"sha": first_tag, "object": {
            "type": "tag", "sha": second_tag,
            "url": second_url,
        }}
        second_value = {"sha": second_tag, "object": {
            "type": "commit", "sha": commit,
            "url": f"https://api.github.com/repos/{repository}/git/commits/{commit}",
        }}
        self.routes[first_url] = lambda _request: _json_response(first_value, first_url)
        self.routes[second_url] = lambda _request: _json_response(second_value, second_url)
        old_contents = next(url for url in self.routes if f"repos/{repository}/contents/" in url)
        contents_response = self.routes.pop(old_contents)
        new_contents = old_contents.split("?", 1)[0] + f"?ref={commit}"
        self.routes[new_contents] = FakeResponse(contents_response.body, new_contents)
        fetch_all(self.manifest, self.destination, opener=self.opener)
        requested = [request.full_url for request, _ in self.opener.requests]
        self.assertEqual(requested.count(first_url), 2)
        self.assertEqual(requested.count(second_url), 2)
        self.assertIn(new_contents, requested)

    def test_rejects_tag_cycles_and_wrong_object_types(self):
        source = self.manifest["sources"][0]
        repository = source["repository"]
        tag = self.tags["starwatch"]
        ref_url = f"https://api.github.com/repos/{repository}/git/ref/tags/{tag}"
        tag_sha = "d" * 40
        tag_url = f"https://api.github.com/repos/{repository}/git/tags/{tag_sha}"
        cases = [
            {"type": "tree", "sha": tag_sha,
             "url": f"https://api.github.com/repos/{repository}/git/trees/{tag_sha}"},
            {"type": "tag", "sha": tag_sha, "url": tag_url},
        ]
        for index, target in enumerate(cases):
            with self.subTest(index=index):
                self.setUp()
                ref_value = {"ref": f"refs/tags/{tag}", "object": target}
                self.routes[ref_url] = lambda _request, value=ref_value: _json_response(value, ref_url)
                if target["type"] == "tag":
                    cycle = {"sha": tag_sha, "object": target}
                    self.routes[tag_url] = lambda _request, value=cycle: _json_response(value, tag_url)
                with self.assertRaisesRegex(FetchError, "tag|object|cycle"):
                    fetch_all(self.manifest, self.destination, opener=self.opener)

    def test_tag_movement_after_fetch_rolls_back_all_products(self):
        self.destination.mkdir()
        (self.destination / "old").write_text("preserve")
        source = self.manifest["sources"][0]
        repository = source["repository"]
        tag = self.tags["starwatch"]
        original = self.commits["starwatch"]
        moved = "f" * 40
        ref_url = f"https://api.github.com/repos/{repository}/git/ref/tags/{tag}"
        calls = 0

        def moving_ref(_request):
            nonlocal calls
            calls += 1
            commit = original if calls == 1 else moved
            value = {"ref": f"refs/tags/{tag}", "object": {
                "type": "commit", "sha": commit,
                "url": f"https://api.github.com/repos/{repository}/git/commits/{commit}",
            }}
            return _json_response(value, ref_url)

        self.routes[ref_url] = moving_ref
        with self.assertRaisesRegex(FetchError, "moved"):
            fetch_all(self.manifest, self.destination, opener=self.opener)
        self.assertEqual(calls, 2)
        self.assertEqual((self.destination / "old").read_text(), "preserve")
        self.assertEqual(list(self.base.glob(".downloads.new-*")), [])

    def test_rejects_malformed_duplicate_json_fields_and_unsafe_names(self):
        source = self.manifest["sources"][0]
        url = f"https://api.github.com/repos/{source['repository']}/releases/latest"
        bad_bodies = [
            b'[]',
            b'{"tag_name":"v1","tag_name":"v2","draft":false,"prerelease":false,"assets":[]}',
            b'{"tag_name":"v1","draft":false,"prerelease":false,"assets":'
            b'[{"id":1,"name":"../escape.ipk","url":"https://api.github.com/repos/keithah/openwrt-starwatch/releases/assets/1"}]}',
        ]
        for body in bad_bodies:
            with self.subTest(body=body):
                self.setUp()
                self.routes[url] = FakeResponse(body, url)
                with self.assertRaises(FetchError):
                    fetch_all(self.manifest, self.destination, opener=self.opener)

    def test_rejects_control_characters_even_in_ignored_asset_names(self):
        source = self.manifest["sources"][0]
        url = f"https://api.github.com/repos/{source['repository']}/releases/latest"
        release = json.loads(self.routes[url].body)
        ancillary = {
            "id": 999, "name": "notes\n.txt",
            "url": f"https://api.github.com/repos/{source['repository']}/releases/assets/999",
        }
        self.routes[url] = _json_response(dict(release, assets=[*release["assets"], ancillary]), url)
        with self.assertRaisesRegex(FetchError, "unexpected|asset"):
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
        url = f"https://api.github.com/repos/{source['repository']}/releases/latest"
        release = json.loads(self.routes[url].body)
        invalid = dict(release["assets"][0], url="https://github.com/keithah/file.ipk")
        self.routes[url] = _json_response(dict(release, assets=[invalid, *release["assets"][1:]]), url)
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
        url = f"https://api.github.com/repos/{source['repository']}/releases/latest"
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
