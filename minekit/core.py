"""Shared filesystem discovery and conservative installation primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile


class MineKitError(Exception):
    """An actionable failure to show the user."""


@dataclass(frozen=True)
class Profile:
    name: str
    directory: Path
    version: str
    loader: str


def java_root() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / ".minecraft"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/minecraft"
    return Path.home() / ".minecraft"


def discover_profiles(root: Path) -> list[Profile]:
    """Read the official launcher's configuration without modifying it."""
    profiles = []
    for filename in ("launcher_profiles.json", "launcher_profiles_microsoft_store.json"):
        try:
            data = json.loads((root / filename).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for profile in data.get("profiles", {}).values():
            version_id = profile.get("lastVersionId", "")
            if not version_id or not re.fullmatch(r"[A-Za-z0-9_.+\-]+", version_id) or version_id in (".", ".."):
                continue
            loader = next((x for x in ("neoforge", "fabric", "forge", "quilt") if x in version_id.lower()), "vanilla")
            try:
                metadata = json.loads((root / "versions" / version_id / f"{version_id}.json").read_text(encoding="utf-8"))
                version = metadata.get("inheritsFrom", version_id)
            except (OSError, ValueError):
                version = version_id if loader == "vanilla" else ""
            folder = Path(profile.get("gameDir") or root).expanduser()
            item = Profile(profile.get("name") or version_id, folder, version, loader)
            if item not in profiles:
                profiles.append(item)
    return profiles


def bedrock_roots() -> list[Path]:
    """Recognize both current GDK and older UWP Windows storage layouts."""
    roaming = Path(os.environ.get("APPDATA", Path.home()))
    local = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    candidates = [roaming / "Minecraft Bedrock/users/shared/games/com.mojang",
                  local / "Packages/Microsoft.MinecraftUWP_8wekyb3d8bbwe/LocalState/games/com.mojang"]
    return [path for path in candidates if path.is_dir()]


def safe_filename(name: str) -> str:
    """Reject path traversal, NTFS streams, and Windows device names."""
    if (not name or name in (".", "..") or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or name.endswith((".", " ")) or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", name.split(".")[0])):
        raise MineKitError(f"Unsafe filename: {name!r}")
    return name


def sha512(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha512").hexdigest()


def fabric_id(path: Path) -> str | None:
    """Inspect a Fabric JAR as data, without executing Java code."""
    try:
        with ZipFile(path) as archive:
            info = archive.getinfo("fabric.mod.json")
            if info.file_size > 1024 * 1024:
                raise MineKitError("Oversized Fabric metadata")
            return json.loads(archive.read(info)).get("id")
    except KeyError:
        return None


@dataclass(frozen=True)
class StagedFile:
    source: Path
    destination: Path


def install_files(files: list[StagedFile]) -> list[Path]:
    """Install verified files without overwrites; undo only files we create.

    Existing identical files are skipped. A different file at the same path
    blocks the entire operation. All checks precede any filesystem writes.
    """
    pending = []
    by_destination = set()
    mod_ids: dict[tuple[Path, str], Path] = {}
    for item in files:
        dest = item.destination
        safe_filename(dest.name)
        key = (dest.parent.resolve(), dest.name.casefold())
        if key in by_destination:
            raise MineKitError(f"Duplicate destination: {dest}")
        by_destination.add(key)
        if dest.is_symlink():
            raise MineKitError(f"Refusing symbolic link: {dest}")
        if dest.exists():
            if dest.is_file() and sha512(dest) == sha512(item.source):
                continue
            raise MineKitError(f"Existing file would be overwritten: {dest.name}")
        if dest.suffix.lower() == ".jar":
            mod_id = fabric_id(item.source)
            if mod_id:
                for existing in dest.parent.glob("*.jar"):
                    if fabric_id(existing) == mod_id:
                        raise MineKitError(f"Fabric mod {mod_id} already exists as {existing.name}; review manually.")
                mod_key = (dest.parent.resolve(), mod_id)
                if mod_key in mod_ids:
                    raise MineKitError(f"Duplicate Fabric mod ID: {mod_id}")
                mod_ids[mod_key] = dest
        pending.append(item)
    created = []
    try:
        for item in pending:
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation protects against late collisions too.
            with item.destination.open("xb") as output:
                created.append(item.destination)
                with item.source.open("rb") as source:
                    shutil.copyfileobj(source, output)
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    return created


def install_local(source: Path, directory: Path, kind: str) -> list[Path]:
    """Copy a user-selected archive into its explicitly chosen destination."""
    expected = ".jar" if kind == "mod" else ".zip"
    if source.suffix.lower() != expected:
        raise MineKitError(f"Expected a {expected} file for {kind}.")
    with ZipFile(source) as archive:
        names = archive.namelist()
        if kind == "resourcepack" and "pack.mcmeta" not in names:
            raise MineKitError("Java resource pack must contain pack.mcmeta at its root.")
        if kind == "datapack" and ("pack.mcmeta" not in names or not any(x.startswith("data/") for x in names)):
            raise MineKitError("Java data pack must contain pack.mcmeta and data/.")
        if kind == "shader" and not any(x.startswith("shaders/") for x in names):
            raise MineKitError("Shader ZIP must contain a shaders/ directory at its root.")
    # Stage first so a changing source file cannot be copied directly into the game.
    with tempfile.TemporaryDirectory(prefix="minekit-") as staging:
        staged = Path(staging) / safe_filename(source.name)
        shutil.copyfile(source, staged)
        return install_files([StagedFile(staged, directory / source.name)])
