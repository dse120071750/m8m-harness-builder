from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Builder3TemplateTests(unittest.TestCase):
    def test_generated_candidate_handler_never_wraps_a_flat_default_result(self) -> None:
        text = (ROOT / "templates/milestone/assemble.py").read_text(encoding="utf-8")
        self.assertIn("candidate executor must return explicit", text)
        self.assertIn('M8M_BUILD_STATUS = "BUILD_REQUIRED"', text)
        self.assertIn("M8M_RUNNABLE = False", text)
        self.assertNotIn("if len(OUTPUTS) == 1", text)
        self.assertNotIn("output_values =", text)

    def test_model_draft_cannot_supply_branch_cycle_or_acceptance_control(self) -> None:
        text = (ROOT / "templates/milestone/assemble.py").read_text(encoding="utf-8")
        self.assertIn("forbidden_control", text)
        self.assertIn('"receipt",', text)
        self.assertIn('"branch",', text)
        self.assertIn('"cycle",', text)
        self.assertIn("CONTROL_KIND", text)
        self.assertIn("runtime invokes their", text)
        self.assertNotIn("trusted_control_receipt", text)
        self.assertNotIn('control = value.get("control_receipt")', text)

    def test_generated_skill_templates_preserve_builder3_boundaries(self) -> None:
        for relative in ("templates/SKILL.md", "templates/product-SKILL.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            collapsed = " ".join(text.split())
            with self.subTest(template=relative):
                self.assertIn("agents/openai.yaml", text)
                self.assertIn("agents/<milestone>.yaml", text)
                self.assertIn("references/<milestone>.md", text)
                self.assertIn("flow.yaml", text)
                self.assertIn("generated", text.lower())
                self.assertIn("BUILD_REQUIRED", text)
                self.assertIn("non_runnable", text)
                self.assertIn("scripts/m8m_run.py", text)
                self.assertIn("codebase launcher", collapsed)
                self.assertIn("digest-addressed runtime release", collapsed)
                self.assertIn("never import or invoke `m8m-harness-builder`", collapsed)
                self.assertIn("never pushes, publishes, deploys", collapsed)
                self.assertIn("activates", text)
                self.assertIn("Builder 2.x run folders are untrusted import", collapsed)


if __name__ == "__main__":
    unittest.main()
