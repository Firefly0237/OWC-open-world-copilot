"""Language / script detection used to handle multilingual input without asking the user."""

from __future__ import annotations

from owcopilot.content.lang import detect_language, language_directive


def test_detects_chinese() -> None:
    profile = detect_language("沈青澜走进雾隐城，灯火是绿色的。")
    assert profile.dominant == "zh"
    assert profile.label == "中文"
    assert profile.mixed is False


def test_detects_english() -> None:
    profile = detect_language(
        "The lanterns of the drowned city burned green, and the harbor was silent."
    )
    assert profile.dominant == "en"
    assert profile.label == "英文"


def test_detects_japanese_by_kana_even_with_kanji() -> None:
    # Kanji-heavy but the kana gives it away as Japanese, not Chinese.
    profile = detect_language("沈青澜は霧の街に着いた。灯りは緑色だった。")
    assert profile.dominant == "ja"
    assert profile.label == "日文"


def test_detects_korean() -> None:
    profile = detect_language("심청란은 안개 도시에 도착했다. 등불은 초록색이었다.")
    assert profile.dominant == "ko"


def test_mixed_chinese_english_is_flagged() -> None:
    text = "沈青澜 arrives at the drowned city. 雾隐城 的灯 burned an unnatural green this evening."
    profile = detect_language(text)
    assert profile.mixed is True
    assert "中文" in profile.labels
    assert "英文" in profile.labels


def test_stray_foreign_name_does_not_flip_to_mixed() -> None:
    # One Latin proper noun in an otherwise Chinese paragraph must NOT read as mixed.
    profile = detect_language(
        "沈青澜抵达了名为 Avalon 的小镇，那里的灯火常年是绿色的，钟声日夜错乱。"
        "她沿着长街走了很久，街边的店铺都关着，只有一盏旧灯还亮着微弱的光。"
        "镇上的人说，绿灯亮起的夜里，雾会把人带走，再也回不来。"
    )
    assert profile.dominant == "zh"
    assert profile.mixed is False


def test_empty_and_punctuation_only_text_is_undetermined() -> None:
    assert detect_language("").dominant == "und"
    assert detect_language("   …!?  123  ").dominant == "und"


def test_language_directive_mentions_language_and_proper_nouns() -> None:
    zh = language_directive(detect_language("沈青澜走进雾隐城。"))
    assert "中文" in zh and "专有名词" in zh
    mixed = language_directive(
        detect_language("沈青澜 arrives. 雾隐城 的灯 burned green and the bell rang wrong tonight.")
    )
    assert "多语言" in mixed
