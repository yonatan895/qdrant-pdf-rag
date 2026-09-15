"""Dependency-free context checker tests: temporary trees, no product imports."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.check_agent_context import CHAIN_BUDGET, REQUIRED, check


class ContextCheckTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in REQUIRED:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("content\n", encoding="utf-8")
        self.workflow = self.root/"docs/agent-workflow.md"
        self.workflow.write_text('''<!-- context-map:start -->
| Contract | Canonical owner | Boundaries | Evidence |
|---|---|---|---|
| example | [Owner](owner.md#contract) | [Source](../Makefile) | [Tests](owner.md) |
<!-- context-map:end -->
<!-- instruction-chains:start -->
| Invocation directory | Chain |
|---|---|
| `.` | [Root](../AGENTS.md) |
<!-- instruction-chains:end -->
''', encoding="utf-8")
        (self.root/"docs/owner.md").write_text('<a id="contract"></a>\n', encoding="utf-8")

    def replace(self, old, new):
        self.workflow.write_text(self.workflow.read_text().replace(old, new))

    def test_valid_minimal_layout(self):
        self.assertEqual(check(self.root)[0], [])

    def test_utf8_budget_counts_bytes(self):
        (self.root/"AGENTS.md").write_text("א"*4097, encoding="utf-8")
        self.assertTrue(any("8194" in error for error in check(self.root)[0]))

    def test_crlf_bytes_are_not_normalized_away(self):
        (self.root/"AGENTS.md").write_bytes(b"x\r\n"*2731)
        self.assertTrue(any("8193" in e for e in check(self.root)[0]))

    def test_chain_includes_separators_and_client_limit(self):
        (self.root/"src").mkdir()
        (self.root/"src/AGENTS.md").write_text("x"*(CHAIN_BUDGET-8))
        self.replace('| `.` | [Root](../AGENTS.md) |', '| `src` | [Root](../AGENTS.md), [Nested](../src/AGENTS.md) |')
        self.assertTrue(any("chain budget exceeded" in e for e in check(self.root)[0]))
        (self.root/"src/AGENTS.md").write_text("small")
        self.assertEqual(check(self.root)[0], [])
        self.assertTrue(any("effective client limit" in e for e in check(self.root, 15)[0]))

    def test_broken_local_reference_and_anchor(self):
        self.replace('owner.md#contract', 'owner.md#missing')
        self.assertTrue(any("missing explicit anchor" in e for e in check(self.root)[0]))
        self.replace('owner.md#missing', 'missing.md')
        self.assertTrue(any("broken local reference" in e for e in check(self.root)[0]))

    def test_missing_owner_and_entry_point(self):
        self.replace('[Owner](owner.md#contract)', 'TBD')
        (self.root/".github/pull_request_template.md").unlink()
        errors = check(self.root)[0]
        self.assertTrue(any("canonical local Markdown owner" in e for e in errors))
        self.assertTrue(any("required entry point missing" in e for e in errors))

    def test_template_links_are_checked(self):
        (self.root/".github/pull_request_template.md").write_text('[Guide](../missing.md)')
        self.assertTrue(any("pull_request_template.md: broken" in e for e in check(self.root)[0]))

    def test_deprecated_opencode_fallback_requires_audit(self):
        (self.root/"legacy").mkdir()
        (self.root/"legacy/CONTEXT.md").write_text('legacy instructions')
        errors = check(self.root)[0]
        self.assertEqual(len(errors), 1)
        self.assertIn('legacy/CONTEXT.md: instruction file missing from audited chains', errors[0])

    def test_unlisted_instruction_and_vendor_exclusion(self):
        (self.root/"src").mkdir()
        (self.root/"src/AGENTS.md").write_text('nested')
        vendor = self.root/".agents/skills/example"
        vendor.mkdir(parents=True)
        (vendor/"AGENTS.md").write_text('third-party '*10000)
        errors = check(self.root)[0]
        self.assertEqual(len(errors), 1)
        self.assertIn('src/AGENTS.md', errors[0])

    def test_escape_and_wrong_directory_chain(self):
        self.replace('[Owner](owner.md#contract)', '[Owner](../../outside.md)')
        self.assertTrue(any("stay inside" in e for e in check(self.root)[0]))
        (self.root/"src").mkdir()
        (self.root/"src/AGENTS.md").write_text('nested')
        self.replace('[Root](../AGENTS.md)', '[Root](../AGENTS.md), [Sibling](../src/AGENTS.md)')
        self.assertTrue(any("not an instruction on this directory chain" in e for e in check(self.root)[0]))

    def test_missing_map_has_actionable_diagnostic(self):
        self.replace('<!-- context-map:end -->', '')
        self.assertTrue(any('context-map: expected' in e for e in check(self.root)[0]))

    def test_symlink_outside_repository_is_not_read(self):
        with TemporaryDirectory() as outside:
            private = Path(outside)/"secret"
            private.write_text("private instruction text")
            entry = self.root/"AGENTS.md"
            entry.unlink()
            entry.symlink_to(private)
            errors, _ = check(self.root)
            self.assertTrue(any("escapes repository" in e for e in errors))
            self.assertFalse(any("private instruction text" in e for e in errors))
