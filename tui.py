# Copyright (C) 2026 Miku UI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import base64
import curses
import locale
import os
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from art import SPARKS, banner_lines, logo_lines
from builder import BuildConfig, BuildSession
from devices import Product
from i18n import LANG_CHIPS, detect_lang, t
from sysinfo import HostMonitor, fmt_freq, fmt_pair

MODE_CONFIG = "config"
MODE_PICKER = "picker"
MODE_BUILD = "build"
MODE_DONE = "done"

CLEAN_NONE = "none"
CLEAN_INSTALL = "installclean"
CLEAN_FULL = "clean"

_FOCUS = ("device", "jobs", "gapps", "ccache", "clean", "build")
_CYCLE_PREV = (curses.KEY_LEFT,)
_CYCLE_NEXT = (curses.KEY_RIGHT, ord(" "), curses.KEY_ENTER, 10, 13)
_WHEEL_UP = getattr(curses, "BUTTON4_PRESSED", 0x10000)
_WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0x200000)


def _cycle(values: tuple, current, ch: int):
    if ch not in _CYCLE_PREV and ch not in _CYCLE_NEXT:
        return current
    n = len(values)
    if n == 0:
        return current
    idx = values.index(current) if current in values else 0
    step = -1 if ch in _CYCLE_PREV else 1
    return values[(idx + step) % n]


@dataclass
class Rect:
    y: int
    x: int
    h: int
    w: int

    def contains(self, y: int, x: int) -> bool:
        return self.y <= y < self.y + self.h and self.x <= x < self.x + self.w


@dataclass
class Hit:
    rect: Rect
    action: str
    payload: object = None


@dataclass
class Cell:
    ch: str
    fg: int = -1
    bold: bool = False
    dim: bool = False
    underline: bool = False


class LogBuffer:
    """VT-ish buffer: SGR, CR, CUP, scrolling margins (soong action table)."""

    def __init__(self, maxlen: int = 12000) -> None:
        self.lines: deque[list[Cell]] = deque(maxlen=maxlen)
        self.cur: list[Cell] = []
        self.col = 0
        self.cursor_row = 1
        self.margin_top = 1
        self.margin_bottom = 0
        self.term_rows = 24
        self.term_cols = 80
        self.table: dict[int, list[Cell]] = {}
        self.fg = -1
        self.bold = False
        self.dim = False
        self.underline = False
        self._esc = ""
        self._inplace = False
        self._lock = threading.Lock()
        self.generation = 0

    def set_size(self, rows: int, cols: int) -> None:
        with self._lock:
            self.term_rows = max(2, rows)
            self.term_cols = max(20, cols)

    def feed(self, text: str) -> None:
        with self._lock:
            for ch in text:
                if self._esc:
                    self._esc += ch
                    if self._esc_complete():
                        self._apply_esc(self._esc)
                        self._esc = ""
                    elif len(self._esc) > 96:
                        self._esc = ""
                    continue
                if ch == "\x1b":
                    self._esc = "\x1b"
                    continue
                self._put(ch)
            self.generation += 1

    def snapshot(self) -> tuple[list[list[Cell]], list[Cell], list[list[Cell]], bool]:
        with self._lock:
            table = [
                list(self.table[row])
                for row in sorted(self.table)
                if any(cell.ch != " " for cell in self.table[row])
            ]
            return [list(line) for line in self.lines], list(self.cur), table, self._inplace

    def _in_table(self) -> bool:
        return self.margin_bottom > 0 and self.cursor_row > self.margin_bottom

    def _active(self) -> list[Cell]:
        if self._in_table():
            line = self.table.get(self.cursor_row)
            if line is None:
                line = []
                self.table[self.cursor_row] = line
            return line
        return self.cur

    def _set_active(self, line: list[Cell]) -> None:
        if self._in_table():
            self.table[self.cursor_row] = line
        else:
            self.cur = line

    def _put(self, ch: str) -> None:
        if ch == "\n":
            if self._in_table():
                self.cursor_row += 1
                self.col = 0
                return
            self.lines.append(self.cur)
            self.cur = []
            self.col = 0
            self._inplace = False
            return
        if ch == "\r":
            self.col = 0
            if not self._in_table():
                self._inplace = True
            return
        if ch in ("\b", "\x08"):
            self.col = max(0, self.col - 1)
            return
        if ch == "\t":
            nxt = (self.col + 8) // 8 * 8
            while self.col < nxt:
                self._write(" ")
            return
        if ch in ("\a", "\x0e", "\x0f", "\x00"):
            return
        self._write(ch)

    def _write(self, ch: str) -> None:
        line = self._active()
        cell = Cell(ch, self.fg, self.bold, self.dim, self.underline)
        if self.col < len(line):
            line[self.col] = cell
        else:
            while len(line) < self.col:
                line.append(Cell(" ", self.fg, self.bold, self.dim, self.underline))
            line.append(cell)
        self.col += 1

    def _esc_complete(self) -> bool:
        s = self._esc
        if s.startswith("\x1b]"):
            return s.endswith("\x07") or s.endswith("\x1b\\")
        if s.startswith("\x1b["):
            return len(s) > 2 and "@" <= s[-1] <= "~"
        if len(s) >= 2 and s[1] in "()":
            return len(s) >= 3
        return len(s) >= 2

    def _apply_esc(self, seq: str) -> None:
        if not seq.startswith("\x1b[") or len(seq) < 3:
            return
        body, cmd = seq[2:-1], seq[-1]
        if body.startswith("?"):
            return
        if cmd == "m":
            self._sgr(body)
        elif cmd == "K":
            try:
                mode = int(body) if body else 0
            except ValueError:
                mode = 0
            line = self._active()
            if mode == 0:
                self._set_active(line[: self.col])
            elif mode == 1:
                blank = Cell(" ", self.fg, self.bold, self.dim, self.underline)
                for i in range(min(self.col, len(line))):
                    line[i] = blank
            else:
                self._set_active([])
        elif cmd == "C":
            try:
                self.col += max(1, int(body or "1"))
            except ValueError:
                self.col += 1
        elif cmd == "D":
            try:
                self.col = max(0, self.col - max(1, int(body or "1")))
            except ValueError:
                self.col = max(0, self.col - 1)
        elif cmd == "G":
            try:
                self.col = max(0, int(body or "1") - 1)
            except ValueError:
                self.col = 0
            if not self._in_table():
                self._inplace = True
        elif cmd == "A":
            try:
                n = max(1, int(body or "1"))
            except ValueError:
                n = 1
            self.cursor_row = max(1, self.cursor_row - n)
        elif cmd == "B":
            try:
                n = max(1, int(body or "1"))
            except ValueError:
                n = 1
            self.cursor_row += n
        elif cmd in "Hf":
            self._cup(body)
        elif cmd == "r":
            self._decstbm(body)

    def _cup(self, body: str) -> None:
        parts = body.split(";") if body else []
        try:
            row = int(parts[0]) if parts and parts[0] else 1
        except ValueError:
            row = 1
        try:
            col = int(parts[1]) if len(parts) > 1 and parts[1] else 1
        except ValueError:
            col = 1
        self.cursor_row = max(1, row)
        self.col = max(0, col - 1)

    def _decstbm(self, body: str) -> None:
        if not body:
            self.margin_top = 1
            self.margin_bottom = self.term_rows
            return
        parts = body.split(";")
        try:
            top = int(parts[0]) if parts and parts[0] else 1
        except ValueError:
            top = 1
        try:
            bottom = int(parts[1]) if len(parts) > 1 and parts[1] else self.term_rows
        except ValueError:
            bottom = self.term_rows
        self.margin_top = max(1, top)
        self.margin_bottom = max(self.margin_top, bottom)
        self.table = {row: line for row, line in self.table.items() if row > self.margin_bottom}

    def _sgr(self, body: str) -> None:
        parts: list[int] = []
        for raw in body.split(";") if body else ["0"]:
            try:
                parts.append(int(raw) if raw else 0)
            except ValueError:
                parts.append(0)
        if not parts:
            parts = [0]
        i = 0
        while i < len(parts):
            n = parts[i]
            if n == 0:
                self.fg = -1
                self.bold = False
                self.dim = False
                self.underline = False
            elif n == 1:
                self.bold = True
            elif n == 2:
                self.dim = True
            elif n == 4:
                self.underline = True
            elif n == 22:
                self.bold = False
                self.dim = False
            elif n == 24:
                self.underline = False
            elif 30 <= n <= 37:
                self.fg = n - 30
            elif n == 39:
                self.fg = -1
            elif 90 <= n <= 97:
                self.fg = 8 + (n - 90)
            elif n == 38 and i + 1 < len(parts):
                if parts[i + 1] == 5 and i + 2 < len(parts):
                    self.fg = _xterm256_to_16(parts[i + 2])
                    i += 2
                elif parts[i + 1] == 2:
                    i += min(4, len(parts) - i - 1)
            i += 1


def _xterm256_to_16(n: int) -> int:
    if n < 8:
        return n
    if n < 16:
        return n
    if n > 231:
        return 7 if n >= 244 else 0
    n -= 16
    r, g, b = n // 36, (n // 6) % 6, n % 6
    if r == g == b:
        return 7 if r > 2 else 0
    mx = max(r, g, b)
    if mx == r:
        return 9 if r > 3 else 1
    if mx == g:
        return 10 if g > 3 else 2
    return 12 if b > 3 else 4


def wrap_cells(cells: list[Cell], width: int) -> list[list[Cell]]:
    width = max(1, width)
    if not cells:
        return [[]]
    rows: list[list[Cell]] = []
    row: list[Cell] = []
    used = 0
    for cell in cells:
        cw = dw(cell.ch) or 1
        if used + cw > width and row:
            rows.append(row)
            row = []
            used = 0
        row.append(cell)
        used += cw
    if row:
        rows.append(row)
    return rows or [[]]


def clip_cells(cells: list[Cell], width: int) -> list[Cell]:
    width = max(1, width)
    out: list[Cell] = []
    used = 0
    for cell in cells:
        cw = dw(cell.ch) or 1
        if used + cw > width:
            break
        out.append(cell)
        used += cw
    return out


class BuildTui:
    def __init__(
        self,
        top: Path,
        products: list[Product],
        release: str,
        jobs: int,
        gapps: bool,
        ccache: bool,
        clean: str,
        variant: str,
    ) -> None:
        self.top = top
        self.products = products
        self.release = release
        self.variant = variant
        self.jobs = max(1, jobs)
        self.gapps = gapps
        self.ccache = ccache
        self.clean = clean
        self.selected: Product | None = products[0] if len(products) == 1 else None
        self.mode = MODE_CONFIG
        self.focus = 0
        self.picker_index = 0
        self.picker_off = 0
        self.log = LogBuffer()
        self.log_scroll = 0
        self.session: BuildSession | None = None
        self._stopping: BuildSession | None = None
        self.hits: list[Hit] = []
        self.status_key = ""
        self.status_args: dict[str, object] = {}
        self.lang = detect_lang()
        self._last_click = (0.0, -1, -1)
        self._stdscr: curses.window | None = None
        self._log_geom = Rect(0, 0, 0, 0)
        self._wrapped: list[list[Cell]] = []
        self._view_start = 0
        self._selecting = False
        self._sel_a: tuple[int, int] | None = None
        self._sel_b: tuple[int, int] | None = None
        self._wrap_key: tuple[int, int] | None = None
        self.host = HostMonitor(top)

    def _t(self, key: str, **kwargs: object) -> str:
        return t(self.lang, key, **kwargs)

    def _set_status(self, key: str = "", **kwargs: object) -> None:
        self.status_key = key
        self.status_args = kwargs

    def run(self) -> int:
        _ensure_utf8()
        return curses.wrapper(self._main)

    def _main(self, stdscr: curses.window) -> int:
        self._stdscr = stdscr
        curses.curs_set(0)
        curses.use_default_colors()
        stdscr.keypad(True)
        stdscr.timeout(80)
        _init_colors()
        _enable_mouse()
        try:
            while True:
                self._reap()
                self._draw(stdscr)
                ch = stdscr.getch()
                if ch == curses.ERR:
                    continue
                if ch == curses.KEY_RESIZE:
                    continue
                if ch == curses.KEY_MOUSE:
                    self._mouse()
                    continue
                if self._key(ch):
                    return 0
        finally:
            _disable_mouse()

    def _reap(self) -> None:
        session = self.session
        if self.mode == MODE_BUILD and session is not None and not session.running:
            code = session.returncode
            session.close()
            self.mode = MODE_DONE
            if session.stopped:
                self._set_status("stopped")
            elif code == 0:
                self._set_status("build_ok")
            else:
                self._set_status("build_fail", code=code)
            self.host.tick(force_disk=True)
        if self._stopping is not None and not self._stopping.running:
            self._stopping = None

    def _draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        self.hits = []
        rows, cols = stdscr.getmaxyx()
        if rows < 16 or cols < 60:
            _add(stdscr, 0, 0, self._t("term_too_small"), curses.color_pair(2) | curses.A_BOLD, cols)
            stdscr.refresh()
            return
        left, _gap, cy, cx, ch, cw = _panel_geom(rows, cols)
        self.host.tick()
        self._draw_banner(stdscr, rows, left)
        if self.mode == MODE_PICKER:
            self._draw_picker(stdscr, cy, cx, ch, cw)
        elif self.mode in (MODE_BUILD, MODE_DONE):
            self._draw_build(stdscr, cy, cx, ch, cw)
        else:
            self._draw_config(stdscr, cy, cx, ch, cw)
        stdscr.refresh()

    def _draw_banner(self, stdscr: curses.window, rows: int, width: int) -> None:
        inner = max(0, width - 2)
        x0 = 1
        info = self._host_info_rows(inner)
        logo = logo_lines(inner)
        footer = len(logo) + len(info)
        gap_art = 1 if footer else 0
        gap_logo = 1 if logo and info else 0
        art_h = max(0, rows - footer - gap_art - gap_logo)
        lines, crop = banner_lines(inner, art_h)
        if not lines:
            gap_art = 0
        block = len(lines) + gap_art + len(logo) + gap_logo + len(info)
        if block > rows:
            gap_art = 0
            block = len(lines) + len(logo) + gap_logo + len(info)
        if block > rows:
            gap_logo = 0
            block = len(lines) + len(logo) + len(info)
        y0 = max(0, (rows - min(block, rows)) // 2)
        n = len(lines)
        for i, line in enumerate(lines):
            y = y0 + i
            x = x0
            for ch in line:
                if ch != " " and 0 <= y < rows:
                    _add(stdscr, y, x, ch, _art_attr(ch, i + crop, n + crop), 1)
                x += 1
        for dy, dx, glyph in SPARKS:
            y, x = y0 + dy - crop, x0 + dx
            if y0 <= y < y0 + n and 1 <= x < width:
                _add(stdscr, y, x, glyph, curses.color_pair(14) | curses.A_DIM, 1)
        y = y0 + n
        if gap_art:
            y += 1
        logo_attr = curses.color_pair(11) | curses.A_BOLD
        for line in logo:
            if y >= rows:
                break
            text = clip(line, inner)
            lx = x0 + max(0, (inner - dw(text)) // 2)
            _add(stdscr, y, lx, text, logo_attr, dw(text) or 1)
            y += 1
        if gap_logo:
            y += 1
        for text, pair in info:
            if y >= rows:
                break
            _add(stdscr, y, x0, text, curses.color_pair(pair), inner)
            y += 1

    def _host_info_rows(self, width: int) -> list[tuple[str, int]]:
        host = self.host
        cpu_bits = []
        freq = fmt_freq(host.cpu_mhz)
        if freq:
            cpu_bits.append(freq)
        if host.cpu_pct is not None:
            cpu_bits.append(f"{host.cpu_pct:.0f}%")
        cpu_stat = " · ".join(cpu_bits)
        fields: list[tuple[str, str, str, int]] = [
            (self._t("info_rom"), host.rom, "", 32),
            (self._t("info_os"), host.distro, "", 33),
            (self._t("info_cpu"), host.cpu_model, cpu_stat, 34),
            (self._t("info_ram"), fmt_pair(host.mem_used, host.mem_total), "", 35),
            (self._t("info_disk"), fmt_pair(host.disk_used, host.disk_total), "", 36),
        ]
        label_w = max((dw(name) for name, _value, _extra, _pair in fields), default=0)
        rows: list[tuple[str, int]] = []
        for name, value, extra, pair in fields:
            pad = " " * max(0, label_w - dw(name))
            prefix = f"{name}{pad}  "
            indent = " " * dw(prefix)
            body_w = max(8, width - dw(prefix))
            wrapped = wrap_words(value, body_w)
            if extra:
                wrapped.extend(wrap_words(extra, body_w))
            if not wrapped:
                wrapped = [""]
            rows.append((clip(prefix + wrapped[0], width), pair))
            for more in wrapped[1:]:
                rows.append((clip(indent + more, width), pair))
        return rows

    def _draw_config(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), "✦  Cinderella")
        inner = max(10, w - 4)
        xx = x + 2
        row = y + 2
        limit = y + h - 6
        _add(stdscr, row, xx, self._combo_preview(), curses.color_pair(2) | curses.A_DIM, inner)
        row += 2
        if row < limit:
            _add(stdscr, row, xx, self._t("device"), curses.color_pair(15) | curses.A_DIM, inner)
            row += 1
        if row < limit:
            device_label = self.selected.label if self.selected else self._t("pick_device")
            self._box_btn(
                stdscr, row, xx, inner, f"{device_label}   ▾", "device", None, self._has_focus("device")
            )
            row += 2
        if row < limit:
            _add(stdscr, row, xx, self._t("jobs"), curses.color_pair(15) | curses.A_DIM, inner)
            row += 1
        if row < limit:
            self._jobs_row(stdscr, row, xx, inner)
            row += 2
        row = self._option_block(
            stdscr, row, xx, inner, limit, self._t("gapps"), self._yes_no(), self.gapps, "gapps"
        )
        row = self._option_block(
            stdscr, row, xx, inner, limit, self._t("ccache"), self._yes_no(), self.ccache, "ccache"
        )
        self._option_block(
            stdscr,
            row,
            xx,
            inner,
            limit,
            self._t("clean"),
            (
                (CLEAN_NONE, self._t("clean_none")),
                (CLEAN_INSTALL, self._t("clean_install")),
                (CLEAN_FULL, self._t("clean_full")),
            ),
            self.clean,
            "clean",
            gap=0,
        )
        if self.status_key:
            _add(stdscr, y + h - 6, xx, self._t(self.status_key, **self.status_args), curses.color_pair(6), inner)
        ready = self.selected is not None
        build_label = self._t("build_now")
        btn_h = 3
        btn_w = min(inner, max(22, dw(build_label) + 10))
        btn_x = x + max(0, (w - btn_w) // 2)
        btn_y = y + h - 5
        attr = curses.color_pair(3) | curses.A_BOLD
        if not ready:
            attr = curses.color_pair(10)
        elif self._has_focus("build"):
            attr = curses.color_pair(3) | curses.A_BOLD
        self._fill_btn(stdscr, btn_y, btn_x, btn_h, btn_w, build_label, attr, "build")
        self._draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _has_focus(self, name: str) -> bool:
        return 0 <= self.focus < len(_FOCUS) and _FOCUS[self.focus] == name

    def _set_focus(self, name: str) -> None:
        try:
            self.focus = _FOCUS.index(name)
        except ValueError:
            return

    def _yes_no(self) -> tuple[tuple[bool, str], tuple[bool, str]]:
        return ((True, self._t("yes")), (False, self._t("no")))

    def _jobs_row(self, stdscr: curses.window, y: int, x: int, width: int) -> None:
        focused = self._has_focus("jobs")
        minus_a = curses.color_pair(3) if focused else curses.color_pair(12)
        plus_a = minus_a
        _add(stdscr, y, x, "  −  ", minus_a | curses.A_BOLD, 5)
        self.hits.append(Hit(Rect(y, x, 1, 5), "jobs", -1))
        num = f"{self.jobs:^5d}"
        _add(stdscr, y, x + 6, num, curses.color_pair(11) | curses.A_BOLD, 5)
        self.hits.append(Hit(Rect(y, x + 6, 1, 5), "focus", "jobs"))
        _add(stdscr, y, x + 12, "  +  ", plus_a | curses.A_BOLD, 5)
        self.hits.append(Hit(Rect(y, x + 12, 1, 5), "jobs", 1))

    def _option_block(
        self,
        stdscr: curses.window,
        row: int,
        x: int,
        width: int,
        limit: int,
        title: str,
        options: tuple[tuple[object, str], ...],
        current: object,
        action: str,
        gap: int = 1,
    ) -> int:
        """Dim title plus a chip row. Returns the next row after optional gap."""
        if row < limit:
            _add(stdscr, row, x, title, curses.color_pair(15) | curses.A_DIM, width)
            row += 1
        if row < limit:
            self._chip_row(stdscr, row, x, width, options, current, action)
            row += 1
        return row + gap

    def _chip_row(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        width: int,
        options: tuple[tuple[object, str], ...],
        current: object,
        action: str,
    ) -> None:
        cx = x
        for value, label in options:
            chosen = current == value
            text = f" {label} "
            attr = curses.color_pair(3) | curses.A_BOLD if chosen else curses.color_pair(10)
            w = dw(text)
            if cx + w - x > width:
                break
            _add(stdscr, y, cx, text, attr, w)
            self.hits.append(Hit(Rect(y, cx, 1, w), action, value))
            cx += w + 1

    def _box_btn(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        width: int,
        label: str,
        action: str,
        payload: object,
        focused: bool,
    ) -> None:
        attr = curses.color_pair(3) | curses.A_BOLD if focused else curses.color_pair(12)
        _add(stdscr, y, x, clip(" " + label, width), attr, width)
        self.hits.append(Hit(Rect(y, x, 1, width), action, payload))

    def _fill_btn(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        h: int,
        w: int,
        label: str,
        attr: int,
        action: str,
    ) -> None:
        left = max(0, (w - dw(label)) // 2)
        right = max(0, w - left - dw(label))
        mid = y + h // 2
        for i in range(h):
            if y + i == mid:
                text = " " * left + label + " " * right
            else:
                text = " " * w
            _add(stdscr, y + i, x, text, attr, w)
        self.hits.append(Hit(Rect(y, x, h, w), action))

    def _draw_lang_bar(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        width: int,
        hint: str | None = None,
    ) -> None:
        hint = self._t("hint") if hint is None else hint
        chips: list[tuple[str, str, int]] = []
        chip_span = 0
        for code, label in LANG_CHIPS:
            text = f" {label} "
            w = dw(text)
            chips.append((code, text, w))
            chip_span += w + 1
        chip_span = max(0, chip_span - 1)
        cx = x + max(0, width - chip_span)
        if dw(hint) + 1 <= max(0, cx - x):
            _add(stdscr, y, x, hint, curses.color_pair(10), max(1, cx - x - 1))
        for code, text, w in chips:
            selected = code == self.lang
            attr = curses.color_pair(3) | curses.A_BOLD if selected else curses.color_pair(10)
            _add(stdscr, y, cx, text, attr, w)
            self.hits.append(Hit(Rect(y, cx, 1, w), "lang", code))
            cx += w + 1

    def _draw_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), f"✦  {self._t('picker_title')}")
        inner = max(10, w - 4)
        xx = x + 2
        _add(stdscr, y + 2, xx, self._t("picker_hint"), curses.color_pair(10), inner)
        list_y = y + 4
        list_h = max(1, (y + h - 3) - list_y)
        if self.picker_index < self.picker_off:
            self.picker_off = self.picker_index
        if self.picker_index >= self.picker_off + list_h:
            self.picker_off = self.picker_index - list_h + 1
        for i in range(list_h):
            idx = self.picker_off + i
            if idx >= len(self.products):
                break
            product = self.products[idx]
            selected = idx == self.picker_index
            attr = curses.color_pair(3) | curses.A_BOLD if selected else curses.color_pair(2)
            prefix = "▸ " if selected else "  "
            _add(stdscr, list_y + i, xx, clip(prefix + product.label, inner), attr, inner)
            self.hits.append(Hit(Rect(list_y + i, xx, 1, inner), "pick", idx))
        self._draw_lang_bar(stdscr, y + h - 2, xx, inner, self._t("picker_keys"))

    def _draw_build(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        session = self.session
        combo = session.config.lunch_combo if session else self._combo_preview()
        elapsed = ""
        if session is not None:
            end = session.finished_at or time.time()
            elapsed = "  " + _fmt_elapsed(end - session.started_at)
        if self.mode == MODE_BUILD:
            title = self._t("building")
        else:
            title = self._t(self.status_key, **self.status_args) if self.status_key else self._t("done")
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), f"✦  {title}{elapsed}")
        inner = max(10, w - 4)
        xx = x + 2
        _add(stdscr, y + 2, xx, combo, curses.color_pair(10), inner)
        log_top = y + 4
        log_h = max(3, h - 10)
        log_w = inner
        self._log_geom = Rect(log_top, xx, log_h, log_w)
        self.log.set_size(max(2, log_h), max(20, log_w))
        self._draw_log(stdscr, log_top, xx, log_h, log_w)
        if session is not None:
            session.set_winsize(max(2, log_h), max(20, log_w))
        btn = self._t("stop") if self.mode == MODE_BUILD else self._t("back")
        btn_attr = curses.color_pair(8) | curses.A_BOLD
        if self.mode == MODE_DONE:
            btn_attr = curses.color_pair(9) | curses.A_BOLD
        btn_w = min(inner, max(16, dw(btn) + 10))
        btn_x = x + max(0, (w - btn_w) // 2)
        btn_y = y + h - 5
        self._fill_btn(stdscr, btn_y, btn_x, 3, btn_w, btn, btn_attr, "stop_or_back")
        self._draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _draw_log(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        key = (self.log.generation, w)
        if self._wrap_key != key:
            committed, current, table, inplace = self.log.snapshot()
            wrapped: list[list[Cell]] = []
            for line in committed:
                wrapped.extend(wrap_cells(line, w))
            if current:
                if inplace:
                    wrapped.append(clip_cells(current, w))
                else:
                    wrapped.extend(wrap_cells(current, w))
            for line in table:
                wrapped.append(clip_cells(line, w))
            self._wrapped = wrapped
            self._wrap_key = key
        wrapped = self._wrapped
        max_off = max(0, len(wrapped) - h)
        if self.log_scroll <= 0:
            start = max(0, len(wrapped) - h)
        else:
            self.log_scroll = min(self.log_scroll, max_off)
            start = max(0, len(wrapped) - h - self.log_scroll)
        self._view_start = start
        view = wrapped[start : start + h]
        sel = self._sel_range()
        for i in range(h):
            row = view[i] if i < len(view) else []
            abs_row = start + i
            col = 0
            for cell in row:
                cw = dw(cell.ch) or 1
                attr = _cell_attr(cell)
                if sel and _col_selected(sel, abs_row, col, cw):
                    attr |= curses.A_REVERSE
                _add(stdscr, y + i, x + col, cell.ch, attr, cw)
                col += cw
            if col < w:
                fill_attr = curses.color_pair(7)
                if sel and _col_selected(sel, abs_row, col, w - col):
                    fill_attr |= curses.A_REVERSE
                    _add(stdscr, y + i, x + col, " " * (w - col), fill_attr, w - col)

    def _mouse(self) -> None:
        try:
            _id, mx, my, _z, bstate = curses.getmouse()
        except curses.error:
            return
        if bstate & _WHEEL_UP:
            self._wheel(-3)
            return
        if bstate & _WHEEL_DOWN:
            self._wheel(3)
            return
        pressed = bool(bstate & curses.BUTTON1_PRESSED)
        released = bool(bstate & curses.BUTTON1_RELEASED)
        clicked = bool(bstate & curses.BUTTON1_CLICKED)
        report = bool(bstate & getattr(curses, "REPORT_MOUSE_POSITION", 0))
        in_log = self.mode in (MODE_BUILD, MODE_DONE) and self._log_geom.contains(my, mx)
        if self._selecting and (pressed or report):
            pos = self._log_pos(my, mx, clamp=True)
            if pos is not None:
                self._sel_b = pos
            return
        if pressed and in_log:
            pos = self._log_pos(my, mx, clamp=False)
            if pos is not None:
                self._selecting = True
                self._sel_a = pos
                self._sel_b = pos
            return
        if released and self._selecting:
            pos = self._log_pos(my, mx, clamp=True)
            if pos is not None:
                self._sel_b = pos
            self._copy_selection()
            self._selecting = False
            return
        if not (clicked or released or pressed):
            return
        now = time.time()
        last_t, last_y, last_x = self._last_click
        if now - last_t < 0.12 and last_y == my and last_x == mx:
            return
        self._last_click = (now, my, mx)
        if in_log:
            return
        self._sel_a = None
        self._sel_b = None
        for hit in reversed(self.hits):
            if hit.rect.contains(my, mx):
                self._action(hit.action, hit.payload)
                return

    def _wheel(self, delta: int) -> None:
        if self.mode == MODE_PICKER:
            self.picker_index = min(max(0, self.picker_index + delta), max(0, len(self.products) - 1))
            return
        if self.mode in (MODE_BUILD, MODE_DONE):
            max_off = max(0, len(self._wrapped) - max(1, self._log_geom.h))
            self.log_scroll = min(max(0, self.log_scroll - delta), max_off)

    def _log_pos(self, y: int, x: int, clamp: bool) -> tuple[int, int] | None:
        geom = self._log_geom
        if geom.h <= 0 or geom.w <= 0:
            return None
        if clamp:
            y = min(max(y, geom.y), geom.y + geom.h - 1)
            x = min(max(x, geom.x), geom.x + geom.w - 1)
        elif not geom.contains(y, x):
            return None
        row = self._view_start + (y - geom.y)
        col = x - geom.x
        if row < 0:
            return (0, 0)
        if row >= len(self._wrapped):
            last = max(0, len(self._wrapped) - 1)
            return (last, _row_width(self._wrapped[last] if self._wrapped else []))
        return (row, max(0, col))

    def _sel_range(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if self._sel_a is None or self._sel_b is None:
            return None
        a, b = self._sel_a, self._sel_b
        if a > b:
            a, b = b, a
        if a == b:
            return None
        return (a, b)

    def _copy_selection(self) -> None:
        sel = self._sel_range()
        if sel is None:
            return
        (r1, c1), (r2, c2) = sel
        chunks: list[str] = []
        for idx in range(r1, min(r2 + 1, len(self._wrapped))):
            row = self._wrapped[idx]
            lo = c1 if idx == r1 else 0
            hi = c2 if idx == r2 else 10**9
            col = 0
            piece: list[str] = []
            for cell in row:
                cw = dw(cell.ch) or 1
                if col < hi and col + cw > lo:
                    piece.append(cell.ch)
                col += cw
            chunks.append("".join(piece))
        copied = "\n".join(chunks)
        if copied:
            _copy_to_clipboard(copied)

    def _key(self, ch: int) -> bool:
        if ch in (3,):
            if self.mode == MODE_BUILD:
                self._action("stop_or_back")
                return False
            return True
        if self.mode == MODE_PICKER:
            return self._key_picker(ch)
        if self.mode in (MODE_BUILD, MODE_DONE):
            return self._key_build(ch)
        return self._key_config(ch)

    def _key_config(self, ch: int) -> bool:
        if ch in (ord("q"), ord("Q")):
            return True
        if ch in (9, curses.KEY_DOWN):
            self.focus = (self.focus + 1) % len(_FOCUS)
            return False
        if ch in (getattr(curses, "KEY_BTAB", 353), curses.KEY_UP):
            self.focus = (self.focus - 1) % len(_FOCUS)
            return False
        name = _FOCUS[self.focus]
        if name == "device" and ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._open_picker()
        elif name == "jobs":
            if ch in (curses.KEY_LEFT, ord("-")):
                self.jobs = max(1, self.jobs - 1)
            elif ch in (curses.KEY_RIGHT, ord("+"), ord("=")):
                self.jobs = min(256, self.jobs + 1)
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.jobs = max(1, self.jobs // 10)
            elif ord("0") <= ch <= ord("9"):
                value = self.jobs * 10 + (ch - ord("0"))
                self.jobs = min(256, value)
        elif name == "gapps":
            self.gapps = _cycle((True, False), self.gapps, ch)
        elif name == "ccache":
            self.ccache = _cycle((True, False), self.ccache, ch)
        elif name == "clean":
            self.clean = _cycle((CLEAN_NONE, CLEAN_INSTALL, CLEAN_FULL), self.clean, ch)
        elif name == "build" and ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._start_build()
        return False

    def _key_picker(self, ch: int) -> bool:
        if ch in (27, ord("q"), ord("Q")):
            self.mode = MODE_CONFIG
            return False
        if ch in (curses.KEY_UP,):
            self.picker_index = max(0, self.picker_index - 1)
        elif ch in (curses.KEY_DOWN,):
            self.picker_index = min(len(self.products) - 1, self.picker_index + 1)
        elif ch in (curses.KEY_NPAGE,):
            self.picker_index = min(len(self.products) - 1, self.picker_index + 10)
        elif ch in (curses.KEY_PPAGE,):
            self.picker_index = max(0, self.picker_index - 10)
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._action("pick", self.picker_index)
        return False

    def _key_build(self, ch: int) -> bool:
        if ch in (curses.KEY_UP, _WHEEL_UP):
            self.log_scroll += 1
        elif ch in (curses.KEY_DOWN,):
            self.log_scroll = max(0, self.log_scroll - 1)
        elif ch in (curses.KEY_PPAGE,):
            self.log_scroll += 10
        elif ch in (curses.KEY_NPAGE,):
            self.log_scroll = max(0, self.log_scroll - 10)
        elif ch in (curses.KEY_END,):
            self.log_scroll = 0
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" "), 27):
            self._action("stop_or_back")
        elif ch in (ord("q"), ord("Q")) and self.mode == MODE_DONE:
            self._action("stop_or_back")
        return False

    def _action(self, action: str, payload: object = None) -> None:
        if action == "device":
            self._set_focus("device")
            self._open_picker()
        elif action == "jobs":
            self._set_focus("jobs")
            delta = int(payload or 0)
            self.jobs = min(256, max(1, self.jobs + delta))
        elif action == "focus" and payload == "jobs":
            self._set_focus("jobs")
        elif action == "gapps":
            self._set_focus("gapps")
            if isinstance(payload, bool):
                self.gapps = payload
        elif action == "ccache":
            self._set_focus("ccache")
            if isinstance(payload, bool):
                self.ccache = payload
        elif action == "clean":
            self._set_focus("clean")
            if isinstance(payload, str):
                self.clean = payload
        elif action == "build":
            self._set_focus("build")
            self._start_build()
        elif action == "pick" and isinstance(payload, int):
            if 0 <= payload < len(self.products):
                self.selected = self.products[payload]
                self.picker_index = payload
            self.mode = MODE_CONFIG
            self._set_status()
        elif action == "stop_or_back":
            if self.mode == MODE_BUILD:
                self._stop_build()
            elif self.mode == MODE_DONE:
                self.mode = MODE_CONFIG
        elif action == "lang" and isinstance(payload, str) and payload in {c for c, _n in LANG_CHIPS}:
            self.lang = payload
        elif action == "log":
            pass

    def _open_picker(self) -> None:
        if not self.products:
            self._set_status("no_devices")
            return
        if self.selected is not None:
            try:
                self.picker_index = self.products.index(self.selected)
            except ValueError:
                self.picker_index = 0
        self.mode = MODE_PICKER

    def _start_build(self) -> None:
        if self.selected is None:
            self._set_status("need_device")
            self._open_picker()
            return
        if self._stopping is not None and self._stopping.running:
            self._set_status("stopping")
            return
        log = LogBuffer()
        self.log = log
        self.log_scroll = 0
        self._wrapped = []
        self._wrap_key = None
        self._sel_a = None
        self._sel_b = None
        self._selecting = False
        self._set_status()
        config = BuildConfig(
            product=self.selected,
            jobs=self.jobs,
            gapps=self.gapps,
            ccache=self.ccache,
            clean=self.clean,
            release=self.release,
            variant=self.variant,
        )

        def on_data(text: str, buf: LogBuffer = log) -> None:
            buf.feed(text)

        session = BuildSession(self.top, config, on_data=on_data)
        self.session = session
        self.mode = MODE_BUILD
        session.start()
        rows, cols = self._stdscr.getmaxyx() if self._stdscr is not None else (24, 80)
        _left, _gap, _cy, _cx, ch, cw = _panel_geom(rows, cols)
        session.set_winsize(max(8, ch - 8), max(20, cw - 4))

    def _stop_build(self) -> None:
        session = self.session
        self.session = None
        self.mode = MODE_CONFIG
        self._set_status("stopped_build")
        if session is None:
            return
        self._stopping = session
        threading.Thread(target=session.stop, daemon=True).start()

    def _combo_preview(self) -> str:
        name = self.selected.product_name if self.selected else "?"
        extras = []
        if self.gapps:
            extras.append("GAPPS")
        if self.ccache:
            extras.append("ccache")
        if self.clean != CLEAN_NONE:
            extras.append(self.clean)
        extra = ("  " + " ".join(extras)) if extras else ""
        return f"{name}-{self.release}-{self.variant}  -j{self.jobs}{extra}"


def run_tui(
    top: Path,
    products: list[Product],
    release: str,
    jobs: int,
    gapps: bool,
    ccache: bool,
    clean: str,
    variant: str,
) -> int:
    app = BuildTui(top, products, release, jobs, gapps, ccache, clean, variant)
    try:
        return app.run()
    except curses.error as exc:
        print(t(detect_lang(), "tui_failed", exc=exc), file=sys.stderr)
        return 2


def _cell_attr(cell: Cell) -> int:
    fg = cell.fg
    if fg < 0:
        attr = curses.color_pair(7)
    elif fg == 0:
        attr = curses.color_pair(10)
    elif fg < 8:
        attr = curses.color_pair(16 + fg)
    else:
        attr = curses.color_pair(24 + (fg - 8)) | curses.A_BOLD
    if cell.bold:
        attr |= curses.A_BOLD
    if cell.dim:
        attr |= curses.A_DIM
    if cell.underline:
        attr |= curses.A_UNDERLINE
    return attr


def _col_selected(
    sel: tuple[tuple[int, int], tuple[int, int]], row: int, col: int, width: int
) -> bool:
    (r1, c1), (r2, c2) = sel
    if row < r1 or row > r2:
        return False
    lo = c1 if row == r1 else 0
    hi = c2 if row == r2 else 10**9
    return col < hi and col + width > lo


def _row_width(row: list[Cell]) -> int:
    return sum(dw(cell.ch) or 1 for cell in row)


def _copy_to_clipboard(text: str) -> None:
    payload = text.encode("utf-8")
    try:
        b64 = base64.b64encode(payload).decode("ascii")
        os.write(1, f"\033]52;c;{b64}\a".encode("ascii"))
    except OSError:
        pass
    for argv in (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["pbcopy"],
    ):
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.run(
                argv,
                input=payload,
                timeout=1.5,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        break


def dw(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def wrap_words(text: str, width: int) -> list[str]:
    width = max(1, width)
    if not text:
        return [""]
    if dw(text) <= width:
        return [text]
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if dw(trial) <= width:
            current = trial
            continue
        if current:
            lines.append(current)
        if dw(word) <= width:
            current = word
            continue
        chunk = ""
        for ch in word:
            if chunk and dw(chunk + ch) > width:
                lines.append(chunk)
                chunk = ch
            else:
                chunk += ch
        current = chunk
    if current:
        lines.append(current)
    return lines or [""]


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for ch in text:
        if unicodedata.combining(ch):
            if out:
                out.append(ch)
            continue
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def _add(win: curses.window, y: int, x: int, text: str, attr: int, width: int) -> None:
    rows, cols = win.getmaxyx()
    if y < 0 or y >= rows or x < 0 or x >= cols or width <= 0:
        return
    # Never write the last column: curses wraps that cell onto the next row.
    width = min(width, cols - x - 1)
    if width <= 0:
        return
    try:
        win.addstr(y, x, clip(text, width), attr)
    except curses.error:
        pass


def _split_cols(cols: int) -> tuple[int, int, int]:
    if cols >= 110:
        left, gap = 48, 3
    elif cols >= 90:
        left, gap = 42, 2
    else:
        left, gap = max(22, cols // 3), 2
    right = cols - left - gap
    if right < 36:
        gap = 1
        left = max(18, cols - 36 - gap)
        right = cols - left - gap
    return left, gap, right


def _panel_geom(rows: int, cols: int) -> tuple[int, int, int, int, int, int]:
    left, gap, _right = _split_cols(cols)
    card_x = left + gap
    card_y = 1
    card_h = max(12, rows - 2)
    card_w = max(24, cols - card_x - 1)
    return left, gap, card_y, card_x, card_h, card_w


def _art_attr(ch: str, row: int, n: int) -> int:
    t = row / max(1, n - 1)
    if ch in ".,'`:;":
        return curses.color_pair(10)
    if t < 0.06 or t > 0.92:
        return curses.color_pair(10)
    if ch in "0OKkXx":
        return curses.color_pair(11) | curses.A_BOLD
    return curses.color_pair(1)


def _rounded_frame(
    win: curses.window, y: int, x: int, h: int, w: int, attr: int, title: str = ""
) -> None:
    if h < 2 or w < 4:
        return
    inner = max(0, w - 2)
    if title:
        label = f" {title} "
        if dw(label) > inner:
            label = clip(label, inner)
        fill = max(0, inner - dw(label))
        top = "╭" + label + "─" * fill + "╮"
    else:
        top = "╭" + "─" * inner + "╮"
    _add(win, y, x, top, attr, w)
    for i in range(1, h - 1):
        _add(win, y + i, x, "│", attr, 1)
        _add(win, y + i, x + w - 1, "│", attr, 1)
    _add(win, y + h - 1, x, "╰" + "─" * inner + "╯", attr, w)


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    if curses.COLORS >= 256:
        curses.init_pair(1, 80, -1)
        curses.init_pair(2, 252, -1)
        curses.init_pair(3, 16, 80)
        curses.init_pair(4, 114, -1)
        curses.init_pair(5, 203, -1)
        curses.init_pair(6, 221, -1)
        curses.init_pair(7, 252, -1)
        curses.init_pair(8, 255, 160)
        curses.init_pair(9, 16, 114)
        curses.init_pair(10, 66, -1)
        curses.init_pair(11, 159, -1)
        curses.init_pair(12, 73, -1)
        curses.init_pair(14, 218, -1)
        curses.init_pair(15, 87, -1)
        curses.init_pair(16, 244, -1)
        curses.init_pair(17, 203, -1)
        curses.init_pair(18, 114, -1)
        curses.init_pair(19, 221, -1)
        curses.init_pair(20, 75, -1)
        curses.init_pair(21, 176, -1)
        curses.init_pair(22, 80, -1)
        curses.init_pair(23, 252, -1)
        curses.init_pair(24, 238, -1)
        curses.init_pair(25, 196, -1)
        curses.init_pair(26, 82, -1)
        curses.init_pair(27, 227, -1)
        curses.init_pair(28, 69, -1)
        curses.init_pair(29, 213, -1)
        curses.init_pair(30, 87, -1)
        curses.init_pair(31, 255, -1)
        curses.init_pair(32, 87, -1)
        curses.init_pair(33, 114, -1)
        curses.init_pair(34, 221, -1)
        curses.init_pair(35, 213, -1)
        curses.init_pair(36, 75, -1)
        return
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_WHITE, -1)
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(4, curses.COLOR_GREEN, -1)
    curses.init_pair(5, curses.COLOR_RED, -1)
    curses.init_pair(6, curses.COLOR_YELLOW, -1)
    curses.init_pair(7, curses.COLOR_WHITE, -1)
    curses.init_pair(8, curses.COLOR_WHITE, curses.COLOR_RED)
    curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(10, curses.COLOR_CYAN, -1)
    curses.init_pair(11, curses.COLOR_CYAN, -1)
    curses.init_pair(12, curses.COLOR_CYAN, -1)
    curses.init_pair(14, curses.COLOR_MAGENTA, -1)
    curses.init_pair(15, curses.COLOR_CYAN, -1)
    curses.init_pair(16, curses.COLOR_BLACK, -1)
    curses.init_pair(17, curses.COLOR_RED, -1)
    curses.init_pair(18, curses.COLOR_GREEN, -1)
    curses.init_pair(19, curses.COLOR_YELLOW, -1)
    curses.init_pair(20, curses.COLOR_BLUE, -1)
    curses.init_pair(21, curses.COLOR_MAGENTA, -1)
    curses.init_pair(22, curses.COLOR_CYAN, -1)
    curses.init_pair(23, curses.COLOR_WHITE, -1)
    for i in range(8):
        try:
            curses.init_pair(24 + i, (curses.COLOR_BLACK, curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_WHITE)[i], -1)
        except curses.error:
            pass
    curses.init_pair(32, curses.COLOR_CYAN, -1)
    curses.init_pair(33, curses.COLOR_GREEN, -1)
    curses.init_pair(34, curses.COLOR_YELLOW, -1)
    curses.init_pair(35, curses.COLOR_MAGENTA, -1)
    curses.init_pair(36, curses.COLOR_BLUE, -1)


def _ensure_utf8() -> None:
    try:
        locale.setlocale(locale.LC_ALL, "")
        enc = (locale.getpreferredencoding(False) or "").lower().replace("-", "")
        if enc in {"utf8", "utf_8"}:
            return
    except locale.Error:
        pass
    os.environ.pop("LC_ALL", None)
    for name in ("C.UTF-8", "C.utf8"):
        try:
            locale.setlocale(locale.LC_ALL, name)
            return
        except locale.Error:
            continue


def _disable_mouse() -> None:
    try:
        os.write(1, b"\033[?1000l\033[?1002l\033[?1003l\033[?1006l")
    except OSError:
        pass


def _enable_mouse() -> None:
    mask = (
        curses.BUTTON1_PRESSED
        | curses.BUTTON1_RELEASED
        | curses.BUTTON1_CLICKED
        | _WHEEL_UP
        | _WHEEL_DOWN
        | getattr(curses, "REPORT_MOUSE_POSITION", 0)
    )
    try:
        curses.mousemask(mask)
        curses.mouseinterval(0)
    except curses.error:
        pass
    try:
        os.write(1, b"\033[?1002h\033[?1006h")
    except OSError:
        pass


def _fmt_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"
