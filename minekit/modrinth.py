"""Modrinth public API client and dependency-aware installation planner."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from zipfile import ZipFile

from .core import MineKitError, StagedFile, install_files, safe_filename

BASE = "https://api.modrinth.com/v2"
USER_AGENT = "Vgarcan/MineKit/0.1.0 (https://github.com/Vgarcan/MineKit)"
FOLDERS = {"mod": "mods", "resourcepack": "resourcepacks", "shader": "shaderpacks", "datapack": "datapacks"}
MAX_FILE = 512 * 1024 * 1024


class Modrinth:
    def get(self, route: str, **params):
        url = BASE + route
        if params:
            url += "?" + urlencode(params)
        with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as response:
            return json.load(response)

    def search(self, query: str, kind: str, version: str, loader: str):
        facets = [[f"all_project_types:{kind}"], [f"versions:{version}"]]
        if kind in ("mod", "shader"):
            facets.append([f"categories:{loader}"])
        return self.get("/search", query=query, facets=json.dumps(facets), limit=40)["hits"]

    def project(self, project_id: str):
        return self.get(f"/project/{quote(project_id, safe='')}")

    def versions(self, project_id: str, version: str, loader: str | None):
        params = {"game_versions": json.dumps([version])}
        if loader:
            params["loaders"] = json.dumps([loader])
        return self.get(f"/project/{quote(project_id, safe='')}/version", **params)

    def version(self, version_id: str):
        return self.get(f"/version/{quote(version_id, safe='')}")


@dataclass(frozen=True)
class Download:
    project_id: str
    version_id: str
    title: str
    filename: str
    url: str
    digest: str
    size: int
    kind: str


@dataclass(frozen=True)
class Plan:
    downloads: tuple[Download, ...]
    notes: tuple[str, ...]


class Resolver:
    """Resolve required metadata dependencies; fail closed on ambiguous pins.

    The resolver chooses newest stable versions and respects exact pins. It does
    not guess missing projects or silently switch loader/game version. It is
    intentionally not a complete SAT solver for arbitrary mod ecosystems.
    """

    def __init__(self, api: Modrinth, version: str, loader: str, shader_loader: str = "iris"):
        self.api, self.game, self.loader, self.shader_loader = api, version, loader, shader_loader

    def resolve(self, project_id: str, kind: str) -> Plan:
        selected = {}
        downloads = {}
        incompatible = []
        notes = []

        def visit(pid: str | None, expected_kind: str, pin: str | None = None):
            exact = self.api.version(pin) if pin else None
            if exact:
                if pid and pid != exact["project_id"]:
                    raise MineKitError("Dependency project and version do not match.")
                pid = exact["project_id"]
            if not pid:
                raise MineKitError("Required dependency has no downloadable Modrinth project.")
            project = self.api.project(pid)
            pid = project["id"]
            if pid in selected:
                if pin and selected[pid]["id"] != pin:
                    raise MineKitError(f"Conflicting version requirements for {project['title']}.")
                return
            if len(selected) >= 100:
                raise MineKitError("Dependency graph exceeds the safety limit of 100 projects.")
            if expected_kind == "mod" and project.get("client_side") == "unsupported":
                raise MineKitError(f"{project['title']} is not intended for Java clients.")
            loader = self.loader if expected_kind == "mod" else self.shader_loader if expected_kind == "shader" else None
            if loader == "vanilla":
                raise MineKitError("Mods need Fabric, Forge, NeoForge or Quilt; choose a modded profile.")
            candidates = [exact] if exact else self.api.versions(pid, self.game, loader)
            candidates = [v for v in candidates if self.game in v.get("game_versions", []) and (not loader or loader in v.get("loaders", [])) and (pin or v.get("version_type") == "release")]
            if not candidates:
                raise MineKitError(f"No compatible {'pinned' if pin else 'stable'} version of {project['title']} for {self.game} / {loader or 'vanilla'}.")
            version = max(candidates, key=lambda v: v.get("date_published", ""))
            selected[pid] = version  # Reserve before recursion to handle cycles.
            extension = ".jar" if expected_kind == "mod" else ".zip"
            files = [f for f in version.get("files", []) if f.get("filename", "").lower().endswith(extension)]
            primary = [f for f in files if f.get("primary")]
            if len(primary) == 1:
                file = primary[0]
            elif len(files) == 1:
                file = files[0]
            else:
                raise MineKitError(f"No unambiguous {extension} file for {project['title']}.")
            filename = safe_filename(file["filename"])
            digest = file.get("hashes", {}).get("sha512", "")
            size = file.get("size", 0)
            if len(digest) != 128 or not 0 < size <= MAX_FILE:
                raise MineKitError(f"Missing verified hash or invalid file size for {filename}.")
            for dep in version.get("dependencies", []):
                dep_type = dep.get("dependency_type")
                if dep_type == "required":
                    visit(dep.get("project_id"), "mod", dep.get("version_id"))
                elif dep_type == "incompatible":
                    incompatible.append((project["title"], dep))
                elif dep_type == "optional":
                    notes.append(f"Optional dependency for {project['title']}: {dep.get('project_id') or dep.get('version_id') or dep.get('file_name')}")
            downloads[pid] = Download(pid, version["id"], project["title"], filename, file["url"], digest, size, expected_kind)

        visit(project_id, kind)
        for title, dep in incompatible:
            for pid, version in selected.items():
                if (dep.get("version_id") and dep["version_id"] == version["id"]) or (not dep.get("version_id") and dep.get("project_id") == pid):
                    raise MineKitError(f"Declared incompatibility in {title} with {downloads[pid].title}.")
            notes.append(f"Review declared incompatible dependency for {title}: {dep.get('project_id') or dep.get('version_id') or dep.get('file_name')}")
        return Plan(tuple(downloads.values()), tuple(notes))


def download_file(item: Download, target: Path):
    """Stream from Modrinth CDN and enforce expected size and SHA-512."""
    def trusted(url):
        u = urlsplit(url)
        return u.scheme == "https" and u.hostname == "cdn.modrinth.com" and not u.username and not u.password

    if not trusted(item.url):
        raise MineKitError("Refusing download from an unrecognized host.")
    digest, total = hashlib.sha512(), 0
    with urlopen(Request(item.url, headers={"User-Agent": USER_AGENT}), timeout=60) as response, target.open("xb") as output:
        if not trusted(response.geturl()):
            raise MineKitError("Download redirected outside the Modrinth CDN.")
        while chunk := response.read(128 * 1024):
            total += len(chunk)
            if total > item.size or total > MAX_FILE:
                raise MineKitError(f"Unexpected size: {item.filename}")
            digest.update(chunk)
            output.write(chunk)
    if total != item.size or digest.hexdigest() != item.digest.lower():
        raise MineKitError(f"Integrity check failed: {item.filename}")
    with ZipFile(target) as archive:
        if item.kind == "resourcepack" and "pack.mcmeta" not in archive.namelist():
            raise MineKitError("Resource pack metadata missing.")
        if item.kind == "datapack" and "pack.mcmeta" not in archive.namelist():
            raise MineKitError("Data pack metadata missing.")


def execute_plan(plan: Plan, game_dir: Path, world_dir: Path | None = None, progress=lambda _: None):
    """Download and validate every file before changing the game directory."""
    if not game_dir.is_dir():
        raise MineKitError("Select an existing Java game directory.")
    with tempfile.TemporaryDirectory(prefix="minekit-") as staging:
        files = []
        for i, item in enumerate(plan.downloads):
            progress(f"Downloading {item.title} ({i + 1}/{len(plan.downloads)})")
            source = Path(staging) / str(i)
            download_file(item, source)
            if item.kind == "datapack":
                if not world_dir or not (world_dir / "level.dat").is_file():
                    raise MineKitError("Select a Java world containing level.dat.")
                dest = world_dir / "datapacks" / item.filename
            else:
                dest = game_dir / FOLDERS[item.kind] / item.filename
            files.append(StagedFile(source, dest))
        return install_files(files)
