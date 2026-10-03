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

import re
from dataclasses import dataclass
from pathlib import Path

_ASSIGN = re.compile(r"^([A-Z0-9_]+)\s*(\+|:)*=\s*(.*)$")
_LOCAL_DIR = re.compile(r"\$\(LOCAL_DIR\)")
_INCLUDE = re.compile(r"^(?:include|-include|sinclude)\s+(\S+\.mk)\s*$")


@dataclass(frozen=True)
class Product:
    product_name: str
    product_model: str
    product_device: str
    makefile: Path

    @property
    def label(self) -> str:
        return f"{self.product_model}-({self.product_name})"


def android_top_from(start: Path) -> Path:
    here = start.resolve()
    candidates = [here, *here.parents]
    for path in candidates:
        if (path / "build" / "envsetup.sh").is_file() and (path / "Makefile").is_file():
            return path
    raise FileNotFoundError("Cannot find Android tree (build/envsetup.sh + Makefile)")


def default_release(top: Path) -> str:
    configs = top / "vendor" / "miku" / "release" / "release_configs"
    if configs.is_dir():
        names = sorted(p.stem for p in configs.glob("*.textproto"))
        if "cp2a" in names:
            return "cp2a"
        if names:
            return names[0]
    return "cp2a"


def discover_products(top: Path) -> list[Product]:
    products: dict[str, Product] = {}
    for products_mk in _android_products_files(top):
        for makefile in _product_makefiles(products_mk):
            product = _parse_product(top, makefile)
            if product is None:
                continue
            products[product.product_name] = product
    return sorted(products.values(), key=lambda p: (p.product_model.lower(), p.product_name))


def resolve_device(products: list[Product], query: str) -> Product:
    raw = query.strip()
    if not raw:
        raise ValueError("device name is empty")
    lowered = raw.lower()

    def pick(matches: list[Product], how: str) -> Product:
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(how)
        labels = ", ".join(p.label for p in matches)
        raise ValueError(f"device {raw!r} is ambiguous ({how}): {labels}")

    try:
        return pick(
            [p for p in products if p.product_device.lower() == lowered],
            "PRODUCT_DEVICE",
        )
    except ValueError as exc:
        if "ambiguous" in str(exc):
            raise

    for pred, how in (
        (lambda p: p.product_name.lower() == lowered, "PRODUCT_NAME"),
        (lambda p: p.product_name.lower() == f"miku_{lowered}", "miku_ prefix"),
        (lambda p: p.makefile.parent.name.lower() == lowered, "device directory"),
    ):
        try:
            return pick([p for p in products if pred(p)], how)
        except ValueError as exc:
            if "ambiguous" in str(exc):
                raise

    labels = "\n".join(f"  {p.label}" for p in products) or "  (none)"
    raise ValueError(f"unknown device {raw!r}\navailable:\n{labels}")


def _android_products_files(top: Path) -> list[Path]:
    files: list[Path] = []
    device_root = top / "device"
    if device_root.is_dir():
        files.extend(sorted(device_root.rglob("AndroidProducts.mk")))
    gsi = top / "vendor" / "miku" / "build" / "target" / "product" / "AndroidProducts.mk"
    if gsi.is_file():
        files.append(gsi)
    return files


def _product_makefiles(products_mk: Path) -> list[Path]:
    local_dir = products_mk.parent
    joined = _join_make_lines(products_mk.read_text(encoding="utf-8", errors="replace"))
    values: list[str] = []
    for line in joined.splitlines():
        match = _ASSIGN.match(line.strip())
        if not match or match.group(1) != "PRODUCT_MAKEFILES":
            continue
        op = match.group(2)
        tokens = match.group(3).split()
        if op != "+":
            values = tokens
        else:
            values.extend(tokens)
    makefiles: list[Path] = []
    for token in values:
        path_token = token.split(":", 1)[-1]
        path_token = _LOCAL_DIR.sub(str(local_dir), path_token)
        path = Path(path_token)
        if not path.is_absolute():
            path = local_dir / path
        if path.is_file():
            makefiles.append(path)
    return makefiles


def _parse_product(top: Path, makefile: Path) -> Product | None:
    text = makefile.read_text(encoding="utf-8", errors="replace")
    fields = _assignments(_join_make_lines(text))
    name = fields.get("PRODUCT_NAME", "").strip()
    if not name:
        name = makefile.stem
    if not _is_miku_product(name, makefile, text):
        return None
    model = fields.get("PRODUCT_MODEL", "").strip() or name
    device = fields.get("PRODUCT_DEVICE", "").strip()
    if not device:
        device = _product_device_from_includes(top, makefile, text) or makefile.parent.name
    return Product(
        product_name=name,
        product_model=model,
        product_device=device,
        makefile=makefile,
    )


def _product_device_from_includes(top: Path, makefile: Path, text: str) -> str:
    for raw in _join_make_lines(text).splitlines():
        match = _INCLUDE.match(raw.strip())
        if not match:
            continue
        token = match.group(1)
        if token.startswith("$(LOCAL_DIR)/"):
            path = makefile.parent / token[len("$(LOCAL_DIR)/") :]
        elif token.startswith("/"):
            path = Path(token)
        else:
            path = top / token
        if not path.is_file():
            continue
        fields = _assignments(_join_make_lines(path.read_text(encoding="utf-8", errors="replace")))
        device = fields.get("PRODUCT_DEVICE", "").strip()
        if device:
            return device
    return ""


def _is_miku_product(name: str, makefile: Path, text: str) -> bool:
    if name.startswith("miku_") or makefile.name.startswith("miku_"):
        return True
    return "vendor/miku/" in text


def _assignments(joined: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in joined.splitlines():
        match = _ASSIGN.match(line.strip())
        if not match:
            continue
        key, _op, value = match.group(1), match.group(2), match.group(3).strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        if key not in out:
            out[key] = value
    return out


def _join_make_lines(text: str) -> str:
    lines: list[str] = []
    buf = ""
    for raw in text.splitlines():
        stripped = _strip_comment(raw).rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        lines.append(buf.strip())
        buf = ""
    if buf.strip():
        lines.append(buf.strip())
    return "\n".join(lines)


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
    return line
