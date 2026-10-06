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
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from art import SPARKS, about_art, banner_lines, logo_lines
from builder import BuildConfig, BuildSession
from devices import Product, resolve_device
from i18n import LANG_CHIPS, detect_lang, t
from logbuf import Cell, LogBuffer
from pages_scm import ScmPage
from phase import PhaseTracker
from prefs import Prefs, load_prefs, save_prefs
from report import (
    BuildReport,
    bar_widths,
    build_report,
    fmt_delta,
    fmt_duration,
    fmt_mb,
    fmt_size_delta,
)
from session import (
    accept_attach,
    client_gone,
    launch as launch_session,
    notify_client,
    release_tty,
    take_tty,
    try_accept,
)
from sysinfo import HostMonitor, fmt_freq, fmt_pair, read_miku_rom_version
from term import (
    Hit,
    Rect,
    _CLOSE_W,
    _MIN_W,
    _WHEEL_DOWN,
    _WHEEL_UP,
    _add,
    _add_segs,
    _cell_attr,
    _cycle,
    _disable_mouse,
    _enable_mouse,
    _ensure_utf8,
    _fmt_elapsed,
    _glyphs_to_segs,
    _init_colors,
    _panel_geom,
    _rounded_frame,
    _sync_curses_size,
    _wrap_glyphs,
    clip,
    clip_cells,
    dw,
    wrap_cells,
    wrap_words,
)

MODE_CONFIG = "config"
MODE_PICKER = "picker"
MODE_BUILD = "build"
MODE_DONE = "done"
MODE_RESULT = "result"

TUI_VERSION = "1.0.0"

# Visible page; independent of mode so a running build is not interrupted.
TAB_BUILD = "build"
TAB_SCM = "scm"
TAB_ABOUT = "about"
TABS = (TAB_BUILD, TAB_SCM, TAB_ABOUT)

_STAGE_PAIR = {
    "prepare": 37,
    "clean": 38,
    "soong": 39,
    "kati": 40,
    "ninja": 41,
    "package": 42,
}
_STAGE_TIME_KEY = {
    "prepare": "result_time_prepare",
    "clean": "result_time_clean",
    "soong": "result_time_soong",
    "kati": "result_time_kati",
    "ninja": "result_time_ninja",
    "package": "result_time_package",
}
_STAGE_NAME_KEY = {
    "prepare": "result_stage_prepare",
    "clean": "result_stage_clean",
    "soong": "result_stage_soong",
    "kati": "result_stage_kati",
    "ninja": "result_stage_ninja",
    "package": "result_stage_package",
}

CLEAN_NONE = "none"
CLEAN_INSTALL = "installclean"
CLEAN_FULL = "clean"

VARIANTS = ("user", "userdebug", "eng")
VOICE_MOE = "moe"
VOICE_PRO = "pro"
VOICE_CHIPS = (
    (VOICE_MOE, "Moe Mode"),
    (VOICE_PRO, "Pro Mode"),
)

_FOCUS = ("device", "variant", "jobs", "gapps", "ccache", "keep_going", "clean", "build")
_CLOSE_MARK = "[x]"
_MIN_MARK = "[-]"
_SCROLL_TRACK = "▕"
_SCROLL_THUMB = "▐"


def _thumb_geom(content: int, view: int, start: int, track: int) -> tuple[int, int]:
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


def _start_from_thumb(thumb_y: int, content: int, view: int, track: int) -> int:
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
        if variant is not None:
            self.variant = variant
        elif prefs.variant is not None:
            self.variant = prefs.variant
        else:
            self.variant = "userdebug"
        if jobs is not None:
            self.jobs = max(1, jobs)
        elif prefs.jobs is not None:
            self.jobs = prefs.jobs
        else:
            self.jobs = max(1, os.cpu_count() or 4)
        if gapps is not None:
            self.gapps = gapps
        elif prefs.gapps is not None:
            self.gapps = prefs.gapps
        else:
            self.gapps = False
        if ccache is not None:
            self.ccache = ccache
        elif prefs.ccache is not None:
            self.ccache = prefs.ccache
        else:
            self.ccache = shutil.which("ccache") is not None
        self.keep_going = prefs.keep_going if prefs.keep_going is not None else False
        self.clean = clean
        self.selected: Product | None = None
        if prefs.device:
            try:
                self.selected = resolve_device(products, prefs.device)
            except ValueError:
                self.selected = None
        if self.selected is None and len(products) == 1:
            self.selected = products[0]
        self.mode = MODE_CONFIG
        self.tab = TAB_BUILD
        self.focus = 0
        self.picker_index = 0
        if self.selected is not None:
            try:
                self.picker_index = products.index(self.selected)
            except ValueError:
                self.picker_index = 0
        self.picker_off = 0
        self.log = LogBuffer()
        self.phase = PhaseTracker()
        self.log_scroll = 0
        self.report: BuildReport | None = None
        self.result_scroll = 0
        self._result_n = 0
        self.session: BuildSession | None = None
        self._stopping: BuildSession | None = None
        self.hits: list[Hit] = []
        self.status_key = ""
        self.status_args: dict[str, object] = {}
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
        self._scroll_owner = self
        self._scroll_attr = "log_scroll"

    def _t(self, key: str, **kwargs: object) -> str:
        return t(self.lang, key, voice=self.voice, **kwargs)

    def _set_status(self, key: str = "", **kwargs: object) -> None:
        self.status_key = key
        self.status_args = kwargs

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
        session = self.session
        if self.mode == MODE_BUILD and session is not None and not session.running:
            code = session.returncode
            session.close()
            if session.stopped:
                self._set_status("stopped")
                self.mode = MODE_DONE
            elif code == 0:
                self._set_status("build_ok")
                self._open_result(session, True)
            else:
                self._set_status("build_fail", code=code)
                self._open_result(session, False)
            self.host.tick(force_disk=True)
        if self._stopping is not None and not self._stopping.running:
            self._stopping = None
        self.scm.reap()

    def _draw(self, stdscr: curses.window) -> None:
        if _sync_curses_size(stdscr):
            stdscr.clear()
        else:
            stdscr.erase()
        self.hits = []
        self._scroll_geom = Rect(0, 0, 0, 0)
        self._log_geom = Rect(0, 0, 0, 0)
        self._scroll_owner = self
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
        elif self.mode == MODE_PICKER:
            self._draw_picker(stdscr, cy, cx, ch, cw)
        elif self.mode == MODE_RESULT:
            self._draw_result(stdscr, cy, cx, ch, cw)
        elif self.mode in (MODE_BUILD, MODE_DONE):
            self._draw_build(stdscr, cy, cx, ch, cw)
        else:
            self._draw_config(stdscr, cy, cx, ch, cw)
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
        row = self._option_block(
            stdscr,
            row,
            xx,
            inner,
            limit,
            self._t("variant"),
            tuple((name, name) for name in VARIANTS),
            self.variant,
            "variant",
        )
        if row < limit:
            _add(stdscr, row, xx, self._t("jobs"), curses.color_pair(15) | curses.A_DIM, inner)
            row += 1
        if row < limit:
            self._jobs_row(stdscr, row, xx, inner, self.jobs, "jobs", "jobs")
            row += 2
        row = self._option_block(
            stdscr, row, xx, inner, limit, self._t("gapps"), self._yes_no(), self.gapps, "gapps"
        )
        row = self._option_block(
            stdscr, row, xx, inner, limit, self._t("ccache"), self._yes_no(), self.ccache, "ccache"
        )
        row = self._option_block(
            stdscr,
            row,
            xx,
            inner,
            limit,
            self._t("keep_going"),
            self._yes_no(),
            self.keep_going,
            "keep_going",
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
        self._draw_lang_bar(stdscr, y + h - 2, xx, inner, voices=True)

    def _has_focus(self, name: str) -> bool:
        if self.tab == TAB_SCM:
            return self.scm.has_focus(name)
        return 0 <= self.focus < len(_FOCUS) and _FOCUS[self.focus] == name

    def _set_focus(self, name: str) -> None:
        if self.tab == TAB_SCM:
            self.scm.set_focus(name)
            return
        try:
            self.focus = _FOCUS.index(name)
        except ValueError:
            return

    def _yes_no(self) -> tuple[tuple[bool, str], tuple[bool, str]]:
        return ((True, self._t("yes")), (False, self._t("no")))

    def _phase_title(self) -> str:
        key, args = self.phase.snapshot()
        return self._t(key, **args)

    def _jobs_row(
        self,
        stdscr: curses.window,
        y: int,
        x: int,
        width: int,
        value: int,
        action: str,
        focus: str,
        focused: bool | None = None,
    ) -> None:
        if focused is None:
            focused = self._has_focus(focus)
        minus_a = curses.color_pair(3) if focused else curses.color_pair(12)
        plus_a = minus_a
        _add(stdscr, y, x, "  −  ", minus_a | curses.A_BOLD, 5)
        self.hits.append(Hit(Rect(y, x, 1, 5), action, -1))
        num = f"{value:^5d}"
        _add(stdscr, y, x + 6, num, curses.color_pair(11) | curses.A_BOLD, 5)
        self.hits.append(Hit(Rect(y, x + 6, 1, 5), "focus", focus))
        _add(stdscr, y, x + 12, "  +  ", plus_a | curses.A_BOLD, 5)
        self.hits.append(Hit(Rect(y, x + 12, 1, 5), action, 1))

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
        title_attr: int | None = None,
    ) -> int:
        """Dim title plus a chip row. Returns the next row after optional gap."""
        if row < limit:
            attr = title_attr if title_attr is not None else curses.color_pair(15) | curses.A_DIM
            _add(stdscr, row, x, title, attr, width)
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

    def _show_close(self) -> bool:
        if self.tab == TAB_BUILD and self.mode == MODE_BUILD:
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
        if self.mode == MODE_CONFIG:
            self._leave = True
        elif self.mode == MODE_PICKER:
            self.mode = MODE_CONFIG
        elif self.mode == MODE_RESULT:
            self.mode = MODE_DONE
        elif self.mode == MODE_DONE:
            self.mode = MODE_CONFIG

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

    def _picker_rows(self) -> list[tuple[str, int | str]]:
        rows: list[tuple[str, int | str]] = [("head", "picker_local")]
        for i, product in enumerate(self.products):
            if not product.remote:
                rows.append(("item", i))
        rows.append(("head", "picker_remote"))
        for i, product in enumerate(self.products):
            if product.remote:
                rows.append(("item", i))
        return rows

    def _draw_picker(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), f"✦  {self._t('picker_title')}")
        inner = max(10, w - 4)
        xx = x + 2
        list_y = y + 3
        list_h = max(1, (y + h - 3) - list_y)
        rows = self._picker_rows()
        sel_row = 0
        for i, (kind, payload) in enumerate(rows):
            if kind == "item" and payload == self.picker_index:
                sel_row = i
                break
        if sel_row < self.picker_off:
            self.picker_off = sel_row
        if sel_row >= self.picker_off + list_h:
            self.picker_off = sel_row - list_h + 1
        max_off = max(0, len(rows) - list_h)
        if self.picker_off > max_off:
            self.picker_off = max_off
        for i in range(list_h):
            idx = self.picker_off + i
            if idx >= len(rows):
                break
            kind, payload = rows[idx]
            if kind == "head":
                _add(
                    stdscr,
                    list_y + i,
                    xx,
                    self._t(str(payload)),
                    curses.color_pair(15) | curses.A_DIM,
                    inner,
                )
                continue
            product_idx = int(payload)
            product = self.products[product_idx]
            selected = product_idx == self.picker_index
            attr = curses.color_pair(3) | curses.A_BOLD if selected else curses.color_pair(2)
            prefix = "▸ " if selected else "  "
            _add(stdscr, list_y + i, xx, clip(prefix + product.label, inner), attr, inner)
            self.hits.append(Hit(Rect(list_y + i, xx, 1, inner), "pick", product_idx))
        self._draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _draw_build(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        session = self.session
        combo = session.config.lunch_combo if session else self._combo_preview()
        elapsed = ""
        if session is not None:
            end = session.finished_at or time.time()
            elapsed = "  " + _fmt_elapsed(end - session.started_at)
        if self.mode == MODE_BUILD:
            title = self._phase_title()
        else:
            title = self._t(self.status_key, **self.status_args) if self.status_key else self._t("done")
        room = max(0, w - 4 - dw(elapsed))
        title = clip(title, room)
        _rounded_frame(
            stdscr, y, x, h, w, curses.color_pair(12), f"✦  {title}{elapsed}", self._chrome_reserve()
        )
        inner = max(10, w - 4)
        xx = x + 2
        _add(stdscr, y + 2, xx, combo, curses.color_pair(10), inner)
        log_top = y + 4
        log_h = max(3, h - 10)
        log_w = inner
        self._log_geom = Rect(log_top, xx, log_h, log_w)
        self._scroll_attr = "log_scroll"
        self.log.set_size(max(2, log_h), max(20, log_w))
        self._draw_log(stdscr, log_top, xx, log_h, log_w)
        self._draw_log_scrollbar(stdscr, log_top, x + w - 2, log_h)
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

    def _draw_result(self, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        report = self.report
        if report is None:
            self._draw_build(stdscr, y, x, h, w)
            return
        session = self.session
        elapsed = ""
        if session is not None:
            end = session.finished_at or time.time()
            elapsed = "  " + _fmt_elapsed(end - session.started_at)
        frame = clip(self._t("result_frame"), max(0, w - 4 - dw(elapsed)))
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), f"✦  {frame}{elapsed}")
        inner = max(10, w - 4)
        xx = x + 2
        body_top = y + 2
        body_h = max(3, h - 8)
        self._log_geom = Rect(body_top, xx, body_h, inner)
        rows = self._result_rows(report, inner)
        self._result_n = len(rows)
        max_off = max(0, len(rows) - body_h)
        self.result_scroll = min(max(0, self.result_scroll), max_off)
        view = rows[self.result_scroll : self.result_scroll + body_h]
        for i, segs in enumerate(view):
            _add_segs(stdscr, body_top + i, xx, segs, inner)
        btn = self._t("back")
        btn_w = min(inner, max(16, dw(btn) + 10))
        btn_x = x + max(0, (w - btn_w) // 2)
        btn_y = y + h - 5
        self._fill_btn(
            stdscr, btn_y, btn_x, 3, btn_w, btn, curses.color_pair(9) | curses.A_BOLD, "stop_or_back"
        )
        self._draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _result_rows(self, report: BuildReport, width: int) -> list[list[tuple[str, int]]]:
        label_a = curses.color_pair(15)
        value_a = curses.color_pair(2) | curses.A_BOLD
        title_a = curses.color_pair(11) | curses.A_BOLD
        dim_a = curses.color_pair(10)
        rows: list[list[tuple[str, int]]] = []

        def blank() -> None:
            rows.append([])

        def title(text: str) -> None:
            rows.append([(text, title_a)])

        def kv(label: str, value: str, vattr: int = value_a) -> None:
            rows.extend(_kv_rows(label, value, label_a, vattr, width))

        headline = self._t("result_headline", version=report.version) + self._t(
            "result_headline_ok" if report.ok else "result_headline_fail"
        )
        for line in wrap_words(headline, width) or [headline]:
            rows.append([(line, title_a)])
        blank()
        targets = str(report.targets) if report.targets is not None else self._t("result_unknown")
        kv(self._t("result_targets"), targets)
        kv(self._t("result_targets_delta"), *_delta_pair(report.targets_delta, self._t("result_first"), False))
        blank()
        title(self._t("result_timing"))
        for timing in report.timings:
            pair = _STAGE_PAIR.get(timing.key, 2)
            key = _STAGE_TIME_KEY.get(timing.key)
            if key is None:
                continue
            label_c = curses.color_pair(pair)
            rows.extend(_kv_rows(self._t(key), fmt_duration(timing.seconds), label_c, value_a, width))
        rows.extend(
            _kv_rows(self._t("result_time_total"), fmt_duration(report.total), title_a, value_a, width)
        )
        blank()
        bar_w = max(8, width)
        parts = bar_widths([t.seconds for t in report.timings], bar_w)
        bar: list[tuple[str, int]] = []
        filled = 0
        for timing, n in zip(report.timings, parts):
            if n <= 0:
                continue
            pair = _STAGE_PAIR.get(timing.key, 44)
            bar.append(("█" * n, curses.color_pair(pair) | curses.A_BOLD))
            filled += n
        if filled < bar_w:
            bar.append(("░" * (bar_w - filled), curses.color_pair(44)))
        if not bar:
            bar.append(("░" * bar_w, curses.color_pair(44)))
        rows.append(bar)
        cap = self._t("result_bar_caption")
        pad = max(0, (width - dw(cap)) // 2)
        rows.append([(" " * pad + cap, dim_a)])
        if report.ok:
            blank()
            title(self._t("result_artifacts"))
            kv(self._t("result_device_code"), report.device or self._t("result_unknown"))
            kv(self._t("result_device_name"), report.model or self._t("result_unknown"))
            if report.gapps:
                kv(self._t("result_has_gapps"), self._t("yes"), curses.color_pair(4) | curses.A_BOLD)
            else:
                kv(self._t("result_has_gapps"), self._t("no"), curses.color_pair(5) | curses.A_BOLD)
            path = report.artifact_rel or self._t("result_unknown")
            kv(self._t("result_artifact_path"), path)
            size = fmt_mb(report.artifact_size) if report.artifact_size is not None else self._t("result_unknown")
            kv(self._t("result_artifact_size"), size)
            kv(self._t("result_size_delta"), *_delta_pair(report.size_delta, self._t("result_first"), True))
            kv(self._t("result_sha256"), report.sha256 or self._t("result_unknown"))
            if report.signed is True:
                kv(self._t("result_signed"), self._t("yes"), curses.color_pair(4) | curses.A_BOLD)
                if report.key_rel:
                    kv(self._t("result_key_path"), report.key_rel)
            elif report.signed is False:
                kv(self._t("result_signed"), self._t("no"), curses.color_pair(5) | curses.A_BOLD)
        else:
            blank()
            title(self._t("result_fail_analysis"))
            stage_key = _STAGE_NAME_KEY.get(report.fail_stage or "", "result_stage_prepare")
            stage_pair = _STAGE_PAIR.get(report.fail_stage or "prepare", 2)
            kv(self._t("result_fail_stage"), self._t(stage_key), curses.color_pair(stage_pair) | curses.A_BOLD)
            if not report.failures:
                kv(self._t("result_fail_reason"), self._t("result_unknown"))
            for item in report.failures:
                if report.fail_stage == "ninja" and item.module:
                    kv(self._t("result_fail_module"), item.module)
                rows.append([(self._t("result_fail_reason"), label_a)])
                for glyphs in item.rows:
                    wrapped = _wrap_glyphs(glyphs, max(1, width - 2))
                    for piece in wrapped:
                        rows.append([("  ", dim_a), *_glyphs_to_segs(piece)])
                blank()
        return rows

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
        buf = log if log is not None else self.log
        key = (id(buf), buf.generation, w)
        if self._wrap_key != key:
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
            self._wrapped = wrapped
            self._wrap_key = key
        wrapped = self._wrapped
        max_off = max(0, len(wrapped) - h)
        owner = getattr(self, "_scroll_owner", self)
        scroll = getattr(owner, scroll_attr)
        if scroll <= 0:
            start = max(0, len(wrapped) - h)
        else:
            scroll = min(scroll, max_off)
            setattr(owner, scroll_attr, scroll)
            start = max(0, len(wrapped) - h - scroll)
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

    def _draw_log_scrollbar(self, stdscr: curses.window, y: int, x: int, h: int) -> None:
        content = len(self._wrapped)
        if h <= 0 or content <= h:
            return
        # Padding column plus the frame so the thumb is easier to grab.
        self._scroll_geom = Rect(y, x, h, 2)
        thumb_y, thumb_h = _thumb_geom(content, h, self._view_start, h)
        track_a = curses.color_pair(10)
        thumb_a = curses.color_pair(11) | curses.A_BOLD
        if self._scroll_drag is not None:
            thumb_a = curses.color_pair(15) | curses.A_BOLD
        for i in range(h):
            glyph = _SCROLL_THUMB if thumb_y <= i < thumb_y + thumb_h else _SCROLL_TRACK
            attr = thumb_a if glyph == _SCROLL_THUMB else track_a
            _add(stdscr, y + i, x, glyph, attr, 1)

    def _begin_scroll_drag(self, my: int) -> None:
        self._btn_down = False
        self._btn_hit = None
        self._selecting = False
        self._sel_a = None
        self._sel_b = None
        geom = self._scroll_geom
        content = len(self._wrapped)
        if geom.h <= 0 or content <= geom.h:
            return
        thumb_y, thumb_h = _thumb_geom(content, geom.h, self._view_start, geom.h)
        rel = my - geom.y
        if thumb_y <= rel < thumb_y + thumb_h:
            self._scroll_drag = rel - thumb_y
        else:
            self._scroll_drag = thumb_h // 2
        self._apply_scroll_drag(my)

    def _apply_scroll_drag(self, my: int) -> None:
        geom = self._scroll_geom
        content = len(self._wrapped)
        if geom.h <= 0 or content <= geom.h or self._scroll_drag is None:
            return
        thumb_y = my - geom.y - self._scroll_drag
        start = _start_from_thumb(thumb_y, content, geom.h, geom.h)
        # log_scroll is counted from the tail so 0 keeps following new output.
        owner = getattr(self, "_scroll_owner", self)
        attr = getattr(self, "_scroll_attr", "log_scroll")
        setattr(owner, attr, max(0, content - geom.h) - start)

    def _hit_at(self, y: int, x: int) -> Hit | None:
        for hit in reversed(self.hits):
            if hit.rect.contains(y, x):
                return hit
        return None

    def _cancel_press(self) -> None:
        self._btn_down = False
        self._btn_hit = None
        self._scroll_drag = None

    def _fire_click(self, action: str, payload: object) -> None:
        now = time.time()
        last_t, last_a, last_p = self._click_guard
        if now - last_t < 0.2 and last_a == action and last_p == payload:
            return
        self._click_guard = (now, action, payload)
        self._sel_a = None
        self._sel_b = None
        self._action(action, payload)

    def _mouse(self) -> None:
        try:
            _id, mx, my, _z, bstate = curses.getmouse()
        except curses.error:
            return
        if bstate & _WHEEL_UP:
            self._cancel_press()
            self._wheel(-3)
            return
        if bstate & _WHEEL_DOWN:
            self._cancel_press()
            self._wheel(3)
            return
        pressed = bool(bstate & curses.BUTTON1_PRESSED)
        released = bool(bstate & curses.BUTTON1_RELEASED)
        clicked = bool(bstate & curses.BUTTON1_CLICKED)
        report = bool(bstate & getattr(curses, "REPORT_MOUSE_POSITION", 0))
        if self._scroll_drag is not None:
            if pressed or report:
                self._apply_scroll_drag(my)
                return
            if released:
                self._apply_scroll_drag(my)
                self._cancel_press()
                return
            return
        if pressed and self._scroll_geom.contains(my, mx):
            self._begin_scroll_drag(my)
            return
        if clicked and not self._btn_down and self._scroll_geom.contains(my, mx):
            self._begin_scroll_drag(my)
            self._cancel_press()
            return
        in_log = self._log_geom.contains(my, mx) and (
            (self.tab == TAB_BUILD and self.mode in (MODE_BUILD, MODE_DONE))
            or (self.tab == TAB_SCM and self.scm.showing_log())
        )
        if self._selecting and (pressed or report):
            pos = self._log_pos(my, mx, clamp=True)
            if pos is not None:
                self._sel_b = pos
            return
        if pressed and in_log and not self._btn_down:
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
            self._cancel_press()
            return
        if in_log:
            if released:
                self._cancel_press()
            return
        # mouseinterval(0) reports press and release separately so log drag
        # works; UI hits must wait for release over the same control.
        if pressed:
            if not self._btn_down:
                hit = self._hit_at(my, mx)
                self._btn_down = True
                self._btn_hit = (hit.action, hit.payload) if hit else None
            return
        if report:
            return
        if released:
            armed = self._btn_down
            press_hit = self._btn_hit
            self._cancel_press()
            hit = self._hit_at(my, mx)
            if hit is None:
                return
            if armed and press_hit != (hit.action, hit.payload):
                return
            self._fire_click(hit.action, hit.payload)
            return
        if clicked:
            if self._btn_down:
                return
            hit = self._hit_at(my, mx)
            if hit is not None:
                self._fire_click(hit.action, hit.payload)

    def _wheel(self, delta: int) -> None:
        if self.tab == TAB_SCM:
            if self.scm.dialog is not None:
                return
            self.scm.wheel(self, delta)
            return
        if self.tab != TAB_BUILD:
            return
        if self.mode == MODE_PICKER:
            self.picker_index = min(max(0, self.picker_index + delta), max(0, len(self.products) - 1))
            return
        if self.mode == MODE_RESULT:
            max_off = max(0, self._result_n - max(1, self._log_geom.h))
            self.result_scroll = min(max(0, self.result_scroll + delta), max_off)
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
        self._cancel_press()
        if ch in (3,):
            if self.mode == MODE_BUILD:
                self._action("stop_or_back")
                return False
            return True
        if self.tab == TAB_SCM and self.scm.dialog is not None:
            return self.scm.key_dialog(self, ch)
        if self.tab != TAB_BUILD:
            return self._key_other_tab(ch)
        if self.mode == MODE_PICKER:
            return self._key_picker(ch)
        if self.mode == MODE_RESULT:
            return self._key_result(ch)
        if self.mode in (MODE_BUILD, MODE_DONE):
            return self._key_build(ch)
        return self._key_config(ch)

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

    def _key_config(self, ch: int) -> bool:
        if ch in (ord("q"), ord("Q")):
            self._action("quit")
            return self._leave
        if ch in (9, curses.KEY_DOWN):
            self.focus = (self.focus + 1) % len(_FOCUS)
            return False
        if ch in (getattr(curses, "KEY_BTAB", 353), curses.KEY_UP):
            self.focus = (self.focus - 1) % len(_FOCUS)
            return False
        before = self._prefs_snapshot()
        name = _FOCUS[self.focus]
        if name == "device" and ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._open_picker()
        elif name == "variant":
            self.variant = _cycle(VARIANTS, self.variant, ch)
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
        elif name == "keep_going":
            self.keep_going = _cycle((True, False), self.keep_going, ch)
        elif name == "clean":
            self.clean = _cycle((CLEAN_NONE, CLEAN_INSTALL, CLEAN_FULL), self.clean, ch)
        elif name == "build" and ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._start_build()
        self._persist_if_changed(before)
        return False

    def _key_picker(self, ch: int) -> bool:
        if ch in (ord("q"), ord("Q")):
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
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._action("stop_or_back")
        elif ch in (ord("q"), ord("Q")) and self.mode == MODE_DONE:
            self._action("stop_or_back")
        return False

    def _key_result(self, ch: int) -> bool:
        if ch in (curses.KEY_UP, _WHEEL_UP):
            self.result_scroll = max(0, self.result_scroll - 1)
        elif ch in (curses.KEY_DOWN,):
            self.result_scroll += 1
        elif ch in (curses.KEY_PPAGE,):
            self.result_scroll = max(0, self.result_scroll - 10)
        elif ch in (curses.KEY_NPAGE,):
            self.result_scroll += 10
        elif ch in (curses.KEY_HOME,):
            self.result_scroll = 0
        elif ch in (curses.KEY_END,):
            self.result_scroll = max(0, self._result_n)
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" "), ord("q"), ord("Q")):
            self._action("stop_or_back")
        return False

    def _action(self, action: str, payload: object = None) -> None:
        before = self._prefs_snapshot()
        if self.scm.handle_action(self, action, payload):
            self._persist_if_changed(before)
            return
        if action == "device":
            self._set_focus("device")
            self._open_picker()
        elif action == "variant":
            self._set_focus("variant")
            if isinstance(payload, str) and payload in VARIANTS:
                self.variant = payload
        elif action == "jobs":
            self._set_focus("jobs")
            delta = int(payload or 0)
            self.jobs = min(256, max(1, self.jobs + delta))
        elif action == "focus" and isinstance(payload, str):
            self._set_focus(payload)
        elif action == "gapps":
            self._set_focus("gapps")
            if isinstance(payload, bool):
                self.gapps = payload
        elif action == "ccache":
            self._set_focus("ccache")
            if isinstance(payload, bool):
                self.ccache = payload
        elif action == "keep_going":
            self._set_focus("keep_going")
            if isinstance(payload, bool):
                self.keep_going = payload
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
            elif self.mode == MODE_RESULT:
                self.mode = MODE_DONE
            elif self.mode == MODE_DONE:
                self.mode = MODE_CONFIG
        elif action == "minimize":
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
        device = self.selected.product_name if self.selected is not None else None
        return (
            device,
            self.variant,
            self.jobs,
            self.gapps,
            self.ccache,
            self.keep_going,
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
                device=self.selected.product_name if self.selected is not None else None,
                variant=self.variant,
                jobs=self.jobs,
                gapps=self.gapps,
                ccache=self.ccache,
                keep_going=self.keep_going,
                lang=self.lang,
                voice=self.voice,
                git_transport=self.scm.git_transport,
                check_jobs=self.scm.check_jobs,
            ),
        )

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
        phase = PhaseTracker()
        self.log = log
        self.phase = phase
        self.log_scroll = 0
        self.report = None
        self.result_scroll = 0
        self._wrapped = []
        self._wrap_key = None
        self._sel_a = None
        self._sel_b = None
        self._selecting = False
        self._scroll_drag = None
        self._set_status()
        config = BuildConfig(
            product=self.selected,
            jobs=self.jobs,
            gapps=self.gapps,
            ccache=self.ccache,
            keep_going=self.keep_going,
            clean=self.clean,
            release=self.release,
            variant=self.variant,
        )

        def on_data(text: str, buf: LogBuffer = log, tracker: PhaseTracker = phase) -> None:
            buf.feed(text)
            tracker.feed(text)

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
        if self.keep_going:
            extras.append("-k 0")
        if self.clean != CLEAN_NONE:
            extras.append(self.clean)
        extra = ("  " + " ".join(extras)) if extras else ""
        return f"{name}-{self.release}-{self.variant}  -j{self.jobs}{extra}"

    def _open_result(self, session: BuildSession, ok: bool) -> None:
        phase_key, entered, ninja_total, ninja_done, package_path = self.phase.snapshot_report()
        started = session.started_at
        finished = session.finished_at or time.time()
        report = build_report(
            top=self.top,
            product=session.config.product,
            version=read_miku_rom_version(self.top),
            ok=ok,
            started=started,
            finished=finished,
            entered=entered,
            phase_key=phase_key,
            ninja_total=ninja_total,
            ninja_done=ninja_done,
            package_hint=package_path,
            log_rows=self.log.glyph_rows(),
            gapps=session.config.gapps,
        )
        self.report = report
        self.result_scroll = 0
        self._scroll_drag = None
        self.mode = MODE_RESULT


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


def _kv_rows(
    label: str, value: str, label_attr: int, value_attr: int, width: int
) -> list[list[tuple[str, int]]]:
    if dw(label) + dw(value) <= width:
        return [[(label, label_attr), (value, value_attr)]]
    rows: list[list[tuple[str, int]]] = [[(label, label_attr)]]
    for line in wrap_words(value, max(1, width - 2)):
        rows.append([("  " + line, value_attr)])
    return rows


def _delta_pair(delta: int | None, first: str, size: bool) -> tuple[str, int]:
    if delta is None:
        return first, curses.color_pair(6) | curses.A_BOLD
    text = fmt_size_delta(delta) if size else fmt_delta(delta)
    if delta > 0:
        return text, curses.color_pair(5) | curses.A_BOLD
    if delta < 0:
        return text, curses.color_pair(4) | curses.A_BOLD
    return text, curses.color_pair(10)


def _art_attr(ch: str, row: int, n: int) -> int:
    t = row / max(1, n - 1)
    if ch in ".,'`:;":
        return curses.color_pair(10)
    if t < 0.06 or t > 0.92:
        return curses.color_pair(10)
    if ch in "0OKkXx":
        return curses.color_pair(11) | curses.A_BOLD
    return curses.color_pair(1)
