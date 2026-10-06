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
import shutil
import threading
import time
from pathlib import Path

from core.builder import BuildConfig, BuildSession
from core.devices import Product, resolve_device
from ui.logbuf import LogBuffer
from pages.result import result_rows
from core.phase import PhaseTracker
from core.prefs import Prefs
from core.report import BuildReport, build_report
from core.sysinfo import read_miku_rom_version
from ui.term import (
    Hit,
    Rect,
    _WHEEL_UP,
    _add,
    _add_segs,
    _cycle,
    _fmt_elapsed,
    _panel_geom,
    _rounded_frame,
    clip,
    dw,
)
from ui.ctx import Page
from ui.widgets import box_btn, fill_btn, jobs_row, option_block, yes_no

MODE_CONFIG = "config"
MODE_PICKER = "picker"
MODE_BUILD = "build"
MODE_DONE = "done"
MODE_RESULT = "result"

CLEAN_NONE = "none"
CLEAN_INSTALL = "installclean"
CLEAN_FULL = "clean"

VARIANTS = ("user", "userdebug", "eng")
_FOCUS = ("device", "variant", "jobs", "gapps", "ccache", "keep_going", "clean", "build")


class BuildPage(Page):
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
        prefs: Prefs,
    ) -> None:
        self.top = top
        self.products = products
        self.release = release
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
        self.status_key = ""
        self.status_args: dict[str, object] = {}

    def has_focus(self, name: str) -> bool:
        return 0 <= self.focus < len(_FOCUS) and _FOCUS[self.focus] == name

    def set_focus(self, name: str) -> None:
        try:
            self.focus = _FOCUS.index(name)
        except ValueError:
            return

    def hide_close(self) -> bool:
        return self.mode == MODE_BUILD

    def showing_log(self) -> bool:
        return self.mode in (MODE_BUILD, MODE_DONE)

    def consume_interrupt(self) -> bool:
        if self.mode == MODE_BUILD:
            self._stop_build()
            return True
        return False

    def set_status(self, key: str = "", **kwargs: object) -> None:
        self.status_key = key
        self.status_args = kwargs

    def reap(self, ctx) -> None:
        session = self.session
        if self.mode == MODE_BUILD and session is not None and not session.running:
            code = session.returncode
            session.close()
            if session.stopped:
                self.set_status("stopped")
                self.mode = MODE_DONE
            elif code == 0:
                self.set_status("build_ok")
                self._open_result(ctx, session, True)
            else:
                self.set_status("build_fail", code=code)
                self._open_result(ctx, session, False)
            ctx.tick_host(force_disk=True)
        if self._stopping is not None and not self._stopping.running:
            self._stopping = None

    def close(self) -> bool:
        if self.mode == MODE_PICKER:
            self.mode = MODE_CONFIG
            return True
        if self.mode == MODE_RESULT:
            self.mode = MODE_DONE
            return True
        if self.mode == MODE_DONE:
            self.mode = MODE_CONFIG
            return True
        return False

    def draw(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        if self.mode == MODE_PICKER:
            self._draw_picker(ctx, stdscr, y, x, h, w)
        elif self.mode == MODE_RESULT:
            self._draw_result(ctx, stdscr, y, x, h, w)
        elif self.mode in (MODE_BUILD, MODE_DONE):
            self._draw_build(ctx, stdscr, y, x, h, w)
        else:
            self._draw_config(ctx, stdscr, y, x, h, w)

    def _draw_config(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), "✦  Cinderella")
        inner = max(10, w - 4)
        xx = x + 2
        row = y + 2
        limit = y + h - 6
        hits = ctx.hits
        _add(stdscr, row, xx, self.combo_preview(), curses.color_pair(2) | curses.A_DIM, inner)
        row += 2
        if row < limit:
            _add(stdscr, row, xx, ctx.t("device"), curses.color_pair(15) | curses.A_DIM, inner)
            row += 1
        if row < limit:
            device_label = self.selected.label if self.selected else ctx.t("pick_device")
            box_btn(
                stdscr,
                hits,
                row,
                xx,
                inner,
                f"{device_label}   ▾",
                "device",
                None,
                self.has_focus("device"),
            )
            row += 2
        row = option_block(
            stdscr,
            hits,
            row,
            xx,
            inner,
            limit,
            ctx.t("variant"),
            tuple((name, name) for name in VARIANTS),
            self.variant,
            "variant",
        )
        if row < limit:
            _add(stdscr, row, xx, ctx.t("jobs"), curses.color_pair(15) | curses.A_DIM, inner)
            row += 1
        if row < limit:
            jobs_row(
                stdscr,
                hits,
                row,
                xx,
                inner,
                self.jobs,
                "jobs",
                "jobs",
                focused=self.has_focus("jobs"),
            )
            row += 2
        yn = yes_no(ctx.t)
        row = option_block(stdscr, hits, row, xx, inner, limit, ctx.t("gapps"), yn, self.gapps, "gapps")
        row = option_block(stdscr, hits, row, xx, inner, limit, ctx.t("ccache"), yn, self.ccache, "ccache")
        row = option_block(
            stdscr,
            hits,
            row,
            xx,
            inner,
            limit,
            ctx.t("keep_going"),
            yn,
            self.keep_going,
            "keep_going",
        )
        option_block(
            stdscr,
            hits,
            row,
            xx,
            inner,
            limit,
            ctx.t("clean"),
            (
                (CLEAN_NONE, ctx.t("clean_none")),
                (CLEAN_INSTALL, ctx.t("clean_install")),
                (CLEAN_FULL, ctx.t("clean_full")),
            ),
            self.clean,
            "clean",
            gap=0,
        )
        if self.status_key:
            _add(
                stdscr,
                y + h - 6,
                xx,
                ctx.t(self.status_key, **self.status_args),
                curses.color_pair(6),
                inner,
            )
        ready = self.selected is not None
        build_label = ctx.t("build_now")
        btn_h = 3
        btn_w = min(inner, max(22, dw(build_label) + 10))
        btn_x = x + max(0, (w - btn_w) // 2)
        btn_y = y + h - 5
        attr = curses.color_pair(3) | curses.A_BOLD
        if not ready:
            attr = curses.color_pair(10)
        elif self.has_focus("build"):
            attr = curses.color_pair(3) | curses.A_BOLD
        fill_btn(stdscr, hits, btn_y, btn_x, btn_h, btn_w, build_label, attr, "build")
        ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner, voices=True)

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

    def _draw_picker(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), f"✦  {ctx.t('picker_title')}")
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
                    ctx.t(str(payload)),
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
            ctx.hits.append(Hit(Rect(list_y + i, xx, 1, inner), "pick", product_idx))
        ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _draw_build(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        session = self.session
        combo = session.config.lunch_combo if session else self.combo_preview()
        elapsed = ""
        if session is not None:
            end = session.finished_at or time.time()
            elapsed = "  " + _fmt_elapsed(end - session.started_at)
        if self.mode == MODE_BUILD:
            title = self._phase_title(ctx)
        else:
            title = ctx.t(self.status_key, **self.status_args) if self.status_key else ctx.t("done")
        room = max(0, w - 4 - dw(elapsed))
        title = clip(title, room)
        _rounded_frame(
            stdscr, y, x, h, w, curses.color_pair(12), f"✦  {title}{elapsed}", ctx.chrome_reserve()
        )
        inner = max(10, w - 4)
        xx = x + 2
        _add(stdscr, y + 2, xx, combo, curses.color_pair(10), inner)
        log_top = y + 4
        log_h = max(3, h - 10)
        log_w = inner
        ctx.log_geom = Rect(log_top, xx, log_h, log_w)
        ctx.set_scroll(self)
        self.log.set_size(max(2, log_h), max(20, log_w))
        ctx.draw_log(stdscr, log_top, xx, log_h, log_w, log=self.log, scroll_attr="log_scroll")
        ctx.draw_log_scrollbar(stdscr, log_top, x + w - 2, log_h)
        if session is not None:
            session.set_winsize(max(2, log_h), max(20, log_w))
        btn = ctx.t("stop") if self.mode == MODE_BUILD else ctx.t("back")
        btn_attr = curses.color_pair(8) | curses.A_BOLD
        if self.mode == MODE_DONE:
            btn_attr = curses.color_pair(9) | curses.A_BOLD
        btn_w = min(inner, max(16, dw(btn) + 10))
        btn_x = x + max(0, (w - btn_w) // 2)
        btn_y = y + h - 5
        fill_btn(stdscr, ctx.hits, btn_y, btn_x, 3, btn_w, btn, btn_attr, "stop_or_back")
        ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _draw_result(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        report = self.report
        if report is None:
            self._draw_build(ctx, stdscr, y, x, h, w)
            return
        session = self.session
        elapsed = ""
        if session is not None:
            end = session.finished_at or time.time()
            elapsed = "  " + _fmt_elapsed(end - session.started_at)
        frame = clip(ctx.t("result_frame"), max(0, w - 4 - dw(elapsed)))
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), f"✦  {frame}{elapsed}")
        inner = max(10, w - 4)
        xx = x + 2
        body_top = y + 2
        body_h = max(3, h - 8)
        ctx.log_geom = Rect(body_top, xx, body_h, inner)
        rows = result_rows(report, inner, ctx.t)
        self._result_n = len(rows)
        max_off = max(0, len(rows) - body_h)
        self.result_scroll = min(max(0, self.result_scroll), max_off)
        view = rows[self.result_scroll : self.result_scroll + body_h]
        for i, segs in enumerate(view):
            _add_segs(stdscr, body_top + i, xx, segs, inner)
        btn = ctx.t("back")
        btn_w = min(inner, max(16, dw(btn) + 10))
        btn_x = x + max(0, (w - btn_w) // 2)
        btn_y = y + h - 5
        fill_btn(
            stdscr, ctx.hits, btn_y, btn_x, 3, btn_w, btn, curses.color_pair(9) | curses.A_BOLD, "stop_or_back"
        )
        ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _phase_title(self, ctx) -> str:
        key, args = self.phase.snapshot()
        return ctx.t(key, **args)

    def combo_preview(self) -> str:
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

    def wheel(self, ctx, delta: int) -> None:
        if self.mode == MODE_PICKER:
            self.picker_index = min(max(0, self.picker_index + delta), max(0, len(self.products) - 1))
            return
        if self.mode == MODE_RESULT:
            max_off = max(0, self._result_n - max(1, ctx.log_geom.h))
            self.result_scroll = min(max(0, self.result_scroll + delta), max_off)
            return
        if self.mode in (MODE_BUILD, MODE_DONE):
            max_off = max(0, len(ctx.wrapped) - max(1, ctx.log_geom.h))
            self.log_scroll = min(max(0, self.log_scroll - delta), max_off)

    def key(self, ctx, ch: int) -> bool:
        if self.mode == MODE_PICKER:
            return self._key_picker(ctx, ch)
        if self.mode == MODE_RESULT:
            return self._key_result(ctx, ch)
        if self.mode in (MODE_BUILD, MODE_DONE):
            return self._key_build(ctx, ch)
        return self._key_config(ctx, ch)

    def _key_config(self, ctx, ch: int) -> bool:
        if ch in (ord("q"), ord("Q")):
            ctx.action("quit")
            return ctx.leave
        if ch in (9, curses.KEY_DOWN):
            self.focus = (self.focus + 1) % len(_FOCUS)
            return False
        if ch in (getattr(curses, "KEY_BTAB", 353), curses.KEY_UP):
            self.focus = (self.focus - 1) % len(_FOCUS)
            return False
        before = ctx.snapshot()
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
            self._start_build(ctx)
        ctx.persist_if_changed(before)
        return False

    def _key_picker(self, ctx, ch: int) -> bool:
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
            ctx.action("pick", self.picker_index)
        return False

    def _key_build(self, ctx, ch: int) -> bool:
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
            ctx.action("stop_or_back")
        elif ch in (ord("q"), ord("Q")) and self.mode == MODE_DONE:
            ctx.action("stop_or_back")
        return False

    def _key_result(self, ctx, ch: int) -> bool:
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
            ctx.action("stop_or_back")
        return False

    def click(self, ctx, action: str, payload: object = None) -> bool:
        if action == "device":
            self.set_focus("device")
            self._open_picker()
            return True
        if action == "variant":
            self.set_focus("variant")
            if isinstance(payload, str) and payload in VARIANTS:
                self.variant = payload
            return True
        if action == "jobs":
            self.set_focus("jobs")
            delta = int(payload or 0)
            self.jobs = min(256, max(1, self.jobs + delta))
            return True
        if action == "focus" and isinstance(payload, str) and payload in _FOCUS:
            self.set_focus(payload)
            return True
        if action == "gapps":
            self.set_focus("gapps")
            if isinstance(payload, bool):
                self.gapps = payload
            return True
        if action == "ccache":
            self.set_focus("ccache")
            if isinstance(payload, bool):
                self.ccache = payload
            return True
        if action == "keep_going":
            self.set_focus("keep_going")
            if isinstance(payload, bool):
                self.keep_going = payload
            return True
        if action == "clean":
            self.set_focus("clean")
            if isinstance(payload, str):
                self.clean = payload
            return True
        if action == "build":
            self.set_focus("build")
            self._start_build(ctx)
            return True
        if action == "pick" and isinstance(payload, int):
            if 0 <= payload < len(self.products):
                self.selected = self.products[payload]
                self.picker_index = payload
            self.mode = MODE_CONFIG
            self.set_status()
            return True
        if action == "stop_or_back":
            if self.mode == MODE_BUILD:
                self._stop_build()
            elif self.mode == MODE_RESULT:
                self.mode = MODE_DONE
            elif self.mode == MODE_DONE:
                self.mode = MODE_CONFIG
            return True
        return False

    def _open_picker(self) -> None:
        if not self.products:
            self.set_status("no_devices")
            return
        if self.selected is not None:
            try:
                self.picker_index = self.products.index(self.selected)
            except ValueError:
                self.picker_index = 0
        self.mode = MODE_PICKER

    def _start_build(self, ctx) -> None:
        if self.selected is None:
            self.set_status("need_device")
            self._open_picker()
            return
        if self._stopping is not None and self._stopping.running:
            self.set_status("stopping")
            return
        log = LogBuffer()
        phase = PhaseTracker()
        self.log = log
        self.phase = phase
        self.log_scroll = 0
        self.report = None
        self.result_scroll = 0
        ctx.clear_pointer_state()
        self.set_status()
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
        rows, cols = ctx.stdscr.getmaxyx() if ctx.stdscr is not None else (24, 80)
        _left, _gap, _cy, _cx, ch, cw = _panel_geom(rows, cols)
        session.set_winsize(max(8, ch - 8), max(20, cw - 4))

    def _stop_build(self) -> None:
        session = self.session
        self.session = None
        self.mode = MODE_CONFIG
        self.set_status("stopped_build")
        if session is None:
            return
        self._stopping = session
        threading.Thread(target=session.stop, daemon=True).start()

    def _open_result(self, ctx, session: BuildSession, ok: bool) -> None:
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
        ctx.clear_scroll_drag()
        self.mode = MODE_RESULT
