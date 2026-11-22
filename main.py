"""MineKit graphical launcher; all slow operations use a background thread."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
import base64
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit

from minekit import __version__
from minekit.bedrock import check_engine, dependency_report, import_pack, inspect_pack
from minekit.core import MineKitError, bedrock_roots, discover_profiles, install_local, java_root
from minekit.loaders import install_fabric
from minekit.library import Context, Library, default_library
from minekit.modrinth import FOLDERS, KINDS, SORTS, Modrinth, Resolver, SearchPage


class MineKit(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"MineKit {__version__} • Minecraft content installer")
        self.geometry("1000x680")
        self.minsize(760, 530)
        self.api = Modrinth()
        self.library = Library(default_library())
        self.events = queue.Queue()
        self.busy = False
        self.results = []
        self.result_context = None
        self.page = None
        self.icons = []
        self.pins = {}
        self.basket = {}
        self.basket_context = None
        self.profiles = discover_profiles(java_root())
        self.root_dir = tk.StringVar(value=str(java_root()))
        self.version = tk.StringVar(value="")
        self.loader = tk.StringVar(value="fabric")
        self.shader_loader = tk.StringVar(value="iris")
        self.kind = tk.StringVar(value="mod")
        self.query = tk.StringVar()
        self.sort = tk.StringVar(value="relevance")
        self.category = tk.StringVar()
        self.basket_status = tk.StringVar(value="Selection: 0")
        self.page_status = tk.StringVar(value="Search the Modrinth catalog")
        self.status = tk.StringVar(value="Ready • Choose a profile or enter your version.")
        self.bedrock_path = None
        self.bedrock_version = tk.StringVar()
        self._build()
        self.after(100, self._poll)

    def _build(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"))
        style.configure("Accent.TButton", foreground="white", background="#287957", padding=8)
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="MineKit", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Your files. Your launcher. Java & Bedrock.").pack(anchor="w", pady=(0, 12))
        tabs = self.tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        java = ttk.Frame(tabs, padding=12)
        library = self.library_tab = ttk.Frame(tabs, padding=12)
        bedrock = ttk.Frame(tabs, padding=12)
        tabs.add(java, text="Explore • Java")
        tabs.add(library, text="Library")
        tabs.add(bedrock, text="Bedrock Edition")
        config = ttk.LabelFrame(java, text="Installation target", padding=10)
        config.pack(fill="x")
        config.columnconfigure(1, weight=1)
        ttk.Label(config, text="Launcher profile").grid(row=0, column=0, sticky="w")
        profiles = ttk.Combobox(config, state="readonly", values=[f"{p.name} • {p.version or '?'} • {p.loader}" for p in self.profiles])
        profiles.grid(row=0, column=1, columnspan=3, sticky="ew", padx=6, pady=3)
        def choose_profile(_=None):
            if profiles.current() >= 0:
                p = self.profiles[profiles.current()]
                self.root_dir.set(str(p.directory)); self.version.set(p.version); self.loader.set(p.loader)
        profiles.bind("<<ComboboxSelected>>", choose_profile)
        if self.profiles:
            profiles.current(0); choose_profile()
        ttk.Label(config, text="Game folder").grid(row=1, column=0, sticky="w")
        ttk.Entry(config, textvariable=self.root_dir).grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=3)
        ttk.Button(config, text="Browse", command=self._browse_directory).grid(row=1, column=3)
        ttk.Label(config, text="Game version").grid(row=2, column=0, sticky="w")
        self.version_choices = ttk.Combobox(config, textvariable=self.version)
        self.version_choices.grid(row=2, column=1, sticky="ew", padx=6, pady=3)
        ttk.Combobox(config, textvariable=self.loader, state="readonly", values=("fabric", "forge", "neoforge", "quilt", "vanilla"), width=14).grid(row=2, column=2, padx=4)
        ttk.Button(config, text="Get loader", command=self._loader_site).grid(row=2, column=3)
        search = ttk.Frame(java)
        search.pack(fill="x", pady=10)
        ttk.Combobox(search, textvariable=self.kind, state="readonly", values=KINDS, width=14).pack(side="left")
        ttk.Label(search, text="Shader engine:").pack(side="left", padx=(8, 0))
        ttk.Combobox(search, textvariable=self.shader_loader, state="readonly", values=("iris", "optifine"), width=9).pack(side="left", padx=4)
        entry = ttk.Entry(search, textvariable=self.query)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _: self._search())
        ttk.Button(search, text="Search / URL", command=self._search).pack(side="left")
        filters = ttk.Frame(java)
        filters.pack(fill="x", pady=(0, 8))
        ttk.Label(filters, text="Sort:").pack(side="left")
        ttk.Combobox(filters, textvariable=self.sort, state="readonly", values=SORTS, width=12).pack(side="left", padx=4)
        ttk.Label(filters, text="Category:").pack(side="left")
        self.category_choices = ttk.Combobox(filters, textvariable=self.category, width=17)
        self.category_choices.pack(side="left", padx=4)
        ttk.Button(filters, text="Load versions & categories", command=self._load_filters).pack(side="left", padx=4)
        ttk.Label(filters, text="Source: Modrinth", foreground="#287957").pack(side="right")
        results = ttk.Frame(java)
        results.pack(fill="both", expand=True)
        style.configure("Catalog.Treeview", rowheight=44)
        self.tree = ttk.Treeview(results, columns=("author", "downloads", "description"), show="tree headings", selectmode="extended", style="Catalog.Treeview")
        self.tree.heading("#0", text="Project"); self.tree.column("#0", width=220, minwidth=160)
        for key, width in (("author", 105), ("downloads", 90), ("description", 420)):
            self.tree.heading(key, text=key.title()); self.tree.column(key, width=width, stretch=key == "description")
        scrollbar = ttk.Scrollbar(results, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y"); self.tree.pack(side="left", fill="both", expand=True)
        pager = ttk.Frame(java)
        pager.pack(fill="x", pady=(6, 0))
        ttk.Button(pager, text="Previous", command=lambda: self._page_move(-1)).pack(side="left")
        ttk.Button(pager, text="Next", command=lambda: self._page_move(1)).pack(side="left", padx=4)
        ttk.Label(pager, textvariable=self.page_status).pack(side="left", padx=4)
        ttk.Button(pager, text="Choose version", command=self._choose_version).pack(side="right")
        ttk.Button(pager, text="Add selected", command=self._add_selection).pack(side="right", padx=4)
        actions = ttk.Frame(java)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Review & download", style="Accent.TButton", command=self._plan).pack(side="left")
        ttk.Label(actions, textvariable=self.basket_status).pack(side="left", padx=6)
        ttk.Button(actions, text="Clear", command=self._clear_selection).pack(side="left")
        ttk.Button(actions, text="Install local file", command=self._local).pack(side="left", padx=6)
        ttk.Button(actions, text="Open game folder", command=self._open_folder).pack(side="right")
        ttk.Label(library, text="Saved downloads • available for reuse without Internet").pack(anchor="w")
        ttk.Label(library, text=str(self.library.root), wraplength=800).pack(anchor="w", pady=(0, 8))
        self.library_tree = ttk.Treeview(library, columns=("title", "edition", "version", "loader", "files", "size"), show="headings", selectmode="browse")
        for key, width in (("title", 230), ("edition", 75), ("version", 145), ("loader", 80), ("files", 50), ("size", 85)):
            self.library_tree.heading(key, text=key.title()); self.library_tree.column(key, width=width, stretch=key in ("title", "version"))
        self.library_tree.pack(fill="both", expand=True)
        library_actions = ttk.Frame(library); library_actions.pack(fill="x", pady=8)
        ttk.Button(library_actions, text="Install / import", style="Accent.TButton", command=self._library_install).pack(side="left")
        ttk.Button(library_actions, text="Details", command=self._library_details).pack(side="left", padx=6)
        ttk.Button(library_actions, text="Export files", command=self._library_export).pack(side="left")
        ttk.Button(library_actions, text="Refresh", command=self._refresh_library).pack(side="right")
        ttk.Label(library, text="Java installs use the game profile selected in Explore. Modpacks are download-only for now.", wraplength=850).pack(anchor="w")
        self._refresh_library()
        ttk.Label(bedrock, text="Inspect and import Bedrock Add-Ons, resource packs and worlds.").pack(anchor="w")
        roots = bedrock_roots()
        ttk.Label(bedrock, text="Storage: " + (str(roots[0]) if roots else "Not detected; native import requires Minecraft for Windows."), wraplength=850).pack(anchor="w", pady=6)
        bedrock_filters = ttk.Frame(bedrock); bedrock_filters.pack(fill="x", pady=4)
        ttk.Label(bedrock_filters, text="Installed Bedrock version (optional):").pack(side="left")
        ttk.Entry(bedrock_filters, textvariable=self.bedrock_version, width=14).pack(side="left", padx=6)
        ttk.Button(bedrock_filters, text="Browse Bedrock on CurseForge", command=lambda: webbrowser.open("https://www.curseforge.com/minecraft-bedrock")).pack(side="right")
        ttk.Button(bedrock, text="Select .mcpack / .mcaddon / .mcworld", command=self._bedrock_select).pack(anchor="w", pady=8)
        self.bedrock_report = tk.Text(bedrock, wrap="word", height=15, state="disabled")
        self.bedrock_report.pack(fill="both", expand=True)
        ttk.Button(bedrock, text="Import using Minecraft", style="Accent.TButton", command=self._bedrock_import).pack(anchor="w", pady=8)
        ttk.Button(bedrock, text="Save selected pack to library", command=self._bedrock_save).pack(anchor="w", pady=2)
        ttk.Label(bedrock, text="After importing, activate packs in the desired world. No game files are extracted directly.").pack(anchor="w")
        ttk.Separator(outer).pack(fill="x", pady=8)
        ttk.Label(outer, textvariable=self.status, wraplength=920).pack(anchor="w")

    def _run(self, work, complete):
        if self.busy:
            return
        self.busy = True
        self.status.set("Working…")
        def worker():
            try:
                self.events.put(("done", complete, work()))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "progress":
                self.status.set(event[1]); continue
            self.busy = False
            self.status.set("Ready")
            if event[0] == "error":
                messagebox.showerror("MineKit", event[1])
            else:
                event[1](event[2])
        self.after(100, self._poll)

    def _browse_directory(self):
        path = filedialog.askdirectory()
        if path:
            self.root_dir.set(path)

    def _loader_site(self):
        sites = {"fabric": "https://fabricmc.net/use/installer/", "forge": "https://files.minecraftforge.net/", "neoforge": "https://neoforged.net/", "quilt": "https://quiltmc.org/install/"}
        if self.busy:
            return
        if self.loader.get() == "fabric":
            version = self.version.get().strip()
            if not version:
                messagebox.showwarning("Fabric", "Enter the Minecraft game version first."); return
            choice = messagebox.askyesnocancel("Install Fabric", f"Install Fabric for {version} via the official installer into {java_root()}?\nClose the Minecraft Launcher first. Java must be on PATH.\nYes: automatic installation. No: open official website.")
            if choice is True:
                self._run(lambda: install_fabric(java_root(), version), lambda loader: messagebox.showinfo("Fabric", f"Fabric installer completed ({loader}). Restart MineKit to refresh profiles."))
            elif choice is False:
                webbrowser.open(sites["fabric"])
        elif self.loader.get() in sites:
            webbrowser.open(sites[self.loader.get()])
        else:
            messagebox.showinfo("Loader", "Vanilla Minecraft does not need a mod loader.")

    def _open_folder(self):
        folder = Path(self.root_dir.get())
        if folder.is_dir():
            webbrowser.open(folder.resolve().as_uri())

    def _context(self):
        return Context(self.version.get().strip(), self.loader.get(), self.shader_loader.get())

    def _filters(self):
        return (self._context(), self.kind.get(), self.query.get().strip(), self.sort.get(), self.category.get().strip())

    def _load_filters(self):
        kind = self.kind.get()
        def show(data):
            versions, categories = data
            self.version_choices.configure(values=versions)
            if not self.version.get() and versions:
                self.version.set(versions[0])
            if self.kind.get() == kind:
                self.category_choices.configure(values=("", *categories))
            self.status.set("Filters updated from Modrinth.")
        self._run(lambda: (self.api.game_versions(), self.api.categories(kind)), show)

    def _thumbnail(self, data):
        if not data:
            return None
        try:
            # Pillow provides JPEG/WebP support when installed; Tk's native
            # PNG/GIF decoder keeps the Python-only startup path available.
            from PIL import Image, ImageTk
            image = Image.open(io.BytesIO(data))
            if image.width * image.height > 4_000_000:
                return None
            image.thumbnail((36, 36))
            return ImageTk.PhotoImage(image)
        except ImportError:
            try:
                image = tk.PhotoImage(data=base64.b64encode(data))
                return image.subsample(max(1, (max(image.width(), image.height()) + 35) // 36))
            except tk.TclError:
                return None
        except Exception:
            return None

    def _search(self, offset=0):
        if self.busy:
            return
        settings = self._filters()
        context, kind, query, sort, category = settings
        loader = context.shader_loader if kind == "shader" else context.loader
        if not context.version:
            messagebox.showwarning("Search", "Enter a Minecraft version or use Load versions & categories."); return
        def search():
            url = urlsplit(query)
            if url.scheme:
                parts = url.path.strip("/").split("/")
                if url.scheme != "https" or url.hostname != "modrinth.com" or len(parts) != 2 or parts[0] != kind:
                    raise MineKitError("Paste a Modrinth project URL matching the selected category (without a version suffix).")
                project = self.api.project(parts[1]); project["project_id"] = project["id"]
                page = SearchPage([project], 0, 1, 24)
            else:
                page = self.api.search(query, kind, context.version, loader, offset=offset, sort=sort, category=category)
            with ThreadPoolExecutor(max_workers=6) as pool:
                icons = list(pool.map(lambda hit: self.api.icon(hit.get("icon_url", "")), page.hits))
            return page, icons
        def show(data):
            page, icons = data
            self.page, self.results = page, page.hits
            self.result_context = settings
            self.pins.clear(); self.icons.clear()
            self.tree.delete(*self.tree.get_children())
            for index, hit in enumerate(page.hits):
                photo = self._thumbnail(icons[index])
                if photo:
                    self.icons.append(photo)
                self.tree.insert("", "end", iid=str(index), text=hit.get("title", "Unknown"), image=photo or "", values=(hit.get("author", ""), hit.get("downloads", ""), hit.get("description", "")))
            self.page_status.set(f"{page.offset+1 if page.hits else 0}–{page.offset+len(page.hits)} of {page.total}")
            self.status.set("Select projects; choose an exact version or use the newest compatible release.")
        self._run(search, show)

    def _page_move(self, direction):
        if self.busy or not self.page:
            return
        if self._filters() != self.result_context:
            self._search(); return
        new_offset = self.page.offset + direction * self.page.limit
        if 0 <= new_offset < self.page.total:
            self._search(new_offset)

    def _choose_version(self):
        if self.busy or len(self.tree.selection()) != 1:
            messagebox.showinfo("Versions", "Select one project first."); return
        if self._filters() != self.result_context:
            messagebox.showwarning("Versions", "Search again after changing filters."); return
        hit = self.results[int(self.tree.selection()[0])]
        context, kind, *_ = self.result_context
        pid = hit["project_id"]
        def show(versions):
            if not versions:
                messagebox.showinfo("Versions", "No versions match this game and loader."); return
            versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
            dialog = tk.Toplevel(self); dialog.title(f"Choose version • {hit['title']}"); dialog.geometry("640x380"); dialog.transient(self); dialog.grab_set()
            ttk.Label(dialog, text=f"{context.version} • {kind} • Choose an exact version").pack(anchor="w", padx=12, pady=8)
            choices = tk.Listbox(dialog, exportselection=False)
            choices.pack(fill="both", expand=True, padx=12)
            for version in versions:
                choices.insert("end", f"{version.get('version_number', version['id'])}  •  {version.get('version_type', '?')}  •  {version.get('name', '')}")
            def select():
                if not choices.curselection():
                    return
                v = versions[choices.curselection()[0]]
                self.pins[pid] = v["id"]
                if pid in self.basket:
                    self.basket[pid] = (kind, v["id"], hit["title"])
                self.status.set(f"Pinned {hit['title']} to {v.get('version_number', v['id'])}.")
                dialog.destroy()
            ttk.Button(dialog, text="Use exact version", command=select).pack(pady=8)
        self._run(lambda: Resolver(self.api, context.version, context.loader, context.shader_loader).candidates(pid, kind, include_prerelease=True), show)

    def _add_selection(self):
        if self.busy or not self.tree.selection():
            return False
        if self._filters() != self.result_context:
            messagebox.showwarning("Selection", "Search again after changing filters."); return False
        context, kind, *_ = self.result_context
        if self.basket and self.basket_context != context:
            messagebox.showwarning("Selection", "Clear the current selection before choosing a different game version or loader."); return False
        self.basket_context = context
        for index in self.tree.selection():
            hit = self.results[int(index)]; pid = hit["project_id"]
            self.basket[pid] = (kind, self.pins.get(pid), hit["title"])
        self.basket_status.set(f"Selection: {len(self.basket)}")
        self.status.set("Selection saved; you may search a different category or page.")
        return True

    def _clear_selection(self):
        if self.busy:
            return
        self.basket.clear(); self.basket_context = None; self.basket_status.set("Selection: 0")

    def _target(self, kind):
        root = Path(self.root_dir.get())
        if not self.root_dir.get().strip() or not root.is_dir():
            messagebox.showwarning("Target", "Choose an existing Java game directory."); return None
        world = None
        if kind == "datapack":
            choice = filedialog.askdirectory(title="Select a Java world folder containing level.dat", initialdir=str(root / "saves"))
            if not choice:
                return None
            world = Path(choice)
            if not (world / "level.dat").is_file():
                messagebox.showwarning("Target", "The selected folder does not contain level.dat."); return None
        return root, world

    def _plan(self):
        if self.busy:
            return
        if not self.basket and not self._add_selection():
            return
        context = self.basket_context
        if context != self._context():
            messagebox.showwarning("Selection", "The selection targets a different game version or loader. Restore its settings or clear it."); return
        roots = [(pid, kind, pin) for pid, (kind, pin, _) in self.basket.items()]
        names = [title for _, _, title in self.basket.values()]
        title = names[0] if len(names) == 1 else f"{names[0]} + {len(names)-1} projects"
        self._run(lambda: Resolver(self.api, context.version, context.loader, context.shader_loader).resolve_many(roots), lambda plan: self._review_plan(plan, context, title))

    def _review_plan(self, plan, context, title):
        dialog = tk.Toplevel(self); dialog.title("Review download plan"); dialog.geometry("760x480"); dialog.transient(self); dialog.grab_set()
        ttk.Label(dialog, text=f"{title}  •  {context.version} / {context.loader}", font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=12, pady=8)
        text = tk.Text(dialog, wrap="word", height=18)
        text.pack(fill="both", expand=True, padx=12)
        lines = [f"Total: {len(plan.downloads)} files · {sum(x.size for x in plan.downloads)/1048576:.1f} MiB", "", *[f"• {x.title} {x.version_number} [{x.kind}]\n  {x.filename}" for x in plan.downloads], "", *plan.notes, "", "Metadata compatibility is not a runtime guarantee. Install only trusted content."]
        text.insert("1.0", "\n".join(lines)); text.configure(state="disabled")
        actions = ttk.Frame(dialog, padding=12); actions.pack(fill="x")
        download_only = any(item.kind == "modpack" for item in plan.downloads)
        def save(install):
            target = None
            if install:
                if self._context() != context:
                    messagebox.showwarning("Target", "Choose the matching profile first.", parent=dialog); return
                target = self._target("datapack" if any(x.kind == "datapack" for x in plan.downloads) else "mod")
                if not target:
                    return
                if not messagebox.askyesno("Install", f"Close Minecraft. Install into {target[0]}?", parent=dialog):
                    return
            dialog.destroy()
            def work():
                saved = self.library.save(plan, context, title, self._progress)
                if target:
                    return saved, self.library.install(saved, context, *target, progress=self._progress)
                return saved, None
            self._run(work, self._saved)
        ttk.Button(actions, text="Save to library", style="Accent.TButton", command=lambda: save(False)).pack(side="left")
        if not download_only:
            ttk.Button(actions, text="Save & install", command=lambda: save(True)).pack(side="left", padx=8)
        else:
            ttk.Label(actions, text="Modpacks can be saved but not installed yet.").pack(side="left", padx=8)
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right")

    def _progress(self, status):
        self.events.put(("progress", status))

    def _saved(self, result):
        saved, installed = result
        self._refresh_library(); self._clear_selection(); self.tabs.select(self.library_tab)
        if installed is None:
            messagebox.showinfo("Library", f"Saved {saved.title}. You can install or export it later without downloading again.")
        else:
            self._installed(installed)

    def _refresh_library(self):
        self.library_tree.delete(*self.library_tree.get_children())
        for saved in self.library.list():
            self.library_tree.insert("", "end", iid=saved.id, values=(saved.title, saved.edition, saved.context.version, saved.context.loader, len(saved.plan.downloads), f"{sum(x.size for x in saved.plan.downloads)/1048576:.1f} MiB"))

    def _selected_library(self):
        if self.busy or not self.library_tree.selection():
            return None
        return self.library.get(self.library_tree.selection()[0])

    def _library_details(self):
        saved = self._selected_library()
        if not saved:
            return
        dialog = tk.Toplevel(self); dialog.title(saved.title); dialog.geometry("760x450"); dialog.transient(self)
        text = tk.Text(dialog, wrap="word"); text.pack(fill="both", expand=True, padx=10, pady=10)
        lines = [f"{saved.edition} • {saved.context.version} • {saved.context.loader}", f"Saved: {saved.created}", "", *[f"{x.title} {x.version_number}\n{x.filename} · {x.kind}\nSHA-512: {x.digest}" for x in saved.plan.downloads], "", *saved.plan.notes, "", "Previous installation targets (historical):", *self.library.installed_on(saved.id)]
        text.insert("1.0", "\n".join(lines)); text.configure(state="disabled")

    def _library_install(self):
        saved = self._selected_library()
        if not saved:
            return
        if saved.edition == "Bedrock":
            if messagebox.askyesno("Bedrock", f"Import {saved.title} through Minecraft? Review dependencies in Details first."):
                version = self.bedrock_version.get().strip()
                self._run(lambda: import_pack(self.library.bedrock_file(saved), version), lambda _: messagebox.showinfo("MineKit", "Import requested. Check inside Minecraft."))
            return
        if any(x.kind == "modpack" for x in saved.plan.downloads):
            messagebox.showinfo("Modpack", "Full modpacks are download-only for now. Use Export files to recover the .mrpack."); return
        context = self._context()
        if context != saved.context:
            messagebox.showwarning("Profile", "Select the matching game version and loader in Explore before installation."); return
        target = self._target("datapack" if any(x.kind == "datapack" for x in saved.plan.downloads) else "mod")
        if target and messagebox.askyesno("Confirm install", f"Install into {target[0]} using saved files? Close Minecraft first."):
            self._run(lambda: self.library.install(saved, context, *target, progress=self._progress), self._installed)

    def _library_export(self):
        saved = self._selected_library()
        if not saved:
            return
        folder = filedialog.askdirectory(title="Export original filenames to folder")
        if folder:
            self._run(lambda: self.library.export(saved, Path(folder)), lambda paths: messagebox.showinfo("Export", f"Exported {len(paths)} files. Existing identical files were skipped."))

    def _installed(self, files):
        messagebox.showinfo("MineKit", f"Installed {len(files)} new file(s). Identical existing files were skipped.\nLaunch the matching Minecraft profile and enable resource/shader packs in game.")

    def _local(self):
        if self.busy:
            return
        kind = self.kind.get()
        if kind == "modpack":
            messagebox.showinfo("Modpack", "Local modpack deployment is not supported yet."); return
        target = self._target(kind)
        if not target:
            return
        root, world = target
        path = filedialog.askopenfilename(filetypes=[("Java archives", "*.jar *.zip")])
        if not path:
            return
        destination = (world if world else root) / FOLDERS[kind]
        if messagebox.askyesno("Manual installation", f"Copy {Path(path).name} to {destination}?\nLocal compatibility and dependencies are not verified. Close Minecraft first."):
            self._run(lambda: install_local(Path(path), destination, kind), self._installed)

    def _bedrock_select(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(filetypes=[("Bedrock content", "*.mcpack *.mcaddon *.mcworld")])
        if not path:
            return
        self.bedrock_path = None
        version = self.bedrock_version.get().strip()
        def inspect():
            packs = inspect_pack(Path(path))
            return packs, check_engine(packs, version)
        def show(data):
            packs, engine = data
            self.bedrock_path = Path(path)
            lines = [f"File: {Path(path).name}", ""]
            for pack in packs:
                lines += [f"{pack.name} • {pack.version}", f"UUID: {pack.uuid}", f"Modules: {', '.join(pack.modules)}", f"Minimum engine: {pack.minimum_engine}", ""]
            lines += dependency_report(packs) or ["No unresolved bundled dependencies reported."]
            lines += engine
            self.bedrock_report.configure(state="normal"); self.bedrock_report.delete("1.0", "end"); self.bedrock_report.insert("1.0", "\n".join(lines)); self.bedrock_report.configure(state="disabled")
        self._run(inspect, show)

    def _bedrock_import(self):
        if self.busy or not self.bedrock_path:
            return
        path = self.bedrock_path
        if messagebox.askyesno("Import Bedrock content", f"Open {path.name} with Minecraft?\nOnly import trusted packs. Activate it in your world after importing."):
            version = self.bedrock_version.get().strip()
            self._run(lambda: import_pack(path, version), lambda _: messagebox.showinfo("MineKit", "Import requested. Verify the result inside Minecraft."))

    def _bedrock_save(self):
        if self.busy or not self.bedrock_path:
            return
        path = self.bedrock_path
        self._run(lambda: self.library.save_bedrock(path), lambda saved: self._saved((saved, None)))


if __name__ == "__main__":
    MineKit().mainloop()
