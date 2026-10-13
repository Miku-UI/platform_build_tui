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
from dataclasses import dataclass

from ui.term import Hit, Rect, _add, _rounded_frame, clip, dw, wrap_words


@dataclass
class Dialog:
    title_key: str
    choices: tuple[tuple[str, str], ...] = ()
    body_key: str = ""
    focus: int = 0
    kind: str = ""
    git_transport: str = "https"
    check_jobs: int = 8
    danger_payloads: tuple[str, ...] = ()


def paint_dialog(ctx, stdscr: curses.window, y: int, x: int, h: int, w: int, dlg: Dialog) -> None:
    title = ctx.t(dlg.title_key)
    wrap_w = max(8, w - 8)
    body_lines: list[str] = []
    if dlg.body_key:
        body_lines = wrap_words(ctx.t(dlg.body_key), wrap_w)
    choice_rows = [wrap_words(ctx.t(key), max(4, wrap_w - 2)) for _payload, key in dlg.choices]
    text_w = max(
        [dw(title), *(dw(line) for line in body_lines), *(dw(line) + 2 for rows in choice_rows for line in rows)],
        default=10,
    )
    box_w = min(w - 2, max(24, text_w + 4))
    box_h = 3 + len(body_lines) + (1 if body_lines else 0) + sum(len(rows) for rows in choice_rows) + 1
    box_h = min(max(5, box_h), h - 2)
    by = y + max(1, (h - box_h) // 2)
    bx = x + max(1, (w - box_w) // 2)
    fill_a = curses.color_pair(7)
    for i in range(box_h):
        _add(stdscr, by + i, bx, " " * box_w, fill_a, box_w)
    _rounded_frame(stdscr, by, bx, box_h, box_w, curses.color_pair(12), f"✦  {title}", 0)
    cy = by + 1
    cx = bx + 2
    cw = max(1, box_w - 4)
    for line in body_lines:
        cy += 1
        if cy >= by + box_h - 1:
            break
        _add(stdscr, cy, cx, line, curses.color_pair(2), cw)
    if body_lines:
        cy += 1
    for i, ((payload, _key), rows) in enumerate(zip(dlg.choices, choice_rows)):
        selected = i == dlg.focus
        if payload in dlg.danger_payloads:
            attr = curses.color_pair(8) | curses.A_BOLD if selected else curses.color_pair(5)
        else:
            attr = curses.color_pair(3) | curses.A_BOLD if selected else curses.color_pair(2)
        first = min(cy + 1, by + box_h - 2)
        for j, line in enumerate(rows):
            cy += 1
            if cy >= by + box_h - 1:
                break
            prefix = "▸ " if selected and j == 0 else "  "
            _add(stdscr, cy, cx, clip(prefix + line, cw), attr, cw)
        last = cy
        ctx.hits.append(Hit(Rect(first, cx, max(1, last - first + 1), cw), "dialog", payload))
