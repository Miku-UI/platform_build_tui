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

from art import about_art
from ctx import Ctx, Page
from term import _add, _rounded_frame, dw


class AboutPage(Page):
    def __init__(self, version: str) -> None:
        self.version = version

    def draw(self, ctx: Ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), "✦  Miku UI", ctx.chrome_reserve())
        inner = max(10, w - 4)
        xx = x + 2
        top = y + 2
        bottom = y + h - 3
        area_h = max(0, bottom - top + 1)
        title = "Miku UI Build System TUI"
        ver = f"ver {self.version}"
        art = about_art()
        keep = max(0, area_h - 2)
        if len(art) > keep:
            extra = len(art) - keep
            start = extra // 2
            art = art[start : start + keep]
        block = [*art, title, ver]
        if len(block) > area_h:
            block = block[:area_h]
        block_w = min(inner, max((dw(line) for line in block), default=0))
        bx = xx + max(0, (inner - block_w) // 2)
        y0 = top + max(0, (area_h - len(block)) // 2)
        art_n = min(len(art), len(block))
        for i, line in enumerate(block):
            yy = y0 + i
            if yy > bottom:
                break
            if i < art_n:
                lx = bx
                attr = curses.color_pair(11)
            else:
                lx = bx + max(0, (block_w - min(dw(line), block_w)) // 2)
                attr = curses.color_pair(11) | curses.A_BOLD if line == title else curses.color_pair(10)
            _add(stdscr, yy, lx, line, attr, max(1, xx + inner - lx))
        ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)
