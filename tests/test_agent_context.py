"""Dependency-free context checker tests: temporary trees, no product imports."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts.check_agent_context import CHAIN_BUDGET, REQUIRED, check

REAL_ROOT = Path(__file__).resolve().parents[1]
SHA = "a"*40


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
        # Skills allowlist errors are covered by VendoredSkillsTests; this test
        # owns the instruction-walk exclusion of vendored trees.
        errors = [e for e in check(self.root)[0] if 'instruction file' in e]
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


class VendoredSkillsTests(TestCase):
    """The curated skills tree is an allowlisted, pinned subset (issue #458)."""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in REQUIRED:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("content\n", encoding="utf-8")
        (self.root/"docs/agent-workflow.md").write_text(
            "<!-- context-map:start -->\n| C | O | B | E |\n|---|---|---|---|\n"
            "| x | [O](o.md) | [B](o.md) | [E](o.md) |\n<!-- context-map:end -->\n"
            "<!-- instruction-chains:start -->\n| D | C |\n|---|---|\n"
            "| `.` | [R](../AGENTS.md) |\n<!-- instruction-chains:end -->\n")
        (self.root/"docs/o.md").write_text("owner\n")
        skills = self.root/".agents/skills"
        (skills/"qdrant-sizing").mkdir(parents=True)
        (skills/"qdrant-sizing/SKILL.md").write_text("vendor\n")
        (skills/"index.md").write_text("[sizing](qdrant-sizing/SKILL.md)\n")
        (self.root/".agents/qdrant-skills-provenance.md").write_text(
            f"pin `{SHA}`\n<!-- skills-allowlist:start -->\n- qdrant-sizing\n<!-- skills-allowlist:end -->\n")
        (self.root/"NOTICE.qdrant-skills").write_text(f"commit {SHA}\n")
        (self.root/"LICENSE.qdrant-skills").write_text("Apache-2.0\n")

    def test_curated_tree_is_valid(self):
        self.assertEqual(check(self.root)[0], [])

    def test_excluded_skill_cannot_be_reintroduced_by_pin_refresh(self):
        for name in ("qdrant-model-migration", "qdrant-search-quality", "qdrant-edge"):
            with self.subTest(name=name):
                (self.root/".agents/skills"/name).mkdir()
                errors = check(self.root)[0]
                self.assertTrue(any(name in e and "allowlist" in e for e in errors), errors)
                (self.root/".agents/skills"/name).rmdir()

    def test_allowlisted_skill_must_exist_and_index_links_must_resolve(self):
        (self.root/".agents/skills/qdrant-sizing/SKILL.md").unlink()
        (self.root/".agents/skills/qdrant-sizing").rmdir()
        errors = check(self.root)[0]
        self.assertTrue(any("allowlisted skill qdrant-sizing missing" in e for e in errors))
        self.assertTrue(any("broken local reference" in e for e in errors))

    def test_pin_notice_and_license_must_agree(self):
        (self.root/"NOTICE.qdrant-skills").write_text("commit "+"b"*40+"\n")
        self.assertTrue(any("same pinned SHA" in e for e in check(self.root)[0]))
        (self.root/"LICENSE.qdrant-skills").unlink()
        self.assertTrue(any("LICENSE.qdrant-skills" in e for e in check(self.root)[0]))

    def test_repository_skills_tree_matches_its_allowlist(self):
        self.assertEqual([e for e in check(REAL_ROOT)[0] if "skill" in e.lower()], [])

    def add_engineering_provider(self):
        pin = "b" * 40
        (self.root/".agents/mattpocock-skills-provenance.md").write_text(
            f"pin `{pin}`\n<!-- skills-allowlist:start -->\n- tdd\n<!-- skills-allowlist:end -->\n")
        (self.root/"NOTICE.mattpocock-skills").write_text(f"commit {pin}\n")
        (self.root/"LICENSE.mattpocock-skills").write_text("MIT\n")
        wrapper = self.root/".agents/skills/tdd/SKILL.md"
        wrapper.parent.mkdir()
        wrapper.write_text("[Upstream](../../vendor/mattpocock-skills/tdd/SKILL.md)\n")
        upstream = self.root/".agents/vendor/mattpocock-skills/tdd/SKILL.md"
        upstream.parent.mkdir(parents=True)
        upstream.write_text("original upstream skill\n")
        with (self.root/".agents/skills/index.md").open("a") as index:
            index.write("[TDD](tdd/SKILL.md)\n")

    def test_independently_pinned_providers_can_share_the_skills_directory(self):
        self.add_engineering_provider()
        self.assertEqual(check(self.root)[0], [])

    def test_second_provider_does_not_permit_unselected_categories(self):
        self.add_engineering_provider()
        for name in ("qdrant-edge", "research"):
            with self.subTest(name=name):
                (self.root/".agents/skills"/name).mkdir()
                self.assertTrue(any(name in e and "allowlist" in e for e in check(self.root)[0]))
                (self.root/".agents/skills"/name).rmdir()

    def test_provider_pin_and_license_checks_remain_independent(self):
        self.add_engineering_provider()
        (self.root/"NOTICE.mattpocock-skills").write_text(f"commit {SHA}\n")
        (self.root/"LICENSE.mattpocock-skills").unlink()
        errors = check(self.root)[0]
        self.assertTrue(any("mattpocock-skills" in e and "same pinned SHA" in e for e in errors))
        self.assertTrue(any("LICENSE.mattpocock-skills" in e for e in errors))
        self.assertFalse(any("NOTICE.qdrant-skills" in e for e in errors))

    def test_provider_allowlists_cannot_claim_the_same_skill(self):
        self.add_engineering_provider()
        record = self.root/".agents/mattpocock-skills-provenance.md"
        record.write_text(record.read_text().replace("- tdd", "- tdd\n- qdrant-sizing"))
        self.assertTrue(any("qdrant-sizing" in e and "multiple provider" in e for e in check(self.root)[0]))

    def test_repository_entry_point_must_resolve_its_local_upstream_reference(self):
        self.add_engineering_provider()
        (self.root/".agents/vendor/mattpocock-skills/tdd/SKILL.md").unlink()
        self.assertTrue(any("tdd/SKILL.md" in e and "broken local reference" in e for e in check(self.root)[0]))

    def test_repository_engineering_vendor_bytes_match_the_pinned_manifest(self):
        import hashlib

        manifest = REAL_ROOT/".agents/mattpocock-skills.sha256"
        recorded = {path: digest for digest, path in (line.split("  ", 1) for line in manifest.read_text().splitlines())}
        vendor = REAL_ROOT/".agents/vendor/mattpocock-skills"
        actual = {p.relative_to(REAL_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in vendor.rglob("*") if p.is_file()}
        self.assertTrue(recorded)
        self.assertEqual(actual, recorded)
