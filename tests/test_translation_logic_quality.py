import pytest

from app.translation import translation_logic_issues, translation_quality_issues


@pytest.mark.parametrize(
    ("source", "translated", "failure"),
    [
        ("before September 2026", "2026年9月より前", None),
        ("before September 2026", "2026年9月以前", None),
        ("before September 2026", "2026年9月までに", "logic_before_not_preserved"),
        ("by December 31, 2026", "2026年12月31日までに", None),
        ("by December 31, 2026", "2026年12月31日より前", "logic_by_not_preserved"),
        ("on or before July 31, 2026", "2026年7月31日以前", None),
        ("on or before July 31, 2026", "2026年7月31日までに", None),
        ("on or before July 31, 2026", "2026年7月31日より前", "logic_on_or_before_not_preserved"),
        ("after January 1, 2027", "2027年1月1日より後", None),
        ("after January 1, 2027", "2027年1月1日以降", "logic_after_not_preserved"),
        ("on or after January 1, 2027", "2027年1月1日以降", None),
        ("on or after January 1, 2027", "2027年1月1日より後", "logic_on_or_after_not_preserved"),
        ("at least three countries", "少なくとも3か国", None),
        ("at least three countries", "3か国以上", None),
        ("at least three countries", "3か国未満", "logic_at_least_not_preserved"),
        ("more than 100,000", "10万を超える", None),
        ("more than 100,000", "10万以上", "logic_more_than_not_preserved"),
        ("less than 5 percent", "5パーセント未満", None),
        ("less than 5 percent", "5パーセント以下", "logic_less_than_not_preserved"),
        ("no more than 10", "10以下", None),
        ("no more than 10", "10未満", "logic_no_more_than_not_preserved"),
        ("only if the event occurs", "発生した場合に限り", None),
        ("only if the event occurs", "発生した場合", "logic_only_if_not_preserved"),
        ("will not leave NATO", "NATOを離脱しない", None),
        ("will not leave NATO", "NATOを離脱する", "logic_negation_not_preserved"),
        ("without approval", "承認なしで", None),
        ("without approval", "承認を得ずに", None),
        ("without approval", "承認した場合", "logic_negation_not_preserved"),
        ("between 3 and 5 countries", "3から5か国の間", None),
        ("between 3 and 5 countries", "3か国以上", "logic_between_not_preserved"),
        ("exactly 3 countries", "ちょうど3か国", None),
        ("exactly 3 countries", "少なくとも3か国", "logic_exactly_not_preserved"),
        ("either France or Germany", "フランスまたはドイツ", None),
        ("either France or Germany", "フランスとドイツの両方", "logic_either_or_not_preserved"),
        ("both France and Germany", "フランスとドイツの両方", None),
        ("both France and Germany", "フランスまたはドイツ", "logic_both_and_not_preserved"),
    ],
)
def test_logic_operator_preservation(source, translated, failure):
    issues = translation_logic_issues(source, translated)
    if failure:
        assert failure in issues
    else:
        assert not issues


def test_logic_failures_are_added_to_existing_quality_issues():
    issues = translation_quality_issues("Will it happen before September 2026?", "2026年9月までに起きますか？")
    assert "logic_before_not_preserved" in issues


def test_operator_free_text_has_no_logic_failure():
    assert translation_logic_issues("Will Japan hold an election in 2026?", "日本は2026年に選挙を行いますか？") == []


def test_compound_operator_does_not_duplicate_before_or_by_checks():
    issues = translation_logic_issues("on or before July 31, 2026", "2026年7月31日より前")
    assert issues == ["logic_on_or_before_not_preserved"]


def test_compound_operator_does_not_duplicate_after_or_negation_checks():
    assert translation_logic_issues("on or after January 1, 2027", "2027年1月1日より後") == ["logic_on_or_after_not_preserved"]
    assert translation_logic_issues("no more than 10", "10未満") == ["logic_no_more_than_not_preserved"]


def test_logic_normalizes_case_full_width_spaces_and_punctuation():
    assert translation_logic_issues("BEFORE September 2026", "２０２６年９月　以前。") == []
