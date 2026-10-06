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

import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

SCOPE_FORKS = "forks"
SCOPE_AOSP = "aosp"
SCOPE_ALL = "all"

STATUS_AHEAD = "ahead"
STATUS_BEHIND = "behind"
STATUS_DIVERGED = "diverged"

GIT_HTTPS = "https"
GIT_SSH = "ssh"

FORK_XMLS = ("miku.xml", "caf.xml", "diva.xml", "lineage.xml")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SCP_RE = re.compile(r"^git@([^:]+):(.+)$")
_SSH_RE = re.compile(r"^ssh://(?:([^@/]+)@)?([^/]+)/(.+)$")
_HTTP_RE = re.compile(r"^https?://([^/]+)/(.+)$")
CHECK_JOBS_DEFAULT = 8
_CHECK_WORKERS = CHECK_JOBS_DEFAULT
_GIT_TIMEOUT = 45


@dataclass(frozen=True)
class ManifestProject:
    path: str
    name: str
    remote: str
    revision: str
    url: str
    origin: str

    @property
    def fork(self) -> bool:
        return self.origin in FORK_XMLS


@dataclass(frozen=True)
class DiffRow:
    path: str
    status: str


class CommandSession:
    """PTY-backed subprocess, same shape as BuildSession for the log pane."""

    def __init__(
        self,
        top: Path,
        argv: list[str],
        on_data: Callable[[str], None] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.top = top
        self.argv = argv
        self.on_data = on_data
        self.env = env
        self.proc: subprocess.Popen[bytes] | None = None
        self.master_fd: int | None = None
        self._reader: threading.Thread | None = None
        self._returncode: int | None = None
        self.started_at = 0.0
        self.finished_at = 0.0
        self.stopped = False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def returncode(self) -> int | None:
        if self.proc is not None:
            code = self.proc.poll()
            if code is not None:
                self._returncode = code
        return self._returncode

    def start(self) -> None:
        env = os.environ.copy() if self.env is None else dict(self.env)
        env.setdefault("TERM", "xterm-256color")
        env["GIT_TERMINAL_PROMPT"] = "0"
        self.started_at = time.time()
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        packed = struct.pack("HHHH", 24, 80, 0, 0)
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, packed)
        except OSError:
            pass
        self.proc = subprocess.Popen(
            self.argv,
            cwd=self.top,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        self._reader = threading.Thread(target=self._read_pty, daemon=True)
        self._reader.start()

    def set_winsize(self, rows: int, cols: int) -> None:
        if self.master_fd is None:
            return
        packed = struct.pack("HHHH", max(2, rows), max(20, cols), 0, 0)
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)
        except OSError:
            pass

    def close(self) -> None:
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=0.2)

    def stop(self) -> None:
        self.stopped = True
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            self._returncode = self.proc.returncode
            self.finished_at = time.time()
            self.close()
            return
        try:
            pgid = os.getpgid(self.proc.pid)
        except ProcessLookupError:
            self._returncode = self.proc.poll()
            self.finished_at = time.time()
            self.close()
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            if _wait_exit(self.proc, 2.0):
                break
        else:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _wait_exit(self.proc, 1.0)
        self._returncode = self.proc.poll()
        if self._returncode is None:
            self._returncode = -signal.SIGKILL
        self.finished_at = time.time()
        self.close()

    def _read_pty(self) -> None:
        fd = self.master_fd
        if fd is None:
            return
        leftover = b""
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                if self.proc is not None and self.proc.poll() is not None:
                    try:
                        chunk = os.read(fd, 8192)
                    except OSError:
                        chunk = b""
                    leftover += chunk
                    break
                continue
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                break
            if not chunk:
                break
            leftover += chunk
            text, leftover = _decode_chunks(leftover)
            if text and self.on_data:
                self.on_data(text)
        if leftover and self.on_data:
            self.on_data(leftover.decode("utf-8", errors="replace"))
        if self.proc is not None and self.proc.poll() is not None:
            self._returncode = self.proc.returncode
            self.finished_at = time.time()


def repo_argv(top: Path) -> list[str]:
    bundled = top / ".repo" / "repo" / "repo"
    if os.access(bundled, os.X_OK):
        return [str(bundled)]
    found = shutil.which("repo")
    if found:
        return [found]
    if bundled.is_file():
        return [sys.executable, str(bundled)]
    raise FileNotFoundError("repo")


def sync_argv(top: Path, jobs: int, force_checkout: bool, fail_fast: bool) -> list[str]:
    argv = repo_argv(top) + ["sync", f"-j{max(1, int(jobs))}"]
    if force_checkout:
        argv.append("--force-checkout")
    if fail_fast:
        argv.append("--fail-fast")
    return argv


def local_paths(top: Path) -> list[str]:
    listed = top / ".repo" / "project.list"
    if not listed.is_file():
        return []
    paths: list[str] = []
    for line in listed.read_text(encoding="utf-8", errors="replace").splitlines():
        path = line.strip()
        if path:
            paths.append(path)
    return paths


def load_projects(top: Path) -> list[ManifestProject]:
    manifests = top / ".repo" / "manifests"
    root = top / ".repo" / "manifest.xml"
    if not root.is_file() or not manifests.is_dir():
        return []
    remotes: dict[str, tuple[str, str | None]] = {}
    default_remote = ""
    default_revision = ""
    by_path: dict[str, ManifestProject] = {}
    by_name: dict[str, str] = {}
    visited: set[Path] = set()

    def handle(path: Path) -> None:
        nonlocal default_remote, default_revision
        resolved = path.resolve()
        if resolved in visited or not path.is_file():
            return
        visited.add(resolved)
        try:
            tree = ET.parse(path)
        except ET.ParseError:
            return
        xml_origin = path.name
        for el in list(tree.getroot()):
            tag = _tag(el.tag)
            if tag == "remote":
                name = el.get("name") or ""
                if not name:
                    continue
                fetch = el.get("fetch") or remotes.get(name, ("", None))[0]
                revision = el.get("revision") or remotes.get(name, ("", None))[1]
                remotes[name] = (fetch, revision)
            elif tag == "default":
                if el.get("remote"):
                    default_remote = el.get("remote") or default_remote
                if el.get("revision"):
                    default_revision = el.get("revision") or default_revision
            elif tag == "include":
                name = el.get("name") or ""
                if name:
                    handle(_resolve_include(path, name, manifests))
            elif tag == "remove-project":
                name = el.get("name") or ""
                proj_path = el.get("path") or by_name.get(name, "")
                if proj_path and proj_path in by_path:
                    del by_path[proj_path]
                if name and name in by_name:
                    del by_name[name]
            elif tag == "project":
                name = el.get("name") or ""
                proj_path = el.get("path") or name
                if not proj_path:
                    continue
                remote = el.get("remote") or default_remote
                fetch, remote_rev = remotes.get(remote, ("", None))
                revision = el.get("revision") or remote_rev or default_revision
                url = _join_url(fetch, name)
                by_path[proj_path] = ManifestProject(
                    path=proj_path,
                    name=name,
                    remote=remote,
                    revision=revision,
                    url=url,
                    origin=xml_origin,
                )
                if name:
                    by_name[name] = proj_path

    handle(root)
    extra = top / ".repo" / "local_manifests"
    if extra.is_dir():
        for xml in sorted(extra.glob("*.xml")):
            handle(xml)
    return [by_path[key] for key in sorted(by_path)]


def filter_projects(projects: list[ManifestProject], scope: str, present: set[str]) -> list[ManifestProject]:
    out: list[ManifestProject] = []
    for proj in projects:
        if proj.path not in present:
            continue
        if scope == SCOPE_FORKS:
            if not proj.fork:
                continue
        elif scope == SCOPE_AOSP:
            if proj.fork or proj.origin != "default.xml":
                continue
        out.append(proj)
    return out


def classify_counts(behind: int, ahead: int) -> str | None:
    if behind <= 0 and ahead <= 0:
        return None
    if behind <= 0:
        return STATUS_AHEAD
    if ahead <= 0:
        return STATUS_BEHIND
    return STATUS_DIVERGED


def check_one(
    top: Path,
    proj: ManifestProject,
    *,
    fetch: bool = True,
    transport: str = GIT_HTTPS,
) -> DiffRow | None:
    work = top / proj.path
    if not (work / ".git").exists():
        return None
    local = _rev_parse(work, "HEAD")
    if not local:
        return None
    upstream = _upstream_sha(work, proj, fetch=fetch, transport=transport)
    if not upstream:
        return None
    if local == upstream:
        return None
    counts = _left_right(work, upstream, local)
    if counts is None:
        return None
    status = classify_counts(*counts)
    if status is None:
        return None
    return DiffRow(path=proj.path, status=status)


def check_projects(
    top: Path,
    scope: str,
    on_progress: Callable[[int, int, str], None] | None = None,
    stop: threading.Event | None = None,
    *,
    fetch: bool = True,
    transport: str = GIT_HTTPS,
    workers: int = CHECK_JOBS_DEFAULT,
) -> list[DiffRow]:
    present = set(local_paths(top))
    projects = filter_projects(load_projects(top), scope, present)
    total = len(projects)
    diffs: list[DiffRow] = []
    if total == 0:
        if on_progress:
            on_progress(0, 0, "")
        return diffs
    done = 0
    workers = max(1, min(256, int(workers)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(check_one, top, proj, fetch=fetch, transport=transport): proj
            for proj in projects
        }
        for fut in as_completed(futs):
            if stop is not None and stop.is_set():
                for pending in futs:
                    pending.cancel()
                break
            proj = futs[fut]
            row: DiffRow | None = None
            try:
                row = fut.result()
            except Exception:
                row = None
            done += 1
            if row is not None:
                diffs.append(row)
            if on_progress:
                on_progress(done, total, proj.path)
    diffs.sort(key=lambda row: row.path)
    return diffs


def _resolve_include(current: Path, name: str, manifests: Path) -> Path:
    rel = current.parent / name
    if rel.is_file():
        return rel
    alt = manifests / name
    if alt.is_file():
        return alt
    return rel


def _tag(name: str) -> str:
    if "}" in name:
        return name.rsplit("}", 1)[-1]
    return name


def _join_url(fetch: str, name: str) -> str:
    if not fetch:
        return name
    if not name:
        return fetch
    if fetch.endswith("/"):
        return fetch + name
    return fetch + "/" + name


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "true")
    return env


def _git(work: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(work), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace").strip()


def _rev_parse(work: Path, spec: str) -> str:
    code, out = _git(work, "rev-parse", "--verify", "-q", spec)
    if code == 0 and _SHA_RE.fullmatch(out):
        return out
    return ""


def _is_fixed_revision(revision: str) -> bool:
    if _SHA_RE.fullmatch(revision):
        return True
    if revision.startswith("refs/tags/"):
        return True
    return False


def _revision_specs(remote: str, revision: str) -> list[str]:
    if revision.startswith("refs/"):
        return [revision]
    if _SHA_RE.fullmatch(revision):
        return [revision]
    specs = [f"refs/remotes/{remote}/{revision}", f"refs/tags/{revision}"]
    if remote:
        specs.append(f"refs/remotes/origin/{revision}")
    return specs


def _resolve_local(work: Path, remote: str, revision: str) -> str:
    for spec in _revision_specs(remote, revision):
        sha = _rev_parse(work, spec)
        if sha:
            return sha
    return ""


def rewrite_git_url(url: str, transport: str) -> str:
    url = url.strip()
    if not url:
        return url
    if transport == GIT_SSH:
        return _url_to_ssh(url)
    return _url_to_https(url)


def _url_to_ssh(url: str) -> str:
    if url.startswith("git@") or url.startswith("ssh://"):
        return url
    matched = _HTTP_RE.match(url)
    if not matched:
        return url
    host, path = matched.group(1), matched.group(2).rstrip("/")
    if _ssh_path_host(host):
        return f"ssh://{host}/{path}"
    if not path.endswith(".git"):
        path += ".git"
    return f"git@{host}:{path}"


def _url_to_https(url: str) -> str:
    if url.startswith("https://"):
        return url
    if url.startswith("http://"):
        return "https://" + url[7:]
    matched = _SCP_RE.match(url)
    if matched:
        return f"https://{matched.group(1)}/{matched.group(2)}"
    matched = _SSH_RE.match(url)
    if matched:
        host = matched.group(2)
        if ":" in host and not host.startswith("["):
            host = host.split(":", 1)[0]
        return f"https://{host}/{matched.group(3)}"
    return url


def _ssh_path_host(host: str) -> bool:
    return "googlesource.com" in host or "git.codelinaro.org" in host


def _remote_fetch_url(work: Path, proj: ManifestProject) -> str:
    code, names = _git(work, "remote")
    remotes = names.split() if code == 0 else []
    name = ""
    if proj.remote and proj.remote in remotes:
        name = proj.remote
    elif "origin" in remotes:
        name = "origin"
    elif remotes:
        name = remotes[0]
    if name:
        code, url = _git(work, "remote", "get-url", name)
        if code == 0 and url:
            return url
    return proj.url


def _fetch_refspec(proj: ManifestProject, revision: str) -> str:
    if _is_fixed_revision(revision) or revision.startswith("refs/tags/"):
        return revision
    remote = proj.remote or "origin"
    if revision.startswith("refs/heads/"):
        branch = revision[len("refs/heads/") :]
        return f"{revision}:refs/remotes/{remote}/{branch}"
    if revision.startswith("refs/"):
        return revision
    return f"{revision}:refs/remotes/{remote}/{revision}"


def _upstream_sha(
    work: Path,
    proj: ManifestProject,
    *,
    fetch: bool = True,
    transport: str = GIT_HTTPS,
) -> str:
    revision = proj.revision or "HEAD"
    if _is_fixed_revision(revision):
        pinned = _resolve_local(work, proj.remote, revision)
        if pinned:
            return pinned
    if fetch:
        url = rewrite_git_url(_remote_fetch_url(work, proj), transport)
        if url:
            spec = _fetch_refspec(proj, revision)
            code, _out = _git(work, "fetch", "--quiet", "--no-tags", "--", url, spec)
            if code == 0:
                fetched = _rev_parse(work, "FETCH_HEAD")
                if fetched:
                    return fetched
    return _resolve_local(work, proj.remote, revision)


def _left_right(work: Path, upstream: str, head: str) -> tuple[int, int] | None:
    code, out = _git(work, "rev-list", "--left-right", "--count", f"{upstream}...{head}")
    if code != 0 or not out:
        return None
    parts = out.replace("\t", " ").split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _decode_chunks(buf: bytes) -> tuple[str, bytes]:
    for keep in range(0, min(4, len(buf))):
        piece = buf if keep == 0 else buf[:-keep]
        rest = b"" if keep == 0 else buf[-keep:]
        try:
            return piece.decode("utf-8"), rest
        except UnicodeDecodeError:
            continue
    return buf.decode("utf-8", errors="replace"), b""


def _wait_exit(proc: subprocess.Popen[bytes], timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return proc.poll() is not None
