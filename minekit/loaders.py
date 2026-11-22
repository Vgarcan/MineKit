"""Explicitly authorized Fabric client installation via Fabric's official CLI."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from .core import MineKitError
from .modrinth import USER_AGENT


def _read(url: str, limit: int) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as response:
        if not response.geturl().startswith(("https://meta.fabricmc.net/", "https://maven.fabricmc.net/")):
            raise MineKitError("Fabric download redirected outside its official domains.")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise MineKitError("Fabric response exceeds the size limit.")
    return data


def install_fabric(root: Path, game_version: str) -> str:
    """Choose a stable loader for the exact game release and invoke its installer.

    No shells or arbitrary executable paths are accepted. The official installer
    owns launcher profile creation, so the launcher must be closed first.
    """
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", game_version):
        raise MineKitError("Enter an explicit stable Minecraft version, e.g. 1.21.1.")
    if not root.is_dir() or not any((root / name).is_file() for name in ("launcher_profiles.json", "launcher_profiles_microsoft_store.json")):
        raise MineKitError("Run the official Minecraft Launcher once before installing Fabric.")
    java = shutil.which("java")
    if java is None:
        raise MineKitError("Java is not on PATH. Use Fabric's official Windows installer instead.")
    versions = json.loads(_read(f"https://meta.fabricmc.net/v2/versions/loader/{game_version}", 2_000_000))
    loader = next((v["loader"]["version"] for v in versions if v["loader"].get("stable")), None)
    installers = json.loads(_read("https://meta.fabricmc.net/v2/versions/installer", 2_000_000))
    installer = next((v["version"] for v in installers if v.get("stable")), None)
    if not loader or not installer or not all(re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", v) for v in (loader, installer)):
        raise MineKitError("No stable Fabric loader/installer was found.")
    url = f"https://maven.fabricmc.net/net/fabricmc/fabric-installer/{installer}/fabric-installer-{installer}.jar"
    data = _read(url, 32 * 1024 * 1024)
    expected = _read(url + ".sha1", 1024).decode("ascii").strip().split()[0]
    if hashlib.sha1(data).hexdigest() != expected.lower():
        raise MineKitError("Fabric installer integrity check failed.")
    with tempfile.TemporaryDirectory(prefix="minekit-fabric-") as folder:
        jar = Path(folder) / "fabric-installer.jar"
        jar.write_bytes(data)
        result = subprocess.run([java, "-jar", str(jar), "client", "-dir", str(root.resolve()), "-mcversion", game_version, "-loader", loader], capture_output=True, text=True, timeout=300, check=False)
        if result.returncode:
            raise MineKitError("Fabric installer failed: " + (result.stderr or result.stdout)[-1500:])
    return loader
