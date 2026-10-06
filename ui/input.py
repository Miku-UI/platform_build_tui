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
import os
import shutil
import subprocess
import time

from ui.logbuf import Cell, LogBuffer
from ui.term import (
    Hit,
    Rect,
    _WHEEL_DOWN,
    _WHEEL_UP,
    _add,
    _cell_attr,
    clip_cells,
    dw,
    wrap_cells,
)

_SCROLL_TRACK = "▕"
_SCROLL_THUMB = "▐"


def thumb_geom(content: int, view: int, start: int, track: int) -> tuple[int, int]:
    view = max(1, view)
    track = max(1, track)
    content = max(view, content)
    thumb_h = max(1, min(track, (view * track) // content))
    max_start = content - view
    travel = track - thumb_h
    if max_start <= 0 or travel <= 0:
        return 0, track
    start = min(max(0, start), max_start)
    thumb_y = (start * travel + max_start // 2) // max_start
    return min(thumb_y, travel), thumb_h


def start_from_thumb(thumb_y: int, content: int, view: int, track: int) -> int:
    view = max(1, view)
    track = max(1, track)
    content = max(view, content)
    thumb_h = max(1, min(track, (view * track) // content))
    max_start = content - view
    travel = track - thumb_h
    if max_start <= 0 or travel <= 0:
        return 0
    thumb_y = min(max(0, thumb_y), travel)
    return (thumb_y * max_start + travel // 2) // travel


def col_selected(
    sel: tuple[tuple[int, int], tuple[int, int]], row: int, col: int, width: int
) -> bool:
    (r1, c1), (r2, c2) = sel
    if row < r1 or row > r2:
        return False
    lo = c1 if row == r1 else 0
    hi = c2 if row == r2 else 10**9
    return col < hi and col + width > lo


def row_width(row: list[Cell]) -> int:
    return sum(dw(cell.ch) or 1 for cell in row)


def copy_to_clipboard(text: str) -> None:
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


def draw_log(
    app,
    stdscr: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    *,
    log: LogBuffer,
    scroll_attr: str = "log_scroll",
) -> None:
    buf = log
    key = (id(buf), buf.generation, w)
    if app._wrap_key != key:
        committed, current, table, inplace = buf.snapshot()
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
        app._wrapped = wrapped
        app._wrap_key = key
    wrapped = app._wrapped
    max_off = max(0, len(wrapped) - h)
    owner = getattr(app, "_scroll_owner", app)
    scroll = getattr(owner, scroll_attr)
    if scroll <= 0:
        start = max(0, len(wrapped) - h)
    else:
        scroll = min(scroll, max_off)
        setattr(owner, scroll_attr, scroll)
        start = max(0, len(wrapped) - h - scroll)
    app._view_start = start
    view = wrapped[start : start + h]
    sel = sel_range(app)
    for i in range(h):
        row = view[i] if i < len(view) else []
        abs_row = start + i
        col = 0
        for cell in row:
            cw = dw(cell.ch) or 1
            attr = _cell_attr(cell)
            if sel and col_selected(sel, abs_row, col, cw):
                attr |= curses.A_REVERSE
            _add(stdscr, y + i, x + col, cell.ch, attr, cw)
            col += cw
        if col < w:
            fill_attr = curses.color_pair(7)
            if sel and col_selected(sel, abs_row, col, w - col):
                fill_attr |= curses.A_REVERSE
                _add(stdscr, y + i, x + col, " " * (w - col), fill_attr, w - col)


def draw_log_scrollbar(app, stdscr: curses.window, y: int, x: int, h: int) -> None:
    content = len(app._wrapped)
    if h <= 0 or content <= h:
        return
    app._scroll_geom = Rect(y, x, h, 2)
    thumb_y, thumb_h = thumb_geom(content, h, app._view_start, h)
    track_a = curses.color_pair(10)
    thumb_a = curses.color_pair(11) | curses.A_BOLD
    if app._scroll_drag is not None:
        thumb_a = curses.color_pair(15) | curses.A_BOLD
    for i in range(h):
        glyph = _SCROLL_THUMB if thumb_y <= i < thumb_y + thumb_h else _SCROLL_TRACK
        attr = thumb_a if glyph == _SCROLL_THUMB else track_a
        _add(stdscr, y + i, x, glyph, attr, 1)


def begin_scroll_drag(app, my: int) -> None:
    app._btn_down = False
    app._btn_hit = None
    app._selecting = False
    app._sel_a = None
    app._sel_b = None
    geom = app._scroll_geom
    content = len(app._wrapped)
    if geom.h <= 0 or content <= geom.h:
        return
    thumb_y, thumb_h = thumb_geom(content, geom.h, app._view_start, geom.h)
    rel = my - geom.y
    if thumb_y <= rel < thumb_y + thumb_h:
        app._scroll_drag = rel - thumb_y
    else:
        app._scroll_drag = thumb_h // 2
    apply_scroll_drag(app, my)


def apply_scroll_drag(app, my: int) -> None:
    geom = app._scroll_geom
    content = len(app._wrapped)
    if geom.h <= 0 or content <= geom.h or app._scroll_drag is None:
        return
    thumb_y = my - geom.y - app._scroll_drag
    start = start_from_thumb(thumb_y, content, geom.h, geom.h)
    owner = getattr(app, "_scroll_owner", app)
    attr = getattr(app, "_scroll_attr", "log_scroll")
    setattr(owner, attr, max(0, content - geom.h) - start)


def hit_at(app, y: int, x: int) -> Hit | None:
    for hit in reversed(app.hits):
        if hit.rect.contains(y, x):
            return hit
    return None


def cancel_press(app) -> None:
    app._btn_down = False
    app._btn_hit = None
    app._scroll_drag = None


def fire_click(app, action: str, payload: object) -> None:
    now = time.time()
    last_t, last_a, last_p = app._click_guard
    if now - last_t < 0.2 and last_a == action and last_p == payload:
        return
    app._click_guard = (now, action, payload)
    app._sel_a = None
    app._sel_b = None
    app._action(action, payload)


def handle_mouse(app) -> None:
    try:
        _id, mx, my, _z, bstate = curses.getmouse()
    except curses.error:
        return
    if bstate & _WHEEL_UP:
        cancel_press(app)
        handle_wheel(app, -3)
        return
    if bstate & _WHEEL_DOWN:
        cancel_press(app)
        handle_wheel(app, 3)
        return
    pressed = bool(bstate & curses.BUTTON1_PRESSED)
    released = bool(bstate & curses.BUTTON1_RELEASED)
    clicked = bool(bstate & curses.BUTTON1_CLICKED)
    report = bool(bstate & getattr(curses, "REPORT_MOUSE_POSITION", 0))
    if app._scroll_drag is not None:
        if pressed or report:
            apply_scroll_drag(app, my)
            return
        if released:
            apply_scroll_drag(app, my)
            cancel_press(app)
            return
        return
    if pressed and app._scroll_geom.contains(my, mx):
        begin_scroll_drag(app, my)
        return
    if clicked and not app._btn_down and app._scroll_geom.contains(my, mx):
        begin_scroll_drag(app, my)
        cancel_press(app)
        return
    in_log = app._log_geom.contains(my, mx) and app.showing_log()
    if app._selecting and (pressed or report):
        pos = log_pos(app, my, mx, clamp=True)
        if pos is not None:
            app._sel_b = pos
        return
    if pressed and in_log and not app._btn_down:
        pos = log_pos(app, my, mx, clamp=False)
        if pos is not None:
            app._selecting = True
            app._sel_a = pos
            app._sel_b = pos
        return
    if released and app._selecting:
        pos = log_pos(app, my, mx, clamp=True)
        if pos is not None:
            app._sel_b = pos
        copy_selection(app)
        app._selecting = False
        cancel_press(app)
        return
    if in_log:
        if released:
            cancel_press(app)
        return
    if pressed:
        if not app._btn_down:
            hit = hit_at(app, my, mx)
            app._btn_down = True
            app._btn_hit = (hit.action, hit.payload) if hit else None
        return
    if report:
        return
    if released:
        armed = app._btn_down
        press_hit = app._btn_hit
        cancel_press(app)
        hit = hit_at(app, my, mx)
        if hit is None:
            return
        if armed and press_hit != (hit.action, hit.payload):
            return
        fire_click(app, hit.action, hit.payload)
        return
    if clicked:
        if app._btn_down:
            return
        hit = hit_at(app, my, mx)
        if hit is not None:
            fire_click(app, hit.action, hit.payload)


def handle_wheel(app, delta: int) -> None:
    page = app.current_page()
    if page.overlay():
        return
    page.wheel(app.make_ctx(), delta)


def log_pos(app, y: int, x: int, clamp: bool) -> tuple[int, int] | None:
    geom = app._log_geom
    if geom.h <= 0 or geom.w <= 0:
        return None
    if clamp:
        y = min(max(y, geom.y), geom.y + geom.h - 1)
        x = min(max(x, geom.x), geom.x + geom.w - 1)
    elif not geom.contains(y, x):
        return None
    row = app._view_start + (y - geom.y)
    col = x - geom.x
    if row < 0:
        return (0, 0)
    if row >= len(app._wrapped):
        last = max(0, len(app._wrapped) - 1)
        return (last, row_width(app._wrapped[last] if app._wrapped else []))
    return (row, max(0, col))


def sel_range(app) -> tuple[tuple[int, int], tuple[int, int]] | None:
    if app._sel_a is None or app._sel_b is None:
        return None
    a, b = app._sel_a, app._sel_b
    if a > b:
        a, b = b, a
    if a == b:
        return None
    return (a, b)


def copy_selection(app) -> None:
    sel = sel_range(app)
    if sel is None:
        return
    (r1, c1), (r2, c2) = sel
    chunks: list[str] = []
    for idx in range(r1, min(r2 + 1, len(app._wrapped))):
        row = app._wrapped[idx]
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
        copy_to_clipboard(copied)
