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

import curses
import fcntl
import locale
import os
import struct
import termios
import unicodedata
from dataclasses import dataclass

from logbuf import Cell, _cells_to_glyphs
from report import Glyph

_WHEEL_UP = getattr(curses, "BUTTON4_PRESSED", 0x10000)
_WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0x200000)
_CLOSE_W = 3
_MIN_W = 3
_CYCLE_PREV = (curses.KEY_LEFT,)
_CYCLE_NEXT = (curses.KEY_RIGHT, ord(" "), curses.KEY_ENTER, 10, 13)


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


def _cycle(values: tuple, current, ch: int):
    if ch not in _CYCLE_PREV and ch not in _CYCLE_NEXT:
        return current
    n = len(values)
    if n == 0:
        return current
    idx = values.index(current) if current in values else 0
    step = -1 if ch in _CYCLE_PREV else 1
    return values[(idx + step) % n]


def _fmt_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


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


def _wrap_glyphs(glyphs: list[Glyph], width: int) -> list[list[Glyph]]:
    cells = [Cell(g.ch, g.fg, g.bold, g.dim, g.underline) for g in glyphs]
    return [_cells_to_glyphs(row) for row in wrap_cells(cells, width)]


def _glyphs_to_segs(glyphs: list[Glyph]) -> list[tuple[str, int]]:
    if not glyphs:
        return [("", curses.color_pair(7))]
    segs: list[tuple[str, int]] = []
    buf = [glyphs[0].ch]
    attr = _cell_attr(Cell(glyphs[0].ch, glyphs[0].fg, glyphs[0].bold, glyphs[0].dim, glyphs[0].underline))
    for g in glyphs[1:]:
        nxt = _cell_attr(Cell(g.ch, g.fg, g.bold, g.dim, g.underline))
        if nxt == attr:
            buf.append(g.ch)
            continue
        segs.append(("".join(buf), attr))
        buf = [g.ch]
        attr = nxt
    segs.append(("".join(buf), attr))
    return segs


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


def _add_segs(
    win: curses.window, y: int, x: int, segs: list[tuple[str, int]], width: int
) -> None:
    col = 0
    for text, attr in segs:
        if col >= width:
            break
        chunk = clip(text, width - col)
        if not chunk:
            continue
        _add(win, y, x + col, chunk, attr, width - col)
        col += dw(chunk)


def _tty_winsize(fd: int = 1) -> tuple[int, int] | None:
    try:
        rows, cols = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8)))[:2]
    except OSError:
        return None
    if rows < 1 or cols < 1:
        return None
    return rows, cols


def _sync_curses_size(stdscr: curses.window) -> bool:
    # The TUI daemon is not always the SIGWINCH target, so curses LINES/COLS
    # can stay frozen after a terminal resize. Read the tty size ourselves.
    size = _tty_winsize()
    if size is None:
        return False
    rows, cols = size
    try:
        cur_rows, cur_cols = stdscr.getmaxyx()
    except curses.error:
        cur_rows, cur_cols = 0, 0
    if rows == cur_rows and cols == cur_cols:
        return False
    for apply in (
        lambda: curses.resizeterm(rows, cols),
        lambda: curses.resize_term(rows, cols),
        lambda: stdscr.resize(rows, cols),
    ):
        try:
            apply()
            return True
        except (curses.error, AttributeError):
            continue
    return False


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


def _rounded_frame(
    win: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    attr: int,
    title: str = "",
    reserve_right: int = _MIN_W + _CLOSE_W,
) -> None:
    if h < 2 or w < 4:
        return
    inner = max(0, w - 2)
    usable = max(0, inner - max(0, reserve_right))
    if title:
        label = f" {title} "
        if dw(label) > usable:
            label = clip(label, usable)
        fill = max(0, usable - dw(label))
        top = "╭" + label + "─" * fill + "─" * max(0, reserve_right) + "╮"
    else:
        top = "╭" + "─" * usable + "─" * max(0, reserve_right) + "╮"
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
        curses.init_pair(37, 87, -1)
        curses.init_pair(38, 221, -1)
        curses.init_pair(39, 213, -1)
        curses.init_pair(40, 75, -1)
        curses.init_pair(41, 114, -1)
        curses.init_pair(42, 250, -1)
        curses.init_pair(44, 66, -1)
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
    curses.init_pair(37, curses.COLOR_CYAN, -1)
    curses.init_pair(38, curses.COLOR_YELLOW, -1)
    curses.init_pair(39, curses.COLOR_MAGENTA, -1)
    curses.init_pair(40, curses.COLOR_BLUE, -1)
    curses.init_pair(41, curses.COLOR_GREEN, -1)
    curses.init_pair(42, curses.COLOR_WHITE, -1)
    curses.init_pair(44, curses.COLOR_CYAN, -1)


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
