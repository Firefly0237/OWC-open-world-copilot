# Unity Localization CSV：opt-in 的 String Table Collection 字符串表产物。
"""Unity Localization CSV — an opt-in String Table Collection export for ``com.unity.localization``.

Unity's official Localization package ships a CSV extension (Window ▸ Asset Management ▸
Localization Tables, then the ⋮ menu ▸ Import/Export) whose column format is fixed and documented:
a first ``Key`` column, then one column per Locale whose header is ``LocaleIdentifier.ToString()``
— ``"<Locale display name>(<code>)"`` with no space before the parenthesis (e.g. ``English(en)``,
``French(fr)``). ``Id`` and ``Shared Comments`` columns are optional ("CSV data must contain a Key
or Id column. All other columns are optional."), so we emit the documented **minimal subset**: Key
+ one column per locale. Empty cells mean "no translation for that locale" — we never copy the
default language into an empty cell, because that would fabricate a translation status.

Why CSV rather than C# code: the string table travels through Unity's *official decoupled channel*.
Writing Localization dependencies into our UPM package would force a hard ``com.unity.localization``
dependency on every consumer; the CSV path keeps the importer package minimal and lets teams who
use the Localization package pull the strings in with one click.

Source of the Key set: the ``LocalizedText`` records (``localization/texts``). ``LocalizedText``
has no domain/category field, so the simplest faithful mapping is a **single** String Table
Collection (one CSV, one column per locale) rather than a per-domain split. Deterministic and
LLM-free (offline, $0), same as the rest of the export path.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from ..content.models import ContentBundle
from .models import ExportedFile

UNITY_LOCALIZATION_SUBDIR = "localization"
STRING_TABLE_COLLECTION_NAME = "owcopilot_strings"
UNITY_STRING_TABLE_FILENAME = f"{STRING_TABLE_COLLECTION_NAME}.csv"
UNITY_STRING_TABLE_KIND = "unity_localization_csv"
UNITY_LOCALIZATION_README_FILENAME = "README.md"
UNITY_LOCALIZATION_README_KIND = "unity_localization_readme"

# Reproduces Unity ``LocaleIdentifier.ToString()`` = ``f"{CultureInfo.EnglishName}({code})"`` for
# the mainstream game-localization locales whose EnglishName is stable across .NET/ICU versions. The
# ``(code)`` inside the header is the machine-reliable part Unity matches on; the display name only
# drives the default column auto-mapping on import. Region-qualified Chinese codes are mapped to the
# script-neutral display names per the accepted spec — the exact runtime EnglishName of a region
# code (e.g. "Chinese (Simplified, China)") is platform-dependent, so a team whose Locale asset uses
# a different name simply remaps the column once in the import dialog (the ``(code)`` still
# identifies it). A code not in this table falls back to its bare code as the header, which matches
# ``LocaleIdentifier.ToString()``'s behaviour when no CultureInfo resolves.
_LOCALE_DISPLAY_NAME: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "ru": "Russian",
    "ja": "Japanese",
    "ko": "Korean",
    "pl": "Polish",
    "nl": "Dutch",
    "tr": "Turkish",
    "ar": "Arabic",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "cs": "Czech",
    "uk": "Ukrainian",
    "hu": "Hungarian",
    "ro": "Romanian",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "el": "Greek",
    "zh": "Chinese",
    "zh-Hans": "Chinese (Simplified)",
    "zh-Hant": "Chinese (Traditional)",
    "zh-CN": "Chinese (Simplified)",
    "zh-SG": "Chinese (Simplified)",
    "zh-TW": "Chinese (Traditional)",
    "zh-HK": "Chinese (Traditional)",
}


@dataclass(frozen=True)
class UnityLocalizationSummary:
    """The cross-check facts a reviewer reconciles against the in-editor CSV import report."""

    collection_name: str
    # path relative to the ``unity/`` dir, e.g. ``localization/owcopilot_strings.csv``
    csv_path: str
    locales: list[str]
    string_count: int


def locale_column_header(code: str) -> str:
    """The Unity Localization CSV column header for a locale: ``<display name>(<code>)``.

    Falls back to the bare code when the display name is unknown (matches Unity's own
    ``LocaleIdentifier.ToString()`` when no CultureInfo resolves)."""
    name = _LOCALE_DISPLAY_NAME.get(code)
    return f"{name}({code})" if name else code


def _string_table(bundle: ContentBundle) -> tuple[list[str], list[str], dict[tuple[str, str], str]]:
    """Pivot ``LocalizedText`` into (sorted keys, sorted locale codes, {(key, locale): text}).

    The Key set is exactly the set of ``LocalizedText.text_key`` values; a missing (key, locale)
    pair is simply absent from the map, so its cell is written empty."""
    keys: set[str] = set()
    locales: set[str] = set()
    cells: dict[tuple[str, str], str] = {}
    for text in bundle.localized_texts.values():
        keys.add(text.text_key)
        locales.add(text.locale)
        cells[(text.text_key, text.locale)] = text.text
    return sorted(keys), sorted(locales), cells


def render_string_table_csv(bundle: ContentBundle) -> str:
    """Render the single String Table Collection CSV (Key + one column per locale).

    Uses the ``csv`` module so commas, double-quotes and embedded newlines round-trip through
    Unity's RFC-4180 CSV reader (quotes doubled, whole field quoted when needed)."""
    keys, locales, cells = _string_table(bundle)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["Key", *(locale_column_header(code) for code in locales)])
    for key in keys:
        writer.writerow([key, *(cells.get((key, code), "") for code in locales)])
    return buffer.getvalue()


def write_unity_localization(
    bundle: ContentBundle, unity_dir: str | Path
) -> tuple[list[ExportedFile], UnityLocalizationSummary]:
    """Write ``localization/owcopilot_strings.csv`` + ``localization/README.md`` under unity_dir.

    Returns the file entries (paths relative to ``unity_dir``, so the caller prefixes ``unity/``,
    mirroring how ``write_unity_manifest`` records its file) plus the summary the sidecar manifest
    records for cross-checking."""
    _, locales, _ = _string_table(bundle)
    output = Path(unity_dir) / UNITY_LOCALIZATION_SUBDIR
    output.mkdir(parents=True, exist_ok=True)

    # write_bytes (not write_text): text-mode writes translate '\n' to os.linesep, which both
    # corrupts newlines embedded inside a quoted CSV field and makes output platform-dependent.
    # Writing UTF-8 bytes keeps '\n' line terminators and embedded newlines byte-identical anywhere.
    csv_text = render_string_table_csv(bundle)
    (output / UNITY_STRING_TABLE_FILENAME).write_bytes(csv_text.encode("utf-8"))
    (output / UNITY_LOCALIZATION_README_FILENAME).write_bytes(_readme().encode("utf-8"))

    files = [
        _entry(output, UNITY_STRING_TABLE_FILENAME, UNITY_STRING_TABLE_KIND),
        _entry(output, UNITY_LOCALIZATION_README_FILENAME, UNITY_LOCALIZATION_README_KIND),
    ]
    summary = UnityLocalizationSummary(
        collection_name=STRING_TABLE_COLLECTION_NAME,
        csv_path=f"{UNITY_LOCALIZATION_SUBDIR}/{UNITY_STRING_TABLE_FILENAME}",
        locales=locales,
        string_count=len({text.text_key for text in bundle.localized_texts.values()}),
    )
    return files, summary


def _entry(output: Path, filename: str, kind: str) -> ExportedFile:
    digest = hashlib.sha256((output / filename).read_bytes()).hexdigest()
    return ExportedFile(path=f"{UNITY_LOCALIZATION_SUBDIR}/{filename}", kind=kind, sha256=digest)


def _readme() -> str:
    return (
        "# Unity Localization 字符串表（opt-in）\n"
        "\n"
        f"`{UNITY_STRING_TABLE_FILENAME}` 是按 Unity 官方 Localization 包"
        "（`com.unity.localization`）的 CSV 扩展格式导出的单张 String Table Collection："
        "首列 `Key`，随后每个 locale 一列，"
        "列头为 `<Locale 名称>(<locale-code>)`（如 `English(en)`）。某 locale 无译文的格子留空，"
        "绝不用默认语言回填（那会伪造翻译状态）。\n"
        "\n"
        "## 导入到 Unity\n"
        "\n"
        "1. 装好 `com.unity.localization` 包，建好 String Table Collection 与对应 Locale。\n"
        "2. Window ▸ Asset Management ▸ Localization Tables 打开表窗口。\n"
        f"3. 点右上角竖排省略号（⋮）▸ Import ▸ 选择本文件 `{UNITY_STRING_TABLE_FILENAME}`。\n"
        "4. `Key` 列匹配条目键，`<名称>(<code>)` 列按 locale 匹配到各 Locale。\n"
        "\n"
        "为什么走 CSV 而不在 UPM 包里写 Localization 代码：CSV 是官方解耦通道。若在 "
        "`com.owcopilot.content-import` 包里直接依赖 Localization，会给包引入 "
        "`com.unity.localization` 硬依赖；CSV 让导入器包保持最小依赖，"
        "用 Localization 的团队一键导入即可。\n"
    )
