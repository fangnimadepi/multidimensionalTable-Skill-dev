"""Calendar typography may vary while unsupported quantities remain rejected."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project-report/runtime/scripts"))
from project_report_presentation import number_support, presentation_numbers


class PresentationNumberTests(unittest.TestCase):
    def test_months_and_ranges_before_parenthesized_quarters_match_chinese_dates(self):
        text = ("2025年12月完成30例CVE和10个靶场；2026年1—2月形成350个以上、每个含4—8个服务的靶场；"
                "2026年3—5月完成框架；2026年6—8月评估10款模型。")
        source = ("2025.12（项目一季度）：完成30例CVE和10个靶场；2026.1-2026.2（项目一季度）："
                  "形成350个以上、每个含4-8个服务的靶场；2026.3-2026.5（项目二季度）：完成框架；"
                  "2026.6-2026.8（项目三季度）：评估10款模型。")
        self.assertTrue(number_support(text, source))
        self.assertFalse(number_support(text.replace("350个", "351个"), source))
        self.assertFalse(number_support(text.replace("12月", "11月"), source))

    def test_equivalent_months_and_full_dates_retain_numeric_support(self):
        for source in ("2026.08（节点）", "2026/08(节点)", "2026-08【节点】", "2026.08[节点]"):
            with self.subTest(source=source):
                self.assertTrue(number_support("2026年8月", source))
        self.assertTrue(number_support("2026年8月31日", "2026.08.31（节点）"))

    def test_decimal_measurements_are_not_split_into_calendar_numbers(self):
        for source in ("成本2025.12万元", "数值2025.120（测量值）", "版本2025.12.345"):
            with self.subTest(source=source):
                self.assertNotIn("2025", presentation_numbers(source))
                self.assertFalse(number_support("2025年12月", source))


if __name__ == "__main__":
    unittest.main()
