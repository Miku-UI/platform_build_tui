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
import os
import socket
import sys
from pathlib import Path

from art import SPARKS, about_art, banner_lines, logo_lines
from devices import Product
from i18n import LANG_CHIPS, detect_lang, t
from input import (
    cancel_press,
    draw_log,
    draw_log_scrollbar,
    handle_mouse,
    handle_wheel,
)
from logbuf import Cell, LogBuffer
from pages_build import (
    CLEAN_FULL,
    CLEAN_INSTALL,
    CLEAN_NONE,
    MODE_BUILD,
    BuildPage,
)
from pages_scm import ScmPage
from prefs import Prefs, load_prefs, save_prefs
from session import (
    accept_attach,
    client_gone,
    launch as launch_session,
    notify_client,
    release_tty,
    take_tty,
    try_accept,
)
from sysinfo import HostMonitor, fmt_freq, fmt_pair
from term import (
    Hit,
    Rect,
    _CLOSE_W,
    _MIN_W,
    _add,
    _disable_mouse,
    _enable_mouse,
    _ensure_utf8,
    _init_colors,
    _panel_geom,
    _rounded_frame,
    _sync_curses_size,
    clip,
    dw,
    wrap_words,
)

TUI_VERSION = "1.0.0"

TAB_BUILD = "build"
TAB_SCM = "scm"
TAB_ABOUT = "about"
TABS = (TAB_BUILD, TAB_SCM, TAB_ABOUT)

VOICE_MOE = "moe"
VOICE_PRO = "pro"
VOICE_CHIPS = (
    (VOICE_MOE, "Moe Mode"),
    (VOICE_PRO, "Pro Mode"),
)

_CLOSE_MARK = "[x]"
_MIN_MARK = "[-]"


class BuildTui:
    def __init__(
        self,
        top: Path,
        products: list[Product],
        release: str,
        jobs: int | None,
        gapps: bool | None,
        ccache: bool | None,
        clean: str,
        variant: str | None,
    ) -> None:
        self.top = top
        self.products = products
        self.release = release
        prefs = load_prefs(top)
        self.build = BuildPage(top, products, release, jobs, gapps, ccache, clean, variant, prefs)
        self.tab = TAB_BUILD
        self.hits: list[Hit] = []
        self.lang = prefs.lang or detect_lang()
        self.voice = prefs.voice or VOICE_PRO
        self._btn_down = False
        self._btn_hit: tuple[str, object] | None = None
        self._click_guard: tuple[float, str, object] = (0.0, "", None)
        self._stdscr: curses.window | None = None
        self._log_geom = Rect(0, 0, 0, 0)
        self._scroll_geom = Rect(0, 0, 0, 0)
        self._scroll_drag: int | None = None
        self._wrapped: list[list[Cell]] = []
        self._view_start = 0
        self._selecting = False
        self._sel_a: tuple[int, int] | None = None
        self._sel_b: tuple[int, int] | None = None
        self._wrap_key: tuple[int, int] | None = None
        self._leave = False
        self._detach = False
        self._listen: socket.socket | None = None
        self._client: socket.socket | None = None
        self._client_pgrp = 0
        self._steal: tuple | None = None
        self._devnull = -1
        self.host = HostMonitor(top)
        self.scm = ScmPage(top, prefs.git_transport, prefs.check_jobs)
        self._scroll_owner = self.build
        self._scroll_attr = "log_scroll"

    def _t(self, key: str, **kwargs: object) -> str:
        return t(self.lang, key, voice=self.voice, **kwargs)

    def showing_log(self) -> bool:
        if self.tab == TAB_SCM:
            return self.scm.showing_log()
        return self.tab == TAB_BUILD and self.build.showing_log()

    def run(self) -> int:
        _ensure_utf8()
        return curses.wrapper(self._main)

    def run_daemon(self, listen: socket.socket) -> int:
        self._devnull = os.open("/dev/null", os.O_RDWR)
        self._listen = listen
        pending: tuple | None = None
        code = 0
        while True:
            if pending is None:
                listen.setblocking(True)
                try:
                    conn, _addr = listen.accept()
                except OSError:
                    break
                try:
                    tty_fd, meta = accept_attach(conn)
                except OSError:
                    notify_client(conn, "exit 2")
                    continue
            else:
                conn, tty_fd, meta = pending
                pending = None
            self._client = conn
            self._client_pgrp = int(meta.get("pgrp") or 0)
            self._detach = False
            self._leave = False
            self._steal = None
            listen.setblocking(False)
            term = meta.get("term")
            if isinstance(term, str) and term:
                os.environ["TERM"] = term
            take_tty(tty_fd)
            try:
                _ensure_utf8()
                code = curses.wrapper(self._main)
            except curses.error:
                code = 2
            finally:
                for stream in (sys.stdout, sys.stderr):
                    try:
                        stream.flush()
                    except Exception:
                        pass
                release_tty(tty_fd, self._client_pgrp)
                if self._devnull >= 0:
                    os.dup2(self._devnull, 0)
                    os.dup2(self._devnull, 1)
                    os.dup2(self._devnull, 2)
                try:
                    os.close(tty_fd)
                except OSError:
                    pass
            steal = self._steal
            self._steal = None
            if steal is not None:
                notify_client(conn, "detach")
                pending = steal
                continue
            if self._detach:
                notify_client(conn, "detach")
                continue
            notify_client(conn, f"exit {int(code)}")
            break
        return code

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
                self._poll_session()
                if self._leave or self._detach:
                    return 0
                self._draw(stdscr)
                ch = stdscr.getch()
                if ch == curses.ERR:
                    continue
                if ch == curses.KEY_RESIZE:
                    continue
                if ch == curses.KEY_MOUSE:
                    self._mouse()
                    if self._leave or self._detach:
                        return 0
                    continue
                if self._key(ch):
                    return 0
        finally:
            _disable_mouse()

    def _poll_session(self) -> None:
        if client_gone(self._client):
            self._detach = True
            return
        if self._listen is None:
            return
        steal = try_accept(self._listen)
        if steal is not None:
            self._steal = steal
            self._detach = True

    def _reap(self) -> None:
        self.build.reap(self)
        self.scm.reap()

    def _draw(self, stdscr: curses.window) -> None:
        if _sync_curses_size(stdscr):
            stdscr.clear()
        else:
            stdscr.erase()
        self.hits = []
        self._scroll_geom = Rect(0, 0, 0, 0)
        self._log_geom = Rect(0, 0, 0, 0)
        self._scroll_owner = self.build
        self._scroll_attr = "log_scroll"
        rows, cols = stdscr.getmaxyx()
        if rows < 16 or cols < 60:
            _add(stdscr, 0, 0, self._t("term_too_small"), curses.color_pair(2) | curses.A_BOLD, cols)
            stdscr.refresh()
            return
        left, _gap, cy, cx, ch, cw = _panel_geom(rows, cols)
        self.host.tick()
        self._draw_banner(stdscr, rows, left)
        if self.tab == TAB_SCM:
            self.scm.draw(self, stdscr, cy, cx, ch, cw)
        elif self.tab == TAB_ABOUT:
            self._draw_about(stdscr, cy, cx, ch, cw)
        else:
            self.build.draw(self, stdscr, cy, cx, ch, cw)
        self._draw_tab_bar(stdscr, cy, cx, cw)
        self._draw_card_chrome(stdscr, cy, cx, cw)
        if self.tab == TAB_SCM and self.scm.dialog is not None:
            keep = [hit for hit in self.hits if hit.action in ("tab", "minimize", "quit")]
            self.hits = keep
            self.scm.draw_dialog(self, stdscr, cy, cx, ch, cw)
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

    def _show_close(self) -> bool:
        if self.tab == TAB_BUILD and self.build.hide_close():
            return False
        if self.tab == TAB_SCM and self.scm.hide_close():
            return False
        return True

    def _chrome_reserve(self) -> int:
        if self._show_close():
            return _MIN_W + _CLOSE_W
        return _MIN_W

    def _draw_tab_bar(self, stdscr: curses.window, y: int, x: int, w: int) -> None:
        xx = x + 2
        inner = max(10, w - 4)
        cx = xx
        for tab in TABS:
            label = f" {self._t(f'tab_{tab}')} "
            tw = dw(label)
            if cx + tw - xx > inner:
                break
            attr = curses.color_pair(3) | curses.A_BOLD if tab == self.tab else curses.color_pair(10)
            _add(stdscr, y + 1, cx, label, attr, tw)
            self.hits.append(Hit(Rect(y + 1, cx, 1, tw), "tab", tab))
            cx += tw + 1

    def _draw_about(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), "✦  Miku UI", self._chrome_reserve())
        inner = max(10, w - 4)
        xx = x + 2
        top = y + 2
        bottom = y + h - 3
        area_h = max(0, bottom - top + 1)
        title = "Miku UI Build System TUI"
        ver = f"ver {TUI_VERSION}"
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
        self._draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _close_page(self) -> None:
        if self.tab == TAB_SCM:
            if self.scm.close():
                return
            self.tab = TAB_BUILD
            return
        if self.tab != TAB_BUILD:
            self.tab = TAB_BUILD
            return
        if not self.build.close():
            self._leave = True

    def _draw_card_chrome(self, stdscr: curses.window, y: int, x: int, w: int) -> None:
        reserve = self._chrome_reserve()
        if w < reserve + 3:
            return
        cx = x + w - 1 - reserve
        attr = curses.color_pair(10) | curses.A_BOLD
        _add(stdscr, y, cx, _MIN_MARK, attr, _MIN_W)
        self.hits.append(Hit(Rect(y, cx, 1, _MIN_W), "minimize"))
        if self._show_close():
            cx += _MIN_W
            _add(stdscr, y, cx, _CLOSE_MARK, attr, _CLOSE_W)
            self.hits.append(Hit(Rect(y, cx, 1, _CLOSE_W), "quit"))

    def _draw_lang_bar(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        width: int,
        voices: bool = False,
    ) -> None:
        left = x
        if voices:
            cx = x
            for code, label in VOICE_CHIPS:
                text = f" {label} "
                w = dw(text)
                if cx + w - x > width:
                    break
                attr = curses.color_pair(3) | curses.A_BOLD if code == self.voice else curses.color_pair(10)
                _add(stdscr, y, cx, text, attr, w)
                self.hits.append(Hit(Rect(y, cx, 1, w), "voice", code))
                cx += w + 1
            left = cx
        chips: list[tuple[str, str, int]] = []
        chip_span = 0
        for code, label in LANG_CHIPS:
            text = f" {label} "
            w = dw(text)
            chips.append((code, text, w))
            chip_span += w + 1
        chip_span = max(0, chip_span - 1)
        cx = x + max(0, width - chip_span)
        if voices and left > x:
            cx = max(cx, left + 1)
        for code, text, w in chips:
            if cx + w - x > width:
                break
            selected = code == self.lang
            attr = curses.color_pair(3) | curses.A_BOLD if selected else curses.color_pair(10)
            _add(stdscr, y, cx, text, attr, w)
            self.hits.append(Hit(Rect(y, cx, 1, w), "lang", code))
            cx += w + 1

    def _draw_log(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        h: int,
        w: int,
        *,
        log: LogBuffer | None = None,
        scroll_attr: str = "log_scroll",
    ) -> None:
        draw_log(self, stdscr, y, x, h, w, log=log, scroll_attr=scroll_attr)

    def _draw_log_scrollbar(self, stdscr: curses.window, y: int, x: int, h: int) -> None:
        draw_log_scrollbar(self, stdscr, y, x, h)

    def _mouse(self) -> None:
        handle_mouse(self)

    def _wheel(self, delta: int) -> None:
        handle_wheel(self, delta)

    def _key(self, ch: int) -> bool:
        cancel_press(self)
        if ch in (3,):
            if self.build.mode == MODE_BUILD:
                self._action("stop_or_back")
                return False
            return True
        if self.tab == TAB_SCM and self.scm.dialog is not None:
            return self.scm.key_dialog(self, ch)
        if self.tab != TAB_BUILD:
            return self._key_other_tab(ch)
        return self.build.key(self, ch)

    def _key_other_tab(self, ch: int) -> bool:
        if ch in (ord("q"), ord("Q")):
            self._action("quit")
            return self._leave
        if self.tab == TAB_SCM and self.scm.key(self, ch):
            return False
        if ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
            self._cycle_tab(-1 if ch == curses.KEY_LEFT else 1)
        return False

    def _cycle_tab(self, step: int) -> None:
        idx = TABS.index(self.tab) if self.tab in TABS else 0
        self.tab = TABS[(idx + step) % len(TABS)]

    def _action(self, action: str, payload: object = None) -> None:
        before = self._prefs_snapshot()
        if self.scm.handle_action(self, action, payload):
            self._persist_if_changed(before)
            return
        if self.build.handle_action(self, action, payload):
            self._persist_if_changed(before)
            return
        if action == "minimize":
            self._detach = True
        elif action == "quit":
            self._close_page()
        elif action == "tab" and isinstance(payload, str) and payload in TABS:
            self.tab = payload
        elif action == "voice" and isinstance(payload, str) and payload in {c for c, _n in VOICE_CHIPS}:
            self.voice = payload
        elif action == "lang" and isinstance(payload, str) and payload in {c for c, _n in LANG_CHIPS}:
            self.lang = payload
        elif action == "log":
            pass
        self._persist_if_changed(before)

    def _prefs_snapshot(self) -> tuple:
        device = self.build.selected.product_name if self.build.selected is not None else None
        return (
            device,
            self.build.variant,
            self.build.jobs,
            self.build.gapps,
            self.build.ccache,
            self.build.keep_going,
            self.lang,
            self.voice,
            self.scm.git_transport,
            self.scm.check_jobs,
        )

    def _persist_if_changed(self, before: tuple) -> None:
        if self._prefs_snapshot() != before:
            self._persist()

    def _persist(self) -> None:
        save_prefs(
            self.top,
            Prefs(
                device=self.build.selected.product_name if self.build.selected is not None else None,
                variant=self.build.variant,
                jobs=self.build.jobs,
                gapps=self.build.gapps,
                ccache=self.build.ccache,
                keep_going=self.build.keep_going,
                lang=self.lang,
                voice=self.voice,
                git_transport=self.scm.git_transport,
                check_jobs=self.scm.check_jobs,
            ),
        )


def run_tui(
    top: Path,
    products: list[Product],
    release: str,
    jobs: int | None,
    gapps: bool | None,
    ccache: bool | None,
    clean: str,
    variant: str | None,
) -> int:
    def make() -> BuildTui:
        return BuildTui(top, products, release, jobs, gapps, ccache, clean, variant)

    try:
        return launch_session(top, make)
    except curses.error as exc:
        print(t(detect_lang(), "tui_failed", exc=exc), file=sys.stderr)
        return 2


def _art_attr(ch: str, row: int, n: int) -> int:
    t = row / max(1, n - 1)
    if ch in ".,'`:;":
        return curses.color_pair(10)
    if t < 0.06 or t > 0.92:
        return curses.color_pair(10)
    if ch in "0OKkXx":
        return curses.color_pair(11) | curses.A_BOLD
    return curses.color_pair(1)
