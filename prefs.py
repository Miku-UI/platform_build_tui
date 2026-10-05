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

import json
import os
from dataclasses import dataclass
from pathlib import Path

from i18n import LANGS

TUI_DIR_NAME = ".miku-tui"
PREFS_NAME = "prefs.json"
_VARIANTS = frozenset({"user", "userdebug", "eng"})
_VOICES = frozenset({"moe", "pro"})


@dataclass(frozen=True)
class Prefs:
    device: str | None = None
    variant: str | None = None
    jobs: int | None = None
    gapps: bool | None = None
    ccache: bool | None = None
    lang: str | None = None
    voice: str | None = None


def tui_dir(top: Path, *, create: bool = False) -> Path:
    path = top / TUI_DIR_NAME
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    return path


def prefs_path(top: Path) -> Path:
    return tui_dir(top) / PREFS_NAME


def load_prefs(top: Path) -> Prefs:
    path = prefs_path(top)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return Prefs()
    if not isinstance(raw, dict):
        return Prefs()
    device = raw.get("device")
    variant = raw.get("variant")
    jobs = raw.get("jobs")
    gapps = raw.get("gapps")
    ccache = raw.get("ccache")
    lang = raw.get("lang")
    voice = raw.get("voice")
    return Prefs(
        device=device.strip() if isinstance(device, str) and device.strip() else None,
        variant=variant if variant in _VARIANTS else None,
        jobs=jobs if isinstance(jobs, int) and not isinstance(jobs, bool) and 1 <= jobs <= 256 else None,
        gapps=gapps if isinstance(gapps, bool) else None,
        ccache=ccache if isinstance(ccache, bool) else None,
        lang=lang if lang in LANGS else None,
        voice=voice if voice in _VOICES else None,
    )


def save_prefs(top: Path, prefs: Prefs) -> None:
    data: dict[str, object] = {}
    if prefs.device:
        data["device"] = prefs.device
    if prefs.variant in _VARIANTS:
        data["variant"] = prefs.variant
    if prefs.jobs is not None:
        data["jobs"] = max(1, min(256, int(prefs.jobs)))
    if isinstance(prefs.gapps, bool):
        data["gapps"] = prefs.gapps
    if isinstance(prefs.ccache, bool):
        data["ccache"] = prefs.ccache
    if prefs.lang in LANGS:
        data["lang"] = prefs.lang
    if prefs.voice in _VOICES:
        data["voice"] = prefs.voice
    tui_dir(top, create=True)
    path = prefs_path(top)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
