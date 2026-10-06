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

from report import BuildReport, bar_widths, fmt_delta, fmt_duration, fmt_mb, fmt_size_delta
from term import _glyphs_to_segs, _wrap_glyphs, dw, wrap_words

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


def kv_rows(
    label: str, value: str, label_attr: int, value_attr: int, width: int
) -> list[list[tuple[str, int]]]:
    if dw(label) + dw(value) <= width:
        return [[(label, label_attr), (value, value_attr)]]
    rows: list[list[tuple[str, int]]] = [[(label, label_attr)]]
    for line in wrap_words(value, max(1, width - 2)):
        rows.append([("  " + line, value_attr)])
    return rows


def delta_pair(delta: int | None, first: str, size: bool) -> tuple[str, int]:
    if delta is None:
        return first, curses.color_pair(6) | curses.A_BOLD
    text = fmt_size_delta(delta) if size else fmt_delta(delta)
    if delta > 0:
        return text, curses.color_pair(5) | curses.A_BOLD
    if delta < 0:
        return text, curses.color_pair(4) | curses.A_BOLD
    return text, curses.color_pair(10)


def result_rows(report: BuildReport, width: int, t) -> list[list[tuple[str, int]]]:
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
        rows.extend(kv_rows(label, value, label_a, vattr, width))

    headline = t("result_headline", version=report.version) + t(
        "result_headline_ok" if report.ok else "result_headline_fail"
    )
    for line in wrap_words(headline, width) or [headline]:
        rows.append([(line, title_a)])
    blank()
    targets = str(report.targets) if report.targets is not None else t("result_unknown")
    kv(t("result_targets"), targets)
    kv(t("result_targets_delta"), *delta_pair(report.targets_delta, t("result_first"), False))
    blank()
    title(t("result_timing"))
    for timing in report.timings:
        pair = _STAGE_PAIR.get(timing.key, 2)
        key = _STAGE_TIME_KEY.get(timing.key)
        if key is None:
            continue
        label_c = curses.color_pair(pair)
        rows.extend(kv_rows(t(key), fmt_duration(timing.seconds), label_c, value_a, width))
    rows.extend(kv_rows(t("result_time_total"), fmt_duration(report.total), title_a, value_a, width))
    blank()
    bar_w = max(8, width)
    parts = bar_widths([timing.seconds for timing in report.timings], bar_w)
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
    cap = t("result_bar_caption")
    pad = max(0, (width - dw(cap)) // 2)
    rows.append([(" " * pad + cap, dim_a)])
    if report.ok:
        blank()
        title(t("result_artifacts"))
        kv(t("result_device_code"), report.device or t("result_unknown"))
        kv(t("result_device_name"), report.model or t("result_unknown"))
        if report.gapps:
            kv(t("result_has_gapps"), t("yes"), curses.color_pair(4) | curses.A_BOLD)
        else:
            kv(t("result_has_gapps"), t("no"), curses.color_pair(5) | curses.A_BOLD)
        path = report.artifact_rel or t("result_unknown")
        kv(t("result_artifact_path"), path)
        size = fmt_mb(report.artifact_size) if report.artifact_size is not None else t("result_unknown")
        kv(t("result_artifact_size"), size)
        kv(t("result_size_delta"), *delta_pair(report.size_delta, t("result_first"), True))
        kv(t("result_sha256"), report.sha256 or t("result_unknown"))
        if report.signed is True:
            kv(t("result_signed"), t("yes"), curses.color_pair(4) | curses.A_BOLD)
            if report.key_rel:
                kv(t("result_key_path"), report.key_rel)
        elif report.signed is False:
            kv(t("result_signed"), t("no"), curses.color_pair(5) | curses.A_BOLD)
    else:
        blank()
        title(t("result_fail_analysis"))
        stage_key = _STAGE_NAME_KEY.get(report.fail_stage or "", "result_stage_prepare")
        stage_pair = _STAGE_PAIR.get(report.fail_stage or "prepare", 2)
        kv(t("result_fail_stage"), t(stage_key), curses.color_pair(stage_pair) | curses.A_BOLD)
        if not report.failures:
            kv(t("result_fail_reason"), t("result_unknown"))
        for item in report.failures:
            if report.fail_stage == "ninja" and item.module:
                kv(t("result_fail_module"), item.module)
            rows.append([(t("result_fail_reason"), label_a)])
            for glyphs in item.rows:
                wrapped = _wrap_glyphs(glyphs, max(1, width - 2))
                for piece in wrapped:
                    rows.append([("  ", dim_a), *_glyphs_to_segs(piece)])
            blank()
    return rows
