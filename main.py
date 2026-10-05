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
import os
import shutil
import sys
from pathlib import Path

_TUI_DIR = Path(__file__).resolve().parent
if str(_TUI_DIR) not in sys.path:
    sys.path.insert(0, str(_TUI_DIR))

from builder import BuildConfig, run_cli  # noqa: E402
from devices import android_top_from, default_release, discover_products, resolve_device  # noqa: E402
from i18n import detect_lang, t  # noqa: E402
from tui import CLEAN_FULL, CLEAN_INSTALL, CLEAN_NONE, run_tui  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        top = android_top_from(_TUI_DIR)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    products = discover_products(top)
    release = args.release or default_release(top)
    clean = CLEAN_FULL if args.clean else CLEAN_INSTALL if args.installclean else CLEAN_NONE

    if args.device:
        try:
            product = resolve_device(products, args.device)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        jobs = args.jobs if args.jobs is not None else max(1, os.cpu_count() or 4)
        ccache = shutil.which("ccache") is not None if args.ccache is None else args.ccache
        config = BuildConfig(
            product=product,
            jobs=jobs,
            gapps=args.gapps,
            ccache=ccache,
            clean=clean,
            release=release,
            variant=args.variant or "userdebug",
        )
        extras = []
        if config.gapps:
            extras.append("MIKU_GAPPS=true")
        if config.ccache:
            extras.append("USE_CCACHE=1")
        if config.clean != CLEAN_NONE:
            extras.append(config.clean)
        extra = f" ({', '.join(extras)})" if extras else ""
        print(
            t(
                detect_lang(),
                "cli_building",
                label=product.label,
                combo=config.lunch_combo,
                jobs=config.jobs,
                extra=extra,
            )
        )
        return run_cli(top, config)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(t(detect_lang(), "cli_no_tty"), file=sys.stderr)
        return 2
    return run_tui(
        top=top,
        products=products,
        release=release,
        jobs=args.jobs,
        gapps=True if args.gapps else None,
        ccache=args.ccache,
        clean=clean,
        variant=args.variant,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    lang = detect_lang()
    parser = argparse.ArgumentParser(
        prog="build_miku.py",
        description=t(lang, "cli_desc"),
    )
    parser.add_argument("--device", "-d", help=t(lang, "cli_device"))
    parser.add_argument("--gapps", action="store_true", help=t(lang, "cli_gapps"))
    parser.add_argument("-j", "--jobs", type=int, metavar="N", help=t(lang, "cli_jobs"))
    parser.add_argument(
        "--ccache",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=t(lang, "cli_ccache"),
    )
    parser.add_argument("--clean", action="store_true", help=t(lang, "cli_clean"))
    parser.add_argument("--installclean", action="store_true", help=t(lang, "cli_installclean"))
    parser.add_argument("--release", help=t(lang, "cli_release"))
    parser.add_argument("--variant", default=None, help=t(lang, "cli_variant"))
    args = parser.parse_args(argv)
    if args.jobs is not None and args.jobs < 1:
        parser.error(t(lang, "cli_jobs_err"))
    return args


if __name__ == "__main__":
    raise SystemExit(main())
