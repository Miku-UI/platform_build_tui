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

"""Patchsets under vendor/patchset, conflict checks, and a build-only overlay."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PATCHSET_DIR = Path("vendor") / "patchset"
_FOLDER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_DIFF_GIT = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_HUNK_PATH = re.compile(r"^(?:---|\+\+\+) (?:[ab]/)?(\S+)", re.M)
_FAIL_LINE = re.compile(r"^error: patch failed: (.+):\d+\s*$", re.M)
_NO_APPLY = re.compile(r"^error: (.+): patch does not apply\s*$", re.M)
_MISSING = re.compile(r"^error: (.+): No such file or directory\s*$", re.M)
_MS_BIND = 4096


@dataclass(frozen=True)
class PatchFile:
    project: str
    path: Path


@dataclass(frozen=True)
class PatchSet:
    folder: str
    title: dict[str, str]
    description: dict[str, str]
    author: str
    author_url: str
    recommend: bool
    patches: tuple[PatchFile, ...]


@dataclass(frozen=True)
class Conflict:
    """others is empty when the patchset does not apply onto the source tree."""

    others: tuple[str, ...]
    files: tuple[str, ...]


@dataclass(frozen=True)
class SetView:
    patchset: PatchSet
    enabled: bool
    conflicts: tuple[Conflict, ...]


def localized(table: dict[str, str], lang: str) -> str:
    for key in (lang, "zh", "en"):
        value = table.get(key, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in table.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def load_patchsets(top: Path) -> list[PatchSet]:
    root = top / PATCHSET_DIR
    if not root.is_dir():
        return []
    found: list[PatchSet] = []
    for child in root.iterdir():
        if not child.is_dir() or child.is_symlink() or not _FOLDER_NAME.match(child.name):
            continue
        parsed = _load_one(child)
        if parsed is not None:
            found.append(parsed)
    found.sort(key=lambda item: item.folder)
    return found


def analyze(
    top: Path,
    enabled: tuple[str, ...] | list[str],
    progress: Callable[[int, int], None] | None = None,
) -> list[SetView]:
    sets = load_patchsets(top)
    on = tuple(name for name in sorted(set(enabled)) if isinstance(name, str))
    return _views(sets, on, _compute(top, sets, on, progress))


def apply_defaults(
    top: Path,
    enabled: tuple[str, ...] | list[str],
    known: tuple[str, ...] | list[str],
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    """Apply first-seen defaults. Recommended sets start enabled."""
    return _settle(load_patchsets(top), enabled, known)


def prepare(top: Path, enabled: tuple[str, ...] | list[str]) -> tuple[tuple[tuple[str, str], ...], bool]:
    """Return (apply plan, blocked). An empty plan applies nothing."""
    views = analyze(top, enabled)
    if any(view.enabled and view.conflicts for view in views):
        return (), True
    steps: list[tuple[str, str]] = []
    for view in views:
        if not view.enabled:
            continue
        for patch in view.patchset.patches:
            steps.append((patch.project, str(patch.path)))
    return tuple(steps), False


def wrap_overlay(top: Path, plan: tuple[tuple[str, str], ...], inner: list[str]) -> tuple[list[str], Path]:
    if not plan:
        raise ValueError("empty patch plan")
    if shutil.which("unshare") is None:
        raise FileNotFoundError("unshare")
    shm = Path("/dev/shm")
    if not shm.is_dir():
        raise FileNotFoundError("/dev/shm")
    run = Path(tempfile.mkdtemp(prefix="miku-patchset-", dir=shm))
    for name in ("upper", "work", "lower"):
        (run / name).mkdir(mode=0o700)
    plan_path = run / "plan.json"
    plan_path.write_text(
        json.dumps([{"project": project, "patch": patch} for project, patch in plan], indent=2) + "\n",
        encoding="utf-8",
    )
    argv = [
        "unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "--propagation",
        "private",
        sys.executable,
        str(Path(__file__).resolve()),
        "--overlay",
        "--top",
        str(top),
        "--run",
        str(run),
        "--",
        *inner,
    ]
    return argv, run


def cleanup_run(path: Path | None) -> None:
    if path is None:
        return
    work = path / "work" / "work"
    try:
        if work.is_dir():
            work.chmod(0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _load_one(folder: Path) -> PatchSet | None:
    manifest_path = folder / "manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    title = _string_map(raw.get("title"))
    description = _string_map(raw.get("description"))
    if not title:
        return None
    author = raw.get("author")
    author_text = author.strip() if isinstance(author, str) else ""
    url = raw.get("author_url")
    url_text = url.strip() if isinstance(url, str) else ""
    if url_text and not url_text.startswith(("https://", "http://")):
        url_text = ""
    patches_raw = raw.get("patches")
    if not isinstance(patches_raw, list) or not patches_raw:
        return None
    patches: list[PatchFile] = []
    for item in patches_raw:
        if not isinstance(item, dict):
            return None
        file_name = item.get("file")
        project = item.get("project")
        if not isinstance(file_name, str) or not isinstance(project, str):
            return None
        rel_file = _safe_file(file_name)
        rel_project = _safe_project(project)
        if rel_file is None or rel_project is None:
            return None
        patch_path = folder / rel_file
        if not patch_path.is_file() or patch_path.is_symlink():
            return None
        patches.append(PatchFile(rel_project.as_posix(), patch_path.resolve()))
    return PatchSet(
        folder.name,
        title,
        description,
        author_text,
        url_text,
        raw.get("recommend") is True,
        tuple(patches),
    )


def _settle(
    sets: list[PatchSet],
    enabled: tuple[str, ...] | list[str],
    known: tuple[str, ...] | list[str],
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    on = {name for name in enabled if isinstance(name, str) and name.strip()}
    seen = {name for name in known if isinstance(name, str) and name.strip()}
    changed = False
    for item in sets:
        if item.folder in seen:
            continue
        seen.add(item.folder)
        changed = True
        if item.recommend:
            on.add(item.folder)
    return tuple(sorted(on)), tuple(sorted(seen)), changed


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, str) and item.strip():
            out[key] = item.strip()
    return out


def _safe_file(text: str) -> str | None:
    name = text.strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    return name


def _safe_project(text: str) -> Path | None:
    raw = text.strip()
    if not raw or raw.startswith("/") or "\\" in raw:
        return None
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def patch_paths(patch: Path) -> tuple[str, ...]:
    try:
        text = patch.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return ()
    found: list[str] = []

    def add(token: str) -> None:
        if token in ("/dev/null", "dev/null"):
            return
        rel = token[2:] if token.startswith(("a/", "b/")) else token
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            return
        if rel not in found:
            found.append(rel)

    for match in _DIFF_GIT.finditer(text):
        add(match.group(1))
        add(match.group(2))
    if not found:
        for match in _HUNK_PATH.finditer(text):
            add(match.group(1))
    return tuple(found)


def _views(sets: list[PatchSet], enabled: tuple[str, ...], conflicts: dict[str, tuple[Conflict, ...]]) -> list[SetView]:
    on = set(enabled)
    return [
        SetView(item, item.folder in on, conflicts.get(item.folder, ()))
        for item in sets
    ]


def _flat(sets: list[PatchSet]) -> tuple[PatchFile, ...]:
    return tuple(patch for item in sets for patch in item.patches)


def _blame_earlier(top: Path, earlier: list[PatchSet], item: PatchSet, stack_files: tuple[str, ...]) -> tuple[Conflict, ...]:
    blamed: list[Conflict] = []
    for other in earlier:
        ok, files = _apply(top, other.patches + item.patches)
        if not ok:
            blamed.append(Conflict((other.folder,), files))
    if blamed:
        return tuple(blamed)
    return (Conflict(tuple(other.folder for other in earlier), stack_files),)


def _conflicts_against(top: Path, included: list[PatchSet], item: PatchSet) -> tuple[Conflict, ...]:
    pos = next(index for index, entry in enumerate(included) if entry.folder == item.folder)
    earlier = included[:pos]
    if not earlier:
        return ()
    ok, files = _apply(top, _flat(included[: pos + 1]))
    if ok:
        return ()
    return _blame_earlier(top, earlier, item, files)


def _compute(
    top: Path,
    sets: list[PatchSet],
    enabled: tuple[str, ...],
    progress: Callable[[int, int], None] | None,
) -> dict[str, tuple[Conflict, ...]]:
    on = set(enabled)
    turned_on = [item for item in sets if item.folder in on]
    total = len(sets)
    out: dict[str, tuple[Conflict, ...]] = {}
    for done, item in enumerate(sets):
        if progress is not None:
            progress(done, total)
        alone_ok, alone_files = _apply(top, item.patches)
        if not alone_ok:
            out[item.folder] = (Conflict((), alone_files),)
            continue
        if item.folder not in on:
            out[item.folder] = ()
            continue
        out[item.folder] = _conflicts_against(top, turned_on, item)
    if progress is not None:
        progress(total, total)
    return out


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["LANGUAGE"] = "C"
    return env


def _apply(top: Path, patches: tuple[PatchFile, ...]) -> tuple[bool, tuple[str, ...]]:
    dest = Path(tempfile.mkdtemp(prefix="miku-ps-"))
    try:
        _materialize(top, dest, patches)
        for patch in patches:
            proc = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", str(patch.path)],
                cwd=dest / patch.project,
                capture_output=True,
                text=True,
                env=_git_env(),
            )
            if proc.returncode != 0:
                return False, _error_files(patch.project, proc.stderr)
        return True, ()
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def _materialize(top: Path, dest: Path, patches: tuple[PatchFile, ...]) -> None:
    seen: set[tuple[str, str]] = set()
    for patch in patches:
        project_dir = dest / patch.project
        project_dir.mkdir(parents=True, exist_ok=True)
        for rel in patch_paths(patch.path):
            key = (patch.project, rel)
            if key in seen:
                continue
            seen.add(key)
            src = top / patch.project / rel
            if not src.is_file():
                continue
            target = project_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


def _error_files(project: str, stderr: str) -> tuple[str, ...]:
    found: list[str] = []
    for pattern in (_FAIL_LINE, _NO_APPLY, _MISSING):
        for match in pattern.finditer(stderr):
            rel = match.group(1).strip()
            if rel.startswith(("a/", "b/")):
                rel = rel[2:]
            item = f"{project}/{rel}" if project else rel
            if item not in found:
                found.append(item)
    if found:
        return tuple(found)
    for line in stderr.splitlines():
        text = line.strip()
        if text:
            return (text,)
    return ()


def _overlay_main(argv: list[str]) -> int:
    if "--" not in argv:
        print("patchset: missing command", file=sys.stderr)
        return 1
    split = argv.index("--")
    options = argv[:split]
    command = argv[split + 1 :]
    if not command:
        print("patchset: missing command", file=sys.stderr)
        return 1
    try:
        top = Path(options[options.index("--top") + 1])
        run = Path(options[options.index("--run") + 1])
    except (ValueError, IndexError):
        print("patchset: overlay arguments are incomplete", file=sys.stderr)
        return 1
    try:
        plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("patchset: could not read the patch plan", file=sys.stderr)
        return 1
    if not isinstance(plan, list):
        print("patchset: patch plan is invalid", file=sys.stderr)
        return 1
    try:
        _mount_tree(top, run)
    except OSError as exc:
        print(f"patchset: overlay mount failed: {exc}", file=sys.stderr)
        return 1
    canary = run / "upper" / ".patchset-canary"
    try:
        canary.write_text("1", encoding="utf-8")
    except OSError as exc:
        print(f"patchset: {exc}", file=sys.stderr)
        return 1
    if not (top / ".patchset-canary").is_file():
        print("patchset: overlay did not cover the source tree", file=sys.stderr)
        return 1
    applied = 0
    for item in plan:
        if not isinstance(item, dict):
            print("patchset: patch plan is invalid", file=sys.stderr)
            return 1
        project = item.get("project")
        patch = item.get("patch")
        if not isinstance(project, str) or not isinstance(patch, str):
            print("patchset: patch plan is invalid", file=sys.stderr)
            return 1
        repo = top / project
        proc = subprocess.run(
            ["git", "-C", str(repo), "apply", "--whitespace=nowarn", patch],
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        if proc.returncode != 0:
            print(f"patchset: failed to apply {patch} onto {project}", file=sys.stderr)
            if proc.stderr:
                print(proc.stderr, file=sys.stderr, end="" if proc.stderr.endswith("\n") else "\n")
            return 1
        applied += 1
    print(f"patchset: applied {applied} patches", file=sys.stderr)
    try:
        os.chdir(top)
    except OSError as exc:
        print(f"patchset: {exc}", file=sys.stderr)
        return 1
    os.execvp(command[0], command)
    return 127


def _mount_tree(top: Path, run: Path) -> None:
    lower = run / "lower"
    upper = run / "upper"
    work = run / "work"
    out_lower = lower / "out"
    _mount(str(top), str(lower), None, _MS_BIND, None)
    out_lower.mkdir(exist_ok=True)
    # upper/work are on the tmpfs that holds the run directory.
    _mount(
        "overlay",
        str(top),
        "overlay",
        0,
        f"lowerdir={lower},upperdir={upper},workdir={work}",
    )
    # Keep build output on the real out/ after this mount namespace goes away.
    _mount(str(out_lower), str(top / "out"), None, _MS_BIND, None)


def _mount(source: str, target: str, fstype: str | None, flags: int, data: str | None) -> None:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_char_p,
    ]
    libc.mount.restype = ctypes.c_int
    result = libc.mount(
        source.encode(),
        target.encode(),
        None if fstype is None else fstype.encode(),
        ctypes.c_ulong(flags),
        None if data is None else data.encode(),
    )
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), target)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--overlay" in args:
        return _overlay_main(args)
    print("patchset: nothing to do", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
