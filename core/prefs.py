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

from core.builder import MEM_LIMIT_KINDS, sanitize_env_vars
from ui.i18n import LANGS

TUI_DIR_NAME = ".miku-tui"
PREFS_NAME = "prefs.json"
_VARIANTS = frozenset({"user", "userdebug", "eng"})
_VOICES = frozenset({"moe", "pro"})
_GIT_TRANSPORTS = frozenset({"https", "ssh"})


@dataclass(frozen=True)
class Prefs:
    device: str | None = None
    variant: str | None = None
    jobs: int | None = None
    gapps: bool | None = None
    ccache: bool | None = None
    keep_going: bool | None = None
    lang: str | None = None
    voice: str | None = None
    git_transport: str | None = None
    check_jobs: int | None = None
    mem_limit: str | None = None
    env_vars: tuple[tuple[str, str], ...] = ()
    mark_upgrade: bool | None = None
    apply_patchsets: bool | None = None
    patchset_enabled: tuple[str, ...] = ()
    patchset_known: tuple[str, ...] = ()


def _patchset_names(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and "/" not in item and "\\" not in item:
            names.append(item.strip())
    return names


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
    keep_going = raw.get("keep_going")
    lang = raw.get("lang")
    voice = raw.get("voice")
    git_transport = raw.get("git_transport")
    check_jobs = raw.get("check_jobs")
    mem_limit = raw.get("mem_limit")
    mark_upgrade = raw.get("mark_upgrade")
    apply_patchsets = raw.get("apply_patchsets")
    enabled = _patchset_names(raw.get("patchset_enabled"))
    known = _patchset_names(raw.get("patchset_known"))
    return Prefs(
        device=device.strip() if isinstance(device, str) and device.strip() else None,
        variant=variant if variant in _VARIANTS else None,
        jobs=jobs if isinstance(jobs, int) and not isinstance(jobs, bool) and 1 <= jobs <= 256 else None,
        gapps=gapps if isinstance(gapps, bool) else None,
        ccache=ccache if isinstance(ccache, bool) else None,
        keep_going=keep_going if isinstance(keep_going, bool) else None,
        lang=lang if lang in LANGS else None,
        voice=voice if voice in _VOICES else None,
        git_transport=git_transport if git_transport in _GIT_TRANSPORTS else None,
        check_jobs=check_jobs if isinstance(check_jobs, int) and not isinstance(check_jobs, bool) and 1 <= check_jobs <= 256 else None,
        mem_limit=mem_limit if mem_limit in MEM_LIMIT_KINDS else None,
        env_vars=sanitize_env_vars(raw.get("env_vars")),
        mark_upgrade=mark_upgrade if isinstance(mark_upgrade, bool) else None,
        apply_patchsets=apply_patchsets if isinstance(apply_patchsets, bool) else None,
        patchset_enabled=tuple(dict.fromkeys(enabled)),
        patchset_known=tuple(dict.fromkeys(known)),
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
    if isinstance(prefs.keep_going, bool):
        data["keep_going"] = prefs.keep_going
    if prefs.lang in LANGS:
        data["lang"] = prefs.lang
    if prefs.voice in _VOICES:
        data["voice"] = prefs.voice
    if prefs.git_transport in _GIT_TRANSPORTS:
        data["git_transport"] = prefs.git_transport
    if prefs.check_jobs is not None:
        data["check_jobs"] = max(1, min(256, int(prefs.check_jobs)))
    if prefs.mem_limit in MEM_LIMIT_KINDS:
        data["mem_limit"] = prefs.mem_limit
    if prefs.env_vars:
        data["env_vars"] = [{"name": name, "value": value} for name, value in prefs.env_vars]
    if isinstance(prefs.mark_upgrade, bool):
        data["mark_upgrade"] = prefs.mark_upgrade
    if isinstance(prefs.apply_patchsets, bool):
        data["apply_patchsets"] = prefs.apply_patchsets
    if prefs.patchset_enabled:
        data["patchset_enabled"] = list(prefs.patchset_enabled)
    if prefs.patchset_known:
        data["patchset_known"] = list(prefs.patchset_known)
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
