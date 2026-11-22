"""Bedrock pack inspection and native Windows import."""

from __future__ import annotations

import io
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from .core import MineKitError

MAX_NESTED = 128 * 1024 * 1024
MAX_MANIFEST = 1024 * 1024


@dataclass(frozen=True)
class Pack:
    name: str
    uuid: str
    version: str
    minimum_engine: str
    modules: tuple[str, ...]
    dependencies: tuple[str, ...]


def read_manifest(data: dict) -> Pack:
    header = data["header"]
    identifier = str(uuid.UUID(header["uuid"]))
    def version(value):
        return ".".join(map(str, value)) if isinstance(value, list) else str(value)
    return Pack(header.get("name", identifier), identifier, version(header["version"]),
                version(header.get("min_engine_version", "unknown")),
                tuple(m["type"] for m in data.get("modules", [])),
                tuple((str(uuid.UUID(d["uuid"])) if "uuid" in d else d["module_name"]) + " @ " + version(d.get("version", "unknown")) for d in data.get("dependencies", [])))


def inspect_pack(path: Path) -> list[Pack]:
    """Read manifests only, including .mcpack files embedded in .mcaddon."""
    if path.suffix.lower() not in (".mcpack", ".mcaddon", ".mcworld"):
        raise MineKitError("Select a Bedrock .mcpack, .mcaddon or .mcworld file.")
    found = []
    def scan(archive: ZipFile, nested_allowed: bool):
        for info in archive.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in info.filename or ":" in info.filename:
                raise MineKitError(f"Unsafe archive path: {info.filename}")
            if name.name == "manifest.json":
                if info.file_size > MAX_MANIFEST:
                    raise MineKitError("Manifest exceeds safety limit.")
                found.append(read_manifest(json.loads(archive.read(info))))
            elif nested_allowed and name.suffix.lower() == ".mcpack":
                if info.file_size > MAX_NESTED:
                    raise MineKitError("Nested pack exceeds safety limit.")
                with ZipFile(io.BytesIO(archive.read(info))) as nested:
                    scan(nested, False)
    with ZipFile(path) as archive:
        scan(archive, True)
        if not found and (path.suffix.lower() != ".mcworld" or "level.dat" not in archive.namelist()):
            raise MineKitError("No valid Bedrock pack manifest or world found.")
    return found


def dependency_report(packs: list[Pack]) -> list[str]:
    """Report bundled dependencies; do not pretend to resolve unknown UUIDs."""
    provided = {(p.uuid, p.version) for p in packs}
    notes = []
    for pack in packs:
        for dependency in pack.dependencies:
            identifier, _, version = dependency.partition(" @ ")
            if identifier.startswith("@minecraft/"):
                notes.append(f"{pack.name}: native Script API {dependency}; verify runtime support.")
            elif (identifier, version) not in provided:
                notes.append(f"{pack.name}: external dependency {dependency}; verify it is already installed.")
    return notes


def check_engine(packs: list[Pack], game_version: str) -> list[str]:
    """A minimum engine check is only a lower bound, not a runtime guarantee."""
    def version(value):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value):
            return None
        parts = tuple(map(int, value.split(".")))
        return parts + (0,) * (4 - len(parts))
    if not game_version.strip():
        return ["Game version not specified; minimum engine compatibility unverified."]
    installed = version(game_version.strip())
    if installed is None:
        raise MineKitError("Enter a numeric Bedrock game version such as 1.21.100.")
    notes = []
    for pack in packs:
        minimum = version(pack.minimum_engine)
        if minimum is None:
            notes.append(f"{pack.name}: minimum engine unknown.")
        elif installed < minimum:
            raise MineKitError(f"{pack.name} requires Bedrock {pack.minimum_engine} or newer; selected {game_version}.")
        else:
            notes.append(f"{pack.name}: minimum engine requirement satisfied; runtime compatibility not guaranteed.")
    return notes


def import_pack(path: Path, game_version: str = ""):
    """Use Minecraft's registered file handler, leaving world activation to it."""
    check_engine(inspect_pack(path), game_version)
    if sys.platform != "win32":
        raise MineKitError("Bedrock native import is supported on Windows only.")
    os.startfile(str(path.resolve()))  # type: ignore[attr-defined]
