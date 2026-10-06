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

import threading
from collections import deque
from dataclasses import dataclass

from report import Glyph


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

    def plain_lines(self) -> list[str]:
        return ["".join(g.ch for g in row).rstrip() for row in self.glyph_rows()]

    def glyph_rows(self) -> list[list[Glyph]]:
        committed, current, table, _inplace = self.snapshot()
        rows = [_cells_to_glyphs(line) for line in committed]
        if current:
            rows.append(_cells_to_glyphs(current))
        for line in table:
            rows.append(_cells_to_glyphs(line))
        return rows

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


def _cells_to_glyphs(cells: list[Cell]) -> list[Glyph]:
    return [Glyph(c.ch, c.fg, c.bold, c.dim, c.underline) for c in cells]
