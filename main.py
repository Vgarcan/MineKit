"""MineKit graphical launcher; all slow operations use a background thread."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit

from minekit import __version__
from minekit.bedrock import dependency_report, import_pack, inspect_pack
from minekit.core import MineKitError, bedrock_roots, discover_profiles, install_local, java_root
from minekit.loaders import install_fabric
from minekit.modrinth import FOLDERS, Modrinth, Resolver, execute_plan


class MineKit(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"MineKit {__version__} • Minecraft content installer")
        self.geometry("1000x680")
        self.minsize(760, 530)
        self.api = Modrinth()
        self.events = queue.Queue()
        self.busy = False
        self.results = []
        self.result_context = None
        self.profiles = discover_profiles(java_root())
        self.root_dir = tk.StringVar(value=str(java_root()))
        self.version = tk.StringVar(value="")
        self.loader = tk.StringVar(value="fabric")
        self.shader_loader = tk.StringVar(value="iris")
        self.kind = tk.StringVar(value="mod")
        self.query = tk.StringVar()
        self.status = tk.StringVar(value="Ready • Choose a profile or enter your version.")
        self.bedrock_path = None
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
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        java = ttk.Frame(tabs, padding=12)
        bedrock = ttk.Frame(tabs, padding=12)
        tabs.add(java, text="Java Edition")
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
        ttk.Entry(config, textvariable=self.version).grid(row=2, column=1, sticky="ew", padx=6, pady=3)
        ttk.Combobox(config, textvariable=self.loader, state="readonly", values=("fabric", "forge", "neoforge", "quilt", "vanilla"), width=14).grid(row=2, column=2, padx=4)
        ttk.Button(config, text="Get loader", command=self._loader_site).grid(row=2, column=3)
        search = ttk.Frame(java)
        search.pack(fill="x", pady=10)
        ttk.Combobox(search, textvariable=self.kind, state="readonly", values=tuple(FOLDERS), width=14).pack(side="left")
        ttk.Label(search, text="Shader engine:").pack(side="left", padx=(8, 0))
        ttk.Combobox(search, textvariable=self.shader_loader, state="readonly", values=("iris", "optifine"), width=9).pack(side="left", padx=4)
        entry = ttk.Entry(search, textvariable=self.query)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _: self._search())
        ttk.Button(search, text="Search / URL", command=self._search).pack(side="left")
        self.tree = ttk.Treeview(java, columns=("title", "author", "downloads", "description"), show="headings", selectmode="browse")
        for key, width in (("title", 180), ("author", 110), ("downloads", 90), ("description", 380)):
            self.tree.heading(key, text=key.title()); self.tree.column(key, width=width, stretch=key in ("title", "description"))
        self.tree.pack(fill="both", expand=True)
        actions = ttk.Frame(java)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Preview & install", style="Accent.TButton", command=self._plan).pack(side="left")
        ttk.Button(actions, text="Install local file", command=self._local).pack(side="left", padx=6)
        ttk.Button(actions, text="Open game folder", command=self._open_folder).pack(side="right")
        ttk.Label(bedrock, text="Inspect and import Bedrock Add-Ons, resource packs and worlds.").pack(anchor="w")
        roots = bedrock_roots()
        ttk.Label(bedrock, text="Storage: " + (str(roots[0]) if roots else "Not detected; native import requires Minecraft for Windows."), wraplength=850).pack(anchor="w", pady=6)
        ttk.Button(bedrock, text="Select .mcpack / .mcaddon / .mcworld", command=self._bedrock_select).pack(anchor="w", pady=8)
        self.bedrock_report = tk.Text(bedrock, wrap="word", height=15, state="disabled")
        self.bedrock_report.pack(fill="both", expand=True)
        ttk.Button(bedrock, text="Import using Minecraft", style="Accent.TButton", command=self._bedrock_import).pack(anchor="w", pady=8)
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

    def _search(self):
        if self.busy:
            return
        query, version, kind = self.query.get().strip(), self.version.get().strip(), self.kind.get()
        loader = self.shader_loader.get() if kind == "shader" else self.loader.get()
        if not query or not version:
            messagebox.showwarning("Search", "Enter a Minecraft version and a search term or Modrinth URL."); return
        def search():
            url = urlsplit(query)
            if url.scheme:
                parts = url.path.strip("/").split("/")
                if url.scheme != "https" or url.hostname != "modrinth.com" or len(parts) < 2 or parts[0] != kind:
                    raise MineKitError("Paste a Modrinth project URL matching the selected category.")
                project = self.api.project(parts[1]); project["project_id"] = project["id"]
                return [project]
            return self.api.search(query, kind, version, loader)
        def show(results):
            self.results = results
            self.result_context = (kind, version, loader)
            self.tree.delete(*self.tree.get_children())
            for index, hit in enumerate(results):
                self.tree.insert("", "end", iid=str(index), values=(hit.get("title"), hit.get("author", ""), hit.get("downloads", ""), hit.get("description", "")))
            self.status.set(f"{len(results)} results • Select one to resolve dependencies.")
        self._run(search, show)

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
        if self.busy or not self.tree.selection():
            return
        hit = self.results[int(self.tree.selection()[0])]
        kind, version, loader, shader = self.kind.get(), self.version.get().strip(), self.loader.get(), self.shader_loader.get()
        if self.result_context != (kind, version, shader if kind == "shader" else loader):
            messagebox.showwarning("Search", "Your search settings changed. Search again before installing."); return
        if not version:
            messagebox.showwarning("Target", "Enter a game version."); return
        target = self._target(kind)
        if not target:
            return
        root, world = target
        def preview(plan):
            lines = [f"Game: {version} / {loader}   Target: {root}", "", *[f"• {x.title} — {x.filename}" for x in plan.downloads], *plan.notes, "", "Close Minecraft before installing. Review existing mods and only install trusted content. Continue?"]
            if messagebox.askyesno("Review installation", "\n".join(lines)):
                self._run(lambda: execute_plan(plan, root, world, lambda s: self.events.put(("progress", s))), self._installed)
        self._run(lambda: Resolver(self.api, version, loader, shader).resolve(hit["project_id"], kind), preview)

    def _installed(self, files):
        messagebox.showinfo("MineKit", f"Installed {len(files)} new file(s). Identical existing files were skipped.\nLaunch the matching Minecraft profile and enable resource/shader packs in game.")

    def _local(self):
        if self.busy:
            return
        kind = self.kind.get()
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
        def show(packs):
            self.bedrock_path = Path(path)
            lines = [f"File: {Path(path).name}", ""]
            for pack in packs:
                lines += [f"{pack.name} • {pack.version}", f"UUID: {pack.uuid}", f"Modules: {', '.join(pack.modules)}", f"Minimum engine: {pack.minimum_engine}", ""]
            lines += dependency_report(packs) or ["No unresolved bundled dependencies reported."]
            self.bedrock_report.configure(state="normal"); self.bedrock_report.delete("1.0", "end"); self.bedrock_report.insert("1.0", "\n".join(lines)); self.bedrock_report.configure(state="disabled")
        self._run(lambda: inspect_pack(Path(path)), show)

    def _bedrock_import(self):
        if self.busy or not self.bedrock_path:
            return
        path = self.bedrock_path
        if messagebox.askyesno("Import Bedrock content", f"Open {path.name} with Minecraft?\nOnly import trusted packs. Activate it in your world after importing."):
            self._run(lambda: import_pack(path), lambda _: messagebox.showinfo("MineKit", "Import requested. Verify the result inside Minecraft."))


if __name__ == "__main__":
    MineKit().mainloop()
