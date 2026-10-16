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
from dataclasses import dataclass
from pathlib import Path

from core.builder import (
    CUSTOM_TARGET_MAX,
    GSI_TARGET,
    MEM_LIMIT_AGGRESSIVE,
    MEM_LIMIT_KINDS,
    MEM_LIMIT_OFF,
    MEM_LIMIT_STANDARD,
    TARGET_CUSTOM,
    TARGET_OTA,
    BuildConfig,
    BuildSession,
    mem_limit_tag,
    resolve_make_target,
    sanitize_make_targets,
    target_kinds_for,
)
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
    _cycle,
    _fmt_elapsed,
    _panel_geom,
    _rounded_frame,
    clip,
    dw,
)
from ui.ctx import Page
from ui.dialog import Dialog, paint_dialog
from ui.widgets import box_btn, fill_btn, jobs_row, option_block, text_field, yes_no

MODE_CONFIG = "config"
MODE_PICKER = "picker"
MODE_BUILD = "build"
MODE_DONE = "done"
MODE_RESULT = "result"

CLEAN_NONE = "none"
CLEAN_INSTALL = "installclean"
CLEAN_FULL = "clean"

VARIANTS = ("user", "userdebug", "eng")
_FOCUS_OUTER = (
    "device",
    "variant",
    "target",
    "gapps",
    "clean",
)
_FOCUS_INNER = (
    "jobs",
    "mem_limit",
    "ccache",
    "keep_going",
)
_CUSTOM_FOCUS_INPUT = 0
_CUSTOM_FOCUS_OK = 1
_STOP_GRACE_S = 0.4


@dataclass
class CustomTargetDialog:
    text: str
    cursor: int
    focus: int = _CUSTOM_FOCUS_INPUT
    error: bool = False


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
        self.mem_limit = prefs.mem_limit if prefs.mem_limit in MEM_LIMIT_KINDS else MEM_LIMIT_OFF
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
        self.target_kind = TARGET_OTA
        self.custom_target = ""
        self.custom_dialog: CustomTargetDialog | None = None
        self.dialog: Dialog | None = None
        self._mem_limit_pending: str | None = None
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
        self.advanced_open = False
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

    def overlay(self) -> bool:
        return self.custom_dialog is not None or self.dialog is not None

    def draw_overlay(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        if self.dialog is not None:
            paint_dialog(ctx, stdscr, y, x, h, w, self.dialog)
            return
        self._draw_custom_dialog(ctx, stdscr, y, x, h, w)

    def key_overlay(self, ctx, ch: int) -> bool:
        if self.dialog is not None:
            return self._key_mem_limit_dialog(ctx, ch)
        return self._key_custom_dialog(ctx, ch)

    def _focus_names(self) -> tuple[str, ...]:
        extra = _FOCUS_INNER if self.advanced_open else ()
        return _FOCUS_OUTER + extra + ("build",)

    def has_focus(self, name: str) -> bool:
        names = self._focus_names()
        return 0 <= self.focus < len(names) and names[self.focus] == name

    def set_focus(self, name: str) -> None:
        try:
            self.focus = self._focus_names().index(name)
        except ValueError:
            return

    def hide_close(self) -> bool:
        return self.mode == MODE_BUILD

    def showing_log(self) -> bool:
        return self.mode in (MODE_BUILD, MODE_DONE, MODE_RESULT)

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
        if self.dialog is not None:
            self._cancel_mem_limit()
            return True
        if self.custom_dialog is not None:
            self.custom_dialog = None
            return True
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
        self._clamp_target_kind()
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
        shown_kind = TARGET_CUSTOM if self.custom_dialog is not None else self.target_kind
        row = option_block(
            stdscr,
            hits,
            row,
            xx,
            inner,
            limit,
            ctx.t("target"),
            self._target_options(ctx),
            shown_kind,
            "target",
        )
        yn = yes_no(ctx.t)
        row = option_block(stdscr, hits, row, xx, inner, limit, ctx.t("gapps"), yn, self.gapps, "gapps")
        row = option_block(
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
        )
        if row < limit:
            mark = "▾" if self.advanced_open else "▸"
            text = f"{mark}  {ctx.t('advanced')}"
            _add(stdscr, row, xx, clip(text, inner), curses.color_pair(15) | curses.A_DIM, inner)
            hits.append(Hit(Rect(row, xx, 1, inner), "advanced"))
            row += 1
        if self.advanced_open:
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
            row = option_block(
                stdscr,
                hits,
                row,
                xx,
                inner,
                limit,
                ctx.t("mem_limit"),
                (
                    (MEM_LIMIT_OFF, ctx.t("mem_limit_off")),
                    (MEM_LIMIT_STANDARD, ctx.t("mem_limit_standard")),
                    (MEM_LIMIT_AGGRESSIVE, ctx.t("mem_limit_aggressive")),
                ),
                self._shown_mem_limit(),
                "mem_limit",
            )
            row = option_block(stdscr, hits, row, xx, inner, limit, ctx.t("ccache"), yn, self.ccache, "ccache")
            option_block(
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
        if session is not None:
            extra = mem_limit_tag(session.config.mem_limit)
            extra = f"  {extra}" if extra else ""
            combo = f"{session.config.lunch_combo}  make {session.config.make_target}  -j{session.config.jobs}{extra}"
        else:
            combo = self.combo_preview()
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
        ctx.draw_seg_rows(stdscr, body_top, xx, body_h, inner, rows, self.result_scroll)
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
        tag = mem_limit_tag(self._shown_mem_limit())
        if tag:
            extras.append(tag)
        extra = ("  " + " ".join(extras)) if extras else ""
        return f"{name}-{self.release}-{self.variant}  make {self._make_target()}  -j{self.jobs}{extra}"

    def _make_target(self) -> str:
        return resolve_make_target(self.target_kind, self.custom_target, self.selected)

    def _shown_mem_limit(self) -> str:
        if self.dialog is not None and self._mem_limit_pending in MEM_LIMIT_KINDS:
            return self._mem_limit_pending
        return self.mem_limit

    def _choose_mem_limit(self, value: str) -> None:
        if value not in MEM_LIMIT_KINDS:
            return
        if value == MEM_LIMIT_OFF or value == self.mem_limit or self.mem_limit != MEM_LIMIT_OFF:
            self.mem_limit = value
            self._mem_limit_pending = None
            self.dialog = None
            return
        self._open_mem_limit_dialog(value)

    def _open_mem_limit_dialog(self, value: str) -> None:
        self._mem_limit_pending = value
        self.dialog = Dialog(
            title_key="scm_sync_warn_title",
            body_key="mem_limit_warn_body",
            choices=(
                ("mem_limit_ok", "mem_limit_warn_ok"),
                ("cancel", "cancel"),
            ),
            kind="mem_limit",
            danger_payloads=("mem_limit_ok",),
        )

    def _confirm_mem_limit(self) -> None:
        if self._mem_limit_pending in MEM_LIMIT_KINDS:
            self.mem_limit = self._mem_limit_pending
        self._mem_limit_pending = None
        self.dialog = None

    def _cancel_mem_limit(self) -> None:
        self.mem_limit = MEM_LIMIT_OFF
        self._mem_limit_pending = None
        self.dialog = None

    def _key_mem_limit_dialog(self, ctx, ch: int) -> bool:
        dlg = self.dialog
        if dlg is None:
            return False
        n = len(dlg.choices)
        if n == 0:
            return False
        if ch in (curses.KEY_UP,):
            dlg.focus = (dlg.focus - 1) % n
        elif ch in (curses.KEY_DOWN, 9):
            dlg.focus = (dlg.focus + 1) % n
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            ctx.action("dialog", dlg.choices[dlg.focus][0])
        return False

    def _target_kinds(self) -> tuple[str, ...]:
        return target_kinds_for(self.selected)

    def _clamp_target_kind(self) -> None:
        if self.target_kind not in self._target_kinds():
            self.target_kind = TARGET_OTA

    def _target_options(self, ctx) -> tuple[tuple[object, str], ...]:
        custom = self.custom_target if self.custom_target else ctx.t("target_custom")
        gsi = self.selected is not None and self.selected.is_gsi
        options: list[tuple[object, str]] = []
        for kind in self._target_kinds():
            if kind == TARGET_OTA:
                label = ctx.t("target_gsi") if gsi else ctx.t("target_ota")
            elif kind == TARGET_CUSTOM:
                label = custom
            else:
                label = ctx.t("target_fastboot")
            options.append((kind, label))
        return tuple(options)

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
        names = self._focus_names()
        if ch in (9, curses.KEY_DOWN):
            self.focus = (self.focus + 1) % len(names)
            return False
        if ch in (getattr(curses, "KEY_BTAB", 353), curses.KEY_UP):
            self.focus = (self.focus - 1) % len(names)
            return False
        before = ctx.snapshot()
        name = names[self.focus]
        if name == "device" and ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._open_picker()
        elif name == "variant":
            self.variant = _cycle(VARIANTS, self.variant, ch)
        elif name == "target":
            kinds = self._target_kinds()
            if self.target_kind == TARGET_CUSTOM and ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
                self._open_custom_dialog()
            else:
                nxt = _cycle(kinds, self.target_kind, ch)
                if nxt == TARGET_CUSTOM:
                    self._open_custom_dialog()
                elif nxt in kinds:
                    self.target_kind = nxt
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
        elif name == "mem_limit":
            nxt = _cycle(MEM_LIMIT_KINDS, self._shown_mem_limit(), ch)
            self._choose_mem_limit(nxt)
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
            if self._in_start_grace():
                return False
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
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
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
        if action == "target":
            self.set_focus("target")
            kinds = self._target_kinds()
            if payload == TARGET_CUSTOM:
                self._open_custom_dialog()
            elif payload in kinds:
                self.target_kind = payload
            return True
        if action == "target_custom_input":
            if self.custom_dialog is not None:
                self.custom_dialog.focus = _CUSTOM_FOCUS_INPUT
            return True
        if action == "target_custom_ok":
            self._confirm_custom_dialog()
            return True
        if action == "target_custom_cancel":
            self.custom_dialog = None
            return True
        if action == "jobs":
            self.set_focus("jobs")
            delta = int(payload or 0)
            self.jobs = min(256, max(1, self.jobs + delta))
            return True
        if action == "mem_limit":
            self.set_focus("mem_limit")
            if isinstance(payload, str) and payload in MEM_LIMIT_KINDS:
                self._choose_mem_limit(payload)
            return True
        if action == "dialog" and isinstance(payload, str):
            if payload == "mem_limit_ok":
                self._confirm_mem_limit()
            elif payload == "cancel":
                self._cancel_mem_limit()
            return True
        if action == "focus" and isinstance(payload, str) and payload in self._focus_names():
            self.set_focus(payload)
            return True
        if action == "advanced":
            names = self._focus_names()
            current = names[self.focus] if 0 <= self.focus < len(names) else "clean"
            self.advanced_open = not self.advanced_open
            if not self.advanced_open and current in _FOCUS_INNER:
                current = "clean"
            self.set_focus(current)
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
                self._clamp_target_kind()
            self.mode = MODE_CONFIG
            self.set_status()
            return True
        if action == "stop_or_back":
            if self.mode == MODE_BUILD:
                if self._in_start_grace():
                    return True
                self._stop_build()
            elif self.mode == MODE_RESULT:
                self.mode = MODE_DONE
                ctx.clear_pointer_state()
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

    def _open_custom_dialog(self) -> None:
        text = self.custom_target
        self.custom_dialog = CustomTargetDialog(text=text, cursor=len(text))
        self.set_focus("target")

    def _confirm_custom_dialog(self) -> None:
        dlg = self.custom_dialog
        if dlg is None:
            return
        cleaned = sanitize_make_targets(dlg.text)
        if not cleaned:
            dlg.error = True
            dlg.focus = _CUSTOM_FOCUS_INPUT
            return
        self.custom_target = cleaned
        self.target_kind = TARGET_CUSTOM
        self.custom_dialog = None
        self.set_status()

    def _draw_custom_dialog(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        dlg = self.custom_dialog
        if dlg is None:
            return
        title = ctx.t("target_custom")
        ok_l = f" {ctx.t('target_custom_ok')} "
        err = ctx.t("target_custom_invalid") if dlg.error else ""
        box_w = min(w - 2, max(36, dw(title) + 8, dw(err) + 4, 28))
        box_h = min(9 if dlg.error else 8, h - 2)
        by = y + max(1, (h - box_h) // 2)
        bx = x + max(1, (w - box_w) // 2)
        fill_a = curses.color_pair(7)
        for i in range(box_h):
            _add(stdscr, by + i, bx, " " * box_w, fill_a, box_w)
        _rounded_frame(stdscr, by, bx, box_h, box_w, curses.color_pair(12), f"✦  {title}", 0)
        cx = bx + 2
        cw = max(8, box_w - 4)
        field_y = by + 2
        cur_x = text_field(
            stdscr,
            ctx.hits,
            field_y,
            cx,
            cw,
            dlg.text,
            "",
            "target_custom_input",
            dlg.focus == _CUSTOM_FOCUS_INPUT,
            dlg.cursor,
        )
        if dlg.error and field_y + 1 < by + box_h - 2:
            _add(stdscr, field_y + 1, cx, err, curses.color_pair(5) | curses.A_BOLD, cw)
        btn_y = by + box_h - 2
        ok_a = curses.color_pair(3) | curses.A_BOLD if dlg.focus == _CUSTOM_FOCUS_OK else curses.color_pair(10)
        ok_x = cx + max(0, cw - dw(ok_l))
        _add(stdscr, btn_y, ok_x, ok_l, ok_a, dw(ok_l))
        ctx.hits.append(Hit(Rect(btn_y, ok_x, 1, dw(ok_l)), "target_custom_ok"))
        if dlg.focus == _CUSTOM_FOCUS_INPUT:
            try:
                curses.curs_set(1)
                stdscr.move(field_y, min(cur_x, cx + cw - 1))
            except curses.error:
                pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def _key_custom_dialog(self, ctx, ch: int) -> bool:
        dlg = self.custom_dialog
        if dlg is None:
            return False
        if ch in (9, curses.KEY_DOWN):
            dlg.focus = _CUSTOM_FOCUS_OK
            return False
        if ch in (getattr(curses, "KEY_BTAB", 353), curses.KEY_UP):
            dlg.focus = _CUSTOM_FOCUS_INPUT
            return False
        if dlg.focus == _CUSTOM_FOCUS_OK:
            if ch in (curses.KEY_LEFT,):
                dlg.focus = _CUSTOM_FOCUS_INPUT
            elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
                ctx.action("target_custom_ok")
            return False
        if ch in (curses.KEY_ENTER, 10, 13):
            ctx.action("target_custom_ok")
            return False
        if ch in (curses.KEY_LEFT,):
            dlg.cursor = max(0, dlg.cursor - 1)
            return False
        if ch in (curses.KEY_RIGHT,):
            dlg.cursor = min(len(dlg.text), dlg.cursor + 1)
            return False
        if ch in (curses.KEY_HOME,):
            dlg.cursor = 0
            return False
        if ch in (curses.KEY_END,):
            dlg.cursor = len(dlg.text)
            return False
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if dlg.cursor > 0:
                dlg.text = dlg.text[: dlg.cursor - 1] + dlg.text[dlg.cursor :]
                dlg.cursor -= 1
                dlg.error = False
            return False
        if ch in (getattr(curses, "KEY_DC", 330),):
            if dlg.cursor < len(dlg.text):
                dlg.text = dlg.text[: dlg.cursor] + dlg.text[dlg.cursor + 1 :]
                dlg.error = False
            return False
        if 32 <= ch <= 126:
            char = chr(ch)
            if char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./+- ":
                return False
            if len(dlg.text) >= CUSTOM_TARGET_MAX:
                return False
            dlg.text = dlg.text[: dlg.cursor] + char + dlg.text[dlg.cursor :]
            dlg.cursor += 1
            dlg.error = False
        return False

    def _start_build(self, ctx) -> None:
        if self.selected is None:
            self.set_status("need_device")
            self._open_picker()
            return
        if self._stopping is not None and self._stopping.running:
            self.set_status("stopping")
            return
        self._clamp_target_kind()
        if self.target_kind == TARGET_CUSTOM and not sanitize_make_targets(self.custom_target):
            self._open_custom_dialog()
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
        make_target = self._make_target()
        config = BuildConfig(
            product=self.selected,
            jobs=self.jobs,
            gapps=self.gapps,
            ccache=self.ccache,
            keep_going=self.keep_going,
            clean=self.clean,
            release=self.release,
            variant=self.variant,
            make_target=make_target,
            show_sha256=self.target_kind == TARGET_OTA and not self.selected.is_gsi,
            mem_limit=self.mem_limit,
        )

        def on_data(text: str, buf: LogBuffer = log, tracker: PhaseTracker = phase) -> None:
            buf.feed(text)
            tracker.feed(text)

        session = BuildSession(self.top, config, on_data=on_data)
        self.session = session
        self.mode = MODE_BUILD
        try:
            session.start()
        except FileNotFoundError:
            self.session = None
            self.mode = MODE_CONFIG
            self.set_status("mem_limit_no_systemd")
        rows, cols = ctx.stdscr.getmaxyx() if ctx.stdscr is not None else (24, 80)
        _left, _gap, _cy, _cx, ch, cw = _panel_geom(rows, cols)
        session.set_winsize(max(8, ch - 8), max(20, cw - 4))

    def _in_start_grace(self) -> bool:
        session = self.session
        if session is None or self.mode != MODE_BUILD:
            return False
        return (time.time() - session.started_at) < _STOP_GRACE_S

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
            show_sha256=session.config.show_sha256,
            systemimage=session.config.make_target == GSI_TARGET,
        )
        self.report = report
        self.result_scroll = 0
        ctx.clear_pointer_state()
        self.mode = MODE_RESULT
