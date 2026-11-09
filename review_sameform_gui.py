#!/usr/bin/env python3
"""Hand-curate the characters whose ref and target glyph forms match.

Shows every Han character both fonts can render, side by side, and records a
yes/no verdict on whether the two glyph *forms* agree (ignoring weight and
style). Accepted characters are written to a plain text file suitable for
scripts/generate_custom_charlist_dataset.py --char-list.

A similarity score is shown for information, but do not lean on it: measured
against known CN/JP form differences it ranks them at the 39th percentile,
barely better than chance. Glyph form differences are routinely smaller than the
style difference between two typefaces, so the eye is the only reliable judge
here. Characters are therefore presented in codepoint order by default.

State and output are keyed to the target font by default. Use --session for a
separate set of verdicts when changing the reference font. Review one pair per run.

Keys
----
  y / 1 / space  same form  -> goes in the output list
  n / 0 / x      different form
  left / right   move without deciding
  u              clear the verdict on the current character
  A              mark every remaining undecided character as same (asks first)
  ctrl+s         confirm the export path (saving is automatic anyway)

Every decision immediately rewrites both the progress file and the character
list, so closing the window, killing the process or losing power costs at most
the keystroke in flight. Reopening resumes at the first undecided character.

Usage
-----
    python scripts/review_sameform_gui.py --target run/target.otf
    python scripts/review_sameform_gui.py --target run/Konatu.ttf
    python scripts/review_sameform_gui.py --target run/target.otf --charset gb2312
    python scripts/review_sameform_gui.py --target run/target.otf --ref run/other.ttf --session target_other
"""
import argparse
import json
import os
import sys
import tkinter as tk
from tkinter import messagebox

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageTk

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data_processing.charsets import get_charset_codepoints
from data_processing.font_utils import GlyphRenderer, get_cjk_codepoints, load_font

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_DIR = os.path.join(REPO, "run")

SAME, DIFF = "same", "diff"
SCORE_RENDER = 128   # glyphs are rendered at this size for scoring
SCORE_FEAT = 32      # then reduced to this before comparison
SCORE_BLUR = 2.0     # blur radius that washes out stroke weight

BG = "#1e1e1e"
FG = "#e8e8e8"
DIM = "#888888"
OK = "#4ec9b0"
NO = "#f48771"


def shape_feature(img):
    """Blurred, contrast-normalised glyph vector.

    The blur is what makes this usable across weights: ref is ExtraLight and the
    targets are Regular, so comparing raw ink mostly measures stroke thickness.
    After blurring and normalising, what is left is where the strokes sit.
    """
    small = (img.convert("L")
                .resize((SCORE_FEAT, SCORE_FEAT), Image.LANCZOS)
                .filter(ImageFilter.GaussianBlur(SCORE_BLUR)))
    a = 255.0 - np.asarray(small, dtype=np.float64)
    a -= a.mean()
    norm = np.linalg.norm(a)
    return a / norm if norm else a


def similarity(a, b):
    """Normalised cross-correlation of two shape features, roughly 0..1."""
    return float((a * b).sum())


def compute_scores(codepoints, ref_font, tgt_font, cache_path, cache_key):
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                blob = json.load(f)
            if blob.get("key") == cache_key:
                return {int(k): v for k, v in blob["scores"].items()}
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    print(f"scoring {len(codepoints)} glyph pairs (one-off, cached afterwards)...")
    ref_r = GlyphRenderer(ref_font, SCORE_RENDER)
    tgt_r = GlyphRenderer(tgt_font, SCORE_RENDER)
    scores = {}
    for i, cp in enumerate(codepoints):
        a, b = ref_r.render(cp), tgt_r.render(cp)
        scores[cp] = similarity(shape_feature(a), shape_feature(b)) if a and b else 0.0
        if (i + 1) % 1000 == 0:
            print(f"  {i + 1}/{len(codepoints)}")

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"key": cache_key, "scores": {str(k): v for k, v in scores.items()}}, f)
    return scores


class Reviewer:
    def __init__(self, root, items, ref_font, tgt_font, paths, only_undecided, panel):
        self.root = root
        self.panel = panel
        self.state_path, self.out_txt = paths
        self.state = self._load_state()

        if only_undecided:
            items = [it for it in items if chr(it[0]) not in self.state]
            if not items:
                messagebox.showinfo("done", "Nothing left to review.")
                root.destroy()
                return
        self.items = items
        self.idx = self._first_undecided()

        self.ref_r = GlyphRenderer(ref_font, 256)
        self.tgt_r = GlyphRenderer(tgt_font, 256)
        self._photos = []

        root.title(f"same glyph form?   {os.path.basename(ref_font)}  vs  {os.path.basename(tgt_font)}")
        root.configure(bg=BG)

        self.header = tk.Label(root, bg=BG, fg=FG, font=("Consolas", 13), pady=6)
        self.header.pack(fill="x")
        self.canvas = tk.Label(root, bg=BG)
        self.canvas.pack(padx=10)
        self.verdict = tk.Label(root, bg=BG, fg=DIM, font=("Consolas", 12), pady=4)
        self.verdict.pack(fill="x")
        tk.Label(root, bg=BG, fg=DIM, font=("Consolas", 10), pady=6,
                 text="y/space same   n/x different   left/right move   u undo   "
                      "A accept rest   ctrl+s export"
                 ).pack(fill="x")

        for k in ("y", "Y", "1"):
            root.bind(k, lambda e: self._decide(SAME))
        root.bind("<space>", lambda e: self._decide(SAME))
        for k in ("n", "N", "0", "x", "X"):
            root.bind(k, lambda e: self._decide(DIFF))
        root.bind("<Left>", lambda e: self._move(-1))
        root.bind("<Right>", lambda e: self._move(1))
        root.bind("u", lambda e: self._undo())
        root.bind("A", lambda e: self._accept_rest())
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
        """Persist progress and the character list after every decision.

        Both files are written to a temporary path and swapped in, so a crash
        mid-write cannot leave a truncated file behind.
        """
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
        cp, score = self.items[self.idx]
        ch = chr(cp)
        size = self.panel
        blank = Image.new("RGB", (256, 256), "white")

        gap = 10
        strip = Image.new("RGB", (size * 2 + gap, size), (30, 30, 30))
        for i, img in enumerate([self.ref_r.render(cp) or blank, self.tgt_r.render(cp) or blank]):
            if img.size != (size, size):
                img = img.resize((size, size), Image.LANCZOS)
            strip.paste(img, (i * (size + gap), 0))
        d = ImageDraw.Draw(strip)
        d.text((4, 4), "ref", fill=(120, 120, 120))
        d.text((size + gap + 4, 4), "target", fill=(120, 120, 120))

        photo = ImageTk.PhotoImage(strip)
        self._photos = [photo]
        self.canvas.configure(image=photo)

        n_same = sum(1 for v in self.state.values() if v == SAME)
        self.header.configure(
            text=f"  {ch}   U+{cp:04X}   sim {score:.3f}        "
                 f"{self.idx + 1} / {len(self.items)}        "
                 f"decided {len(self.state)}   same {n_same}"
        )

        v = self.state.get(ch)
        self.verdict.configure(
            text={SAME: ">> SAME FORM <<", DIFF: ">> DIFFERENT <<"}.get(v, "undecided"),
            fg={SAME: OK, DIFF: NO}.get(v, DIM),
        )

    def _decide(self, value):
        self.state[chr(self.items[self.idx][0])] = value
        self._save_state()
        self._move(1)

    def _undo(self):
        if self.state.pop(chr(self.items[self.idx][0]), None) is not None:
            self._save_state()
        self._render()

    def _accept_rest(self):
        pending = [chr(cp) for cp, _ in self.items[self.idx:] if chr(cp) not in self.state]
        if not pending:
            messagebox.showinfo("nothing to do", "No undecided characters from here on.")
            return
        if not messagebox.askyesno(
            "accept the rest?",
            f"Mark {len(pending)} undecided characters from position {self.idx + 1} "
            f"onward as SAME FORM?\n\nUndo one at a time with 'u'.",
        ):
            return
        for ch in pending:
            self.state[ch] = SAME
        self._save_state()
        self._render()

    def _move(self, step):
        self.idx = max(0, min(len(self.items) - 1, self.idx + step))
        self._render()

    def _export(self, verbose=False):
        same = sorted(ch for ch, v in self.state.items() if v == SAME)
        os.makedirs(os.path.dirname(self.out_txt), exist_ok=True)
        tmp = self.out_txt + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("".join(same))
        os.replace(tmp, self.out_txt)
        if verbose:
            messagebox.showinfo("exported", f"{len(same)} same-form characters -> {self.out_txt}")
        return len(same)

    def _on_close(self):
        n = self._export()
        print(f"decided {len(self.state)} / {len(self.items)}, {n} marked same")
        print(f"output -> {self.out_txt}")
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default=os.path.join(RUN_DIR, "ref.otf"))
    ap.add_argument("--target", default=os.path.join(RUN_DIR, "target.otf"),
                    help="Target font to compare against. Review one per run.")
    ap.add_argument("--charset", default="all",
                    choices=["all", "gb2312", "gbk", "big5", "jisx0208", "ksx1001"],
                    help="Restrict the review to a named charset (default: every shared Han glyph).")
    ap.add_argument("--sort", default="codepoint",
                    choices=["codepoint", "similarity", "suspicious"],
                    help="codepoint order by default. The similarity orderings are a weak "
                         "signal (known form differences land near the 39th percentile), "
                         "so treat them as a hint, not a filter.")
    ap.add_argument("--only-undecided", action="store_true")
    ap.add_argument("--session", help="Separate progress/export/cache name, e.g. target_ref. Defaults to the target filename stem.")
    ap.add_argument("--panel", type=int, default=300, help="Panel size in pixels.")
    args = ap.parse_args()
    if args.session and (args.session in (".", "..") or
                         any(c in args.session for c in '<>:"/\\|?*') or
                         args.session.endswith((".", " "))):
        ap.error("--session must be a plain filename stem without path separators.")

    ref_font, ref_path = load_font(args.ref)
    tgt_font, tgt_path = load_font(args.target)
    stem = args.session or tgt_path.stem

    shared = get_cjk_codepoints(ref_font) & get_cjk_codepoints(tgt_font)
    if args.charset != "all":
        shared &= get_charset_codepoints(args.charset)
    codepoints = sorted(shared)
    print(f"{len(codepoints)} characters renderable by both ref and {tgt_path.name}")

    score_cache = os.path.join(RUN_DIR, f"sameform_{stem}_scores.json")
    state_path = os.path.join(RUN_DIR, f"sameform_{stem}_state.json")
    out_txt = os.path.join(RUN_DIR, f"sameform_{stem}.txt")

    key = (f"ncc{SCORE_RENDER}-{SCORE_FEAT}-{SCORE_BLUR}:"
           f"{os.path.getmtime(ref_path)}:{os.path.getmtime(tgt_path)}:{args.charset}")
    scores = compute_scores(codepoints, str(ref_path), str(tgt_path), score_cache, key)

    items = [(cp, scores.get(cp, 0.0)) for cp in codepoints]
    if args.sort == "similarity":
        items.sort(key=lambda t: -t[1])
    elif args.sort == "suspicious":
        items.sort(key=lambda t: t[1])

    root = tk.Tk()
    app = Reviewer(root, items, str(ref_path), str(tgt_path),
                   (state_path, out_txt), args.only_undecided, args.panel)
    if getattr(app, "items", None):
        root.mainloop()


if __name__ == "__main__":
    main()
