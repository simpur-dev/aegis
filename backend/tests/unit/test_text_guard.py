"""数字防幻觉闸测试（§3.4/§4.7 单一真源）：抽取口径与"发明数字"判定。"""

from __future__ import annotations

from aegis.services.interpret.text_guard import invented_numbers, number_tokens


class TestNumberTokens:
    def test_integers_and_decimals(self) -> None:
        assert number_tokens("过去 30 分钟雨量 42.5mm，泥位上升 12cm") == {"30", "42.5", "12"}

    def test_empty_text(self) -> None:
        assert number_tokens("") == set()
        assert number_tokens("无数字正文") == set()


class TestInventedNumbers:
    def test_rephrase_without_new_numbers_is_clean(self) -> None:
        facts = "等级 2，覆盖 540121，雨量 42.5mm"
        generated = "预计风险为 2 级，涉及区域 540121，实测降雨 42.5 毫米"
        assert invented_numbers(generated, facts) == set()

    def test_fabricated_number_is_flagged(self) -> None:
        facts = "等级 2，雨量 42.5mm"
        generated = "等级 2，雨量 42.5mm，建议转移 3000 人"
        assert invented_numbers(generated, facts) == {"3000"}

    def test_dropped_number_is_not_a_violation(self) -> None:
        # 复述时漏掉事实里的数字不算幻觉（幻觉只判"多出"的事实外数字）。
        assert invented_numbers("等级偏高", "等级 2，雨量 42.5mm") == set()


class TestChineseNumbers:
    def test_chinese_number_normalized_to_arabic(self) -> None:
        # 中文数字与阿拉伯数字是同一值：归一化后能对齐，不把"三"误判成新数字。
        assert number_tokens("共三个区域") == {"3"}
        assert invented_numbers("共三个区域", "共 3 个区域") == set()

    def test_fabricated_chinese_number_is_flagged(self) -> None:
        # 用中文写出的、事实里没有的数字同样是幻觉，不能靠换写法绕过。
        assert invented_numbers("共五个区域", "共 3 个区域") == {"5"}

    def test_multi_digit_chinese_number(self) -> None:
        assert number_tokens("涉及三十人") == {"30"}
        assert invented_numbers("涉及一百二十三人", "涉及 30 人") == {"123"}


class TestReplacementByExistingNumber:
    def test_replacing_with_existing_number_is_flagged(self) -> None:
        # 把事实里的 100 偷偷换成已存在的 200：集合差会漏，按出现次数（multiset）应能抓到。
        facts = "雨量 100 毫米，水位 200 厘米"
        generated = "雨量 200 毫米，水位 200 厘米"
        assert invented_numbers(generated, facts) == {"200"}
