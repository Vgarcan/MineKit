# MineKit

A lightweight desktop installer for Minecraft content. Keep the official Minecraft Launcher; use MineKit to discover and install files into the correct game profile.

## Contents

- [Getting started](#getting-started)
- [Java Edition](#java-edition)
- [Bedrock Edition](#bedrock-edition)
- [Safety and limitations](#safety-and-limitations)
- [Development](#development)
- [Roadmap](#roadmap)

## Getting started

Requires Python 3.11+ with Tkinter. Windows is the primary target; the Java engine also supports Linux and macOS default locations. No account, database or third-party launcher is required.

```bash
git clone https://github.com/Vgarcan/MineKit.git
cd MineKit
python main.py
```

On Linux, install your distribution's Python Tk package if Tkinter is not included. Start the official Minecraft Launcher once before using MineKit.

## Java Edition

1. Choose an existing launcher profile or set the game directory and exact game version manually.
2. Select Fabric, Forge, NeoForge, Quilt or vanilla. Get loader opens the corresponding official site; Fabric can also run its official client installer if Java is on PATH.
3. Select mods, resource packs, shaders or data packs. For shaders choose Iris or OptiFine, independently of the mod loader.
4. Enter a search term or a matching Modrinth project URL. Select a result and preview the files and dependencies.
5. Close Minecraft, review the target and confirm installation. Launch the matching profile afterwards.

The resolver filters by Minecraft version, mod loader and stable releases; recursively resolves required Modrinth dependencies, honors exact version pins, and blocks declared conflicts within the plan. Optional dependencies are reported, not silently installed. All downloads are staged, size checked and verified with SHA-512 before game files change. Existing identical files are skipped rather than duplicated. Different files at the same path and duplicate Fabric mod IDs are blocked.

Local `.jar` and `.zip` files can also be copied manually. Their version/dependency compatibility is not guaranteed. Java data packs require a selected world containing `level.dat`.

## Bedrock Edition

Select an existing `.mcpack`, `.mcaddon` or `.mcworld` file. MineKit inspects pack manifests (including nested packs), lists UUIDs, versions, minimum engine versions, modules and unresolved external dependencies. On Windows, Import using Minecraft opens the native file handler. Activate imported resource or behavior packs in the desired Minecraft world.

MineKit recognizes current and legacy Bedrock storage locations but deliberately does not modify world JSON or extract arbitrary archives into those directories.

## Safety and limitations

- Mod files contain executable code. Download only from trusted authors. Hash verification confirms integrity, not safety.
- Back up your worlds before modifying them. MineKit will not overwrite existing different files or silently update/remove mods. If an install fails while copying, it attempts to remove only files created during that operation.
- The first release does not guarantee all runtime compatibility. Modrinth metadata may be incomplete; Fabric metadata is used for duplicate IDs, not full semantic-version solving. Existing mods, non-Modrinth dependencies, and cross-project incompatibilities may need manual review.
- The dependency resolver is conservative and may refuse complex contradictory requirements rather than find an alternate compatible graph.
- The official Fabric installer modifies launcher profiles; it is intentionally invoked only after confirmation. Verify the resulting profile. Forge, NeoForge and Quilt installers are linked, not automated yet.
- Bedrock external UUID dependencies are reported, not downloaded automatically. Pack activation remains in Minecraft. No curated Bedrock online catalog is integrated yet.
- This is an early development release, not affiliated with Mojang or Microsoft. No Windows game-runtime validation has been performed in the Linux development environment.

## Development

```bash
python -m unittest discover -s tests -v
python -m compileall -q main.py minekit tests
```

| Module | Responsibility |
| --- | --- |
| `main.py` | Tkinter UI, background tasks and explicit confirmations |
| `minekit/core.py` | Profiles, safe file operations and local archive validation |
| `minekit/modrinth.py` | API client, dependency planner and integrity-checked downloads |
| `minekit/loaders.py` | Official Fabric installer integration |
| `minekit/bedrock.py` | Bedrock manifest inspection and native import |
| `tests/` | Offline regression tests |

Build a portable Windows executable locally with `python -m pip install pyinstaller` followed by `python -m PyInstaller --onefile --windowed --name MineKit main.py`.

## Roadmap

- Full loader/version constraint parsing, inventory, update plans and backups.
- Forge and NeoForge automatic installer integration.
- Bedrock installed-pack inventory, dependency matching and online discovery via supported APIs.
- Isolated Java configurations, export/import profiles, and shader recommendations.
- Optional server management for Paper/Geyser; not part of the client installation engine.

## References

- [Modrinth API](https://docs.modrinth.com/api/)
- [Fabric player documentation](https://docs.fabricmc.net/players/)
- [Bedrock add-on documentation](https://learn.microsoft.com/en-us/minecraft/creator/)
