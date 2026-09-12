import unittest

from tc_template.fb_scaffold import standard_fb
from tc_template.lint import lint_objects, review_write_candidate


class FbContractLintTests(unittest.TestCase):
    def test_standard_scaffold_satisfies_new_contract_rules(self):
        fb = standard_fb("Contract")
        findings = lint_objects([{
            "name": fb["fb_name"], "folder": "POUs",
            "declaration": fb["fb_declaration"],
            "implementation": fb["fb_implementation"], "methods": [],
        }])
        rules = {item["rule"] for item in findings}
        self.assertNotIn("declaration-order", rules)
        self.assertNotIn("transaction-state", rules)
        self.assertNotIn("transaction-timeout", rules)
        self.assertNotIn("fb-call-count", rules)

    def test_reports_bad_declaration_and_transaction_contract(self):
        declaration = """FUNCTION_BLOCK FB_Bad
VAR
    fbTimer : TON;
END_VAR
VAR_INPUT
    bExecute : BOOL;
END_VAR
VAR_OUTPUT
    bDone : BOOL;
    bBusy : BOOL;
    bError : BOOL;
    nErrId : UDINT;
END_VAR"""
        findings = lint_objects([{
            "name": "FB_Bad", "folder": "POUs", "declaration": declaration,
            "implementation": "bBusy := bExecute;", "methods": [],
        }])
        rules = {item["rule"] for item in findings}
        self.assertIn("declaration-order", rules)
        self.assertIn("transaction-state", rules)
        self.assertIn("fb-call-count", rules)

    def test_write_review_advises_undocumented_and_unranged_input(self):
        candidate = {
            "name": "FB_Recipe", "folder": "POUs", "methods": [],
            "declaration": """FUNCTION_BLOCK FB_Recipe
VAR_INPUT
    nTemperature : INT;
END_VAR""",
            "implementation": "",
        }
        review = review_write_candidate(candidate, changed_area="declaration")
        self.assertTrue(review["approved"])
        self.assertTrue({"declaration-comment", "declaration-range"}.issubset(
            {item["rule"] for item in review["advisories"]}))

    def test_write_review_requires_child_fb_call_before_state_machine(self):
        candidate = {
            "name": "FB_GatedCall", "folder": "POUs", "methods": [],
            "declaration": """FUNCTION_BLOCK FB_GatedCall
VAR
    eState : E_TestState; // 状态机
    fbTimer : TON; // 周期定时器
END_VAR""",
            "implementation": """CASE eState OF
    E_TestState.Idle:
        fbTimer(IN := TRUE, PT := T#1S);
END_CASE""",
        }
        review = review_write_candidate(candidate, changed_area="implementation")
        self.assertTrue(review["approved"])
        self.assertIn("fb-call-zone", {item["rule"] for item in review["advisories"]})

    def test_interface_declaration_rejects_end_interface(self):
        review = review_write_candidate({
            "name": "I_Test", "folder": "Interfaces", "methods": [],
            "declaration": "INTERFACE I_Test\nEND_INTERFACE",
            "implementation": "",
        }, changed_area="declaration")
        self.assertFalse(review["approved"])
        self.assertIn("interface-terminator", {
            item["rule"] for item in review["blocking_findings"]
        })

    def test_interface_declaration_rejects_inline_method_or_property(self):
        for member in ("METHOD Start : BOOL", "PROPERTY State : INT"):
            with self.subTest(member=member):
                review = review_write_candidate({
                    "name": "I_Test", "folder": "Interfaces", "methods": [],
                    "declaration": f"INTERFACE I_Test\n{member}",
                    "implementation": "",
                }, changed_area="declaration")
                self.assertFalse(review["approved"])
                self.assertIn("interface-member-inline", {
                    item["rule"] for item in review["blocking_findings"]
                })

    def test_write_review_blocks_tcsa_error_level_safety_findings(self):
        review = review_write_candidate({
            "name": "FB_Safety", "folder": "POUs", "methods": [],
            "declaration": """FUNCTION_BLOCK FB_Safety
VAR_INPUT
    nInput : INT; // test input, range 0..10
END_VAR""",
            "implementation": "nInput := 10 / 0;",
        }, changed_area="implementation")
        self.assertFalse(review["approved"])
        self.assertTrue({"TCSA0037", "TCSA0040"}.issubset({
            item["rule"] for item in review["blocking_findings"]
        }))


if __name__ == "__main__":
    unittest.main()
