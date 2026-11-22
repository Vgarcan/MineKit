"""Modrinth public API client and dependency-aware installation planner."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from zipfile import ZipFile

from .core import MineKitError, StagedFile, install_files, safe_filename
from . import __version__

BASE = "https://api.modrinth.com/v2"
USER_AGENT = f"Vgarcan/MineKit/{__version__} (https://github.com/Vgarcan/MineKit)"
FOLDERS = {"mod": "mods", "resourcepack": "resourcepacks", "shader": "shaderpacks", "datapack": "datapacks"}
KINDS = (*FOLDERS, "modpack")
SORTS = ("relevance", "downloads", "follows", "newest", "updated")
EXTENSIONS = {"mod": ".jar", "resourcepack": ".zip", "shader": ".zip", "datapack": ".zip", "modpack": ".mrpack"}
MAX_FILE = 512 * 1024 * 1024


def _trusted_cdn(url: str) -> bool:
    u = urlsplit(url)
    return u.scheme == "https" and u.hostname == "cdn.modrinth.com" and not u.username and not u.password and u.port in (None, 443)


class _CdnRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _trusted_cdn(newurl):
            raise MineKitError("Refusing a redirect outside the Modrinth CDN.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_cdn(url: str, timeout: int):
    if not _trusted_cdn(url):
        raise MineKitError("Refusing download from an unrecognized host.")
    return build_opener(_CdnRedirect()).open(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)


@dataclass(frozen=True)
class SearchPage:
    hits: list[dict]
    offset: int
    total: int
    limit: int


class Modrinth:
    def get(self, route: str, **params):
        url = BASE + route
        if params:
            url += "?" + urlencode(params)
        with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as response:
            return json.load(response)

    def search(self, query: str, kind: str, version: str, loader: str, *, offset=0, limit=24, sort="relevance", category="") -> SearchPage:
        if kind not in KINDS or not version or sort not in SORTS or offset < 0 or not 1 <= limit <= 100:
            raise MineKitError("Invalid catalog filters.")
        facets = [[f"all_project_types:{kind}"], [f"versions:{version}"]]
        if kind in ("mod", "shader", "modpack") and loader != "vanilla":
            facets.append([f"categories:{loader}"])
        if category:
            facets.append([f"categories:{category}"])
        result = self.get("/search", query=query, facets=json.dumps(facets), limit=limit, offset=offset, index=sort)
        return SearchPage(result["hits"], offset, result["total_hits"], limit)

    def game_versions(self) -> list[str]:
        return [entry["version"] for entry in self.get("/tag/game_version") if entry["version_type"] == "release"]

    def categories(self, kind: str) -> list[str]:
        return sorted({entry["name"] for entry in self.get("/tag/category") if entry["project_type"] == kind})

    def project(self, project_id: str):
        return self.get(f"/project/{quote(project_id, safe='')}")

    def versions(self, project_id: str, version: str, loader: str | None):
        params = {"game_versions": json.dumps([version])}
        if loader:
            params["loaders"] = json.dumps([loader])
        return self.get(f"/project/{quote(project_id, safe='')}/version", **params)

    def version(self, version_id: str):
        return self.get(f"/version/{quote(version_id, safe='')}")

    def icon(self, url: str) -> bytes | None:
        """Icons are optional UI decoration, never a reason to fail a search."""
        try:
            if not _trusted_cdn(url):
                return None
            with _open_cdn(url, 5) as response:
                if not _trusted_cdn(response.geturl()):
                    return None
                data = response.read(512 * 1024 + 1)
            return data if len(data) <= 512 * 1024 else None
        except (OSError, ValueError, MineKitError):
            return None


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
    version_number: str = ""


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
        return self.resolve_many([(project_id, kind, None)])

    def loader_for(self, kind: str) -> str | None:
        return {"mod": self.loader, "modpack": self.loader, "shader": self.shader_loader,
                "resourcepack": "minecraft", "datapack": "datapack"}.get(kind)

    def candidates(self, project_id: str, kind: str, *, include_prerelease=False):
        loader = self.loader_for(kind)
        if loader == "vanilla":
            return []
        return [v for v in self.api.versions(project_id, self.game, loader)
                if self.game in v.get("game_versions", []) and (not loader or loader in v.get("loaders", []))
                and (include_prerelease or v.get("version_type") == "release")
                and v.get("environment") not in ("server_only", "dedicated_server_only")]

    def resolve_many(self, roots: list[tuple[str, str, str | None]]) -> Plan:
        if not roots:
            raise MineKitError("Select at least one project.")
        selected = {}
        downloads = {}
        incompatible = []
        notes = []
        kinds = {}
        root_pins = {pid: pin for pid, _, pin in roots if pin}

        def visit(pid: str | None, expected_kind: str | None, pin: str | None = None):
            pin = pin or root_pins.get(pid)
            exact = self.api.version(pin) if pin else None
            if exact:
                if pid and pid != exact["project_id"]:
                    raise MineKitError("Dependency project and version do not match.")
                pid = exact["project_id"]
            if not pid:
                raise MineKitError("Required dependency has no downloadable Modrinth project.")
            project = self.api.project(pid)
            pid = project["id"]
            expected_kind = expected_kind or project.get("project_type", "mod")
            if expected_kind not in KINDS:
                raise MineKitError(f"Unsupported dependency type for {project['title']}: {expected_kind}")
            if pid in selected:
                if pin and selected[pid]["id"] != pin:
                    raise MineKitError(f"Conflicting version requirements for {project['title']}.")
                if kinds[pid] != expected_kind:
                    raise MineKitError(f"Conflicting content types for {project['title']}.")
                return
            if len(selected) >= 100:
                raise MineKitError("Dependency graph exceeds the safety limit of 100 projects.")
            if expected_kind == "mod" and project.get("client_side") == "unsupported":
                raise MineKitError(f"{project['title']} is not intended for Java clients.")
            loader = self.loader_for(expected_kind)
            if loader == "vanilla":
                raise MineKitError("Mods need Fabric, Forge, NeoForge or Quilt; choose a modded profile.")
            candidates = [exact] if exact else self.candidates(pid, expected_kind)
            candidates = [v for v in candidates if self.game in v.get("game_versions", []) and (not loader or loader in v.get("loaders", [])) and (pin or v.get("version_type") == "release") and v.get("environment") not in ("server_only", "dedicated_server_only")]
            if not candidates:
                raise MineKitError(f"No compatible {'pinned' if pin else 'stable'} version of {project['title']} for {self.game} / {loader or 'vanilla'}.")
            version = max(candidates, key=lambda v: v.get("date_published", ""))
            selected[pid] = version  # Reserve before recursion to handle cycles.
            kinds[pid] = expected_kind
            extension = EXTENSIONS[expected_kind]
            files = [f for f in version.get("files", []) if f.get("filename", "").lower().endswith(extension) and f.get("file_type") not in ("sources-jar", "dev-jar", "javadoc-jar", "signature")]
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
            if not re.fullmatch(r"[a-fA-F0-9]{128}", digest) or not isinstance(size, int) or not 0 < size <= MAX_FILE:
                raise MineKitError(f"Missing verified hash or invalid file size for {filename}.")
            # A modpack's manifest is authoritative. v0.2 stores it but does not
            # deploy it or install its dependencies as loose mods.
            for dep in (() if expected_kind == "modpack" else version.get("dependencies", [])):
                dep_type = dep.get("dependency_type")
                if dep_type == "required":
                    visit(dep.get("project_id"), None, dep.get("version_id"))
                elif dep_type == "incompatible":
                    incompatible.append((project["title"], dep))
                elif dep_type == "optional":
                    notes.append(f"Optional dependency for {project['title']}: {dep.get('project_id') or dep.get('version_id') or dep.get('file_name')}")
            downloads[pid] = Download(pid, version["id"], project["title"], filename, file["url"], digest.lower(), size, expected_kind, version.get("version_number", ""))

        for project_id, kind, pin in roots:
            visit(project_id, kind, pin)
        for title, dep in incompatible:
            for pid, version in selected.items():
                if (dep.get("version_id") and dep["version_id"] == version["id"]) or (not dep.get("version_id") and dep.get("project_id") == pid):
                    raise MineKitError(f"Declared incompatibility in {title} with {downloads[pid].title}.")
            notes.append(f"Review declared incompatible dependency for {title}: {dep.get('project_id') or dep.get('version_id') or dep.get('file_name')}")
        return Plan(tuple(downloads.values()), tuple(notes))


def download_file(item: Download, target: Path):
    """Stream from Modrinth CDN and enforce expected size and SHA-512."""
    digest, total = hashlib.sha512(), 0
    with _open_cdn(item.url, 60) as response, target.open("xb") as output:
        if not _trusted_cdn(response.geturl()):
            raise MineKitError("Download redirected outside the Modrinth CDN.")
        while chunk := response.read(128 * 1024):
            total += len(chunk)
            if total > item.size or total > MAX_FILE:
                raise MineKitError(f"Unexpected size: {item.filename}")
            digest.update(chunk)
            output.write(chunk)
    if total != item.size or digest.hexdigest() != item.digest.lower():
        raise MineKitError(f"Integrity check failed: {item.filename}")
    validate_archive(item, target)


def validate_archive(item: Download, path: Path):
    """Check the declared file type even when reusing a cached SHA-512 object."""
    with ZipFile(path) as archive:
        names = archive.namelist()
        if item.kind == "resourcepack" and ("pack.mcmeta" not in names or not any(name.startswith("assets/") for name in names)):
            raise MineKitError("Resource pack metadata missing.")
        if item.kind == "datapack" and ("pack.mcmeta" not in names or not any(name.startswith("data/") for name in names)):
            raise MineKitError("Data pack metadata missing.")
        if item.kind == "shader" and not any(name.startswith("shaders/") for name in names):
            raise MineKitError("Shader archive is missing shaders/.")
        if item.kind == "modpack" and "modrinth.index.json" not in names:
            raise MineKitError("Modpack manifest missing.")


def inspect_modpack(path: Path, game: str, loader: str) -> str:
    """Read .mrpack metadata without extracting or executing its contents."""
    with ZipFile(path) as archive:
        info = archive.getinfo("modrinth.index.json")
        if info.file_size > 8 * 1024 * 1024:
            raise MineKitError("Modpack manifest is too large.")
        data = json.loads(archive.read(info))
    dependencies = data.get("dependencies", {})
    key = {"fabric": "fabric-loader", "quilt": "quilt-loader", "forge": "forge", "neoforge": "neoforge"}.get(loader)
    if data.get("formatVersion") != 1 or data.get("game") != "minecraft" or dependencies.get("minecraft") != game or (key and key not in dependencies):
        raise MineKitError("Modpack's actual manifest does not match the selected Minecraft version/loader.")
    files = data.get("files")
    if not isinstance(files, list):
        raise MineKitError("Invalid modpack file list.")
    return f"Modpack manifest: {len(files)} listed files; {game} / {key or 'vanilla'} {dependencies.get(key, '')}. Download-only; no deployment performed."


def execute_plan(plan: Plan, game_dir: Path, world_dir: Path | None = None, progress=lambda _: None):
    """Download and validate every file before changing the game directory."""
    if not game_dir.is_dir():
        raise MineKitError("Select an existing Java game directory.")
    if any(item.kind == "modpack" for item in plan.downloads):
        raise MineKitError("Modpacks are download-only; isolated installation is not supported yet.")
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
