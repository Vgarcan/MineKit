"""Regression tests for MineKit v0.2. All providers and game paths are isolated."""

import hashlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from minekit.bedrock import Pack, check_engine
from minekit.core import MineKitError
from minekit.library import Context, Library
from minekit.modrinth import Download, Modrinth, Plan, Resolver, _trusted_cdn, inspect_modpack
from test_minekit import FakeAPI, required, version


def archive_bytes(entries):
    memory = io.BytesIO()
    with ZipFile(memory, "w") as archive:
        for filename, contents in entries.items():
            archive.writestr(filename, contents)
    return memory.getvalue()


class CatalogTests(unittest.TestCase):
    def test_search_facets_and_pagination(self):
        api = Modrinth()
        with patch.object(api, "get", return_value={"hits": [{"project_id": "abc"}], "total_hits": 100}) as get:
            page = api.search("", "mod", "1.21.1", "fabric", offset=24, sort="downloads", category="optimization")
        self.assertEqual((page.offset, page.total, page.limit), (24, 100, 24))
        self.assertEqual(get.call_args.args, ("/search",))
        params = get.call_args.kwargs
        self.assertEqual(params["index"], "downloads")
        self.assertEqual(json.loads(params["facets"]), [["all_project_types:mod"], ["versions:1.21.1"], ["categories:fabric"], ["categories:optimization"]])
        self.assertEqual(params["query"], "")

    def test_versions_only_release(self):
        with patch.object(Modrinth, "get", return_value=[{"version": "1.21.1", "version_type": "release"}, {"version": "snapshot", "version_type": "snapshot"}]):
            self.assertEqual(Modrinth().game_versions(), ["1.21.1"])

    def test_multi_root_deduplication(self):
        api = FakeAPI([version("iris", [required("api")]), version("sodium", [required("api")]), version("api")])
        plan = Resolver(api, "1.21.1", "fabric").resolve_many([("iris", "mod", None), ("sodium", "mod", None)])
        self.assertEqual({x.project_id for x in plan.downloads}, {"api", "iris", "sodium"})
        self.assertEqual(len(plan.downloads), 3)

    def test_multi_root_conflicts(self):
        api = FakeAPI([version("a", [{"project_id": "b", "dependency_type": "incompatible"}]), version("b")])
        with self.assertRaises(MineKitError):
            Resolver(api, "1.21.1", "fabric").resolve_many([("a", "mod", None), ("b", "mod", None)])

    def test_explicit_pre_release_version(self):
        selected = version("test", vid="beta")
        selected["version_type"] = "beta"
        resolver = Resolver(FakeAPI([selected]), "1.21.1", "fabric")
        with self.assertRaises(MineKitError):
            resolver.resolve("test", "mod")
        plan = resolver.resolve_many([("test", "mod", "beta")])
        self.assertEqual(plan.downloads[0].version_id, "beta")

    def test_modpack_does_not_resolve_loose_dependencies(self):
        packed = version("pack", [required("unknown")])
        packed["files"][0]["filename"] = "test.mrpack"
        plan = Resolver(FakeAPI([packed]), "1.21.1", "fabric").resolve("pack", "modpack")
        self.assertEqual([x.kind for x in plan.downloads], ["modpack"])

    def test_resource_and_datapack_filter(self):
        for kind, loader in (("resourcepack", "minecraft"), ("datapack", "datapack")):
            v = version("pack", loader=loader); v["files"][0]["filename"] = "pack.zip"
            self.assertEqual(Resolver(FakeAPI([v]), "1.21.1", "fabric").resolve("pack", kind).downloads[0].kind, kind)

    def test_untrusted_download_host_rejected(self):
        for url in ("http://cdn.modrinth.com/a", "https://cdn.modrinth.com.evil.test/a", "https://x@cdn.modrinth.com/a", "https://cdn.modrinth.com:444/a"):
            self.assertFalse(_trusted_cdn(url))
        self.assertTrue(_trusted_cdn("https://cdn.modrinth.com/a"))


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = Library(self.root / "library")
        self.context = Context("1.21.1", "fabric", "iris")
        self.data = archive_bytes({"fabric.mod.json": '{"id":"sodium"}'})
        self.item = Download("sodium", "v1", "Sodium", "sodium.jar", "https://cdn.modrinth.com/sodium.jar", hashlib.sha512(self.data).hexdigest(), len(self.data), "mod", "1.0")
        self.plan = Plan((self.item,), ())

    def tearDown(self):
        self.temp.cleanup()

    def download(self, item, target):
        target.write_bytes(self.data)

    def save(self):
        with patch("minekit.library.download_file", side_effect=self.download):
            return self.library.save(self.plan, self.context, "Sodium")

    def test_saved_plan_reuses_cached_bytes_and_is_persistent(self):
        saved = self.save()
        with patch("minekit.library.download_file") as download:
            second = Library(self.library.root).save(self.plan, self.context, "Sodium")
        download.assert_not_called()
        self.assertEqual(saved.id, second.id)
        self.assertEqual(len(list(self.library.objects.iterdir())), 1)
        self.assertEqual(len(self.library.list()), 1)

    def test_save_does_not_modify_game_and_offline_install_reuses_plan(self):
        game = self.root / "game"; game.mkdir()
        saved = self.save()
        self.assertEqual(list(game.iterdir()), [])
        with patch("minekit.library.download_file") as download:
            result = self.library.install(saved, self.context, game)
            again = self.library.install(saved, self.context, game)
        download.assert_not_called()
        self.assertEqual(result, [game / "mods/sodium.jar"])
        self.assertEqual(again, [])
        self.assertEqual(result[0].read_bytes(), self.data)
        self.assertEqual(self.library.installed_on(saved.id), [str(game.resolve())])

    def test_mismatched_context_never_writes_game(self):
        game = self.root / "game"; game.mkdir()
        saved = self.save()
        with self.assertRaises(MineKitError):
            self.library.install(saved, Context("1.20.1", "forge", "iris"), game)
        self.assertEqual(list(game.iterdir()), [])

    def test_cache_corruption_never_installs(self):
        saved = self.save()
        self.library.object_path(self.item.digest).write_bytes(b"corrupt")
        game = self.root / "game"; game.mkdir()
        with self.assertRaises(MineKitError):
            self.library.install(saved, self.context, game)
        self.assertEqual(list(game.iterdir()), [])

    def test_failed_download_does_not_register_plan(self):
        with patch("minekit.library.download_file", side_effect=OSError("network failure")), self.assertRaises(OSError):
            self.library.save(self.plan, self.context, "Sodium")
        self.assertEqual(self.library.list(), [])

    def test_hash_verified_even_if_download_helper_misbehaves(self):
        with patch("minekit.library.download_file", side_effect=lambda item, target: target.write_bytes(b"bad")), self.assertRaises(MineKitError):
            self.library.save(self.plan, self.context, "Sodium")
        self.assertEqual(self.library.list(), [])

    def test_export_restores_original_filename(self):
        saved = self.save()
        export = self.root / "export"
        self.assertEqual(self.library.export(saved, export), [export / self.item.filename])
        self.assertEqual((export / self.item.filename).read_bytes(), self.data)

    def test_bedrock_save_and_materialize(self):
        manifest = {"header": {"name": "Example", "uuid": str(uuid.uuid4()), "version": [1, 0, 0], "min_engine_version": [1, 21, 0]}, "modules": [{"type": "resources"}]}
        pack = self.root / "example.mcpack"
        pack.write_bytes(archive_bytes({"manifest.json": json.dumps(manifest)}))
        saved = self.library.save_bedrock(pack)
        self.assertEqual(saved.edition, "Bedrock")
        self.assertEqual(self.library.bedrock_file(saved).read_bytes(), pack.read_bytes())

    def test_modpack_download_only(self):
        data = archive_bytes({"modrinth.index.json": json.dumps({"formatVersion": 1, "game": "minecraft", "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16.0"}, "files": []})})
        item = Download("pack", "v1", "Pack", "pack.mrpack", "", hashlib.sha512(data).hexdigest(), len(data), "modpack")
        with patch("minekit.library.download_file", side_effect=lambda i, target: target.write_bytes(data)):
            saved = self.library.save(Plan((item,), ()), self.context, "Pack")
        game = self.root / "game"; game.mkdir()
        with self.assertRaises(MineKitError):
            self.library.install(saved, self.context, game)
        self.assertEqual(list(game.iterdir()), [])
        self.assertIn("Modpack manifest", saved.plan.notes[-1])
        with self.assertRaises(MineKitError):
            inspect_modpack(self.library.object_path(item.digest), "1.20.1", "fabric")


class BedrockEngineTests(unittest.TestCase):
    def test_numeric_minimum_engine(self):
        pack = Pack("Example", str(uuid.uuid4()), "1.0.0", "1.21.100", ("resources",), ())
        with self.assertRaises(MineKitError):
            check_engine([pack], "1.21.99")
        self.assertIn("satisfied", check_engine([pack], "1.21.100")[0])
        self.assertIn("unverified", check_engine([pack], "")[0])


if __name__ == "__main__":
    unittest.main()
