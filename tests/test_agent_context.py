"""Dependency-free context checker tests: temporary trees, no product imports."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

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
| example | [Owner](owner.md#contract) | [Source](../Taskfile.yml) | [Tests](owner.md) |
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
        self.assertTrue(any("pull_request_template.md: template links must use canonical" in e for e in check(self.root)[0]))

    def test_template_canonical_url_valid(self):
        canonical = "https://github.com/yonatan895/qdrant-pdf-rag/blob/main/docs/owner.md#contract"
        (self.root/".github/pull_request_template.md").write_text(f'[Owner]({canonical})')
        (self.root/".github/ISSUE_TEMPLATE/agent-task.md").write_text(f'[Owner]({canonical})')
        self.assertEqual(check(self.root)[0], [])

    def test_template_canonical_missing_file_and_anchor(self):
        base = "https://github.com/yonatan895/qdrant-pdf-rag/blob/main/"
        (self.root/".github/pull_request_template.md").write_text(f'[Missing]({base}missing.md)')
        self.assertTrue(any("broken canonical reference" in e for e in check(self.root)[0]))
        (self.root/".github/pull_request_template.md").write_text(
            f'[Missing]({base}docs/owner.md#missing)')
        self.assertTrue(any("missing explicit anchor" in e for e in check(self.root)[0]))

    def test_template_rejects_old_relative_form_but_docs_allow_relative(self):
        # Ordinary documentation-relative links remain supported.
        (self.root/"AGENTS.md").write_text('[Owner](docs/owner.md#contract)\n')
        self.assertEqual(check(self.root)[0], [])
        # The same file-relative form is rejected inside GitHub-facing templates.
        (self.root/".github/pull_request_template.md").write_text('[Owner](../docs/owner.md#contract)')
        errors = check(self.root)[0]
        self.assertTrue(any("template links must use canonical" in e for e in errors))
        # Canonical escape must not read outside the repository.
        evil = "https://github.com/yonatan895/qdrant-pdf-rag/blob/main/../outside.md"
        (self.root/".github/pull_request_template.md").write_text(f'[Evil]({evil})')
        self.assertTrue(any("stay inside" in e for e in check(self.root)[0]))
        # Unrelated external links in templates remain skipped without network.
        (self.root/".github/pull_request_template.md").write_text('[Ext](https://example.com/docs/guide)')
        self.assertEqual(check(self.root)[0], [])

    def test_deprecated_opencode_fallback_requires_audit(self):
        (self.root/"legacy").mkdir()
        (self.root/"legacy/CONTEXT.md").write_text('legacy instructions')
        errors = check(self.root)[0]
        self.assertEqual(len(errors), 1)
        self.assertIn('legacy/CONTEXT.md: instruction file missing from audited chains', errors[0])

    def test_template_repository_urls_require_expected_host_repo_ref_and_scheme(self):
        for target in (
            'https://example.org/yonatan895/qdrant-pdf-rag/blob/main/docs/owner.md#contract',
            'https://github.com/yonatan895/other/blob/main/docs/owner.md#contract',
            'https://github.com/yonatan895/qdrant-pdf-rag/blob/other/docs/owner.md#contract',
            'http://github.com/yonatan895/qdrant-pdf-rag/blob/main/docs/owner.md#contract',
            'https://github.com/yonatan895/qdrant-pdf-rag/blob/main/docs/owner.md?raw=1#contract',
        ):
            with self.subTest(target=target):
                (self.root/'.github/pull_request_template.md').write_text(f'[Guide]({target})')
                self.assertTrue(any('expected canonical repository URL' in e for e in check(self.root)[0]))
        (self.root/'.github/pull_request_template.md').write_text(
            '[Empty](https://github.com/yonatan895/qdrant-pdf-rag/blob/main/)')
        self.assertTrue(any('must name a repository-relative file' in e for e in check(self.root)[0]))

    def test_canonical_urls_cannot_read_outside_repository(self):
        with TemporaryDirectory() as outside:
            private = Path(outside)/'secret.md'
            private.write_text('<a id="private"></a>\nPRIVATE-CONTENT')
            (self.root/'docs/link.md').symlink_to(private)
            original = Path.read_bytes

            def guarded_read(path):
                self.assertNotEqual(path.resolve(), private)
                return original(path)

            with patch.object(Path, 'read_bytes', guarded_read):
                for target in ('docs/link.md#private', '../outside.md', '%2e%2e/outside.md'):
                    with self.subTest(target=target):
                        (self.root/'.github/pull_request_template.md').write_text(
                            '[Guide](https://github.com/yonatan895/qdrant-pdf-rag/blob/main/'+target+')')
                        errors = check(self.root)[0]
                        self.assertTrue(any('stay inside' in e for e in errors))
                        self.assertFalse(any('PRIVATE-CONTENT' in e for e in errors))

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
