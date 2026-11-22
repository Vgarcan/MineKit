"""Persistent, content-addressed download library. No game files are written
until the user explicitly installs a saved plan.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .bedrock import dependency_report, inspect_pack
from .core import MineKitError, StagedFile, install_files, safe_filename, sha512
from .modrinth import Download, FOLDERS, Plan, download_file, inspect_modpack, validate_archive


def default_library() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MineKit"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/MineKit"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "minekit"


@dataclass(frozen=True)
class Context:
    version: str
    loader: str
    shader_loader: str


@dataclass(frozen=True)
class SavedPlan:
    id: str
    title: str
    edition: str
    context: Context
    plan: Plan
    created: str


class Library:
    """SQLite contains metadata only; bytes are reused by their SHA-512 hash.

    Each operation opens its own SQLite connection, so a background download
    never shares a connection with Tkinter's UI thread. Library plans are
    immutable snapshots; the exact versions can be installed offline later.
    """

    def __init__(self, root: Path):
        self.root = root
        self.objects = root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.db = root / "library.db"
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    edition TEXT NOT NULL,
                    context TEXT NOT NULL,
                    manifest TEXT NOT NULL,
                    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS installations (
                    plan_id TEXT NOT NULL REFERENCES plans(id),
                    target TEXT NOT NULL,
                    installed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(plan_id, target)
                );
            """)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def object_path(self, digest: str) -> Path:
        if not re.fullmatch(r"[a-fA-F0-9]{128}", digest):
            raise MineKitError("Invalid library SHA-512 identifier.")
        return self.objects / digest.lower()

    def _checked_object(self, item: Download) -> Path:
        path = self.object_path(item.digest)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item.size or sha512(path) != item.digest.lower():
            raise MineKitError(f"Missing or corrupted library file: {item.filename}. Review the library before retrying.")
        return path

    def _publish(self, source: Path, item: Download):
        target = self.object_path(item.digest)
        if target.exists() or target.is_symlink():
            self._checked_object(item)
            return
        if source.stat().st_size != item.size or sha512(source) != item.digest.lower():
            raise MineKitError(f"Integrity verification failed for {item.filename}.")
        # Source and destination are on the same filesystem; atomically link
        # the verified temporary file into content storage without overwriting.
        try:
            os.link(source, target)
        except FileExistsError:
            self._checked_object(item)

    def _register(self, title: str, edition: str, context: Context, plan: Plan) -> SavedPlan:
        manifest = json.dumps({"downloads": [asdict(item) for item in plan.downloads], "notes": list(plan.notes)}, sort_keys=True)
        settings = json.dumps(asdict(context), sort_keys=True)
        identifier = hashlib.sha256(json.dumps([edition, settings, manifest], ensure_ascii=False).encode()).hexdigest()
        with self._connection() as conn:
            conn.execute("INSERT OR IGNORE INTO plans(id,title,edition,context,manifest) VALUES(?,?,?,?,?)", (identifier, title, edition, settings, manifest))
        return self.get(identifier)

    def save(self, plan: Plan, context: Context, title: str, progress=lambda _: None) -> SavedPlan:
        if not plan.downloads:
            raise MineKitError("Cannot save an empty plan.")
        notes = list(plan.notes)
        with tempfile.TemporaryDirectory(prefix="staging-", dir=self.root) as staging:
            for index, item in enumerate(plan.downloads):
                path = self.object_path(item.digest)
                if path.exists() or path.is_symlink():
                    self._checked_object(item)
                    progress(f"Reusing {item.title}")
                    validate_archive(item, path)
                else:
                    progress(f"Downloading {item.title} ({index+1}/{len(plan.downloads)})")
                    staged = Path(staging) / str(index)
                    download_file(item, staged)
                    self._publish(staged, item)
                if item.kind == "modpack":
                    notes.append(inspect_modpack(path, context.version, context.loader))
        # A partial download can leave a reusable verified blob behind, but
        # it never creates a plan that appears ready to install.
        return self._register(title, "Java", context, Plan(plan.downloads, tuple(notes)))

    def save_bedrock(self, source: Path) -> SavedPlan:
        """Store a validated user-supplied Bedrock file, not Marketplace content."""
        with tempfile.TemporaryDirectory(prefix="staging-", dir=self.root) as staging:
            temp = Path(staging) / safe_filename(source.name)
            shutil.copyfile(source, temp)
            packs = inspect_pack(temp)
            digest = sha512(temp)
            item = Download("local", digest, source.stem, temp.name, "", digest, temp.stat().st_size, "bedrock")
            self._publish(temp, item)
            context = Context("Bedrock / inspect minimum engine in manifest", "native", "native")
            plan = Plan((item,), tuple(dependency_report(packs)))
        return self._register(source.stem, "Bedrock", context, plan)

    def _decode(self, row) -> SavedPlan:
        manifest = json.loads(row["manifest"])
        plan = Plan(tuple(Download(**item) for item in manifest["downloads"]), tuple(manifest["notes"]))
        return SavedPlan(row["id"], row["title"], row["edition"], Context(**json.loads(row["context"])), plan, row["created"])

    def get(self, identifier: str) -> SavedPlan:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM plans WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise MineKitError("Library entry not found.")
        return self._decode(row)

    def list(self) -> list[SavedPlan]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM plans ORDER BY created DESC, id").fetchall()
        return [self._decode(row) for row in rows]

    def installed_on(self, identifier: str) -> list[str]:
        """Historical install targets; this is not a live compatibility claim."""
        with self._connection() as conn:
            rows = conn.execute("SELECT target FROM installations WHERE plan_id=? ORDER BY installed DESC", (identifier,)).fetchall()
        return [row["target"] for row in rows]

    def install(self, saved: SavedPlan, context: Context, game_dir: Path, world_dir: Path | None = None, progress=lambda _: None) -> list[Path]:
        if saved.edition != "Java":
            raise MineKitError("Bedrock plans must use native import.")
        if saved.context != context:
            raise MineKitError("The saved plan targets a different game version or loader. Select the matching profile.")
        if any(item.kind not in FOLDERS for item in saved.plan.downloads):
            raise MineKitError("Complete modpacks are download-only in this release. Do not copy them into mods/.")
        if not game_dir.is_dir():
            raise MineKitError("Select an existing Java game directory.")
        files = []
        for item in saved.plan.downloads:
            progress(f"Checking {item.title}")
            source = self._checked_object(item)
            if item.kind == "datapack":
                if not world_dir or not (world_dir / "level.dat").is_file():
                    raise MineKitError("Data packs need a world containing level.dat.")
                destination = world_dir / "datapacks"
            else:
                destination = game_dir / FOLDERS[item.kind]
            files.append(StagedFile(source, destination / safe_filename(item.filename)))
        created = install_files(files)
        target = str(game_dir.resolve()) + (f" / world {world_dir.resolve()}" if world_dir else "")
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO installations(plan_id,target) VALUES(?,?)", (saved.id, target))
        return created

    def export(self, saved: SavedPlan, folder: Path) -> list[Path]:
        """Materialize original filenames, preserving immutable library bytes."""
        files = [StagedFile(self._checked_object(item), folder / safe_filename(item.filename)) for item in saved.plan.downloads]
        return install_files(files)

    def bedrock_file(self, saved: SavedPlan) -> Path:
        if saved.edition != "Bedrock" or len(saved.plan.downloads) != 1:
            raise MineKitError("Select a saved Bedrock file.")
        folder = self.root / "imports" / saved.id
        self.export(saved, folder)
        return folder / saved.plan.downloads[0].filename
