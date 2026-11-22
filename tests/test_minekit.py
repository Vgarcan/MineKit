"""Offline regression tests: no network, launcher, or real game data needed."""

import hashlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from minekit.bedrock import dependency_report, inspect_pack
from minekit.core import MineKitError, StagedFile, discover_profiles, install_files, install_local, safe_filename
from minekit.modrinth import Download, Resolver, download_file


def version(pid, dependencies=(), game="1.21.1", loader="fabric", vid=None):
    return {"id": vid or pid + "v", "project_id": pid, "version_type": "release", "game_versions": [game], "loaders": [loader], "date_published": "2026-01-01", "dependencies": list(dependencies), "files": [{"filename": f"{pid}.jar", "url": f"https://cdn.modrinth.com/{pid}.jar", "primary": True, "size": 100, "hashes": {"sha512": "a" * 128}}]}


class FakeAPI:
    def __init__(self, versions):
        self.data = {v["project_id"]: v for v in versions}
    def project(self, pid):
        return {"id": pid, "title": pid, "client_side": "required"}
    def versions(self, pid, game, loader):
        return [self.data[pid]]
    def version(self, vid):
        return next(v for v in self.data.values() if v["id"] == vid)


def required(pid):
    return {"project_id": pid, "dependency_type": "required"}


class ResolverTests(unittest.TestCase):
    def test_transitive_dependency_deduplicated(self):
        api = FakeAPI([version("iris", [required("sodium"), required("api")]), version("sodium", [required("api")]), version("api")])
        plan = Resolver(api, "1.21.1", "fabric").resolve("iris", "mod")
        self.assertEqual([x.project_id for x in plan.downloads], ["api", "sodium", "iris"])

    def test_cycles_terminate(self):
        api = FakeAPI([version("a", [required("b")]), version("b", [required("a")])])
        self.assertEqual(len(Resolver(api, "1.21.1", "fabric").resolve("a", "mod").downloads), 2)

    def test_wrong_game_or_loader_rejected(self):
        for v in (version("a", game="1.20.1"), version("a", loader="forge")):
            with self.assertRaises(MineKitError):
                Resolver(FakeAPI([v]), "1.21.1", "fabric").resolve("a", "mod")

    def test_missing_dependency_rejected(self):
        api = FakeAPI([version("a", [{"dependency_type": "required", "file_name": "unknown.jar"}])])
        with self.assertRaises(MineKitError):
            Resolver(api, "1.21.1", "fabric").resolve("a", "mod")

    def test_declared_conflict_rejected(self):
        api = FakeAPI([version("a", [required("b"), {"project_id": "b", "dependency_type": "incompatible"}]), version("b")])
        with self.assertRaises(MineKitError):
            Resolver(api, "1.21.1", "fabric").resolve("a", "mod")

    def test_optional_not_installed(self):
        api = FakeAPI([version("a", [{"project_id": "b", "dependency_type": "optional"}])])
        plan = Resolver(api, "1.21.1", "fabric").resolve("a", "mod")
        self.assertEqual(len(plan.downloads), 1)
        self.assertTrue(plan.notes)

    def test_exact_dependency_pin(self):
        api = FakeAPI([version("a", [{"project_id": "b", "version_id": "b-pinned", "dependency_type": "required"}]), version("b", vid="b-pinned")])
        self.assertEqual(Resolver(api, "1.21.1", "fabric").resolve("a", "mod").downloads[0].version_id, "b-pinned")


class FileTests(unittest.TestCase):
    def test_unsafe_names(self):
        for name in ("../a", "a/b", "a\\b", "CON.jar", "x:stream", "a "):
            with self.assertRaises(MineKitError):
                safe_filename(name)

    def test_no_overwrite_and_identical_skip(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); source = p / "source.zip"; source.write_bytes(b"new")
            target = p / "out" / "pack.zip"
            self.assertEqual(install_files([StagedFile(source, target)]), [target])
            self.assertEqual(install_files([StagedFile(source, target)]), [])
            source.write_bytes(b"changed")
            with self.assertRaises(MineKitError):
                install_files([StagedFile(source, target)])
            self.assertEqual(target.read_bytes(), b"new")

    def test_preflight_does_not_partially_install(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); source = p / "source.zip"; source.write_bytes(b"new")
            conflict = p / "existing.zip"; conflict.write_bytes(b"old")
            first = p / "new.zip"
            with self.assertRaises(MineKitError):
                install_files([StagedFile(source, first), StagedFile(source, conflict)])
            self.assertFalse(first.exists())

    def test_duplicate_fabric_id(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); mods = p / "mods"; mods.mkdir()
            for file in (p / "new.jar", mods / "old.jar"):
                with ZipFile(file, "w") as archive:
                    archive.writestr("fabric.mod.json", json.dumps({"id": "duplicate"}))
            with self.assertRaises(MineKitError):
                install_files([StagedFile(p / "new.jar", mods / "new.jar")])

    def test_local_resource_pack(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); source = p / "texture.zip"
            with ZipFile(source, "w") as archive:
                archive.writestr("pack.mcmeta", "{}")
            self.assertEqual(len(install_local(source, p / "resourcepacks", "resourcepack")), 1)

    def test_hash_and_size_verification(self):
        data = io.BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr("fabric.mod.json", '{"id":"test"}')
        raw = data.getvalue()
        class Response(io.BytesIO):
            def geturl(self): return "https://cdn.modrinth.com/test.jar"
        with tempfile.TemporaryDirectory() as temp, patch("minekit.modrinth._open_cdn", side_effect=lambda *a, **k: Response(raw)):
            p = Path(temp)
            item = Download("a", "v", "A", "a.jar", "https://cdn.modrinth.com/test.jar", hashlib.sha512(raw).hexdigest(), len(raw), "mod")
            download_file(item, p / "good")
            bad = Download("a", "v", "A", "a.jar", item.url, "0" * 128, len(raw), "mod")
            with self.assertRaises(MineKitError):
                download_file(bad, p / "bad")

    def test_profile_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); v = "fabric-loader-0.16.0-1.21.1"
            (p / "launcher_profiles.json").write_text(json.dumps({"profiles": {"a": {"name": "Test", "lastVersionId": v}}}))
            folder = p / "versions" / v; folder.mkdir(parents=True)
            (folder / f"{v}.json").write_text(json.dumps({"inheritsFrom": "1.21.1"}))
            profile = discover_profiles(p)[0]
            self.assertEqual((profile.version, profile.loader), ("1.21.1", "fabric"))


class BedrockTests(unittest.TestCase):
    def manifest(self, name, identifier, deps=()):
        return {"header": {"name": name, "uuid": identifier, "version": [1, 0, 0]}, "modules": [{"type": "resources"}], "dependencies": list(deps)}

    def test_bundled_dependency(self):
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "addon.mcaddon"
            with ZipFile(p, "w") as archive:
                archive.writestr("A/manifest.json", json.dumps(self.manifest("A", a, [{"uuid": b, "version": [1, 0, 0]}])))
                archive.writestr("B/manifest.json", json.dumps(self.manifest("B", b)))
            packs = inspect_pack(p)
            self.assertEqual(len(packs), 2)
            self.assertEqual(dependency_report(packs), [])

    def test_missing_dependency_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "pack.mcpack"
            with ZipFile(p, "w") as archive:
                archive.writestr("manifest.json", json.dumps(self.manifest("A", str(uuid.uuid4()), [{"uuid": str(uuid.uuid4()), "version": [1, 0, 0]}])))
            self.assertIn("external dependency", dependency_report(inspect_pack(p))[0])

    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "pack.mcpack"
            with ZipFile(p, "w") as archive:
                archive.writestr("../manifest.json", "{}")
            with self.assertRaises(MineKitError):
                inspect_pack(p)


if __name__ == "__main__":
    unittest.main()
