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

from ui.term import Hit, Rect, _add, clip, dw


def yes_no(t) -> tuple[tuple[bool, str], tuple[bool, str]]:
    return ((True, t("yes")), (False, t("no")))


def jobs_row(
    stdscr: curses.window,
    hits: list[Hit],
    y: int,
    x: int,
    width: int,
    value: int,
    action: str,
    focus: str,
    focused: bool,
) -> None:
    minus_a = curses.color_pair(3) if focused else curses.color_pair(12)
    plus_a = minus_a
    _add(stdscr, y, x, "  −  ", minus_a | curses.A_BOLD, 5)
    hits.append(Hit(Rect(y, x, 1, 5), action, -1))
    num = f"{value:^5d}"
    _add(stdscr, y, x + 6, num, curses.color_pair(11) | curses.A_BOLD, 5)
    hits.append(Hit(Rect(y, x + 6, 1, 5), "focus", focus))
    _add(stdscr, y, x + 12, "  +  ", plus_a | curses.A_BOLD, 5)
    hits.append(Hit(Rect(y, x + 12, 1, 5), action, 1))


def chip_row(
    stdscr: curses.window,
    hits: list[Hit],
    y: int,
    x: int,
    width: int,
    options: tuple[tuple[object, str], ...],
    current: object,
    action: str,
    limit: int | None = None,
) -> int:
    cx = x
    y0 = y
    last_y = y
    drew = False
    for value, label in options:
        chosen = current == value
        text = f" {label} "
        attr = curses.color_pair(3) | curses.A_BOLD if chosen else curses.color_pair(10)
        w = dw(text)
        if w > width:
            text = clip(text, width)
            w = dw(text)
        if cx > x and cx + w - x > width:
            y += 1
            cx = x
        if limit is not None and y >= limit:
            break
        _add(stdscr, y, cx, text, attr, w)
        hits.append(Hit(Rect(y, cx, 1, w), action, value))
        last_y = y
        drew = True
        cx += w + 1
    return (last_y - y0 + 1) if drew else 1


def option_block(
    stdscr: curses.window,
    hits: list[Hit],
    row: int,
    x: int,
    width: int,
    limit: int,
    title: str,
    options: tuple[tuple[object, str], ...],
    current: object,
    action: str,
    gap: int = 1,
    title_attr: int | None = None,
) -> int:
    """Dim title plus a chip row. Returns the next row after optional gap."""
    if row < limit:
        attr = title_attr if title_attr is not None else curses.color_pair(15) | curses.A_DIM
        _add(stdscr, row, x, title, attr, width)
        row += 1
    if row < limit:
        row += chip_row(stdscr, hits, row, x, width, options, current, action, limit)
    return row + gap


def box_btn(
    stdscr: curses.window,
    hits: list[Hit],
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
    hits.append(Hit(Rect(y, x, 1, width), action, payload))


def text_field(
    stdscr: curses.window,
    hits: list[Hit],
    y: int,
    x: int,
    width: int,
    value: str,
    placeholder: str,
    action: str,
    focused: bool,
    cursor: int = 0,
) -> int:
    """One-line input. Returns the screen column of the cursor."""
    inner = max(1, width - 2)
    cursor = min(max(0, cursor), len(value))
    start = 0
    if cursor - start >= inner:
        start = cursor - inner + 1
    shown = value[start : start + inner] if value else clip(placeholder, inner)
    pad = max(0, inner - dw(shown))
    text = " " + shown + " " * pad
    if dw(text) < width:
        text += " "
    attr = curses.color_pair(13) | (curses.A_BOLD if focused else 0)
    _add(stdscr, y, x, clip(text, width), attr, width)
    hits.append(Hit(Rect(y, x, 1, width), action, None))
    return x + 1 + (cursor - start)


def fill_btn(
    stdscr: curses.window,
    hits: list[Hit],
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
    hits.append(Hit(Rect(y, x, h, w), action))
