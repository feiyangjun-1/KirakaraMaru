#!/usr/bin/env python3
"""Pick the best sampler output per character, or reject the character outright.

Shows ref | <one panel per sampler> for every generated character and records
either the winning sampler or "none". Chosen glyphs are written into the output
folder as <char>.png as you go; rejected characters are collected in a text file
so they can be regenerated on their own.

The panel marked "*" is a suggestion, scored by SSIM against the source glyph.
On an earlier font pair that rule beat always-taking-euler by only 0.003 mean
SSIM, so treat the star as a starting point, not an answer.

Sampler folders are discovered under --gen-root, so this works with whatever
combination of samplers has finished generating.

Keys
----
  1..9           pick that sampler, advance
  space, enter   accept the suggested sampler (*), advance
  0, x           none are usable, advance
  left / right   move without deciding
  u              clear the decision on the current character
  ctrl+s         confirm the export paths (saving is automatic anyway)

Every decision immediately rewrites the state file, the reject list and the
chosen glyph, so closing the window or killing the process costs at most the
keystroke in flight.

Usage
-----
    python scripts/review_gui.py
    python scripts/review_gui.py --only-undecided
    python scripts/review_gui.py --gen-root run/gen_gb2312_balanced500
"""
import argparse
import csv
from collections import Counter
import json
import os
import re
import sys
import tkinter as tk
from tkinter import messagebox

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageTk

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data_processing.font_utils import GlyphRenderer, load_font

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(REPO, "run")

NONE = "none"
SUGGEST_FROM = ("euler", "ab2")   # heun measured consistently worst; never suggested

BG = "#1e1e1e"
FG = "#e8e8e8"
DIM = "#888888"
PICK = "#4ec9b0"
REJECT = "#f48771"


def method_of(label):
    """Sampler name out of a panel label, which may carry steps and cfg.

    Labels look like "euler", "euler@1.8" or "euler-100@2.6" depending on what
    had to be disambiguated, so strip both suffixes rather than one.
    """
    return label.split("@")[0].split("-")[0]


def discover_samplers(gen_root):
    """Map variant label -> its 'generated' folder, for whatever has been produced.

    The label keeps the cfg value whenever one method was run at several
    settings, so a cfg sweep shows up as separate panels instead of collapsing
    onto one another.
    """
    found = {}
    if not os.path.isdir(gen_root):
        raise SystemExit(f"generation root not found: {gen_root}")
    entries = []
    for name in sorted(os.listdir(gen_root)):
        sub = os.path.join(gen_root, name, "generated")
        if not os.path.isdir(sub):
            continue
        method = name.split("-")[0]
        cfg = re.search(r"cfg([\d.]+)", name)
        steps = re.search(r"steps(\d+)", name)
        entries.append((method, steps.group(1) if steps else None,
                        cfg.group(1) if cfg else None, sub))

    counts = Counter(method for method, _, _, _ in entries)
    for method, steps, cfg, sub in entries:
        if counts[method] == 1:
            label = method
        else:
            # only spell out what actually varies, so panels stay readable
            label = method + (f"-{steps}" if steps else "") + (f"@{cfg}" if cfg else "")
        found[label] = sub
    if not found:
        raise SystemExit(f"no '<sampler>/generated' folders under {gen_root}")
    return found


def index_glyphs(folder):
    out = {}
    for fn in os.listdir(folder):
        m = re.search(r"U\+([0-9A-Fa-f]{4,6})", fn)
        if m:
            out[int(m.group(1), 16)] = os.path.join(folder, fn)
    return out


def ssim(a, b):
    """Plain SSIM on two equal-sized grayscale float arrays."""
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mu1 = cv2.GaussianBlur(a, (11, 11), 1.5)
    mu2 = cv2.GaussianBlur(b, (11, 11), 1.5)
    mu1_sq, mu2_sq, mu1_mu2 = mu1 * mu1, mu2 * mu2, mu1 * mu2
    s1 = cv2.GaussianBlur(a * a, (11, 11), 1.5) - mu1_sq
    s2 = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mu2_sq
    s12 = cv2.GaussianBlur(a * b, (11, 11), 1.5) - mu1_mu2
    num = (2 * mu1_mu2 + C1) * (2 * s12 + C2)
    den = (mu1_sq + mu2_sq + C1) * (s1 + s2 + C2)
    return float(np.mean(num / den))


def build_suggestions(codepoints, glyphs, ref_font, cache_path, key):
    """Suggest, per character, the euler/ab2 output closest to the source glyph."""
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                blob = json.load(f)
            if blob.get("key") == key:
                return {int(k): v for k, v in blob["pick"].items()}
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    pool = [lab for lab in glyphs if method_of(lab) in SUGGEST_FROM] or list(glyphs)
    print(f"scoring suggestions over {pool} (one-off, cached afterwards)...")
    renderer = GlyphRenderer(ref_font, 256)
    pick = {}
    for i, cp in enumerate(codepoints):
        src = renderer.render(cp)
        if src is None:
            pick[cp] = pool[0]
            continue
        s = np.asarray(src.convert("L"), dtype=np.float64)
        best, best_score = pool[0], -2.0
        for name in pool:
            path = glyphs[name].get(cp)
            if path is None:
                continue
            g = np.asarray(Image.open(path).convert("L").resize((256, 256)), dtype=np.float64)
            score = ssim(s, g)
            if score > best_score:
                best, best_score = name, score
        pick[cp] = best
        if (i + 1) % 1000 == 0:
            print(f"  {i + 1}/{len(codepoints)}")

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"key": key, "pick": {str(k): v for k, v in pick.items()}}, f)
    return pick


class Reviewer:
    def __init__(self, root, items, glyphs, order, ref_font, paths, only_undecided, panel):
        self.root = root
        self.glyphs = glyphs
        self.order = order
        self.panel = panel
        self.state_path, self.reject_txt, self.select_csv, self.best_dir = paths
        self.state = self._load_state()

        if only_undecided:
            items = [it for it in items if chr(it[0]) not in self.state]
            if not items:
                messagebox.showinfo("done", "Nothing left to review.")
                root.destroy()
                return
        self.items = items
        self.idx = self._first_undecided()

        os.makedirs(self.best_dir, exist_ok=True)
        self.ref = GlyphRenderer(ref_font, 256)
        self._photos = []

        root.title("pick the best sampler output")
        root.configure(bg=BG)
        self.header = tk.Label(root, bg=BG, fg=FG, font=("Consolas", 13), pady=6)
        self.header.pack(fill="x")
        self.canvas = tk.Label(root, bg=BG)
        self.canvas.pack(padx=10)
        self.labels = tk.Label(root, bg=BG, fg=DIM, font=("Consolas", 12), pady=4)
        self.labels.pack(fill="x")
        self.status = tk.Label(
            root, bg=BG, fg=DIM, font=("Consolas", 10), pady=6,
            text="1..%d pick   space accept *   0 none   left/right move   u undo"
                 % len(order))
        self.status.pack(fill="x")

        for n in range(1, len(order) + 1):
            root.bind(str(n), self._on_number)
        root.bind("<space>", lambda e: self._accept_suggested())
        root.bind("<Return>", lambda e: self._accept_suggested())
        for k in ("0", "x", "X"):
            root.bind(k, lambda e: self._decide(NONE))
        root.bind("<Left>", lambda e: self._move(-1))
        root.bind("<Right>", lambda e: self._move(1))
        root.bind("u", lambda e: self._undo())
        root.bind("<Control-s>", lambda e: self._export(verbose=True))
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._render()

    def _load_state(self):
        if os.path.isfile(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=0)
        os.replace(tmp, self.state_path)
        self._export()

    def _first_undecided(self):
        for i, (cp, _) in enumerate(self.items):
            if chr(cp) not in self.state:
                return i
        return 0

    def _render(self):
        cp, suggested = self.items[self.idx]
        ch = chr(cp)
        size = self.panel
        blank = Image.new("RGB", (256, 256), "white")
        gap = 8

        panels = [self.ref.render(cp) or blank]
        for name in self.order:
            path = self.glyphs[name].get(cp)
            panels.append(Image.open(path).convert("RGB") if path else blank)

        strip = Image.new("RGB", (size * len(panels) + gap * (len(panels) - 1), size), (30, 30, 30))
        for i, img in enumerate(panels):
            if img.size != (size, size):
                img = img.resize((size, size), Image.LANCZOS)
            strip.paste(img, (i * (size + gap), 0))
        d = ImageDraw.Draw(strip)
        for i, lab in enumerate(["ref"] + self.order):
            d.text((i * (size + gap) + 4, 4), lab, fill=(120, 120, 120))

        photo = ImageTk.PhotoImage(strip)
        self._photos = [photo]
        self.canvas.configure(image=photo)

        decision = self.state.get(ch)
        rejects = sum(1 for v in self.state.values() if v == NONE)
        self.header.configure(
            text=f"  {ch}   U+{cp:04X}        {self.idx + 1} / {len(self.items)}        "
                 f"decided {len(self.state)}   none {rejects}")

        parts = []
        for n, name in enumerate(self.order, start=1):
            star = "*" if name == suggested else " "
            sel = " <" if decision == name else "  "
            parts.append(f"[{n}] {name}{star}{sel}")
        if decision == NONE:
            parts.append("   >> NONE <<")
        self.labels.configure(
            text="      ".join(parts),
            fg=REJECT if decision == NONE else (PICK if decision else DIM))

    def _on_number(self, event):
        self._decide(self.order[int(event.char) - 1])

    def _accept_suggested(self):
        self._decide(self.items[self.idx][1])

    def _apply(self, ch, cp, value):
        dest = os.path.join(self.best_dir, f"{ch}.png")
        try:
            if value == NONE:
                if os.path.isfile(dest):
                    os.remove(dest)
            else:
                path = self.glyphs[value].get(cp)
                if path:
                    Image.open(path).convert("RGB").save(dest)
        except OSError as exc:
            self.status.configure(text=f"could not update {dest}: {exc}", fg=REJECT)

    def _decide(self, value):
        cp = self.items[self.idx][0]
        self.state[chr(cp)] = value
        self._save_state()
        self._apply(chr(cp), cp, value)
        self._move(1)

    def _undo(self):
        cp, suggested = self.items[self.idx]
        if self.state.pop(chr(cp), None) is not None:
            self._save_state()
            self._apply(chr(cp), cp, suggested)
        self._render()

    def _move(self, step):
        self.idx = max(0, min(len(self.items) - 1, self.idx + step))
        self._render()

    def _export(self, verbose=False):
        rejects = sorted(ch for ch, v in self.state.items() if v == NONE)
        os.makedirs(os.path.dirname(self.reject_txt), exist_ok=True)
        tmp = self.reject_txt + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("".join(rejects))
        os.replace(tmp, self.reject_txt)

        with open(self.select_csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["char", "codepoint", "suggested", "chosen"])
            for cp, suggested in self.items:
                ch = chr(cp)
                if ch in self.state:
                    w.writerow([ch, f"U+{cp:04X}", suggested, self.state[ch]])
        if verbose:
            messagebox.showinfo(
                "saved",
                f"{len(rejects)} rejected -> {self.reject_txt}\n"
                f"glyphs -> {self.best_dir}")
        return len(rejects)

    def _on_close(self):
        n = self._export()
        print(f"decided {len(self.state)} / {len(self.items)}, {n} marked none")
        print(f"glyphs  -> {self.best_dir}")
        print(f"rejects -> {self.reject_txt}")
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gen-root", default=os.path.join(RUN, "gen_gb2312_balanced500"))
    ap.add_argument("--ref-font", default=os.path.join(RUN, "ref.otf"))
    ap.add_argument("--out", default=os.path.join(RUN, "gb2312_best"))
    ap.add_argument("--only-undecided", action="store_true")
    ap.add_argument("--panel", type=int, default=256)
    args = ap.parse_args()

    folders = discover_samplers(args.gen_root)
    glyphs = {name: index_glyphs(folder) for name, folder in folders.items()}
    order = [s for s in ("euler", "ab2", "heun") if s in glyphs]
    order += [s for s in glyphs if s not in order]
    print("samplers: " + ", ".join(f"{s} ({len(glyphs[s])})" for s in order))

    codepoints = sorted(set.intersection(*(set(g) for g in glyphs.values())))
    print(f"{len(codepoints)} characters present in every sampler")

    _, ref_path = load_font(args.ref_font)
    tag = os.path.basename(os.path.normpath(args.gen_root))
    cache = os.path.join(RUN, f"review_{tag}_suggest.json")
    key = f"{sorted(order)}:{len(codepoints)}:{os.path.getmtime(ref_path)}"
    pick = build_suggestions(codepoints, glyphs, str(ref_path), cache, key)

    items = [(cp, pick.get(cp, order[0])) for cp in codepoints]
    paths = (os.path.join(RUN, f"review_{tag}_state.json"),
             os.path.join(RUN, f"review_{tag}_rejects.txt"),
             os.path.join(RUN, f"review_{tag}_selections.csv"),
             args.out)

    root = tk.Tk()
    app = Reviewer(root, items, glyphs, order, str(ref_path), paths,
                   args.only_undecided, args.panel)
    if getattr(app, "items", None):
        root.mainloop()


if __name__ == "__main__":
    main()
