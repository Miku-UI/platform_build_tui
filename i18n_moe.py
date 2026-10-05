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

# Keys match i18n.py. Missing keys fall back to i18n.py for that language.
_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "phase_setup": "Miku is preparing the env... (ˉ﹃ˉ)",
        "phase_lunch": "Miku is setting up Lunch ( •̀ ω •́ )✧",
        "phase_clean": "Miku is sweeping the battlefield... (╯°□°)╯︵ ┻━┻",
        "phase_config": "Miku is getting the device config ready... (●ˇ∀ˇ●)",
        "phase_soong": "Miku is studying the source... (＠_＠;)",
        "phase_kati": "Miku is planning the build... (★ ω ★)",
        "phase_ninja": "Miku is compiling... (o゜▽゜)o☆",
        "phase_package": "Almost there... Miku is packing up...（；´д｀）ゞ",
        "result_headline": "Miku UI {version} build ",
        "result_headline_ok": "all done!",
        "result_headline_fail": "went wrong...",
        "result_timing": "Where did the time go",
        "result_bar_caption": "time pie, but a bar",
        "result_artifacts": "Treats from this build",
        "result_fail_analysis": "What made Miku trip",
    },
    "zh": {
        "phase_setup": "Miku 准备环境ing... (ˉ﹃ˉ)",
        "phase_lunch": "Miku 配置 Lunch 中 ( •̀ ω •́ )✧",
        "phase_clean": "Miku 打扫战场ing... (╯°□°)╯︵ ┻━┻",
        "phase_config": "Miku 正在准备要构建的机型配置ing... (●ˇ∀ˇ●)",
        "phase_soong": "Miku 研究源码ing... (＠_＠;)",
        "phase_kati": "Miku 规划构建计划ing... (★ ω ★)",
        "phase_ninja": "Miku 正在编译ing... (o゜▽゜)o☆",
        "phase_package": "就快要好啦... Miku 正在打包ing...（；´д｀）ゞ",
        "result_headline": "Miku UI {version} 构建",
        "result_headline_ok": "完成啦",
        "result_headline_fail": "失败了呜",
        "result_timing": "时间都去哪儿了",
        "result_bar_caption": "耗时分布",
        "result_artifacts": "这次编译的小礼物",
        "result_fail_analysis": "Miku 摔倒的原因是…",
    },
    "ja": {
        "phase_setup": "ミク、環境づくり中… (ˉ﹃ˉ)",
        "phase_lunch": "ミク、Lunch せってい中 ( •̀ ω •́ )✧",
        "phase_clean": "ミク、戦場リセット中… (╯°□°)╯︵ ┻━┻",
        "phase_config": "ミク、機種コンフィグ用意してるよ… (●ˇ∀ˇ●)",
        "phase_soong": "ミク、ソース研究中… (＠_＠;)",
        "phase_kati": "ミク、ビルド計画ちゅう… (★ ω ★)",
        "phase_ninja": "ミク、コンパイルしてるよ… (o゜▽゜)o☆",
        "phase_package": "もうすぐだよ… ミク、パッケージ中…（；´д｀）ゞ",
        "result_headline": "Miku UI {version} ビルド",
        "result_headline_ok": "できたよ",
        "result_headline_fail": "失敗しちゃった",
        "result_timing": "時間、どこいった？",
        "result_bar_caption": "時間の内訳だよ",
        "result_artifacts": "今回のお土産",
        "result_fail_analysis": "つまずいた理由は…",
    },
    "ru": {
        "phase_setup": "Мику готовит окружение... (ˉ﹃ˉ)",
        "phase_lunch": "Мику настраивает Lunch ( •̀ ω •́ )✧",
        "phase_clean": "Мику зачищает поле боя... (╯°□°)╯︵ ┻━┻",
        "phase_config": "Мику готовит конфиг устройства... (●ˇ∀ˇ●)",
        "phase_soong": "Мику изучает исходники... (＠_＠;)",
        "phase_kati": "Мику планирует сборку... (★ ω ★)",
        "phase_ninja": "Мику компилирует... (o゜▽゜)o☆",
        "phase_package": "Почти готово... Мику пакует...（；´д｀）ゞ",
        "result_headline": "Сборка Miku UI {version} ",
        "result_headline_ok": "готова!",
        "result_headline_fail": "ой, не вышло...",
        "result_timing": "Куда ушло время",
        "result_bar_caption": "как распределилось время",
        "result_artifacts": "Гостинцы этой сборки",
        "result_fail_analysis": "Почему Мику споткнулась",
    },
    "tr": {
        "phase_setup": "Miku ortamı hazırlıyor... (ˉ﹃ˉ)",
        "phase_lunch": "Miku Lunch ayarlıyor ( •̀ ω •́ )✧",
        "phase_clean": "Miku savaş alanını temizliyor... (╯°□°)╯︵ ┻━┻",
        "phase_config": "Miku cihaz yapılandırmasını hazırlıyor... (●ˇ∀ˇ●)",
        "phase_soong": "Miku kaynak kodunu inceliyor... (＠_＠;)",
        "phase_kati": "Miku derleme planı yapıyor... (★ ω ★)",
        "phase_ninja": "Miku derliyor... (o゜▽゜)o☆",
        "phase_package": "Az kaldı... Miku paketiliyor...（；´д｀）ゞ",
        "result_headline": "Miku UI {version} derlemesi ",
        "result_headline_ok": "bitti!",
        "result_headline_fail": "olmadı...",
        "result_timing": "Zaman nereye gitti",
        "result_bar_caption": "süre dağılımı",
        "result_artifacts": "Bu derlemenin hediyeleri",
        "result_fail_analysis": "Miku neden takıldı",
    },
}


def lookup(lang: str, key: str) -> str | None:
    table = _STRINGS.get(lang)
    if table is not None and key in table:
        return table[key]
    return None
