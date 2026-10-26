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
import webbrowser
from pathlib import Path

from core.patchset import Conflict, SetView, analyze, apply_defaults, localized
from ui.ctx import Page
from ui.term import Hit, Rect, _add, _rounded_frame, clip, dw, wrap_words


class PatchsetPage(Page):
    def __init__(self, top: Path, enabled: tuple[str, ...], known: tuple[str, ...]) -> None:
        self.top = top
        self.enabled = set(enabled)
        self.known = set(known)
        self.prefs_dirty = False
        self.views: list[SetView] = []
        self.loading = False
        self.load_done = 0
        self.load_total = 0
        self.index = 0
        self.list_off = 0
        self.detail: str | None = None
        self.detail_scroll = 0
        self.detail_max = 0
        self.detail_focus = 0
        self._shown = False
        self._seq = 0
        self._lock = threading.Lock()

    def overlay(self) -> bool:
        return self.detail is not None

    def close(self) -> bool:
        if self.detail is None:
            return False
        self.detail = None
        return True

    def settle(self) -> bool:
        enabled, known, changed = apply_defaults(self.top, self.enabled, self.known)
        self.enabled = set(enabled)
        self.known = set(known)
        if changed:
            self.prefs_dirty = True
        return changed

    def note_shown(self) -> None:
        if self._shown:
            return
        self._shown = True
        self.settle()
        self.request_scan()

    def note_hidden(self) -> None:
        self._shown = False

    def request_scan(self) -> None:
        self._seq += 1
        seq = self._seq
        enabled = tuple(sorted(self.enabled))
        top = self.top
        with self._lock:
            self.loading = True
            self.load_done = 0
            self.load_total = 0

        def progress(done: int, total: int) -> None:
            with self._lock:
                if seq != self._seq:
                    return
                self.load_done = done
                self.load_total = total

        def work() -> None:
            views = analyze(top, enabled, progress)
            with self._lock:
                if seq != self._seq:
                    return
                self.views = views
                self.loading = False
                self.load_done = len(views)
                self.load_total = len(views)

        threading.Thread(target=work, daemon=True).start()

    def draw(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        _rounded_frame(
            stdscr,
            y,
            x,
            h,
            w,
            curses.color_pair(12),
            f"✦  {ctx.t('patchset_frame')}",
            ctx.chrome_reserve(),
        )
        inner = max(10, w - 4)
        xx = x + 2
        with self._lock:
            loading = self.loading
            done = self.load_done
            total = self.load_total
            views = list(self.views)
        if loading:
            self._draw_loading(ctx, stdscr, y, x, h, w, inner, xx, done, total)
            return
        self._draw_list(ctx, stdscr, y, x, h, w, inner, xx, views)

    def draw_overlay(self, ctx, stdscr: curses.window, y: int, x: int, h: int, w: int) -> None:
        view = self._selected_view()
        if view is None:
            self.detail = None
            return
        full_w = max(8, w - 2)
        bw = min(full_w, max(20, full_w // 2))
        inner = max(8, bw - 4)
        lines = self._detail_lines(ctx, view, inner)
        chrome = 6
        bh = min(max(chrome, len(lines) + chrome), max(chrome, h - 2))
        by = y + max(0, (h - bh) // 2)
        bx = x + max(0, (w - bw) // 2)
        fill_a = curses.color_pair(7)
        for i in range(bh):
            _add(stdscr, by + i, bx, " " * bw, fill_a, bw)
        _rounded_frame(stdscr, by, bx, bh, bw, curses.color_pair(12), f"✦  {ctx.t('patchset_frame')}", 0)
        xx = bx + 2
        body_top = by + 2
        body_h = max(1, bh - 6)
        max_off = max(0, len(lines) - body_h)
        self.detail_max = max_off
        self.detail_scroll = min(max(0, self.detail_scroll), max_off)
        for row in range(body_h):
            idx = self.detail_scroll + row
            if idx >= len(lines):
                break
            text, attr, url = lines[idx]
            yy = body_top + row
            _add(stdscr, yy, xx, clip(text, inner), attr, inner)
            if url:
                ctx.hits.append(Hit(Rect(yy, xx, 1, inner), "patchset_url", url))
        self._draw_detail_buttons(ctx, stdscr, by + bh - 3, xx, inner, view.enabled)

    def key(self, ctx, ch: int) -> bool:
        if self.loading:
            return False
        with self._lock:
            count = len(self.views)
        if count <= 0:
            return False
        if ch in (curses.KEY_UP,):
            self.index = max(0, self.index - 1)
        elif ch in (curses.KEY_DOWN,):
            self.index = min(count - 1, self.index + 1)
        elif ch in (curses.KEY_PPAGE,):
            self.index = max(0, self.index - 10)
        elif ch in (curses.KEY_NPAGE,):
            self.index = min(count - 1, self.index + 10)
        elif ch in (curses.KEY_HOME,):
            self.index = 0
        elif ch in (curses.KEY_END,):
            self.index = count - 1
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._open_index()
        return False

    def key_overlay(self, ctx, ch: int) -> bool:
        if ch in (curses.KEY_UP,):
            self.detail_scroll = max(0, self.detail_scroll - 1)
        elif ch in (curses.KEY_DOWN,):
            self.detail_scroll = min(self.detail_max, self.detail_scroll + 1)
        elif ch in (curses.KEY_PPAGE,):
            self.detail_scroll = max(0, self.detail_scroll - 10)
        elif ch in (curses.KEY_NPAGE,):
            self.detail_scroll = min(self.detail_max, self.detail_scroll + 10)
        elif ch in (curses.KEY_LEFT, getattr(curses, "KEY_BTAB", 353)):
            self.detail_focus = (self.detail_focus - 1) % 2
        elif ch in (curses.KEY_RIGHT, 9):
            self.detail_focus = (self.detail_focus + 1) % 2
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            if self.detail_focus == 0:
                ctx.action("patchset_close")
            else:
                ctx.action("patchset_toggle")
        return False

    def wheel(self, ctx, delta: int) -> None:
        if self.detail is not None:
            self.detail_scroll = min(max(0, self.detail_scroll + delta), self.detail_max)
            return
        if self.loading:
            return
        with self._lock:
            count = len(self.views)
        if count <= 0:
            return
        self.index = min(max(0, self.index + delta), count - 1)

    def click(self, ctx, action: str, payload: object = None) -> bool:
        if action == "patchset_item" and isinstance(payload, str):
            self._open_folder(payload)
            return True
        if action == "patchset_close":
            self.detail_focus = 0
            self.detail = None
            return True
        if action == "patchset_toggle":
            self.detail_focus = 1
            self._toggle_open()
            return True
        if action == "patchset_url" and isinstance(payload, str):
            _open_url(payload)
            return True
        return False

    def _draw_loading(self, ctx, stdscr, y, x, h, w, inner, xx, done, total) -> None:
        top = y + 2
        bottom = y + h - 3
        area_h = max(0, bottom - top + 1)
        label = ctx.t("patchset_loading")
        bar_w = min(inner, max(12, inner - 4))
        count = f"{done}/{total}" if total else "0/0"
        block = [label, "", "", count]
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

    def _draw_list(self, ctx, stdscr, y, x, h, w, inner, xx, views: list[SetView]) -> None:
        list_y = y + 3
        list_h = max(1, (y + h - 3) - list_y)
        if not views:
            _add(stdscr, list_y, xx, ctx.t("patchset_empty"), curses.color_pair(10), inner)
            ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)
            return
        rows = _section_rows(views)
        folders = _section_folders(views)
        if self.index >= len(folders):
            self.index = len(folders) - 1
        if self.index < 0:
            self.index = 0
        item_at = [pos for pos, (kind, _payload) in enumerate(rows) if kind == "item"]
        sel = item_at[self.index]
        if sel < self.list_off:
            self.list_off = sel
        if sel >= self.list_off + list_h:
            self.list_off = sel - list_h + 1
        max_off = max(0, len(rows) - list_h)
        self.list_off = min(max(0, self.list_off), max_off)
        by_folder = {view.patchset.folder: view for view in views}
        lang = ctx.lang
        for row in range(list_h):
            idx = self.list_off + row
            if idx >= len(rows):
                break
            kind, payload = rows[idx]
            yy = list_y + row
            if kind == "head":
                _add(stdscr, yy, xx, ctx.t(payload), curses.color_pair(15) | curses.A_DIM, inner)
                continue
            if kind != "item":
                continue
            view = by_folder[payload]
            selected = payload == folders[self.index]
            mark = _mark(view)
            title = localized(view.patchset.title, lang) or view.patchset.folder
            prefix = f"{mark} "
            if selected:
                _add(stdscr, yy, xx, clip(prefix + title, inner), curses.color_pair(3) | curses.A_BOLD, inner)
            else:
                mark_w = dw(prefix)
                _add(stdscr, yy, xx, prefix, _mark_attr(view), mark_w)
                _add(stdscr, yy, xx + mark_w, clip(title, max(1, inner - mark_w)), curses.color_pair(2), max(1, inner - mark_w))
            ctx.hits.append(Hit(Rect(yy, xx, 1, inner), "patchset_item", view.patchset.folder))
        ctx.draw_lang_bar(stdscr, y + h - 2, xx, inner)

    def _detail_lines(self, ctx, view: SetView, width: int) -> list[tuple[str, int, str]]:
        lang = ctx.lang
        item = view.patchset
        lines: list[tuple[str, int, str]] = []
        title = localized(item.title, lang) or item.folder
        desc = localized(item.description, lang)
        dim = curses.color_pair(15) | curses.A_DIM
        body = curses.color_pair(2)
        for text in _wrap_keep(title, width):
            lines.append((text, curses.color_pair(11) | curses.A_BOLD, ""))
        lines.append(("", body, ""))
        for text in _wrap_keep(desc, width):
            lines.append((text, body, ""))
        lines.append(("", body, ""))
        lines.append((ctx.t("patchset_author"), dim, ""))
        author = item.author or "—"
        url = item.author_url
        author_attr = curses.color_pair(11) | curses.A_UNDERLINE if url else body
        for text in _wrap_keep(author, width):
            lines.append((text, author_attr, url))
        if url:
            for text in _wrap_keep(url, width):
                lines.append((text, author_attr, url))
        lines.append(("", body, ""))
        lines.append((ctx.t("patchset_repos"), dim, ""))
        repos: list[str] = []
        for patch in item.patches:
            if patch.project not in repos:
                repos.append(patch.project)
        if not repos:
            lines.append(("—", body, ""))
        for project in repos:
            for text in _wrap_keep(project, width):
                lines.append((text, body, ""))
        lines.append(("", body, ""))
        if not view.conflicts:
            lines.append((ctx.t("patchset_no_conflict"), curses.color_pair(4), ""))
            return lines
        names = {entry.patchset.folder: localized(entry.patchset.title, lang) or entry.patchset.folder for entry in self._views_copy()}
        sep = "、" if lang == "zh" else ", "
        for pos, conflict in enumerate(view.conflicts):
            if pos:
                lines.append(("", body, ""))
            name = _conflict_name(ctx, conflict, names, sep)
            files = "\n".join(conflict.files)
            message = ctx.t("patchset_conflict_with", name=name, files=files)
            for text in _wrap_keep(message, width):
                lines.append((text, curses.color_pair(5), ""))
        return lines

    def _draw_detail_buttons(self, ctx, stdscr, y: int, x: int, width: int, enabled: bool) -> None:
        labels = (
            (0, ctx.t("confirm"), "patchset_close"),
            (1, ctx.t("patchset_disable") if enabled else ctx.t("patchset_enable"), "patchset_toggle"),
        )
        cx = x
        for focus, label, action in labels:
            text = f" {label} "
            w = dw(text)
            if cx + w - x > width:
                break
            attr = curses.color_pair(3) | curses.A_BOLD if focus == self.detail_focus else curses.color_pair(12)
            _add(stdscr, y, cx, text, attr, w)
            ctx.hits.append(Hit(Rect(y, cx, 1, w), action, focus))
            cx += w + 2

    def _open_index(self) -> None:
        with self._lock:
            if not self.views or self.loading:
                return
            folders = _section_folders(self.views)
            if self.index >= len(folders):
                return
            folder = folders[self.index]
        self._open_folder(folder)

    def _open_folder(self, folder: str) -> None:
        with self._lock:
            views = list(self.views)
        folders = _section_folders(views)
        if folder not in folders:
            return
        self.index = folders.index(folder)
        self.detail = folder
        self.detail_scroll = 0
        self.detail_focus = 0

    def _toggle_open(self) -> None:
        folder = self.detail
        if not folder:
            return
        self.known.add(folder)
        if folder in self.enabled:
            self.enabled.discard(folder)
        else:
            self.enabled.add(folder)
        self.detail = None
        self.request_scan()

    def _selected_view(self) -> SetView | None:
        folder = self.detail
        if not folder:
            return None
        with self._lock:
            views = list(self.views)
        for view in views:
            if view.patchset.folder == folder:
                return view
        return None

    def _views_copy(self) -> list[SetView]:
        with self._lock:
            return list(self.views)


def _section_folders(views: list[SetView]) -> list[str]:
    recommended = [view.patchset.folder for view in views if view.patchset.recommend]
    available = [view.patchset.folder for view in views if not view.patchset.recommend]
    return recommended + available


def _section_rows(views: list[SetView]) -> list[tuple[str, str]]:
    folders = _section_folders(views)
    split = sum(1 for view in views if view.patchset.recommend)
    rows: list[tuple[str, str]] = [("head", "patchset_recommended")]
    rows.extend(("item", folder) for folder in folders[:split])
    rows.append(("gap", ""))
    rows.append(("head", "patchset_available"))
    rows.extend(("item", folder) for folder in folders[split:])
    return rows


def _mark(view: SetView) -> str:
    if view.enabled and view.conflicts:
        return "[!]"
    if view.enabled:
        return "[x]"
    return "[ ]"


def _mark_attr(view: SetView) -> int:
    if view.enabled and view.conflicts:
        return curses.color_pair(5)
    if view.enabled:
        return curses.color_pair(4)
    return curses.color_pair(10)


def _conflict_name(ctx, conflict: Conflict, names: dict[str, str], sep: str) -> str:
    if not conflict.others:
        return ctx.t("patchset_source")
    return sep.join(names.get(folder, folder) for folder in conflict.others)


def _wrap_keep(text: str, width: int) -> list[str]:
    if text == "":
        return [""]
    lines: list[str] = []
    for part in text.split("\n"):
        lines.extend(wrap_words(part, width) if part else [""])
    return lines or [""]


def _open_url(url: str) -> None:
    if not url.startswith(("https://", "http://")):
        return
    try:
        webbrowser.open(url)
    except webbrowser.Error:
        return
