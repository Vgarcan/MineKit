"""Opt-in end-to-end check of public API + CDN; never touches real game files."""

import os
import tempfile
import unittest
from pathlib import Path

from minekit.library import Context, Library
from minekit.modrinth import Modrinth, Resolver


@unittest.skipUnless(os.environ.get("MINEKIT_LIVE_TEST") == "1", "Opt-in public network test")
class LiveModrinthTests(unittest.TestCase):
    def test_search_resolve_and_cache_sodium(self):
        api = Modrinth()
        context = Context("1.21.1", "fabric", "iris")
        page = api.search("sodium", "mod", context.version, context.loader)
        self.assertTrue(page.hits)
        project = api.project("sodium")
        self.assertTrue(any(hit["project_id"] == project["id"] for hit in page.hits))
        plan = Resolver(api, context.version, context.loader, context.shader_loader).resolve(project["id"], "mod")
        with tempfile.TemporaryDirectory() as directory:
            library = Library(Path(directory))
            saved = library.save(plan, context, "Sodium live test")
            self.assertTrue(saved.plan.downloads)
            self.assertTrue(all(library.object_path(item.digest).is_file() for item in saved.plan.downloads))
            self.assertEqual(library.save(plan, context, "Sodium live test").id, saved.id)
