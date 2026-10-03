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

import os

LANGS = ("en", "zh", "ja", "ru")
LANG_CHIPS = (
    ("zh", "中"),
    ("en", "EN"),
    ("ja", "日"),
    ("ru", "РУ"),
)

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "term_too_small": "Please enlarge the terminal (60x16 min)",
        "device": "Device",
        "pick_device": "Select a device",
        "jobs": "Jobs",
        "clean": "Clean build",
        "clean_none": "None",
        "clean_install": "installclean",
        "clean_full": "make clean",
        "build_now": "BUILD NOW!",
        "hint": "click  ·  tab  ·  q",
        "on": "ON",
        "off": "OFF",
        "picker_title": "Select device",
        "picker_hint": "Click an item, Esc to cancel",
        "picker_keys": "↑↓  ·  enter",
        "building": "Building",
        "done": "Done",
        "stopped": "Stopped",
        "build_ok": "Build finished",
        "build_fail": "Build failed  exit={code}",
        "stop": "STOP",
        "back": "BACK",
        "no_devices": "No buildable devices found",
        "need_device": "Select a device first",
        "stopping": "Stopping the previous build…",
        "stopped_build": "Build stopped",
        "tui_failed": "TUI failed: {exc}. Pass --device to build without the UI.",
        "cli_desc": "Miku UI build helper. No args opens the TUI; --device builds in this terminal.",
        "cli_device": "device, e.g. zeekr / miku_zeekr",
        "cli_gapps": "export MIKU_GAPPS=true",
        "cli_jobs": "make -jN (default: CPU count)",
        "cli_ccache": "enable ccache (on by default; --no-ccache disables)",
        "cli_clean": "run make clean before building",
        "cli_installclean": "run make installclean before building",
        "cli_release": "lunch release (default from vendor/miku, usually cp2a)",
        "cli_variant": "lunch variant (default userdebug)",
        "cli_jobs_err": "--jobs must be >= 1",
        "cli_no_tty": "TUI needs a terminal. Pass --device to build without the UI.",
        "cli_building": "Building {label} as {combo} -j{jobs}{extra}",
        "info_rom": "ROM",
        "info_os": "OS",
        "info_cpu": "CPU",
        "info_ram": "RAM",
        "info_disk": "DISK",
    },
    "zh": {
        "term_too_small": "请放大终端（至少 60x16）",
        "device": "构建机型",
        "pick_device": "选择机型",
        "jobs": "编译线程数",
        "clean": "干净构建",
        "clean_none": "否",
        "clean_install": "installclean",
        "clean_full": "make clean",
        "build_now": "开始构建",
        "hint": "点击  ·  Tab  ·  q",
        "on": "开",
        "off": "关",
        "picker_title": "选择机型",
        "picker_hint": "点击一项，Esc 取消",
        "picker_keys": "↑↓  ·  Enter",
        "building": "构建中",
        "done": "完成",
        "stopped": "已停止",
        "build_ok": "构建完成",
        "build_fail": "构建失败  exit={code}",
        "stop": "停止",
        "back": "返回",
        "no_devices": "未找到可构建机型",
        "need_device": "请先选择构建机型",
        "stopping": "正在停止上一次构建…",
        "stopped_build": "已停止构建",
        "tui_failed": "TUI 启动失败：{exc}。请加 --device 在终端直接构建。",
        "cli_desc": "Miku UI 构建助手。无参数打开 TUI；指定 --device 则直接在当前终端构建。",
        "cli_device": "机型，如 zeekr / miku_zeekr",
        "cli_gapps": "构建时 export MIKU_GAPPS=true",
        "cli_jobs": "make -jN（默认 CPU 核数）",
        "cli_ccache": "启用 ccache（默认开启；--no-ccache 关闭）",
        "cli_clean": "构建前 make clean",
        "cli_installclean": "构建前 make installclean",
        "cli_release": "lunch release（默认读 vendor/miku，一般为 cp2a）",
        "cli_variant": "lunch variant（默认 userdebug）",
        "cli_jobs_err": "--jobs 必须 >= 1",
        "cli_no_tty": "TUI 需要终端。请加 --device 在终端直接构建。",
        "cli_building": "正在构建 {label}：{combo} -j{jobs}{extra}",
        "info_rom": "版本",
        "info_os": "系统",
        "info_cpu": "CPU",
        "info_ram": "内存",
        "info_disk": "磁盘",
    },
    "ja": {
        "term_too_small": "端末を大きくしてください（最小 60x16）",
        "device": "デバイス",
        "pick_device": "デバイスを選択",
        "jobs": "ジョブ数",
        "clean": "クリーンビルド",
        "clean_none": "しない",
        "clean_install": "installclean",
        "clean_full": "make clean",
        "build_now": "ビルド開始",
        "hint": "クリック  ·  Tab  ·  q",
        "on": "ON",
        "off": "OFF",
        "picker_title": "デバイス選択",
        "picker_hint": "項目をクリック、Esc で戻る",
        "picker_keys": "↑↓  ·  Enter",
        "building": "ビルド中",
        "done": "完了",
        "stopped": "停止しました",
        "build_ok": "ビルド完了",
        "build_fail": "ビルド失敗  exit={code}",
        "stop": "停止",
        "back": "戻る",
        "no_devices": "ビルド可能なデバイスがありません",
        "need_device": "先にデバイスを選択してください",
        "stopping": "前回のビルドを停止しています…",
        "stopped_build": "ビルドを停止しました",
        "tui_failed": "TUI を起動できません: {exc}。--device で端末からビルドしてください。",
        "cli_desc": "Miku UI ビルドヘルパー。引数なしで TUI、--device でこの端末からビルド。",
        "cli_device": "デバイス（例: zeekr / miku_zeekr）",
        "cli_gapps": "MIKU_GAPPS=true を export",
        "cli_jobs": "make -jN（既定: CPU 数）",
        "cli_ccache": "ccache を使う（既定でオン、--no-ccache でオフ）",
        "cli_clean": "ビルド前に make clean",
        "cli_installclean": "ビルド前に make installclean",
        "cli_release": "lunch の release（vendor/miku から、通常 cp2a）",
        "cli_variant": "lunch の variant（既定 userdebug）",
        "cli_jobs_err": "--jobs は 1 以上にしてください",
        "cli_no_tty": "TUI には端末が必要です。--device で直接ビルドしてください。",
        "cli_building": "{label} をビルドします: {combo} -j{jobs}{extra}",
        "info_rom": "ROM",
        "info_os": "OS",
        "info_cpu": "CPU",
        "info_ram": "メモリ",
        "info_disk": "ディスク",
    },
    "ru": {
        "term_too_small": "Увеличьте терминал (мин. 60x16)",
        "device": "Устройство",
        "pick_device": "Выберите устройство",
        "jobs": "Потоки",
        "clean": "Чистая сборка",
        "clean_none": "Нет",
        "clean_install": "installclean",
        "clean_full": "make clean",
        "build_now": "СБОРКА",
        "hint": "клик  ·  tab  ·  q",
        "on": "ВКЛ",
        "off": "ВЫКЛ",
        "picker_title": "Выбор устройства",
        "picker_hint": "Нажмите пункт, Esc — отмена",
        "picker_keys": "↑↓  ·  enter",
        "building": "Сборка",
        "done": "Готово",
        "stopped": "Остановлено",
        "build_ok": "Сборка завершена",
        "build_fail": "Ошибка сборки  exit={code}",
        "stop": "СТОП",
        "back": "НАЗАД",
        "no_devices": "Нет доступных устройств",
        "need_device": "Сначала выберите устройство",
        "stopping": "Останавливается предыдущая сборка…",
        "stopped_build": "Сборка остановлена",
        "tui_failed": "TUI не запустился: {exc}. Укажите --device для сборки в терминале.",
        "cli_desc": "Сборка Miku UI. Без аргументов — TUI; --device собирает в этом терминале.",
        "cli_device": "устройство, например zeekr / miku_zeekr",
        "cli_gapps": "export MIKU_GAPPS=true",
        "cli_jobs": "make -jN (по умолчанию: число CPU)",
        "cli_ccache": "включить ccache (по умолчанию да; --no-ccache выключает)",
        "cli_clean": "перед сборкой выполнить make clean",
        "cli_installclean": "перед сборкой выполнить make installclean",
        "cli_release": "lunch release (из vendor/miku, обычно cp2a)",
        "cli_variant": "lunch variant (по умолчанию userdebug)",
        "cli_jobs_err": "--jobs должен быть >= 1",
        "cli_no_tty": "TUI нужен терминал. Укажите --device для сборки без UI.",
        "cli_building": "Сборка {label}: {combo} -j{jobs}{extra}",
        "info_rom": "ROM",
        "info_os": "ОС",
        "info_cpu": "ЦП",
        "info_ram": "ОЗУ",
        "info_disk": "ДИСК",
    },
}


def detect_lang() -> str:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(var, "")
        if not raw:
            continue
        if _is_c(raw):
            return "en"
        break
    language = os.environ.get("LANGUAGE", "")
    if language:
        for part in language.split(":"):
            code = _normalize(part)
            if code:
                return code
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(var, "")
        if not raw or _is_c(raw):
            continue
        code = _normalize(raw)
        if code:
            return code
    return "en"


def t(lang: str, key: str, **kwargs: object) -> str:
    table = _STRINGS.get(lang) or _STRINGS["en"]
    text = table.get(key) or _STRINGS["en"].get(key, key)
    if kwargs:
        return text.format(**kwargs)
    return text


def _is_c(tag: str) -> bool:
    token = tag.strip().replace("-", "_").split(".")[0].lower()
    return token in ("c", "posix")


def _normalize(tag: str) -> str | None:
    token = tag.strip().replace("-", "_").split(".")[0].lower()
    if not token or _is_c(token):
        return None
    primary = token.split("_", 1)[0]
    if primary in LANGS:
        return primary
    return None
