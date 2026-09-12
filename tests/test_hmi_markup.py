from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HmiMarkupContractTests(unittest.TestCase):
    def test_empty_divs_are_expanded_for_html_runtime(self) -> None:
        source = (ROOT / "tc_template" / "TcCom.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("function Convert-TcHmiHtmlCompatibleMarkup", source)
        self.assertIn("'<div${attributes}></div>'", source)
        self.assertIn(
            "$markup = Convert-TcHmiHtmlCompatibleMarkup $builder.ToString()",
            source,
        )
        self.assertIn(
            "return (Convert-TcHmiHtmlCompatibleMarkup $builder.ToString())",
            source,
        )

    def test_hmi_read_supports_focused_and_bounded_results(self) -> None:
        source = (ROOT / "tc_template" / "TcCom.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("[string]$ControlId = ''", source)
        self.assertIn("[bool]$IncludeContent = $true", source)
        self.assertIn("[int]$MaxControls = 200", source)
        self.assertIn("$allNodes | Where-Object", source)
        self.assertIn("controls_truncated=", source)
        self.assertIn("if ($IncludeContent) { $result['content'] = $text }", source)


if __name__ == "__main__":
    unittest.main()
