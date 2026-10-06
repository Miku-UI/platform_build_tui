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
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ui.logbuf import LogBuffer
from core.source import (
    CHECK_JOBS_DEFAULT,
    GIT_HTTPS,
    GIT_SSH,
    SCOPE_ALL,
    SCOPE_AOSP,
    SCOPE_FORKS,
    STATUS_AHEAD,
    STATUS_BEHIND,
    STATUS_DIVERGED,
    CommandSession,
    DiffRow,
    check_projects,
    local_paths,
    sync_argv,
)
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
    wrap_words,
)
from ui.ctx import Page
from ui.widgets import chip_row, fill_btn, jobs_row, option_block, yes_no

SCM_HOME = "home"
SCM_CHECK = "check"
SCM_RESULT = "result"
SCM_SYNC_CFG = "sync_cfg"
SCM_SYNC = "sync"

_SCM_HOME_ITEMS = (
    ("check", "scm_check"),
    ("check_offline", "scm_check_offline"),
    ("sync", "scm_sync"),
    ("settings", "scm_settings"),
)
_GIT_TRANSPORTS = (GIT_HTTPS, GIT_SSH)
_SETTINGS_FOCUS = ("transport", "check_jobs", "apply", "cancel")
_SCM_SYNC_FOCUS = ("sync_jobs", "sync_force", "sync_ignore", "sync_start")
_SCM_STATUS_KEY = {
    STATUS_AHEAD: "scm_status_ahead",
    STATUS_BEHIND: "scm_status_behind",
    STATUS_DIVERGED: "scm_status_diverged",
}
_SCM_STATUS_PAIR = {
    STATUS_AHEAD: 4,
    STATUS_BEHIND: 6,
    STATUS_DIVERGED: 5,
}


@dataclass
class Dialog:
    title_key: str
    choices: tuple[tuple[str, str], ...] = ()
    body_key: str = ""
    focus: int = 0
    kind: str = ""
    git_transport: str = GIT_HTTPS
    check_jobs: int = CHECK_JOBS_DEFAULT


class ScmPage(Page):
    def __init__(self, top: Path, git_transport: str | None, check_jobs: int | None) -> None:
        self.top = top
        self.page = SCM_HOME
        self.home_focus = 0
        self.focus = 0
        self.sync_jobs = 4
        self.sync_force = False
        self.sync_ignore = True
        self.log = LogBuffer()
        self.log_scroll = 0
        self.session: CommandSession | None = None
        self.status_key = ""
        self.status_args: dict[str, object] = {}
        self.dialog: Dialog | None = None
        self.diffs: list[DiffRow] = []
        self.result_off = 0
        self.result_index = 0
        self.check_done = 0
        self.check_total = 0
        self.check_path = ""
        self.check_stop: threading.Event | None = None
        self.check_fetch = True
        self.git_transport = git_transport if git_transport in _GIT_TRANSPORTS else GIT_HTTPS
        self.check_jobs = check_jobs if check_jobs is not None else CHECK_JOBS_DEFAULT
        self._lock = threading.Lock()
        self._checking = False
        self._reaped = False

    def showing_log(self) -> bool:
        return self.page == SCM_SYNC

    def hide_close(self) -> bool:
        return self.page == SCM_SYNC and self.session is not None and self.session.running

    def has_focus(self, name: str) -> bool:
        return 0 <= self.focus < len(_SCM_SYNC_FOCUS) and _SCM_SYNC_FOCUS[self.focus] == name

    def set_focus(self, name: str) -> None:
        try:
            self.focus = _SCM_SYNC_FOCUS.index(name)
        except ValueError:
            return

    def overlay(self) -> bool:
        return self.dialog is not None

    def draw_overlay(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        self.draw_dialog(ctx, stdscr, y, x, h, w)

    def key_overlay(self, ctx, ch: int) -> bool:
        return self.key_dialog(ctx, ch)

    def reap(self, ctx=None) -> None:
        scm = self.session
        if self.page == SCM_SYNC and scm is not None and not scm.running and not self._reaped:
            self._reaped = True
            code = scm.returncode
            scm.close()
            if scm.stopped:
                self.status_key = "stopped"
                self.status_args = {}
            elif code == 0:
                self.status_key = "scm_sync_ok"
                self.status_args = {}
            else:
                self.status_key = "scm_sync_fail"
                self.status_args = {"code": code}

    def draw(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        if self.page == SCM_CHECK:
            self._draw_check(ctx, stdscr, y, x, h, w)
        elif self.page == SCM_RESULT:
            self._draw_result(ctx, stdscr, y, x, h, w)
        elif self.page == SCM_SYNC_CFG:
            self._draw_sync_cfg(ctx, stdscr, y, x, h, w)
        elif self.page == SCM_SYNC:
            self._draw_sync(ctx, stdscr, y, x, h, w)
        else:
            self._draw_home(ctx, stdscr, y, x, h, w)

    def _draw_home(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), f"✦  {ctx.t('scm_frame')}", ctx.chrome_reserve())
        inner = max(10, w - 4)
        xx = x + 2
        row = y + 3
        for i, (name, key) in enumerate(_SCM_HOME_ITEMS):
            if row >= y + h - 3:
                break
            selected = i == self.home_focus
            attr = curses.color_pair(3) | curses.A_BOLD if selected else curses.color_pair(12)
            label = clip(("▸ " if selected else "  ") + ctx.t(key), inner)
            _add(stdscr, row, xx, label, attr, inner)
            ctx.hits.append(Hit(Rect(row, xx, 1, inner), "scm_home", name))
            row += 2
        if self.status_key:
            _add(
                stdscr,
                y + h - 4,
                xx,
                ctx.t(self.status_key, **self.status_args),
                curses.color_pair(6),
                inner,
            )
        ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _draw_check(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), f"✦  {ctx.t('scm_frame')}", ctx.chrome_reserve())
        inner = max(10, w - 4)
        xx = x + 2
        with self._lock:
            done = self.check_done
            total = self.check_total
            path = self.check_path
        top = y + 2
        bottom = y + h - 3
        area_h = max(0, bottom - top + 1)
        label = ctx.t("scm_checking")
        bar_w = min(inner, max(12, inner - 4))
        count = f"{done}/{total}" if total else "0/0"
        block = [label, "", "", count]
        if path:
            block.append(clip(path, inner))
        y0 = top + max(0, (area_h - len(block)) // 2)
        for i, line in enumerate(block):
            yy = y0 + i
            if yy > bottom:
                break
            if i == 2:
                frac = done / total if total else 0
                fill = min(bar_w, int(bar_w * frac))
                bar = "█" * fill + "░" * (bar_w - fill)
                lx = xx + max(0, (inner - bar_w) // 2)
                _add(stdscr, yy, lx, bar, curses.color_pair(11) | curses.A_BOLD, bar_w)
                continue
            lx = xx + max(0, (inner - dw(line)) // 2)
            attr = curses.color_pair(11) | curses.A_BOLD if i == 0 else curses.color_pair(10)
            _add(stdscr, yy, lx, line, attr, max(1, xx + inner - lx))
        ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _draw_result(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), f"✦  {ctx.t('scm_frame')}", ctx.chrome_reserve())
        inner = max(10, w - 4)
        xx = x + 2
        _add(stdscr, y + 2, xx, ctx.t("scm_diff_title"), curses.color_pair(15) | curses.A_DIM, inner)
        list_y = y + 4
        list_h = max(1, (y + h - 3) - list_y)
        with self._lock:
            rows = list(self.diffs)
        ctx.log_geom = Rect(list_y, xx, list_h, inner)
        if not rows:
            _add(stdscr, list_y, xx, ctx.t("scm_check_clean"), curses.color_pair(10), inner)
            ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)
            return
        n = len(rows)
        if self.result_index >= n:
            self.result_index = n - 1
        if self.result_index < 0:
            self.result_index = 0
        if self.result_index < self.result_off:
            self.result_off = self.result_index
        if self.result_index >= self.result_off + list_h:
            self.result_off = self.result_index - list_h + 1
        max_off = max(0, n - list_h)
        if self.result_off > max_off:
            self.result_off = max_off
        if self.result_off < 0:
            self.result_off = 0
        for i in range(list_h):
            idx = self.result_off + i
            if idx >= n:
                break
            item = rows[idx]
            selected = idx == self.result_index
            status = ctx.t(_SCM_STATUS_KEY.get(item.status, "result_unknown"))
            pair = _SCM_STATUS_PAIR.get(item.status, 10)
            status_w = dw(status)
            gap = 2
            path_w = max(4, inner - status_w - gap)
            prefix = "▸ " if selected else "  "
            path = clip(prefix + item.path, path_w)
            pad = max(0, inner - dw(path) - status_w)
            if selected:
                line = path + " " * pad + status
                _add(stdscr, list_y + i, xx, clip(line, inner), curses.color_pair(3) | curses.A_BOLD, inner)
            else:
                _add(stdscr, list_y + i, xx, path, curses.color_pair(2), path_w)
                _add(
                    stdscr,
                    list_y + i,
                    xx + dw(path) + pad,
                    status,
                    curses.color_pair(pair) | curses.A_BOLD,
                    status_w,
                )
            ctx.hits.append(Hit(Rect(list_y + i, xx, 1, inner), "scm_pick", idx))
        ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _draw_sync_cfg(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(stdscr, y, x, h, w, curses.color_pair(12), f"✦  {ctx.t('scm_frame')}", ctx.chrome_reserve())
        inner = max(10, w - 4)
        xx = x + 2
        row = y + 2
        limit = y + h - 6
        _add(stdscr, row, xx, self._sync_preview(), curses.color_pair(2) | curses.A_DIM, inner)
        row += 2
        if row < limit:
            _add(stdscr, row, xx, ctx.t("scm_sync_jobs"), curses.color_pair(15) | curses.A_DIM, inner)
            row += 1
        if row < limit:
            jobs_row(
                stdscr,
                ctx.hits,
                row,
                xx,
                inner,
                self.sync_jobs,
                "sync_jobs",
                "sync_jobs",
                focused=self.has_focus("sync_jobs"),
            )
            row += 2
        row = option_block(
            stdscr,
            ctx.hits,
            row,
            xx,
            inner,
            limit,
            ctx.t("scm_sync_force"),
            yes_no(ctx.t),
            self.sync_force,
            "sync_force",
            title_attr=curses.color_pair(5) | curses.A_BOLD,
        )
        option_block(
            stdscr,
            ctx.hits,
            row,
            xx,
            inner,
            limit,
            ctx.t("scm_sync_ignore"),
            yes_no(ctx.t),
            self.sync_ignore,
            "sync_ignore",
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
        start_label = ctx.t("scm_sync_start")
        btn_w = min(inner, max(22, dw(start_label) + 10))
        btn_x = x + max(0, (w - btn_w) // 2)
        btn_y = y + h - 5
        attr = curses.color_pair(3) | curses.A_BOLD
        if self.has_focus("sync_start"):
            attr = curses.color_pair(3) | curses.A_BOLD
        fill_btn(stdscr, ctx.hits, btn_y, btn_x, 3, btn_w, start_label, attr, "sync_start")
        ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _draw_sync(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        session = self.session
        elapsed = ""
        if session is not None:
            end = session.finished_at or time.time()
            elapsed = "  " + _fmt_elapsed(end - session.started_at)
        running = session is not None and session.running
        if running:
            title = ctx.t("scm_syncing")
        elif self.status_key:
            title = ctx.t(self.status_key, **self.status_args)
        else:
            title = ctx.t("done")
        room = max(0, w - 4 - dw(elapsed))
        title = clip(title, room)
        _rounded_frame(
            stdscr, y, x, h, w, curses.color_pair(12), f"✦  {title}{elapsed}", ctx.chrome_reserve()
        )
        inner = max(10, w - 4)
        xx = x + 2
        _add(stdscr, y + 2, xx, self._sync_preview(), curses.color_pair(10), inner)
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
        btn = ctx.t("scm_sync_start") if running else ctx.t("back")
        btn_w = min(inner, max(16, dw(btn) + 10))
        btn_x = x + max(0, (w - btn_w) // 2)
        btn_y = y + h - 5
        if running:
            left = max(0, (btn_w - dw(btn)) // 2)
            right = max(0, btn_w - left - dw(btn))
            dim = curses.color_pair(10)
            for i in range(3):
                text = (" " * left + btn + " " * right) if i == 1 else " " * btn_w
                _add(stdscr, btn_y + i, btn_x, text, dim, btn_w)
        else:
            fill_btn(stdscr, ctx.hits, btn_y, btn_x, 3, btn_w, btn, curses.color_pair(9) | curses.A_BOLD, "scm_back")
        ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def draw_dialog(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        dlg = self.dialog
        if dlg is None:
            return
        if dlg.kind == "settings":
            self._draw_settings_dialog(ctx, stdscr, y, x, h, w)
            return
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
            if dlg.kind == "force" and payload == "force_ok":
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

    def _draw_settings_dialog(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        dlg = self.dialog
        if dlg is None:
            return
        title = ctx.t("scm_settings")
        transport_title = ctx.t("scm_git_transport")
        jobs_title = ctx.t("scm_check_jobs")
        apply_l = f" {ctx.t('apply')} "
        cancel_l = f" {ctx.t('cancel')} "
        https_l = " HTTPS "
        ssh_l = " SSH "
        box_w = min(
            w - 2,
            max(
                36,
                dw(title) + 8,
                dw(transport_title) + 4,
                dw(jobs_title) + 4,
                dw(https_l) + dw(ssh_l) + 3,
                dw(apply_l) + dw(cancel_l) + 3,
            )
            + 4,
        )
        box_h = min(13, h - 2)
        by = y + max(1, (h - box_h) // 2)
        bx = x + max(1, (w - box_w) // 2)
        fill_a = curses.color_pair(7)
        for i in range(box_h):
            _add(stdscr, by + i, bx, " " * box_w, fill_a, box_w)
        _rounded_frame(stdscr, by, bx, box_h, box_w, curses.color_pair(12), f"✦  {title}", 0)
        cx = bx + 2
        cw = max(1, box_w - 4)
        row = by + 2
        limit = by + box_h - 2
        if row < limit:
            _add(stdscr, row, cx, transport_title, curses.color_pair(15) | curses.A_DIM, cw)
            row += 1
        if row < limit:
            chip_row(
                stdscr,
                ctx.hits,
                row,
                cx,
                cw,
                ((GIT_HTTPS, "HTTPS"), (GIT_SSH, "SSH")),
                dlg.git_transport,
                "settings_transport",
            )
            row += 2
        if row < limit:
            _add(stdscr, row, cx, jobs_title, curses.color_pair(15) | curses.A_DIM, cw)
            row += 1
        if row < limit:
            jobs_row(
                stdscr,
                ctx.hits,
                row,
                cx,
                cw,
                dlg.check_jobs,
                "settings_jobs",
                "check_jobs",
                focused=dlg.focus == 1,
            )
        btn_y = by + box_h - 2
        ax = cx
        apply_a = curses.color_pair(3) | curses.A_BOLD if dlg.focus == 2 else curses.color_pair(10)
        cancel_a = curses.color_pair(3) | curses.A_BOLD if dlg.focus == 3 else curses.color_pair(10)
        _add(stdscr, btn_y, ax, apply_l, apply_a, dw(apply_l))
        ctx.hits.append(Hit(Rect(btn_y, ax, 1, dw(apply_l)), "dialog", "apply"))
        ax += dw(apply_l) + 2
        _add(stdscr, btn_y, ax, cancel_l, cancel_a, dw(cancel_l))
        ctx.hits.append(Hit(Rect(btn_y, ax, 1, dw(cancel_l)), "dialog", "cancel"))

    def _sync_preview(self) -> str:
        parts = [f"repo sync -j{self.sync_jobs}"]
        if self.sync_force:
            parts.append("--force-checkout")
        if not self.sync_ignore:
            parts.append("--fail-fast")
        return " ".join(parts)

    def close(self) -> bool:
        if self.dialog is not None:
            self.dialog = None
            return True
        if self.page == SCM_CHECK:
            self._cancel_check()
            self.page = SCM_HOME
            return True
        if self.page == SCM_RESULT:
            self.page = SCM_HOME
            return True
        if self.page == SCM_SYNC_CFG:
            self.page = SCM_HOME
            return True
        if self.page == SCM_SYNC:
            if self.session is not None and self.session.running:
                return True
            self.page = SCM_HOME
            return True
        return False

    def _cancel_check(self) -> None:
        if self.check_stop is not None:
            self.check_stop.set()
        self._checking = False

    def _open_scope_dialog(self, fetch: bool = True) -> None:
        if not local_paths(self.top):
            self.status_key = "scm_no_repo"
            self.status_args = {}
            return
        self.status_key = ""
        self.check_fetch = fetch
        self.dialog = Dialog(
            title_key="scm_scope_title",
            choices=(
                (SCOPE_FORKS, "scm_scope_forks"),
                (SCOPE_AOSP, "scm_scope_aosp"),
                (SCOPE_ALL, "scm_scope_all"),
                ("cancel", "cancel"),
            ),
            kind="scope",
        )

    def _open_force_dialog(self) -> None:
        self.dialog = Dialog(
            title_key="scm_sync_warn_title",
            body_key="scm_sync_warn_body",
            choices=(
                ("force_ok", "scm_sync_warn_ok"),
                ("cancel", "cancel"),
            ),
            kind="force",
        )

    def _open_settings_dialog(self) -> None:
        self.dialog = Dialog(
            title_key="scm_settings",
            kind="settings",
            git_transport=self.git_transport if self.git_transport in _GIT_TRANSPORTS else GIT_HTTPS,
            check_jobs=max(1, min(256, self.check_jobs)),
        )

    def _start_check(self, scope: str) -> None:
        fetch = self.check_fetch
        transport = self.git_transport if self.git_transport in _GIT_TRANSPORTS else GIT_HTTPS
        workers = max(1, min(256, self.check_jobs))
        self._cancel_check()
        stop = threading.Event()
        self.check_stop = stop
        self.check_done = 0
        self.check_total = 1
        self.check_path = ""
        self.diffs = []
        self.result_off = 0
        self.result_index = 0
        self._checking = True
        self.page = SCM_CHECK

        def on_progress(done: int, total: int, path: str, event: threading.Event = stop) -> None:
            if event.is_set():
                return
            with self._lock:
                self.check_done = done
                self.check_total = max(total, 1)
                self.check_path = path

        def worker() -> None:
            diffs = check_projects(
                self.top,
                scope,
                on_progress=on_progress,
                stop=stop,
                fetch=fetch,
                transport=transport,
                workers=workers,
            )
            if stop.is_set():
                return
            with self._lock:
                self.diffs = diffs
                self.check_done = max(self.check_done, self.check_total)
                self.check_path = ""
                self._checking = False
                self.page = SCM_RESULT

        threading.Thread(target=worker, daemon=True).start()

    def _start_sync(self, ctx) -> None:
        try:
            argv = sync_argv(self.top, self.sync_jobs, self.sync_force, not self.sync_ignore)
        except FileNotFoundError:
            self.status_key = "scm_no_repo"
            self.status_args = {}
            return
        log = LogBuffer()
        self.log = log
        self.log_scroll = 0
        ctx.invalidate_wrap()
        self.status_key = ""
        self.status_args = {}
        session = CommandSession(self.top, argv, on_data=log.feed)
        self.session = session
        self._reaped = False
        self.page = SCM_SYNC
        session.start()
        rows, cols = ctx.stdscr.getmaxyx() if ctx.stdscr is not None else (24, 80)
        _left, _gap, _cy, _cx, ch, cw = _panel_geom(rows, cols)
        session.set_winsize(max(8, ch - 8), max(20, cw - 4))

    def move_result(self, delta: int) -> None:
        with self._lock:
            n = len(self.diffs)
        if n <= 0:
            self.result_index = 0
            return
        self.result_index = min(max(0, self.result_index + delta), n - 1)

    def wheel(self, ctx, delta: int) -> None:
        if self.page == SCM_RESULT:
            self.move_result(delta)
            return
        if self.page == SCM_SYNC:
            max_off = max(0, len(ctx.wrapped) - max(1, ctx.log_geom.h))
            self.log_scroll = min(max(0, self.log_scroll - delta), max_off)
        elif self.page == SCM_HOME:
            self.home_focus = min(max(0, self.home_focus + (1 if delta > 0 else -1)), len(_SCM_HOME_ITEMS) - 1)

    def key_dialog(self, ctx, ch: int) -> bool:
        dlg = self.dialog
        if dlg is None:
            return False
        if ch in (ord("q"), ord("Q")):
            ctx.action("dialog", "cancel")
            return ctx.leave
        if dlg.kind == "settings":
            return self._key_settings_dialog(ctx, ch)
        n = len(dlg.choices)
        if n == 0:
            return False
        if ch in (curses.KEY_UP,):
            dlg.focus = (dlg.focus - 1) % n
        elif ch in (curses.KEY_DOWN, 9):
            dlg.focus = (dlg.focus + 1) % n
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            ctx.action("dialog", dlg.choices[dlg.focus][0])
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
            ctx.cycle_tab(-1 if ch == curses.KEY_LEFT else 1)
        return False

    def _key_settings_dialog(self, ctx, ch: int) -> bool:
        dlg = self.dialog
        if dlg is None:
            return False
        n = len(_SETTINGS_FOCUS)
        if ch in (curses.KEY_UP, getattr(curses, "KEY_BTAB", 353)):
            dlg.focus = (dlg.focus - 1) % n
            return False
        if ch in (curses.KEY_DOWN, 9):
            dlg.focus = (dlg.focus + 1) % n
            return False
        name = _SETTINGS_FOCUS[dlg.focus] if 0 <= dlg.focus < n else "transport"
        if name == "transport" and ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord(" "), curses.KEY_ENTER, 10, 13):
            dlg.git_transport = _cycle(_GIT_TRANSPORTS, dlg.git_transport, ch)
            return False
        if name == "check_jobs":
            if ch in (curses.KEY_LEFT, ord("-")):
                dlg.check_jobs = max(1, dlg.check_jobs - 1)
            elif ch in (curses.KEY_RIGHT, ord("+"), ord("=")):
                dlg.check_jobs = min(256, dlg.check_jobs + 1)
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                dlg.check_jobs = max(1, dlg.check_jobs // 10)
            elif ord("0") <= ch <= ord("9"):
                dlg.check_jobs = min(256, dlg.check_jobs * 10 + (ch - ord("0")))
            return False
        if ch in (curses.KEY_LEFT,):
            if name == "cancel":
                dlg.focus = 2
            elif name == "apply":
                dlg.focus = 1
            return False
        if ch in (curses.KEY_RIGHT,) and name == "apply":
            dlg.focus = 3
            return False
        if ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            if name == "apply":
                ctx.action("dialog", "apply")
            elif name == "cancel":
                ctx.action("dialog", "cancel")
        return False

    def key(self, ctx, ch: int) -> bool:
        if self.page == SCM_HOME:
            if ch in (curses.KEY_UP,):
                self.home_focus = max(0, self.home_focus - 1)
                return True
            if ch in (curses.KEY_DOWN, 9):
                self.home_focus = min(len(_SCM_HOME_ITEMS) - 1, self.home_focus + 1)
                return True
            if ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
                ctx.action("scm_home", _SCM_HOME_ITEMS[self.home_focus][0])
                return True
            return False
        if self.page == SCM_RESULT:
            if ch in (curses.KEY_UP, _WHEEL_UP):
                self.move_result(-1)
                return True
            if ch in (curses.KEY_DOWN,):
                self.move_result(1)
                return True
            if ch in (curses.KEY_PPAGE,):
                self.move_result(-10)
                return True
            if ch in (curses.KEY_NPAGE,):
                self.move_result(10)
                return True
            if ch in (curses.KEY_HOME,):
                self.result_index = 0
                return True
            if ch in (curses.KEY_END,):
                with self._lock:
                    n = len(self.diffs)
                self.result_index = max(0, n - 1)
                return True
            return False
        if self.page == SCM_SYNC_CFG:
            return self._key_sync_cfg(ctx, ch)
        if self.page == SCM_SYNC:
            if ch in (curses.KEY_UP, _WHEEL_UP):
                self.log_scroll += 1
                return True
            if ch in (curses.KEY_DOWN,):
                self.log_scroll = max(0, self.log_scroll - 1)
                return True
            if ch in (curses.KEY_PPAGE,):
                self.log_scroll += 10
                return True
            if ch in (curses.KEY_NPAGE,):
                self.log_scroll = max(0, self.log_scroll - 10)
                return True
            if ch in (curses.KEY_END,):
                self.log_scroll = 0
                return True
            running = self.session is not None and self.session.running
            if not running and ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
                ctx.action("scm_back")
                return True
            return True
        return False

    def _key_sync_cfg(self, ctx, ch: int) -> bool:
        if ch in (9, curses.KEY_DOWN):
            self.focus = (self.focus + 1) % len(_SCM_SYNC_FOCUS)
            return True
        if ch in (getattr(curses, "KEY_BTAB", 353), curses.KEY_UP):
            self.focus = (self.focus - 1) % len(_SCM_SYNC_FOCUS)
            return True
        name = _SCM_SYNC_FOCUS[self.focus]
        if name == "sync_jobs":
            if ch in (curses.KEY_LEFT, ord("-")):
                self.sync_jobs = max(1, self.sync_jobs - 1)
            elif ch in (curses.KEY_RIGHT, ord("+"), ord("=")):
                self.sync_jobs = min(256, self.sync_jobs + 1)
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.sync_jobs = max(1, self.sync_jobs // 10)
            elif ord("0") <= ch <= ord("9"):
                self.sync_jobs = min(256, self.sync_jobs * 10 + (ch - ord("0")))
            else:
                return False
            return True
        if name == "sync_force":
            nxt = _cycle((True, False), self.sync_force, ch)
            if nxt != self.sync_force:
                if nxt:
                    self._open_force_dialog()
                else:
                    self.sync_force = False
            return True
        if name == "sync_ignore":
            self.sync_ignore = _cycle((True, False), self.sync_ignore, ch)
            return True
        if name == "sync_start" and ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._start_sync(ctx)
            return True
        return False

    def click(self, ctx, action: str, payload: object = None) -> bool:
        if action == "focus" and payload == "check_jobs":
            if self.dialog is not None and self.dialog.kind == "settings":
                self.dialog.focus = 1
            return True
        if action == "focus" and isinstance(payload, str) and payload in _SCM_SYNC_FOCUS:
            self.set_focus(payload)
            return True
        if action == "scm_pick" and isinstance(payload, int):
            with self._lock:
                n = len(self.diffs)
            if 0 <= payload < n:
                self.result_index = payload
            return True
        if action == "scm_home" and isinstance(payload, str):
            if payload == "check":
                self.home_focus = 0
                self._open_scope_dialog(True)
            elif payload == "check_offline":
                self.home_focus = 1
                self._open_scope_dialog(False)
            elif payload == "sync":
                self.home_focus = 2
                self.focus = 0
                self.status_key = ""
                self.page = SCM_SYNC_CFG
            elif payload == "settings":
                self.home_focus = 3
                self._open_settings_dialog()
            return True
        if action == "sync_jobs":
            self.set_focus("sync_jobs")
            delta = int(payload or 0)
            self.sync_jobs = min(256, max(1, self.sync_jobs + delta))
            return True
        if action == "sync_force":
            self.set_focus("sync_force")
            if isinstance(payload, bool):
                if payload and not self.sync_force:
                    self._open_force_dialog()
                elif not payload:
                    self.sync_force = False
            return True
        if action == "sync_ignore":
            self.set_focus("sync_ignore")
            if isinstance(payload, bool):
                self.sync_ignore = payload
            return True
        if action == "sync_start":
            self.set_focus("sync_start")
            self._start_sync(ctx)
            return True
        if action == "scm_back":
            self.page = SCM_HOME
            return True
        if action == "settings_transport" and isinstance(payload, str) and payload in _GIT_TRANSPORTS:
            if self.dialog is not None and self.dialog.kind == "settings":
                self.dialog.git_transport = payload
                self.dialog.focus = 0
            return True
        if action == "settings_jobs":
            if self.dialog is not None and self.dialog.kind == "settings":
                self.dialog.focus = 1
                delta = int(payload or 0)
                self.dialog.check_jobs = min(256, max(1, self.dialog.check_jobs + delta))
            return True
        if action == "dialog" and isinstance(payload, str):
            kind = self.dialog.kind if self.dialog is not None else ""
            draft = self.dialog.git_transport if self.dialog is not None else GIT_HTTPS
            draft_jobs = self.dialog.check_jobs if self.dialog is not None else CHECK_JOBS_DEFAULT
            self.dialog = None
            if payload == "cancel":
                if kind == "force":
                    self.sync_force = False
            elif payload == "apply" and kind == "settings":
                if draft in _GIT_TRANSPORTS:
                    self.git_transport = draft
                self.check_jobs = min(256, max(1, int(draft_jobs)))
            elif payload in (SCOPE_FORKS, SCOPE_AOSP, SCOPE_ALL):
                self._start_check(payload)
            elif payload == "force_ok":
                self.sync_force = True
            return True
        return False
