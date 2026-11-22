# MineKit

A lightweight desktop installer and download library for Minecraft content. Keep the official Minecraft Launcher; use MineKit to discover, save and install files into the correct game profile. Version 0.2 adds a browsable catalog and a persistent library.

## Contents

- [Getting started](#getting-started)
- [Java Edition](#java-edition)
- [Download library](#download-library)
- [Bedrock Edition](#bedrock-edition)
- [Safety and limitations](#safety-and-limitations)
- [Development](#development)
- [Roadmap](#roadmap)

## Getting started

Requires Python 3.11+ with Tkinter. Windows is the primary target; the Java engine also supports Linux and macOS default locations. No account, database server or third-party launcher is required. SQLite is included with Python; Pillow is optional for additional thumbnail formats (`python -m pip install pillow`).

```bash
git clone https://github.com/Vgarcan/MineKit.git
cd MineKit
python main.py
```

On Linux, install your distribution's Python Tk package if Tkinter is not included. Start the official Minecraft Launcher once before using MineKit.

## Java Edition

1. Choose an existing launcher profile or set the game directory and exact game version manually.
2. Select Fabric, Forge, NeoForge, Quilt or vanilla. Get loader opens the corresponding official site; Fabric can also run its official client installer if Java is on PATH.
3. Open **Explore** and select mods, resource packs, shaders, data packs or modpacks. For shaders choose Iris or OptiFine independently of the mod loader.
4. Use **Load versions & categories** to retrieve current release versions and catalog categories. Type a search term (or leave it blank to browse), sort by popularity, date or relevance, and move through pages of results. You can also paste a matching Modrinth project URL.
5. Select one or more items. **Choose version** pins an exact compatible release; otherwise MineKit selects the newest stable version. Use **Add selected** to build a selection across pages and categories with the same game/loader context. Clear the selection before changing that context.
6. Select **Review & download** to resolve the complete selection, inspect versions, filenames, size and dependencies.
7. Choose **Save to library**, or **Save & install** if you want immediate installation. Close Minecraft before installation; launch the matching profile afterwards.

The resolver filters by Minecraft version, mod loader and stable releases; recursively resolves required Modrinth dependencies across the entire selection, honors exact version pins, and blocks declared conflicts within the plan. Optional dependencies are reported, not silently installed. All downloads are staged, size checked and verified with SHA-512 before game files change. Existing identical files are skipped rather than duplicated. Different files at the same path and duplicate Fabric mod IDs are blocked. Full `.mrpack` modpacks can be found, downloaded, and manifest-checked, but deployment to an isolated game instance is intentionally not yet implemented.

Local `.jar` and `.zip` files can also be copied manually. Their version/dependency compatibility is not guaranteed. Java data packs require a selected world containing `level.dat`.

## Download library

The Library tab allows saved plans to be installed offline into a matching Java game profile, exported with their original filenames, or inspected with exact versions, SHA-512 identifiers and historical install destinations. Bedrock packs can also be saved and imported later. Exporting a modpack does not install its constituent mods.

Downloaded files are stored once by content hash, regardless of how many plans refer to them. Every plan is an immutable manifest of the exact files and game/loader settings. The game directory is never changed when you choose Save to library alone. A corrupted cache blocks installation. Files are not silently replaced or deleted.

Default data location on Windows: `%LOCALAPPDATA%\MineKit`. Linux uses `$XDG_DATA_HOME/minekit` (normally `~/.local/share/minekit`); macOS uses `~/Library/Application Support/MineKit`.

```text
MineKit/
  library.db       SQLite metadata and installation history
  objects/         Verified content addressed by SHA-512
  imports/         Bedrock files with native extensions
```

## Bedrock Edition

Select an existing `.mcpack`, `.mcaddon` or `.mcworld` file. MineKit inspects pack manifests (including nested packs), lists UUIDs, versions, minimum engine versions, modules and unresolved external dependencies. Enter your installed Bedrock version to reject packs that require a newer engine; satisfying the minimum is not proof of full compatibility. Save the pack to your library or choose Import using Minecraft on Windows to open the native file handler. Activate imported resource or behavior packs in the desired Minecraft world.

MineKit recognizes current and legacy Bedrock storage locations but deliberately does not modify world JSON or extract arbitrary archives into those directories.

## Safety and limitations

- Mod files contain executable code. Download only from trusted authors. Hash verification confirms integrity, not safety.
- Back up your worlds before modifying them. MineKit will not overwrite existing different files or silently update/remove mods. If an install fails while copying, it attempts to remove only files created during that operation.
- The first release does not guarantee all runtime compatibility. Modrinth metadata may be incomplete; Fabric metadata is used for duplicate IDs, not full semantic-version solving. Existing mods, non-Modrinth dependencies, and cross-project incompatibilities may need manual review.
- The dependency resolver is conservative and may refuse complex contradictory requirements rather than find an alternate compatible graph.
- The official Fabric installer modifies launcher profiles; it is intentionally invoked only after confirmation. Verify the resulting profile. Forge, NeoForge and Quilt installers are linked, not automated yet.
- Bedrock external UUID dependencies are reported, not downloaded automatically. Pack activation remains in Minecraft. The CurseForge button opens its public Bedrock site; direct search/download integration requires authorized API access, respecting provider distribution restrictions. No API key is built into this public project.
- The library records historical install destinations, not a full current inventory. Updates, uninstall/disable, version solving against every installed mod, and world backup are follow-up work.
- This is an early development release, not affiliated with Mojang or Microsoft. No Windows game-runtime validation has been performed in the Linux development environment.

## Development

```bash
python -m unittest discover -s tests -v
python -m compileall -q main.py minekit tests
```

An opt-in network smoke test searches, resolves and downloads Sodium into a temporary library without writing to Minecraft. Run it with `MINEKIT_LIVE_TEST=1` in the environment and `python -m unittest discover -s tests -p test_live_modrinth.py -v`. CI runs this separately so temporary provider failures do not block deterministic tests or the executable build.

| Module | Responsibility |
| --- | --- |
| `main.py` | Tkinter UI, background tasks and explicit confirmations |
| `minekit/core.py` | Profiles, safe file operations and local archive validation |
| `minekit/modrinth.py` | API client, dependency planner and integrity-checked downloads |
| `minekit/library.py` | SQLite immutable plans, reusable verified objects and offline installation |
| `minekit/loaders.py` | Official Fabric installer integration |
| `minekit/bedrock.py` | Bedrock manifest inspection and native import |
| `tests/` | Offline regression tests |

Build a portable Windows executable locally with `python -m pip install pyinstaller pillow` followed by `python -m PyInstaller --onefile --windowed --name MineKit main.py`. The GitHub Actions workflow runs tests and a Windows Tkinter startup smoke test, then publishes the executable as an artifact.

## Roadmap

- Full loader/version constraint parsing, live inventory, compatible update plans and backups.
- Isolated modpack deployment from a validated `.mrpack` manifest.
- Forge and NeoForge automatic installer integration.
- Bedrock installed-pack inventory, dependency matching and online discovery via supported APIs.
- Isolated Java configurations, export/import profiles, and shader recommendations.
- Optional server management for Paper/Geyser; not part of the client installation engine.

## References

- [Modrinth API](https://docs.modrinth.com/api/)
- [Fabric player documentation](https://docs.fabricmc.net/players/)
- [Bedrock add-on documentation](https://learn.microsoft.com/en-us/minecraft/creator/)
