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

"""Shell services for a page. Pages import this module, not tui."""

from __future__ import annotations

import curses

from logbuf import LogBuffer
from term import Hit, Rect


class Page:
    def draw(self, ctx: Ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        return

    def key(self, ctx: Ctx, ch: int) -> bool:
        return False

    def wheel(self, ctx: Ctx, delta: int) -> None:
        return

    def click(self, ctx: Ctx, action: str, payload: object = None) -> bool:
        return False

    def close(self) -> bool:
        return False

    def reap(self, ctx: Ctx) -> None:
        return

    def hide_close(self) -> bool:
        return False

    def showing_log(self) -> bool:
        return False

    def overlay(self) -> bool:
        return False

    def draw_overlay(self, ctx: Ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        return

    def key_overlay(self, ctx: Ctx, ch: int) -> bool:
        return False

    def consume_interrupt(self) -> bool:
        return False


class Ctx:
    def __init__(self, shell: object) -> None:
        self._sh = shell

    def t(self, key: str, **kwargs: object) -> str:
        return self._sh._t(key, **kwargs)

    @property
    def hits(self) -> list[Hit]:
        return self._sh.hits

    def chrome_reserve(self) -> int:
        return self._sh._chrome_reserve()

    def draw_lang_bar(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        width: int,
        voices: bool = False,
    ) -> None:
        self._sh._draw_lang_bar(stdscr, y, x, width, voices=voices)

    def draw_log(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        h: int,
        w: int,
        *,
        log: LogBuffer,
        scroll_attr: str = "log_scroll",
    ) -> None:
        self._sh._draw_log(stdscr, y, x, h, w, log=log, scroll_attr=scroll_attr)

    def draw_log_scrollbar(self, stdscr: curses.window, y: int, x: int, h: int) -> None:
        self._sh._draw_log_scrollbar(stdscr, y, x, h)

    @property
    def log_geom(self) -> Rect:
        return self._sh._log_geom

    @log_geom.setter
    def log_geom(self, rect: Rect) -> None:
        self._sh._log_geom = rect

    def set_scroll(self, owner: object, attr: str = "log_scroll") -> None:
        self._sh._scroll_owner = owner
        self._sh._scroll_attr = attr

    @property
    def wrapped(self) -> list:
        return self._sh._wrapped

    def invalidate_wrap(self) -> None:
        self._sh._wrap_key = None

    def clear_pointer_state(self) -> None:
        self._sh._wrapped = []
        self._sh._wrap_key = None
        self._sh._sel_a = None
        self._sh._sel_b = None
        self._sh._selecting = False
        self._sh._scroll_drag = None

    def clear_scroll_drag(self) -> None:
        self._sh._scroll_drag = None

    @property
    def stdscr(self):
        return self._sh._stdscr

    def action(self, action: str, payload: object = None) -> None:
        self._sh._action(action, payload)

    @property
    def leave(self) -> bool:
        return self._sh._leave

    def cycle_tab(self, step: int) -> None:
        self._sh._cycle_tab(step)

    def snapshot(self) -> tuple:
        return self._sh._prefs_snapshot()

    def persist_if_changed(self, before: tuple) -> None:
        self._sh._persist_if_changed(before)

    def tick_host(self, force_disk: bool = False) -> None:
        self._sh.host.tick(force_disk=force_disk)
