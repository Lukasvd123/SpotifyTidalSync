"""Pick the correct Tidal track by hand when matching gets it wrong."""

import threading
import tkinter as tk
from tkinter import ttk

from ..config import UI_FONT, logger
from .theme import ACCENT, DIM, FIELD, PANEL, TEXT, DarkWindow


class ManualMatchWindow(DarkWindow):
    def __init__(self, parent, engine, source_state):
        super().__init__(parent, "Fix Match", 700, 500)
        self.engine = engine
        self.title_text = source_state.title
        self.artist_text = source_state.artist
        self.results = []

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=FIELD, foreground="#eeeeee",
                        fieldbackground=FIELD, borderwidth=0, rowheight=25)
        style.map("Treeview", background=[("selected", ACCENT)])
        style.configure("Treeview.Heading", background=PANEL, foreground="#dddddd",
                        relief="flat")

        header = tk.Frame(self, bg=PANEL)
        header.pack(fill="x", padx=20, pady=20)
        tk.Label(header, text=f"Fixing: {self.title_text}", bg=PANEL, fg=TEXT,
                 font=(UI_FONT, 12, "bold")).pack(anchor="w")
        tk.Label(header, text=f"Artist: {self.artist_text}", bg=PANEL, fg=DIM,
                 font=(UI_FONT, 10)).pack(anchor="w")

        search = tk.Frame(self, bg=PANEL)
        search.pack(fill="x", padx=20, pady=5)
        self.entry = tk.Entry(search, bg="#333333", fg=TEXT, insertbackground=TEXT,
                              relief="flat", font=(UI_FONT, 10))
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 10), ipady=3)
        self.entry.insert(0, f"{self.title_text} {self.artist_text}".strip())
        self.entry.bind("<Return>", lambda _e: self.do_search())
        tk.Button(search, text="Search", command=self.do_search, bg="#444444",
                  fg=TEXT, relief="flat", padx=12, pady=2).pack(side="left")

        self.tree = ttk.Treeview(self, columns=("Title", "Artist", "Album"),
                                 show="headings", height=10)
        for column, width in (("Title", 250), ("Artist", 150), ("Album", 200)):
            self.tree.heading(column, text=column)
            self.tree.column(column, width=width)
        self.tree.pack(fill="both", expand=True, padx=20, pady=10)
        self.tree.bind("<Double-1>", lambda _e: self.select())

        buttons = tk.Frame(self, bg=PANEL)
        buttons.pack(fill="x", padx=20, pady=20)
        tk.Button(buttons, text="Use This Track", command=self.select, bg="#008800",
                  fg=TEXT, relief="flat", padx=15, pady=5,
                  font=(UI_FONT, 9, "bold")).pack(side="right")
        tk.Button(buttons, text="Cancel", command=self.destroy, bg="#444444",
                  fg=TEXT, relief="flat", padx=12, pady=5).pack(side="right", padx=10)

        self.do_search()

    def do_search(self):
        query = self.entry.get().strip()
        if not query:
            return
        for row in self.tree.get_children():
            self.tree.delete(row)
        threading.Thread(target=self._search, args=(query,), daemon=True).start()

    def _search(self, query):
        try:
            results = self.engine.search_tidal(query)
        except Exception as e:
            logger.error(f"Manual search failed: {e}")
            return
        self.results = results
        try:
            self.after(0, self._show)
        except Exception:
            pass

    def _show(self):
        if not self.winfo_exists():
            return
        for track in self.results:
            try:
                self.tree.insert("", "end",
                                 values=(track.name, track.artist.name, track.album.name))
            except Exception:
                continue

    def select(self):
        selection = self.tree.selection()
        if not selection:
            return
        index = self.tree.index(selection[0])
        if index >= len(self.results):
            return
        track = self.results[index]
        threading.Thread(target=self.engine.manual_map, args=(track,), daemon=True).start()
        self.destroy()
