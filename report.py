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
import re
from dataclasses import dataclass
from pathlib import Path

from devices import Product

STATS_NAME = ".miku-tui-stats.json"

# Display buckets for timing / fail-stage labels. phase_config folds into soong.
STAGE_PREPARE = "prepare"
STAGE_CLEAN = "clean"
STAGE_SOONG = "soong"
STAGE_KATI = "kati"
STAGE_NINJA = "ninja"
STAGE_PACKAGE = "package"
STAGE_TOTAL = "total"

_FAILED_RE = re.compile(r"^FAILED:\s+(.*)$")
_PROG_PREFIX = re.compile(r"^\[(?:[^\]]*?\s)?\d+/\d+(?:[^\]]*)\]\s*")
_DIAG_RE = re.compile(r"^\S.*?:\d+(?::\d+)?: (?:error|warning|note):")
_CARET_RE = re.compile(r"^[ \t]*[~^]+[~^ \t]*$")
_SKIP_HEADS = (
    "Outputs:",
    "Error:",
    "Command:",
    "Output:",
    "stderr:",
)
_AOSP_TESTKEY = re.compile(
    r"(?:^|/)(?:build/make/target/product/security/testkey|build/target/product/security/testkey)$"
)


@dataclass(frozen=True)
class Glyph:
    ch: str
    fg: int = -1
    bold: bool = False
    dim: bool = False
    underline: bool = False


@dataclass
class FailItem:
    module: str | None
    rows: list[list[Glyph]]


@dataclass
class Timing:
    key: str
    seconds: float


@dataclass
class BuildReport:
    ok: bool
    version: str
    targets: int | None
    targets_delta: int | None
    timings: list[Timing]
    total: float
    device: str
    model: str
    gapps: bool
    artifact_rel: str | None
    artifact_size: int | None
    size_delta: int | None
    sha256: str | None
    signed: bool | None
    key_rel: str | None
    fail_stage: str | None
    failures: list[FailItem]
    artifact_abs: Path | None = None


def product_out(top: Path, product: Product) -> Path:
    return top / "out" / "target" / "product" / product.product_device


def relpath(path: Path, top: Path) -> str:
    try:
        return str(path.resolve().relative_to(top.resolve()))
    except ValueError:
        return str(path)


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = int(seconds)
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    if hh:
        return f"{hh}h {mm:02d}m {ss:02d}s"
    return f"{mm}m {ss:02d}s"


def fmt_mb(size: int) -> str:
    return f"{size / (1024 * 1024):.2f} MB"


def fmt_delta(delta: int) -> str:
    if delta > 0:
        return f"+{delta}"
    return str(delta)


def fmt_size_delta(delta: int) -> str:
    mb = delta / (1024 * 1024)
    if mb > 0:
        return f"+{mb:.2f} MB"
    if mb < 0:
        return f"{mb:.2f} MB"
    return "0.00 MB"


def stage_from_phase(key: str) -> str:
    if key in ("phase_setup", "phase_lunch"):
        return STAGE_PREPARE
    if key == "phase_clean":
        return STAGE_CLEAN
    if key in ("phase_config", "phase_soong"):
        return STAGE_SOONG
    if key == "phase_kati":
        return STAGE_KATI
    if key == "phase_ninja":
        return STAGE_NINJA
    if key == "phase_package":
        return STAGE_PACKAGE
    return STAGE_PREPARE


def timings_from_entered(
    entered: dict[str, float], started: float, finished: float
) -> list[Timing]:
    def first(*keys: str) -> float | None:
        times = [entered[k] for k in keys if k in entered]
        return min(times) if times else None

    def later(*keys: str) -> float:
        t = first(*keys)
        return t if t is not None else finished

    out: list[Timing] = []
    lunch_end = later(
        "phase_clean",
        "phase_config",
        "phase_soong",
        "phase_kati",
        "phase_ninja",
        "phase_package",
    )
    out.append(Timing(STAGE_PREPARE, max(0.0, lunch_end - started)))
    if "phase_clean" in entered:
        out.append(
            Timing(
                STAGE_CLEAN,
                max(
                    0.0,
                    later(
                        "phase_config",
                        "phase_soong",
                        "phase_kati",
                        "phase_ninja",
                        "phase_package",
                    )
                    - entered["phase_clean"],
                ),
            )
        )
    analyze_start = first("phase_config", "phase_soong")
    if analyze_start is not None:
        out.append(
            Timing(
                STAGE_SOONG,
                max(
                    0.0,
                    later("phase_kati", "phase_ninja", "phase_package") - analyze_start,
                ),
            )
        )
    if "phase_kati" in entered:
        out.append(
            Timing(
                STAGE_KATI,
                max(0.0, later("phase_ninja", "phase_package") - entered["phase_kati"]),
            )
        )
    if "phase_ninja" in entered:
        out.append(
            Timing(
                STAGE_NINJA,
                max(0.0, later("phase_package") - entered["phase_ninja"]),
            )
        )
    if "phase_package" in entered:
        out.append(Timing(STAGE_PACKAGE, max(0.0, finished - entered["phase_package"])))
    return out


def is_aosp_testkey(cert: str) -> bool:
    n = cert.replace("\\", "/").strip()
    if not n:
        return True
    return bool(_AOSP_TESTKEY.search(n.rstrip("/")))


def read_certificate(out_dir: Path) -> str:
    info = out_dir / "misc_info.txt"
    try:
        text = info.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for raw in text.splitlines():
        if raw.startswith("default_system_dev_certificate="):
            return raw.split("=", 1)[1].strip()
    return ""


def load_stats(out_dir: Path) -> dict[str, int]:
    path = out_dir / STATS_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key in ("targets", "size_bytes"):
        val = raw.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            out[key] = val
    return out


def save_stats(out_dir: Path, stats: dict[str, int]) -> None:
    path = out_dir / STATS_NAME
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(stats, indent=2, sort_keys=True) + "\n"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def find_artifact(out_dir: Path, hinted: str | None, started: float) -> Path | None:
    if hinted:
        cand = Path(hinted)
        if cand.is_file():
            return cand
    newest: Path | None = None
    newest_mtime = started - 1
    try:
        zips = list(out_dir.glob("MikuUI-*.zip"))
    except OSError:
        zips = []
    for path in zips:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= started and mtime >= newest_mtime:
            newest = path
            newest_mtime = mtime
    return newest


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def read_sha256sum(artifact: Path) -> str | None:
    path = artifact.with_name(artifact.name + ".sha256sum")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parts = text.split()
    token = parts[0] if parts else ""
    if _SHA256_RE.match(token):
        return token.lower()
    return None


def glyphs_from_text(text: str) -> list[Glyph]:
    return [Glyph(ch) for ch in text]


def parse_failures(rows: list[list[Glyph]]) -> list[FailItem]:
    items: list[FailItem] = []
    i = 0
    n = len(rows)
    while i < n:
        match = _FAILED_RE.match(_row_plain(rows[i]))
        if match:
            target = match.group(1).strip()
            if target.startswith("ninja:"):
                i += 1
                continue
            body, j = _collect_diag(rows, i + 1)
            items.append(FailItem(_module_from_target(target), body or [glyphs_from_text(target)]))
            i = j
            continue
        i += 1
    if items:
        return items
    body, _ = _collect_diag(rows, 0, until_failed=False)
    if body:
        items.append(FailItem(None, body))
    return items


def _collect_diag(
    rows: list[list[Glyph]], start: int, *, until_failed: bool = True
) -> tuple[list[list[Glyph]], int]:
    body: list[list[Glyph]] = []
    skipping = False
    j = start
    n = len(rows)
    while j < n:
        stripped = _strip_prog_row(rows[j])
        s = _row_plain(stripped)
        if until_failed and s.startswith("FAILED:"):
            break
        if s.startswith("ninja: build stopped") or s.startswith("error: ninja:"):
            break
        if "Build Failure:" in s or "steps failed:" in s:
            break
        if skipping:
            if s.startswith("Output:") or s.startswith("stderr:") or _is_diag_start(s):
                skipping = False
            else:
                j += 1
                continue
        if any(s.startswith(head) for head in _SKIP_HEADS):
            if s.startswith("Command:") or s.startswith("Outputs:"):
                skipping = True
            j += 1
            continue
        if _is_diag_start(s) or (body and _is_diag_cont(s)):
            body.append(stripped)
            if len(body) >= 120:
                j += 1
                break
        j += 1
    while body and not _row_plain(body[-1]):
        body.pop()
    return body, j


def _row_plain(row: list[Glyph]) -> str:
    return "".join(g.ch for g in row).rstrip()


def _strip_prog_row(row: list[Glyph]) -> list[Glyph]:
    text = "".join(g.ch for g in row)
    match = _PROG_PREFIX.match(text)
    if match is None:
        return row
    return row[match.end() :]


def _is_diag_start(line: str) -> bool:
    if _DIAG_RE.match(line):
        return True
    return line.startswith("error:") and "ninja:" not in line[:24]


def _is_diag_cont(line: str) -> bool:
    if not line:
        return True
    if _CARET_RE.match(line):
        return True
    if line[0] in " \t":
        return True
    if line.lstrip().startswith("|") or re.match(r"^\d+\s+\|", line):
        return True
    return False


def _module_from_target(target: str) -> str:
    token = target.split()[0] if target else target
    if ":" in token:
        return token.rsplit(":", 1)[-1] or token
    name = Path(token).name
    return name or token


def bar_widths(durations: list[float], width: int, min_cells: int = 1) -> list[int]:
    width = max(0, int(width))
    n = len(durations)
    if width == 0 or n == 0:
        return [0] * n
    pos = [i for i, d in enumerate(durations) if d > 0]
    if not pos:
        return [0] * n
    floor = max(1, min_cells)
    if floor * len(pos) > width:
        floor = max(1, width // len(pos))
    total = sum(durations[i] for i in pos)
    raw = [0.0] * n
    for i in pos:
        raw[i] = durations[i] / total * width
    cells = [int(x) for x in raw]
    remain = width - sum(cells)
    order = sorted(pos, key=lambda i: raw[i] - cells[i], reverse=True)
    for i in order[: max(0, remain)]:
        cells[i] += 1
    for i in pos:
        if cells[i] < floor:
            cells[i] = floor
    extra = sum(cells) - width
    donors = sorted(pos, key=lambda i: cells[i], reverse=True)
    di = 0
    while extra > 0 and di < len(donors) * (width + 1):
        i = donors[di % len(donors)]
        if cells[i] > floor:
            cells[i] -= 1
            extra -= 1
        di += 1
    if extra < 0:
        frac = sorted(pos, key=lambda i: raw[i] - int(raw[i]), reverse=True)
        for i in frac[: -extra]:
            cells[i] += 1
    return cells


def build_report(
    *,
    top: Path,
    product: Product,
    version: str,
    ok: bool,
    started: float,
    finished: float,
    entered: dict[str, float],
    phase_key: str,
    ninja_total: int | None,
    ninja_done: int | None,
    package_hint: str | None,
    log_rows: list[list[Glyph]],
    gapps: bool,
) -> BuildReport:
    finished = max(finished, started)
    out_dir = product_out(top, product)
    prev = load_stats(out_dir)
    timings = timings_from_entered(entered, started, finished)
    total = finished - started
    if ok:
        targets = ninja_total if ninja_total and ninja_total > 0 else None
    else:
        targets = ninja_done if ninja_done is not None else None
    targets_delta = None
    if targets is not None and "targets" in prev:
        targets_delta = targets - prev["targets"]

    artifact_abs = None
    artifact_rel = None
    artifact_size = None
    size_delta = None
    sha256: str | None = None
    signed: bool | None = None
    key_rel = None
    if ok:
        artifact_abs = find_artifact(out_dir, package_hint, started)
        if artifact_abs is not None:
            artifact_rel = relpath(artifact_abs, top)
            try:
                artifact_size = artifact_abs.stat().st_size
            except OSError:
                artifact_size = None
            if artifact_size is not None and "size_bytes" in prev:
                size_delta = artifact_size - prev["size_bytes"]
        sha256 = read_sha256sum(artifact_abs) if artifact_abs is not None else None
        cert = read_certificate(out_dir)
        if cert:
            signed = not is_aosp_testkey(cert)
            if signed:
                key_path = Path(cert)
                if not key_path.is_absolute():
                    key_path = top / key_path
                key_rel = relpath(key_path, top)
        else:
            signed = False

    failures: list[FailItem] = []
    fail_stage = None
    if not ok:
        fail_stage = stage_from_phase(phase_key)
        failures = parse_failures(log_rows)

    new_stats: dict[str, int] = dict(prev)
    if targets is not None:
        new_stats["targets"] = targets
    if ok and artifact_size is not None:
        new_stats["size_bytes"] = artifact_size
    if new_stats != prev:
        save_stats(out_dir, new_stats)

    return BuildReport(
        ok=ok,
        version=version,
        targets=targets,
        targets_delta=targets_delta,
        timings=timings,
        total=total,
        device=product.product_device,
        model=product.product_model,
        gapps=gapps,
        artifact_rel=artifact_rel,
        artifact_size=artifact_size,
        size_delta=size_delta,
        sha256=sha256,
        signed=signed,
        key_rel=key_rel,
        fail_stage=fail_stage,
        failures=failures,
        artifact_abs=artifact_abs,
    )
