from __future__ import annotations

import unittest

from tc_template.static_analysis import analyze_objects


class StaticAnalysisTests(unittest.TestCase):
    def _analyse(self, declaration: str, implementation: str, **extra) -> dict:
        return analyze_objects([{
            "name": "FB_Analysis", "folder": "POUs",
            "declaration": declaration, "implementation": implementation,
            "methods": [], **extra,
        }], max_complexity=3)

    def test_reports_explicit_te1200_boundary(self) -> None:
        result = self._analyse("FUNCTION_BLOCK FB_Analysis", "")
        self.assertFalse(result["te1200_executed"])
        self.assertEqual("not-executed", result["te1200_status"])
        self.assertIn("cannot prove", result["disclaimer"])

    def test_finds_input_write_and_literal_division_by_zero(self) -> None:
        result = self._analyse(
            "FUNCTION_BLOCK FB_Analysis\nVAR_INPUT\n    nInput : INT;\nEND_VAR",
            "nInput := 2;\nnValue := 10 / 0;",
        )
        findings = {item["rule"]: item for item in result["findings"]}
        self.assertEqual("error", findings["TCSA0037"]["severity"])
        self.assertEqual(1, findings["TCSA0037"]["line"])
        self.assertEqual("error", findings["TCSA0040"]["severity"])
        self.assertEqual(2, findings["TCSA0040"]["line"])

    def test_input_rule_ignores_named_arguments_inside_fb_call(self) -> None:
        result = self._analyse(
            "FUNCTION_BLOCK FB_Analysis\nVAR_INPUT\n"
            "    bExecute : BOOL;\n    sPathName : STRING;\nEND_VAR",
            "fbFileOpen(\n"
            "    sPathName := sPathName,\n"
            "    bExecute :=,\n"
            "    tTimeout := T#1S);\n"
            "bExecute := FALSE;",
        )
        findings = [item for item in result["findings"] if item["rule"] == "TCSA0037"]
        self.assertEqual(1, len(findings))
        self.assertIn("bExecute", findings[0]["message"])
        self.assertEqual(5, findings[0]["line"])

    def test_parentheses_inside_string_do_not_hide_direct_input_write(self) -> None:
        result = self._analyse(
            "FUNCTION_BLOCK FB_Analysis\nVAR_INPUT\n    sPathName : STRING;\nEND_VAR",
            "sLog := 'call(not a real call)';\nsPathName := 'changed';",
        )
        findings = [item for item in result["findings"] if item["rule"] == "TCSA0037"]
        self.assertEqual(1, len(findings))
        self.assertEqual(2, findings[0]["line"])

    def test_finds_real_equality_and_does_not_mistake_assignment(self) -> None:
        result = self._analyse(
            "FUNCTION_BLOCK FB_Analysis\nVAR\n    lrActual : LREAL;\nEND_VAR",
            "lrActual := 1.0;\nbMatch := lrActual = 1.0;",
        )
        matches = [f for f in result["findings"] if f["rule"] == "TCSA0054"]
        self.assertEqual(1, len(matches))
        self.assertEqual(2, matches[0]["line"])

    def test_finds_commented_code_temp_fb_and_complexity(self) -> None:
        result = self._analyse(
            "FUNCTION_BLOCK FB_Analysis\nVAR_TEMP\n    fbTimer : FB_Timer;\nEND_VAR",
            "// IF bOld THEN\nIF bA THEN\n    IF bB THEN\n        IF bC THEN\n        END_IF\n    END_IF\nEND_IF",
        )
        rules = {f["rule"] for f in result["findings"]}
        self.assertTrue({"TCSA0140", "TCSA0167", "TCSA0178"}.issubset(rules))

    def test_finds_unused_local_variable(self) -> None:
        result = self._analyse(
            "FUNCTION_BLOCK FB_Analysis\nVAR\n    nUnused : INT;\n    nUsed : INT;\nEND_VAR",
            "nUsed := 1;",
        )
        unused = [f for f in result["findings"] if f["rule"] == "TCSA0033"]
        self.assertEqual(1, len(unused))
        self.assertIn("nUnused", unused[0]["message"])

    def test_safe_divisors_and_masked_text_do_not_block_writes(self):
        result = self._analyse('', '''sText := 'nInput := 2; / 0';
(* outer (* / 0 *) / 0 *)
nValue := 10 / 0.5;
nValue := 10 / 0.001;
nValue := 10 / (0 + 1);
nValue := 10 / 16#01;
nValue := 10 / 1e-3;''')
        self.assertFalse([f for f in result['findings'] if f['severity'] == 'error'])

    def test_zero_literal_complete_tokens(self):
        result = self._analyse('', 'x := 1 / (0.0);\nx := 1 MOD 0;\nx := 1 / -0;')
        self.assertEqual(3, result['summary']['errors'])

    def test_same_line_input_assignment_and_method_shadowing(self):
        result = self._analyse(
            'FUNCTION_BLOCK FB_Analysis\nVAR_INPUT\n nInput : INT;\nEND_VAR',
            'IF bRun THEN nInput := 1; END_IF;',
            methods=[{'name': 'Work', 'declaration': 'METHOD Work\nVAR\n nInput : INT;\nEND_VAR',
                      'implementation': 'nInput := 2;'},
                     {'name': 'Bad', 'declaration': 'METHOD Bad\nVAR_INPUT\n nArg : INT;\nEND_VAR',
                      'implementation': 'nArg := 3;'}])
        matches = [f for f in result['findings'] if f['rule'] == 'TCSA0037']
        self.assertEqual({'implementation', 'member:Bad'}, {f['area'] for f in matches})

    def test_nested_if_else_does_not_satisfy_case_else(self):
        result = self._analyse('', '''CASE eState OF
0: IF bRun THEN x := 1; ELSE x := 2; END_IF;
1: CASE eOther OF 0: x := 3; ELSE x := 4; END_CASE;
END_CASE;''')
        matches = [f for f in result['findings'] if f['rule'] == 'TCSA0075']
        self.assertEqual([1], [f['line'] for f in matches])

    def test_standard_timer_in_var_temp_is_reported(self):
        result = self._analyse('PROGRAM MAIN\nVAR_TEMP\n fbTimer : TON;\nEND_VAR', 'fbTimer();')
        self.assertTrue(any(f['rule'] == 'TCSA0167' for f in result['findings']))


if __name__ == "__main__":
    unittest.main()
