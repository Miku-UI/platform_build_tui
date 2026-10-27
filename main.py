#!/usr/bin/env python3
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

import argparse
import sys
from pathlib import Path

_TUI_DIR = Path(__file__).resolve().parent
if str(_TUI_DIR) not in sys.path:
    sys.path.insert(0, str(_TUI_DIR))

from core.builder import (  # noqa: E402
    ENV_VARS_MAX,
    MAKE_TARGET,
    MEM_LIMIT_KINDS,
    MEM_LIMIT_OFF,
    BuildConfig,
    make_upgrade_build_number,
    mem_limit_tag,
    run_cli,
    sanitize_env_name,
    sanitize_env_value,
    sanitize_make_targets,
)
from core.devices import android_top_from, default_release, discover_products, resolve_device  # noqa: E402
from core.patchset import analyze, load_patchsets  # noqa: E402
from ui.i18n import detect_lang, t  # noqa: E402
from tui import CLEAN_FULL, CLEAN_INSTALL, CLEAN_NONE, run_tui  # noqa: E402

_VARIANTS = ("user", "userdebug", "eng")
_JOBS_MAX = 256


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    lang = detect_lang()
    if _want_cli(args):
        return _run_cli(args, lang)
    try:
        top = android_top_from(_TUI_DIR)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(t(lang, "cli_no_tty"), file=sys.stderr)
        return 2
    return run_tui(
        top=top,
        products=discover_products(top),
        release=args.release or default_release(top),
        jobs=args.jobs,
        gapps=True if args.gapps else None,
        ccache=True if args.ccache else None,
        clean=args.clean if args.clean is not None else CLEAN_NONE,
        variant=args.variant,
    )


def _want_cli(args: argparse.Namespace) -> bool:
    return bool(
        args.device
        or args.target is not None
        or args.keep_going
        or args.mem_limit is not None
        or args.mark_upgrade
        or args.env
        or args.patchset
    )


def _run_cli(args: argparse.Namespace, lang: str) -> int:
    missing = _missing_flags(args)
    if missing:
        print(t(lang, "cli_required", flags=", ".join(missing)), file=sys.stderr)
        return 2
    target = sanitize_make_targets(args.target)
    if not target:
        print(t(lang, "cli_target_err"), file=sys.stderr)
        return 2
    env_pairs, env_err = _env_pairs(args.env or [])
    if env_err:
        print(t(lang, "cli_env_err", item=env_err), file=sys.stderr)
        return 2
    names, dup = _patch_names(args.patchset or [])
    if dup:
        print(t(lang, "cli_patchset_dup", name=dup), file=sys.stderr)
        return 2
    try:
        top = android_top_from(_TUI_DIR)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        product = resolve_device(discover_products(top), args.device)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if names:
        known = [item.folder for item in load_patchsets(top)]
        unknown = [name for name in names if name not in known]
        if unknown:
            print(
                t(
                    lang,
                    "cli_patchset_unknown",
                    name=", ".join(unknown),
                    known=", ".join(known) or "-",
                ),
                file=sys.stderr,
            )
            return 2
        plan, blocked = _patch_plan(top, names)
        if blocked:
            _print_conflicts(lang, blocked)
            return 2
    else:
        plan = ()
    mark_upgrade = bool(args.mark_upgrade)
    config = BuildConfig(
        product=product,
        jobs=args.jobs,
        gapps=bool(args.gapps),
        ccache=bool(args.ccache),
        keep_going=bool(args.keep_going),
        clean=args.clean if args.clean is not None else CLEAN_NONE,
        release=args.release,
        variant=args.variant,
        make_target=target,
        show_sha256=target == MAKE_TARGET and not product.is_gsi,
        mem_limit=args.mem_limit if args.mem_limit is not None else MEM_LIMIT_OFF,
        env_vars=env_pairs,
        mark_upgrade=mark_upgrade,
        build_number=make_upgrade_build_number() if mark_upgrade else "",
        lunch=True,
        patch_plan=plan,
    )
    print(
        t(
            lang,
            "cli_building",
            label=product.label,
            combo=config.lunch_combo,
            target=config.make_target,
            jobs=config.jobs,
            extra=_summary_extra(config, names),
        )
    )
    try:
        return run_cli(top, config)
    except FileNotFoundError as exc:
        key = _missing_tool_key(exc)
        if not key:
            raise
        print(t(lang, key), file=sys.stderr)
        return 2


def _missing_flags(args: argparse.Namespace) -> list[str]:
    missing: list[str] = []
    if not args.device:
        missing.append("--device")
    if not args.release:
        missing.append("--release")
    if args.variant is None:
        missing.append("--variant")
    if args.target is None:
        missing.append("--target")
    if args.jobs is None:
        missing.append("-j/--jobs")
    return missing


def _env_pairs(raw: list[str]) -> tuple[tuple[tuple[str, str], ...], str]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if "=" not in item:
            return (), item
        name, value = item.split("=", 1)
        key = sanitize_env_name(name)
        val = sanitize_env_value(value)
        if not key or val is None or key in seen or len(out) >= ENV_VARS_MAX:
            return (), item
        seen.add(key)
        out.append((key, val))
    return tuple(out), ""


def _patch_names(raw: list[str]) -> tuple[tuple[str, ...], str]:
    seen: set[str] = set()
    for name in raw:
        if name in seen:
            return (), name
        seen.add(name)
    return tuple(sorted(seen)), ""


def _patch_plan(top: Path, names: tuple[str, ...]):
    views = analyze(top, names)
    blocked = [view for view in views if view.enabled and view.conflicts]
    if blocked:
        return (), blocked
    steps = [
        (patch.project, str(patch.path))
        for view in views
        if view.enabled
        for patch in view.patchset.patches
    ]
    return tuple(steps), []


def _print_conflicts(lang: str, views) -> None:
    print(t(lang, "patchset_conflict_body"), file=sys.stderr)
    for view in views:
        for conflict in view.conflicts:
            if conflict.others:
                name = ", ".join(conflict.others)
            else:
                name = t(lang, "patchset_source")
            files = "\n".join(conflict.files)
            print(
                f"{view.patchset.folder}: " + t(lang, "patchset_conflict_with", name=name, files=files),
                file=sys.stderr,
            )


def _summary_extra(config: BuildConfig, patch_names: tuple[str, ...]) -> str:
    parts = [
        "gapps=on" if config.gapps else "gapps=off",
        "ccache=on" if config.ccache else "ccache=off",
        f"clean={config.clean}",
        "keep-going=on" if config.keep_going else "keep-going=off",
        f"upgrade={config.build_number}" if config.mark_upgrade else "upgrade=off",
        mem_limit_tag(config.mem_limit) or "mem=off",
        "patchset=" + ",".join(patch_names) if patch_names else "patchset=none",
    ]
    if config.env_vars:
        parts.extend(f"{name}={value}" for name, value in config.env_vars)
    else:
        parts.append("env=none")
    return "  " + " ".join(parts)


def _missing_tool_key(exc: FileNotFoundError) -> str:
    missing = exc.filename or (exc.args[0] if exc.args else "")
    return {
        "unshare": "patchset_no_unshare",
        "/dev/shm": "patchset_no_shm",
        "systemd-run": "mem_limit_no_systemd",
    }.get(str(missing), "")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    lang = detect_lang()
    parser = argparse.ArgumentParser(
        prog="build_miku.py",
        description=t(lang, "cli_desc"),
    )
    required = parser.add_argument_group(t(lang, "cli_group"))
    required.add_argument("--device", "-d", help=t(lang, "cli_device"))
    required.add_argument("--release", help=t(lang, "cli_release"))
    required.add_argument("--variant", choices=_VARIANTS, default=None, help=t(lang, "cli_variant"))
    required.add_argument("--target", help=t(lang, "cli_target"))
    required.add_argument("-j", "--jobs", type=int, metavar="N", help=t(lang, "cli_jobs"))
    optional = parser.add_argument_group(t(lang, "cli_group_opt"))
    optional.add_argument("--gapps", action="store_true", help=t(lang, "cli_gapps"))
    optional.add_argument("--ccache", action="store_true", help=t(lang, "cli_ccache"))
    optional.add_argument("--keep-going", action="store_true", help=t(lang, "cli_keep_going"))
    optional.add_argument("--mark-upgrade", action="store_true", help=t(lang, "cli_mark_upgrade"))
    optional.add_argument(
        "--clean",
        choices=(CLEAN_NONE, CLEAN_INSTALL, CLEAN_FULL),
        default=None,
        help=t(lang, "cli_clean"),
    )
    optional.add_argument(
        "--mem-limit",
        choices=MEM_LIMIT_KINDS,
        default=None,
        help=t(lang, "cli_mem_limit"),
    )
    repeat = parser.add_argument_group(t(lang, "cli_group_repeat"))
    repeat.add_argument("--env", action="append", default=None, metavar="NAME=VALUE", help=t(lang, "cli_env"))
    repeat.add_argument(
        "--patchset",
        action="append",
        default=None,
        metavar="FOLDER",
        help=t(lang, "cli_patchset"),
    )
    args = parser.parse_args(argv)
    if args.jobs is not None and not 1 <= args.jobs <= _JOBS_MAX:
        parser.error(t(lang, "cli_jobs_err"))
    return args


if __name__ == "__main__":
    raise SystemExit(main())
