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

"""Build-complete leek art from vendor/miku/build/tasks/miku.mk."""

ONION_LINES = [
    r"               ..,:cc::c,",
    r"              ;xO000K0000l.",
    r"             ,k00000000000o.",
    r"            .d000000000xlk0o.",
    r"            c00kxxO000x'.l00o.",
    r"           ,kKk:,cx000kl.,k00o.",
    r"          .d00kxk0000000:.o000d.",
    r"          c000000000000O; ,O000d.",
    r"         ,k000000000OkOO; .o0000d.",
    r"        .x00000000kc.;x0:  ;O0000d.",
    r"       .o00000000Kk, 'xKl  .d00000d.",
    r"      .l00000000000xllkKx.  :O00000d.",
    r"      :O0000000000000000O:  .x000000l.",
    r"     ,k000000000000000000d.  c000000O:",
    r"    'x0000000000000kox000O;  ,O00000Kx'",
    r"   ,x00000000kl:d00OxkO000c  .x0000000:",
    r"  ,k000000000l. .x00Kx:;:,.  .d0000000o.",
    r" 'x0000000000:   ;OKKO;      .x0000000d.",
    r".l0000000000O,   .o000l.     ;O000000Kx.",
    r",k0000000000O,    ;O0Kx'    .o00000000l.",
    r":000000000000:    .o00O;    ;O0000000x'",
    r"c0000000000Ol;.    'cx0d.  .x0000000O;",
    r";O00000000k,  .      ,x0o. :0000000O:",
    r".l00000000c           'x0l.,dk000Oo'",
    r" .;xO0Odkk'            .xO: .,xOl'",
    r"   .,ol:xd.             ,kx.  ''",
    r"       'xk'             .l0c",
    r"       .d0l.            .d0x'",
    r"       .l0k,            ,k0d.",
    r"        .;,.            .,;.",
]

# Sparse sparkles, relative to the top-left of the onion block.
SPARKS = (
    (0, 6, "·"),
    (3, 1, "✦"),
    (8, 38, "·"),
    (14, 0, "✧"),
    (21, 40, "·"),
    (26, 4, "✦"),
    (29, 18, "·"),
)

LOGO_LINES = [
    "███    ███ ██ ██   ██ ██    ██     ██    ██ ██",
    "████  ████ ██ ██  ██  ██    ██     ██    ██ ██",
    "██ ████ ██ ██ █████   ██    ██     ██    ██ ██",
    "██  ██  ██ ██ ██  ██  ██    ██     ██    ██ ██",
    "██      ██ ██ ██   ██  ██████       ██████  ██",
]


def banner_lines(width: int, height: int) -> tuple[list[str], int]:
    art = [line[:width] for line in ONION_LINES]
    if height <= 0:
        return [], len(art)
    crop = 0
    if len(art) > height:
        crop = len(art) - height
        art = art[crop:]
    return art, crop


def logo_lines(width: int) -> list[str]:
    if width < 8:
        return []
    return [line[:width] for line in LOGO_LINES]
