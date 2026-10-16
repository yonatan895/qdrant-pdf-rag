# Test justification ledger (record)

Status: dated record for issue #769 (PR #770), 2026-10-16. Owner: [testing contract](../testing.md).

Each row is one test function kept after the #769 simplification. The record states the class, the
guarantee that would be lost if the test were removed, and the owning contract. Parametrized cases
share their function's row. The suite went from 6,466 to 3,587 collected items in #770, and to
3,660 after the guarantee restoration in the addendum. Classes:

- `core`: product behavior or data integrity;
- `infra`: deployment, CI-gate or tooling contract;
- `noise-kept`: timing- or wording-sensitive, kept because it protects a stated contract;
- `gate-control`: a negative control or gate-integrity test for a test or CI tool.

This is a snapshot, not a working rule. New or changed tests justify themselves in their own PR.

| Class | Functions |
|---|---:|
| `core` | 1383 |
| `infra` | 885 |
| `noise-kept` | 27 |
| `gate-control` | 127 |
| total | 2422 |

## `tests/test_acceptance.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_criteria_reject_incoherent_documents` | core | Registration without who and when, non-production certifying profiles, or invalid metrics and pairings load as valid criteria. | issue #367 (docs/eval.md) |
| `test_valid_set_loads_and_hash_identifies_bytes` | core | A valid set does not load or its hash no longer identifies the adjudicated bytes. | issue #367 (docs/eval.md) |
| `test_validator_rejects` | core | Release-set defects (author is adjudicator, missing evidence, duplicate queries, tuning-loop independence, extra fields, missing manifest) are accepted into an acceptance set. | issue #367 (docs/eval.md) |
| `test_overlap_with_tuning_queries_is_detected` | core | Release-set queries that overlap tuning queries are not detected, contaminating the independent set. | issue #367 (docs/eval.md) |
| `test_release_set_refused_without_rc` | core | The independent release set is readable outside the RC venue. | issue #367 (docs/eval.md) |
| `test_release_set_pin_failures_refuse` | core | A release set with a wrong, misnamed or missing pin loads, or does not recover after repair. | issue #367 (docs/eval.md) |
| `test_golden_readers_never_open_the_release_set_even_under_rc` | core | Golden readers open the independent set (directly or via symlink), leaking it into tuning. | issue #367 (docs/eval.md) |
| `test_renamed_copy_is_caught_by_record_guard` | core | A renamed copy of the release set is parsed as a golden set. | issue #367 (docs/eval.md) |
| `test_pilot_all_pass_registered_production_is_accepted` | core | A fully passing registered production pilot is not accepted. | issue #367 (docs/eval.md) |
| `test_pilot_boundary_20_of_24_passes_19_fails` | core | The pre-registered 20/24 boundary shifts, accepting or rejecting wrongly. | issue #367 (docs/eval.md) |
| `test_any_fabrication_or_critical_failure_rejects` | core | A single fabricated instruction or critical failure does not reject. | issue #367 (docs/eval.md) |
| `test_missing_outcomes_cannot_pass_by_omission` | core | Dropping outcomes lets a run pass. | issue #367 (docs/eval.md) |
| `test_skipped_error_and_incomplete_rows_count_as_failures` | core | Skipped, errored or incomplete rows are excluded from the denominator. | issue #367 (docs/eval.md) |
| `test_false_refusal_is_a_usefulness_miss` | core | A refusal on an answerable case is not counted as a usefulness miss. | issue #367 (docs/eval.md) |
| `test_coverage_criteria_make_missing_classes_insufficient_never_pass` | core | A set missing a required query class can pass. | issue #367 (docs/eval.md) |
| `test_standin_and_proposed_are_never_certifying` | core | Stand-in profiles or proposed criteria certify acceptance. | issue #367 (docs/eval.md) |
| `test_bindings_are_enforced` | core | Outcomes scored against a different set or criteria hash are accepted. | issue #367 (docs/eval.md) |
| `test_run_corpus_and_outcome_reviewers_are_bound_to_set` | core | Outcomes from a different corpus revision or undeclared reviewer are accepted. | issue #367 (docs/eval.md) |
| `test_unexecuted_critical_probe_cannot_certify` | core | A skipped critical probe still certifies. | issue #367 (docs/eval.md) |
| `test_critical_case_requires_predeclared_expected_safe_outcome` | core | Critical probes without a pre-declared safe outcome load. | issue #367 (docs/eval.md) |
| `test_critical_probe_needs_explicit_assessment` | core | Unassessed or unsafe critical probes do not block acceptance. | issue #367 (docs/eval.md) |
| `test_critical_probe_passes_only_its_declared_expected_state` | core | The adjudicator flag overrides an inconsistent observed state on a critical probe. | issue #367 (docs/eval.md) |
| `test_missing_critical_outcome_is_unassessed` | core | A missing critical outcome is treated as assessed. | issue #367 (docs/eval.md) |
| `test_structural_outcome_defects_refuse` | core | Duplicate, unknown-case, null-field, non-finite or incomplete-profile outcomes are scored. | issue #367 (docs/eval.md) |
| `test_wilson_bound_matches_reference_values` | core | The Wilson bound used for statistical claims is wrong against reference values. | issue #367 (docs/eval.md) |
| `test_single_repeat_binary_wilson_claim_remains_supported` | core | A single-repeat binary Wilson claim is mislabelled or rejected. | issue #367 (docs/eval.md) |
| `test_release_stage_uses_wilson_bound_not_point_estimate` | core | Release stage passes on point estimates instead of the Wilson bound and mislabels the claim. | issue #367 (docs/eval.md) |
| `test_release_stage_below_minimum_n_is_insufficient` | core | A release run with too few cases passes. | issue #367 (docs/eval.md) |
| `test_flip_rate_detects_unstable_cases` | core | Cases that flip across repeats are not counted, hiding instability. | issue #367 (docs/eval.md) |
| `test_repeat_count_must_match_registration` | core | A run with a different repeat count than pre-registered is scored. | issue #367 (docs/eval.md) |
| `test_cli_scores_and_writes_report` | core | The acceptance CLI does not score outcomes, write the report or validate a set. | issue #367 (docs/eval.md) |
| `test_cli_refuses_in_dev_and_on_tuning_overlap` | core | The CLI runs outside the RC venue or with a tuning overlap. | issue #367 (docs/eval.md) |
| `test_real_acceptance_script_outside_repo_still_detects_known_query` | infra | The shipped acceptance script outside the repo stops detecting overlap with known dev queries. | issue #367 (docs/eval.md) |
| `test_real_acceptance_script_refuses_unavailable_required_comparison` | infra | The shipped script validates a set when a required comparison dataset is missing, empty or invalid. | issue #367 (docs/eval.md) |

## `tests/test_agent_api.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_search_returns_cite_fields_without_calling_llm` | core | /v1/search could call the LLM or return hits without cite fields and a request id, breaking no-LLM search clients. | docs/agent.md#http-model-contract |
| `test_answer_validates_citations_and_script` | core | A fabricated citation could reach the client or the script and its language be lost. | docs/agent.md#answer-contract |
| `test_answer_surfaces_inferred_citation_provenance` | core | Bracket-only citations would look explicit, so clients and evals cannot tell inferred from model-listed cites. | #269 |
| `test_answer_refuses_without_reasoning_model` | core | /v1/answer without a configured reasoning model would fail opaquely instead of the stable 503 not_configured. | docs/agent.md#http-model-contract |
| `test_answer_strips_fabricated_body_citation` | core | A citation line quoted mid-answer that was never retrieved would reach the client as if supported. | docs/agent.md#answer-contract |
| `test_answer_rejects_retrieved_but_unsupplied_citation` | core | A retrieved hit dropped by budget packing could be cited though the model never saw it. | #364 |
| `test_answer_stream_rejects_retrieved_but_unsupplied_citation` | core | The SSE final could accept an unsupplied citation that the JSON path rejects. | #364 |
| `test_strip_unauthorized_removes_fabricated_cites_in_every_wrapping` | core | Wrapping a fabricated cite in markup, list markers or links would smuggle it past the body strip. | docs/agent.md#answer-contract |
| `test_strip_unauthorized_leaves_prose_and_inline_mentions` | core | Legitimate prose and inline doc-number mentions would be deleted, while pasted heading paths would remain as fake references. | docs/agent.md#answer-contract |
| `test_answer_script_block_passes_through_unvalidated` | core | Citation-shaped lines inside a code fence would be altered or stripped, corrupting returned scripts. | docs/agent.md#answer-contract |
| `test_healthz_degraded_paths_leak_no_upstream_text` | core | A degraded /healthz would forward Qdrant or embed response bodies and exception text to callers. | docs/agent.md#serving-contract |
| `test_router_and_http_exception_errors_use_fixed_envelopes` | core | Developer text or non-stable shapes would leak in 404, 405 and HTTPException responses. | docs/agent.md#http-model-contract |
| `test_unhandled_error_returns_internal_shape` | core | An escaping exception would expose its message instead of the fixed internal envelope. | docs/agent.md#http-model-contract |
| `test_healthz_qdrant_unready_structured` | core | An unreachable Qdrant would show raw connection text instead of the stable qdrant_unready envelope that probes rely on. | docs/agent.md#serving-contract |
| `test_healthz_representation_drift_degrades` | core | A drifted stored generation would report ready and serve wrong-representation vectors. | #391 |
| `test_healthz_representation_legacy_unknown_and_pending_degrade` | core | Legacy, unreadable or pending-migration stores would read as ready, or leak store exception text. | #391 |
| `test_lifespan_refuses_pending_contract` | core | The agent would start and serve queries against a half-migrated generation before a probe notices. | #391 |
| `test_unservable_generation_refuses_retrieval_with_fixed_503` | core | Retrieval would run against a legacy, unreadable or empty generation, or liveness would restart a healthy pod over a data problem. | #391 |
| `test_gate_degrades_after_metadata_drift_and_recovers` | core | Readiness and retrieval would not follow metadata drift or repair without an agent restart. | #391 |
| `test_answer_chat_and_stream_refused_before_core` | core | A non-servable generation could produce a 200 SSE stream that dies later instead of the same 503 JSON on every conversational path. | #391 |
| `test_request_binds_validated_physical_and_alias_switch_revalidates` | core | An alias swap could redirect an in-flight request to an unvalidated collection or the alias name itself be queried. | #391 |
| `test_healthz_embed_probe_forwards_gateway_key` | core | The /healthz embed probe would omit the embed leg key and report the gateway unready, or the pinned ok body shape would drift. | docs/agent.md#http-model-contract |
| `test_healthz_rerank_up_or_alternate_route_is_ready` | core | A healthy reranker, or one served via the alternate route, would be reported unready and pull the pod from service. | docs/agent.md#serving-contract |
| `test_healthz_rerank_all_routes_down_is_unready_without_upstream_text` | core | A dead reranker would pass readiness, or upstream text would leak into the body or logs. | docs/agent.md#serving-contract |
| `test_healthz_rerank_disabled_is_null_and_unaffected` | core | A disabled reranker would still be probed or affect readiness. | docs/agent.md#serving-contract |
| `test_healthz_rerank_probe_timeout_is_unready` | noise-kept | A hung reranker would block /healthz indefinitely instead of reporting rerank unready within the bounded timeout. | docs/agent.md#serving-contract |
| `test_healthz_rerank_probe_is_ttl_cached` | core | Every kubelet tick would hit the reranker GPU, and failures would not be cached or re-probed after expiry. | docs/agent.md#serving-contract |
| `test_rerank_recovery_restores_readiness_and_next_search` | core | After the reranker recovers, readiness or the next search would stay failed. | docs/agent.md#serving-contract |
| `test_error_shape_is_structured` | core | Retrieval failures or validation errors would reach clients as stack traces or unstable shapes. | docs/agent.md#http-model-contract |
| `test_blank_or_control_query_refused_before_any_work` | core | Blank or control-character queries would consume embedding, Qdrant and LLM work instead of the fixed 422. | #579 |
| `test_padded_real_query_is_searched_unmodified` | core | Padded real queries would be rejected or normalized, changing identifier search semantics. | #579 |
| `test_answer_retrieval_failure_reads_retrieval_failed` | core | A retrieval fault on /v1/answer would read differently from /v1/search or leak upstream text. | docs/agent.md#http-model-contract |
| `test_answer_llm_failure_reads_answer_failed` | core | A model failure would be mislabeled as a retrieval fault or leak the exception message. | docs/agent.md#http-model-contract |
| `test_prompt_build_failure_maps_to_internal` | core | A local prompt-construction bug would be reported as an upstream 502 and leak internals. | docs/agent.md#http-model-contract |
| `test_parse_answer_shape_and_rejected_attempt_telemetry` | core | Fabricated or malformed citations would be accepted, or rejection counters used by eval would be wrong or lost on abstention. | #299 |
| `test_parse_answer_bracketed_fallback` | core | Bracket markers would resolve to the wrong excerpt, or parenthetical noise like z/OS (3.1) would become citations. | #364 |
| `test_parse_answer_refusal_zeroes_citations` | core | A correct refusal that lists citations would look grounded while grounding nothing. | #135 |
| `test_parse_answer_grounded_answer_with_hedging_sentence_keeps_citations` | core | A grounded answer opening with a hedging sentence, or a mixed answer, would lose its real citations. | #135 |
| `test_parse_answer_citations_positions_and_case` | core | Top-placed, mid-placed or upper-case Citations blocks would eat answer prose or be missed. | docs/agent.md#answer-contract |
| `test_parse_answer_code_fence_and_script_extraction` | core | Scripts would not be extracted or thinking fences would leak, and prose fences would not unwrap. | docs/agent.md#answer-contract |
| `test_parse_answer_script_lang` | core | The script language tag would be lost, normalized wrongly or lost on abstention. | #336 |
| `test_build_messages_context_budgeting` | core | Oversized chunks or total context would overrun the prompt window or the complex budget would not shrink. | docs/agent.md#answer-contract |
| `test_classify_query_complexity` | core | Queries would get the wrong reasoning effort and context budget, truncating hard questions or wasting budget on simple ones. | docs/agent.md#answer-contract |
| `test_answer_dispatches_reasoning_effort_by_complexity` | core | The configured reasoning effort, temperature and complex prompt extension would not reach the model by query complexity. | docs/agent.md#http-model-contract |
| `test_answer_configured_xhigh_reaches_model` | core | A configured xhigh effort would be dropped on the answer routes, JSON or SSE. | docs/agent.md#http-model-contract |
| `test_answer_accepts_per_request_temperature_override` | core | Deterministic eval sampling via the temperature override would be ignored, or omission would not keep the setting. | #596 |
| `test_httpx_llm_client_passes_reasoning_params` | core | The reasoning effort, model and temperature would not be sent to the real model endpoint. | docs/agent.md#http-model-contract |
| `test_parse_answer_citation_block_forms` | core | Heading-style or header-less citation blocks would leak into the answer or be missed, and trailing prose would be eaten. | docs/agent.md#answer-contract |
| `test_parse_answer_bibliography_keeps_exact_cites_before_unmapped_tail` | core | Exact citations would be lost when followed by omitted or fabricated bibliography lines, for both doc-number and filename-stem ids. | #364 |
| `test_parse_answer_filename_stem_citation_block` | core | Filename-stem documents would lose their only valid citation and leak cite lines into the answer. | docs/agent.md#answer-contract |
| `test_parse_answer_filename_stem_body_citation_hygiene` | core | Fabricated filename-stem cites in the body would reach clients, or unrelated prose would be stripped. | docs/agent.md#answer-contract |
| `test_build_messages_preserves_syntax_and_message_fidelity` | core | Syntax chunks would be truncated by the narrative cap, losing operands needed for correct commands. | docs/agent.md#answer-contract |
| `test_answer_audit_log_with_chat_result` | core | Token usage and citation-attempt counters would be missing from the audit log that evals and operators read. | #299 |
| `test_answer_alert_on_non_stop_finish_reason` | core | A truncated generation would not raise the non-stop alert operators watch. | docs/agent.md#http-model-contract |
| `test_build_messages_tokenizer_budget_respected` | core | Token-planned prompts could exceed the model window and fail at the model. | docs/agent.md#answer-contract |
| `test_build_messages_complex_budget_prices_thinking_reserve` | core | Complex prompts would not reserve thinking tokens, leaving no room for the answer. | #298 |
| `test_prompt_budget_prices_simple_reasoning_without_changing_complex` | core | Simple-query reasoning reserve would be unpriced, or change complex budgeting, on single-turn and chat prompts. | docs/agent.md#answer-contract |
| `test_as_chat_result_accepts_only_structured_completions` | core | A bare-string model return would be accepted without finish reason or usage, hiding truncation. | #365 |
| `test_chat_content_none_becomes_empty_string` | core | A model returning null content would surface the string None to users. | docs/agent.md#http-model-contract |
| `test_server_timing_headers_emitted_on_search_and_answer` | core | The L3 harness and operators lose per-stage timings in Server-Timing. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_httpx_llm_client_streaming_measures_ttft_on_first_content_token` | core | Time to first token would be wrong or content and usage lost in streaming mode. | docs/agent.md#http-model-contract |
| `test_httpx_llm_client_streaming_falls_back_to_post_on_empty_or_failed_stream` | core | An empty or dropped stream would end with no answer instead of recovering through the non-streaming POST. | docs/agent.md#http-model-contract |
| `test_v1_answer_streaming_sse_token_deltas_and_final_event` | core | SSE clients lose token deltas, the single final event or its citations, script and usage. | docs/agent.md#answer-contract |
| `test_v1_answer_stream_query_param_and_body_precedence` | core | Streaming would be selected by the wrong source, so clients get JSON when they asked for SSE or the reverse. | docs/agent.md#http-model-contract |
| `test_v1_answer_empty_hits_streaming` | core | Empty retrieval over SSE would call the model or emit a final with a different schema. | docs/agent.md#answer-contract |
| `test_v1_answer_empty_hits_identifier_names_codes_json_and_stream` | core | A mistyped identifier would get generic guidance instead of the named code, differing between JSON and SSE. | #132 |
| `test_empty_hits_answer_names_only_bounded_codes` | core | A code-salad or trap query would be echoed unbounded or leak excerpt text. | #132 |
| `test_sse_final_schemas_match_across_paths` | core | Empty and normal finals, error frames and SSE framing would drift apart so clients mis-parse one path. | docs/agent.md#answer-contract |
| `test_v1_answer_20_concurrent_requests_no_threadpool_starvation` | core | Concurrent answers would serialize on a worker pool or the loop, starving the service under load. | docs/agent.md#http-model-contract |
| `test_httpx_llm_client_chat_stream_async` | core | Async streaming deltas, ttft, finish reason or usage would be lost. | docs/agent.md#http-model-contract |
| `test_httpx_llm_client_chat_stream_empty_content_recovers_via_post` | core | A reasoning-only stream would produce an empty answer instead of recovering via POST without duplicating tokens. | docs/agent.md#http-model-contract |
| `test_overlong_query_fails_closed_before_retrieval` | core | Overlong queries would reach retrieval and the model instead of the 422 envelope. | #87 |
| `test_query_cap_is_settings_driven` | core | The operator query-length cap would be ignored in favor of a hardcoded limit. | #87 |
| `test_answer_rejects_out_of_range_temperature` | core | Out-of-range temperatures would surface as upstream 502s instead of client 422s. | #596 |
| `test_answer_temperature_rejects_non_finite` | core | NaN or infinity would be accepted by the request model and forwarded to the model. | #596 |
| `test_answer_accepts_boundary_temperature` | core | The documented temperature bounds would be rejected at their edges. | #596 |
| `test_build_messages_bounds_splunk_context` | core | Caller-supplied telemetry would starve excerpts out of the window, or bounded context would be needlessly truncated. | #87 |
| `test_metrics_enabled_serves_prometheus_exposition` | core | Enabled metrics would not be scrapeable by UWM. | #187 |
| `test_lifespan_reranker_unreachable_warns_but_still_listens` | core | A dead optional reranker would stop the agent from starting or fail silently without a startup warning. | docs/agent.md#serving-contract |
| `test_lifespan_zowe_probe_warning_leaks_no_exception_text` | core | A failed Zowe bridge probe would write exception text or secrets into startup logs. | AGENTS.md#hard-boundaries |
| `test_prompt_build_runs_off_the_event_loop` | core | A slow synchronous tokenizer verify would stall every in-flight request. | #269 |
| `test_answer_accepted_json_and_sse_agree` | core | A valid cited answer would not be marked accepted, or the SSE final would differ from JSON. | docs/agent.md#answer-contract |
| `test_answer_unverified_draft_states` | core | Uncited or bracket-only answers would be presented as accepted, or an uncited script would skip review. | #365 |
| `test_answer_abstention_is_insufficient_evidence` | core | A refusal would carry citations or a refusal-with-script would lose its review flag. | #365 |
| `test_answer_length_finish_is_generation_incomplete_json_and_sse` | core | A length-truncated generation would read as accepted guidance. | #365 |
| `test_answer_premise_correction_keeps_citations` | core | A cited false-premise correction would be misread as an abstention and lose its citations. | #365 |
| `test_verification_state_for_matrix` | core | The precedence between refusal, unfinished generation and citation outcome would drift so incomplete answers read as accepted. | #365 |
| `test_answer_empty_generation_is_incomplete` | core | An empty model generation would read as a draft or accepted answer. | #365 |
| `test_v1_answer_json_success_records_ttft_and_llm_model` | core | RED metrics would lose ttft and model labels for successful answers. | #401 |
| `test_metrics_scrape_failure_serves_stable_503` | core | A failing metrics collector would leak exception details to scrapers. | #187 |
| `test_awaited_retrieval_async_error_maps_to_upstream_error` | core | Async retrieval failures would leak text or skip RED metrics on search, answer and chat. | #370 |
| `test_awaited_retrieval_cancellation_propagates` | core | Request cancellation would be swallowed or converted into an error. | #370 |
| `test_lifespan_startup_refusal_closes_created_clients_once` | core | A startup refusal would leak pools and Qdrant clients until process exit. | #369 |
| `test_lifespan_cancellation_at_yield_closes_created_clients_once` | core | Cancellation while serving would skip cleanup, and the next lifespan would not start on fresh clients. | #369 |
| `test_lifespan_one_failing_close_does_not_skip_the_others` | core | One failing client close would leave later clients open. | #369 |
| `test_lifespan_closes_what_it_created_not_swapped_doubles` | core | Shutdown would close objects published by someone else instead of what the lifespan created. | #369 |
| `test_request_keeps_resources_it_captured_when_published_names_change` | core | A mid-request resource swap would mix retrieval and model views within one request. | #369 |
| `test_security_refusal_state_json_and_sse` | core | Secret-extraction refusals would keep citations or be mislabeled, and non-refusals would be zeroed, differently on JSON and SSE. | AGENTS.md#hard-boundaries |
| `test_mixed_answer_classification_is_independent_of_verbosity` | core | A supported answer with a missing-detail caveat would be zeroed or reclassified depending on surrounding prose length. | #630 |
| `test_is_abstention_whole_response_controls` | core | Refusal recognition would misclassify credential refusals, quoted text or refusals followed by instructions. | #630 |
| `test_answer_partial_and_interrupted_refusal_state_json_and_sse` | core | Partial and cut-off refusals would finalize differently on JSON and SSE. | #630 |
| `test_answer_json_and_sse_share_supplied_evidence_projection` | core | Clients could not audit the exact shipped excerpts, or JSON and SSE would differ. | #635 |
| `test_answer_supplied_evidence_keeps_markup_as_data_and_unknown_version` | core | Hostile excerpt markup would be altered, or unknown product and version replaced with the request scope. | #635 |
| `test_answer_same_display_citation_two_versions_stay_distinct` | core | Two versions sharing one display citation would collapse so users cannot tell which version supported an answer. | #635 |

## `tests/test_agent_context.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `ContextCheckTests::test_valid_minimal_layout` | gate-control | The context checker rejects a valid layout, blocking the qa:context gate. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_utf8_budget_counts_bytes` | gate-control | Multibyte or CRLF instruction files exceed the byte budget unnoticed. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_chain_includes_separators_and_client_limit` | gate-control | An instruction chain exceeds the client limit unnoticed. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_broken_local_reference_and_anchor` | gate-control | Broken contract links or anchors in the context map pass the gate. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_missing_owner_and_entry_point` | gate-control | A contract row with no owner or a missing required entry point passes. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_template_canonical_url_valid` | gate-control | Valid canonical URLs in GitHub templates are rejected. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_template_canonical_missing_file_and_anchor` | gate-control | A template link to a missing file or anchor passes. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_template_rejects_old_relative_form_but_docs_allow_relative` | gate-control | Relative links in templates, or traversal in canonical links, pass the gate. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_deprecated_opencode_fallback_requires_audit` | gate-control | An unaudited legacy instruction file is loaded by agents without review. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_template_repository_urls_require_expected_host_repo_ref_and_scheme` | gate-control | A template link to another host, repository, ref, scheme or raw query passes as canonical. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_canonical_urls_cannot_read_outside_repository` | gate-control | A canonical link follows a symlink or encoded traversal and reads private files into the output. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_unlisted_instruction_and_vendor_exclusion` | gate-control | An instruction file not in any audited chain passes, or vendored trees are mistaken for instructions. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_escape_and_wrong_directory_chain` | gate-control | A context link escaping the repository or a sibling instruction in the wrong chain passes. | docs/agent-workflow.md#context-map |
| `ContextCheckTests::test_symlink_outside_repository_is_not_read` | gate-control | A symlinked entry point reads private text outside the repository into diagnostics. | docs/agent-workflow.md#context-map |
| `VendoredSkillsTests::test_curated_tree_is_valid` | gate-control | The curated vendored skills tree is rejected, blocking qa:context. | AGENTS.md#hard-boundaries |
| `VendoredSkillsTests::test_excluded_skill_cannot_be_reintroduced_by_pin_refresh` | gate-control | A pin refresh reintroduces an excluded vendored skill unnoticed. | AGENTS.md#hard-boundaries |
| `VendoredSkillsTests::test_allowlisted_skill_must_exist_and_index_links_must_resolve` | gate-control | An allowlisted skill goes missing or an index link breaks without failing the gate. | AGENTS.md#hard-boundaries |
| `VendoredSkillsTests::test_pin_notice_and_license_must_agree` | gate-control | A vendored skill pin whose notice and license disagree passes. | AGENTS.md#hard-boundaries |
| `VendoredSkillsTests::test_repository_skills_tree_matches_its_allowlist` | gate-control | The real repository skills tree drifts from its allowlist. | AGENTS.md#hard-boundaries |
| `VendoredSkillsTests::test_independently_pinned_providers_can_share_the_skills_directory` | gate-control | Multiple independently pinned providers are rejected. | AGENTS.md#hard-boundaries |
| `VendoredSkillsTests::test_provider_pin_and_license_checks_remain_independent` | gate-control | One provider's bad pin or license is masked or blamed on another provider. | AGENTS.md#hard-boundaries |
| `VendoredSkillsTests::test_provider_allowlists_cannot_claim_the_same_skill` | gate-control | Two providers claim the same skill name unnoticed. | AGENTS.md#hard-boundaries |
| `VendoredSkillsTests::test_repository_entry_point_must_resolve_its_local_upstream_reference` | gate-control | A repository entry point references a missing vendored upstream file. | AGENTS.md#hard-boundaries |
| `VendoredSkillsTests::test_repository_engineering_vendor_bytes_match_the_pinned_manifest` | gate-control | Vendored skill bytes change without a pin-bump PR. | AGENTS.md#hard-boundaries |
| `ContextCheckTests::test_missing_map_marker_fails_the_check` | infra | Without it a deleted context-map marker makes the checker pass with no owners validated instead of failing the CI context gate. | docs/agent-workflow.md#context-map |

## `tests/test_agent_doctor.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `DoctorTests::test_task_runner_requires_verified_workspace_binary` | infra | The doctor accepts an unverified Task binary or executes one found on PATH. | docs/task-runner.md |
| `DoctorTests::test_helm_missing_foreign_or_tampered_is_rejected_without_execution` | infra | The doctor accepts or runs a foreign or tampered Helm. | docs/deploy.md#deployment-policy |
| `DoctorTests::test_prepared_ci_interpreter_does_not_require_local_venv` | infra | CI with a prepared interpreter fails the doctor for lacking .venv. | docs/task-runner.md |
| `DoctorTests::test_complete_inventory_failure_is_not_ready_despite_matching_named_versions` | infra | The environment passes the doctor with matching named versions but a wrong full package inventory. | docs/deploy.md#deployment-policy |
| `DoctorTests::test_missing_tools_and_environment` | infra | Missing docker, venv, Qdrant image or BM25 cache is reported ready for the sim profile. | docs/live-stack.md#verification-minimums |
| `DoctorTests::test_incompatible_python_gil_jit_and_package_version` | infra | A wrong Python version, free-threaded, JIT or drifted package passes the doctor. | AGENTS.md#hard-boundaries |
| `DoctorTests::test_optional_docker_unavailable_timeout_and_success` | infra | Docker probing reports the wrong status on failure or timeout, or uses the wrong host. | docs/live-stack.md#verification-minimums |
| `DoctorTests::test_exit_codes_and_redaction` | infra | The doctor reads or executes private files, leaks secrets, or returns the wrong exit code. | AGENTS.md#hard-boundaries |
| `DoctorTests::test_actual_probe_protocol_and_redaction` | infra | The interpreter probe accepts malformed output, runs without isolation, or leaks stderr. | AGENTS.md#hard-boundaries |
| `DoctorTests::test_missing_and_unsupported_config` | infra | A missing images.txt or unsupported Python requirement is reported ready. | docs/task-runner.md |
| `HelmPreparationTests::test_offline_archive_installs_verified_tool` | infra | The offline Helm archive does not install the verified tool. | docs/deploy.md#deployment-policy |
| `HelmPreparationTests::test_missing_or_tampered_archive_preserves_previous_tool` | infra | A missing or tampered Helm archive overwrites the previously installed tool. | docs/deploy.md#deployment-policy |
| `HelmPreparationTests::test_wrong_member_digest_never_executes_or_installs` | infra | A Helm archive member with the wrong digest is executed or installed. | docs/deploy.md#deployment-policy |
| `test_bm25_cache_checks_the_selected_revision_and_exact_bytes` | infra | The doctor accepts a BM25 cache for a different revision or with altered bytes. | docs/deploy.md#deployment-policy |
| `test_simulator_missing_image_never_starts_or_pulls` | infra | The doctor starts or pulls a Qdrant image when the prepared one is missing. | AGENTS.md#hard-boundaries |
| `DoctorTests::test_missing_or_tampered_cached_task_archive_is_not_ready` | infra | Without it the doctor reports Task ready when only the executable matches and the cached pinned archive is absent or tampered, so artifact packaging uses unattested bytes. | docs/agent-workflow.md#offline-check-format-and-prerequisite-diagnosis |
| `DoctorTests::test_deploy_profile_requires_its_inputs_and_a_cluster_cli` | infra | Without it the deploy profile reports ready with a missing oc/kubectl or missing air-gap inputs and never contacts a service. | docs/agent-workflow.md#offline-check-format-and-prerequisite-diagnosis |

## `tests/test_airgap_bootstrap_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_bootstrap_missing_sums_fails` | infra | Without it an extracted bundle with no checksum list is accepted. | docs/deploy.md#deployment-policy |
| `test_bootstrap_corrupt_checksum_fails` | infra | Without it a corrupted bundle member is copied into the workspace. | docs/deploy.md#deployment-policy |
| `test_bootstrap_tampered_sums_fails_signature` | infra | Without it a modified checksum list passes without signature failure. | docs/deploy.md#deployment-policy |
| `test_bootstrap_refuses_signed_bundle_without_third_party_notices` | infra | Without it a signed bundle missing the notices member is accepted. | issue 376 |
| `test_bootstrap_trusted_pub_mismatch_refuses` | infra | Without it SNEAKERNET_TRUSTED_PUB no longer pins the signer. | docs/deploy.md#deployment-policy |
| `test_signed_handoff_executes_workflow_with_shipped_model_modes` | infra | Without it the published-bundle handoff workflow, including shipped model modes and pull secret, is never executed end to end. | docs/testing.md#airgap-tier |
| `test_bootstrap_actual_runner_without_build_or_app_tools_and_rerun` | infra | Without it bootstrap needs tools absent in the gap, the clone or verified dist/ is wrong, or a rerun runs an untrusted executable or loses operator files. | docs/deploy.md#deployment-policy |
| `test_bootstrap_task_tampering_fails_before_install` | infra | Without it a tampered or mismatched Task archive is installed as the host tool. | docs/deploy.md#deployment-policy |
| `test_bootstrap_wrong_host_fails_before_install` | infra | Without it the linux-amd64 Task binary is installed on an unsupported host. | docs/deploy.md#deployment-policy |
| `test_bootstrap_refuses_modified_installer` | infra | Without it a modified installer script is executed during bootstrap. | issue 414 |
| `test_bootstrap_refuses_tracked_edit_then_rerun_passes_after_revert` | infra | Without it a tracked edit at the approved commit is accepted, operator work is touched, or the revert does not recover. | issue 414 |
| `test_bootstrap_refuses_index_flags_that_hide_edits_then_rerun_passes` | infra | Without it edits hidden by index flags pass bootstrap. | issue 414 |
| `test_bootstrapped_workspace_passes_then_refuses_in_the_real_guard` | infra | Without it the dist chain bootstrap leaves is not what the downstream checkout guard verifies. | issue 414 |
| `test_bootstrap_approved_upgrade_requires_explicit_checkout_preserves_operator_state` | infra | Without it a new bundle executes through the old workspace or an upgrade overwrites operator files. | tests/hazards/critical.json |
| `test_bootstrap_failed_copy_leaves_existing_dist_and_env_untouched` | infra | Without it a failed, killed or silently corrupting copy damages an accepted dist/ or the operator's airgap.env. | issue 414 |
| `test_bootstrap_killed_first_run_never_leaves_accepted_bundle` | infra | Without it a process killed mid-staging or mid-commit leaves dist/ looking like an accepted bundle. | issue 414 |
| `test_interrupted_promotion_warns_of_partial_dist_then_recovers_complete_bundle` | infra | Without it a partially promoted dist/ is used silently and recovery loses operator evidence. | issue 414 |

## `tests/test_airgap_deploy_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_packaging_pr_gate_checks_vendored_qdrant_chart` | infra | Without it the packaging PR job can stop running the vendored Qdrant chart lint and render checks and unvalidated chart changes merge. | docs/testing.md#airgap-tier |
| `test_pull_secret_wired_agent_render_keeps_mapping` | infra | Without it the pull secret is inserted at the wrong indent, kubectl rejects the manifest, or the placeholder pull secret reaches the cluster. | docs/deploy.md#deployment-policy |
| `test_model_budget_operator_precedence_reaches_rendered_pod` | infra | Without it explicit env or Task settings lose to stale file values and the pod runs different model budgets than the operator chose. | issue 402 |
| `test_pull_secret_bad_name_fails_closed` | infra | Without it a sed-active PULL_SECRET value corrupts the deployed manifest. | docs/deploy.md#configuration-contract |
| `test_agent_qdrant_write_key_fails_closed` | infra | Without it a render that wires the full-access Qdrant key into the serving agent is applied. | AGENTS.md#hard-boundaries (issue 366) |
| `test_two_qdrant_charts_refuse_before_any_command` | infra | Without it deploy picks one of several vendored Qdrant charts arbitrarily. | issue 272 |
| `test_manifest_chart_sha_mismatch_refuses_before_mutation` | infra | Without it an altered vendored chart is installed under a signed release. | issue 272 |
| `test_manifest_without_chart_sha_refuses` | infra | Without it a bundle manifest that omits the chart digest silently skips chart verification. | issue 272 |
| `test_chart_identity_failure_then_fix_passes_on_next_run` | infra | Without it a refused chart identity could leave state that blocks the corrected next deploy, or the verified chart is not the one installed. | issue 272 |
| `test_dry_run_with_manifest_is_noticed_not_verified` | infra | Without it dry-run claims release verification it did not perform. | docs/deploy.md#deployment-policy |
| `test_missing_production_values_fails_before_mutation` | infra | Without it deploy proceeds without the production Qdrant values and can create a shrunken or default cluster. | AGENTS.md#hard-boundaries |
| `test_invalid_configuration_fails_closed` | infra | Without it a missing values file, blank revision, bad tracing endpoint or bad Secret name is accepted at deploy time. | docs/deploy.md#configuration-contract |
| `test_rehearsal_knobs_reach_helm` | infra | Without it CI rehearsal storage size and extra values stop reaching Helm, so lab runs cannot fit quotas without editing prod values. | docs/testing.md#airgap-tier |
| `test_rendered_manifest_substituted_and_written` | infra | Without it the default render leaves placeholders, wires the wrong Qdrant key, or drops the revision, image and read-only defaults. | docs/deploy.md#deployment-policy |
| `test_tracing_enabled_deploys_jaeger_and_wires_endpoint` | infra | Without it enabling tracing does not render Jaeger or wire the agent endpoint, and the off sentinel leaves Jaeger behind. | issue 83 |
| `test_metrics_enabled_deploys_servicemonitor_and_wires_env` | infra | Without it metrics enablement does not render the ServiceMonitor, or non-true values wrongly render it. | issue 187 |
| `test_gateway_keys_wired_when_secret_set` | infra | Without it gateway keys are not referenced from the Secret or are inlined into the pod. | AGENTS.md#hard-boundaries |
| `test_agent_route_renders_oauth_sidecar_and_reencrypt_route` | infra | Without it the console Route renders without the oauth sidecar or reencrypt policy. | issue 373 |
| `test_agent_route_fails_closed_on_pending_oauth_pin` | infra | Without it deploy enables the Route with an unpinned oauth-proxy digest. | issue 373 |
| `test_app_adoption_checks_ownership_before_any_release_mutation` | infra | Without it deploy adopts objects owned by another release, controller or manager. | docs/deploy.md#deployment-policy |
| `test_missing_referenced_secret_key_blocks_before_mutation` | infra | Without it a Secret missing a referenced data key is found only when pods fail to start. | docs/deploy.md#configuration-contract |
| `test_invalid_schema_fails_before_namespace_or_release_mutation` | infra | Without it a chart schema violation is discovered after the namespace or release was already mutated. | docs/deploy.md#configuration-contract |
| `test_model_revision_yaml_characters_round_trip` | infra | Without it YAML-significant characters in the model revision or name are mangled between the operator input and the pod environment. | docs/testing.md#evidence-design |
| `test_corrupt_ownership_inventory_blocks_mutation` | infra | Without it an unreadable ownership inventory is treated as nothing owned and deploy proceeds. | docs/deploy.md#deployment-policy |
| `test_old_helm_fails_before_any_release_mutation` | infra | Without it an unsupported Helm version performs a release with different behavior. | docs/deploy.md#deployment-policy |
| `test_disabled_legacy_resources_are_checked_then_cleaned_after_rollout` | infra | Without it disabled legacy resources are removed before the new workloads are ready, or foreign-owned ones are deleted. | docs/deploy.md#deployment-policy |
| `test_disabled_resource_read_failures_block_mutations` | infra | Without it a failed discovery or read is treated as absence and cleanup or upgrade runs blind. | docs/deploy.md#deployment-policy |
| `test_forbidden_servicemonitor_read_is_a_notice_for_namespace_admins` | infra | Without it namespace admins cannot deploy because they cannot read monitoring.coreos.com, or client text with identities leaks. | docs/install_and_ops.md (issue 680) |
| `test_other_servicemonitor_read_error_still_blocks_mutations` | infra | Without it any ServiceMonitor read error is downgraded to a notice. | docs/install_and_ops.md (issue 680) |
| `test_readable_owned_leftover_servicemonitor_is_still_cleaned` | infra | Without it a leftover owned ServiceMonitor from a metrics-enabled release is never removed. | docs/install_and_ops.md (issue 680) |
| `test_disabled_cleanup_never_accepts_a_pvc` | infra | Without it a cleanup inventory containing a PVC could delete retained storage. | AGENTS.md#hard-boundaries |
| `test_legacy_cleanup_waits_for_successful_rollouts` | infra | Without it legacy resources are deleted although the new rollout failed, leaving no working service. | docs/deploy.md#deployment-policy |
| `test_shared_gateway_aliases_and_secret_reach_requests` | infra | Without it the shared-gateway URL, model names or single Secret key do not reach the real runtime clients and requests lose their Bearer key. | docs/deploy.md#configuration-contract |
| `test_shared_gateway_specific_url_and_caller_override_win` | infra | Without it a file value or the shared URL overrides a specific per-operation URL or the caller's choice. | issue 402 |
| `test_missing_shared_gateway_key_blocks_before_mutation` | infra | Without it a shared gateway Secret lacking its key is found only at pod start. | docs/deploy.md#configuration-contract |
| `test_invalid_shared_gateway_key_refuses_before_commands` | infra | Without it an invalid shared Secret key name or a key without a Secret reaches manifests. | docs/deploy.md#configuration-contract |
| `test_jaeger_false_without_destination_fails_before_mutation` | infra | Without it disabling Jaeger leaves the agent exporting to a nonexistent host and nothing refuses before render. | issue 568 |
| `test_jaeger_false_with_explicit_collector_exports_to_it` | infra | Without it intentionally reusing the former Jaeger hostname becomes impossible or a rollout waits for a backend that is not deployed. | issue 568 |
| `test_upgrade_from_bundled_jaeger_selects_destination_before_removal` | infra | Without it existing bundled Jaeger is removed before the explicit destination is released, or its PVC enters the cleanup. | issue 568 |
| `test_route_on_live_deploy_verifies_the_actual_route_and_renders_the_cluster_ca` | infra | Without it the Route is not checked against the live OAuth contract and the rendered CA is not the namespace service CA. | issue 373 |
| `test_route_not_converged_after_release_is_deleted_and_fails` | infra | Without it a Route that drifted from the OAuth contract stays as a public unauthenticated console. | AGENTS.md#hard-boundaries (issue 373) |
| `test_unconverged_route_that_cannot_be_deleted_says_so_loudly` | infra | Without it a failed removal of an exposed Route is silent and the operator does not know to delete it. | issue 373 |
| `test_route_unreadable_after_release_is_not_a_pass` | infra | Without it an unreadable Route after release is reported as verified. | issue 373 |
| `test_route_refusal_then_corrected_run_succeeds` | infra | Without it a refusal and Route removal leaves state that blocks the corrected next deploy. | issue 373 |
| `test_other_routes_to_agent_or_qdrant_refuse_before_any_mutation` | infra | Without it a foreign Route to the agent or Qdrant Service is accepted, exposing them publicly. | AGENTS.md#hard-boundaries (issue 373) |
| `test_other_route_alternate_backend_to_qdrant_refuses` | infra | Without it a Route that reaches Qdrant only through an alternate backend evades the exposure check. | AGENTS.md#hard-boundaries (issue 373) |
| `test_unrelated_routes_do_not_block_the_deploy` | infra | Without it unrelated site Routes block every deployment. | issue 373 |
| `test_owned_route_with_unowned_alternate_backend_refuses_before_mutation` | infra | Without it the owned Route can carry an alternate backend to Qdrant unnoticed. | AGENTS.md#hard-boundaries (issue 373) |
| `test_route_off_retires_existing_public_route_before_the_first_mutation` | infra | Without it turning the Route off leaves the direct-access Route live during the release, or deletes again on every run. | issue 373 |
| `test_route_off_removal_failure_blocks_the_release` | infra | Without it the release continues although a disabled owned Route could not be removed. | issue 373 |
| `test_unlistable_routes_fail_closed_before_mutation` | infra | Without it an unlistable Route inventory is treated as no public Route. | AGENTS.md#hard-boundaries (issue 373) |
| `test_corrupt_route_inventory_blocks_mutation` | infra | Without it a corrupt Route inventory is treated as empty and deploy proceeds. | issue 373 |
| `test_route_inventory_failure_never_echoes_route_data` | infra | Without it a refused Route inventory prints Route contents such as private keys into logs. | AGENTS.md#hard-boundaries |
| `test_missing_cookie_secret_key_blocks_route_before_mutation` | infra | Without it a Route is deployed with an oauth cookie Secret lacking its key. | issue 373 |
| `test_unusable_service_ca_blocks_route_before_mutation` | infra | Without it a non-PEM service CA is rendered into the Route. | issue 373 |
| `test_release_renders_first_party_images_by_verified_digest` | infra | Without it first-party images deploy by mutable tag instead of the registry digest verified against the bundle. | issue 272 |
| `test_missing_registry_image_refuses_before_any_mutation` | infra | Without it deploy against an empty or partial registry mutates the cluster before image pulls fail. | issue 272 |
| `test_manifest_without_config_digests_refuses_before_any_mutation` | infra | Without it a manifest lacking image config digests skips registry identity verification. | issue 272 |
| `test_swapped_tag_refuses_before_mutation_then_reload_passes_next_run` | infra | Without it a registry tag swapped after load is deployed, or the refusal blocks the next run after reload. | issue 272 |
| `test_running_pod_with_different_digest_fails_after_rollout` | infra | Without it pods running a different image than the verified digest are reported as success. | issue 272 |
| `test_missing_running_pod_for_verified_image_fails` | infra | Without it a rollout with no current pod passes image identity verification. | issue 272 |
| `test_dry_run_is_tag_only_and_reads_no_registry` | infra | Without it dry-run contacts the registry or claims digest verification. | docs/deploy.md#deployment-policy |
| `test_digest_from_caller_environment_is_never_rendered` | infra | Without it a digest injected through the caller environment is rendered as if verified. | issue 272 |
| `test_release_identity_ignores_unrelated_retained_jobs` | infra | Without it retained diagnostic Job pods make a correct rollout fail identity checks. | issue 272 |
| `test_release_identity_wrong_target_repository_cannot_use_diagnostic_pod` | infra | Without it a diagnostic pod can stand in for a target pod running another repository. | issue 272 |
| `test_release_identity_requires_status_on_each_target_replica` | infra | Without it a replica with no container status passes the image identity check. | issue 272 |
| `test_existing_owned_route_failure_paths_preserve_only_compatible_exposure` | infra | Without it a failed release leaves a direct-access Route live, or deletes a valid OAuth Route and operator Routes, and the next run does not recover. | issue 373 |
| `test_named_operator_owned_route_is_not_retired_or_adopted` | infra | Without it a same-named operator-owned Route is deleted or adopted. | docs/deploy.md#deployment-policy |
| `test_deploy_registry_structure_is_validated_before_mutation` | infra | Without it a malformed registry manifest is accepted as an image identity. | issue 272 |
| `test_post_release_operator_ownership_is_not_deleted_on_route_drift` | infra | Without it drift handling deletes a Route whose ownership is not established. | docs/deploy.md#deployment-policy |

## `tests/test_airgap_ingest_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_ingest_dryrun_renders_clean_manifest` | infra | Without it the ingest Job renders with placeholders, the wrong writer key, wrong args or leaked gateway keys. | docs/deploy.md#deployment-policy |
| `test_ingest_replacement_waits_for_prior_writer` | infra | Without it a replacement Job starts while a terminating pod still holds the writer lock. | issue 391 |
| `test_ingest_logs_follow_retry_and_stop_owned_stream` | infra | Without it a retried Job's pod logs are hidden or the log child process outlives the launcher. | docs/install_and_ops.md |
| `test_ingest_cancellation_reaps_local_observers` | infra | Without it cancelling the launcher leaves orphaned kubectl observers or loses the warning that the Job may still be running. | docs/install_and_ops.md |
| `test_invalid_configuration_fails_closed` | infra | Without it NFS storage, missing corpus PVC, malformed retire entries, bad booleans or Secret names reach the ingest render. | docs/deploy.md#configuration-contract |
| `test_ingest_qdrant_readonly_key_fails_closed` | infra | Without it an ingest Job rendered with the read-only key is applied and cannot write. | AGENTS.md#hard-boundaries (issue 366) |
| `test_ingest_qdrant_copresent_readonly_key_fails_closed` | infra | Without it a read-only key smuggled beside the writer key in the ingest render passes. | AGENTS.md#hard-boundaries (issue 366) |
| `test_ingest_retire_docs_rendered_with_alias_publish` | infra | Without it retirement entries (comma or newline separated) and the repair flag do not reach the Job arguments. | issue 391 |
| `test_ingest_retire_docs_preserves_interior_spaces_in_product_label` | infra | Without it a retirement revision with spaces or pipes is fragmented and the backend refuses or retires the wrong source. | docs/testing.md#evidence-design |
| `test_ingest_collection_policy_absent_fails_closed` | infra | Without it ingest silently downgrades to a 1/1/1 collection when no policy is selected. | issue 360 |
| `test_ingest_collection_policy_preset_supplies_production_tuple` | infra | Without it the production 6/3/2 tuple stops being the default when the operator selects nothing. | issue 360 |
| `test_ingest_collection_policy_precedence_caller_over_file_over_preset` | infra | Without it the preset or file overrides an explicit operator selection of the collection policy. | issue 360 |
| `test_ingest_collection_policy_invalid_fails_closed` | infra | Without it non-numeric or non-positive replication factors reach collection creation. | issue 360 |
| `test_production_preset_is_the_owner_decision` | infra | Without it the checked-in production collection default can change silently. | AGENTS.md#hard-boundaries (issue 360) |
| `test_ingest_jaeger_false_requires_intentional_destination` | infra | Without it the ingest Job inherits the defaulted bundled Jaeger hostname after the backend was disabled. | issue 568 |
| `test_ingest_pull_secret_wired_when_set` | infra | Without it the ingest pull secret is not wired or breaks out of the pod-spec mapping. | docs/deploy.md#deployment-policy |
| `test_ingest_cli_corpus_pvc_beats_env_file` | infra | Without it a stale CORPUS_PVC in the env file mounts the wrong corpus. | issue 402 |
| `test_ingest_gateway_keys_wired_when_secret_set` | infra | Without it ingest gateway keys are not Secret references, or reasoning and rerank keys leak into the ingest Job. | AGENTS.md#hard-boundaries |
| `test_ingest_direct_peers_roundtrip_and_precedence` | infra | Without it direct-peer URLs with whitespace and delimiters are altered between operator input (file, env, Task) and the Job environment. | docs/testing.md#evidence-design |
| `test_ingest_does_not_infer_direct_peers` | infra | Without it peer URLs are invented for an unclustered selection. | issue 360 |
| `test_shared_gateway_ingest_and_context_refs` | infra | Without it the shared gateway URL and Secret reference do not reach the ingest embed and context legs, or the Task bridge loses caller precedence. | docs/deploy.md#configuration-contract |
| `test_ingest_release_renders_job_image_by_verified_digest` | infra | Without it the ingest Job runs by mutable tag instead of the verified registry digest. | issue 272 |
| `test_ingest_swapped_registry_tag_refuses_before_any_cluster_call_then_next_run_passes` | infra | Without it a swapped ingest image tag is applied, or the refusal blocks the next run after reload. | issue 272 |
| `test_ingest_missing_registry_image_refuses_before_any_cluster_call` | infra | Without it ingest mutates the cluster although the packed image is not in the registry. | issue 272 |

## `tests/test_airgap_load_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_load_missing_artifacts_fails_closed` | infra | Without it load runs with no unpacked bundle and fails obscurely or loads stale files. | docs/install_and_ops.md |
| `test_load_corrupted_checksum_fails` | infra | Without it a corrupted image archive is pushed into the internal registry. | docs/deploy.md#deployment-policy |
| `test_load_manifest_sha_mismatch_fails_closed` | infra | Without it images of another release SHA are loaded under this checkout. | docs/deploy.md#deployment-policy |
| `test_load_success_with_parent_dir` | infra | Without it the documented unpack-next-to-clone layout stops being discovered. | docs/install_and_ops.md |
| `test_load_success_with_bundle_dir_override` | infra | Without it AIRGAP_BUNDLE_DIR is ignored and the wrong bundle is loaded. | docs/install_and_ops.md |
| `test_load_tampered_sums_fails_signature` | infra | Without it a modified checksum list passes because only the unsigned list was checked. | docs/deploy.md#deployment-policy |
| `test_load_image_digest_mismatch_fails_closed` | infra | Without it a validly signed bundle with a lying MANIFEST digest loads wrong image bytes. | docs/deploy.md#deployment-policy |
| `test_load_refuses_manifest_without_config_digest_before_any_push` | infra | Without it a manifest lacking config digests pushes images that deploy cannot later verify. | issue 272 |
| `test_load_refuses_manifest_config_digest_that_is_not_the_archives` | infra | Without it a MANIFEST config digest unrelated to the archive is accepted and pushed. | issue 272 |
| `test_load_trusted_pub_mismatch_refuses` | infra | Without it SNEAKERNET_TRUSTED_PUB no longer pins the signer and a bundle signed by another key loads. | docs/deploy.md#deployment-policy |
| `test_load_signed_missing_task_member_refuses_before_push` | infra | Without it a signed bundle lacking the Task host tool members loads. | docs/deploy.md#deployment-policy |
| `test_load_ignores_retained_optional_image_outside_current_signed_inventory` | infra | Without it an old oauth-proxy archive left in the workspace is pushed although the current signed inventory excludes it. | docs/deploy.md#deployment-policy |
| `test_load_success_proves_registry_serves_packed_config` | infra | Without it exit status of skopeo copy is trusted instead of reading the tag back and reporting the verified digest pin. | issue 272 |
| `test_load_refuse_then_fix_next_run_passes` | infra | Without it a registry tag serving a different image is accepted, or the refusal blocks the next run after the fix. | issue 272 |
| `test_load_refuses_registry_manifest_list_for_tag` | infra | Without it a tag resolving to a manifest list instead of one image is accepted. | issue 272 |
| `test_load_refuses_unreadable_registry_after_push` | infra | Without it a push that stored nothing is reported as loaded. | issue 272 |
| `test_load_refuses_archive_changed_after_digest_binding` | infra | Without it an archive swapped after digest binding is pushed. | issue 272 |
| `test_load_registry_read_back_follows_skopeo_args_and_insecure_flag` | infra | Without it site skopeo flags (auth, certs, TLS) do not reach the push and read-back, or copy-only flags break inspect. | docs/install_and_ops.md |
| `test_load_dryrun_is_hermetic_and_not_release_verified` | infra | Without it dry-run pushes, reads a registry or claims identity verification. | docs/deploy.md#deployment-policy |
| `test_load_verifies_optional_oauth_proxy_image_like_the_others` | infra | Without it the optional oauth-proxy image skips the registry identity check applied to other images. | issue 373 |
| `test_load_preserves_layer_count_requirement` | infra | Without it a registry image with extra layers passes as the packed image. | issue 272 |

## `tests/test_airgap_pack_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_pack_sha_mismatch_fails_closed` | infra | Without it a bundle is packed for a SHA that is not the checked-out commit. | docs/deploy.md#deployment-policy |
| `test_pack_refuses_several_chart_archives_then_passes_after_fix` | infra | Without it several vendored charts are packed, or the manifest chart digest does not match the single chart. | issue 272 |
| `test_pack_missing_skopeo_fails_closed` | infra | Without it packing proceeds without the image tool and produces an incomplete bundle. | docs/install_and_ops.md |
| `test_pack_missing_signing_key_fails_closed` | infra | Without it an unsigned sneakernet bundle can be produced. | docs/deploy.md#deployment-policy |
| `test_pack_trusted_key_labels_signed_true` | infra | Without it the bundle mislabels whether the signing key is a trusted one. | docs/deploy.md#deployment-policy |
| `test_pack_outside_git_repo_fails_closed` | infra | Without it packing outside a git clone produces a bundle with no verifiable source. | docs/install_and_ops.md |
| `test_pack_success_builds_verified_tarball` | infra | Without it the bundle members, digest-pinned images, manifest, SBOM, notices, signature or offline bootstrap break unnoticed. | docs/deploy.md#deployment-policy |
| `test_pack_bundles_oauth_proxy_once_digest_recorded` | infra | Without it the console oauth-proxy image is left out of the bundle, manifest or SBOM once its digest is recorded, or the bundle does not verify and clone. | issue 373 |
| `test_pack_rejects_bad_task_before_image_calls` | infra | Without it an unapproved Task archive is bundled as a host tool. | docs/deploy.md#deployment-policy |
| `test_pack_rejects_actual_inventory_drift_before_signing` | infra | Without it image contents that disagree with the dependency inventory are signed into a bundle. | docs/deploy.md#deployment-policy |
| `test_pack_refuses_unreviewed_license_change_before_any_image_call` | infra | Without it unreviewed license or dependency changes enter the transfer bundle. | issue 376 |

## `tests/test_airgap_pipeline_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_pipeline_unknown_argument_fails_before_stages` | infra | Without it a mistyped flag is ignored and an unintended full pipeline runs. | docs/install_and_ops.md |
| `test_pipeline_dryrun_full` | infra | Without it the five-stage orchestration or honest dry-run completion message breaks. | docs/testing.md#airgap-tier |
| `test_pipeline_live_gateway_probe_gates_ingest` | infra | Without it ingest runs although the live gateway probe from the deployed agent failed, or the stage order changes. | docs/install_and_ops.md |
| `test_pipeline_bad_probe_budget_fails_before_stages` | infra | Without it an invalid probe token budget is accepted after stages already ran. | docs/install_and_ops.md |
| `test_pipeline_dryrun_including_load` | infra | Without it the load stage is not honored in dry-run orchestration. | docs/testing.md#airgap-tier |
| `test_pipeline_skip_ingest_flag` | infra | Without it --skip-ingest stops skipping ingestion or hides that it was skipped. | docs/install_and_ops.md |
| `test_pipeline_without_corpus_pvc_awaits_ingest` | infra | Without it a missing CORPUS_PVC fails the pipeline instead of stopping at an explicit await-ingest state. | docs/install_and_ops.md |
| `test_incomplete_reasoning_stops_before_load_then_corrected_config_runs` | infra | Without it incomplete reasoning model configuration is discovered after load and deploy have mutated the site. | docs/deploy.md#configuration-contract |

## `tests/test_airgap_smoke_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_smoke_dryrun_succeeds` | infra | Without it the smoke preview stops printing the commands it would run. | docs/testing.md#airgap-tier |
| `test_smoke_uses_oc_when_kubectl_missing` | infra | Without it smoke fails on bastions that only have oc. | docs/install_and_ops.md |
| `test_smoke_clean_skip_on_empty_collection` | infra | Without it an empty collection fails smoke instead of reporting infrastructure ready. | docs/install_and_ops.md |
| `test_smoke_fails_on_search_error` | infra | Without it a failed search request is reported as a passing smoke. | docs/install_and_ops.md |
| `test_smoke_fails_on_degraded_healthz` | infra | Without it a degraded agent passes smoke. | docs/install_and_ops.md |
| `test_smoke_kc_env_override_respected` | infra | Without it the KC client override is ignored and the wrong cluster client is used. | docs/install_and_ops.md |
| `test_smoke_tracing_off_sentinel_skips_trace_check` | infra | Without it smoke demands a trace when tracing is intentionally off. | issue 83 |
| `test_smoke_tracing_on_by_default_runs_check` | infra | Without it default tracing is not verified and a missing span passes smoke. | issue 83 |
| `test_smoke_tracing_ok_when_span_landed` | infra | Without it smoke reports tracing failure even when the span arrived. | issue 83 |
| `test_smoke_tracing_custom_query_url_reaches_pod` | infra | Without it the operator-set Jaeger query URL is ignored by the trace poll. | issue 83 |
| `test_smoke_tracing_skipped_on_empty_collection` | infra | Without it smoke checks for a span although nothing was traced. | issue 83 |
| `test_smoke_external_collector_never_claims_trace_arrival` | infra | Without it smoke claims trace arrival was verified at an external collector it never queried. | issue 568 |

## `tests/test_airgap_validate_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_validate_clean_exits_zero` | infra | Without it a regression that makes preflight reject a valid configuration (or drop the printed policy, tracing and revision summary) blocks every deployment. | docs/deploy.md#configuration-contract |
| `test_invalid_configuration_fails_closed` | infra | Without it placeholder or wrong settings (partial or impossible collection policy, NFS storage, bad DENSE_DIM, blank embed revision, bad URL scheme, bad Secret name, bad route boolean) pass preflight and reach the cluster. | docs/deploy.md#configuration-contract |
| `test_validate_agent_write_key_fails_closed` | infra | Without it a chart that gives the serving agent the full-access Qdrant key passes preflight, letting search traffic mutate the corpus. | AGENTS.md#hard-boundaries (issue 366) |
| `test_validate_ingest_readonly_key_fails_closed` | infra | Without it an ingest Job downgraded to the read-only key passes preflight and then fails or silently cannot write the corpus. | AGENTS.md#hard-boundaries (issue 366) |
| `test_validate_ingest_copresent_readonly_key_fails_closed` | infra | Without it a read-only Qdrant key smuggled next to the writer key in the ingest template passes preflight. | AGENTS.md#hard-boundaries (issue 366) |
| `test_validate_jaeger_false_requires_intentional_destination` | infra | Without it disabling bundled Jaeger silently leaves the agent exporting traces to a hostname that no longer exists. | issue 568 |
| `test_validate_optional_model_url_bad_scheme_refused` | infra | Without it a model endpoint without http(s) scheme reaches rendered manifests and every model call fails at runtime. | docs/deploy.md#configuration-contract |
| `test_validate_missing_skopeo_fails` | infra | Without it preflight passes on a bastion lacking skopeo and the load stage fails later mid-pipeline. | docs/install_and_ops.md |
| `test_explicit_env_beats_env_file` | infra | Without it a stale airgap.env value overrides the operator's explicit command-line setting, e.g. deploying to the wrong registry or storage class. | issue 402 |
| `test_env_file_feeds_unset_and_empty_vars_from_path_with_spaces` | infra | Without it an env file in a path with spaces, or empty variables, are not read and the operator's configuration is silently ignored. | issue 402 |
| `test_missing_explicit_env_file_fails_before_any_tool` | infra | Without it a typo in AIRGAP_ENV silently becomes an environment-only run and deploys with defaults. | issue 478 |
| `test_taskfiles_do_not_load_airgap_env` | infra | Without it a Taskfile dotenv load lets stale file values override explicit caller values in every task. | issue 402 |
| `test_validate_refuses_plaintext_gateway_key_in_env_file` | infra | Without it a gateway virtual key written into airgap.env travels on sneakernet media and lingers on bastions instead of living in the cluster Secret. | AGENTS.md#hard-boundaries |
| `test_validate_allows_empty_and_commented_gateway_keys` | infra | Without it the plaintext-key refusal rejects harmless empty or commented lines and blocks the shipped example file. | docs/deploy.md#configuration-contract |
| `test_validate_pull_secret_bad_name_fails_closed` | infra | Without it a sed-active character in PULL_SECRET silently rewrites the rendered manifest or Helm values. | docs/deploy.md#configuration-contract |
| `test_validate_live_verifies_gateway_secret` | infra | Without it a live preflight stops confirming that the referenced gateway key Secret exists before deploy. | docs/deploy.md#configuration-contract |
| `test_validate_live_missing_gateway_secret_fails` | infra | Without it deployment proceeds against a missing gateway Secret and pods crash at start. | docs/deploy.md#configuration-contract |
| `test_validate_live_missing_namespace_notices_secret` | infra | Without it a first deployment into a not-yet-created namespace fails instead of telling the operator to create the Secret first. | docs/install_and_ops.md |
| `test_validate_live_refuses_image_sha_only_bypass` | infra | Without it setting IMAGE_SHA alone lets a stale checkout run under a release claim from another bundle. | issue 414 |
| `test_validate_live_matching_checkout_passes_manifest_section` | infra | Without it the release guard could refuse the legitimately matching signed checkout and block all approved deployments. | issue 414 |
| `test_checkout_guard_dryrun_skips_mismatch` | infra | Without it dry-run previews fail on mismatched checkouts or the connected-development notice path is lost. | issue 414 |
| `test_checkout_guard_refuses_unresolved_checkout_when_manifest_is_packed` | infra | Without it a copied tree with no git identity runs live under a packed-release claim just because IMAGE_SHA was set. | issue 414 |
| `test_checkout_guard_refuses_release_evidence_without_manifest` | infra | Without it deleting dist/MANIFEST.txt while leaving bundle evidence downgrades a release run to the unverified development path. | issue 414 |
| `test_checkout_guard_allows_untracked_operator_files` | infra | Without it a clean checkout fails or prints noise when the operator has airgap.env, evidence or scratch files, which blocks normal use. | issue 414 |
| `test_checkout_guard_refuses_tracked_edit_at_matching_head` | infra | Without it an edited tracked script or chart at the matching HEAD runs under the release claim. | issue 414 |
| `test_checkout_guard_next_run_passes_after_reverting_edit` | infra | Without it a refusal could be sticky or silently reset the operator's staged work instead of passing after the edit is reverted. | issue 414 |
| `test_checkout_guard_works_in_git_file_worktree` | infra | Without it linked git worktrees (a .git file) are mis-judged and either bypass or wrongly fail the release guard. | issue 414 |
| `test_checkout_guard_refuses_manifest_without_signed_bundle` | infra | Without it a bare unsigned MANIFEST.txt establishes a release identity. | issue 414 |
| `test_checkout_guard_refuses_manifest_edited_after_signing` | infra | Without it a MANIFEST edited after signing (with sha still equal HEAD) is trusted. | issue 414 |
| `test_checkout_guard_refuses_forged_manifest_and_sums` | infra | Without it a MANIFEST and SHA256SUMS rewritten consistently without the signing key are accepted. | issue 414 |
| `test_checkout_guard_requires_manifest_listed_exactly_once` | infra | Without it a validly signed checksum list that lists MANIFEST.txt twice or not at all authenticates the wrong file. | issue 414 |
| `test_checkout_guard_trusted_pub_pins_the_key` | infra | Without it SNEAKERNET_TRUSTED_PUB stops pinning the signer and a bundle signed by any key is accepted. | issue 414 |
| `test_checkout_guard_refuses_index_flags_that_hide_edits` | infra | Without it an edit hidden by assume-unchanged is invisible to git diff and runs under the release claim. | issue 414 |
| `test_checkout_guard_refuses_sparse_checkout` | infra | Without it a sparse checkout missing part of the release tree is assumed clean. | issue 414 |
| `test_checkout_guard_parent_bundle_layout_is_verified` | infra | Without it the documented unpack-next-to-clone layout trusts ../MANIFEST.txt without verifying its signature chain. | issue 414 |
| `test_checkout_guard_parent_sums_count_only_when_bundle_shaped` | infra | Without it an unrelated SHA256SUMS in the parent directory blocks connected development, or a real bundle list there is ignored. | issue 414 |
| `test_checkout_guard_wired_into_all_launch_paths` | infra | Without it deploy, ingest, validate or load could drop the checkout guard and no behavioral test would notice. | issue 414 |
| `test_model_validation_preserves_caller_over_file_precedence` | infra | Without it a model URL in the env file overrides the caller's value, or an invalid caller URL is echoed to the terminal. | issue 402 |
| `test_contextual_model_pair_required_in_preflight` | infra | Without it enabling contextual embedding without its model leaves ingest failing only after the Job starts. | docs/deploy.md#configuration-contract |
| `test_shipped_example_model_mode_is_explicit` | infra | Without it the shipped airgap.env.example stops validating in either shared-gateway or legacy mode, breaking the operator's first run. | docs/deploy.md#configuration-contract |
| `test_validate_invalid_operator_boolean_refused` | infra | Without it a misspelled boolean silently selects a default and changes behavior, and the bad value leaks into output. | docs/deploy.md#configuration-contract |
| `test_validate_file_model_budgets_fail_before_later_stages` | infra | Without it out-of-range model budgets or an unsupported reasoning effort pass preflight and fail in the running pod. | docs/deploy.md#configuration-contract |
| `test_validate_valid_operator_boolean_accepted` | infra | Without it documented boolean spellings are rejected and block legitimate configurations. | docs/deploy.md#configuration-contract |
| `test_validate_refuses_the_retired_in_place_switch` | infra | Without it INGEST_ALIAS_PUBLISH=false is silently ignored although in-place ingest was retired. | issue 360 |
| `test_validate_invalid_boolean_from_env_file_then_corrected` | infra | Without it a rejected env file could leave state that blocks the corrected next run. | docs/deploy.md#configuration-contract |
| `test_validate_nfs_snapshot_storage_refused_then_corrected` | infra | Without it Qdrant snapshot volumes can land on NFS, which Qdrant does not support for block data. | AGENTS.md#hard-boundaries |
| `test_validate_live_storageclass_notfound_fails` | infra | Without it a missing StorageClass is found only when the PVC stays Pending after deploy. | docs/install_and_ops.md |
| `test_validate_live_shared_snapshot_storageclass_is_read_once` | infra | Without it the cluster is probed redundantly when the snapshot class equals the data class. | docs/deploy.md#configuration-contract |
| `test_validate_live_distinct_snapshot_storageclass_probe` | infra | Without it a distinct snapshot StorageClass is never checked, or a client error is reported as success or leaks upstream text. | docs/deploy.md#configuration-contract |
| `test_validate_live_storageclass_forbidden_is_not_reported_absent` | infra | Without it a namespace admin who cannot read StorageClasses is told the class is missing, or the unverified status is hidden. | docs/install_and_ops.md (issue 678) |
| `test_validate_live_notfound_name_containing_forbidden_still_fails` | infra | Without it a missing class whose name contains the word forbidden is mis-classified as a permission problem and passes. | docs/deploy.md#configuration-contract |
| `test_validate_live_storageclass_other_error_fails` | infra | Without it an unexpected client error (e.g. connection refused) is treated as absence or success. | docs/deploy.md#configuration-contract |
| `test_validate_live_namespace_admin_passes_cluster_probe` | infra | Without it preflight demands cluster-wide reads and fails for the namespace-admin identity the site actually has. | docs/install_and_ops.md (issue 678) |
| `test_validate_live_unauthenticated_or_unreachable_api_fails` | infra | Without it an anonymous or unreachable API server passes the cluster probe. | docs/install_and_ops.md (issue 678) |
| `test_validate_live_forbidden_namespace_still_checks_secret` | infra | Without it a namespace-scoped deployer who cannot read the Namespace object skips the Secret check entirely. | docs/install_and_ops.md (issue 678) |
| `test_validate_live_forbidden_secret_fails_not_absent` | infra | Without it a forbidden Secret read is reported as not found, hiding that the deployer needs namespace-scoped get on secrets. | docs/install_and_ops.md |
| `test_validate_live_scc_readable_reports_openshift` | infra | Without it the operator loses the restricted-v2 guidance on OpenShift clusters. | docs/deploy.md#deployment-policy |
| `test_validate_live_scc_missing_type_reports_standard_kubernetes` | infra | Without it a non-OpenShift cluster is not recognized from the missing SCC resource type. | docs/deploy.md#deployment-policy |
| `test_validate_live_scc_failure_is_not_standard_kubernetes` | infra | Without it a failed SCC read is claimed to prove a non-OpenShift cluster. | docs/deploy.md#deployment-policy |
| `test_validate_route_dryrun_accepts_recorded_consistent_pin` | infra | Without it a recorded, chart-consistent oauth-proxy pin is refused and the console Route cannot be enabled. | issue 373 |
| `test_validate_route_dryrun_pending_pin_refused` | infra | Without it the console Route is enabled with an unpinned sha256:PENDING oauth-proxy image. | issue 373 |
| `test_validate_route_off_ignores_the_oauth_pin` | infra | Without it a pending oauth pin blocks deployments that do not use the Route. | issue 373 |
| `test_validate_route_pin_tag_must_match_chart` | infra | Without it images.txt and the chart render different oauth-proxy tags and the pinned digest is not the image deployed. | issue 373 |
| `test_validate_route_manifest_without_oauth_member_refused` | infra | Without it a bundle packed without the oauth-proxy image is used with AGENT_ROUTE=true and the sidecar cannot be pulled. | issue 373 |
| `test_validate_route_manifest_with_other_pin_refused` | infra | Without it a bundle carrying a different oauth-proxy digest than images.txt is deployed. | issue 373 |
| `test_validate_route_manifest_with_matching_pin_passes` | infra | Without it the bundle-consistency check refuses a correct bundle. | issue 373 |
| `test_validate_route_live_success` | infra | Without it the live Route prerequisites (cookie Secret, service CA) stop being confirmed before deploy. | issue 373 |
| `test_validate_route_live_cookie_secret_failures` | infra | Without it a forbidden cookie Secret read is reported as success or absence instead of requiring namespace-scoped get. | issue 373 |
| `test_validate_route_live_cookie_key_missing` | infra | Without it a cookie Secret lacking the cookie-secret key passes and oauth-proxy fails at start. | issue 373 |
| `test_validate_route_live_service_ca_must_be_pem` | infra | Without it a non-PEM service CA becomes the Route destination certificate and the console is unreachable. | issue 373 |
| `test_validate_route_live_requires_route_api` | infra | Without it AGENT_ROUTE=true on a cluster without the Route API passes preflight. | issue 373 |
| `test_validate_live_refuses_foreign_route_to_protected_service` | infra | Without it a non-OAuth Route exposing the agent or Qdrant Service passes preflight, publishing an unauthenticated path. | AGENTS.md#hard-boundaries (issue 373) |
| `test_validate_live_unlistable_routes_fail_closed` | infra | Without it a deployer that cannot list Routes is treated as having no public path. | AGENTS.md#hard-boundaries (issue 373) |
| `test_validate_live_unrelated_route_and_owned_route_pass` | infra | Without it unrelated site Routes or the owned rag-agent Route wrongly block preflight. | issue 373 |

## `tests/test_answer_chat_orchestration.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_stream_outcome_is_pinned` | noise-kept | The /v1/answer and /v1/chat streaming outcome changes unnoticed (SSE bytes, headers, logs, RED metrics, span status). | issue #583; docs/agent.md#http-model-contract |
| `test_json_outcome_is_pinned` | noise-kept | The JSON answer/chat outcome changes unnoticed (body, Server-Timing, logs, RED metrics, spans). | issue #583; docs/agent.md#http-model-contract |
| `test_close_outcome_is_pinned` | noise-kept | A client disconnect mid-stream is recorded as the wrong outcome or double-aborts after a terminal frame. | issue #583; docs/agent.md#http-model-contract |

## `tests/test_answer_core.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_core_headerless_bibliography_preserves_verification_rules` | core | A headerless bibliography line is accepted as a citation or kept in the answer body, or a fabricated cite is accepted. | docs/agent.md#answer-contract |
| `test_core_requires_answer_body_after_parsing` | core | A citation-only, scaffolding-only or empty reply is accepted as an answer, or valid answers are demoted. | docs/agent.md#answer-contract |
| `test_core_stream_and_truncated_finish_reach_the_same_verdict` | core | Streaming parses differently from buffered, or a length-truncated answer is accepted as complete. | docs/agent.md#answer-contract |
| `test_core_adversarial_model_outputs` | core | Hostile model output (fabricated, cut or unmapped citations, thinking-only) is verified or leaks fragments into the body. | issue #597; docs/agent.md#answer-contract |
| `test_core_prompt_header_residue_is_not_an_answer_body` | core | An echo of prompt scaffolding plus a cite is accepted as an answer. | issue #576; docs/agent.md#answer-contract |
| `test_core_prompt_header_with_real_prose_is_still_accepted` | core | Real prose after a prompt-header echo is wrongly rejected. | issue #576; docs/agent.md#answer-contract |
| `test_core_preserves_instructions_under_citation_alias_headers` | core | Instruction lists under a Sources/References header are stripped as citations, deleting real steps. | docs/agent.md#answer-contract |
| `test_citation_header_telemetry_remains_canonical` | noise-kept | Citation-header telemetry stops distinguishing the canonical Citations header from aliases, corrupting prompt-compliance metrics. | docs/agent.md#answer-contract |
| `test_core_generic_citation_is_not_answer_prose` | core | A bare citation echoed as the whole answer is accepted as an answer body. | docs/agent.md#answer-contract |
| `test_core_retrieval_branch_feeds_prompt_and_output` | core | The core's own retrieval leg does not feed the prompt/output or timings. | docs/agent.md#answer-contract |
| `test_core_retrieval_failure_raises_typed_error` | core | Retrieval failures surface as raw exceptions instead of a typed error the routes map to a fixed client error. | docs/agent.md#http-model-contract |
| `test_core_llm_failure_raises_typed_error` | core | LLM failures surface untyped, leaking upstream text to clients. | docs/agent.md#http-model-contract |
| `test_core_stream_empty_hits_yields_only_final` | core | No-hit streams call the model or emit tokens instead of the deterministic no-evidence final. | docs/agent.md#answer-contract |
| `test_resolve_search_query_condenses_only_when_enabled` | core | Chat condensation runs when disabled (extra LLM call) or not when enabled. | docs/agent.md#answer-contract |
| `test_core_reasoning_effort_explicit_override_sync` | core | Explicit reasoning effort is ignored, invalid values accepted, or stream and sync disagree. | docs/agent.md#http-model-contract |
| `test_core_selected_reasoning_effort_reaches_http_request` | core | The chosen reasoning effort is not sent to the model or counted with a different template than the completion. | docs/agent.md#http-model-contract |
| `test_core_normalizes_chat_for_retrieval_complexity_and_prompt` | core | Chat retrieval/classification uses a redundant query or later non-user turn, or mutates caller input. | docs/agent.md#answer-contract |
| `test_core_invalid_chat_refuses_even_with_precomputed_hits` | core | An invalid chat turn is answered anyway because precomputed hits exist, retrieving or calling the model. | docs/agent.md#answer-contract |
| `test_core_stream_rejects_falsy_done_finish` | core | A stream with missing or malformed finish is finalized as accepted stop. | issue #365; docs/agent.md#http-model-contract |
| `test_core_stream_without_done_raises_missing_finish` | core | A stream that ends without a done item is finalized as complete. | issue #365; docs/agent.md#http-model-contract |
| `test_core_stream_explicit_done_still_finalizes` | core | The strict finish gate rejects well-formed streams (positive control). | issue #365; docs/agent.md#http-model-contract |
| `test_structured_model_results_match_buffered_and_fallback_stream` | core | Sync/async model adaptation loses finish reason, usage or ttft metadata. | docs/agent.md#http-model-contract |
| `test_unstructured_completion_fails_then_next_structured_answer_succeeds` | core | An unstructured model result is accepted as completed, or one bad completion wedges the next ordinary answer. | docs/agent.md#http-model-contract |
| `test_closing_core_stream_releases_operation_without_closing_shared_model` | core | Closing a client stream leaks the upstream operation or closes the shared model client, breaking the next request. | docs/agent.md#http-model-contract |
| `test_core_import_graph_excludes_transports_and_application_singleton` | infra | The answer core imports app/SSE/web/MCP modules, coupling it to transports and the application singleton. | docs/architecture.md |
| `test_core_import_checker_rejects_forbidden_dependency` | gate-control | The import-boundary checker passes a deferred forbidden import, so the architecture test no longer guards the core. | docs/architecture.md |
| `test_agent_resources_import_without_the_application_singleton` | infra | The resource view cannot be imported without loading the app module and its lifespan globals. | issue #369; docs/architecture.md |
| `test_use_case_runs_on_explicit_resources_and_borrows_the_model` | core | The answer use case closes the borrowed model, uses the wrong collection/handles, or blocks the event loop on async legs. | issue #369; docs/agent.md#http-model-contract |
| `test_core_type_boundary_rejects_storage_injection_and_write_use` | infra | The core accepts a writer/admin storage client or write calls, weakening the read-only serving credential boundary. | AGENTS.md hard boundaries; docs/architecture.md |
| `test_cancelling_pending_model_read_closes_operation_and_propagates` | core | Cancelling a pending model read leaks the upstream operation, swallows cancellation, or breaks the next answer. | docs/agent.md#http-model-contract |
| `test_stream_grammar_rejects_invalid_sequences_and_next_operation_recovers` | core | Malformed stream frames are finalized as answers, or the operation leaks and breaks the next request. | issue #365; docs/agent.md#http-model-contract |
| `test_single_incomplete_terminal_remains_incomplete` | core | A single length-terminated stream is reported as a complete accepted answer. | issue #365; docs/agent.md#answer-contract |
| `test_core_no_supplied_evidence_never_invokes_generation` | core | The model is called, and cost incurred, with zero usable evidence, or the budget error leaks query or Splunk text. | issue #634; docs/agent.md#answer-contract |
| `test_core_one_usable_excerpt_invokes_generation_once` | core | A prompt with one usable excerpt is wrongly refused or generates more than once (positive control). | issue #634; docs/agent.md#answer-contract |
| `test_core_fixed_content_overflow_stays_plain_budget_error` | core | Irreducible fixed-content overflow is misclassified as an evidence error. | issue #634; docs/agent.md#answer-contract |
| `test_core_no_hits_remains_deterministic_not_budget_error` | core | No-hit questions call the model or raise a budget error instead of insufficient_evidence. | issue #634; docs/agent.md#answer-contract |

## `tests/test_bench_gate.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_update_baseline_emits_nested_shape_that_the_gate_reads` | gate-control | A tool-written flat baseline makes the CI bench gate a permanent no-op against its own output. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_unmeasured_metrics_refuse_required_gate` | gate-control | Unmeasurable Qdrant memory/disk pass a requested gate. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_errors_under_load_fail_the_gate` | gate-control | A load phase with failing requests looks healthy to the bench gate. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_baseline_missing_keys_refuse_required_gate` | gate-control | A baseline lacking gated keys scores as pass. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_aggregate_runs_noise_floor_policy` | noise-kept | Repeat aggregation averages away a failing pass or lets contended latency fail the gate. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_env_mismatch_fails_distinct_before_metric_gates` | gate-control | A baseline from another cpu count yields a misleading p95 regression instead of a distinct env error. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_qdrant_image_mismatch_fails_distinct` | gate-control | A Qdrant image change reports as a metric regression instead of an env mismatch. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_ci_prepares_qdrant_before_check_or_record_and_stops_on_preparation_failure` | infra | CI benchmarks run without the pinned Qdrant prepared, or continue after preparation failure. | AGENTS.md hard boundaries (pinned images) |
| `test_requested_baseline_refuses_before_work_and_preserves_outputs_then_recovers` | gate-control | A missing, corrupt or directory baseline lets the benchmark run work and overwrite previous outputs. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_requested_baseline_uses_preflight_snapshot_and_still_fails_regressions` | gate-control | Swapping the baseline file during measurement selects a lenient gate, or regressions stop failing the run. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_no_check_and_explicit_record_keep_their_operations` | gate-control | Ungated runs write a baseline or an explicit record run fails to produce a usable repeated baseline. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_invalid_gate_numbers_refuse_baseline_result_and_recording` | gate-control | Non-finite, negative or boolean metrics are accepted in a baseline, a result or a recording. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_invalid_or_failed_error_counts_cannot_qualify_or_record` | gate-control | Invalid or nonzero error counts qualify a result or are recorded as a baseline. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_invalid_pass_cannot_hide_in_aggregation_or_overwrite_outputs` | gate-control | One invalid repeat is hidden by aggregation or overwrites reference, output or summary files. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_semantically_invalid_reference_refuses_before_work` | gate-control | A structurally valid but semantically invalid reference starts a benchmark run before refusing. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_real_empty_load_cannot_qualify_benchmark` | gate-control | A zero-request load run reports zero errors and zero latency and qualifies the benchmark. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_required_measurements_have_positive_integer_request_counts` | gate-control | Non-integer or zero request counts qualify or record as a baseline. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_docker_stats_memory_units_are_converted_to_mb` | infra | Without it a wrong unit factor skews the recorded Qdrant memory and every benchmark comparison against it. | docs/testing.md |

## `tests/test_bm25_weights.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_preparation_verifies_bytes_and_refreshes_identity_without_download` | infra | Preparation verifies pinned weight bytes and refreshes the receipt without downloading, so a valid air-gapped cache is reused. | docs/deploy.md#deployment-policy |
| `test_invalid_cache_requires_explicit_repair` | infra | A damaged cache fails verification without mutation and is fixed only by explicit preparation, so corrupt weights are never silently used. | docs/deploy.md#deployment-policy |
| `test_unloadable_metadata_fails_verification_and_explicit_preparation_repairs` | infra | Unreadable or foreign cache metadata fails verification and explicit preparation repairs it, so a bad sidecar cannot pass the gate. | docs/deploy.md#deployment-policy |
| `test_staged_failure_preserves_prior_cache_then_recovers` | infra | A failed or bad-checksum acquisition leaves the prior cache intact and no stage residue, so a transient failure cannot destroy a usable cache. | docs/deploy.md#deployment-policy |
| `test_failed_pin_replacement_preserves_usable_previous_cache` | infra | A failed pin bump keeps the previous cache verifying and the approved new pin installs later, so upgrades are recoverable. | docs/deploy.md#deployment-policy |
| `test_verify_only_cli_never_imports_or_acquires` | infra | Verify-only mode never imports inference code or acquires weights, so the air-gap verification step has no side effects. | docs/deploy.md#deployment-policy |
| `test_interrupted_publication_withholds_receipt_and_next_prepare_repairs` | infra | An interrupted publish withholds the completion receipt and the next preparation repairs, so a half-published cache is never certified. | docs/deploy.md#deployment-policy |
| `test_unsupported_model_refuses_before_effects` | infra | Only the pinned Qdrant/bm25 model is accepted and any other is refused before effects, so unapproved weights cannot enter the gap. | docs/deploy.md#deployment-policy |
| `test_preparation_refuses_unowned_members_without_mutation` | infra | Foreign files in the cache directory are refused without mutation, so preparation cannot overwrite or delete operator data. | docs/deploy.md#deployment-policy |
| `test_preparation_refuses_symlink_escapes` | infra | Symlinks that redirect the cache outside its root are refused, so preparation cannot write or delete outside the cache. | docs/deploy.md#deployment-policy |
| `test_cooperative_preparers_serialize_and_second_reuses` | infra | Concurrent preparers serialize on a lock and the second reuses the result, so two jobs cannot corrupt the cache or download twice. | docs/deploy.md#deployment-policy |
| `test_task_reaches_real_preparation_with_literal_inputs` | infra | The artifacts:bm25 task passes inputs literally, rejects other models and mutates nothing on refusal, so shell metacharacters in paths cannot run commands. | docs/task-runner.md |
| `test_verified_snapshot_remains_loadable_after_cache_relocation` | infra | A verified cache relocated to a new path still loads offline in the real sparse embedder with no network, so air-gapped serving can load the bundled weights. | docs/deploy.md#deployment-policy |

## `tests/test_budget.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_local_profile_resolves_to_validated_operating_points` | core | Without it the local serving profile drifts from the soak-validated GPU utilization, windows and runner flags and the pair stops fitting. | docs/live-stack.md |
| `test_local_allocation_order_is_not_brittle` | core | Without it the local pack fits only in one allocation order and a reordering produces an unfit plan. | docs/live-stack.md |
| `test_generate_falls_back_to_eager_when_compiled_tight` | core | Without it a tight host gets an OOM-prone compiled plan or a silent downgrade without notes or warning. | docs/live-stack.md |
| `test_deficit_fails_closed_with_remedies` | core | Without it a host too small for the model gets an OOM plan instead of a refusal with remedies. | docs/agent.md#http-model-contract |
| `test_zero_free_host_fails` | core | Without it a reserve covering the whole GPU is sized anyway. | docs/agent.md#http-model-contract |
| `test_max_util_guard_fails_closed` | core | Without it a tiny reserve emits gpu utilization near 1.0 and the server OOMs. | docs/agent.md#http-model-contract |
| `test_empty_profile_rejected` | core | Without it an empty profile resolves to a plan with no servers. | docs/agent.md#http-model-contract |
| `test_prod_profile_resolves` | core | Without it the production sizing requirements handed to the platform team stop resolving. | docs/deploy.md |
| `test_prefix_cache_flows_to_plan_and_explain` | core | Without it a prefix-cache request is dropped from the plan and the launcher flag. | docs/live-stack.md |
| `test_triple_pack_fits_with_measured_operating_points` | core | Without it the three-server local pack no longer reproduces the live-validated operating points. | docs/live-stack.md |
| `test_big_reasoning_triple_refuses_closed` | core | Without it the E4B plus embed plus rerank pack that does not fit 8GB resolves into an OOM plan. | docs/live-stack.md |
| `test_crc_profile_preserves_models_and_windows` | core | Without it the CRC profile changes models or windows, or loses its eager and text-only safety flags. | docs/live-stack.md |
| `test_explicit_allocation_below_footprint_refuses` | core | Without it an explicit GPU allocation below the model footprint is accepted and OOMs at load. | docs/agent.md#http-model-contract |

## `tests/test_budget_cli.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_cli_matches_library_for_every_profile_server` | infra | Without it the CLI that the launcher evals diverges from the library or emits lines that break the shell eval. | docs/live-stack.md |
| `test_unknown_profile_fails_closed` | infra | Without it an unknown profile prints something eval-able or exits zero. | docs/live-stack.md |
| `test_unknown_role_fails_closed` | infra | Without it an unknown role prints something eval-able or exits zero. | docs/live-stack.md |
| `test_check_pack_refuses_unfitting_pack_that_single_resolve_emits` | core | Without it a single server fitting alone is launched although the full pack does not fit. | docs/live-stack.md |
| `test_deficit_host_override_fails_closed_with_remedies` | infra | Without it a what-if host deficit emits flags on stdout instead of failing nonzero. | docs/live-stack.md |
| `test_check_pack_on_a_fitting_pack_emits_the_same_flags_as_single_resolve` | infra | Without it the --check-pack preflight can change or break the flag set the launcher evals for a pack that does fit. | docs/deploy.md#deployment-policy |

## `tests/test_capture_pool.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_legs_to_record_shape` | core | Captured pool records change shape so replay cannot reproduce the ranked lists and chunk table. | docs/testing.md |
| `test_legs_to_record_rejects_bad_ce` | core | NaN or non-numeric cross-encoder scores are recorded and poison replay sweeps. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_legs_to_record_rejects_bad_legs` | core | Malformed legs are recorded as valid pools. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_record_to_rows_round_trip` | core | Replay rows lose dense/sparse rank or CE data, changing swept ranking results. | docs/testing.md |
| `test_record_to_rows_rejects_unknown_chunk` | core | A leg referencing a chunk absent from the chunk table replays with missing data. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_record_to_rows_rejects_bad_record` | core | A malformed capture record is replayed instead of refused. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_capture_query_nl_kind_with_fake_legs` | core | Capture of a natural-language query records the wrong legs or metadata. | docs/testing.md |
| `test_capture_query_identifier_kind_bypasses_ce` | core | Identifier queries get cross-encoder scores that production search never computes. | docs/testing.md |
| `test_capture_query_explicit_reranker_scores_nl_pool` | core | An explicit reranker is not used on NL pools so the captured CE scores are absent. | docs/testing.md |
| `test_capture_query_depth_records_deeper_never_shallower` | core | A capture is shallower than production prefetch so replay at the production depth lacks candidates. | docs/testing.md |
| `test_capture_query_ce_scores_stop_at_ce_depth_per_leg` | core | CE scoring goes deeper than replay can use, or leaves replayable hits unscored. | docs/testing.md |
| `test_record_to_rows_headings_replace_placeholder_and_refuse_gaps` | core | Replay uses placeholder headings or a partial heading join, skewing heading-based ranking. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_legs_to_record_keeps_physical_page_for_diversification` | core | Replay loses the physical page, changing page diversification results. | docs/testing.md |
| `test_capture_cli_refuses_out_of_range_depth` | infra | An out-of-range depth starts a capture and writes an output file. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_task_capture_defaults_and_overrides` | infra | The capture Task picks the wrong golden/out path, or captured files contain manual text. | AGENTS.md hard boundaries |
| `test_task_capture_holdout_refusal_and_recovery` | infra | A holdout capture runs with a bad pin or in dev venue and overwrites a good capture. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_capture_explicit_cli_and_empty_inputs` | infra | Missing or empty golden/out arguments run a capture instead of failing. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_capture_retry_records_the_pool_search_prefetches` | core | Capture records a pool search would never fetch, such as a known-wrong sibling code on the retry filter. | docs/testing.md |
| `test_legs_to_record_empty_query_rejected` | core | Without it a capture with an empty query is recorded as a valid pool and later replay sweeps score a question nobody asked. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_record_to_rows_refuses_out_of_range_leg_and_max_rank` | core | Without it replay silently selects a nonexistent leg or an invalid depth, so a sweep reports rankings from the wrong or unbounded prefetch pool. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_capture_bundle_dir_defaults_golden_and_output_name_and_honors_max_queries` | core | Without it --bundle-dir reads the wrong golden file, writes pools outside the dated bundle name or ignores --max-queries, so capture bundles are unfindable or incomplete. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_capture_records_failed_query_as_error_row_exits_nonzero_and_next_run_succeeds` | core | Without it a failing query aborts or hides in a capture run, or the run exits 0 with a missing pool, so a replay sweep silently drops queries. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |

## `tests/test_chart_lifecycle_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_full_sequence_green` | infra | Without it the lifecycle rehearsal (install, upgrade, failed upgrade, rollback, redeploy) can skip steps or mutate the data namespace unnoticed. | docs/testing.md#helm-coverage |
| `test_fault_unexpected_success_fails` | infra | Without it a fault injection that unexpectedly succeeds passes and the rollback path is never proven. | docs/testing.md#helm-coverage |
| `test_chart_pvc_change_trips_guard` | infra | Without it a chart-namespace PVC replacement during upgrade passes and data loss goes unnoticed. | docs/testing.md#helm-coverage |
| `test_data_pvc_change_trips_guard` | infra | Without it a data-plane PVC identity change during the rehearsal is accepted. | docs/testing.md#helm-coverage |
| `test_statefulset_trips_guard` | infra | Without it a StatefulSet appearing in the chart namespace is accepted. | docs/testing.md#helm-coverage |
| `test_statefulset_in_manifest_trips_guard` | infra | Without it a release manifest that owns a StatefulSet is accepted although the chart must not own Qdrant. | docs/testing.md#helm-coverage |
| `test_smoke_failure_blocks` | infra | Without it a failing smoke search does not stop the rehearsal before the next upgrade. | docs/testing.md#helm-coverage |
| `test_b_mark_mismatch_blocks` | infra | Without it the rehearsal continues although the B revision is not actually serving. | docs/testing.md#helm-coverage |
| `test_preexisting_namespace_is_kept` | infra | Without it the rehearsal deletes a namespace it did not create. | docs/testing.md#helm-coverage |
| `test_arg_validation_before_cluster_calls` | infra | Without it a missing argument runs cluster commands before failing. | docs/testing.md#helm-coverage |
| `test_missing_data_ns_fails_closed` | infra | Without it a missing DATA_NS lets the rehearsal run without the data-plane read-only guard. | docs/testing.md#helm-coverage |
| `test_failed_cluster_reads_block_acceptance` | infra | Without it failed PVC or manifest reads are treated as no change and the rehearsal reports success. | docs/testing.md#helm-coverage |
| `test_values_paths_reach_helm_without_word_splitting` | infra | Without it values paths with spaces or brackets are split and helm receives wrong files. | docs/testing.md#helm-coverage |
| `test_published_bundle_entry_uses_resolved_operator_values` | infra | Without it the bundle rehearsal entry uses wrong or unquoted operator values such as a revision with pipes. | docs/testing.md#helm-coverage |
| `test_admission_rejection_is_not_failed_rollout_evidence` | infra | Without it an admission rejection of the fault image is counted as a rollout failure proof. | docs/testing.md#helm-coverage |

## `tests/test_chat_api.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_chat_completions_single_turn_json` | core | OpenAI-compatible chat clients lose the chat.completion shape, the reasoning model name, citations, hits or verification state. | docs/agent.md#http-model-contract |
| `test_chat_native_route_matches_openai_alias` | core | The native /v1/chat and /v1/chat/completions routes could diverge so one client family gets different answers or citations. | docs/agent.md#http-model-contract |
| `test_chat_completions_stream_error_is_openai_error` | core | A mid-stream model failure would end with a fake finish_reason or no error object, so clients treat a broken answer as complete. | docs/agent.md#http-model-contract |
| `test_chat_completions_json_incomplete_upstream_is_502` | core | An upstream response lacking an explicit finish would be returned as a successful stop instead of the fixed 502 envelope. | #365 |
| `test_chat_completions_stream_error_frame_then_done_is_failure` | core | An upstream error frame followed by [DONE] could be streamed as success or leak the upstream error text to the client. | #365 |
| `test_chat_completions_empty_hits_short_circuit` | core | With zero retrieved excerpts the model would be called and could invent an ungrounded answer instead of the deterministic no-excerpts message. | docs/agent.md#answer-contract |
| `test_chat_completions_not_configured_fail_fast` | core | A missing reasoning model would surface as an upstream failure rather than the stable 503 not_configured error. | docs/agent.md#http-model-contract |
| `test_chat_completions_streaming_sse` | core | Streaming chat clients lose chunk framing, token deltas, the terminal citations chunk or the [DONE] sentinel. | docs/agent.md#http-model-contract |
| `test_chat_configured_xhigh_reaches_model` | core | A configured xhigh reasoning effort would be silently dropped on chat routes so the operator tuning has no effect. | docs/agent.md#http-model-contract |
| `test_build_chat_messages_pruning_and_cap` | core | Old excerpts or oversized history would be carried into the chat prompt, overrunning the window and mixing stale evidence with the fresh manifest. | docs/agent.md#answer-contract |
| `test_condense_query_direct_bypass` | core | Follow-ups containing a message id or abend code would be rewritten by the model, changing the identifier the user asked about. | ADR-0004 |
| `test_chat_json_surfaces_script_with_review_flag` | core | A cited script would be dropped from chat JSON or served without the review flag, so it could be run unreviewed. | #365 |
| `test_chat_stream_terminal_chunk_carries_state_and_script` | core | Streaming chat clients would never receive the authoritative verification state, script and citations in the terminal chunk. | #365 |
| `test_chat_active_user_agrees_across_retrieval_and_prompt` | core | A trailing assistant or system message could become the searched or prompted question, retrieving and answering the wrong thing. | docs/agent.md#answer-contract |
| `test_chat_blank_active_user_refuses_before_any_work` | core | A blank active user turn would fall back to the assistant tail and trigger retrieval and model calls instead of a 422. | #579 |
| `test_chat_unsearchable_active_user_refuses_before_any_work` | core | Whitespace or control-character questions would reach embedding, Qdrant and the model instead of the fixed 422. | #579 |
| `test_chat_condensation_uses_only_history_before_normalized_user` | core | Condensation could use later or untrusted system turns, or an unstructured or failing rewrite could change the searched question. | ADR-0004 |
| `test_chat_invalid_active_turn_has_no_model_or_retrieval` | core | Empty, user-less or overlong chat bodies would cost retrieval and model calls instead of failing validation up front. | #314 |
| `test_chat_rejects_out_of_range_temperature` | core | Out-of-range temperature overrides would reach the model and surface as upstream 502 instead of a client 422. | #596 |
| `test_chat_temperature_rejects_non_finite` | core | NaN or infinite temperatures would pass request validation and be forwarded to the model. | #596 |
| `test_chat_ignored_tail_still_counts_toward_body_limit` | core | Padding after the active turn could bypass the chat body-size bound. | docs/agent.md#http-model-contract |
| `test_direct_chat_builders_share_active_user` | core | Direct callers of the chat builders would pick a different active question than the HTTP route or mutate the caller's messages. | docs/agent.md#answer-contract |
| `test_chat_json_and_stream_carry_supplied_evidence` | core | Chat clients cannot audit which excerpt text supported an answer, or JSON and SSE would report different evidence. | #635 |

## `tests/test_check_live.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_incomplete_stream_refused` | infra | Without it live acceptance accepts an HTTP-successful stream that errored, was truncated, duplicated final or has trailing events. | docs/live-stack.md#verification-minimums |
| `test_http_200_is_not_grounded_acceptance` | infra | Without it an incomplete, ungrounded or citation-free 200 response counts as grounded acceptance. | docs/live-stack.md#verification-minimums |
| `test_complete_run_and_terminal_error` | infra | Without it acceptance passes a run with a failed console follow-up or a trap query that did not refuse, or leaks answer text in the report. | docs/live-stack.md#verification-minimums |

## `tests/test_check_pod_images.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_runtime_prefixes_and_every_replica` | infra | Without it a runtime-prefixed imageID or a single unchecked replica could pass the pinned-digest release gate. | docs/deploy.md#deployment-policy |
| `test_different_digest_is_reported` | infra | Without it a pod running a different image than the verified digest is accepted as the release. | docs/deploy.md#deployment-policy |
| `test_unrelated_diagnostic_jobs_do_not_invalidate_rollout` | infra | Without it leftover diagnostic Job pods cause false release-gate failures. | docs/deploy.md#deployment-policy |
| `test_wrong_target_repository_cannot_hide_behind_expected_diagnostic` | infra | Without it a pod from a wrong repository could be masked by an unrelated pod carrying the expected digest. | docs/deploy.md#deployment-policy |
| `test_wrong_workload_template_repository_is_rejected` | infra | Without it a workload template pointing at an unapproved repository passes the pinned-image check. | docs/deploy.md#deployment-policy |
| `test_required_replica_without_status_fails_despite_healthy_replica` | infra | Without it a replica with no container status is ignored because a sibling replica is healthy. | docs/deploy.md#deployment-policy |
| `test_missing_replica_cannot_be_supplied_by_unrelated_pod` | infra | Without it a missing replica is papered over by an unrelated pod and the rollout is wrongly certified complete. | docs/deploy.md#deployment-policy |
| `test_terminating_old_replica_does_not_invalidate_current_rollout` | infra | Without it a terminating old-image pod blocks a correct rollout from being accepted. | docs/deploy.md#deployment-policy |
| `test_old_deployment_replicaset_does_not_supply_current_pods` | infra | Without it pods of an old ReplicaSet are counted as the current rollout and stale images are certified. | docs/deploy.md#deployment-policy |
| `test_current_workload_selection_is_required` | infra | Without it pods from another revision, owner or finished phase are counted as current replicas. | docs/deploy.md#deployment-policy |
| `test_completed_init_container_still_requires_verified_image` | infra | Without it an init container running an unverified image escapes the pinned-image gate after completing. | docs/deploy.md#deployment-policy |
| `test_cli_exit_codes_and_fixed_refusals` | infra | Without it the shell pipeline cannot tell pass, mismatch, bad input and usage errors apart and may continue a failed release. | docs/deploy.md#deployment-policy |
| `test_service_container_cannot_be_replaced_by_same_named_init_container` | infra | Without it an init container named like the service satisfies the check while the real service image is never verified. | docs/deploy.md#deployment-policy |
| `test_oauth_proxy_requires_its_own_container_status` | infra | Without it the OAuth proxy sidecar image goes unverified when only the agent container reports status. | docs/deploy.md#deployment-policy |

## `tests/test_chunk_ibm_shape.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_outline_maps_sections` | core | Outline bookmarks become nested heading paths and front matter and notices are skipped, so citations show the right section. | docs/ingest.md |
| `test_chunk_id_stable_across_runs` | core | Chunk ids are identical across re-chunking the same PDF, so re-ingest overwrites instead of duplicating points. | docs/ingest.md#identity-contract |
| `test_message_chunk_ids_members_and_page_label` | core | A message section yields a message chunk with ids, members, deepest heading and printed page label, so message lookups cite correctly. | docs/ingest.md |
| `test_long_section_split_with_overlap` | core | Long sections split into ordered chunks with the overlap tail, so no passage is lost at a split boundary. | docs/ingest.md |
| `test_detect_code_region_matrix` | core | JCL, REXX and console regions are detected and prose with URLs or inline comments is not, so only real code is made statement-atomic. | issue 79 |
| `test_jcl_statement_grouping` | core | JCL cards group with continuations and wrapped cards rejoin while bare delimiters stay separate, so statements are never split mid-card. | issue 79 |
| `test_rexx_comment_quote_and_continuation` | core | REXX statements respect comments, quotes and continuations, so programs are not sliced inside a statement. | issue 79 |
| `test_jcl_fixture_statements_never_split` | core | On a real generated PDF every JCL statement stays whole and no chunk opens on a continuation, so retrieved JCL is runnable. | issue 79 |
| `test_instream_data_splits_between_lines_not_as_atom` | core | SYSIN data splits between records and never as one giant atom, so oversize in-stream data cannot overflow the embed window. | issue 79 |
| `test_code_runs_share_single_newlines` | core | Adjacent code items join with single newlines and prose keeps the double newline, and block page spans are correct, so exact-card sparse matching and page citations hold. | issue 79 |
| `test_rexx_fixture_statements_never_split` | core | Generated REXX PDFs keep every statement and the block comment whole with stable ids, so retrieved REXX is intact. | issue 79 |
| `test_oversize_code_para_splits_at_statement_starts` | core | Oversize code splits only at statement starts, an oversize single statement stays whole and overlap seeds back off to whole statements. | issue 79 |
| `test_oversize_table_splits_at_row_boundaries` | core | Oversize tables split at row boundaries with whole-row overlap and code is not mistaken for a table, so rows are never char-sliced. | issue 216 |
| `test_mixed_prose_jcl_extracts_cards` | core | In a mixed paragraph JCL cards become atomic while prose stays a blob and a lone //see mention stays prose. | issue 79 |
| `test_chunk_id_pins_uuid5_namespace_and_key` | core | The point id is the literal UUID5 of the revision-keyed string, so a namespace or key-format change cannot silently re-id the whole corpus. | AGENTS.md hard boundary: UUID5 chunk-key identity |
| `test_fallback_sections_split_no_toc_book` | core | A book with no outline is sectioned by heading leads and windowed deterministically, so generic PDFs do not collapse into one blob. | AGENTS.md hard boundary: parsing stays generic |
| `test_nested_outline_chunks_deepest_once_no_duplicates` | core | Nested outlines chunk each page once under the deepest entry and same-page parents lose no text, so there are no duplicate chunks or dropped pages. | issue 577 |
| `test_skipped_child_heading_does_not_cut_parent` | core | Skipped child headings never bound a kept parent, so no pages are lost from the outline. | issue 577 |
| `test_make_chunks_stores_physical_span_and_honest_label` | core | Each chunk stores its physical page span and a printed label only when labels locate the whole span, with identity still pinned to the start page. | issue 271 |
| `test_make_chunks_single_labeled_page_keeps_label` | core | A single-page chunk keeps even a repeated folio as its label, so single pages remain locatable. | issue 271 |
| `test_deep_outline_pdf_chunks_each_page_once_under_deepest_kept_section` | core | On a real 4-level outline PDF every body page is chunked once and partially labeled pages never fail or invent a label. | issue 577 |
| `test_column_major_table_pdf_keeps_text_cells_and_location` | core | Column-major tables come out as whole name-default-meaning rows in order, so table values stay associated with their keys. | issue 85 |
| `test_change_bar_pdf_keeps_prose_in_order` | core | Revision-bar glyph lines are dropped and prose stays in order, so change bars do not pollute chunks. | issue 85 |
| `test_column_major_wrapped_rows_and_repeated_headers` | core | Wrapped cell tails and repeated header rows reassemble correctly and only reorder words, so table facts are not lost or invented. | issue 85 |
| `test_prose_beside_table_is_not_merged_into_rows` | core | Prose beside a table stays prose while the table still becomes rows, so sidebars are not fused into data. | issue 85 |
| `test_multicolumn_prose_extraction_is_byte_identical_to_plain` | core | Table correction never touches multi-column or single-column prose, so ordinary text extraction is unchanged. | issue 85 |
| `test_row_major_drawn_table_cells_share_a_line` | core | Row-major drawn tables also yield whole rows, so table facts are associated regardless of draw order. | issue 85 |
| `test_lone_header_pair_on_one_baseline_is_not_merged` | core | Running header or footer pairs on one baseline are not treated as tables, so page chrome is not fused into rows. | issue 85 |
| `test_short_independent_column_procedures_keep_original_sequence` | core | Short side-by-side procedure columns keep their original sequence, so steps are not interleaved into a wrong procedure. | issue 85 |
| `test_short_prose_beside_captioned_table_keeps_associations` | core | A short sidebar next to a captioned table stays out of the row set, so table rows keep correct associations. | issue 85 |
| `test_centered_multiword_headings_keep_wrapped_row_associations` | core | Centered multi-word headings with wrapped cells still produce correct rows, so real-world tables keep key-value associations. | issue 85 |
| `test_overlapping_headings_cannot_share_one_inferred_data_column` | core | Ambiguous overlapping headings are left as plain text, so a wrong column inference cannot swap values between keys. | issue 85 |
| `test_standalone_diagram_bar_in_body_is_preserved` | core | A lone vertical bar in a diagram is kept rather than dropped as a change bar, so diagrams survive extraction. | issue 85 |
| `test_margin_positioned_diagram_wall_keeps_its_bars` | core | A margin wall of bars in a diagram is preserved, so only real revision bars are removed. | issue 85 |
| `test_table_associations_round_trip_through_stored_and_queried_payload` | core | Table row associations survive storage and query so the stored payload and the queried hit carry identical whole rows. | issue 85 |
| `test_same_page_bookmarks_each_get_their_own_section` | core | Several bookmarks on one page each get their own section and cited chunk with no byte lost or repeated, so message manuals cite the right message. | docs/ingest.md |
| `test_same_page_bookmarks_fall_back_to_the_whole_page` | core | If a later bookmark title is missing or out of order the page stays one section, so bad page text cannot mis-cut messages. | docs/ingest.md |
| `test_same_page_split_needs_page_text_and_distinct_headings` | core | Same-page splitting needs page text and distinct headings, so duplicate headings cannot mint one chunk key twice. | docs/ingest.md#identity-contract |
| `test_same_page_message_bookmarks_end_to_end` | core | On a generated PDF each message bookmark becomes its own message chunk with its own id, so per-message lookup works end to end. | docs/ingest.md |
| `test_recurring_subsection_titles_do_not_open_pieces` | core | Subsection titles recurring under every entry stay with their entry, so tiny context-free duplicate chunks do not appear. | docs/ingest.md |
| `test_same_page_bookmarks_keep_outline_order_for_heading_paths` | core | Deeper entries stay under their own parent when a shallower page-mate follows, so heading paths cite the right section. | issue 663 |

## `tests/test_ci_application_contracts.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_stream_success_and_reported_failure` | gate-control | The e2e stream checker accepts a failure as success or a success as an expected failure on either wire protocol. | docs/live-stack.md#verification-minimums |
| `test_bad_stream_never_counts_as_success` | gate-control | An empty, truncated, malformed, length-finished, uncited or late-token stream counts as success. | docs/live-stack.md#verification-minimums |
| `test_checker_reaches_all_http_paths` | infra | The e2e acceptance run skips /v1/answer, /v1/chat, /v1/chat/completions or the console stream. | docs/live-stack.md#verification-minimums |
| `test_expected_failure_rejects_false_success` | gate-control | A fault run where the agent still answers successfully passes as the expected failure. | docs/live-stack.md#verification-minimums |

## `tests/test_ci_gateway.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_ingest_local_patch_reproduces_quote_sensitive_predicate` | infra | The gateway rendering gate fails on valid kubectl-normalized YAML, or valid single-key rendering is not recognized as passing. | AGENTS.md#hard-boundaries |
| `test_gateway_check_identifies_each_wrong_or_missing_reference` | gate-control | A rendered consumer with a wrong, missing, optional or plaintext gateway key reference passes and leaks the key. | AGENTS.md#hard-boundaries |
| `test_gateway_check_identifies_wrong_url` | gate-control | A consumer pointed at the wrong gateway URL passes, or the diagnostic leaks credentials in the URL. | AGENTS.md#hard-boundaries |
| `test_gateway_check_rejects_placeholders_with_attribution` | gate-control | An unresolved gateway placeholder in rendered manifests passes without naming the resource. | docs/deploy.md#deployment-policy |
| `test_gateway_check_retains_leg_overrides_and_legacy_secret_keys` | infra | Per-leg URL overrides and legacy secret keys are rejected, or unrelated secret data appears in diagnostics. | AGENTS.md#hard-boundaries |
| `test_gateway_check_requires_exact_consumer_identity` | gate-control | Renames of the agent container or deployment, duplicate env entries or a wrong model alias pass the check. | docs/deploy.md#deployment-policy |
| `test_gateway_check_requires_exact_context_model` | gate-control | A wrong, missing or secret-sourced CONTEXT_LLM_MODEL in the ingest job passes and leaks the sentinel. | docs/deploy.md#deployment-policy |
| `test_gateway_check_rejects_source_context_model_drift` | gate-control | Operator configuration whose context model differs from the selected value passes. | docs/deploy.md#deployment-policy |
| `test_gateway_check_round_trips_context_model_and_rejects_drift` | infra | A context model with spaces and separators (or empty) is mangled between airgap.env and the ingest Job. | docs/deploy.md#deployment-policy |
| `test_workflow_has_no_duplicate_mapping_keys` | infra | A duplicate YAML key makes GitHub reject e2e.yml and silently skip every job and check. | docs/testing.md#evidence-design |
| `test_fault_lane_requires_contract_failure_and_recovery` | gate-control | The gateway fault lane passes when the injected fault caused no failure, an unrelated failure, or no recovery. | docs/live-stack.md#verification-minimums |
| `test_parallel_build_join_preserves_failure_and_sha_bound_inputs` | gate-control | A failed build-input, image or select job leaves the build gate green, or inputs are not bound to the commit. | docs/testing.md#evidence-design |
| `test_installer_artifact_corruption_stops_before_installation` | infra | CI installs a checksum-corrupt kubectl, kind or tool download. | docs/deploy.md#deployment-policy |

## `tests/test_ci_gitlab_parity.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_current_gitlab_definition_conforms` | gate-control | The air-gapped GitLab gate drifts from the GitHub gate (lint scope, junit gate, sim selection, digest, hazards). | docs/deploy.md#deployment-policy |
| `test_sim_exclusions_are_exactly_the_docker_driving_integration_files` | infra | The offline GitLab sim lane silently omits integration files, or runs files that need Docker. | docs/deploy.md#deployment-policy |
| `test_static_rules_reject_each_weakened_definition` | gate-control | A weakened GitLab job (allow_failure, masked status, narrowed selection, missing doctor) is not detected by the parity rules. | docs/testing.md#evidence-design |
| `test_lint_job_fails_when_any_tool_fails` | gate-control | A failing ruff or mypy leaves the GitLab lint job green or keeps running after the failure. | docs/testing.md#evidence-design |
| `test_unit_and_sim_jobs_reject_every_non_clean_outcome` | gate-control | A skipped, xfail, failed, empty or missing junit report passes the GitLab unit or sim job. | docs/testing.md#evidence-design |
| `test_stale_report_cannot_satisfy_the_gate` | gate-control | A clean junit left by an earlier run satisfies a run that wrote no report. | docs/testing.md#evidence-design |
| `test_sim_job_prerequisites_fail_before_pytest` | infra | The GitLab sim job runs tests with an unverified BM25 cache, failing doctor or missing archives. | docs/deploy.md#deployment-policy |
| `test_gate_l1_regression_fails_the_job_and_posts_no_note` | gate-control | A gate-l1 regression exits green or posts an advisory note after failing. | docs/live-stack.md#verification-minimums |
| `test_real_pytest_reports_classify_as_the_gate_requires` | gate-control | The junit-clean checker accepts real pytest skip, xfail or collection-error reports. | docs/testing.md#evidence-design |
| `test_junit_gate_uses_the_github_evidence_counting_rule` | gate-control | The GitLab junit gate and GitHub evidence counter disagree on what a clean report is. | docs/testing.md#evidence-design |

## `tests/test_ci_lifecycle.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_snapshot_check_restores_and_compares_data` | infra | The snapshot lane passes without restoring the snapshot or with different counts or exact query results. | docs/live-stack.md#verification-minimums |
| `test_snapshot_job_inherits_read_only_security_with_its_own_temporary_storage` | infra | The snapshot maintenance job loses read-only root, dropped capabilities or attaches ingest corpus volumes. | AGENTS.md#hard-boundaries |
| `test_lifecycle_runs_snapshot_as_writer_job_and_cleans_up` | infra | The lifecycle lane uses the read-only serving key for snapshots, skips cleanup or hides a failed step. | AGENTS.md#hard-boundaries |
| `test_snapshot_job_refuses_wrong_credential_reference` | gate-control | The snapshot job accepts a read-only or literal Qdrant key instead of the writer Secret reference. | AGENTS.md#hard-boundaries |
| `test_snapshot_helper_refuses_bad_arguments_before_cluster_calls` | infra | A bad namespace argument reaches kubectl before being refused. | docs/live-stack.md#verification-minimums |

## `tests/test_classify_messages.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_message_families_beyond_classic` | core | Anchored message ids of CICS, IMS, DB2 and security families classify as message, so those manuals get message chunk_type without a new vocabulary. | docs/ingest.md |
| `test_message_buried_after_explanation` | core | An id card after one or two explanation lines still classifies as message while mid-line mentions and cards past the scan window stay narrative. | docs/ingest.md |
| `test_syntax_table_and_narrative` | core | Syntax diagrams, column tables and prose classify to their chunk types, so retrieval type filtering sees the right type. | docs/ingest.md |
| `test_chrome_strip` | core | Repeated running headers and footers are detected on long page lists and stripped while body lines stay, so chrome does not pollute chunks. | docs/testing.md |
| `test_strip_page_keeps_roman_letter_words` | core | Words made of roman-letter characters such as XML are kept and only strict numerals are dropped as folios, so content is not deleted. | docs/ingest.md |
| `test_strip_page_drops_bare_page_numbers_at_page_edges` | core | Folios are removed only at page edges with one folio per page, so body numbers and codes are preserved. | issue 604 |
| `test_strip_page_drops_entry_range_headers_above_a_footer_folio` | core | Entry-range headers such as ICH408I bullet ICH409I are stripped at page edges but ordinary bullet pairs and body ranges stay, so message manuals are not polluted or truncated. | issue 604 |
| `test_strip_page_keeps_blank_lines_and_inner_dot_numbers` | core | Blank lines and section numbers like 1.2 survive stripping, so layout and headings are not damaged. | docs/ingest.md |
| `test_chunk_type_vocabulary_is_frozen` | core | chunk_type stays exactly message, syntax, table, narrative and each sample classifies into it, so no new vocabulary value leaks into payloads. | AGENTS.md hard boundary: four-type chunk_type vocabulary |

## `tests/test_code_entries.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_code_entries_split_into_atomic_entries` | core | Bare completion-code entries split into atomic units with prose kept separate, so a code lookup returns just its own entry. | issue 591 |
| `test_index_runs_and_code_like_words_are_not_entries` | core | Index runs and inline code-like words are not split as entries, so index pages do not become false code entries. | issue 591 |
| `test_every_entry_code_is_recorded_not_just_the_first` | core | Every entry code in a chunk is recorded in canonical form including user and wait-state codes, so system-code filters can select any entry. | issue 591 |
| `test_make_chunks_records_every_entry_without_touching_text` | core | The payload lists every entry code, the stored text is the manual's own words with unit spans aligned, and wait-state codes get the W form. | issue 591 |
| `test_query_and_payload_agree_on_every_code_family` | core | The query parser and ingest emit identical code strings per family and the filter targets the stored field, so the code filter can actually match. | issue 591 |
| `test_strip_chrome_keeps_entry_lines_and_strips_only_chrome` | core | Bare entry code lines and labels survive chrome stripping while footers, range lines and folios go, so codes like 806 are not deleted as folios. | issue 604 |
| `test_make_chunks_after_strip_chrome_records_every_entry_code` | core | Through the real strip-then-chunk path every entry code including 806 and 064 reaches the stored system_codes field. | issue 604 |
| `test_code_section_gate_records_labelled_sections_only` | core | Codes are recorded only from labelled code sections, so module tables and index pages cannot pass the system-code prefilter. | issue 621 |
| `test_label_opening_the_next_page_confirms_the_section` | core | A label opening the next page still confirms the section as a code section, so entries split across a page break are recorded. | issue 621 |
| `test_dump_listing_outside_a_code_section_stays_within_the_cap` | core | A dump listing outside a code section stays under the chunk cap, so it cannot overflow the embed window. | issue 612 |
| `test_oversize_entry_in_a_code_section_is_cut_at_line_boundaries` | core | An oversize entry is cut at line boundaries with its code recorded, so no chunk overflows the window and no line is sliced. | issue 621 |

## `tests/test_colab.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_ingest_reingest_is_refused_outside_production_before_state` | infra | Without it a synthetic run accepts a production-only reingest choice and publishes state for it (#737). | #737 |
| `test_cleanup_observes_usage_after_last_work_poll_before_exact_release_then_next_run_works` | infra | Without it the final CU spend is not observed before release, so the record of paid usage is missing or stale. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_orphan_assignment_cannot_establish_release_then_named_recovery_allows_next_run` | infra | Without it an unnamed orphan assignment is stopped by endpoint guess (possibly a foreign VM) or marked released without proof. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_unbound_allocation_with_empty_listing_remains_unverified` | infra | Without it an empty session listing after an unknown allocation is accepted as an absence proof and a billing VM is left running. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_generated_ssh_proxy_never_reallocates_pruned_session_then_next_run_works` | infra | Without it the generated SSH proxy allocates a new paid session after the owned one is pruned. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_fresh_run_refuses_preexisting_private_cli_store_without_any_cloud_command` | infra | Without it a run adopts a pre-existing CLI store holding foreign sessions and may stop or reuse them. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_unavailable_final_usage_is_explicit_and_cannot_block_exact_release_then_next_run` | infra | Without it an unavailable usage read blocks release or is silently recorded as a spend figure. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_manual_recovery_records_final_usage_or_explicit_unavailable_and_still_releases` | infra | Without it manual stop recovery omits the final usage record or skips release when usage is unavailable. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_successful_workload_with_unavailable_final_usage_has_no_cpu_receipt_then_next_run_works` | infra | Without it a CPU receipt is issued although final CU accounting could not be observed. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_native_gateway_pg_readiness_interruption_and_recovery_preserve_foreign_objects_then_next_serve` | infra | Without it interruption during gateway Postgres readiness leaks the owned container or removes a foreign Docker object. | docs/colab-playground.md#keep-the-existing-host-resource-owner |
| `test_fresh_cli_proves_storage_releases_forward_and_next_ordinary_prepare_works` | infra | Without it fresh initialization writes the retained binding without proving the target storage or leaves its Qdrant forward running. | AGENTS.md hard boundaries (Qdrant unprivileged ClusterIP; writer credentials) |
| `test_fresh_cli_refusal_releases_forward_then_retry_reproves_target` | infra | Without it a refused fresh initialization leaves a forward bound or a retry reuses a stale proof of the replaced producer. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_fresh_http_progress_cannot_extend_absolute_deadline` | noise-kept | Without it a trickling Qdrant response extends the fresh initialization past its absolute deadline. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_fresh_cleanup_finishes_before_honoring_another_interrupt` | infra | Without it a second interrupt during forward cleanup orphans the kubectl forward. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_production_prepare_refuses_maxima_only_census_without_independent_inputs` | infra | Without it production prepare accepts a maxima-only token census and publishes unmeasured evidence as prepared. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_bad_assignment_releases_only_owned_session_before_model_runtime` | infra | Without it a wrong GPU assignment starts model runtime, or release stops a session this run does not own. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_spent_cu_cap_refuses_even_when_current_rate_is_zero` | infra | Without it a run continues after the CU cap was already spent because the current usage rate reads zero. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_interrupt_quiesces_workload_before_remote_export_and_release` | infra | Without it Ctrl-C exports artifacts and releases the VM while the workload still writes, losing or corrupting results. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_workload_executes_the_prepared_checkout_from_a_decoy_cwd` | infra | Without it the workload runs the caller's mutable checkout instead of the frozen prepared execution tree. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_paid_execution_uses_frozen_uploads_gateway_task_and_fresh_imports_during_origin_mutation` | infra | Without it editing the origin tree during a paid run changes the code, gateway or Task actually executed. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_tampered_execution_snapshot_refuses_before_tools_then_restored_snapshot_runs` | infra | Without it a tampered frozen source, Task or dotenv executes tools, or the refusal blocks the restored snapshot. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_prepare_publishes_only_exact_bounded_source_and_pinned_tool_then_next_candidate_runs` | infra | Without it prepare publishes a source or Task tool that changed during copy or exceeds its bounds. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_prepare_copy_enforces_separate_source_and_tool_caps_without_allocating` | infra | Without it source and tool copies can exceed their byte or entry caps before any allocation check. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_snapshot_recovery_releases_owned_session_despite_changed_origin_then_next_run_works` | infra | Without it stop recovery executes the changed origin or fails to release the owned VM after a persistent stop failure. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_separate_run_states_share_assignment_ownership_and_allow_next_run` | infra | Without it two run states each start a paid assignment on one account. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_slow_diagnostics_poll_is_not_ready_then_run_completes_and_next_run_works` | noise-kept | Without it one slow readiness poll fails a healthy session instead of counting as not ready yet (#746). | #746 |
| `test_transient_budget_poll_failure_keeps_session_then_run_completes_and_next_run_works` | noise-kept | Without it a one-off usage-read failure aborts a healthy paid run (#746). | #746 |
| `test_budget_poll_failure_without_fresh_owned_observation_refuses_then_releases` | infra | Without it a stale CU observation is tolerated indefinitely so the cap is not enforced. | #746; docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_failed_poll_right_after_allocation_cannot_bound_cap_and_fails_closed` | infra | Without it a run continues with a work expiry not bounded by an owned CU observation. | #746; docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_transient_release_failure_is_retried_until_absence_then_next_run_works` | infra | Without it a failed stop or absence check leaves the session billing unverified. | #746; docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_slow_export_cannot_consume_owned_vm_release_budget` | noise-kept | Without it a slow artifact export eats the time reserved for releasing the VM. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_failed_stop_upload_still_attempts_download_and_stays_incomplete_then_next_run_works` | infra | Without it a failed stop upload skips the artifact download or the run is reported complete and certified. | #740; docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_slow_stop_upload_still_exports_artifacts_and_issues_cpu_receipt_then_next_run_works` | noise-kept | Without it a stop upload slower than the former fixed cap drops the artifact export and the CPU receipt. | #740 |
| `test_cli_capture_keeps_stdout_separate_from_bounded_noisy_stderr_and_allows_next_command` | infra | Without it noisy CLI stderr is unbounded or mixes into the stdout the controller parses for identity. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_oversize_cli_stdout_cannot_establish_partial_structured_identity` | infra | Without it truncated CLI stdout is parsed as a complete session identity. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_spawned_noisy_streams_are_capped_drained_and_followed_by_an_ordinary_child` | infra | Without it a noisy spawned child fills disk or blocks on an undrained pipe. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_late_scheduled_drain_keeps_complete_identity_output_then_next_command_works` | noise-kept | Without it a late-scheduled drain thread truncates identity-shaped output and fails a pre-allocation command (#755). | #755 |
| `test_root_exit_with_descendant_held_pipes_is_bounded_and_next_command_works` | infra | Without it a descendant holding the pipes hangs the controller or yields partial identity output as complete. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_cli_timeout_reaps_an_uncooperative_root_and_allows_next_command` | infra | Without it a timed-out CLI that ignores SIGTERM stays alive and blocks cleanup. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_invalid_cli_timeout_is_refused_before_launch` | infra | Without it a NaN, zero or non-numeric timeout launches an unbounded cloud command. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_capture_write_allowance_persists_across_controller_recovery` | infra | Without it a controller restart resets the output allowance and permits unbounded log growth. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_output_limits_never_prevent_owned_release_and_a_fresh_ordinary_operation` | infra | Without it an exhausted or corrupt output budget blocks the release of an owned billing VM. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_prepared_dirty_source_change_refuses_before_allocation_then_ordinary_cpu_run` | infra | Without it a run allocates after the prepared source changed from the verified candidate. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_prepared_candidate_and_execution_identity_drift_refuses_before_allocation` | infra | Without it drifted candidate, profile or tool identity still allocates a paid VM. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_successful_cpu_receipt_is_created_only_after_exact_release_absence` | infra | Without it a CPU transport receipt is issued before the release absence proof exists. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_explicit_private_docker_gateway_is_frozen_and_checked_before_allocation` | infra | Without it an ambient gateway address replaces the prepared one, or an unavailable listener is found after allocation. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_non_private_or_non_numeric_docker_gateway_refuses_preparation` | infra | Without it a public, loopback or malformed Docker gateway address is accepted and exposes the model gateway. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_cpu_transport_survives_delayed_listener_inside_run_deadline_then_next_run_works` | noise-kept | Without it a listener that binds late inside the deadline fails a good CPU transport run. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_sandbox_hold_hands_off_without_snapshot_interpreter_then_releases_and_next_run_works` | infra | Without it the sandbox hold needs the snapshot interpreter or skips release after the hand-off. | docs/colab-playground.md#sandbox-lane-daily-iteration |
| `test_failed_cpu_run_has_no_receipt_and_recovery_allows_a_fresh_normal_run` | infra | Without it a failed workload, download or stop still produces a CPU transport receipt. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_gpu_lock_must_validate_before_allocation_and_valid_prerequisites_allow_next_run` | infra | Without it a missing, ranged or stale GPU inference lock is found only after the paid VM is allocated. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_gpu_rejects_incompatible_or_unsuccessful_cpu_receipt_before_allocation` | infra | Without it a GPU run accepts a CPU receipt for a different candidate, transport identity or unreleased session. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_gpu_prerequisite_byte_drift_after_prepare_refuses_before_allocation` | infra | Without it a lock or receipt changed after prepare is used for a paid GPU run. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_invalid_poll_interval_refuses_preparation_without_allocation` | infra | Without it a non-finite or out-of-range poll interval weakens the CU polling bound. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_sensitive_capture_keeps_bounded_memory_without_disk_then_ordinary_capture_works` | infra | Without it secret-bearing command output is written to disk logs. | AGENTS.md hard boundaries (logs contain no secrets; air-gap) |
| `test_native_settings_use_proven_qdrant_and_gateway_generated_legs_despite_case_aliases` | infra | Without it lowercase ambient settings redirect native Qdrant or model URLs and keys away from the proven targets. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_handoff_canonicalizes_new_aliases_in_a_different_shell_and_preserves_native_representation` | infra | Without it a later shell alias changes query prefix or context model so the representation fingerprint no longer matches the approved one. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_complete_production_admission_hands_exact_target_progress_and_native_settings_to_fresh_child` | infra | Without it the native child starts with a different target, progress or settings than the production admission verified. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_production_drift_and_wrappers_refuse_before_native_workload_then_next_native_run_succeeds` | infra | Without it drifted production source, ledger or runtime software starts the native workload. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_malformed_production_sidecar_cannot_block_exact_recovery_then_next_native_run_works` | infra | Without it a malformed production sidecar blocks release recovery of an owned VM. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_retained_storage_forwards_exact_proven_pod_without_logging_live_config` | infra | Without it the forward targets an unproven pod or live Qdrant config and keys are written to logs. | AGENTS.md hard boundaries (logs contain no secrets; air-gap) |
| `test_retained_service_must_match_explicit_selection_before_forwarding` | infra | Without it a service other than the explicitly selected one is forwarded. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_sidecar_pvc_cannot_admit_ephemeral_qdrant_then_valid_storage_works` | infra | Without it ephemeral Qdrant storage is admitted as retained production storage. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_retained_producer_rechecks_exact_identity_and_forward_after_readiness` | infra | Without it a producer pod replaced or a forward lost after readiness is still used as proven. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_budget_poll_refuses_retained_forward_loss_or_producer_drift` | infra | Without it a running budget poll tolerates a lost forward or changed producer during a paid run. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_workload_cannot_succeed_after_retained_container_restart` | infra | Without it a workload that ran across a Qdrant container restart is reported successful. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_running_budget_poll_lost_forward_releases_owned_vm` | infra | Without it a lost Qdrant forward during a run leaves the paid VM running. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_host_health_cannot_certify_failed_actual_gateway_network_then_next_run_succeeds` | infra | Without it host health passes the transport although the actual gateway network probe failed. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_normal_workload_gets_frozen_gateway_network_handoff_and_debug_disabled` | infra | Without it ambient gateway names, networks or debug flags override the prepared values and network ownership is not cleaned. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_preexisting_gateway_network_is_pinned_and_preserved` | infra | Without it a pre-existing Docker network is created over or removed by the run. | docs/colab-playground.md#keep-the-existing-host-resource-owner |
| `test_run_failure_records_only_controller_text_then_next_run_is_clean` | infra | Without it upstream or exception text reaches the manifest or client output. | AGENTS.md hard boundaries (logs contain no secrets; air-gap) |
| `test_probe_ownership_survives_timeout_or_interrupt_before_transport_ok` | infra | Without it a probe timeout or interrupt loses network ownership so cleanup cannot remove what the run created. | docs/colab-playground.md#keep-the-existing-host-resource-owner |
| `test_network_release_has_independent_budget_after_vm_release_then_recovery_works` | infra | Without it a failed or hung Docker network release blocks VM release or is not recoverable. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_exact_network_cleanup_remains_available_after_absolute_vm_deadline` | infra | Without it network cleanup is skipped after the VM deadline and leaks the owned network. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_gateway_ownership_observer_refuses_invalid_or_replaced_objects` | infra | Without it a replaced or mismatched gateway network is claimed as owned and later removed. | docs/colab-playground.md#keep-the-existing-host-resource-owner |
| `test_explicit_gateway_names_are_frozen_at_prepare_and_reused_by_owner_probe` | infra | Without it ambient gateway names change between prepare and the ownership probe. | docs/colab-playground.md#prepare-the-full-corpus-gpu-run |
| `test_same_name_replacement_is_never_removed_and_next_preexisting_run_works` | infra | Without it a same-name replacement network created by someone else is removed. | docs/colab-playground.md#keep-the-existing-host-resource-owner |
| `test_two_hour_poll_metadata_is_bounded_linear_and_critical_state_is_immediate` | noise-kept | Without it a two-hour run grows the manifest without bound or defers critical ownership state writes. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_successful_cpu_workload_with_undurable_cleanup_cannot_issue_receipt` | infra | Without it a receipt is issued although cleanup metadata could not be persisted. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_normal_critical_metadata_write_failure_refuses_before_allocation_then_normal_run_works` | infra | Without it a failed approval metadata write still allocates a VM with no durable ownership record. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_port_admission_refuses_live_listener_then_allows_closed_transport_reuse` | infra | Without it a live listener is not refused or TIME_WAIT from a closed transport blocks reuse of the port. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_read_only_existing_recovery_lock_keeps_exclusive_ownership_then_next_run_works` | infra | Without it recovery runs beside an active owner or fails to open a read-only existing lock. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_recovery_refuses_unsafe_existing_lock_without_tool_calls` | infra | Without it recovery uses a missing, symlinked or foreign-owned lock and calls cloud tools. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_normal_allocation_cannot_bypass_unwritable_lock_creation_then_next_run_works` | infra | Without it allocation proceeds without its exclusive run lock. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_private_key_unlink_failure_is_incomplete_after_exact_release_then_recovery_works` | infra | Without it a private SSH key left on disk after a failed unlink is reported as a complete cleanup. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_manifest_and_diagnostic_sink_disk_full_cannot_interrupt_release_then_recovery_works` | infra | Without it a full disk for manifest and stderr interrupts the VM release. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_closed_child_pipes_overflow_after_scheduler_pause_is_capped_then_next_command_works` | infra | Without it a paused scheduler lets command capture read past its byte cap (unbounded diagnostics) or lose the truncation flag, and later commands fail. | #755 |

## `tests/test_colab_corpus.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_complete_census_reconciles_independent_totals_without_mutating_inputs` | core | Without it, a census whose totals are miscounted or whose inputs get mutated during admission could authorize a production run on wrong corpus accounting. | docs/colab-playground.md |
| `test_two_maxima_or_missing_census_cannot_authorize_production` | core | Without it, a missing census or a bare pair of token maxima could pass as proof that the whole corpus was measured. | docs/colab-playground.md |
| `test_partial_failed_or_substituted_evidence_refuses` | core | Without it, a partial, failed or substituted document/query row could be admitted, giving a silent subset of the corpus. | docs/colab-playground.md |
| `test_repeated_rows_cannot_mask_an_unaccounted_input` | core | Without it, a duplicated source, document or query row could stand in for an unmeasured input and still match the counts. | docs/colab-playground.md |
| `test_aggregate_counts_and_maxima_are_recomputed` | core | Without it, a producer-reported summary (pages, maxima, failures) could disagree with the rows and still be trusted. | docs/colab-playground.md |
| `test_counts_never_coerce_booleans_or_nonintegers` | core | Without it, True, floats or strings in count fields would be coerced to valid numbers and corrupt the reconciled census totals. | docs/colab-playground.md |
| `test_census_contract_must_match_independent_current_expectations` | core | Without it, a census measured under a different model, tokenizer, counting option or window than the current expectation would be admitted. | docs/colab-playground.md |
| `test_actual_pooling_window_accepts_boundary_and_refuses_one_more_token` | core | Without it, an input one token over the effective pooling window would be silently truncated or the exact boundary wrongly refused. | docs/colab-playground.md |
| `test_source_paths_round_trip_whitespace_unicode_and_delimiters_exactly` | core | Without it, source paths with spaces, unicode or delimiter characters could be altered in transit and match the wrong source. | docs/colab-playground.md |
| `test_noncanonical_or_escaping_source_paths_refuse` | core | Without it, absolute, parent-escaping or non-canonical source paths could address files outside the declared corpus. | docs/colab-playground.md |
| `test_progress_survives_controller_attribution_order_and_timestamp_changes` | core | Without it, a changed producer, row order or timestamp would invalidate retained document progress and force needless re-ingestion. | docs/colab-playground.md |
| `test_query_only_and_nontruncating_window_changes_keep_document_checkpoints` | core | Without it, golden or query-recipe changes would discard valid document checkpoints, or document-affecting changes would reuse them. | docs/colab-playground.md |
| `test_compatible_new_evidence_cannot_reuse_an_incompatible_retained_ledger` | core | Without it, a retained ledger from a different representation, recipe, labels or tokenizer could be adopted for new data. | docs/colab-playground.md |
| `test_changed_source_set_cannot_reuse_retained_progress` | core | Without it, progress recorded for one source set could be reused after a source changed, leaving stale vectors unidentified. | docs/colab-playground.md |
| `test_unknown_or_corrupt_ledger_never_initializes_itself` | core | Without it, a missing or corrupt retained ledger would be silently initialized instead of requiring an explicit fresh/rebuild decision. | docs/colab-playground.md |
| `test_schema_unknown_fields_and_missing_chunk_type_refuse` | core | Without it, census rows with unknown fields or a missing chunk type could be admitted with an unaccounted chunk class. | docs/colab-playground.md |
| `test_metadata_byte_ceiling_and_fixed_diagnostics` | core | Without it, oversized metadata would be accepted unbounded or errors could leak private metadata text to the operator. | AGENTS.md#hard-boundaries |

## `tests/test_colab_hold.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_stop_file_ends_the_hold_before_the_deadline` | infra | Without it the sandbox hold ignores the stop marker and keeps the paid VM alive until its deadline. | docs/colab-playground.md#sandbox-lane-daily-iteration |
| `test_deadline_margin_ends_the_hold_without_waiting_past_it` | infra | Without it the hold sleeps past the work expiry and cleanup starts after the CU budget deadline. | docs/colab-playground.md#sandbox-lane-daily-iteration |
| `test_missing_or_invalid_controller_inputs_refuse_before_holding` | infra | Without it a missing state or non-finite expiry makes the hold run unbounded instead of refusing. | docs/colab-playground.md#sandbox-lane-daily-iteration |
| `test_missing_sandbox_handoff_refuses_before_holding` | infra | Without it the hold starts with no sandbox handoff and bills a VM that cannot be used. | docs/colab-playground.md#sandbox-lane-daily-iteration |

## `tests/test_colab_production.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_expected_sources_golden_settings_and_native_query_inputs_are_independent` | core | Without it, the expected sources, golden hash, query counts and exported settings could be derived from the evidence they are meant to check. | docs/colab-playground.md |
| `test_resolved_settings_are_frozen_even_when_the_ambient_environment_changes` | core | Without it, ambient environment variables could change the query prefix or split behavior of a prepared production run. | docs/colab-playground.md |
| `test_absent_encoder_preserves_observed_null_and_next_ordinary_prepare` | core | Without it, an absent sentence-transformers encoder config would be guessed instead of kept as observed null, or break the next ordinary prepare. | docs/colab-playground.md |
| `test_present_encoder_keeps_its_strict_semantics` | core | Without it, a present but wrong encoder config (window, lowercasing flag, null) could be accepted as embedding semantics. | docs/colab-playground.md |
| `test_absent_bert_cannot_hide_an_alternative_runtime_encoder` | core | Without it, an alternative sentence_* encoder config could silently define pooling while the BERT config is absent. | docs/colab-playground.md |
| `test_absent_encoder_keeps_observation_model_and_provenance_checks` | core | Without it, a forged encoder observation, lowercasing, shrunken model window or changed provenance would pass when the encoder is absent. | docs/colab-playground.md |
| `test_frozen_exports_and_unsets_round_trip_actual_native_settings` | core | Without it, the exported environment could produce a child process whose settings differ from the admitted representation fingerprint. | docs/colab-playground.md |
| `test_actual_source_bytes_are_checked_even_when_metadata_and_census_agree` | core | Without it, altered PDF bytes could be admitted because the recorded metadata and census still agree with each other. | docs/colab-playground.md |
| `test_complete_admission_keeps_full_metadata_private_and_binds_actual_target` | core | Without it, full census metadata could leak into the public manifest or admission could bind to the wrong storage target. | docs/colab-playground.md |
| `test_unknown_ledger_does_not_initialize_from_empty_host_directory` | core | Without it, an empty host data directory would be adopted as a retained index without an explicit fresh decision. | docs/colab-playground.md |
| `test_explicit_fresh_binding_allows_next_ordinary_prepare_without_claiming_ingestion` | core | Without it, the explicit fresh decision could fail to enable the next prepare or falsely claim data was ingested. | docs/colab-playground.md |
| `test_initialization_refuses_existing_or_dangling_native_target_state` | core | Without it, a fresh initialization could adopt an existing collection, alias or build residue and overwrite or orphan retained data. | docs/colab-playground.md |
| `test_fresh_names_cannot_hide_target_publication_control` | core | Without it, unrelated collection names could hide a publication control record for the target and a fresh claim would be false. | docs/colab-playground.md |
| `test_producer_replacement_prevents_ledger_commit` | core | Without it, a ledger could be committed after the producer was replaced mid-flight. | docs/colab-playground.md |
| `test_native_publisher_lock_blocks_initialization_without_stealing_ownership` | core | Without it, initialization could race a live publisher or steal its lock. | docs/colab-playground.md |
| `test_concurrent_destination_is_never_replaced` | core | Without it, a ledger written concurrently by another process could be overwritten, losing its retained binding. | docs/colab-playground.md |
| `test_interrupted_staged_write_leaves_no_ledger_then_fresh_retry_works` | core | Without it, a crash during the staged write could leave a partial ledger or block the next ordinary initialization. | docs/colab-playground.md |
| `test_loader_restores_next_process_identities_without_repreparing` | core | Without it, the next process could not restore prepared identities and would re-prepare or run with changed identities. | docs/colab-playground.md |
| `test_loader_refuses_tampered_sidecar_without_overwriting_it` | core | Without it, a tampered sidecar could be loaded or silently rewritten, hiding altered expectations. | docs/colab-playground.md |
| `test_unsupported_resolved_settings_refuse_without_using_ambient_defaults` | core | Without it, unsupported settings (contextual embedding, input truncation, no alias publish, wrong revision) could fall back to ambient defaults. | docs/colab-playground.md |
| `test_native_query_capture_uses_actual_split_expansion_and_prefix` | core | Without it, the census would count query inputs differently from what native retrieval embeds. | docs/colab-playground.md |
| `test_arbitrary_production_wrappers_or_native_overrides_refuse` | core | Without it, an arbitrary wrapper or extra native flags could run in place of the approved production workload. | AGENTS.md#hard-boundaries |
| `test_protected_dataset_authorization_stays_with_native_owner` | core | Without it, a protected holdout or release set could be used as the golden file without the native owner's authorization. | docs/colab-playground.md |
| `test_actual_golden_bytes_and_native_row_validation_are_required` | core | Without it, a changed golden file or invalid golden rows could be admitted on stale or unchecked hashes. | docs/colab-playground.md |
| `test_self_consistent_census_cannot_hide_wrong_native_effective_query_count` | core | Without it, a census internally consistent but with the wrong effective query count would be admitted. | docs/colab-playground.md |
| `test_complete_census_must_fit_frozen_native_document_admission_bounds` | core | Without it, a census with documents exceeding native ingest bounds would be admitted and then fail or truncate at ingest. | docs/colab-playground.md |
| `test_actual_source_membership_is_checked_before_admission` | core | Without it, extra, missing, symlinked or duplicate-content source files could be admitted against the declared source set. | docs/colab-playground.md |
| `test_source_scan_uses_explicit_entry_stream_and_deadline_bounds` | core | Without it, a huge or slow corpus tree could exhaust the scan with no bound or deadline. | docs/colab-playground.md |
| `test_explicit_source_bounds_do_not_coerce_booleans_or_invalid_counts` | core | Without it, a boolean or non-positive scan bound would be coerced into a valid limit. | docs/colab-playground.md |
| `test_artifact_actual_bytes_cannot_be_replaced_by_agreeing_metadata` | core | Without it, altered tokenizer bytes could pass because the manifest metadata still claims the original hash. | docs/colab-playground.md |
| `test_optional_tokenizer_artifacts_cannot_escape_the_approved_inventory` | core | Without it, an extra tokenizer file outside the approved inventory could change token counts unnoticed. | docs/colab-playground.md |
| `test_private_evidence_cannot_approve_its_own_pooling_semantics` | core | Without it, the evidence bundle could approve its own producer, runtime pins, argv, special tokens or window. | docs/colab-playground.md |
| `test_incomplete_approved_runtime_pins_refuse_instead_of_guessing` | core | Without it, missing approved runtime pins would be guessed from the evidence instead of refused. | docs/colab-playground.md |
| `test_canonical_corpus_root_changes_require_explicit_retained_rebuild` | core | Without it, moving the corpus root would reuse the retained index even though source identities changed. | docs/colab-playground.md |
| `test_query_and_golden_changes_reuse_compatible_document_progress` | core | Without it, golden or query changes would force a needless document rebuild or alter the retained ledger. | docs/colab-playground.md |
| `test_unrelated_controller_edits_do_not_invalidate_document_progress` | core | Without it, editing unrelated controller files would invalidate progress, or a changed native producer recipe would go undetected. | docs/colab-playground.md |
| `test_actual_retained_target_drift_refuses` | core | Without it, a changed namespace, claim, PV or storage path would be accepted as the same retained target. | docs/colab-playground.md |
| `test_source_and_ledger_are_rechecked_before_allocation` | core | Without it, sources changed after preparation would be used for GPU allocation without re-verification. | docs/colab-playground.md |
| `test_changed_retained_ledger_or_sidecar_never_authorizes_start` | core | Without it, a modified ledger or sidecar after preparation could still authorize the workload to start. | docs/colab-playground.md |
| `test_full_prepared_metadata_must_remain_private` | core | Without it, the full prepared metadata could be left world-readable. | AGENTS.md#hard-boundaries |
| `test_runtime_observations_match_exact_artifacts_pins_and_launched_flags` | core | Without it, a runtime serving different artifacts, pins or flags than approved would be accepted. | docs/colab-playground.md |
| `test_complete_census_requires_independently_approved_producer` | core | Without it, a census from an unapproved producer would be admitted. | docs/colab-playground.md |
| `test_final_workload_environment_refuses_ambient_semantic_overrides` | core | Without it, ambient environment variables or the wrong cwd could change the semantics of the final production workload. | docs/colab-playground.md |
| `test_case_insensitive_settings_cannot_override_frozen_exports_or_unsets` | core | Without it, a lowercase or duplicate environment name could override frozen settings in the child process. | docs/colab-playground.md |
| `test_interpreter_spelling_is_preserved_even_for_a_venv_symlink` | core | Without it, a venv symlink interpreter would be resolved to the base Python and lose the venv packages at launch. | docs/colab-playground.md |

## `tests/test_colab_profiles.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_g4_reference_keeps_immutable_models_and_independent_allocations` | core | Without it, the G4 launch profile could drift off its pinned model revisions, argv or memory split and fail or misbehave on the Colab GPU. | docs/colab-playground.md |
| `test_gpu_admission_refuses_unknown_missing_or_insufficient_capacity` | core | Without it, a GPU assignment with unmeasured or too-small VRAM, RAM or disk would be admitted and fail late after model downloads. | docs/colab-playground.md |
| `test_cpu_transport_also_requires_measured_finite_resources` | core | Without it, the CPU transport profile would start on unmeasured RAM or disk and fail after bulk transfer. | docs/colab-playground.md |
| `test_finite_census_capacity_at_declared_minima_is_accepted` | core | Without it, exact-minimum integer or float capacity readings would be wrongly refused, blocking valid Colab runs. | docs/colab-playground.md |
| `test_gpu_name_must_be_observed_text_not_a_stringified_container` | core | Without it, a non-text GPU name could be stringified and match the expected GPU, admitting an unverified accelerator. | docs/colab-playground.md |
| `test_profile_window_and_deadline_require_bounded_integers` | core | Without it, non-integer or out-of-range deadline and embedding window values would build a profile exceeding the Colab session or model window. | docs/colab-playground.md |
| `test_profile_accepts_integer_bounds_without_changing_model_arguments` | core | Without it, valid boundary window/deadline values could be refused or could alter the pinned model arguments. | docs/colab-playground.md |

## `tests/test_colab_runtime.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_actual_inline_probe_refuses_invalid_identity_before_inference` | infra | Without it the remote probe lets an invalid candidate identity reach inference, so results are attributed to the wrong code. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_cpu_preflight_records_actual_platform_without_inference_python` | infra | Without it CPU transport admission misreports the platform or demands a 3.12 inference interpreter it does not need. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_cpu_preflight_records_unknown_optional_platform_facts` | infra | Without it missing optional platform metadata crashes the CPU preflight instead of being recorded as unknown. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_gpu_preflight_checks_interpreter_before_setup_and_preserves_facts` | infra | Without it a GPU run is admitted on a wrong or missing CPython 3.12 and fails after paid setup instead of before. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_inference_interpreter_probe_uses_real_isolated_stdlib_process` | infra | Without it the interpreter probe breaks on paths with spaces or runs in a non-isolated process that reports the wrong version. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_inference_interpreter_probe_reports_unknown_evidence` | infra | Without it a timed-out or malformed probe is reported as a good interpreter instead of explicit unknown evidence. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_software_probe_observes_installed_tokenizers_version` | infra | Without it the observed software facts omit tokenizers, so the lock-bound software check cannot catch a drifted tokenizer. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_snapshot_fetch_uses_parallel_transfer_and_records_its_time` | infra | Without it the model fetch loses its parallel transfer or stops recording elapsed time and transfer kind in the startup evidence. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_runtime_environment_enables_bounded_xet_despite_ambient_disable` | infra | Without it an ambient HF disable flag silently changes the model download path and its bounds. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_inference_install_works_without_ensurepip_and_targets_fresh_environment` | infra | Without it the inference install targets the kernel environment or runs with unhashed/unsupported installers instead of a fresh hash-locked venv. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_preflight_records_kernel_pip_without_installing_or_bootstrapping` | infra | Without it the preflight installs or bootstraps pip, or accepts an unsupported kernel installer version. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_missing_kernel_installer_refuses_gpu_but_next_cpu_remains_usable` | infra | Without it a missing kernel installer starts paid GPU work, or the refusal poisons the next CPU transport run. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_model_http_json_requires_an_object_and_next_response_succeeds` | infra | Without it a non-object model response is treated as valid readiness and later requests stay broken. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_reasoning_readiness_requires_completed_final_content` | infra | Without it a truncated or empty reasoning warmup counts as ready and the paid run starts on a model that cannot answer. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_embedding_readiness_checks_native_dimension_and_normalization` | infra | Without it an embedding server with the wrong dimension or unnormalized vectors is declared ready. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_inference_lock_is_required_hash_bound_and_contains_reference_pins` | infra | Without it the inference install proceeds without a lock matching the profile digest and reference pins. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_inference_lock_refuses_nonexact_or_unsupported_requirement_tokens` | infra | Without it ranges, URL requirements or index options in the lock allow unpinned or off-index installs. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_inference_lock_accepts_exact_local_versions_and_multiple_wheel_hashes` | infra | Without it valid exact local-version pins with several wheel hashes are wrongly rejected, blocking every GPU run. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_inference_lock_refuses_canonical_duplicate_pins` | infra | Without it a differently spelled duplicate pin overrides a reviewed pin. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_inference_lock_refuses_unhashed_requirement` | infra | Without it an unhashed requirement is installed without --require-hashes protection. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_inference_lock_refuses_growth_after_opened_file_metadata` | infra | Without it a lock file that grows after open is read past its hashed size and the digest no longer binds the content. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_inference_lock_refuses_symlink_and_nonregular_file` | infra | Without it a symlink or FIFO is accepted as the lock, so a swapped file can supply the installed set. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_remote_output_and_archive_are_finite_and_exclude_model_files` | infra | Without it the remote diagnostics archive grows unbounded or ships model files off the VM. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_diagnostic_packaging_rejects_oversized_or_unowned_members` | infra | Without it oversized or foreign members get packaged into the exported diagnostics archive. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_real_child_output_is_drained_bounded_and_next_operation_works` | infra | Without it a noisy child fills a pipe and stalls, or its output is stored without a bound. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_timed_out_child_is_reaped_before_the_next_operation` | infra | Without it a timed-out remote child is left running and holds resources into the next operation. | docs/colab-playground.md#persistence-acceptance-and-recovery |
| `test_model_startup_shares_readiness_and_cold_inference_deadline` | noise-kept | Without it readiness plus cold inference can exceed the remaining run deadline and cost CU past the cap; the virtual-clock budgets are the stated deadline contract. | docs/colab-playground.md#reference-profiles-and-prerequisites |
| `test_cpu_transport_never_installs_or_downloads_and_exports_after_stop` | infra | Without it the CPU transport run installs software or downloads models, or fails to export artifacts after the stop marker. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_partial_transport_start_failure_cleans_up_and_next_run_works` | infra | Without it a half-started transport leaks a listener and blocks the next run. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_cpu_deadline_refuses_readiness_without_transport_approval` | infra | Without it the CPU run reports ready past its deadline without transport approval. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_transport_approval_is_required_before_any_gpu_bootstrap` | infra | Without it GPU bootstrap and model download start without the verified CPU transport approval, spending CU on an unproven path. | docs/colab-playground.md#cpu-transport-before-paid-gpu |
| `test_gpu_startup_uses_exact_argv_and_warms_reasoning_before_embedding` | infra | Without it a malformed software, snapshot, resource, candidate or pooling observation still starts model servers or reports ready, and cleanup/diagnostics are not produced. | docs/colab-playground.md#reference-profiles-and-prerequisites |

## `tests/test_colab_storage.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_chart_auth_and_local_path_prove_exact_forward_producer_without_credentials` | core | Without it, the storage proof could name the wrong pod/PVC/PV identity or leak the Qdrant API keys into its output. | AGENTS.md#hard-boundaries |
| `test_sidecar_pvc_cannot_hide_ephemeral_serving_container_then_valid_proof_succeeds` | core | Without it, a sidecar mounting the PVC could hide that the serving container writes to ephemeral storage, so indexed data would be lost. | docs/colab-playground.md |
| `test_service_http_port_cannot_mask_a_different_target_container` | core | Without it, a Service port could target a sidecar and the proof would attribute another container's storage to Qdrant. | docs/colab-playground.md |
| `test_named_service_target_also_refuses_duplicate_numeric_listener_ownership` | core | Without it, a sidecar listening on the Qdrant port number could be mistaken for the Qdrant container. | docs/colab-playground.md |
| `test_pod_ready_does_not_replace_actual_qdrant_container_readiness` | core | Without it, a Ready pod with a not-ready Qdrant container could be treated as a serving, proven store. | docs/colab-playground.md |
| `test_effective_config_redirect_to_emptydir_refuses_nominal_pvc` | core | Without it, a config redirecting storage to an emptyDir would pass while the nominal PVC sits unused, losing data at pod restart. | docs/colab-playground.md |
| `test_subpath_pod_mounted_bytes_override_newer_clean_api_config` | core | Without it, a newer clean ConfigMap could mask the stale subPath bytes the running pod actually loaded. | docs/colab-playground.md |
| `test_environment_cannot_redirect_config_or_chart_launch` | core | Without it, an environment variable could silently redirect Qdrant's storage path past the proof. | docs/colab-playground.md |
| `test_writable_image_config_cannot_establish_process_start_files` | core | Without it, a writable image root could let unproven start-up files redefine Qdrant's config after the proof. | docs/colab-playground.md |
| `test_unknown_or_malformed_config_refuses_with_fixed_diagnostic` | core | Without it, unparseable or ambiguous Qdrant config would be guessed at, or errors would echo credential text. | AGENTS.md#hard-boundaries |
| `test_unproven_launch_and_config_sources_refuse` | core | Without it, envFrom, a custom command, a modified initializer or a non-ConfigMap config source could hide the real storage path. | docs/colab-playground.md |
| `test_exact_pv_claim_binding_rejects_name_reuse_and_other_namespace` | core | Without it, a recreated claim or a PV bound to another namespace could be adopted as the retained store. | docs/colab-playground.md |
| `test_bound_rwo_filesystem_is_required_on_both_sides` | core | Without it, an unbound, RWX or block PVC/PV pair could be accepted as the Qdrant data volume. | AGENTS.md#hard-boundaries |
| `test_rwo_filesystem_nfs_is_not_local_path_storage` | core | Without it, an NFS-backed volume could be accepted as the retained Kind local-path store, skipping node-affinity guarantees. | docs/colab-playground.md |
| `test_arbitrary_class_name_is_not_evidence_of_backend` | core | Without it, a StorageClass merely named like local-path with another provisioner would be trusted as the retained backend. | docs/colab-playground.md |
| `test_local_path_affinity_uses_actual_node_hostname_not_assumed_node_name` | core | Without it, a PV pinned to a different node than the pod's hostname label would be accepted and reattach to the wrong disk. | docs/colab-playground.md |
| `test_writable_complete_data_directory_cannot_be_masked` | core | Without it, a read-only, subPath or nested emptyDir mount could mask the PVC and the proof would claim a writable complete data directory. | docs/colab-playground.md |
| `test_no_ready_selected_pod_refuses_unrelated_ready_pod` | core | Without it, an unrelated ready pod could stand in when the selected Qdrant pod is absent. | docs/colab-playground.md |
| `test_another_ready_selected_ephemeral_pod_cannot_hide_behind_valid_first_peer` | core | Without it, a second selected pod with ephemeral storage could hide behind a valid first peer and lose data. | docs/colab-playground.md |
| `test_next_observation_returns_replacement_pod_uid_for_caller_to_reject` | core | Without it, a replaced pod could be reported under the old identity so the caller fails to reject a producer swap. | docs/colab-playground.md |

## `tests/test_colab_tasks.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `ColabTaskTests::test_prepare_preserves_private_values_and_overrides_ambient_inputs` | infra | Without it Task inputs with spaces or shell metacharacters are evaluated as shell or ambient values replace the explicit private state and corpus paths. | docs/colab-playground.md (Task entry points); docs/task-runner.md |
| `ColabTaskTests::test_prepare_bridges_only_an_explicit_ingest_reingest_choice` | infra | Without it a production resume of a pending build is triggered by an ambient or malformed COLAB_INGEST_REINGEST value (#737). | #737 |
| `ColabTaskTests::test_initialize_fresh_preserves_exact_target_inputs_without_cloud_flags` | infra | Without it fresh-initialize loses or reshapes exact target inputs, or injects cloud flags and evaluates trailing shell source. | docs/colab-playground.md (Task entry points); docs/task-runner.md |
| `ColabTaskTests::test_prepare_omits_optional_flags_and_requires_explicit_state_and_image` | infra | Without it prepare runs without an explicit state and pinned gateway image or passes empty optional flags to the controller. | docs/colab-playground.md (Task entry points); docs/task-runner.md |
| `ColabTaskTests::test_run_requires_cu_approval_and_preserves_one_executable_path` | infra | Without it a paid run starts without explicit COLAB_APPROVE_CU or splits the workload path into several argv words. | docs/colab-playground.md (Task entry points); docs/task-runner.md |
| `ColabTaskTests::test_stop_preserves_exact_run_state_and_rejects_trailing_shell_source` | infra | Without it stop recovery targets a mangled state path or executes unreviewed trailing command text. | docs/colab-playground.md (Task entry points); docs/task-runner.md |

## `tests/test_config.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_reasoning_model_required` | core | Without it chat or answer could start with no reasoning model configured and fail on first request. | docs/deploy.md#configuration-contract |
| `test_env_loads` | core | Without it operator environment variables for the embedding dimension stop reaching Settings. | docs/deploy.md#configuration-contract |
| `test_collection_policy_env_override` | infra | Without it operator-chosen shard and replication policy would be ignored, and unset would no longer mean server default. | issue #360 |
| `test_peer_endpoints_env_override` | infra | Without it direct Qdrant peer endpoints would be dropped or inferred instead of explicit. | issue #360 |
| `test_collection_policy_rejects_non_positive` | infra | Without it zero or negative replication or shard counts would load and create a broken collection. | issue #360 |
| `test_alias_publication_is_the_only_ingest_mode` | core | Without it an operator could opt back into retired in-place ingest that mutates the serving collection, or the opt-out would be silently ignored. | docs/ingest.md#publication-contract |
| `test_require_context_llm_fails_closed` | core | Without it contextual embedding enabled with no endpoint would silently fall back to header-only embeddings. | docs/deploy.md#configuration-contract |
| `test_reasoning_effort_round_trips_configuration` | core | Without it configured reasoning efforts from constructor or environment would not reach the answer path. | docs/deploy.md#configuration-contract |
| `test_served_efforts_parse_in_vocabulary_order` | core | Without it the served-effort set would be parsed in operator order or unstripped, changing which efforts are offered. | issue #748 |
| `test_efforts_outside_the_served_set_refuse_startup` | core | Without it a model whose served efforts exclude the configured default would start and fail per request, or leak a bad value in the error. | issue #748 |
| `test_reasoning_effort_validation` | core | Without it unsupported effort names would load and be sent upstream. | docs/deploy.md#configuration-contract |
| `test_rerank_endpoint_order_validation` | core | Without it an invalid rerank endpoint order would silently behave as score-first at request time. | docs/deploy.md#configuration-contract |
| `test_otel_bounds_are_enforced` | infra | Without it a mis-set sample ratio, queue size or export timeout could grow the heap or silently disable sampling. | issue #83 |
| `test_chat_bounds_are_enforced` | core | Without it unbounded chat body, turn or prior-turn limits would admit oversized conversations. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_bearer_auth_headers_matrix` | core | Without it unset or blank keys would send a literal Bearer None header and pasted keys would keep stray whitespace on every model leg. | docs/agent.md#http-model-contract |
| `test_simple_thinking_reserve_loads_from_env` | core | Without it the operator-set thinking reserve would not load or would disturb the other token budgets. | docs/deploy.md#configuration-contract |
| `test_simple_thinking_reserve_rejects_out_of_range` | core | Without it an out-of-range reserve could exceed the model context and break prompt budgeting. | docs/deploy.md#configuration-contract |
| `test_gateway_api_keys_load_from_env` | core | Without it per-leg gateway keys from the Secret environment would not reach the model clients. | AGENTS.md hard boundary: per-leg keys via Secret references |
| `test_every_setting_default_is_pinned` | core | Without it any Settings default could change or be added without a failing check, silently changing production behavior. | AGENTS.md hard boundary: no silent default changes |

## `tests/test_contextual.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_prompt_template_change_requires_a_version_bump` | noise-kept | A context prompt edit must bump the prompt version, otherwise cached contexts generated under the old prompt keep being served. | docs/ingest.md |
| `test_prompt_messages_carry_the_embed_header_fields` | core | The context prompt carries doc id, title, heading path and body, so generated gists situate the right chunk. | issue 78 |
| `test_cache_key_binds_every_generation_input` | core | The context cache key binds doc digest, model, cap, header fields, body, heading and chunk id, so a changed input never hits a stale gist. | issue 416 |
| `test_cache_round_trip_last_wins_and_skips_corrupt_lines` | core | The sidecar cache keeps the latest entry, skips corrupt lines and rejects unknown chunk ids, so a damaged cache neither crashes nor serves wrong contexts. | issue 416 |
| `test_legacy_and_malformed_records_are_misses_not_errors` | core | Legacy and malformed cache records never hit and never raise and the file is left untouched, so upgrades regenerate instead of serving unbound contexts. | issue 416 |
| `test_resolve_cache_path_explicit_wins_and_sibling_default` | infra | An explicit cache path wins and the default sits beside the inventory, so operators know where gists persist. | docs/ingest.md |
| `test_complete_request_contract` | core | The context LLM request is short and deterministic, sends Bearer only when keyed and surfaces HTTP errors, so ingest never silently proceeds with failed gists. | docs/testing.md#tests-must-lock-the-claimed-path |
| `test_generate_contexts_uses_cache_and_generates_misses` | core | Cache hits make zero LLM calls and only misses are generated, so reruns do not repeat expensive generation. | issue 78 |
| `test_model_swap_same_chunk_misses_and_calls_new_model` | core | Changing the context model misses the cache and calls the new model while keeping the old entries, so embeddings never carry another model's gist. | issue 416 |
| `test_max_chars_change_is_policy_correct_in_both_directions` | core | Raising or lowering the context cap regenerates correctly rather than serving a truncated or oversized gist. | issue 416 |
| `test_changed_inputs_under_stable_chunk_id_miss` | core | Body, title or doc digest changes under a stable chunk id miss the cache, so edited documents get fresh contexts. | issue 416 |
| `test_unnormalized_cache_hit_is_regenerated` | core | A cached gist violating the current normalization is regenerated, so stored contexts always follow the current recipe. | issue 416 |
| `test_generate_contexts_empty_gist_fails_loud` | core | An empty gist from the LLM raises rather than embedding an empty prefix, so silent header-only vectors cannot appear. | issue 78 |
| `test_upsert_payload_stores_context_only_when_present` | core | The payload carries context only when a gist exists, so serving and payload schema stay unchanged when contextual embedding is off. | issue 78 |
| `test_run_fails_closed_without_context_llm` | core | Enabling contextual embedding without a context LLM fails the run, so it can never silently embed header-only vectors. | issue 78 |
| `test_parse_one_contextual_path_end_to_end` | core | The worker path generates gists on a miss, hits a primed cache with zero calls, and results pickle across processes, so contextual ingest works across the pool. | issue 78 |
| `test_dry_run_makes_no_context_calls` | core | A dry run builds no LLM client and makes no calls, so planning an ingest never spends model capacity. | docs/ingest.md |
| `test_resume_after_model_swap_regenerates_through_worker` | core | After a model swap the sidecar regenerates every chunk through the worker and then hits fully, so resumed ingests use the right model's gists. | issue 416 |
| `test_resume_with_legacy_sidecar_regenerates_without_deleting_it` | core | A pre-binding sidecar neither crashes nor is served and stays on disk, so operator caches are never destroyed on upgrade. | issue 416 |
| `test_contextual_off_worker_path_is_unchanged` | core | With the flag off no client or sidecar is touched and header-only vectors are produced, so the default ingest path is unchanged. | issue 78 |
| `test_model_swap_embeds_new_prefix_and_leaves_sparse_unchanged` | core | After a model swap dense text carries only the new gist while sparse inputs and vectors stay byte-identical, so BM25 does not churn. | issue 78 |
| `test_interrupted_worker_persists_nothing_and_restart_regenerates` | core | A model failure mid-document returns an error record with no contexts and persists nothing, so a half-embedded document never lands. | issue 78 |
| `test_live_worker_retry_reuses_only_completed_gists` | core | A retry in the same worker reuses completed gists and calls the model only for the rest, giving a complete map without duplicate calls. | issue 78 |
| `test_append_after_torn_tail_does_not_corrupt_the_new_record` | core | An append after a crash-torn partial line starts on a fresh line, so the new valid record is not lost to the loader. | issue 416 |

## `tests/test_dependency_lock.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_supported_wheel_target` | infra | Without it wheels for the wrong interpreter, ABI, glibc, architecture or a path-traversal name are accepted into the air-gap bundle. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_lockfile_and_manifest_must_agree` | infra | Without it a edited lockfile that disagrees with the manifest is trusted. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_urllib3_security_pin_covers_project_and_runtime_profiles` | infra | Without it the urllib3 security floor is lost from the project or lock profiles. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_wheelhouse_rejects_missing_extra_tampered_and_symlink` | infra | Without it a missing, extra, tampered or symlinked wheel passes the hash-bound wheelhouse check. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_offline_preparation_fails_before_any_mutation_or_process` | infra | Without it a corrupt wheelhouse still creates an environment or uses the network offline. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_report_cannot_change_resolver_or_target` | infra | Without it a resolver report from another pip or Python target silently regenerates the lock. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_installed_only_or_wrong_version_dependency_fails` | infra | Without it an injected or wrong-version installed package passes the installed inventory check. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_parallel_acquisition_overlaps_and_verifies_each_download_before_reuse` | infra | Without it parallel downloads are unverified or serial and cache reuse skips verification. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_parallel_failure_drains_workers_preserves_cache_and_next_prepare_recovers` | infra | Without it a failed parallel download corrupts the cache or leaves a completion marker so the next prepare cannot recover. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_invalid_download_parallelism_fails_before_acquisition_or_environment_mutation` | infra | Without it invalid worker counts start downloads or build an environment. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_parallel_preparation_is_explicitly_connected_only` | infra | Without it parallel preparation runs in the offline path and tries the network. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_internal_index_has_no_inherited_extra_index_or_public_fallback` | infra | Without it inherited pip settings let wheels come from a public or untrusted index. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_preparation_uses_verified_pip_and_no_isolated_build_resolution` | infra | Without it installation uses unverified pip or resolves build dependencies from the network. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_actual_image_layers_and_receipt_reconcile` | infra | Without it the installed-package inventory of a built image ignores overwrites, whiteouts or removed packages. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_image_layer_tamper_is_detected_even_when_metadata_stays_valid` | infra | Without it a modified image layer with unchanged metadata is accepted in the inventory. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_serialized_sbom_omission_is_rejected_against_actual_archives` | infra | Without it a release SBOM omitting an installed package is accepted. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_preparation_revalidates_identity_without_reacquiring_approved_bytes` | infra | Without it prepare re-downloads approved wheels or fails to revalidate interpreter identity. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_staged_acquisition_failure_preserves_previous_cache_then_recovers` | infra | Without it a failed acquisition destroys the previous good wheelhouse and the next run cannot recover. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_preparation_refuses_unowned_and_symlink_destinations` | infra | Without it prepare overwrites unowned files or follows symlinks outside the wheelhouse. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_interrupted_member_publication_has_no_marker_and_next_prepare_recovers` | infra | Without it an interrupted publication leaves a completion marker so a half-built wheelhouse is trusted. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_preparers_serialize_and_second_reuses_complete_result` | infra | Without it concurrent preparers corrupt each other or redo the work. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_preparation_freshness_uses_content_and_repair_is_explicit` | infra | Without it freshness depends on mtimes so corrupt wheels are reused or healthy ones re-downloaded. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |
| `test_task_wheelhouse_reaches_real_preparation_owner_with_literal_path` | infra | Without it the Task entry mangles paths with spaces or shell characters or deletes unrelated files. | AGENTS.md hard boundaries (pinned wheels, no runtime downloads) |

## `tests/test_e2e_registry_login.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_registry_login_requires_both_secrets` | infra | The e2e workflow proceeds to package images with a missing registry credential or prints the secret. | AGENTS.md#hard-boundaries |
| `test_registry_login_preserves_credentials_without_password_in_argv` | infra | Registry credentials with shell metacharacters are mangled, or the password is exposed in process arguments. | AGENTS.md#hard-boundaries |
| `test_registry_login_failure_stops_the_step` | infra | A failed registry login does not stop the workflow step. | docs/live-stack.md#verification-minimums |

## `tests/test_embed_budget.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_worst_case_embed_text_fits_the_embed_window_budget` | core | The worst-case embedded string including the maximal context prefix fits the embed window budget, so chunk constants cannot silently outgrow the embed server. | issue 99 |

## `tests/test_eval_answers.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_http_answer_eval_scores_the_deployed_agent` | core | Answer eval against a deployed agent stops scoring the real /v1/answer response (wrong URL, body or recorded deployment identity) and reports a false pass. | docs/testing.md#answer-tier |
| `test_http_answer_eval_refuses_nonservable_health` | core | Eval against an agent whose generation is not servable stops failing closed, so a certifying report is produced over an unservable index and private health detail can leak. | docs/testing.md#answer-tier |
| `test_http_answer_eval_health_errors_are_fixed_and_keep_every_case` | core | A health probe failure no longer yields exit 2 with every case counted as an error, shrinking the denominator and leaking upstream text. | docs/testing.md#answer-tier |
| `test_http_answer_eval_retains_failed_answers_and_continues` | core | One failed or timed-out answer aborts the run or disappears from the denominator, hiding errors in the pass rate. | docs/testing.md#answer-tier |
| `test_http_answer_eval_malformed_answer_fields_cannot_abort_or_pass` | core | A malformed answer payload crashes the runner or counts as a pass, so a broken agent response certifies. | docs/testing.md#answer-tier |
| `test_http_answer_eval_missing_required_fields_are_errors_even_for_abstention` | core | An empty or incomplete answer body is scored as a valid abstention pass instead of an error. | docs/testing.md#answer-tier |
| `test_http_answer_eval_numeric_overflow_retains_prior_and_next_cases` | core | Valid-JSON numeric overflow in a response aborts the whole eval and drops earlier and later cases. | docs/testing.md#answer-tier |
| `test_http_answer_eval_malformed_search_pool_keeps_the_next_case` | core | A malformed /v1/search pool fails the run or leaks response text rather than leaving the pool join unknown. | docs/testing.md#answer-tier |
| `test_http_answer_eval_uses_a_real_http_agent_and_preserves_query_bytes` | core | Over a real socket and Route path prefix the eval alters query bytes or paths, so operator-phrased and injection-shaped queries are not evaluated as written. | docs/testing.md#answer-tier |
| `test_http_answer_eval_rejects_invalid_timeout_before_io` | core | Invalid request timeouts (zero, over the cap, NaN) reach dataset IO or the network instead of being refused up front. | docs/testing.md#answer-tier |
| `test_http_answer_eval_rejects_unsafe_target_before_io` | core | Agent URLs with credentials, query strings, bad ports or non-HTTP schemes are accepted and could leak secrets into reports or manifests. | docs/testing.md#answer-tier |
| `test_refusal_helper_shared_with_agent` | core | Eval and agent use different refusal predicates, so eval verdicts disagree with what the agent serves. | docs/testing.md#answer-tier (issues #135/#305) |
| `test_refusal_marker_battery` | core | Adversarial refusal phrasings stop being recognized and refusals ship with citations of unsupporting chunks. | docs/testing.md#answer-tier (issues #135/#305) |
| `test_answer_grounded_passes` | core | A grounded cited answer is no longer judged a pass, turning the answer tier red on correct output. | docs/testing.md#answer-tier |
| `test_answer_zero_citations_fails` | core | An answer with zero validated citations passes, certifying ungrounded answers. | docs/testing.md#answer-tier |
| `test_answer_inferred_citations_fail_grounding` | core | Citations inferred from bare bracket markers count as grounded evidence. | docs/testing.md#answer-tier (issue #269) |
| `test_answer_refusal_fails` | core | A refusal on an answer-tier query passes, hiding false refusals. | docs/testing.md#answer-tier |
| `test_answer_grounded_scope_caveat_is_not_a_refusal` | core | A substantive grounded answer with a scope caveat is failed as a refusal, falsely failing real answers. | docs/testing.md#answer-tier (issue #305) |
| `test_answer_empty_body_fails` | core | An empty answer body with citations passes. | docs/testing.md#answer-tier |
| `test_abstain_zero_citations_passes` | core | A correct uncited refusal on a trap query is failed, or a clean abstention is not recognized. | docs/testing.md#answer-tier |
| `test_abstain_grounded_without_refusal_fails` | core | A trap query answered with citations passes, so the system answering out-of-corpus questions goes unnoticed. | docs/testing.md#answer-tier |
| `test_abstain_long_grounded_decline_still_warns` | core | A long grounded decline is failed as an answered trap, or the whole-response rule leaks into the trap branch. | docs/testing.md#answer-tier (issue #305) |
| `test_gold_must_contain_enforced_casefolded` | core | Required gold substrings are skipped or case-sensitive, so wrong answers pass. | docs/testing.md#answer-tier |
| `test_gold_must_not_contain_enforced` | core | Forbidden substrings in an answer no longer fail the row. | docs/testing.md#answer-tier |
| `test_abstain_gold_phrase_pin` | core | The seed-literal refusal phrase requirement is silently relaxed for abstain rows. | docs/testing.md#answer-tier |
| `test_zero_hits_path_skips_gold_checks` | core | The canned zero-hits message is judged against gold text so structural abstain passes wrongly fail. | docs/testing.md#answer-tier |
| `test_zero_hits_answer_entry_still_fails_structurally` | core | Gold suppression masks a retrieval gap: an answer-tier query refused by the canned message passes. | docs/testing.md#answer-tier |
| `test_must_cite_identifier_checks_citations` | core | The required identifier is not checked against the citations, so answers that never cite the asked message pass. | docs/testing.md#answer-tier |
| `test_is_zero_hits_answer_recognizes_both_canned_forms` | core | Canned zero-hits wording is not recognized and gold suppression and path labelling misfire. | docs/testing.md#answer-tier |
| `test_select_sample_covers_every_class_deterministically` | core | Sampling drops a query class or becomes nondeterministic, so runs are not comparable. | docs/testing.md#answer-tier |
| `test_select_sample_small_class_fully_drained_first` | core | Small classes are undersampled, so rare classes silently fall out of the sample. | docs/testing.md#answer-tier |
| `test_select_sample_cap_bounds` | core | A cap above total or zero mishandles the sample size. | docs/testing.md#answer-tier |
| `test_summarize_rates_and_counts` | core | The answer-tier summary miscounts judged rows, errors or pass rates, so the denominator is incomplete. | docs/testing.md#answer-tier |
| `test_answer_completeness_requires_gold_pool_and_full_recall` | core | Answer completeness counts rows without a retrieved gold pool or full recall, inflating the metric. | docs/testing.md#answer-tier (issue #305) |
| `test_answer_capture_keeps_answer_lines_only` | core | The log capture joins non-answer or unkeyed lines to rows, attributing truncation and token signals to the wrong request. | docs/testing.md#answer-tier (issue #298) |
| `test_run_query_temperature_default_zero_and_override_recorded` | core | Answer eval samples at an unrecorded temperature so reports are not interpretable or reproducible. | docs/testing.md#answer-tier (issue #596) |
| `test_run_query_forged_accepted_without_answer_body_fails` | core | A forged accepted state over an empty or citation-only body certifies as a pass. | docs/testing.md#answer-tier |
| `test_run_query_joins_answer_signals` | core | Per-request signals from the agent log are not joined to the row, losing truncation and citation attribution. | docs/testing.md#answer-tier (issue #298) |
| `test_run_query_without_signals_leaves_nones` | core | Missing log join fabricates False values instead of unknown, undercounting truncation. | docs/testing.md#answer-tier (issues #299/#368) |
| `test_run_query_pool_join_states` | core | The gold-retrieved flag is wrong or fabricated when the search pool is absent, so reader blame versus retrieval blame is misattributed. | docs/testing.md#answer-tier |
| `test_run_query_flags_cited_then_zeroed_abstention` | core | Cited-then-zeroed abstentions are not flagged and a retrieval-grounded refusal looks like a plain refusal. | docs/testing.md#answer-tier |
| `test_why_mode_precedence_and_branches` | core | Failure WHY taxonomy misclassifies rows or asserts a cut shape on unknown signals, misdirecting debt triage. | docs/testing.md#answer-tier (issue #299) |
| `test_inferred_index_off_gold_maps_indices_to_pool_order` | core | Inferred citation indices are mapped to the wrong pool entries and off-gold inferences are missed or fabricated. | docs/testing.md#answer-tier (issue #299) |
| `test_summarize_by_why_and_off_gold` | core | The WHY and inferred-count aggregation drops or miscounts rows. | docs/testing.md#answer-tier (issue #299) |
| `test_judge_expected_verification_state_opt_in` | core | A served verification state mismatch passes, or non-opted rows churn baselines. | docs/testing.md#answer-tier (issue #365) |
| `test_summarize_splits_false_refusal_and_unsafe_rates` | core | False-refusal and unsafe-answer rates are merged or miscounted, hiding which direction quality is moving. | docs/testing.md#answer-tier (issue #365) |
| `test_run_query_unknown_state_is_contract_error` | core | An unknown verification state from the agent is scored instead of failing as a contract error. | docs/testing.md#answer-tier (issue #365) |
| `test_security_refusal_requires_the_complete_response` | core | A response that leaks secret-looking content after a refusal phrase, or quotes one, is treated as a refusal and passes the trap. | docs/testing.md#answer-tier |
| `test_dev_refusal_rows_judge_refusal_structurally_not_by_wording` | core | Dev refusal rows regress to pinning literal wording, or an uncited non-refusal and a cited non-refusal stop failing. | docs/testing.md#answer-tier (issue #596) |
| `test_run_query_non_200_records_envelope_code` | core | The eval reports every non-200 as unknown code or records upstream message text in the row. | docs/testing.md#answer-tier (issue #307) |
| `test_apply_pool_join_makes_completeness_computable` | core | Citation recall against the joined pool is not computed so completeness stays None or wrong. | docs/testing.md#answer-tier (issue #307) |
| `test_apply_pool_join_never_fails_or_touches_other_rows` | core | The pool join changes verdicts of abstain, error or unmapped rows. | docs/testing.md#answer-tier (issue #307) |
| `test_error_code_of_an_unstructured_error_body_is_unknown_and_never_the_body` | core | Without it a non-object error body leaks into the answer-eval report or crashes the row instead of recording the fixed unknown code. | docs/testing.md#answer-tier |

## `tests/test_eval_chat.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_follow_up_uses_class_template_with_default` | core | The chat A/B follow-up session is built wrongly, so condensed versus literal arms are compared on different turns. | docs/testing.md#evaluation (docs/eval.md) |
| `test_arm_entry_keeps_session_expectations_and_rebinds_id` | core | An arm loses the gold and trap expectations of its session, so scoring is meaningless. | docs/testing.md#evaluation (docs/eval.md) |
| `test_select_entries_filters_and_limits_deterministically` | core | The chat A/B samples abstain rows or varies between runs. | docs/testing.md#evaluation (docs/eval.md) |
| `test_evaluate_sessions_preserves_both_arms_and_condensation_accounting` | core | A condensation failure drops a session, falls back unaccounted, or retrieval runs against a physical other than the validated one. | docs/testing.md#evaluation (docs/eval.md) |

## `tests/test_eval_gate.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_check_baseline_detects_recall_regression` | core | A recall drop beyond the allowed ratio passes the retrieval regression gate. | docs/testing.md#evaluation |
| `test_check_baseline_detects_query_failures` | core | Failed queries do not fail the gate, so a broken run reads green. | docs/testing.md#evaluation |
| `test_main_exit_2_when_collection_mismatch` | core | A baseline recorded on a different corpus is compared silently or yields exit 0, instead of a loud skipped gate (exit 2) with artifacts still written. | docs/testing.md#evaluation (issue #159) |
| `test_main_exit_2_when_check_file_missing` | core | A missing baseline passes the gate; a skipped gate must not read as green. | docs/testing.md#evaluation (issue #159) |
| `test_main_exit_2_when_holdout_without_rc_declaration` | core | A dev run points at the frozen holdout and tunes against it. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_main_exit_2_when_collection_is_not_servable` | core | The eval certifies a generation the agent would refuse to serve. | docs/testing.md#evaluation |
| `test_main_refuses_baseline_from_unservable_collection` | core | A baseline is recorded from an unservable collection and later gates compare against a broken reference. | docs/testing.md#evaluation |
| `test_main_exit_0_when_gate_applied_and_green` | core | A gate that was applied against a matching baseline no longer exits 0, blocking healthy runs. | docs/testing.md#evaluation (issue #159) |
| `test_main_exit_0_when_no_gate_requested` | core | A trend-only run is turned into a failing gate. | docs/testing.md#evaluation (issue #159) |
| `test_main_exit_1_when_query_failures_despite_skip` | core | Query failures lose the more specific failure verdict to the skip signal. | docs/testing.md#evaluation (issue #159) |
| `test_load_golden_validates_and_rejects_empty` | core | Empty queries or empty expected docs load into the golden set and score vacuously. | docs/testing.md#evaluation |
| `test_gain_and_ndcg_at_k_calculation` | core | nDCG gains (doc, heading, page) and ideal-order normalization drift from the documented rule, moving the gated nDCG@8. | docs/testing.md |
| `test_generate_synthetic_golden_corpus` | infra | The L1 gate corpus generator stops embedding query text, identifiers and gold terms, so the gate measures an unanswerable corpus. | docs/testing.md |
| `test_gate_missing_baseline_exits_2` | core | The L1 gate passes without a baseline. | docs/testing.md#evaluation (issue #267) |
| `test_gate_non_hash_baseline_exits_2` | core | The hash-mode L1 gate compares against a non-hash baseline from a different environment. | docs/testing.md#evaluation (issue #267) |
| `test_gate_golden_sha_mismatch_exits_2` | core | A tampered golden set passes the L1 gate. | docs/testing.md#evaluation (issue #267) |
| `test_repo_golden_set_is_sha_pinned` | core | The committed dev golden set drifts from its sha pin unnoticed. | docs/testing.md#evaluation |
| `test_repo_golden_rows_opt_into_acceptance_states` | core | Golden or holdout rows lack the expected verification state, so the answer tier cannot check acceptance states. | docs/testing.md#answer-tier (issue #365) |
| `test_identifier_and_message_id_gates_are_absolute_not_ratio` | core | Identifier or message-id recall@1 is allowed a ratio floor instead of strict no-drop, passing a 0.99 against 1.0. | docs/testing.md#evaluation |
| `test_real_corpus_baseline_uses_no_drop_floor` | core | The real-corpus gate is red at its own sub-1.0 baseline or lets an identifier drop through. | docs/testing.md#evaluation (issue #286) |
| `test_requested_gate_rejects_empty_and_abstain_only_reports` | core | A requested gate certifies a report with no eligible observations. | docs/testing.md#evaluation (issue #367) |
| `test_requested_gate_rejects_missing_required_class` | core | A baseline class absent from the report passes the gate. | docs/testing.md#evaluation (issue #367) |
| `test_main_exit_2_when_embed_mode_mismatch` | core | A baseline recorded in another embed mode gates this run. | docs/testing.md#evaluation (issue #159) |
| `test_cli_scores_actual_rows_before_deciding_gate` | core | The CLI verdict stops reflecting real scoring of rows (empty, abstain-only, missing class, no applicable comparison), and a failure poisons the next invocation. | docs/testing.md#evaluation (issue #367) |
| `test_cli_rejects_missing_and_nonfinite_required_metrics` | core | None, NaN, infinite or non-numeric required metrics pass the gate. | docs/testing.md#evaluation (issue #367) |
| `test_explicit_gate_cannot_be_bypassed_by_diagnostic_cli_modes` | core | --check combined with --no-check or --label-draft silently skips an explicitly requested gate. | docs/testing.md#evaluation (issue #367) |
| `test_task_holdout_uses_real_cli_pin_validation_and_recovers` | infra | The holdout Task runs a tampered holdout, writes over a prior report, or does not recover after the pin is restored. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_task_retrieval_inputs` | infra | Task inputs (EMBED_MODE, VENUE, BUNDLE_DIR) are not resolved or normalized, so evals run in the wrong mode or venue. | docs/task-runner.md |
| `test_task_retrieval_record_then_gate` | infra | Recording a mode-keyed baseline and gating against it break, or recording touches other baselines. | docs/testing.md#evaluation |
| `test_task_retrieval_required_gate_refuses_and_recovers` | infra | The retrieval Task passes with a missing, mismatched or regressed baseline, or does not recover after repair. | docs/testing.md#evaluation |
| `test_retrieval_cli_explicit_paths_and_required_default` | infra | Explicit baseline and golden paths, empty-path refusal, conflicting flags and the required default behave wrongly, so a skipped gate can look green. | docs/testing.md#evaluation |
| `test_ndcg_at_k_ignores_relevant_hits_ranked_past_k` | core | Without it nDCG@k credits relevant documents ranked beyond k, inflating the retrieval gate metric. | docs/testing.md#evaluation |

## `tests/test_eval_scoring.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_shared_predicates_single_source_for_eval_harness_verify` | core | Eval, harness and verify disagree on what a relevant hit or sibling exception is, so gates and golden verification diverge. | docs/testing.md#evaluation |
| `test_score_entry_heading_scoped` | core | Heading-scoped gold counts a hit in the wrong chapter as relevant. | docs/testing.md#evaluation |
| `test_score_entry_abstain_excluded_from_recall` | core | Abstain rows enter recall and MRR denominators, deflating retrieval metrics. | docs/testing.md#evaluation |
| `test_golden_entry_rejects_unknown_class` | core | Typos in query_class are accepted into the golden schema and silently fall out of per-class gates. | docs/testing.md#evaluation |
| `test_score_entry_must_not_doc_violation_ranked` | core | A must_not document in the results is not reported as a ranked trap violation. | docs/testing.md#evaluation |
| `test_score_entry_must_not_window_is_top5` | core | The trap window widens or narrows from top-5, changing which traps fail the gate. | docs/testing.md#evaluation |
| `test_score_entry_must_not_allows_cocarrying_chunk` | core | A page documenting both the query id and its adjacent sibling is wrongly failed as a wrong-sibling answer. | docs/testing.md#evaluation |
| `test_score_entry_must_not_still_gates_sibling_only_chunk` | core | A sibling-only chunk escapes the trap gate through the co-carrying allowance. | docs/testing.md#evaluation |
| `test_score_entry_must_not_unparseable_query_stays_strict` | core | Queries without a parseable id get the lenient allowance, weakening the strict trap gate. | docs/testing.md#evaluation |
| `test_score_entry_page_diagnostic` | core | The page diagnostic counts a right page label on the wrong document as a hit. | docs/testing.md#evaluation |
| `test_summarize_mixed_corpus` | core | Abstain and error rows enter the recall denominator or must_not rate, so aggregate retrieval metrics and the trap rate are wrong. | docs/testing.md#evaluation |
| `test_check_baseline_zero_gate_ignores_baseline_values` | core | A recorded baseline with must_not violations lets future violations pass; the trap gate must stay absolute zero. | docs/testing.md#evaluation |
| `test_update_baseline_roundtrip_with_new_keys` | core | Baselines recorded from a report do not round-trip, so the next check against a fresh baseline fails or hides a regression. | docs/testing.md#evaluation |
| `test_load_golden_abstain_roundtrip` | core | Abstain entries with expected docs are accepted by the loader, or valid abstain rows fail to load. | docs/testing.md#evaluation |
| `test_measurement_preserves_calls_errors_and_next_run` | core | Retrieval eval stops counting failed queries, swallows errors, searches a physical other than the validated serving generation, or a failure poisons the next run. | docs/testing.md#evaluation |
| `test_retrieval_defaults_resolve_each_normalized_invocation` | core | Env-normalized embed mode picks the wrong golden or baseline pair, comparing against the wrong mode-keyed baseline. | docs/testing.md#evaluation |

## `tests/test_eval_task_sampling.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_task_sampling_and_repeat_counts` | infra | Task N and REPEATS inputs select the wrong sample or repeat count, or reference files are rewritten by non-recording runs. | docs/task-runner.md |
| `test_zero_sample_is_not_replaced_with_default` | infra | N=0 is silently replaced with the default sample size. | docs/task-runner.md |
| `test_invalid_sample_refuses_before_measurement` | infra | A malformed N reaches measurement, or shell metacharacters in it execute. | docs/task-runner.md |
| `test_invalid_repeat_refuses_before_measurement` | infra | A zero, negative or malformed REPEATS reaches measurement. | docs/task-runner.md |

## `tests/test_evidence_manifest.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_manifest_lists_only_supplied_excerpts` | core | The evidence manifest lists retrieved-but-unsent excerpts as citable, so answers cite sources the model never saw. | docs/agent.md#answer-contract |
| `test_manifest_records_partial_tail_boundaries` | core | A truncated tail excerpt is recorded as whole, misreporting what evidence the model received. | docs/agent.md#answer-contract |
| `test_reordered_blocks_keep_the_same_evidence_labels` | core | Reordering prompt blocks relabels excerpts so a model's [n] marker maps to the wrong citation. | docs/agent.md#answer-contract |
| `test_manifest_keeps_distinct_identity_for_duplicate_cites` | core | Two chunks sharing a display cite collapse into one provenance identity. | docs/agent.md#answer-contract |
| `test_manifest_maps_nonconsecutive_labels_without_rank_arithmetic` | core | [n] markers are resolved by rank arithmetic and cite the wrong source when labels are nonconsecutive. | docs/agent.md#answer-contract |
| `test_retrieved_but_omitted_explicit_citation_rejected` | core | A citation to a retrieved but unsent excerpt is accepted, giving users a verified-looking cite for unseen evidence. | docs/agent.md#answer-contract |
| `test_retrieved_but_omitted_bracket_marker_rejected` | core | A [n] marker pointing at an omitted excerpt is inferred as a valid citation. | docs/agent.md#answer-contract |
| `test_supplied_explicit_citation_still_accepted` | core | Valid explicit citations of supplied excerpts are rejected, leaving correct answers unverified (positive control). | docs/agent.md#answer-contract |
| `test_supplied_bracket_marker_maps_to_prompt_label` | core | A bracket marker for a supplied excerpt is not mapped to its citation, so inferred cites are lost. | docs/agent.md#answer-contract |
| `test_example_cite_is_not_evidence_when_all_excerpts_trimmed` | core | The prompt's worked-example cite becomes evidence, or the model is called with an over-budget prompt, when every excerpt is trimmed. | issue #368; docs/agent.md#answer-contract |
| `test_prior_turn_citation_is_not_current_evidence` | core | A citation from an earlier chat turn is accepted as evidence for the new answer. | docs/agent.md#answer-contract |
| `test_omitted_citation_rejected_identically_in_stream_and_json` | core | Streaming and JSON consumers disagree on whether an omitted citation is accepted. | issue #364; docs/agent.md#answer-contract |
| `test_bracket_marker_grammar` | core | Bracket-marker parsing accepts malformed or out-of-range markers (or rejects valid ones), inventing or dropping inferred citations. | docs/agent.md#answer-contract |
| `test_marker_in_dropped_or_extracted_fence_is_not_provenance` | core | A [n] inside a discarded thinking fence or extracted script is treated as a citation. | docs/agent.md#answer-contract |
| `test_example_cite_alone_in_citations_block_is_not_supplied` | core | With zero supplied evidence the prompt's example cite is accepted as a real citation. | docs/agent.md#answer-contract |

## `tests/test_evidence_service.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_reference_matches_independent_literal_envelope_bytes` | core | Without it the evidence reference could change encoding and invalidate every issued reference or stop binding the exact stored bytes. | docs/evidence-contract.md#evidence-contract |
| `test_every_stored_field_changes_the_reference` | core | Without it a stored field could change without changing the reference, so a reference would not prove the content. | docs/evidence-contract.md#evidence-contract |
| `test_reference_roundtrip_and_malformed_rejections` | core | Without it malformed or forged reference strings could parse as valid or crash the service. | docs/evidence-contract.md#evidence-contract |
| `test_exact_read_returns_stored_bytes_provenance_and_stays_read_only` | core | Without it an exact read could alter text, drop provenance, or issue a write to Qdrant. | docs/evidence-contract.md#evidence-contract |
| `test_unrecorded_units_are_null_not_empty_and_missing_page_end_is_explicit` | core | Without it absent atomic units would read as no atomic items, misleading clients splitting scripts. | docs/evidence-contract.md#evidence-contract |
| `test_old_reference_survives_alias_swap_and_repair_and_never_reads_successor` | core | Without it an old reference would read the successor build or a forged digest would return other text. | docs/evidence-contract.md#evidence-contract |
| `test_removed_build_is_unavailable_never_the_successor` | core | Without it a retired build's reference would silently return the successor's different text. | docs/evidence-contract.md#evidence-contract |
| `test_missing_or_redirected_controls_refuse_instead_of_guessing` | core | Without it broken control records or aliases would let a read guess a build instead of refusing as corrupt. | docs/evidence-contract.md#evidence-contract |
| `test_a_data_alias_redirected_to_another_build_is_refused` | core | Without it a redirected data alias would return another build's text under this reference. | docs/evidence-contract.md#evidence-contract |
| `test_stored_data_changed_under_the_pinned_build_is_refused` | core | Without it data mutated under a pinned build would be returned although it no longer matches the digest. | docs/evidence-contract.md#evidence-contract |
| `test_payload_that_cannot_form_a_complete_envelope_is_never_returned` | core | Without it a malformed stored payload would return a partial envelope or mint a reference for it. | docs/evidence-contract.md#evidence-contract |
| `test_budget_is_whole_chunk_or_explicit_413_never_a_prefix` | core | Without it a byte budget could return a truncated prefix as if it were the whole evidence. | docs/evidence-contract.md#evidence-contract |
| `test_revocation_applies_to_the_next_read_on_the_same_service` | core | Without it a revoked caller could keep reading from a cached grant. | docs/evidence-contract.md#evidence-contract |
| `test_revocation_between_admission_and_response_is_redecided` | core | Without it access revoked mid-request would still return the evidence. | docs/evidence-contract.md#evidence-contract |
| `test_policy_outage_and_missing_identity_fail_closed` | core | Without it a policy outage or missing identity would allow reads instead of refusing. | docs/evidence-contract.md#evidence-contract |
| `test_denied_caller_learns_nothing_from_corruption_or_size` | core | Without it a denied caller could infer data corruption or size from different errors. | docs/evidence-contract.md#evidence-contract |
| `test_product_and_version_assertions_only_narrow` | core | Without it a caller's product or version assertion could widen access to other manuals. | docs/evidence-contract.md#evidence-contract |
| `test_cancellation_propagates_through_the_real_await_boundary` | core | Without it a cancelled read would leave the storage call running and leak work. | docs/evidence-contract.md#evidence-contract |
| `test_legacy_or_refused_or_foreign_generations_mint_nothing` | core | Without it references could be minted for legacy, refused or foreign generations that cannot be read back. | docs/evidence-contract.md#evidence-contract |
| `test_minted_reference_equals_the_independent_witness_and_reads_back` | core | Without it search hits would carry a reference that differs from the independent witness or fails to read back, and minting would read more than the served hits. | docs/evidence-contract.md#evidence-contract |
| `test_public_failure_table_is_fixed_and_leaks_nothing` | core | Without it public status codes and messages for evidence failures could change or leak internal detail. | AGENTS.md hard boundary: fixed client error messages and stable codes |
| `test_search_hits_carry_a_reference_that_reads_back_exactly` | core | Without it search results would carry no usable reference or a reference that reads back different content over HTTP. | docs/evidence-contract.md#evidence-contract |
| `test_search_without_a_build_binding_returns_null_reference_and_still_succeeds` | core | Without it searching a legacy generation would fail instead of returning hits with no reference. | docs/evidence-contract.md#evidence-contract |
| `test_search_survives_a_reference_fault_with_null_reference` | core | Without it a storage fault during minting would fail the whole search or leak storage text. | docs/evidence-contract.md#evidence-contract |
| `test_http_failure_envelopes_are_fixed_and_leak_nothing` | core | Without it evidence HTTP errors would use unstable codes or leak digests and stored text. | AGENTS.md hard boundary: fixed client error messages and stable codes |
| `test_http_access_outcomes_map_per_contract` | core | Without it denied, outage and unauthenticated access would map to wrong statuses or not recover on the next read. | docs/evidence-contract.md#evidence-contract |
| `test_http_storage_fault_and_deadline_use_fixed_envelopes` | core | Without it storage faults or deadlines would leak storage text or leave the endpoint broken for the next read. | AGENTS.md hard boundary: fixed client error messages and stable codes |
| `test_evidence_endpoint_is_counted_with_a_bounded_label` | core | Without it evidence requests would be unmetered or create one metric label per reference. | docs/agent.md#1-endpoints |

## `tests/test_failfast.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_refuses_vllm_without_dense_dim` | core | Without it an agent with no DENSE_DIM would start and serve vectors of an unknown size instead of refusing to listen. | docs/deploy.md#configuration-contract |
| `test_refuses_vllm_without_embed_endpoint` | core | Without it an agent missing its embed endpoint or model would start and fail every query at request time. | docs/deploy.md#configuration-contract |
| `test_refuses_hash_without_explicit_allow` | core | Without it hash embeddings could run in a deployment that never opted in to ALLOW_HASH_MODE. | AGENTS.md hard boundary: hash embeddings |
| `test_refuses_unknown_embed_mode` | core | Without it a typo or unsupported mode such as a cloud inference name would silently pick some embedder. | AGENTS.md hard boundary: no Qdrant Cloud inference |
| `test_allows_hash_when_explicitly_allowed` | core | Without it the dev/CI hash profile that every hermetic test and rehearsal uses could break unnoticed, or the opt-in could be ignored. | AGENTS.md hard boundary: hash embeddings |
| `test_refuses_vllm_without_model_revision` | core | Without it a mutable gateway alias could be accepted as the embedding model identity, so stored vectors could silently mismatch. | issue #362 |
| `test_lifespan_refuses_drifted_store` | core | Without it the agent would serve a store built under a different embed or rules contract and return wrong results, instead of refusing with the re-embed path. | docs/agent.md#serving-contract |
| `test_lifespan_refuses_legacy_store` | core | Without it a non-empty store with no contract record would be served as if it were validated. | docs/agent.md#serving-contract |
| `test_lifespan_unreachable_store_warns_but_listens` | core | Without it a transient Qdrant outage at boot would stop the agent from listening, hiding the healthz signal operators use. | docs/agent.md#serving-contract |

## `tests/test_gateway_ca.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_ca_mount_targets_only_application_containers` | infra | Without it the gateway CA could be mounted into OAuth/Jaeger containers or missed by agent/ingest so model calls fail TLS verification. | docs/testing.md#helm-coverage |
| `test_ca_absent_or_invalid_render` | infra | Without it a malformed CA ConfigMap name would render a broken pod or an absent name would still inject CA wiring. | docs/testing.md#helm-coverage |
| `test_ca_preflight_requires_configmap_and_key` | infra | Without it a deploy proceeds with a missing CA ConfigMap or key and the pods fail only after rollout. | docs/testing.md#helm-coverage |
| `test_operator_ca_environment_beats_file` | infra | Without it a stale airgap.env value silently overrides the CA ConfigMap name the operator set explicitly. | docs/testing.md#helm-coverage |

## `tests/test_gateway_strict_finish.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_passthrough_keeps_model_scope` | core | Without it a key scoped to one model can use pass-through endpoints for other models. | AGENTS.md hard boundaries (per-leg keys) |
| `test_forwards_an_observed_provider_finish_and_closes` | infra | Without it genuine provider finish reasons are dropped or the upstream stream is left open. | docs/agent.md#http-model-contract |
| `test_missing_provider_finish_never_forwards_success` | infra | Without it the gateway forwards a finish it invented after the provider ended without one, making a truncated answer look complete. | docs/agent.md#http-model-contract |
| `test_unavailable_upstream_state_fails_closed` | infra | Without it a stream with unknown upstream state is forwarded as output. | docs/agent.md#http-model-contract |
| `test_upstream_error_is_propagated_and_stream_closed` | infra | Without it an upstream error is swallowed or the upstream connection leaks. | docs/agent.md#http-model-contract |
| `test_consumer_stop_closes_upstream` | infra | Without it a client disconnect leaves the upstream generation running. | docs/agent.md#http-model-contract |
| `test_rejects_before_router_wrapper_can_erase_provider_state` | infra | Without it the router wrapper hides the provider's missing finish and a synthetic finish is forwarded. | docs/agent.md#http-model-contract |
| `test_adapter_request_failure_is_not_retried` | infra | Without it a failed model request is retried by the gateway and duplicates generation. | docs/agent.md#http-model-contract |

## `tests/test_generic_pdf.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_plain_pdf_uses_filename_stem_and_chunks_without_outline` | core | A PDF with no form number or outline ingests with filename-stem identity and still produces chunks, so non-IBM manuals are not rejected. | AGENTS.md hard boundary: parsing stays generic |
| `test_path_layout_vendor_product_version` | core | vendor/product/version are inferred from the corpus path layout, so unknown-vendor manuals get correct scope labels. | AGENTS.md hard boundary: parsing stays generic |
| `test_detect_vendor_default_unknown` | core | Unrecognized paths fall back to vendor unknown and a named top directory is honored, so the unknown-vendor fallback stays supported. | AGENTS.md hard boundary: parsing stays generic |

## `tests/test_ha_cluster.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_real_three_peer_healthy_fixture` | infra | Without it a healthy 6/3/2 cluster might be reported unhealthy, lose exact payloads on a peer, or the publication cutover gate could drift from the real client. | docs/install_and_ops.md#54-qdrant-632-topology-operations-issue-360 |
| `test_false_ha_configuration_refused` | infra | Without it an RF1 corpus on three ready peers could pass placement verification as highly available. | docs/install_and_ops.md#54-qdrant-632-topology-operations-issue-360 |
| `test_cutover_gate_refuses_false_ha_staging_pair` | infra | Without it publication could cut over to an RF1 staging pair that is not highly available. | docs/install_and_ops.md#54-qdrant-632-topology-operations-issue-360 |
| `test_cutover_gate_refuses_control_only_mismatch` | infra | Without it publication could cut over with a healthy corpus but an RF1 control collection. | docs/install_and_ops.md#54-qdrant-632-topology-operations-issue-360 |
| `test_peer_loss_is_degraded_and_rejoins_healthy` | infra | Without it a lost peer could go unreported, the cutover gate could pass while degraded, or rejoin be declared healthy before catch-up. | docs/install_and_ops.md#54-qdrant-632-topology-operations-issue-360 |
| `test_acknowledged_writes_survive_one_peer_loss` | core | Without it acknowledged writes could be lost when one peer dies. | docs/install_and_ops.md#54-qdrant-632-topology-operations-issue-360 |
| `test_identical_id_retry_converges_without_duplicates` | core | Without it retrying an interrupted write with stable ids could duplicate or diverge points across peers. | docs/install_and_ops.md#54-qdrant-632-topology-operations-issue-360 |
| `test_partition_minority_writes_unacknowledged_and_majority_durable` | infra | Without it minority-side writes could be acknowledged during a partition and later lost or diverge. | docs/install_and_ops.md#54-qdrant-632-topology-operations-issue-360 |
| `test_operator_job_peers_reach_real_placement_gate` | infra | Without it the rendered operator ingest job could skip or fail the real placement gate across direct peers. | docs/install_and_ops.md#54-qdrant-632-topology-operations-issue-360 |
| `test_operator_job_publishes_exact_corpus_on_three_peers` | infra | Without it the canonical airgap ingest job could publish an inexact corpus, fail to recover a refused candidate, or churn on reruns. | docs/install_and_ops.md#54-qdrant-632-topology-operations-issue-360 |

## `tests/test_harness_l1.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_collect_rows_uses_exact_order_depth_and_propagates_errors` | core | A retrieval failure during the L1 harness run is swallowed or queries are scored at a different depth than production search, producing a false-green retrieval gate. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_ci95_paired_signal_excludes_zero_and_noise_straddles_it` | core | The merge gate approves a change on pure noise or rejects a real improvement because the paired bootstrap CI no longer separates signal from noise. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_ci95_regression_pinned_seeded_values` | core | The seeded bootstrap stops being deterministic across machines so the same data yields different merge verdicts on reruns. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_score_row_full_graded_match` | core | Recall/MRR/nDCG for a perfect hit is mis-scored or over-counts duplicate chunks of one doc, inflating the L1 baseline. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_score_row_abstain_excluded_but_traps_checked` | core | Abstain queries get scored as recall misses or their must_not trap violations stop being detected. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_score_row_cocarrying_chunk_not_a_violation` | core | A chunk legitimately carrying the target and a sibling message id is flagged as a P0 trap failure, blocking valid changes. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_aggregate_per_class_and_absolute_trap_precision` | core | The gate sees only aggregate metrics (hiding a per-class regression) or a single trap violation no longer lowers trap precision. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_gate_no_baseline_records` | core | A missing baseline is scored as a pass instead of being treated as record-only. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_gate_identical_run_holds_no_improvement` | core | An unchanged run merges without any primary metric improving beyond CI overlap. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_gate_improvement_merges` | core | A real improvement in recall@5 or MRR can no longer produce a merge verdict. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_gate_trap_failure_holds_even_with_improvement` | core | A P0 trap failure merges because aggregate metrics improved. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_gate_class_regression_holds_despite_overall_gain` | core | A per-class regression beyond the floor is masked by an overall gain and merges. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_gate_golden_entry_mismatch_holds` | core | A dev-venue run is scored against an RC baseline with a different entry set, producing a verdict over a truncated venue. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_baseline_round_trip` | core | Recorded per-query values are not what the gate pairs against, making the stored baseline unusable or the gate a no-op. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_baseline_path_mode_keyed` | infra | Hash and vllm runs share one baseline file so one gate scores against another environment's numbers. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_fingerprint_orders_by_creation_time_and_prefers_recorded_pin` | infra | The snapshot fingerprint picks a stray snapshot instead of the recorded pin so drift skip or restore targets the wrong snapshot. | docs/testing.md |
| `test_resolve_drift_match_skips_restore` | infra | An exact fingerprint match triggers a needless 30s+ restore of a large collection on every gate run. | docs/testing.md |
| `test_resolve_drift_restores_recorded_pin_despite_drift` | infra | A drifted index is restored from a stray snapshot rather than the recorded pin, scoring against the wrong data. | docs/testing.md |
| `test_resolve_drift_fails_when_recorded_pin_deleted` | infra | A gate run silently pins live state when the recorded pin was deleted instead of failing closed. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_resolve_drift_fails_without_recorded_pin` | infra | A gate run with no recorded pin proceeds against whatever index exists instead of refusing to score. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_resolve_drift_record_mode_pins` | infra | Recording a baseline does not pin a snapshot so the baseline cannot be reproduced. | docs/testing.md |
| `test_pin_snapshot_adopts_only_on_exact_match` | infra | Pinning re-adopts a snapshot whose point count differs, or never prunes strays, leaving an ambiguous pin. | docs/testing.md |
| `test_pin_snapshot_drifted_record_creates_new_pin_and_prunes_old` | infra | A record run against a drifted index stores new point count with the old snapshot name, a baseline no restore can reproduce. | docs/testing.md |
| `test_pin_snapshot_no_recorded_pin_creates_fresh` | infra | A first baseline adopts an unknown pre-existing snapshot instead of pinning current state. | docs/testing.md |
| `test_restore_snapshot_uses_recorded_name_and_dir_setting` | infra | Restore recovers from the wrong snapshot name or directory, so the gate runs on an unintended index. | docs/testing.md |
| `test_restore_snapshot_fails_closed_when_missing` | infra | A missing recorded snapshot is ignored and the gate scores against the live index. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_restore_snapshot_fails_when_index_does_not_roll_back` | infra | A recover call that reports success without rolling the index back lets the gate score a mutated index. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_harness_task_and_cli_select_same_mode_inputs` | infra | The Task entry and the CLI choose different dataset, baseline or pin for the same mode, or ambient variables override the declared inputs. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_harness_task_records_selected_dataset_then_next_gate` | infra | Recording writes the wrong mode's baseline or leaves a recorded baseline that the next ordinary gate cannot use. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_harness_direct_explicit_dataset_and_baseline_remain_authoritative` | infra | Explicit --golden/--baseline paths are overridden by defaults or shell-interpreted, scoring the wrong data. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_harness_task_refuses_invalid_inputs_before_external_effects` | infra | A missing baseline or corrupt holdout pin still contacts Qdrant or the embedder before refusing to score. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_empty_classes_get_no_confidence_interval` | core | Without it an empty query class gets a fabricated confidence interval in the L1 promotion evidence. | docs/testing.md#evaluation |
| `test_resolve_always_restores_the_recorded_pin_and_never_pins_live_state` | infra | Without it a gate run with --restore always restores or pins whatever snapshot happens to exist, promoting mutated live state as the baseline. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |

## `tests/test_harness_l2.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_cited_doc_ids_maps_and_records_unmatched` | core | Citations that do not map back to the fetched pool are dropped silently, giving silently-wrong precision/recall. | docs/testing.md |
| `test_precision_recall_no_citations_zero_recall_not_none` | core | A zero-citation answer drops out of the recall denominator and no longer pulls the average down. | docs/testing.md |
| `test_precision_recall_no_gold_is_none` | core | Abstain rows without gold enter the precision/recall averages and distort them. | docs/testing.md |
| `test_run_l2_stays_on_server_default_temperature` | core | L2 inherits the answer eval's temperature 0, comparing recorded thresholds against differently-sampled means. | issue #596 |
| `test_judge_messages_never_contain_citation_markers` | core | The faithfulness judge sees citation markers or doc ids and rates the answer on its labels rather than on the evidence. | docs/testing.md |
| `test_judge_chat_pins_temperature_and_reasoning_effort` | core | The judge runs at server-default effort and returns reasoning in content with no parsable label, causing judge-infra failures. | issue #305 |
| `test_syntax_check_invalid_pattern_fails_closed` | core | A malformed syntax_pattern is treated as a miss or pass instead of a structural failure. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_alert_capture_joins_request_ids` | core | Truncation alerts are not joined to rows by request_id, undercounting truncation or crashing on non-JSON log lines. | docs/testing.md |
| `test_apply_fail_closed_on_unmatched_citation` | core | A validated citation missing from the fetched pool no longer fails the row, so the P/R join degrades silently. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_apply_missing_request_id_fails_closed` | core | A row without request_id reports truncated=false, undercounting truncation. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_apply_syntax_miss_fails_row` | core | An answer lacking the required syntax construct passes the structural gate. | docs/testing.md |
| `test_summarize_rates_and_structural_counts` | core | Structural fails, errors or rates are mis-counted, so the exit code or trend data is wrong. | docs/testing.md |
| `test_gate_passes_on_rates_and_holds_on_fails` | core | Structural failures no longer gate the L2 exit code. | docs/testing.md |
| `test_gate_holds_on_judge_error_alone` | core | An unparseable judge output no longer holds the gate and quality is reported unmeasured as fine. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_apply_fails_vacuous_body_with_validated_citations` | core | An answer consisting only of a copied Citations section photographs healthy precision/recall over zero substance. | issue #192 |
| `test_run_l2_http_sequence_attribution_and_next_run` | core | L2 attributes answers/alerts to the wrong request, leaks state into the next run, or leaves handlers and judge clients open after a search failure. | docs/testing.md |
| `test_precision_recall_semantics` | core | Citation precision/recall are computed wrongly for partial overlap or the None/zero distinction is lost. | docs/testing.md |
| `test_judge_labels_parse_and_fail_closed_on_junk` | core | Judge output with unknown labels or prose is accepted as a label instead of raising. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_syntax_check_miss_is_false_and_bad_pattern_fails_closed` | core | Syntax pattern matches via script are missed or a miss raises an error instead of a False row. | docs/testing.md |
| `test_apply_truncation_join_and_content_floor_exemptions` | core | Truncation is not joined from alerts, or the content floor wrongly fails abstain, script or no-cite answers. | docs/testing.md |
| `test_gate_holds_on_relevance_judge_error_alone` | core | Without it a failing relevance judge lets the answer-tier gate pass on an unjudged run. | docs/testing.md#answer-tier |
| `test_judge_evidence_is_bounded_before_the_prompt` | core | Without it unbounded cited evidence is sent to the judge model, overflowing its window and failing the whole judged run. | docs/testing.md#answer-tier |

## `tests/test_harness_l3.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_gate_verdict_no_baseline_records_but_never_drops_errors` | core | A missing baseline masks request errors so a broken run is recorded or passed. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_gate_verdict_missing_server_timing_header_fails` | core | Responses without Server-Timing headers pass the latency gate with empty stage data. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_gate_verdict_missing_expected_stage_fails` | core | A stage present in the baseline disappearing from the run goes unnoticed as a header regression. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_gate_verdict_env_mismatch_fails_closed` | core | A baseline from a different cpu, concurrency or GPU gates a run and reports spurious regressions instead of a distinct env error. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_gate_verdict_passes_within_tolerance` | core | A healthy run within tolerance is held, blocking the L3 gate. | docs/testing.md |
| `test_gate_verdict_fails_on_stage_latency_regression` | core | A per-stage p95 regression beyond the x3 limit passes. | docs/testing.md |
| `test_main_resolves_mode_baseline_per_invocation` | infra | L3 uses the baseline of a previous invocation's mode, gating hash numbers against vllm or vice versa. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_task_l3_matches_actual_cli_baseline_decision` | infra | The Task and CLI choose different baselines, or an explicit/missing override is ignored or shell-evaluated. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_task_l3_recording_only_updates_selected_synthetic_baseline` | infra | Recording an L3 baseline overwrites another mode's file or the next ordinary gate does not use the new baseline. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_env_snapshot_records_run_provenance_the_gate_compares` | infra | Without it the L3 run env omits concurrency and GPU or records unset options as values, so the env-mismatch gate compares the wrong machine shape. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |

## `tests/test_harness_l4.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_summarize_means_rates_and_sums_faults` | core | Repeat means or summed structural/request/judge faults are wrong so the gate sees healthy numbers. | docs/testing.md |
| `test_gate_passes_at_reference` | core | A run at the reference does not pass the L4 gate. | docs/testing.md |
| `test_gate_holds_inside_the_band` | core | A metric inside tolerance fails outright or passes with no human-review hold. | docs/testing.md |
| `test_gate_fails_outside_the_band` | core | A metric outside tolerance passes the L4 gate. | docs/testing.md |
| `test_gate_structural_fault_dominates_healthy_rates` | core | Structural failures in any repeat are hidden by healthy rate means. | docs/testing.md |
| `test_gate_uncomputed_metric_fails_closed` | core | A metric that could not be computed disappears from the gate instead of failing. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_gate_max_metric_regression_fails` | core | A rise in a lower-is-better metric such as truncation rate is treated as an improvement. | docs/testing.md |
| `test_load_thresholds_missing_fails_closed` | infra | A missing reference file lets L4 score with no thresholds. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_load_thresholds_malformed_reference_fails_closed` | infra | A reference with missing metrics, missing tier metadata or a bad tolerance is used to gate. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_save_thresholds_records_mode_and_rounds` | infra | A recorded reference omits venue, embed mode or reasoning model so cross-tier use cannot be refused. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_record_blockers_allow_structural_debt` | infra | Recording is blocked by known product structural debt that gates every run anyway. | docs/testing.md |
| `test_record_blockers_reject_broken_measurement` | infra | A reference is recorded from a run with request errors, judge errors or uncomputed metrics. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_review_queue_selects_weak_rows_only` | infra | The human-review queue omits borderline rows or floods with clean ones. | docs/testing.md |
| `test_main_exit_2_when_reference_missing` | infra | The L4 CLI scores without a reference instead of exiting 2. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_l4_repeats_preserve_sample_and_judge_legs` | infra | L4 repeats a different sample or disables judge legs, so repeat means do not measure the stated quality. | docs/testing.md |
| `test_classify_max_direction_has_a_borderline_band_before_fail` | core | Without it a lower-is-better metric slightly over its reference is reported as a hard fail (or pass), changing the L4 gate verdict and review queue. | docs/testing.md#evaluation |

## `tests/test_hash_embed.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_hash_embedder_is_deterministic_normalized_and_offline` | infra | The dev/CI hash embedder is deterministic, unit-norm and needs no network, so hash-mode CI runs are reproducible and offline. | AGENTS.md hard boundary: hash embeddings dev/CI only |
| `test_hash_mode_needs_no_dense_dim_and_rejects_vllm_requirement` | infra | Hash mode derives its dimension and refuses vLLM-only requirements, so a hash configuration cannot be mistaken for a served embed endpoint. | AGENTS.md hard boundary: hash embeddings dev/CI only |
| `test_vllm_embedder_dense_query_prepends_prefix` | core | Queries get the asymmetric-embedder prefix while ingested documents stay raw, otherwise query and document vectors mismatch and recall drops. | docs/retrieval.md |
| `test_vllm_embedder_auth_header_present_only_when_key_set` | core | The embed call sends a Bearer header only when a key is configured and omits it otherwise, so keyed gateways authenticate and keyless ones are not sent empty auth. | docs/testing.md#tests-must-lock-the-claimed-path |
| `test_embed_bound_refuses_before_the_post_and_never_truncates` | core | An over-bound embed input is refused with counts only before any request and is never truncated, so oversize text cannot silently degrade vectors or leak into errors. | issue 374 |
| `test_embed_bound_default_is_unbounded_legacy_behaviour` | core | With no bound selected a 43k-character statement is still embedded whole, so the bound feature cannot silently change default behavior. | issue 374 |

## `tests/test_hazard_sensitivity.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_only_intended_behavioral_failure_counts` | gate-control | A mutation run counts as killed for an unrelated failure, or a baseline failure is not flagged. | tests/hazards/critical.json |
| `test_missing_skipped_setup_or_wrong_assertion_never_counts` | gate-control | A missing, duplicated, skipped, setup-error or wrong-assertion result is credited as a hazard kill. | tests/hazards/critical.json |
| `test_corrupt_result_never_counts` | gate-control | A truncated result file is credited as a kill. | tests/hazards/critical.json |
| `test_mutation_requires_exact_preimage_and_keeps_other_bytes` | gate-control | A mutation is applied to a missing preimage, outside the allowed targets, or alters other bytes. | tests/hazards/critical.json |
| `test_pytest_rewritten_assertion_format_counts` | gate-control | A genuine kill reported in pytest's rewritten assertion format is rejected as invalid. | tests/hazards/critical.json |
| `test_expected_assertion_in_earlier_frame_is_not_a_kill` | gate-control | A failure from another frame or test that mentions the expected assertion is credited as the kill. | tests/hazards/critical.json |
| `test_pristine_pair_does_not_credit_baseline_marker_for_irrelevant_mutation` | gate-control | An irrelevant mutation passes because baseline state leaked into the mutated run. | tests/hazards/critical.json |
| `test_parallel_pairs_overlap_in_isolated_processes_and_preserve_failed_baseline` | gate-control | Parallel hazard pairs share state, leave processes behind, or run a mutation after a failed baseline. | tests/hazards/critical.json |
| `test_parallel_pair_workers_are_bounded_before_spawning` | infra | An unbounded worker count spawns processes before being refused. | tests/hazards/critical.json |
| `test_passing_test_cannot_mutate_snapshot_source` | gate-control | A hazard test that rewrites source files still counts as a valid baseline. | tests/hazards/critical.json |
| `test_timeout_reaps_started_descendant_and_next_test_is_clean` | gate-control | A timed-out hazard test leaves a running descendant that corrupts the next hazard run. | tests/hazards/critical.json |

## `tests/test_helm_chart_contracts.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_base_contract_with_independent_controls` | infra | Without it the agent default image, probes, resources, typed environment, read-only Qdrant key or no-hook/no-StatefulSet shape can drift unnoticed. | docs/testing.md#helm-coverage |
| `test_gateway_keys_contract` | infra | Without it model gateway keys stop being injected from the named Secret and the agent loses model access. | docs/testing.md#helm-coverage |
| `test_pull_secret_and_service_name_and_tracing_off` | infra | Without it the pull Secret, service name or tracing-off state is dropped and the image pull or telemetry breaks. | docs/testing.md#helm-coverage |
| `test_nondefault_namespace_registry_and_rerank` | infra | Without it a non-default namespace, registry or rerank setting is ignored on install. | docs/testing.md#helm-coverage |
| `test_route_oauth_contract` | infra | Without it the OAuth sidecar, reencrypt Route, ports or secrets are miswired and the console is exposed unauthenticated or unreachable. | docs/testing.md#helm-coverage |
| `test_jaeger_contract` | infra | Without it the Jaeger image, storage, ports or pipelines drift and traces are lost or the backend fails to start. | docs/testing.md#helm-coverage |
| `test_servicemonitor_contract` | infra | Without it the ServiceMonitor selector or port drifts and metrics are silently not scraped. | docs/testing.md#helm-coverage |
| `test_ingest_job_contract_normal` | infra | Without it the ingest Job loses its writer key, read-only corpus mount, scratch volume or exact arguments. | docs/testing.md#helm-coverage |
| `test_ingest_job_contract_maintenance_with_tricky_revision` | infra | Without it retire/reingest arguments or per-leg Secret references with tricky values are corrupted on the way to the Job. | docs/testing.md#helm-coverage |
| `test_rerank_enabled_without_base_url_falls_back` | infra | Without it the chart rejects the valid rerank-on empty-URL configuration that falls back to the embedding endpoint. | docs/testing.md#helm-coverage |
| `test_schema_rejects_invalid_selected_configuration` | infra | Without it invalid operator values (tags, digests, NFS storage, retired switches, bad endpoints) render a broken release instead of failing before mutation. | docs/testing.md#helm-coverage |
| `test_chart_shape_no_qdrant_no_hook_no_secret_values` | infra | Without it the chart could start owning Qdrant, add Helm hooks or ship secret-like values. | docs/testing.md#helm-coverage |
| `test_numeric_looking_release_sha_remains_an_exact_env_string` | infra | Without it an all-digit release SHA is rendered as a number and loses leading zeros. | docs/testing.md#helm-coverage |
| `test_jaeger_claim_is_retained_across_release_removal` | infra | Without it removing a release deletes the Jaeger trace volume. | docs/testing.md#helm-coverage |
| `test_chart_preserves_positive_operator_dimension_and_worker_range` | infra | Without it large embedding dimensions or worker counts are rejected or altered at render time. | docs/testing.md#helm-coverage |
| `test_direct_chart_optional_budget_limits_reach_pod` | infra | Without it direct chart installs drop model budget limits at the schema boundary. | docs/testing.md#helm-coverage |
| `test_direct_chart_refuses_invalid_model_budget` | infra | Without it out-of-range or mistyped model budgets reach the pod and fail at runtime. | docs/testing.md#helm-coverage |
| `test_ui_off_by_route_matrix` | infra | Without it UI selection and Route/OAuth wiring become coupled so an API-only install changes its exposure. | docs/testing.md#helm-coverage |
| `test_direct_peers_are_typed_and_ingest_only` | infra | Without it Qdrant peer URLs leak into the serving agent or malformed peer lists render silently. | docs/testing.md#helm-coverage |
| `test_jaeger_decoupled_from_export` | infra | Without it operators cannot export traces to an external collector without a bundled Jaeger. | docs/testing.md#helm-coverage |
| `test_jaeger_disabled_without_destination_fails` | infra | Without it disabling bundled Jaeger leaves the agent exporting to a Service nothing deploys. | docs/testing.md#helm-coverage |
| `test_jaeger_disabled_with_tracing_off_is_intentional` | infra | Without it the valid tracing-off configuration is rejected or still deploys Jaeger. | docs/testing.md#helm-coverage |
| `test_jaeger_enabled_with_tracing_off_fails` | infra | Without it an idle Jaeger nothing exports to is deployed. | docs/testing.md#helm-coverage |
| `test_servicemonitor_decoupled_from_exposition` | infra | Without it platform-monitored installs cannot expose metrics without a ServiceMonitor object. | docs/testing.md#helm-coverage |
| `test_servicemonitor_enabled_with_metrics_off_fails` | infra | Without it a ServiceMonitor is created for a disabled metrics endpoint. | docs/testing.md#helm-coverage |
| `test_every_rendered_pod_is_hardened_without_fixed_identities` | infra | Without it a pod loses non-root, seccomp or capability dropping, or gains a fixed UID, breaking restricted-v2. | docs/testing.md#helm-coverage |
| `test_app_read_only_roots_keep_only_declared_writable_paths` | infra | Without it app roots become writable or lose tmp/work mounts so the container is over-privileged or crashes. | docs/testing.md#helm-coverage |
| `test_verified_digest_renders_images_by_digest_keeping_tag_env` | infra | Without it verified digests are not used for pod images so mutable tags run instead of the approved bytes. | docs/testing.md#helm-coverage |

## `tests/test_hygiene.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_repo_contains_no_pdf_or_adobe_catalogs` | core | Vendor PDFs or Adobe catalogs get committed to the repository. | AGENTS.md#hard-boundaries |
| `test_litellm_is_only_imported_by_the_gateway_only_hooks` | core | LiteLLM returns as a product import, dependency or lock pin, or the gateway hooks ship in product images. | AGENTS.md#hard-boundaries |

## `tests/test_identifiers.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_message_id_families` | core | CICS, IMS and four-letter message ids are extracted while module names are not, so identifier routing finds real messages without false hits. | docs/ingest.md |
| `test_query_kind_flips_only_for_real_identifiers` | core | Queries flip to identifier ranking only for real message or system codes, so ordinary words do not hijack search ranking. | docs/retrieval.md |
| `test_golden_sweep_flips_are_real_codes` | core | Across every golden and paraphrase query only the reviewed real codes gain message ids, so a widened regex cannot silently add false identifiers. | docs/retrieval.md |
| `test_syscode_self_contexting_forms_are_normalized` | core | S-prefix, X literal, user-code and wait-state forms normalize to the stored canonical code, so system-code filters match what ingest stored. | issue 591 |
| `test_bare_code_needs_an_adjacent_code_phrase` | core | A bare three-hex token counts as a code only next to a code phrase, so ordinary words and counts are not routed as abend codes. | issue 591 |
| `test_codes_are_not_confused_with_words_models_or_doc_numbers` | core | English words, S390-style models and form numbers are never read as completion codes, so those queries keep normal ranking. | issue 591 |
| `test_all_letter_codes_need_uppercase_and_adjacency` | core | Digit-less codes such as AFB are found only uppercase right after the code phrase, so real codes are reachable without matching words like safe. | issue 591 |

## `tests/test_ingest_completion.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_mismatched_pairs_raise_before_any_write` | core | Without it chunk and vector lists of different lengths are silently truncated and a short document is stored as complete. | docs/ingest.md#identity-contract |
| `test_failed_batch_never_skips_and_retry_publishes_exact` | core | Without it a failed batch leaves residue that a retry skips, duplicates or publishes with wrong content. | docs/ingest.md#publication-contract |
| `test_fail_after_last_batch_before_completion_recovers` | core | Without it a crash after all batches but before the completion write leaves a document unverifiable or duplicated. | docs/ingest.md#publication-contract |
| `test_fail_after_completion_before_inventory_skips_without_reupsert` | core | Without it a crash between completion and inventory re-upserts or deletes already verified data. | docs/ingest.md#publication-contract |
| `test_stale_single_point_never_skips` | core | Without it one surviving matching point is treated as proof and the document is skipped incomplete. | docs/ingest.md#publication-contract |
| `test_zero_chunk_is_explicit_empty_never_complete` | core | Without it a document that yields no chunks is recorded complete and satisfies later skips. | docs/ingest.md#publication-contract |
| `test_completion_tied_to_target_generation` | core | Without it a completion for one target collection or source hash certifies another. | docs/ingest.md#publication-contract |
| `test_completion_collection_shares_corpus_policy_verbatim` | core | Without it control state is created with a different shard and replica policy than the data it gates and is lost with a node. | AGENTS.md#hard-boundaries |
| `test_refresh_failure_leaves_no_stale_completion` | core | Without it a mid-refresh crash leaves the old marker over partial new data and a retry skips. | docs/ingest.md#publication-contract |
| `test_unscoped_or_malformed_completions_never_gate_a_skip` | core | Without it schema-less or sourceless markers satisfy a skip and legacy state is never re-ingested. | docs/ingest.md#publication-contract |
| `test_delete_completion_real_error_propagates` | core | Without it a failed marker invalidation is swallowed and stale and new markers coexist. | docs/ingest.md#publication-contract |
| `test_delete_completion_404_race_is_tolerated` | core | Without it a collection dropped between check and delete fails documents that could publish. | docs/ingest.md#publication-contract |
| `test_product_override_rerun_reingests_end_to_end` | core | Without it a changed --product override is reported as a verified skip and stale labels stay served. | docs/ingest.md#identity-contract |
| `test_concurrent_run_lock_rejects_second_writer` | core | Without it two ingest runs write one progress file and target concurrently. | docs/ingest.md#publication-contract |
| `test_stale_inventory_never_causes_a_false_skip` | core | Without it an inventory that outlived wiped or legacy storage reports a successful skip over missing data. | docs/ingest.md#publication-contract |
| `test_no_completion_collection_means_no_marker_and_not_complete` | core | Without it the first ingest into a fresh deployment fails on the missing completion collection instead of treating every document as not yet complete. | docs/ingest.md#publication-contract |

## `tests/test_ingest_identity.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_label_normalization_and_revision_key_identity` | core | Without it revision keys change across case, whitespace or missing labels and identity collides or splits. | docs/ingest.md#identity-contract |
| `test_plan_duplicates_elects_a_deterministic_winner` | core | Without it byte-identical copies elect a different winner between runs or lose unique files. | docs/ingest.md#identity-contract |
| `test_find_collisions_flags_only_same_doc_id_with_distinct_sha` | core | Without it distinct editions sharing a doc_id go undetected or benign duplicates abort planning. | docs/ingest.md#identity-contract |
| `test_collision_report_names_inputs_not_layout_or_text` | core | Without it the collision error leaks the corpus mount layout or lacks the remediation flags. | docs/ingest.md#identity-contract |
| `test_prescan_doc_ids_never_raise` | core | Without it an unreadable or missing file crashes planning instead of mapping to no doc_id. | docs/ingest.md#identity-contract |
| `test_filename_collision_aborts_before_any_work` | core | Without it colliding filenames or filename form numbers start work before aborting. | docs/ingest.md#identity-contract |
| `test_text_derived_collision_aborts` | core | Without it two files whose text carries one form number collide undetected. | docs/ingest.md#identity-contract |
| `test_duplicate_copies_ingest_winner_once` | core | Without it duplicate copies are ingested twice or the winner changes on rerun. | docs/ingest.md#identity-contract |
| `test_unreadable_file_neither_causes_nor_hides_collision` | core | Without it a corrupt twin aborts a healthy corpus or masks a real collision. | docs/ingest.md#identity-contract |
| `test_nondry_collision_writes_nothing` | core | Without it a real run with a collision upserts or deletes before aborting. | docs/ingest.md#identity-contract |

## `tests/test_ingest_publish.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_publication_identity_addresses_distinct_generations` | core | Without it a changed corpus, rules or model revision could address the already-serving staging name and be built in place under live readers. | docs/ingest.md#publication-contract |
| `test_first_publish_creates_alias_and_clean_rerun_is_noop` | core | Without it a first publish may not create a servable alias, or an identical rerun may flip the alias or allocate a needless generation. | docs/ingest.md#publication-contract |
| `test_refresh_publishes_new_generation_keeps_old_and_rolls_back` | core | Without it a refresh could overwrite the previous generation, lose its contract, or leave the operator unable to roll back to the kept physical. | docs/ingest.md#publication-contract |
| `test_incomplete_staging_metadata_is_repaired_or_refused_never_published` | core | Without it staging interrupted mid-metadata-transfer could be published with no contract, or an untransferable manifest could be silently reused. | docs/ingest.md#publication-contract |
| `test_staging_invisible_until_swap` | core | Without it uncommitted staging points become visible to readers of the alias before verification and cutover. | docs/ingest.md#publication-contract |
| `test_swap_failure_preserves_live_then_recovers` | core | Without it a failed alias swap could leave readers on a partial generation or block the next ordinary publish. | docs/ingest.md#publication-contract |
| `test_swap_rejection_raises_without_applying` | core | Without it a rejected alias operation is treated as success and the operator believes the new generation is live. | docs/ingest.md#publication-contract |
| `test_legacy_migration_preserves_then_clears` | core | Without it migrating a pre-alias physical collection could delete the only copy before a verified retained copy exists, or publish stale rules. | docs/ingest.md#publication-contract |
| `test_publish_refusals_precede_any_allocation` | core | Without it --limit, an empty corpus or a revision collision could allocate staging or create an alias before refusing. | docs/ingest.md#publication-contract |
| `test_verify_failure_blocks_swap` | core | Without it an unverified staging generation is swapped in and readers see incomplete documents. | docs/ingest.md#publication-contract |
| `test_verify_all_complete_matrix` | core | Without it a missing, stale, errored, ghost, unstored or revision-less document could pass the publication gate unnoticed. | docs/ingest.md#publication-contract |
| `test_verify_all_complete_refuses_unreadable_manifest_on_populated_staging` | core | Without it a populated generation with an absent or corrupt representation manifest is certified for serving. | docs/ingest.md#publication-contract |
| `test_verify_all_complete_refuses_a_non_committed_contract` | core | Without it a generation whose re-embed was never committed (pending or mis-cased state) is published as complete. | docs/ingest.md#publication-contract |
| `test_verify_all_complete_refuses_representation_drift` | core | Without it vectors built under a different rules, model or sparse contract are published and silently mix with the live contract. | docs/ingest.md#publication-contract |
| `test_verify_all_complete_ignores_record_only_prefix_drift` | core | Without it a query-prefix-only change, which touches no stored vector, blocks every publication. | docs/ingest.md#publication-contract |
| `test_dangling_alias_publishes_fresh` | core | Without it an alias pointing at a missing collection wedges publication instead of publishing a fresh generation. | docs/ingest.md#publication-contract |
| `test_publish_force_same_contract_repairs_distinct_generation` | core | Without it a forced same-contract rebuild mutates the serving generation in place and loses its points or manifest for rollback. | docs/ingest.md#publication-contract |
| `test_forced_repair_never_touches_live_during_build` | core | Without it a repair build can mutate the serving physical or leave readers on an unvalidated generation during the build. | tests/hazards/critical.json |
| `test_interrupted_same_contract_repair_resumes_recorded_build` | core | Without it a crash mid-repair allocates a second repair generation or mutates live instead of resuming the recorded build. | docs/ingest.md#publication-contract |
| `test_subsequent_ordinary_run_recognizes_repair_steady_state` | core | Without it the next ordinary run after a repair rebuilds, allocates, or silently treats a binding-less generation as legacy. | docs/ingest.md#publication-contract |
| `test_q418_r1_publication_receipt_not_accumulated_across_clones` | core | Without it publication receipts pile up in cloned control collections and a stale receipt can certify the wrong generation. | docs/ingest.md#publication-contract |
| `test_revision_only_change_fails_unforced_and_migrates_when_forced` | core | Without it a model-revision change either mutates live in place or can be reconverged without an explicit re-embed into a distinct generation. | docs/ingest.md#publication-contract |
| `test_publish_unusable_manifest_prevents_alias_cutover` | core | Without it a dropped or pending staging manifest at the gate still allows the alias cutover. | docs/ingest.md#publication-contract |
| `test_steady_state_reverification_refuses_unusable_live_manifest` | core | Without it an ordinary rerun accepts a live generation whose manifest is gone or pending, and a forced repair mutates it. | docs/ingest.md#publication-contract |
| `test_partial_walk_refuses_and_preserves_unretired_doc` | core | Without it a missing mount or file silently retires documents from the live generation. | docs/ingest.md#publication-contract |
| `test_retire_refusals_mutate_nothing` | core | Without it a typo or a retirement contradicting the walked corpus mutates storage, aliases, inventory or the sidecar before refusing. | docs/ingest.md#publication-contract |
| `test_unmarked_legacy_residue_blocks_cutover_without_deletion` | core | Without it unattributable legacy points are published as served content or deleted by the audit. | tests/hazards/critical.json |
| `test_concurrent_publish_same_alias_serializes` | core | Without it two publishers for one alias interleave their builds and corrupt the target. | docs/ingest.md#publication-contract |
| `test_overtaken_publisher_refuses_stale_cutover` | core | Without it a publisher overtaken by another writer switches the alias back to its older candidate. | docs/ingest.md#publication-contract |
| `test_revisit_retained_generation_never_mutated` | core | Without it republishing an older input set reuses and mutates a retained generation that rollback depends on. | docs/ingest.md#publication-contract |
| `test_plan_retire_deletes_takes_legacy_only_for_whole_document_retirement` | core | Without it a named-revision retirement deletes approved legacy points, or a whole-document retirement leaves them behind. | docs/ingest.md#publication-contract |
| `test_whole_doc_retirement_removes_named_and_legacy_history_keeping_rollback` | core | Without it a retired document survives in the new generation through legacy points or its rollback copy is lost. | docs/ingest.md#publication-contract |
| `test_partial_retire_leaves_legacy_with_named_path` | core | Without it a named-only retirement beside legacy history either deletes legacy points or dead-ends the operator without the whole-document path. | docs/ingest.md#publication-contract |
| `test_audit_names_gap_for_unexpected_remnants` | core | Without it residue left after retirement produces a refusal with no actionable operator path. | docs/ingest.md#publication-contract |
| `test_retire_plan_validated_under_target_lock` | core | Without it a retirement approval revoked before the lock is taken still publishes from a stale plan. | docs/ingest.md#publication-contract |
| `test_walked_doc_legacy_history_refuses_without_content_verification` | core | Without it sourceless points sharing a walked doc_id are accepted as coverage without approval, exact ids, rules, count and digests. | docs/ingest.md#publication-contract |
| `test_walked_doc_approved_legacy_history_publishes` | core | Without it approved, digest-verified pre-361B history can no longer be carried forward and every upgrade forces a mass re-ingest. | docs/ingest.md#publication-contract |
| `test_publish_migration_with_explicit_retire_single_run` | core | Without it a representation migration plus an approved removal cannot commit and swap in one run, or applies the removal after the gating audit. | docs/ingest.md#publication-contract |
| `test_publish_migration_retire_unapproved_stray_refuses` | core | Without it a stray revision under a retired document survives the removal and is published. | docs/ingest.md#publication-contract |
| `test_q417_l1_legacy_retirement_survives_cleanup_and_ordinary_runs` | core | Without it retiring a legacy document breaks the next ordinary runs, or those runs rewrite the corpus, markers or allocate generations. | docs/ingest.md#publication-contract |
| `test_q417_l1_legacy_retirement_crash_before_cutover_resumes_safely` | core | Without it a crash before cutover leaves live changed or the retirement committed, and retry cannot succeed. | docs/ingest.md#publication-contract |
| `test_q417_l1_legacy_retirement_crash_after_cutover_recovers_retire_plan` | core | Without it a crash after cutover loses the retirement disposition or reallocates a generation on recovery. | docs/ingest.md#publication-contract |
| `test_q417_l1_unexpected_missing_retained_legacy_fails_control` | core | Without it an approved, unretired legacy document whose chunks vanished still passes the cutover gate. | docs/ingest.md#publication-contract |
| `test_retire_revision_a_while_retaining_and_walking_revision_b_succeeds` | core | Without it retiring one revision deletes its walked or legacy sibling, or leaves the retired revision's markers behind. | docs/ingest.md#publication-contract |
| `test_publication_resolution_refusal_writes_nothing` | core | Without it a refusal resolving sidecar, predecessor or retirement identity writes storage, aliases, inventory or the sidecar first. | docs/ingest.md#publication-contract |
| `test_retire_revision_a_with_deleted_legacy_sibling_fails_verification` | core | Without it deleting a retained legacy sibling during retirement still passes final verification. | docs/ingest.md#publication-contract |
| `test_retire_retry_recovers_its_plan_and_refuses_a_changed_request` | core | Without it a retry after crash loses the authorized plan, or a changed request inherits another transaction's authorization. | docs/ingest.md#publication-contract |
| `test_conflict_preflight_same_bytes_changed_label_replaces_old_revision` | core | Without it identical bytes under a new label cannot replace the old revision, or conflict checks use stale historical labels. | docs/ingest.md#publication-contract |
| `test_stale_completion_markers_revision_scoped_exclusion` | core | Without it retiring one revision excuses stale markers of its retained siblings. | docs/ingest.md#publication-contract |
| `test_forced_build_retry_keeps_verified_document_checkpoint` | core | Without it a retried forced build re-embeds verified documents, or reuses a document whose stored content or markers were damaged. | docs/ingest.md#publication-contract |
| `test_interrupted_first_build_resumes_only_under_explicit_reingest` | core | Without it a plain retry publishes a partial first build, or --reingest redoes verified work. | docs/ingest.md#publication-contract |
| `test_forced_retry_after_all_checkpoints_does_no_embedding` | core | Without it a crash around the manifest commit forces a full re-embed or publishes an uncommitted contract. | docs/ingest.md#publication-contract |
| `test_staging_distribution_gate_passes_only_the_selected_policy` | core | Without it the distribution gate blocks valid single-node or partial-policy deployments or ignores the selected policy. | AGENTS.md#hard-boundaries |
| `test_staging_distribution_gate_refuses_mismatched_or_unverifiable_layouts` | core | Without it a false-HA corpus, a mismatched control collection or an unknown topology is certified for cutover. | AGENTS.md#hard-boundaries |
| `test_staging_placement_gate_passes_healthy_and_single_node_profiles` | core | Without it a healthy 6/3/2 cluster or the explicit 1/1/1 profile cannot publish, or peer endpoint parsing breaks. | AGENTS.md#hard-boundaries |
| `test_staging_placement_gate_refuses_unverifiable_or_degraded_copies` | core | Without it cutover proceeds with degraded, duplicated, transferring or unobserved replicas and readers lose copies. | AGENTS.md#hard-boundaries |
| `test_bounded_publication_lifecycle_traces` | core | Without it an ordering of change, repair and retire with an interruption around cutover loses retained content or breaks the next ordinary runs. | docs/testing.md#evidence-design |
| `test_failed_post_cutover_revalidation_preserves_finalization` | core | Without it a failed post-cutover check consumes the recovery sidecar or commits retirement before certification. | docs/ingest.md#publication-contract |
| `test_corrupt_publish_record_refuses_without_rewriting` | core | Without it a corrupt or widened recorded retirement plan is guessed at or rewritten, authorizing deletions nobody approved. | docs/ingest.md#publication-contract |
| `test_valid_publish_record_preserves_supported_optional_fields` | core | Without it supported sidecar versions lose their retirement plan or requests across resume. | docs/ingest.md#publication-contract |
| `test_corrupt_retirement_retry_preserves_stored_state` | core | Without it a corrupt authorization in the sidecar mutates storage or inventory instead of refusing, and later runs cannot recover. | docs/ingest.md#publication-contract |
| `test_build_uuid_precedes_writes_and_sealed_retry_is_read_only` | core | Without it a build allocates data before its identity is recorded, or a sealed retry mutates stored points. | docs/ingest.md#publication-contract |
| `test_sealed_retry_refuses_inconsistent_build_without_mutation` | core | Without it a sealed build with a mismatched version, id, predecessor, receipt or alias is retried and mutates storage. | docs/ingest.md#publication-contract |
| `test_publication_refuses_a_generation_targeted_by_name` | core | Without it a QDRANT_COLLECTION naming a physical or build alias migrates and deletes the collection its logical alias still serves. | docs/ingest.md#publication-contract |
| `test_completed_legacy_backfill_never_creates_build_identity` | core | Without it a legacy completed generation is retroactively given a build identity, or a failed backfill mutates it. | docs/ingest.md#publication-contract |
| `test_new_allocation_never_reuses_orphan_controls` | core | Without it a new build reuses orphan control collections and inherits stale markers. | docs/ingest.md#publication-contract |
| `test_sealed_content_damage_refuses_retry_without_mutation` | core | Without it a sealed build with lost documents or damaged vectors, controls or seal is retried and published. | docs/ingest.md#publication-contract |
| `test_retained_seal_detects_stored_content_loss` | core | Without it content lost from a retained generation is not detected and rollback publishes damaged data. | docs/ingest.md#publication-contract |
| `test_content_seal_canonical_projection_and_pagination` | core | Without it the content seal omits vectors, payload or binding, or changes with traversal order or page size. | docs/ingest.md#publication-contract |
| `test_content_seal_refuses_incomplete_scan` | core | Without it a seal is captured from a projection-less, non-finite, duplicated or non-terminating scan. | docs/ingest.md#publication-contract |
| `test_completed_binding_without_seal_is_not_retroactively_certified` | core | Without it a pre-seal generation is certified as sealed without evidence. | docs/ingest.md#publication-contract |
| `test_publication_verifies_receipt_readback_before_cutover` | core | Without it a lost or corrupted receipt write is believed and the alias cuts over without a verifiable seal. | docs/ingest.md#publication-contract |
| `test_unfinished_schema_one_sealed_build_requires_its_old_release` | core | Without it a new writer mutates an unfinished old-format build instead of requiring the release that created it. | docs/ingest.md#publication-contract |
| `test_clone_copies_any_source_topology_exactly_through_the_api` | core | Without it staging is cloned by node-local snapshot or loses points on distributed or odd-topology sources. | AGENTS.md#hard-boundaries |
| `test_clone_creates_staging_with_the_selected_distribution_policy` | core | Without it staging is created with server-default shards and replicas instead of the selected policy. | AGENTS.md#hard-boundaries |
| `test_clone_refuses_before_any_write` | core | Without it an unreadable source or existing target is partially written before the refusal. | docs/ingest.md#publication-contract |
| `test_clone_refuses_a_partial_copy` | core | Without it a clone that lost a point becomes a publish base. | docs/ingest.md#publication-contract |
| `test_unknown_topology_legacy_migration_never_deletes_the_squatter` | core | Without it a legacy collection with unknown topology is snapshotted or deleted, losing the only serving copy. | docs/ingest.md#publication-contract |
| `test_legacy_migration_retains_distributed_squatter_then_publishes` | core | Without it a distributed legacy collection is deleted before an exact retained copy exists. | docs/ingest.md#publication-contract |
| `test_legacy_migration_refuses_a_mismatched_retained_copy` | core | Without it an interrupted or foreign retained copy is trusted and the squatter deleted. | docs/ingest.md#publication-contract |
| `test_publication_checks_are_read_only_by_type` | infra | Without it publication resolution and verification helpers can acquire write capability and mutate storage during read-only checks. | docs/architecture.md |
| `test_sealed_build_cannot_serve_until_published` | core | Without it a sealed but unpublished build can be served, exposing a half-built corpus to search and answers. | docs/ingest.md#publication-contract |

## `tests/test_ingest_repair.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_repair_retry_then_ordinary_completion` | core | Without it duplicate legacy residue repair loses its rollback backup, deletes the wrong points or leaves the next ordinary skip unverified. | docs/ingest.md#publication-contract |
| `test_changed_inputs_refuse_before_deletion` | core | Without it a tampered backup, plan, inventory, live vector, replacement, served staging or new residue still deletes points. | docs/ingest.md#publication-contract |
| `test_partial_delete_ack_lost_retry` | core | Without it a repair whose delete acknowledgement was lost cannot be retried safely. | docs/ingest.md#publication-contract |
| `test_writer_lock_and_bound_refuse` | core | Without it repair runs under a held writer lock or beyond its approved bound. | docs/ingest.md#publication-contract |
| `test_manifest_envelope_refuses_before_deletion` | core | Without it repair proceeds against a manifest with a wrong target, digest or committed state. | docs/ingest.md#publication-contract |

## `tests/test_ingest_revisions.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_two_vendors_coexist_either_order` | core | Without it ingesting one revision deletes another that merely shares a printed doc_id, in one or the other order. | docs/ingest.md#identity-contract |
| `test_update_one_revision_leaves_other_untouched` | core | Without it refreshing one revision alters the points or completion of its sibling. | docs/ingest.md#identity-contract |
| `test_refresh_without_lineage_adds_and_leaves_committed` | core | Without it a lineage-less refresh deletes a committed sibling revision. | docs/ingest.md#identity-contract |
| `test_crash_residue_without_completion_is_swept` | core | Without it crash residue with no marker survives as an apparent revision. | docs/ingest.md#identity-contract |
| `test_legacy_sole_history_migrates_on_refresh` | core | Without it pre-361B sourceless history is never migrated or is deleted when other history exists. | docs/ingest.md#identity-contract |
| `test_legacy_unchanged_doc_skips_without_reingest` | core | Without it an upgrade mass re-ingests every unchanged legacy document. | docs/ingest.md#identity-contract |
| `test_sourceless_residue_amid_named_raises` | core | Without it unattributable legacy residue beside a live revision is deleted by doc_id and takes the live revision with it. | docs/ingest.md#identity-contract |
| `test_stored_doc_revisions_and_delete_by_revision` | core | Without it revision deletion takes legacy points or reports the wrong stored revisions. | docs/ingest.md#identity-contract |
| `test_plan_refresh_deletes_matrix` | core | Without it a refresh plans deletes of the wrong revisions or sweeps legacy unnecessarily. | docs/ingest.md#identity-contract |
| `test_triple_variant_refresh_retires_old_marker` | core | Without it a changed vendor, product or version override leaves two markers certifying one revision or duplicates points. | docs/ingest.md#identity-contract |
| `test_marker_listing_paginates_past_old_cap` | core | Without it more than one page of markers under a doc_id are dropped from reads and invalidations. | docs/ingest.md#identity-contract |
| `test_coexisting_revisions_filter_by_exact_product_version` | core | Without it exact product and version filters return or miss the wrong coexisting edition and citations break. | docs/ingest.md#identity-contract |
| `test_revision_locks_serialize_same_revision_only` | core | Without it same-revision writers race, or coexisting revisions block each other. | docs/ingest.md#identity-contract |

## `tests/test_ingest_robustness.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_inventory_is_crash_safe_latest_wins_and_typed` | core | Without it a torn or corrupt inventory line crashes resume, an older record wins, or error types are lost. | docs/ingest.md#publication-contract |
| `test_batch_size_stays_in_bounds` | infra | Without it out-of-range BATCH_SIZE values are accepted and cause pathological upserts. | docs/ingest.md#publication-contract |
| `test_upsert_is_batched` | core | Without it a document is upserted in one giant call instead of settings.batch_size batches. | docs/ingest.md#publication-contract |
| `test_payload_indexes_precede_load_and_cover_every_filter_field` | core | Without it pre-existing collections miss indexes or a retrieval filter field is unindexed and degrades to a scan. | docs/ingest.md#publication-contract |
| `test_models_ipc_pickle_round_trip` | core | Without it a domain model cannot cross the ProcessPoolExecutor boundary and workers fail at runtime. | docs/testing.md#tests-must-lock-the-claimed-path |

## `tests/test_ingest_tracing.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_ingest_roots_and_service_name` | infra | Without it ingest spans are exported under the agent service name and merge in one Jaeger. | docs/architecture.md |
| `test_ingest_error_marks_root_span` | infra | Without it a failed ingest run does not mark its root span as an error or flush tracing. | docs/architecture.md |
| `test_ingest_run_is_tracing_noop_when_disabled` | infra | Without it an ingest without an OTLP endpoint exports spans or breaks. | docs/architecture.md |
| `test_root_span_counters_and_status_on_failed_and_no_task_runs` | infra | Without it run counters and error status are missing on failed or no-task ingest runs and operators lose run outcome signals. | docs/architecture.md |

## `tests/test_integration_sim.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_fresh_binding_then_native_publication_and_next_ingest_on_real_server` | core | Without it a fresh production binding could publish a wrong or duplicated collection on real Qdrant, clobber the preserved legacy collection, or re-write points on the next ordinary ingest. | docs/testing.md#evidence-design |
| `test_ingest_real_server_and_resume` | core | Without it ingest into a real server could stop upserting every document, or a lost inventory would redo work instead of skipping via the Qdrant sha check. | docs/ingest.md |
| `test_alias_publish_rekeys_manifest_across_clone` | core | Without it a second alias publish could silently fail to re-key the manifest on clone and read inherited data as legacy, breaking the next publish. | #391 |
| `test_publication_by_physical_name_is_refused_on_real_server` | core | Without it an operator could publish directly into a physical generation name and bypass the alias cutover protecting serving data. | #391 |
| `test_alias_publish_revision_migration_keeps_old_generation` | core | Without it a revision migration could mutate or lose the old generation instead of leaving it intact for rollback while the new one is verified. | #391 |
| `test_forced_repair_publishes_distinct_generation_on_real_server` | core | Without it a forced --reingest could mutate the serving generation in place or fail to cut over to a suffixed generation, and the next ordinary run could allocate stray collections. | #391 |
| `test_migration_scope_proof_blocks_unmarked_point_on_real_server` | core | Without it a migration could commit while an unmarked old-rules point remains, silently mixing rule generations in the served corpus. | #391 |
| `test_legacy_verification_refuses_corrupt_points_on_real_server` | core | Without it corrupt legacy points could be adopted into a published generation instead of failing content/digest verification. | #391 |
| `test_search_end_to_end_deterministic` | core | Without it the ingest to /v1/search path could break unnoticed: identifier scoping, doc_id filter, plain-doc stem retrieval, determinism or health. | docs/testing.md#simulation-tier |
| `test_answer_deterministic` | core | Without it /v1/answer could return unvalidated citations, a missing script, or non-deterministic answers over real retrieval. | docs/agent.md#http-model-contract |
| `test_ops_cli_real_agent_round_trip_and_recovery` | core | Without it the shipped ops CLI could fail against real FastAPI output, mishandle error envelopes, or leave the upstream stream open after Ctrl-C cancellation. | docs/testing.md#simulation-tier |
| `test_eval_retrieval_on_synthetic_corpus` | core | Without it the retrieval eval harness could mis-score the real pipeline, report non-servable representations as servable, or regress baseline checking. | docs/eval.md |
| `test_vllm_shaped_embed_variant` | core | Without it the production embed path (dense over HTTP, local BM25 sparse) could break while hash-mode tests stay green, and health could misreport representation. | AGENTS.md hard boundary: embeddings and models |
| `test_361_inplace_snapshot_restore_keeps_coexisting_revisions` | core | Without it an in-place snapshot restore could drop or overwrite coexisting revisions, losing retained published data. | docs/ingest.md |
| `test_revision_scoped_retirement_on_real_server` | core | Without it retiring a document or revision could delete points of other revisions or leave retired content served. | #391 |
| `test_forced_migration_retry_retains_verified_document_on_real_qdrant` | core | Without it a retried forced migration could lose or re-embed an already verified document. | #391 |
| `test_bounded_publication_trace_on_real_server` | core | Without it orderings of change, repair and retire with an interruption could diverge from the independent literal oracle on real clone/alias storage. | docs/testing.md#evidence-design |
| `test_post_cutover_missing_control_preserves_retry_on_real_server` | core | Without it a missing live control after cutover could consume an interrupted retirement's recovery state so the retry cannot complete. | docs/testing.md#evidence-design |
| `test_corrupt_retirement_record_refuses_before_real_deletion` | core | Without it a corrupt retirement record could lead to deletion of live data instead of refusal before any deletion. | docs/testing.md#evidence-design |
| `test_inflight_and_warm_http_readers_keep_verified_generation` | core | Without it in-flight or cached HTTP readers could see a half-published successor, or fail to recover after control restoration, during change, repair or retire. | docs/testing.md#evidence-design |
| `test_interrupted_first_build_resumes_on_real_qdrant_only_under_reingest` | core | Without it an interrupted first build could be resumed or served without --reingest, publishing partial content. | docs/testing.md#evidence-design |
| `test_publisher_process_death_resumes_same_build` | core | Without it a publisher killed at any boundary could leave a build that retry cannot resume to the recorded build with exact membership, or fail to release its lock. | docs/testing.md#evidence-design |
| `test_full_build_identity_survives_real_sealed_retry` | core | Without it a sealed retry could allocate a new build identity, breaking resume and exact-membership guarantees. | docs/testing.md#evidence-design |
| `test_retained_content_seal_detects_real_stored_content_loss` | core | Without it lost documents or corrupted dense or sparse vectors in the retained generation would go undetected and be served or rolled back to. | docs/testing.md#evidence-design |

## `tests/test_kube_forward.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_forward_restarts_after_exit_or_hang_and_recovers_then_stops_cleanly` | infra | Without it a dead or hung kubectl port-forward leaves the Qdrant tunnel down or leaks an orphan child after stop. | AGENTS.md hard boundaries (Qdrant ClusterIP-only, loopback forward); #762 |
| `test_never_ready_forward_keeps_bounded_backoff_until_stopped` | noise-kept | Without it a never-ready forward could retry in a tight loop or fail to stop; the 0.2/0.4/0.4 backoff is the stated bounded-restart contract. | AGENTS.md hard boundaries (Qdrant ClusterIP-only, loopback forward); #762 |
| `test_occupied_local_port_is_refused_before_spawning` | infra | Without it the forward could silently bind over or shadow another listener on the Qdrant port and send traffic to the wrong service. | AGENTS.md hard boundaries (Qdrant ClusterIP-only, loopback forward); #762 |
| `test_invalid_inputs_refuse_before_spawning` | infra | Without it an unvalidated namespace, service or privileged port reaches kubectl argv instead of failing closed. | AGENTS.md hard boundaries (Qdrant ClusterIP-only, loopback forward); #762 |
| `test_relay_replaces_an_idle_stream_so_reuse_never_hits_a_dead_one` | noise-kept | Without it pooled Qdrant clients reuse a server-closed idle stream and hit RemoteProtocolError (#762). | #762 |
| `test_relay_never_closes_a_connection_awaiting_its_response` | noise-kept | Without it the idle-close timer can cut a slow in-flight Qdrant response and corrupt a request. | #762 |
| `test_restart_on_the_same_port_right_after_relay_closed_connections` | noise-kept | Without it TIME_WAIT from relay-closed connections blocks a prompt forward restart on the same port. | #762 |
| `test_client_connection_survives_the_server_idle_close_on_a_fresh_stream` | noise-kept | Without it an idle client connection is dropped or the kubectl child is needlessly restarted when only its stream idles out. | #762 |

## `tests/test_license_inventory.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_expression_class_is_conservative` | infra | Without it copyleft, proprietary or unparseable licenses are classified as permissive and pass the release check. | docs/licensing.md |
| `test_recorded_election_decides_a_dual_license` | infra | Without it a dual-licensed dependency is classified without the owner's recorded election. | docs/licensing.md |
| `test_repository_record_reconciles_with_the_real_pins` | infra | Without it the license inventory drifts from the real dependency pins and open decisions disappear. | docs/licensing.md |
| `test_every_locked_runtime_wheel_has_a_record_with_its_exact_identity` | infra | Without it a locked wheel ships without a license record or with the wrong version or hash. | docs/licensing.md |
| `test_pymupdf_cannot_be_recorded_as_a_declared_permissive_dependency` | infra | Without it the AGPL/commercial PyMuPDF is recorded as declared permissive without an owner decision. | docs/licensing.md |
| `test_new_locked_dependency_without_a_record_is_flagged` | infra | Without it a new or removed dependency goes unrecorded and ghost records stay. | docs/licensing.md |
| `test_version_or_wheel_bump_without_review_is_flagged` | infra | Without it a version, wheel or hash bump ships without license re-review. | docs/licensing.md |
| `test_class_claim_must_follow_the_recorded_expression` | infra | Without it a record claims a class that contradicts its SPDX expression. | docs/licensing.md |
| `test_approved_status_needs_a_protected_reference` | infra | Without it a restricted license is marked approved with no owner approval reference. | docs/licensing.md |
| `test_weak_copyleft_requires_a_recorded_obligation` | infra | Without it weak-copyleft dependencies ship without their recorded obligation. | docs/licensing.md |
| `test_wheel_without_license_text_cannot_be_declared` | infra | Without it a wheel with no license text is accepted as declared. | docs/licensing.md |
| `test_release_mode_blocks_on_unresolved_decisions_and_missing_approval` | infra | Without it a release proceeds with open license decisions or no owner approval. | docs/licensing.md |
| `test_release_approval_is_bound_to_the_exact_inventory_and_goes_stale` | infra | Without it an old owner approval stays valid after dependency or license records change. | docs/licensing.md |
| `test_unrecorded_image_pin_is_flagged_then_passes_after_the_record_is_added` | infra | Without it a new container image pin ships without a license record. | docs/licensing.md |
| `test_changed_image_digest_requires_re_review` | infra | Without it an image digest change ships without license re-review. | docs/licensing.md |
| `test_unrecorded_vendored_asset_and_second_chart_are_flagged` | infra | Without it an unrecorded vendored web asset or chart archive ships. | docs/licensing.md |
| `test_missing_or_altered_notice_file_is_flagged` | infra | Without it a vendored license or notice file can be changed or removed unnoticed. | docs/licensing.md |
| `test_pin_file_changes_require_re_review` | infra | Without it changes to model weights or tool pins ship without license re-review. | docs/licensing.md |
| `test_project_license_change_in_pyproject_is_flagged` | infra | Without it the project's own license changes without the record being updated. | docs/licensing.md |
| `test_invalid_inventory_fails_with_a_fixed_message` | infra | Without it a corrupt inventory crashes with a raw exception or leaks details. | docs/licensing.md |
| `test_license_change_in_artifact_metadata_is_flagged_and_matching_metadata_passes` | infra | Without it a license change in an actual wheel is not detected against the record. | docs/licensing.md |
| `test_wheel_bytes_with_a_different_hash_are_not_accepted_as_the_locked_wheel` | infra | Without it a tampered wheel with the right name is used as license evidence. | docs/licensing.md |
| `test_declared_license_precedence_and_seeding` | infra | Without it declared license metadata is read in the wrong precedence and records are seeded with wrong SPDX ids. | docs/licensing.md |
| `test_notices_carry_real_texts_and_open_decisions_and_refuse_a_missing_member` | infra | Without it the shipped notices omit license texts or open decisions or build with a tampered member. | docs/licensing.md |

## `tests/test_load_tier.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_search_load_has_zero_errors_and_full_timings` | core | Without it /v1/search could produce errors or drop Server-Timing under concurrent load. | docs/testing.md#load-tier |
| `test_answer_load_has_zero_errors_and_ttft` | core | Without it /v1/answer could error, lose Server-Timing, or stop reporting ttft under concurrent load. | docs/testing.md#load-tier |
| `test_answer_streams_keep_integrity_under_concurrency` | core | Without it concurrent SSE streams could truncate, duplicate the final event, emit an error, or lose citations. | docs/testing.md#load-tier |
| `test_citation_parity_stream_vs_search_vs_json` | core | Without it concurrent requests could cross-talk so stream, JSON and search citations disagree. | docs/testing.md#load-tier |
| `test_error_shapes_hold_under_load_without_leaks` | core | Without it error envelopes under load could change shape or leak internals to clients. | docs/testing.md#load-tier |
| `test_determinism_after_load` | core | Without it load could corrupt state so identical searches return different results. | docs/testing.md#load-tier |
| `test_aborted_streams_surface_error_without_final` | core | Without it an upstream abort could emit a false final or no error event instead of a clean aborted stream. | docs/testing.md#load-tier |
| `test_aborted_streams_leave_truncation_alerts` | core | Without it aborted streams would not leave exactly one stream_truncated alert, blinding operators, or alerts could carry response text. | docs/testing.md#load-tier |
| `test_ttft_floor_holds_under_paced_mock` | noise-kept | Without it the reported ttft could precede the model first byte, making latency telemetry wrong. | docs/testing.md#load-tier |
| `test_endpoint_outcomes_over_real_http` | core | Without it client disconnects and following completions could be miscounted in rag_requests_total for answer, chat and console. | #375 |

## `tests/test_loadtest.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_parse_server_timing` | infra | Server-Timing parsing misreads quoted or malformed durations, so stages go missing or the missing-header check fails to fire. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_export_to_baseline_preserves_existing_keys` | infra | Exporting an L3 baseline wipes ingest/qdrant/env keys already recorded in the file. | docs/testing.md |
| `test_query_gpu_name_success_and_fail` | infra | The GPU name used in the L3 env gate is misread or crashes when nvidia-smi is absent. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_export_to_baseline_refuses_ci_benchmark_file` | gate-control | GPU numbers are merged into the GitHub-runner CI bench baseline. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_export_to_baseline_refuses_faulty_run` | gate-control | A baseline is recorded from a run with request errors or missing Server-Timing. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_run_load_tracks_missing_timings_on_header_absence` | gate-control | Responses without Server-Timing are not counted so the gate cannot detect lost headers. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_task_load_defaults_and_literal_url_reach_actual_measurement` | infra | The load Task drops, defaults or shell-evaluates AGENT_URL, measuring the wrong endpoint. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `test_export_to_baseline_creates_a_dedicated_file_recording_the_run_environment` | infra | Without it a new L3 baseline is written without the env (cpu, mode, GPU) that gate_verdict_l3 compares, so a later run is gated against an unknown machine. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |

## `tests/test_local_gateway_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_render_routes_legs_and_carries_master_key_and_overrides` | infra | Without it the rendered gateway config misroutes the reasoning, embed or rerank legs, drops reasoning_effort, or ignores URL overrides, so the local stack diverges from the real gateway. | docs/live-stack.md |
| `test_render_callbacks_and_content_logging` | infra | Without it the strict-finish guard goes missing or the bare otel callback exports message content and key hashes to traces. | issue #636 |
| `test_render_includes_the_gateway_hook_beside_its_configuration` | infra | Without it the gateway container starts without the guard, strict-finish or trace-privacy plugins it is configured to load. | issue #636 |
| `test_tokenize_targets_reasoning_origin` | infra | Without it the agent's tokenizer call hits the wrong origin or a doubled /v1 path and falls back to estimation. | docs/agent.md#http-model-contract |
| `test_render_generates_keys_when_unset` | infra | Without it the gateway starts with no master key or with committed sample keys. | AGENTS.md hard boundaries (secrets) |
| `test_env_file_handoff_written_mode_600` | infra | Without it per-leg keys are written world-readable or the master key leaks to the consumers' handoff file. | AGENTS.md hard boundaries (secrets) |
| `test_explicit_embedding_revision_survives_sourced_handoff` | core | Without it a hostile or empty embedding revision is mangled or executed when the handoff file is sourced, corrupting stored embedding identity. | AGENTS.md hard boundaries (models and revisions from their owner) |
| `test_gateway_revision_reaches_stack_ingest_and_serving` | core | Without it ingest and serving embed with different revisions than the gateway declares, breaking vector compatibility. | AGENTS.md hard boundaries (models and revisions from their owner) |
| `test_bad_key_charset_fails_closed` | infra | Without it keys with shell or YAML metacharacters are rendered into config and break or inject into the gateway. | AGENTS.md hard boundaries (secrets) |
| `test_bare_hostname_url_fails_closed` | infra | Without it a scheme-less backend URL is accepted and the gateway fails obscurely at request time. | docs/live-stack.md |
| `test_detailed_debug_only_when_explicitly_enabled` | infra | Without it the gateway logs full request bodies (manual text) by default. | issue #590 |
| `test_invalid_debug_value_fails_before_any_container` | infra | Without it a typo in GATEWAY_DEBUG silently picks a logging mode and starts a container. | issue #590 |
| `test_gateway_log_defaults_off_tmpfs_and_honors_override` | infra | Without it the gateway log lands on RAM-backed tmp and fills memory on the reference host. | issue #590 |
| `test_trace_export_keeps_only_allowlisted_attributes` | core | Without it message content, key hashes and exception text reach the trace backend. | issue #636 |
| `test_transport_probe_uses_gateway_image_network_and_actual_addresses_without_services` | infra | Without it the transport probe uses the wrong image, network or addresses, or leaves state and services behind. | docs/live-stack.md |
| `test_gateway_network_failure_refuses_despite_healthy_host_and_default_bridge_then_recovers` | infra | Without it a gateway-network outage is masked by host and default-bridge reachability and the next probe cannot recover. | docs/live-stack.md |
| `test_preexisting_network_survives_probe_failure_and_release` | infra | Without it the launcher removes a network it did not create. | docs/live-stack.md |
| `test_preexisting_container_or_owner_scope_is_never_adopted_by_native_launch` | infra | Without it the launcher adopts, stops or removes a foreign or earlier-run container. | docs/live-stack.md |
| `test_probe_then_normal_launch_reuses_exact_network_and_renders_probed_urls` | infra | Without it launch uses a different network or URLs than were probed, so the proof does not cover what runs. | docs/live-stack.md |
| `test_network_name_reuse_refuses_launch_and_preserves_replacement_during_cleanup` | infra | Without it a replacement network reusing the name is launched into or deleted during cleanup. | docs/live-stack.md |
| `test_owned_network_release_then_next_probe_uses_new_identity` | infra | Without it release is not idempotent and the next ordinary probe reuses a stale network identity. | docs/live-stack.md |
| `test_network_replacement_after_create_cannot_inherit_cleanup_ownership` | infra | Without it a network swapped after creation is later removed as if owned. | docs/live-stack.md |
| `test_missing_pinned_image_refuses_before_network_creation` | infra | Without it an unpulled gateway image is discovered after a network was already created and pulled from outside the pin. | AGENTS.md hard boundaries (pinned images, no runtime downloads) |
| `test_unprobed_address_or_unpinned_image_refuses_before_docker` | infra | Without it launch accepts addresses that were not probed or an unpinned image tag. | AGENTS.md hard boundaries (pinned images) |
| `test_selected_gateway_limits_bound_both_owned_daemons_without_swap` | infra | Without it the gateway and its database run uncapped with swap, thrashing the host. | docs/live-stack.md (local host resource guard) |
| `test_invalid_or_incomplete_gateway_limits_refuse_before_docker` | infra | Without it malformed memory or log limits start containers unbounded. | docs/live-stack.md (local host resource guard) |
| `test_gateway_release_ignores_invalid_startup_limits_and_allows_next_launch` | infra | Without it a bad limit setting blocks cleanup of owned containers and the next launch. | docs/live-stack.md |
| `test_saturated_gateway_log_preserves_docker_failure_reaps_and_next_launch_works` | infra | Without it a gateway that floods its log masks the real docker failure, fills disk, or leaves the capture process running. | docs/live-stack.md |
| `test_failed_gateway_log_destination_still_drains_without_exception_text` | infra | Without it an unwritable log destination blocks the container on a full pipe or prints exception text. | AGENTS.md hard boundaries (logs carry no secrets or manual text) |

## `tests/test_local_jaeger_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_dryrun_plan_pins_image_and_ports` | infra | Without it Jaeger could bind a non-loopback address or run an unpinned image. | AGENTS.md hard boundaries (pinned images, loopback) |
| `test_bad_bound_fails_closed_before_docker` | infra | Without it a malformed memory or trace bound reaches docker, including injected flags. | docs/live-stack.md (local host resource guard) |
| `test_bad_port_fails_closed` | infra | Without it an invalid Jaeger port is passed to docker. | docs/live-stack.md |
| `test_reuse_path_never_touches_docker` | infra | Without it a running Jaeger is recreated, dropping the operator's traces. | docs/live-stack.md |
| `test_existing_container_name_fails_closed` | infra | Without it the launcher clobbers or adopts a foreign container with the Jaeger name. | docs/live-stack.md |
| `test_non_jaeger_port_squatter_fails_closed` | infra | Without it traces are silently sent to a non-Jaeger listener on the OTLP port. | docs/live-stack.md |
| `test_started_container_carries_both_bounds` | infra | Without it the real start path runs Jaeger without the no-swap memory cap or trace-store bound. | issue #636 |

## `tests/test_local_stack_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_dryrun_plan_orders_stages` | infra | Without it the local stack could start the agent or ingest before the gateway and probe pass, diverging from the production topology order. | docs/live-stack.md |
| `test_dryrun_plan_reflects_operator_overrides` | infra | Without it operator overrides (probe budget, UI flag, trace timeout, port, corpus, rerank off) silently stop reaching the launched commands and ingest merges into the agent's Jaeger service. | docs/live-stack.md |
| `test_bad_probe_budget_fails_before_stack_work` | infra | Without it an out-of-range GATEWAY_PROBE_MAX_TOKENS would start containers before being rejected. | docs/live-stack.md |
| `test_persistent_progress_reaches_ingest_after_stack_cleanup_and_restart` | core | Without it a restarted local stack loses or clobbers retained ingest checkpoints so the next ordinary ingest redoes or corrupts work. | docs/ingest.md |
| `test_authenticated_stack_hands_writer_only_to_ingest_and_readonly_to_agent` | infra | Without it the serving agent or gateway could receive the Qdrant writer credential or print either key, breaking read-only serving credentials. | AGENTS.md hard boundaries (Qdrant credentials) |
| `test_qdrant_credential_enumerator_failure_refuses_before_service_children` | infra | Without it a failure to enumerate credential env would let services start with an unknown credential state and leak key values to stderr. | AGENTS.md hard boundaries (Qdrant credentials) |
| `test_unsafe_qdrant_role_inputs_refuse_before_service_children` | infra | Without it missing, equal or conflicting reader and writer keys would start the stack and serve with write credentials. | AGENTS.md hard boundaries (Qdrant credentials) |
| `test_bad_numeric_fails_closed` | infra | Without it a non-numeric port, dimension or timeout would reach docker or the agent instead of failing at the launcher. | docs/live-stack.md |
| `test_bare_hostname_fails_closed` | infra | Without it a scheme-less model or Qdrant URL would be accepted and fail obscurely deep in the stack. | docs/live-stack.md |
| `test_env_file_inside_repo_fails_closed` | infra | Without it the gateway key handoff file could be written inside the repo checkout and be committed. | AGENTS.md hard boundaries (secrets never committed) |
| `test_missing_corpus_dir_fails_closed` | infra | Without it a mistyped CORPUS_DIR silently ingests nothing and the stack reports success. | docs/live-stack.md |
| `test_invalid_rerank_flag_fails_before_launch` | infra | Without it a typo in RERANK_ENABLED silently picks a rerank mode instead of refusing. | docs/live-stack.md |
| `test_two_backend_live_path_preserves_flag_after_gateway_handoff` | infra | Without it a gateway handoff can flip rerank back on so the stack probes a third backend that was disabled. | docs/live-stack.md |
| `test_ingest_workers_follow_available_ram` | infra | Without it local ingest sizes workers beyond free RAM and thrashes the WSL host. | docs/live-stack.md (local host resource guard) |
| `test_ingest_workers_env_overrides_sizing` | infra | Without it operators cannot pin the ingest worker count and sizing ignores the explicit choice. | docs/live-stack.md (local host resource guard) |
| `test_invalid_ingest_workers_fails_closed` | infra | Without it a zero or non-numeric INGEST_WORKERS reaches ingest instead of being refused. | docs/live-stack.md (local host resource guard) |
| `test_ingest_is_forced_only_when_reingest_is_selected` | core | Without it ordinary local ingest silently re-embeds or replaces the collection, or an explicit reingest request is dropped. | AGENTS.md hard boundaries (no silent baseline changes) |
| `test_invalid_reingest_choice_fails_before_stack_work` | infra | Without it a loosely spelled LOCAL_INGEST_REINGEST is accepted and may trigger or skip a destructive reingest. | AGENTS.md hard boundaries (no silent baseline changes) |
| `test_reingest_without_corpus_fails_before_stack_work` | infra | Without it a reingest request with no corpus starts the stack and does nothing or wipes data. | AGENTS.md hard boundaries (no silent baseline changes) |
| `test_live_low_ram_refuses_before_any_model_or_ingest_call` | infra | Without it the stack starts ingest on a host without free RAM and thrashes the machine. | docs/live-stack.md (local host resource guard) |
| `test_live_pressure_refuses` | infra | Without it ingest starts under high memory or IO pressure and starves the host. | docs/live-stack.md (local host resource guard) |
| `test_live_pressure_just_below_threshold_is_admitted` | infra | Without it the pressure guard could refuse healthy hosts at the boundary and block local work. | docs/live-stack.md (local host resource guard) |
| `test_live_explicit_workers_are_still_admission_checked` | infra | Without it an explicit worker count bypasses the RAM admission check. | docs/live-stack.md (local host resource guard) |
| `test_live_force_start_skips_refusal_but_still_sizes_workers` | infra | Without it FORCE_START either cannot override admission or also drops worker sizing, overcommitting RAM. | docs/live-stack.md (local host resource guard) |
| `test_missing_host_files_skip_checks_with_notice` | infra | Without it hosts lacking meminfo or PSI files fail to start or skip the guard silently. | docs/live-stack.md (local host resource guard) |

## `tests/test_logs.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_json_and_plain_messages_become_one_payload_and_envelope_wins` | core | Without it log lines stop being single JSON objects, or an event field could overwrite the timestamp or level that log pipelines index on. | docs/agent.md#8-log-and-trace-contract |
| `test_invalid_level_fails_with_clear_message` | core | Without it a bad LOG_LEVEL would give an opaque startup error or silently choose a level. | docs/deploy.md#configuration-contract |
| `test_exception_goes_into_payload_one_line` | core | Without it exception bodies (which may hold manual text or secrets) could enter logs, or tracebacks would split into multiple lines. | AGENTS.md hard boundary: logs contain no secrets/manual text |
| `test_configure_logging_is_idempotent` | core | Without it repeated configuration would stack handlers and emit every log line several times. | docs/agent.md#8-log-and-trace-contract |
| `test_trace_ids_follow_the_active_span_and_caller_values_win` | core | Without it logs cannot be joined to traces, or tracing-off logs change shape, or a propagated caller trace id is overwritten. | issue #185 |

## `tests/test_manifest.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_get_qdrant_version_unreachable_is_unknown` | core | An unreachable or erroring Qdrant records a pinned version, forging comparability between run manifests. | docs/testing.md#evaluation (run manifests) |
| `test_get_collection_snapshot_id_with_fake` | core | The snapshot id in run manifests is not read from the Qdrant snapshot list. | docs/testing.md#evaluation (run manifests) |
| `test_write_run_manifest_appends_valid_json` | core | Run manifests are not written when Qdrant is unreachable, or lose metrics or run params. | docs/testing.md#evaluation (run manifests) |

## `tests/test_map_values.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_full_mapping_table` | infra | Without it operator environment keys silently map to wrong release values for images, models, tracing, route and storage. | docs/deploy.md#configuration-contract |
| `test_embed_base_url_derived_from_vllm` | infra | Without it the embedding URL is not derived from the gateway URL and the release points at the wrong endpoint. | docs/deploy.md#configuration-contract |
| `test_otel_mirror` | infra | Without it the OTLP endpoint off/default/custom spellings map to the wrong tracing state. | docs/deploy.md#configuration-contract |
| `test_otel_bad_value_fails_closed` | infra | Without it a malformed OTLP endpoint is deployed and telemetry export fails at runtime. | docs/deploy.md#configuration-contract |
| `test_otel_is_owned_by_the_mapper_not_stale_shell_state` | infra | Without it stale shell-exported tracing decisions override the operator's input in the release. | docs/deploy.md#configuration-contract |
| `test_bundle_decisions_match_the_rendered_release` | infra | Without it shell smoke and cleanup gates disagree with what the chart actually deployed. | docs/deploy.md#configuration-contract |
| `test_bundle_decisions_quote_operator_endpoints_for_shell_eval` | infra | Without it a hostile operator endpoint executes commands when the shell evals the decisions. | docs/deploy.md#configuration-contract |
| `test_lenient_rerank_bool` | infra | Without it rerank enable spellings are misread and the reranker is wrongly on or off. | docs/deploy.md#configuration-contract |
| `test_metrics_literal_true_only` | infra | Without it a loose metrics value enables the scrape endpoint and ServiceMonitor unintentionally. | docs/deploy.md#configuration-contract |
| `test_retired_in_place_switch_accepts_publication` | infra | Without it operators with the retired switch set to true can no longer deploy. | docs/deploy.md#configuration-contract |
| `test_retired_in_place_switch_refuses_false` | infra | Without it an explicit false silently renders a publication the operator did not ask for. | docs/deploy.md#configuration-contract |
| `test_strict_bool_rejects_garbage` | infra | Without it a typo in a boolean such as INGEST_REINGEST is read as false and the run silently differs from intent. | docs/deploy.md#configuration-contract |
| `test_ui_enabled_selection` | infra | Without it UI_ENABLED=false is lost between operator input and the rendered release and the console stays exposed. | docs/deploy.md#configuration-contract |
| `test_ui_enabled_rejects_garbage` | infra | Without it an invalid UI_ENABLED value is accepted or echoed in the error. | docs/deploy.md#configuration-contract |
| `test_contextual_embed_invalid_value_is_refused_not_false` | infra | Without it an invalid contextual-embedding value is read as false and ingest embeds without context. | docs/deploy.md#configuration-contract |
| `test_ingest_enabled_block_and_tricky_retire` | infra | Without it the ingest block drops the collection policy or mangles retire-doc identifiers containing pipes and ampersands. | docs/deploy.md#configuration-contract |
| `test_route_needs_ca_file` | infra | Without it an enabled Route is rendered with no destination CA and the console is unreachable. | docs/deploy.md#configuration-contract |
| `test_fail_closed_inputs` | infra | Without it invalid SHA, dimension, revision, registry or worker values produce a release instead of failing before mutation. | docs/deploy.md#configuration-contract |
| `test_without_ingest_job_mode` | infra | Without it the ingest Job is rendered when the operator asked for a deploy without it. | docs/deploy.md#configuration-contract |
| `test_no_secret_material_leaks` | infra | Without it API keys from the environment are written into the release values file. | docs/deploy.md#configuration-contract |
| `test_emitter_round_trip_byte_exact` | infra | Without it quotes, newlines and unicode in operator values are corrupted by the YAML emitter. | docs/deploy.md#configuration-contract |
| `test_mapper_to_helm_round_trip` | infra | Without it values the mapper writes are rendered differently by Helm (retire-doc, shard policy, agent-only keys). | docs/deploy.md#configuration-contract |
| `test_reasoning_leg_is_mapped_only_when_model_is_set` | infra | Without it a reasoning URL is set without a model or a configured model is dropped. | docs/deploy.md#configuration-contract |
| `test_reasoning_fails_closed_when_model_set_without_base_url` | infra | Without it a reasoning model with no endpoint deploys and answers fail at runtime. | docs/deploy.md#configuration-contract |
| `test_contextual_pair_matches_preflight_validation` | infra | Without it contextual embedding is enabled without its model or URL and ingest fails late. | docs/deploy.md#configuration-contract |
| `test_namespace_rejects_invalid_label_before_values_write` | infra | Without it an invalid or injected NAMESPACE reaches the values file or helm. | docs/deploy.md#configuration-contract |
| `test_chat_condensation_operator_mapping` | infra | Without it the chat condensation flag is not carried into the release. | docs/deploy.md#configuration-contract |
| `test_selected_model_budgets_reach_rendered_agent` | infra | Without it operator-selected model length, reserve, effort and stream settings never reach the agent pod. | docs/deploy.md#configuration-contract |
| `test_model_budget_invalid_selection_fails_without_artifact` | infra | Without it out-of-range budgets produce a values file and echo private values. | docs/deploy.md#configuration-contract |
| `test_effort_outside_served_set_fails_before_artifact` | infra | Without it a reasoning effort the model does not serve is deployed. | docs/deploy.md#configuration-contract |
| `test_preflight_effort_mirror_matches_settings` | infra | Without it the bastion preflight's effort list drifts from the application Settings. | docs/deploy.md#configuration-contract |
| `test_empty_model_budget_inputs_preserve_application_defaults` | infra | Without it empty budget inputs override application defaults. | docs/deploy.md#configuration-contract |
| `test_decoupled_backend_flags_follow_the_parent_by_default` | infra | Without it unset Jaeger/ServiceMonitor flags stop following the tracing/metrics parent. | docs/deploy.md#configuration-contract |
| `test_jaeger_enabled_selection` | infra | Without it explicit Jaeger enable/disable values map to the wrong boolean. | docs/deploy.md#configuration-contract |
| `test_servicemonitor_enabled_selection` | infra | Without it the ServiceMonitor flag is not independent of metrics exposure. | docs/deploy.md#configuration-contract |
| `test_decoupled_flags_reject_garbage` | infra | Without it a typo in the Jaeger flag is accepted as a value. | docs/deploy.md#configuration-contract |
| `test_jaeger_for_disabled_tracing_fails_closed` | infra | Without it Jaeger is requested while tracing is off. | docs/deploy.md#configuration-contract |
| `test_monitor_for_disabled_metrics_fails_closed` | infra | Without it a ServiceMonitor is requested while metrics are off. | docs/deploy.md#configuration-contract |
| `test_jaeger_false_without_destination_fails_closed` | infra | Without it disabling Jaeger without naming a collector exports to a non-existent Service. | docs/deploy.md#configuration-contract |
| `test_jaeger_false_with_intentional_destination` | infra | Without it valid external-collector or tracing-off configurations are rejected. | docs/deploy.md#configuration-contract |
| `test_mapper_to_helm_round_trip_decoupled` | infra | Without it decoupled tracing/metrics flags are lost between the mapper and the Helm render. | docs/deploy.md#configuration-contract |
| `test_image_digest_env_renders_digest_per_image` | infra | Without it verified image digests are not carried to the release and tags run instead. | docs/deploy.md#configuration-contract |
| `test_malformed_image_digest_fails_closed` | infra | Without it a malformed digest is deployed instead of refused. | docs/deploy.md#configuration-contract |

## `tests/test_mcp_agent.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_build_dispatch` | infra | The agent could build a live z/OS client while the flag is off, or enable it with no bridge URL and fail late. | docs/adr/0003-zowe-mcp-read.md |
| `test_call_tool_posts_jsonrpc_and_injects_trace` | core | The agent could send malformed JSON-RPC frames to the bridge or drop W3C trace propagation on this egress. | docs/adr/0003-zowe-mcp-read.md |
| `test_call_tool_rejects_off_allowlist_without_http` | core | The agent could call a non-allowlisted (mutating) bridge tool over the network. | docs/adr/0003-zowe-mcp-read.md |
| `test_call_tool_failures_raise_stable_errors` | core | Tool errors, HTTP 500 or malformed replies could surface raw upstream text or unclassified exceptions. | docs/adr/0003-zowe-mcp-read.md |
| `test_probe_allowlist_gates` | core | A bridge exposing a non-allowlisted tool (for example job_submit) could pass the startup probe unnoticed, and probe failures could leak exception text into logs. | docs/adr/0003-zowe-mcp-read.md |
| `test_routing_matrix` | core | Questions about live job/dataset state could be routed to the manual corpus (or manual questions to live fetches), giving wrong or unnecessary live access. | docs/adr/0003-zowe-mcp-read.md |
| `test_fetch_makes_no_source_call_for_manual_dry_run_unconfigured_or_no_target` | core | Manual, dry-run, unconfigured or target-less queries could still call the mainframe, including an unscoped job listing of every owner. | docs/adr/0003-zowe-mcp-read.md |
| `test_fetch_job_failure_two_calls_and_spool_assumption` | core | A job-failure question could fetch the wrong spool or hide that a default spool file was assumed. | docs/adr/0003-zowe-mcp-read.md |
| `test_fetch_truncates_with_suffix` | core | Live fetched text could exceed the configured byte cap or be cut without a visible truncation marker. | docs/adr/0003-zowe-mcp-read.md |
| `test_fetch_errors_degrade_with_codes` | core | A bridge failure could raise into the answer path instead of degrading with a stable code. | docs/adr/0003-zowe-mcp-read.md |
| `test_fetch_emits_bounded_live_span` | core | Live-fetch trace spans could carry fetched mainframe content (passwords, secrets) into the tracing backend. | AGENTS.md hard boundary: logs contain no secrets/manual text |
| `test_allowlist_is_the_bridge_registry_and_read_only` | core | The client allowlist and bridge registry could drift apart or admit a write/submit/console-style tool without a new ADR. | docs/adr/0003-zowe-mcp-read.md |
| `test_agent_http_surface_exposes_no_mutating_operation_or_live_call` | core | The agent could gain a mutating HTTP method or wire the live fetch into an endpoint without the ADR-0003 observation contract. | docs/adr/0003-zowe-mcp-read.md |
| `test_mock_mode_never_reaches_prod_manifests` | infra | A production manifest carrying MCP_MOCK_DIR would serve fixture data as live mainframe state. | AGENTS.md hard boundary: no mock/hash modes in production manifests |
| `test_dead_bridge_is_warn_only_and_never_blocks_agent_startup` | core | An unreachable bridge could keep the agent from starting, or log the exception text and host instead of a fixed warning. | docs/adr/0003-zowe-mcp-read.md |

## `tests/test_mcp_ftp_bridge.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_dataset_read_sequential_member_listing_and_stable_errors` | core | Dataset reads (sequential, PDS member, member listing) could return wrong content, and failures could lose the not_found code or leak the credential. | docs/adr/0003-zowe-mcp-read.md |
| `test_uss_read_and_path_guards` | core | Relative or traversal USS paths could be accepted, reading files outside the intended scope. | docs/adr/0003-zowe-mcp-read.md |
| `test_job_status_filters_and_jes_spool_read_with_arg_guards` | core | Job status filters and spool reads could return wrong jobs or accept malformed job/spool arguments sent to the mainframe. | docs/adr/0003-zowe-mcp-read.md |
| `test_byte_cap_truncates_with_suffix` | core | An oversized dataset could be fully downloaded and returned instead of being capped and the transfer aborted. | docs/adr/0003-zowe-mcp-read.md |
| `test_timeout_and_jes_refusal_map_to_stable_codes_per_tool` | core | A timeout or JES refusal could surface raw errors, or a JES refusal could disable dataset reads that do not need JES. | docs/adr/0003-zowe-mcp-read.md |
| `test_connect_protects_the_data_channel_only_over_tls` | infra | Credentials and data could cross an unprotected FTP data channel, or protection could be requested where TLS is not in use. | docs/adr/0003-zowe-mcp-read.md |
| `test_initialize_negotiates_and_tools_list_registers_exactly_the_allowlist` | core | A fifth (possibly mutating) tool could be registered on the bridge without failing any test, or MCP clients could fail to negotiate. | docs/adr/0003-zowe-mcp-read.md |
| `test_tools_call_unknown_and_bad_args_rejected` | core | Unknown tools such as job_submit or malformed arguments could reach the mainframe instead of a protocol error. | docs/adr/0003-zowe-mcp-read.md |
| `test_tools_call_logs_tool_name_only` | core | The bridge access log could record dataset names or fetched content, leaking mainframe data into logs. | AGENTS.md hard boundary: logs contain no secrets/manual text |
| `test_stdio_loop_frames_replies_and_skips_blanks` | core | The stdio transport could mis-frame replies or crash on blank or garbage lines, breaking MCP clients. | docs/adr/0003-zowe-mcp-read.md |
| `test_http_transport_lists_tools` | core | The HTTP transport used by the agent could fail to list tools or accept an invalid body instead of returning 400. | docs/adr/0003-zowe-mcp-read.md |
| `test_main_refuses_without_credentials` | core | The bridge could start without FTP credentials and fail later per request instead of refusing at launch. | docs/adr/0003-zowe-mcp-read.md |
| `test_tools_call_span_joins_parent_and_carries_bounded_attrs` | core | Traces from agent to bridge could lose their parent link, or spans could carry spool content or credentials. | AGENTS.md hard boundary: logs contain no secrets/manual text |
| `test_lifespan_wires_tracer_setup_and_shutdown` | infra | The bridge could stop exporting traces to the configured endpoint or lose buffered spans at shutdown. | docs/adr/0003-zowe-mcp-read.md |
| `test_sample_ratio_from_env_clamps_and_falls_back` | infra | A bad OTEL_SAMPLE_RATIO value could disable tracing or crash the bridge at startup. | docs/adr/0003-zowe-mcp-read.md |
| `test_all_tools_issue_only_read_ftp_verbs` | core | Any tool could send STOR/DELE/RNFR or a submitting SITE command to the mainframe, breaking the read-only guarantee. | docs/adr/0003-zowe-mcp-read.md |

## `tests/test_mcp_knowledge.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_framing_and_tool_catalog` | core | The knowledge MCP adapter could expose extra (non read-only) tools or mishandle JSON-RPC framing for MCP clients. | docs/evidence-contract.md (issue #405) |
| `test_bad_arguments_are_protocol_errors_before_any_upstream_call` | core | Invalid arguments (empty query, oversize limit, bool limit, huge max_bytes) could reach the agent HTTP service. | docs/evidence-contract.md (issue #405) |
| `test_search_then_read_matches_the_http_service_exactly` | core | The MCP evidence read could return different content than the HTTP evidence endpoint, breaking parity with the API. | docs/evidence-contract.md (issue #405) |
| `test_failures_have_identical_status_code_and_message_over_both_transports` | core | MCP and HTTP could disagree on malformed/unknown/denied/scoped/tampered failures, leak storage text, or stay latched after denial. | docs/evidence-contract.md (issue #405) |
| `test_reference_is_percent_encoded_and_no_incoming_credentials_are_forwarded` | core | Reference delimiters could alter the upstream path, or incoming caller credentials could be forwarded to the agent. | docs/evidence-contract.md (issue #405) |
| `test_upstream_faults_become_fixed_error_results_without_upstream_text` | core | Upstream connection errors or empty replies could leak internal host text through MCP or crash the adapter. | docs/evidence-contract.md (issue #405) |
| `test_adapter_has_no_storage_model_or_service_internals_to_bypass_with` | core | The adapter could import Qdrant, retrieval or agent internals and bypass the evidence service's access and scope checks. | docs/evidence-contract.md (issue #405) |
| `test_http_notification_cancels_only_its_session_and_next_call_succeeds` | core | A cancel notification could cancel another session's call or leave the session unusable for the next call. | docs/evidence-contract.md (issue #405) |
| `test_http_waiter_cancellation_is_not_a_protocol_cancellation` | core | A dropped HTTP waiter could be treated as an MCP cancellation, aborting upstream work the client never cancelled. | docs/evidence-contract.md (issue #405) |
| `test_stdio_notification_dispatch_and_eof_close_owned_client` | core | Over stdio, cancellation could fail to reach in-flight work, or EOF could leak the owned upstream client. | docs/evidence-contract.md (issue #405) |
| `test_http_shutdown_closes_created_client_once_and_leaves_borrowed_client_open` | core | Shutdown could leak the adapter's own client, close it twice, or close a caller-supplied client. | docs/evidence-contract.md (issue #405) |
| `test_http_shutdown_cancels_pending_work_before_closing_owned_client` | core | Pending requests at shutdown could hang or use a closed client instead of being cancelled first. | docs/evidence-contract.md (issue #405) |

## `tests/test_mcp_sim.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_fetch_live_over_wire` | infra | If the agent's HttpZoweMCP client and the real bridge process disagree on the JSON-RPC wire, live z/OS fetches fail in production while every fake-backed unit test stays green. | docs/adr/0003-zowe-mcp-read.md |

## `tests/test_metric_contract.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_contract_instruments_pinned` | infra | A renamed instrument or changed unit silently renames the Prometheus series that dashboards and alerts query. | docs/metric-contract.md |
| `test_contract_label_keys` | infra | An added or dropped metric label breaks PromQL selectors and can add unbounded cardinality. | docs/metric-contract.md |
| `test_contract_buckets_pinned` | infra | Edited histogram bucket boundaries invalidate latency quantile queries and historical comparison. | docs/metric-contract.md |
| `test_promql_references_real_contract` | infra | The shipped investigation PromQL could reference metric names or labels that no longer exist and return nothing during an incident. | docs/metric-contract.md |

## `tests/test_metrics.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_setup_metrics_disabled_and_idempotent` | infra | Disabled metrics could install a provider, or repeated setup could stack providers. | docs/metric-contract.md |
| `test_endpoint_for_path` | core | Probes, scrapes or unknown paths could be counted as requests, or real endpoints (including the console) not counted. | docs/metric-contract.md (endpoint vocabulary) |
| `test_record_request_counter_and_histograms` | core | Request counters, duration, hit and ttft histograms could miscount, record unmeasured legs as zero, or count unknown endpoints. | docs/metric-contract.md |
| `test_record_request_noop_when_disabled_and_clamps_bad_values` | core | Disabled metrics could raise on the request path, or negative inputs could break recording. | docs/metric-contract.md; #187 |
| `test_search_success_records_ok_series` | core | Each search would not add exactly one ok sample to the scraped exposition, or the query text would appear in it. | docs/metric-contract.md |
| `test_search_failure_records_outcome_series` | core | A failed search would not add exactly one upstream_error sample, hiding failures from alerts. | docs/metric-contract.md |
| `test_overlong_query_records_invalid_request` | core | Framework 422 rejections would not be counted as invalid_request, the only record of those failures. | docs/metric-contract.md (error attribution) |
| `test_verification_label_is_bounded` | core | An unknown verification state could become a metric label value, leaking text and growing cardinality. | docs/metric-contract.md (label vocabularies) |
| `test_finalized_outcome_series` | core | Answer, chat and console results (accepted, draft, incomplete, empty, error; JSON and stream) would record the wrong outcome or verification state, double count, or leak query text. | docs/metric-contract.md; docs/agent.md answer contract |
| `test_stream_close_records_once` | core | A stream closed after a token, final or error frame would be recorded zero or several times, or with the wrong outcome. | docs/agent.md observability; AGENTS.md lifecycle rule |
| `test_console_validation_only_counts_when_enabled` | core | Console validation failures would not be counted when enabled, or a disabled console would still record metrics. | docs/agent.md console sections; docs/metric-contract.md |
| `test_broken_metrics_instrument_does_not_break_request` | core | A metrics instrument fault would fail or truncate a user's stream. | docs/agent.md observability (metrics fail open); #187 |
| `test_answer_not_configured_records_once` | core | The model-not-configured 503 on answer would be counted zero or twice. | docs/metric-contract.md (error attribution) |
| `test_record_request_failure_log_leaks_no_exception_text` | core | A metrics fault's debug log would include exception message text. | AGENTS.md hard boundaries (logs contain no secrets) |

## `tests/test_openshift_identities.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_qdrant_removes_chart_identity_defaults` | infra | Without it the vendored Qdrant chart reintroduces fixed UIDs and breaks restricted-v2 admission. | docs/deploy.md#deployment-policy |
| `test_production_qdrant_requires_hard_worker_separation` | infra | Without it Qdrant replicas can be colocated on one worker and the 3-replica setup gives false HA. | docs/deploy.md#deployment-policy |
| `test_jaeger_accepts_assigned_identity_with_restricted_security` | infra | Without it Jaeger gains fixed identities or lost hardening and is rejected or over-privileged on OpenShift. | docs/deploy.md#deployment-policy |

## `tests/test_ops_cli.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_health_ok_json_and_text` | core | Operators and scripts lose the stable health JSON envelope and text output that automation parses for readiness. | docs/agent.md (issue #172) |
| `test_health_degraded_is_not_ready_with_state_shown` | core | A degraded agent could report exit 0 and be treated as ready, or hide which dependency is down. | docs/agent.md (issue #172) |
| `test_health_qdrant_unready_envelope_is_unavailable_with_server_code` | core | Server error text could leak to the operator console, or an unready store could return the wrong exit code. | AGENTS.md hard boundary: client errors fixed messages and stable codes |
| `test_search_scoped_request_and_full_structured_hits` | core | Product/version scoping could be dropped from the request, or hit fields (cite, pages, message ids, text) lost from the output operators rely on. | docs/agent.md (issue #172) |
| `test_answer_accepted_json_preserves_provenance_fields` | core | Script review flags and verification state could be dropped, so an unvalidated script reads as trusted; inferred citations could be presented as grounding. | docs/agent.md (issue #172) |
| `test_answer_non_accepted_states_exit_3_but_are_labelled` | core | A non-accepted answer (insufficient evidence, draft, incomplete) could exit 0 and be consumed as a verified answer. | docs/agent.md (issue #172) |
| `test_accepted_with_blank_answer_is_never_a_success` | core | An empty answer marked accepted could pass as success in automation. | docs/agent.md (issue #172) |
| `test_stream_complete_prints_only_the_final` | core | Streamed partial tokens could be printed as the answer instead of only the validated final event. | docs/agent.md (issue #172) |
| `test_stream_never_prints_a_completed_answer_without_final` | core | A stream cut short or ended by an error event could print a partial answer as complete, and the next request could fail. | docs/agent.md (issue #172) |
| `test_stream_malformed_frames_fail_closed` | core | Malformed or out-of-order SSE frames could be accepted as an answer instead of failing closed with a fixed error. | docs/agent.md (issue #172) |
| `test_stream_final_with_non_stop_finish_or_state_is_not_accepted` | core | A truncated generation (finish_reason length) could exit 0 as an accepted answer even if the server state lies. | docs/agent.md (issue #172) |
| `test_stream_wrong_content_type_and_pre_stream_refusal` | core | A non-SSE reply or a pre-stream refusal could be mis-parsed instead of mapped to the fixed error codes. | docs/agent.md (issue #172) |
| `test_stream_cancellation_prints_nothing_and_next_request_is_healthy` | core | Ctrl-C mid-stream could print a partial answer or wedge later requests instead of exiting with the cancelled code. | docs/agent.md (issue #172) |
| `test_status_mapping_and_no_server_text_echo` | core | HTTP status classes could map to wrong exit codes, redirects could be followed, and upstream text could leak into output. | AGENTS.md hard boundary: client errors fixed messages and stable codes |
| `test_unknown_server_code_is_dropped` | core | An unrecognized server error code (for example one carrying a bearer token) could be echoed to the operator. | AGENTS.md hard boundary: client errors fixed messages and stable codes |
| `test_malformed_and_truncated_search_responses` | core | Non-JSON, truncated or wrongly typed search bodies (including a non-scalar exact reference) could crash or be accepted instead of exit 6 with a fixed error, and the next call must still work. | docs/agent.md (issue #172) |
| `test_malformed_answer_fields_fail_closed` | core | Wrongly typed answer fields (verification state, citations, review flag) could be accepted as a valid answer. | docs/agent.md (issue #172) |
| `test_oversize_body_is_refused` | core | An oversized response body could exhaust operator memory instead of being refused. | docs/agent.md (issue #172) |
| `test_connection_refused_and_timeout_exit_5_then_recover` | core | A dead or stalled agent could hang the CLI, leak the host address, or leave the next ordinary call failing. | docs/agent.md (issue #172) |
| `test_api_key_header_and_never_printed` | core | The API key could be omitted from the Authorization header or printed to stdout/stderr on success and failure paths. | AGENTS.md hard boundary: logs contain no secrets |
| `test_no_key_means_no_authorization_header_and_flag_does_not_exist` | core | An empty header could be sent when no key is set, or a command-line key flag (visible in process listings) could be reintroduced. | AGENTS.md hard boundary: logs contain no secrets |
| `test_bad_base_url_is_usage_error_without_echo` | core | Malformed URLs (userinfo, wrong scheme, bad port) could be used or echoed including embedded credentials. | docs/agent.md (issue #172) |
| `test_missing_base_url_is_usage_error_and_env_url_works` | core | A missing URL could silently target a default, or the env/trailing-slash forms operators use could break. | docs/agent.md (issue #172) |
| `test_key_refused_over_cleartext_non_loopback_and_bad_keys` | core | The API key could be sent in cleartext to a non-loopback host, or malformed keys echoed in output. | AGENTS.md hard boundary: logs contain no secrets |
| `test_argument_errors_exit_2_before_any_request` | core | Invalid arguments could reach the agent as requests instead of exit 2 with no network traffic. | docs/agent.md (issue #172) |
| `test_usage_errors_do_not_echo_argument_values` | core | Query text or argument values (possibly sensitive) could be echoed in usage errors. | AGENTS.md hard boundary: logs contain no secrets |
| `test_cli_only_reaches_the_three_supported_routes` | core | The ops CLI could grow routes or imports (Qdrant, retrieval, bridge, agent internals) that bypass the serving HTTP boundary. | docs/agent.md (issue #172) |
| `test_terminal_control_sequences_in_server_text_are_neutralized` | core | Escape sequences in server text could execute on the operator terminal (title/color injection). | docs/agent.md (issue #172) |
| `test_tls_verification_is_enforced_and_ca_file_is_honoured` | core | TLS verification could be bypassed or a custom CA ignored, and the key sent to an unverified server. | docs/agent.md (issue #172) |
| `test_entry_point_is_declared_and_module_runs` | infra | The mainframe-rag-ops console script or python -m entry point could break so operators cannot run the CLI. | docs/agent.md (issue #172) |
| `test_cli_process_malformed_nullable_and_enum_fields_have_fixed_error` | core | In a real process, malformed nullable/enum fields (buffered and streamed) could produce a traceback or leak upstream text instead of exit 6. | docs/agent.md (issue #172) |
| `test_search_cli_process_preserves_optional_exact_reference` | core | Exact evidence references containing whitespace, delimiters or non-ASCII could be altered or dropped between the agent and the CLI output. | docs/evidence-contract.md (issue #405) |

## `tests/test_paraphrase.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_paraphrase_entry_page_has_answer_without_query_echo` | infra | The generator stops writing answer text and identifier into the target page, so the paraphrase instrument measures nothing. | docs/testing.md#paraphrase-instrument |
| `test_standard_entry_still_echoes_query` | infra | The paraphrase branch changes standard golden pages, which the main retrieval gate corpus depends on. | docs/testing.md#paraphrase-instrument |
| `test_generator_is_deterministic` | infra | Corpus generation varies between runs, invalidating mode-keyed paraphrase baselines. | docs/testing.md#paraphrase-instrument |
| `test_paraphrase_file_is_valid_instrument_input` | infra | Duplicate ids, missing answer text, dangling traps or doc ids colliding with golden make the instrument invalid. | docs/testing.md#paraphrase-instrument |
| `test_paraphrase_no_query_echo_in_generated_pages` | infra | Generated pages echo the query near-verbatim, so paraphrase results measure lexical matching instead of semantics. | docs/testing.md#paraphrase-instrument |

## `tests/test_parser_ibm_shape.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_extracts_ibm_metadata_title_and_toc` | core | IBM form number, product, version, title and outline are read from a PDF, so payload identity and citations would carry wrong or empty metadata without it. | docs/ingest.md |
| `test_doc_id_tie_break_is_lexicographic` | core | Equal-count form numbers pick the same doc_id on every run, otherwise doc_id flips with PYTHONHASHSEED and churns resume and re-ingest. | docs/ingest.md#identity-contract |
| `test_parse_pdf_sha256_override_and_fallback` | core | The caller-supplied digest lands in ParsedDoc.sha256 verbatim and is stable, so resume skip logic keyed on it is not silently corrupted. | docs/ingest.md#identity-contract |
| `test_walker_finds_pdfs_under_dot_parent_dir` | core | A corpus mounted under a dot or ignored parent directory is still ingested while in-corpus dot dirs stay skipped, so an operator mount path never yields an empty ingest. | issue 594 |
| `test_walker_ignores_pdx_and_idx` | core | Adobe catalog/index files and non-PDFs are never ingested as documents, so vendor index artifacts do not become chunks. | docs/ingest.md |
| `test_extract_page_texts_label_tree_starting_after_page_zero` | core | A PageLabels tree starting after page 0 does not raise IndexError and lose the whole document; earlier pages just have no label. | issue 271 |
| `test_extract_page_texts_lives_in_a_hashed_rule_module` | core | Payload-changing extraction sits in a module covered by the extraction-rules hash, so edits to it force re-ingest instead of silently desyncing payloads. | issue 124 |

## `tests/test_placement.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_policy_selection_accepts_only_declared_profiles` | core | Without it the declared 6/3/2 production and 1/1/1 single-node profiles are not selected correctly. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_policy_claims_refused` | core | Without it contradictory, partial or above-replication policy claims are accepted as a placement policy. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_configured_policy_problems` | core | Without it a collection configured with the wrong shard or replication policy looks healthy. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_healthy_rf3_all_six_shards_and_one_copy_per_shard_refused` | core | Without it three ready peers holding one copy per shard are certified as HA. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_remote_reports_never_count_as_copies` | core | Without it one peer's memory of remote replicas counts as proof the copies exist. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_remote_report_disagreement_or_omission_is_unverifiable` | core | Without it contradicting or missing remote reports are treated as copies instead of an unverifiable observation. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_missing_collection_everywhere_is_unservable_on_one_peer_unverifiable` | core | Without it a collection missing on all peers or one peer is misreported as healthy or degraded. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_unreachable_peer_cannot_certify_healthy` | core | Without it a placement is certified healthy although a peer is unreachable. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_no_copy_observed_with_reachable_peers_is_unservable` | core | Without it a shard with no observed copy is reported servable. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_transfer_in_progress_is_recovering` | core | Without it a placement with an in-flight transfer is certified healthy. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_non_active_replicas_are_recovering_never_healthy` | core | Without it initializing, dead or read-only replicas count as active copies. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_unknown_replica_state_is_unverifiable` | core | Without it an unrecognized replica state is trusted instead of refused. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_unknown_or_disagreeing_shard_count_is_unverifiable` | core | Without it an unknown or inconsistent shard count is assumed correct. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_observed_shard_count_below_policy_is_unservable` | core | Without it fewer shards than the policy are reported as acceptable. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_peer_view_outside_accepted_membership_is_unverifiable` | core | Without it an endpoint from a foreign cluster contributes replicas or an empty membership is accepted. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_remote_reference_outside_membership_is_unverifiable` | core | Without it references to a peer outside the membership count as topology. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_unexpected_shard_id_is_unverifiable` | core | Without it an unsupported shard layout is silently ignored. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_cluster_member_loss_ladder` | core | Without it one lost member is not degraded or two lost members are not refused as unverifiable. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_membership_below_expectation_is_unverifiable` | core | Without it a cluster with fewer members than expected is certified. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_membership_disagreement_is_unverifiable` | core | Without it peers disagreeing on membership are accepted. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_missing_membership_on_reachable_peer_is_unverifiable` | core | Without it a reachable peer reporting no membership is trusted. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_stopped_consensus_is_unverifiable` | core | Without it a cluster with stopped consensus is certified. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_fewer_endpoints_than_expected_is_unverifiable` | core | Without it too few authoritative endpoints still produce a healthy verdict. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_standalone_server_is_the_single_node_profile` | core | Without it a standalone server is accepted as 3-peer production or rejected as single-node. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_report_verdicts` | core | Without it the printed verdict claims healthy, allows degraded for production, or mislabels single-node and alias evidence. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_binding_resolves_alias_or_names_the_physical_pair` | core | Without it the alias binding evidence names the wrong collection pair. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_observe_collection_normalizes_payload` | core | Without it the Qdrant cluster info is misparsed or a fetch failure crashes instead of reporting unreachable. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_documented_service_plus_direct_peers_is_healthy` | core | Without it the documented Service plus direct peers topology is refused or the Service identity becomes a fourth replica. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_three_urls_reaching_one_peer_cannot_satisfy_rf3` | core | Without it three URLs reaching one peer certify RF=3 (false HA). | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_failed_member_cannot_be_replaced_by_a_foreign_endpoint` | core | Without it a lost member is replaced by a foreign endpoint and HA is certified. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_entry_membership_mismatch_is_refused` | core | Without it a Service routed to a different cluster certifies another cluster's placement. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_one_peer_loss_is_degraded_only_with_the_operational_flag` | core | Without it a degraded cluster passes production verification or cannot be accepted for operations. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_allow_degraded_never_accepts_unverifiable_evidence` | core | Without it the allow-degraded flag accepts missing membership, stopped consensus or a missing control collection. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_contradictory_script_options_are_usage_errors` | core | Without it contradictory CLI options run a verification they should refuse. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_production_without_peer_urls_refused` | core | Without it a production claim with no peer endpoints is verified against the Service alone. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_peer_policy_drift_or_unreadable_refused` | core | Without it a peer with wrong or unreadable collection policy is accepted. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_peer_identity_contradiction_across_collections_refused` | core | Without it a peer reporting different identities across collections is accepted. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_script_no_claim_refused_and_contradictory_claim_usage_error` | core | Without it verification runs without a declared profile or with contradictory profiles. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_script_single_node_profile` | core | Without it the single-node profile is not reported as non-HA. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_alias_change_is_retried_against_the_new_generation` | core | Without it a stale generation is certified when the alias moved during observation. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_alias_that_keeps_moving_is_refused` | core | Without it a continuously moving alias is certified. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_peer_alias_disagreement_is_refused` | core | Without it peers pointing the alias at different generations are accepted. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_physical_pair_without_alias_reports_the_physical_target` | core | Without it a physical pair without alias is presented as alias certification. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_replacement_peer_is_degraded_then_recovering_until_every_copy_is_active` | core | Without it an empty replacement peer is certified healthy before it holds every copy. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |
| `test_interrupted_transfer_never_certifies_and_resumes_to_healthy` | core | Without it an interrupted transfer is certified or a completed copy is not recognized as healthy. | AGENTS.md hard boundaries (Qdrant HA, false-HA refusal); #360 |

## `tests/test_probe_gateway.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_probe_all_healthy_recommends_score_first` | infra | Without it the cutover probe cannot pass a healthy gateway or sends the wrong per-leg key. | docs/agent.md#http-model-contract |
| `test_unreachable_required_gateway_leg_fails` | infra | Without it the probe passes while a required gateway leg is unreachable. | docs/agent.md#http-model-contract |
| `test_probe_score_dead_recommends_rerank_first` | infra | Without it a dead score route yields the wrong rerank endpoint order recommendation. | docs/agent.md#http-model-contract |
| `test_probe_rerank_both_dead_fails_without_recommendation` | infra | Without it the probe passes with no working rerank route. | docs/agent.md#http-model-contract |
| `test_probe_reasoning_unset_skips_chat` | infra | Without it an unconfigured reasoning leg fails the probe instead of being skipped. | docs/agent.md#http-model-contract |
| `test_probe_embed_dim_mismatch_fails` | infra | Without it a gateway returning the wrong embedding dimension passes cutover. | docs/agent.md#http-model-contract |
| `test_probe_embed_401_names_key` | infra | Without it a rejected embed key fails with no hint of which key is wrong. | docs/agent.md#http-model-contract |
| `test_probe_tokenize_absent_is_informational` | infra | Without it a gateway lacking the optional tokenizer fails cutover. | docs/agent.md#http-model-contract |
| `test_probe_stream_without_done_fails` | infra | Without it a stream that ends without [DONE] passes the probe. | docs/agent.md#http-model-contract |
| `test_required_reasoning_cannot_skip` | infra | Without it a required reasoning check is skipped when its URL or model is unset and the probe still passes. | docs/agent.md#http-model-contract |
| `test_models_listing_cannot_bypass_required_checks` | infra | Without it the models-listing mode can be combined with required checks to skip them. | docs/agent.md#http-model-contract |
| `test_stream_errors_never_pass` | infra | Without it error, empty, malformed or unfinished streams pass cutover although the agent would fail on them. | docs/agent.md#http-model-contract |
| `test_unusable_json_chat_fails` | infra | Without it a JSON chat reply with no usable final answer passes the probe. | docs/agent.md#http-model-contract |
| `test_probe_uses_application_simple_reasoning_effort` | infra | Without it the probe uses a reasoning effort different from the application's so it proves a different request. | docs/agent.md#http-model-contract |
| `test_required_tokenizer_does_not_accept_estimation` | infra | Without it a required tokenizer check passes on an absent or malformed tokenizer response. | docs/agent.md#http-model-contract |
| `test_required_tokenizer_counts_chat_generation_template` | infra | Without it the tokenizer is probed with a different template than the agent uses, hiding count drift. | docs/agent.md#http-model-contract |
| `test_explicit_completion_budget_reaches_json_and_stream` | infra | Without it the configured completion budget is not sent to the chat or stream request. | docs/agent.md#http-model-contract |
| `test_completion_budget_default_environment_and_cli_precedence` | infra | Without it the CLI flag and environment budget precedence breaks and the probe uses the wrong max_tokens. | docs/agent.md#http-model-contract |
| `test_invalid_completion_budget_fails_before_any_model_request` | infra | Without it an out-of-range budget sends model requests before being rejected. | docs/agent.md#http-model-contract |
| `test_larger_budget_retains_strict_final_answer_gate` | infra | Without it a larger budget lets length-truncated or reasoning-only replies pass as answers. | docs/agent.md#http-model-contract |
| `test_probe_models_flag_lists_ids_and_fails_on_a_dead_leg` | infra | Without it the --models readiness listing exits 0 while a gateway leg is down, so cutover proceeds against a dead endpoint. | docs/agent.md#http-model-contract |
| `test_probe_stream_flag_accepts_a_complete_stream_with_usage_frame` | infra | Without it the stream probe rejects a valid OpenAI stream (usage-only trailer), blocking cutover, or the pass path is never proven. | docs/agent.md#http-model-contract |

## `tests/test_prompt_order.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_unknown_prompt_order_fails_closed` | core | A misconfigured prompt_order silently builds a prompt in an unvetted order instead of failing closed. | docs/agent.md#answer-contract |
| `test_default_assembly_is_exact` | noise-kept | The default retrieval-order prompt changes silently, altering every production answer prompt (excerpt headers, source line, citation tail). | docs/agent.md#answer-contract |
| `test_retrieval_order_shape` | core | Excerpts are re-sorted by cite instead of rank, sysplex context lands after the question, or empty hits lose the section header. | docs/agent.md#answer-contract |
| `test_exact_final_messages_are_the_last_verified_candidate` | core | A prompt whose token budget was verified in one order is sent in another, so an over-window prompt is certified as verified. | tests/hazards/critical.json |
| `test_stable_cache_only_overflow_trims_or_refuses_before_model` | core | A prompt that fits in retrieval order but overflows in stable_cache order is certified from the wrong count and reaches the model over budget. | issue #368; docs/agent.md#answer-contract |
| `test_last_bounded_extra_count_can_confirm_budget` | core | A prompt with no excerpts and an exact remote count is wrongly reported as unverified budget. | docs/agent.md#answer-contract |
| `test_stable_cache_orders_instructions_context_excerpts_question_tail` | core | stable_cache prompts lose their instruction/context/excerpt/question/tail ordering or excerpt framing, breaking the prefix-cache and injection-framing design. | issue #80; docs/agent.md#answer-contract |
| `test_stable_cache_prefix_is_query_independent` | core | The cacheable prompt prefix varies with the query, so the prefix cache never hits and latency rises. | issue #80; docs/agent.md#answer-contract |
| `test_injection_stays_inside_excerpt_blocks` | core | Instruction-like text in a manual chunk escapes its delimited excerpt block into text the model is told to obey (prompt injection). | issue #80; docs/agent.md#answer-contract |
| `test_chat_budget_keeps_passage_and_antecedent_over_older_history` | core | Stale older chat turns displace the current evidence passage or the antecedent turn under a tight window. | issue #633; docs/agent.md#answer-contract |
| `test_chat_budget_keeps_older_turns_whole_and_newest_first_when_slack_exists` | core | Chat history is truncated mid-turn or oldest-first when slack exists, losing the most relevant context. | issue #633; docs/agent.md#answer-contract |
| `test_chat_budget_irreducible_antecedent_refuses_rather_than_dropping_it` | core | When the antecedent cannot fit, chat silently drops it and answers a different question instead of refusing with a budget error. | issue #633; docs/agent.md#answer-contract |
| `test_chat_budget_standalone_and_scope_change_follow_same_policy` | core | A standalone or scope-changing chat question keeps stale history instead of the latest turn plus evidence. | issue #633; docs/agent.md#answer-contract |

## `tests/test_prompt_packing_units.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_per_chunk_cap_never_splits_jcl_statement` | core | A char cap cuts a JCL statement mid-card and ships a partial statement as a complete excerpt. | issue #368; docs/agent.md#answer-contract |
| `test_per_chunk_cap_keeps_table_header_and_whole_rows` | core | A cap ships half a table row or drops the header, corrupting table evidence. | issue #368; docs/agent.md#answer-contract |
| `test_chunk_too_small_for_first_unit_is_omitted` | core | A chunk smaller than its first unit ships an empty or partial body instead of explicit omission. | issue #368; docs/agent.md#answer-contract |
| `test_narrative_keeps_character_truncation` | core | Unit-aware packing breaks safe narrative truncation, dropping prose evidence entirely. | issue #368; docs/agent.md#answer-contract |
| `test_persisted_spans_drive_packing` | core | Persisted unit spans are ignored or mis-honored, truncating a chunk that fits. | issue #368; docs/agent.md#answer-contract |
| `test_budget_remainder_packs_whole_units_then_stops` | core | The total-context remainder cut ships a partial statement. | issue #368; docs/agent.md#answer-contract |
| `test_final_recount_keeps_the_selected_reasoning_template` | core | The final token recount uses a different reasoning template than the request, certifying a wrong budget. | issue #368; docs/agent.md#http-model-contract |
| `test_irreducible_fixed_overflow_raises_before_generation` | core | A prompt whose fixed content exceeds the window reaches the model or leaks prompt text in the error instead of a counts-only budget error. | issue #368; docs/agent.md#answer-contract |
| `test_zero_excerpts_but_fitting_fixed_returns_empty_manifest` | core | When trimming empties the evidence the manifest or verified flag misreports what was sent. | issue #368; docs/agent.md#answer-contract |
| `test_verify_trim_snaps_to_statement_boundary` | core | Verification trimming cuts mid-statement or leaves the prompt over the window. | issue #368; docs/agent.md#answer-contract |
| `test_budget_verified_only_for_a_real_tokenizer_measurement` | core | Char packing or estimator-only counts are reported as verified, hiding unconfirmed budget compliance. | issue #368; docs/agent.md#answer-contract |
| `test_wholly_omitted_chunk_is_not_citation_eligible` | core | A wholly omitted chunk's cite or bracket label is accepted as a citation. | issue #368; docs/agent.md#answer-contract |
| `test_chat_path_matches_single_turn_on_units` | core | The chat packer diverges from single-turn and splits atomic units. | issue #368; docs/agent.md#answer-contract |
| `test_answer_and_chat_routes_report_budget_exceeded_422` | core | Budget overflow returns a 500, exception text or a model call instead of the fixed 422 envelope with stable code. | docs/agent.md#http-model-contract |
| `test_answer_stream_reports_budget_error_event` | core | A streamed budget overflow ends with a final event or no error event. | docs/agent.md#http-model-contract |
| `test_chat_stream_reports_budget_error_frames` | core | Chat streaming on budget overflow emits content deltas or lacks the error frame and [DONE]. | docs/agent.md#http-model-contract |
| `test_console_reports_budget_banner` | core | The operator console shows the generic server-fault banner instead of a shrink-the-request 422. | docs/agent.md#http-model-contract |
| `test_make_chunks_emits_unit_spans_for_structured_and_prose` | core | Ingest stops emitting unit spans for structured chunks (or emits them for prose), so retrieval packs by characters. | issue #368; docs/ingest.md |
| `test_upsert_chunks_persists_units_only_when_present` | core | Unit spans are not persisted to Qdrant payloads (or persisted empty), so character cuts return. | issue #368; docs/ingest.md |
| `test_parse_unit_spans_fail_closed` | core | Corrupt stored unit spans are trusted and drive cuts at invalid offsets. | issue #368; docs/agent.md#answer-contract |
| `test_estimator_drift_trims_past_four_rounds_instead_of_refusing` | core | Dense excerpts that overshoot the estimator make the answer refuse after four trim rounds although evidence could still fit. | issue #307; docs/agent.md#answer-contract |
| `test_requested_later_entry_is_supplied_not_unrelated_prefix` | core | A requested later entry in a chunk is replaced by an unrelated prefix, so the user's identifier is not answered. | issue #632; docs/agent.md#answer-contract |
| `test_unrequested_and_unmatched_queries_keep_prefix_behavior` | core | Queries without a matching identifier stop receiving the chunk prefix. | issue #632; docs/agent.md#answer-contract |
| `test_incidental_mention_is_not_an_entry_heading` | core | An incidental mention of an identifier selects the wrong entry range. | issue #632; docs/agent.md#answer-contract |
| `test_multiple_requested_identifiers_share_one_contiguous_range` | core | Multiple requested identifiers are not shipped together. | issue #632; docs/agent.md#answer-contract |
| `test_unfittable_entry_is_omitted_explicitly` | core | An entry larger than the cap is partially shipped instead of omitted explicitly. | issue #632; docs/agent.md#answer-contract |
| `test_chat_path_supplies_requested_later_entry` | core | Chat answers miss the requested later entry that single-turn finds. | issue #632; docs/agent.md#answer-contract |
| `test_offsets_reconstruct_exactly_after_every_trim_round` | core | Recorded source offsets no longer reproduce the shipped text after trimming, so provenance is wrong or the entry is cut. | issue #632; docs/agent.md#answer-contract |
| `test_prose_cut_ends_on_sentence_boundary` | core | Prose is cut mid-sentence. | issue #632; docs/agent.md#answer-contract |
| `test_prose_cut_never_separates_assertion_from_its_qualifier` | core | A cut separates an assertion from its qualifier, turning guidance into its opposite. | issue #632; docs/agent.md#answer-contract |
| `test_malformed_unit_spans_fall_back_without_inventing_anchors` | core | Malformed spans create invented entry anchors and wrong excerpt starts. | issue #632; docs/agent.md#answer-contract |
| `test_current_chunker_fixture_supplies_requested_code_entry` | core | The real chunker's output defeats requested-entry selection for code lists. | issue #632; docs/agent.md#answer-contract |
| `test_projection_equals_shipped_slice_non_prefix_and_omits_unsupplied` | core | The supplied-evidence projection differs from the text actually sent or lists omitted chunks. | issue #635; docs/agent.md#answer-contract |
| `test_projection_text_is_clipped_and_flagged_at_wire_bound` | core | Oversize projection text goes over the wire unclipped or unflagged. | issue #635; docs/agent.md#answer-contract |
| `test_source_metadata_unknown_stays_unknown_and_is_one_line` | core | Source metadata is inferred from request context or can inject a newline into the prompt. | issue #635; docs/agent.md#answer-contract |
| `test_stored_spans_past_the_cap_are_not_persisted` | core | Without it an oversize atomic-span list is stored on every chunk payload instead of being dropped for pack-time redetection, unbounding stored payload size. | docs/agent.md#answer-contract |
| `test_build_messages_tokenizer_requires_settings` | core | Without it a tokenizer passed without settings crashes with an opaque AttributeError, or sizes the window from nothing, instead of the explicit refusal. | docs/agent.md#answer-contract |
| `test_legacy_prefix_excerpt_derives_its_range_and_forged_body_is_dropped` | core | Without it an excerpt whose body is not the recorded source prefix survives the verification trim and ships as citable evidence it never was. | docs/agent.md#answer-contract |

## `tests/test_qdrant_auth.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_readonly_key_permits_serving_reads` | core | Without it a keyed Qdrant could break the serving agent's read calls made with the read-only key. | AGENTS.md hard boundary: Qdrant read-only vs writer credentials |
| `test_readonly_key_denied_mutations` | core | Without it the serving read-only key could mutate, snapshot or delete the corpus. | AGENTS.md hard boundary: Qdrant read-only vs writer credentials |
| `test_wrong_and_missing_keys_rejected` | core | Without it wrong or absent credentials could reach Qdrant reads or writes. | AGENTS.md hard boundary: Qdrant read-only vs writer credentials |
| `test_write_key_ingests_and_serves_roundtrip` | core | Without it the ingest writer key could lose write access or its data not be readable via the read-only key. | AGENTS.md hard boundary: Qdrant read-only vs writer credentials |
| `test_rotation_rejects_old_keys_without_data_loss` | core | Without it key rotation could leave old keys valid or lose stored data. | AGENTS.md hard boundary: Qdrant read-only vs writer credentials |

## `tests/test_qdrant_cluster.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_require_docker_rejects_digest_mismatch_without_pull` | infra | Without it qa:ha could run against a substituted or unpinned Qdrant image and still report a passing HA verdict. | docs/testing.md#helm-coverage |
| `test_require_docker_returns_immutable_id_after_digest_attestation` | infra | Without it the HA fixture could launch containers by a mutable tag instead of the attested immutable image ID. | docs/testing.md#helm-coverage |
| `test_acknowledged_write_rejoin_refuses_permanent_loss_or_corruption` | gate-control | Without this negative control the qa:ha scenario could silently stop detecting lost or corrupted replica data and pass vacuously. | docs/testing.md#helm-coverage |
| `test_require_docker_rejects_an_empty_inspection_record_without_pull` | infra | Without it an image inspection returning no record is not refused, so qa:ha could launch an unattested Qdrant image. | AGENTS.md#hard-boundaries |
| `test_qdrant_image_pin_ignores_commented_and_blank_lines` | infra | Without it a commented-out or older qdrant line in images.txt becomes the pinned image used by the simulation and the doctor version check. | AGENTS.md#hard-boundaries |
| `test_wait_cluster_ready_requires_every_endpoint_to_see_all_peers` | infra | Without it qa:ha starts its scenarios against a cluster where one endpoint still sees a partial or non-clustered membership, so HA results are attributed to a cluster that never formed. | docs/testing.md#helm-coverage |
| `test_wait_collection_placement_counts_only_active_copies_on_distinct_peers` | infra | Without it qa:ha treats replicas still in Recovery (or several copies on one peer) as placed, so the 6/3/2 replication gate passes before replicas can serve. | docs/testing.md#helm-coverage |
| `test_main_up_reports_cluster_failure_with_exit_one` | infra | Without it a failed disposable-cluster start exits 0, so an operator or CI step proceeds as if the HA fixture existed. | docs/testing.md#helm-coverage |

## `tests/test_qdrant_io.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_ensure_collection_creates_schema_and_indexes_before_load` | core | Without it collections are created without the required vectors, quantization, sparse IDF or payload indexes, degrading retrieval or filters. | docs/ingest.md#publication-contract |
| `test_ensure_collection_refuses_dimension_problems` | core | Without it a dimension mismatch or missing DENSE_DIM writes mismatched vectors. | docs/ingest.md#publication-contract |
| `test_ensure_collection_forwards_selected_policy_verbatim` | core | Without it the selected shard, replica and write-consistency policy does not reach collection creation. | AGENTS.md#hard-boundaries |
| `test_ensure_collection_existing_policy_is_examined_never_recreated` | core | Without it an existing collection is recreated or a healthy one fails on unset or unreadable policy values. | AGENTS.md#hard-boundaries |
| `test_ensure_collection_existing_mismatch_fails_closed_without_mutation` | core | Without it a mismatching live policy is accepted, recreated or indexed instead of refusing. | AGENTS.md#hard-boundaries |
| `test_upsert_chunks_payload_is_slimmed_without_embed_text` | core | Without it points double their storage with embed_text, or lose source_rev or the system_codes key. | docs/ingest.md#identity-contract |
| `test_physical_page_span_round_trips_payload_to_citation` | core | Without it unlabeled spanning chunks cite the wrong page range and citations fail validation. | issue #271 |
| `test_completion_code_round_trips_ingest_to_query_filter` | core | Without it completion codes never reach the stored payload and every code query filter matches nothing. | issue #591 |

## `tests/test_query_demo.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_execute_answer_citation_only_uses_parsed_body_state` | core | The query demo reports a citation-only answer as verified instead of incomplete or unverified. | issue #365 |
| `test_cli_answer_counts_the_same_effort_as_completion_near_window` | core | The demo token-count uses a different reasoning effort than completion, wrongly trimming or overflowing near the context window. | docs/agent.md#http-model-contract |
| `test_main_cli_answer_json_carries_verification_contract` | core | Exported JSON drops verification_state or script_review_required labels carried by the API. | issue #365 |
| `test_main_cli_answer_mode_zero_hits_renders_gracefully` | core | Answer mode crashes when retrieval returns no hits. | issue #181 |
| `test_resolve_runtime_settings_fallback_to_hash` | noise-kept | Demo auto-detect fails instead of falling back to hash mode when no endpoint is reachable. | AGENTS.md hard boundaries (hash mode opt-in) |
| `test_resolve_runtime_settings_probes_send_gateway_keys` | core | Discovery probes omit the per-leg virtual key so auto-detect fails through the gateway or sends the wrong key. | AGENTS.md hard boundaries (per-leg keys) |
| `test_resolve_runtime_settings_explicit_dense_dim_mismatch_fails_closed` | core | A wrong --dense-dim silently searches at the server's native dim. | issue #180 |
| `test_execute_query_emits_v1_search_span` | core | Raw query text enters trace span attributes. | issue #529 |
| `test_cli_limit_below_one_is_a_usage_error` | core | Without it --limit 0 or a negative value reaches search instead of exiting with a usage error. | docs/testing.md#evaluation |
| `test_main_cli_search_json_is_the_machine_readable_hit_list` | core | Without it the JSON search output that scripts consume loses hits, kind or timings. | docs/testing.md#evaluation |
| `test_search_html_escapes_manual_text_and_query` | core | Without it manual text, titles, message ids or the query inject markup into the saved inspection HTML that people open in a browser. | docs/testing.md#evaluation |
| `test_answer_text_says_whether_citations_were_explicit_or_inferred` | core | Without it a reader cannot tell citations the model wrote from ones inferred from bracketed excerpt indices, overstating how verified an answer is. | issue #365 |
| `test_answer_html_escapes_model_answer_citations_and_draft_script` | core | Without it model output (answer, citations, extracted script) injects markup into the saved answer HTML and the draft-script review warning is lost. | docs/testing.md#evaluation |
| `test_resolve_runtime_settings_explicit_cli_overrides_beat_multi_model_discovery` | core | Without it auto-discovery of served models overrides the collection, endpoints, models or dense dimension the operator passed explicitly, so queries hit the wrong collection or model. | AGENTS.md#hard-boundaries |
| `test_resolve_runtime_settings_explicit_model_resolves_to_served_basename` | core | Without it a short model name no longer resolves to the served id, so the request names a model the gateway rejects. | AGENTS.md#hard-boundaries |
| `test_resolve_runtime_settings_unverifiable_dense_dim_warns_and_keeps_the_explicit_value` | core | Without it an unverifiable --dense-dim is silently dropped or aborts instead of warning and keeping the operator's value. | issue #180 |
| `test_resolve_runtime_settings_otel_endpoint_precedence` | infra | Without it the tracing endpoint ignores the explicit flag, the off switch or the environment, so traces go to the wrong collector or leak when disabled. | issue #529 |
| `test_resolve_runtime_settings_explicit_cli_values_survive_unreachable_discovery` | core | Without it explicit model and endpoint flags are lost when discovery probes fail, so a query silently runs against defaults. | AGENTS.md#hard-boundaries |
| `test_resolve_runtime_settings_explicit_model_beats_a_lone_different_served_model` | core | Without it a gateway serving one different model replaces the model the operator asked for, so answers come from a model nobody selected. | AGENTS.md#hard-boundaries |

## `tests/test_query_filters.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_parse_query_identifiers` | core | Message ids, doc numbers and members typed in a question stop being extracted, so exact-code lookups degrade to semantic search. | docs/retrieval.md |
| `test_parse_query_case_folds_codes_and_docnos` | core | Operators who type lowercase codes (dfhac2006, sa38-0673-70) or mixed case get no exact filter and miss the right manual page. | issue #130; docs/retrieval.md |
| `test_parse_query_members_fold_to_canonical_case` | core | Lowercase or all-caps member names (ieasysxx) stop matching the canonical payload form, silently losing member-scoped hits. | issue #133; docs/retrieval.md |
| `test_parse_query_expands_docno_families` | core | Suffix-less or wildcard form numbers no longer expand to edition-suffixed doc_ids, so a query for SC23-6858 finds nothing. | issue #270; docs/retrieval.md |
| `test_parse_query_lowercase_prose_stays_nl` | core | Ordinary prose sprouts identifier filters, which empty results for plain natural-language questions. | docs/retrieval.md |
| `test_filters_carry_exactly_the_requested_identifier_and_scope_keys` | core | Filters gain or lose product/version/identifier keys, over-restricting or leaking results across scope. | docs/retrieval.md |
| `test_format_citation_round_trips_through_citation_line_re` | core | Citation strings produced by retrieval stop matching the validator, so every valid model citation is rejected as unverifiable. | docs/retrieval.md |
| `test_search_uses_query_batch_points_with_field_include_list` | core | Search stops sending dense and sparse legs as one batch with a restricted payload field list, inflating latency and payload size (embed_text leaks). | docs/retrieval.md |
| `test_search_identifier_weights_favor_bm25` | core | Exact-code queries stop favoring the BM25 leg, so the exact page ranks below semantic neighbors. | docs/retrieval.md |
| `test_search_nl_weights_equal` | core | Natural-language queries lose equal dense/sparse weighting and tie order, shifting ranking. | docs/retrieval.md |
| `test_rrf_fuse_tie_order_is_deterministic` | core | Tied fusion scores reorder between runs, making results and citations nondeterministic. | docs/retrieval.md |
| `test_rrf_fuse_type_boosts_promote_syntax_on_sparse_leg` | core | Syntax boost stops applying only to the BM25 leg, or leaks onto dense-only hits, distorting ranking. | issue #216; docs/retrieval.md |
| `test_rrf_fuse_no_boosts_is_byte_identical` | core | Boost flag off or empty changes legacy fusion output, breaking flag-off safety. | issue #216; docs/retrieval.md |
| `test_search_syntax_boost_setting_reaches_ranking` | core | The rrf_sparse_boost_syntax setting stops reaching search ranking, so the operator knob does nothing. | issue #216; docs/retrieval.md |
| `test_diversify_hits_prevents_page_monopoly` | core | One page or document fills the whole result window, hiding other manuals from the user. | docs/retrieval.md |
| `test_async_search_offloads_blocking_embed_from_event_loop` | core | Blocking embed calls run on the event loop, stalling every concurrent request in the agent. | review S1; docs/retrieval.md |
| `test_search_filter_fallback_recovers_unfiltered_hits` | core | An exact doc-id stem that matches zero filtered points no longer retries unfiltered, returning nothing for valid queries. | docs/retrieval.md |
| `test_empty_code_lookup_excludes_annotated_sibling_before_prefetch` | core | A lookup for one message or system code falls back to pages annotated with a different code, answering about the wrong message. | docs/retrieval.md |
| `test_code_fallback_retains_matching_and_multi_code_context` | core | The relaxed code filter rejects pages that carry the requested code among several, or admits pages with only a sibling code. | docs/retrieval.md |
| `test_code_fallback_requires_compatibility_in_each_requested_field` | core | Fallback admits pages incompatible with one of several requested code fields, mixing up messages and system codes. | docs/retrieval.md |
| `test_search_filter_fallback_adversarial_wrappings` | core | Quotes, markdown or CITATIONS-prefixed identifiers shield the code from the filter and its fallback, losing the exact hit. | docs/retrieval.md |
| `test_search_fails_closed_inside_running_loop` | core | Calling sync search inside a running event loop deadlocks instead of raising a directing error. | docs/retrieval.md |
| `test_search_filter_fallback_retains_explicit_scope_and_excludes_out_of_scope` | core | Identifier fallback drops the caller product/version scope and returns out-of-scope manuals. | tests/hazards/critical.json |
| `test_search_filter_fallback_returns_empty_when_no_points_match_scope` | core | When no points exist in the caller scope, fallback relaxes scope and leaks other products instead of returning nothing. | tests/hazards/critical.json; docs/retrieval.md |
| `test_search_pure_scope_filter_zero_matches_no_redundant_fallback` | core | A scope-only query with zero matches pays a redundant second retrieval round trip. | docs/retrieval.md |
| `test_format_citation_physical_page_fallback` | core | Unlabeled manuals lose their physical page in citations, or the fallback tail no longer validates against the citation validator. | issue #271; docs/retrieval.md |
| `test_to_hit_projects_physical_page_span` | core | Stored physical page spans stop reaching hits, or malformed values crash or corrupt citations. | issue #271; docs/retrieval.md |
| `test_diversify_buckets_by_physical_page_not_printed_label` | core | Unlabeled or repeated folios share one bucket so the per-page cap silently becomes a per-document cap and competing manuals are crowded out. | issue #271; docs/retrieval.md |
| `test_search_cites_physical_pages_for_unlabeled_manual` | core | The query path stops carrying physical page fields end to end, so users get page-less citations for unlabeled manuals. | issue #271; docs/retrieval.md |
| `test_search_fallback_retains_single_key_scope` | core | A product-only or version-only scope is widened or invented to the other key during fallback, leaking out-of-scope pages. | tests/hazards/critical.json; docs/retrieval.md |
| `test_search_one_empty_leg_does_not_trigger_fallback` | core | A single empty leg triggers an unneeded fallback retry, changing results and doubling cost. | docs/retrieval.md |
| `test_scope_retention_against_real_local_qdrant_filter_semantics` | core | Hand-rolled fakes hide a real filter bug: scope retention fails against actual Qdrant filter semantics (exact, fallback, out-of-scope doc id, empty scope, conjunctive ids). | tests/hazards/critical.json; docs/retrieval.md |
| `test_async_scope_retention_against_real_local_qdrant` | core | The async transport loses scope on fallback when run against a real Qdrant client. | tests/hazards/critical.json; docs/retrieval.md |

## `tests/test_render_report.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_html_reports_escape_untrusted_values` | core | Query text or environment values in generated HTML reports are rendered unescaped, allowing script injection. | AGENTS.md hard boundaries |
| `test_compare_bench_regression_and_fail_flag` | gate-control | A bench regression compared in reports does not set the regression flag or exit code under --fail-on-regression. | docs/testing.md#reports |
| `test_compare_eval_mixed_directional_change_regression` | gate-control | A query-level degradation is masked by an improved aggregate and the compare gate passes. | docs/testing.md#reports |
| `test_module_tasks_execute_checkout_with_foreign_editable` | infra | Report/answer Tasks execute a foreign editable checkout's module instead of this checkout's code. | docs/task-runner.md |
| `test_report_task_selects_recorded_inputs` | infra | Report Tasks use ambient or wrong report/baseline/output paths, or shell-evaluate special characters. | docs/task-runner.md |
| `test_report_invalid_input_preserves_output_and_recovers` | infra | A missing or corrupt input overwrites the previous report output, or the next ordinary run does not recover. | docs/task-runner.md |
| `test_report_bundle_defaults_require_explicit_opt_in` | infra | Bundle default paths apply without an explicit bundle-dir or empty path overrides write into the bundle. | docs/task-runner.md |
| `test_empty_report_bundle_preserves_root_relative_read_contract` | infra | An empty bundle-dir silently reads or writes unintended host-root files. | docs/task-runner.md |
| `test_markdown_eval_report_escapes_cells_and_labels_every_row_kind` | core | Without it the markdown evidence a release reads can have a stray pipe shift table columns, or hide error and abstain rows and baseline deltas of the evaluation. | docs/testing.md#reports |
| `test_non_html_bench_report_carries_every_measured_value` | core | Without it the text and markdown benchmark reports can drop or mislabel the RSS, Qdrant, RPS and p95 numbers acceptance evidence is read from. | docs/testing.md#reports |
| `test_compare_eval_reports_routing_shifts_and_population_changes_in_every_format` | core | Without it an evaluation comparison can silently pass when a query was dropped from the evaluated set or a query changed routing, in any output format. | docs/testing.md#reports |
| `test_compare_bench_throughput_collapse_is_a_regression_but_a_modest_drop_is_not` | core | Without it a throughput collapse (higher-is-better metric) passes the benchmark comparison and --fail-on-regression exits 0. | docs/testing.md#reports |

## `tests/test_replay.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_replay_pool_orders_legs_by_rank_and_defaults_optional_fields` | infra | Recorded pools rebuild with wrong leg order or lose CE-less and sparse-only rows, so local ranking tuning reads a wrong pool. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |
| `test_replay_schema_rejects_corrupt_capture` | infra | A corrupt capture is silently ranked instead of rejected, so tuning conclusions are drawn from bad data. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |

## `tests/test_replay_sweep.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_replay_alpha_one_follows_ce_alpha_zero_keeps_rrf_order` | infra | The sweep misapplies alpha or scores an unscored pool, so a tuning verdict reflects the wrong ranking. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |
| `test_replay_limits_truncate_and_refuse_beyond_the_recorded_pool` | infra | Configs deeper than the recorded pool are scored as if valid instead of refused or skipped. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |
| `test_replay_split_modes_merge_differently` | infra | Replay ignores the recorded split mode, merging comparative and diagnostic legs wrongly. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |
| `test_score_hits_doc_level_section_level_and_must_not` | infra | Sweep recall, MRR, trap violations or section-level scoring are computed wrongly, so A/B deltas mislead. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |
| `test_summarize_means_classes_and_violations` | infra | Sweep summaries mis-average per-class metrics or undercount violations. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |
| `test_rerank100_replays_a_deep_scored_pool` | infra | The rerank100 config fails on a deep pool whose cross-encoder scores stop at 100. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |
| `test_replay_headings_change_scoring_not_ranking` | infra | Joining headings alters ranking, or section-level scoring is not applied. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |
| `test_join_headings_maps_ids_and_fails_closed_on_a_different_ingest` | infra | Headings from a different ingest are joined to captured ids, producing a wrong section-level verdict. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |
| `test_replay_page_cap_buckets_by_physical_page_like_live` | infra | Replay diversification diverges from live bucketing, so tuned caps do not transfer to production. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |
| `test_join_headings_refuses_a_moved_physical_page` | infra | Headings are joined even when the physical page moved, mis-scoring sections. | docs/eval.md#61-record-replay-ab-tune-prod-ranking-from-local |

## `tests/test_representation.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_build_manifest_literal` | core | Without it the stored representation contract silently changes shape and every published generation reads as drifted. | docs/ingest.md#metadata-contract |
| `test_manifest_digest_and_point_id_literals_are_stable` | core | Without it the manifest digest or point id drifts and stored contracts are lost or re-embedded. | docs/ingest.md#metadata-contract |
| `test_manifest_digest_sensitivity_matrix` | core | Without it a contracted field stops moving the digest and a changed contract is judged identical. | docs/ingest.md#metadata-contract |
| `test_manifest_digest_dim_fallback_never_raises` | core | Without it dry runs in unconfigured environments crash on provenance. | docs/ingest.md#metadata-contract |
| `test_blank_context_llm_model_is_absent` | core | Without it the ingest Job's empty CONTEXT_LLM_MODEL differs from the agent's unset value and every generation reads re-embed required. | docs/ingest.md#metadata-contract |
| `test_manifest_write_read_roundtrip_and_unreadable_states` | core | Without it the manifest is duplicated on rewrite or an absent or corrupt manifest raises instead of reading as legacy. | docs/ingest.md#metadata-contract |
| `test_completion_record_carries_manifest_digest` | core | Without it completion markers lose the contract they were certified under. | docs/ingest.md#metadata-contract |
| `test_weights_revision_matches_pin_file` | infra | Without it the sparse weights revision default drifts from the pinned file and the air-gapped model check fails. | AGENTS.md#hard-boundaries |
| `test_model_revision_env_override_recorded` | core | Without it the operator-attested model revision is not recorded in the contract. | docs/ingest.md#metadata-contract |
| `test_run_writes_manifest_and_logs` | core | Without it a real run does not commit its manifest, skips the pending-then-committed lifecycle, or rewrites on a steady rerun. | docs/ingest.md#metadata-contract |
| `test_dry_run_record_stamps_parent_manifest_view` | core | Without it worker-stamped and parent-computed contract digests disagree and enforcement misjudges inventory. | docs/ingest.md#metadata-contract |
| `test_fingerprint_identity_policy` | core | Without it a revision, dimension or rules change keeps the old generation identity and an old marker certifies re-embedded vectors. | docs/ingest.md#metadata-contract |
| `test_fingerprint_covers_settings_driven_reembed_fields` | core | Without it a settings-driven re-embed input does not change generation identity. | docs/ingest.md#metadata-contract |

## `tests/test_representation_gate.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_compare_each_reembed_field_rejects` | core | Without it a contracted re-embed field changes undetected and mismatched vectors mix. | docs/ingest.md#metadata-contract |
| `test_compare_manifests_compatible_and_record_only` | core | Without it identical or blank-normalized contracts read as drift, the record-only prefix triggers re-embed, or re-embed drift is hidden by it. | docs/ingest.md#metadata-contract |
| `test_attestation_rejects_a_blank_revision_before_store_contact` | core | Without it a vllm run proceeds with an unattested model revision or touches the store before refusing. | docs/ingest.md#metadata-contract |
| `test_attestation_accepts_a_declared_revision_and_exempts_hash_mode` | core | Without it valid attested runs and hash-mode runs are wrongly refused. | docs/ingest.md#metadata-contract |
| `test_preflight_proceeds_for_empty_compatible_and_record_only_targets` | core | Without it empty, compatible or prefix-drifted targets are refused ingest or the drift is not reported. | docs/ingest.md#metadata-contract |
| `test_preflight_same_dim_new_revision_rejects_with_migration_path` | core | Without it same-dimension vectors from a different attested revision are skipped against, with no migration path named. | docs/ingest.md#metadata-contract |
| `test_preflight_legacy_or_corrupt_manifest_rejects_with_attest_path` | core | Without it unversioned or corrupt-manifest collections are written into without an attest path. | docs/ingest.md#metadata-contract |
| `test_rekey_carries_contract_to_staging_id` | core | Without it the cloned staging generation loses its contract or its vector, and the manifest is unreadable at serving. | tests/hazards/critical.json |
| `test_rekey_inherits_live_state_verbatim` | core | Without it cloning launders a migration or a pending contract into a committed one, or writes when the source is absent. | docs/ingest.md#metadata-contract |
| `test_serving_outcome_per_stored_contract` | core | Without it serving treats drifted, legacy, pending or unreadable contracts as servable, or touches the store with an unattested revision. | docs/ingest.md#metadata-contract |
| `test_begin_commit_lifecycle` | core | Without it a migration is not pending before document work, commits early, or a steady rerun writes. | tests/hazards/critical.json |
| `test_begin_record_only_drift_commits_inline` | core | Without it prefix-only drift opens a needless pending window that blocks serving. | docs/ingest.md#metadata-contract |
| `test_preflight_pending_contract_rejects_with_resume_path` | core | Without it an interrupted migration looks compatible and is skipped against. | docs/ingest.md#metadata-contract |

## `tests/test_request_bounds.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_controller_never_exceeds_active_or_queue_and_is_fifo` | core | Without it admission could exceed the concurrency cap or queue limit, or serve waiters out of order. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_controller_release_is_idempotent` | core | Without it a double release would corrupt the active count and admit more requests than allowed. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_controller_queue_timeout_leaves_no_waiter_and_no_leak` | core | Without it timed-out waiters would leak slots and eventually refuse all traffic. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_controller_cancelled_waiter_that_was_just_granted_returns_the_slot` | core | Without it a waiter cancelled right after being granted would hold a slot forever. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_default_settings_impose_no_limit` | core | Without it default settings would start limiting traffic and change behavior for deployments that never opted in. | AGENTS.md hard boundary: no silent default changes |
| `test_overload_returns_stable_code_does_no_work_and_recovers` | core | Without it overload would run model work anyway, use an unstable error code, or not recover for the next request. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_every_product_endpoint_is_admitted` | core | Without it an endpoint could bypass admission and overload the agent. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_probes_are_never_admitted_or_limited` | core | Without it health probes could be queued or rejected under load and get the pod killed. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_bounded_queue_admits_in_order_and_refuses_beyond_it` | core | Without it the queue would be unbounded or reorder requests. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_queue_wait_expiry_is_the_overload_code` | core | Without it clients waiting too long would get an unstable error instead of the overload code. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_stream_holds_slot_until_disconnect_then_releases_exactly_once` | core | Without it streams would release their slot early or twice, breaking the concurrency cap. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_slot_is_released_when_the_request_fails` | core | Without it failed requests would leak slots until the agent refuses everything. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_deadline_cancels_the_leg_cleanly_and_the_next_request_runs` | core | Without it a request past its total deadline would keep running work or block the next request. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_deadline_bounds_the_queue_wait_too` | core | Without it queue wait would not count against the total deadline and requests could exceed it. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_deadline_mid_stream_ends_with_terminal_error_frame_and_frees_the_slot` | core | Without it a stream hitting its deadline would end silently as if complete or hold its slot. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_oversize_query_is_rejected_before_any_model_call` | core | Without it oversize queries would reach the embedder and fail as upstream errors or waste model work. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_embedder_refusal_backstop_is_a_422_not_an_upstream_error` | core | Without it an embedder input-size refusal would surface as a 5xx upstream failure instead of a client error. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_default_embed_bound_is_off_and_long_queries_pass_unchanged` | core | Without it the default embed bound could start truncating or rejecting long queries. | AGENTS.md hard boundary: no silent default changes |
| `test_console_shares_the_admission_slot_and_deadline` | core | Without it the console route would bypass admission and deadlines applied to API routes. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_console_stream_deadline_is_a_terminal_error_frame` | core | Without it a console stream hitting its deadline would end without an error signal. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_console_non_stream_deadline_renders_the_fixed_banner_and_frees_the_slot` | core | Without it a console deadline would show raw error text or leak the slot. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_admission_metrics_are_bounded_and_fail_open` | core | Without it metrics labels could grow without bound or a metrics fault could reject requests. | docs/agent.md#1-endpoints |
| `test_exact_storage_read_shares_admission_and_releases_for_next_request` | core | Without it exact-evidence reads would bypass admission or leak their slot on success, refusal, deadline or disconnect. | docs/evidence-contract.md#evidence-contract |
| `test_stream_deadline_closes_stalled_asgi_send_and_admits_next_queued_request` | noise-kept | Without it a client that stops reading could hold a slot past the deadline and block queued requests; uses short real deadlines. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_producer_deadline_terminal_frames_share_one_absolute_delivery_grace` | noise-kept | Without it terminal frames after a producer deadline could each get a fresh delivery grace and extend the total time; timing-bounded. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |
| `test_deadline_and_admission_include_the_closing_asgi_body_send` | noise-kept | Without it the final body send could run outside the deadline and admission slot; timing-bounded. | docs/agent.md#6a-request-admission-total-deadline-and-embed-input-bound-issue-374 |

## `tests/test_rerank.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_format_rerank_text_shapes` | core | Reranker passages lose chunk-type tags, message ids or table labels, changing cross-encoder scoring for those chunks. | docs/retrieval.md |
| `test_format_rerank_text_never_exceeds_cap_for_any_shape` | core | Oversize chunks exceed the scorer window and the reranker returns HTTP 400, failing the search. | docs/retrieval.md |
| `test_format_rerank_text_cap_keeps_template_label_whole` | core | The length cap truncates the header, heading or Table label instead of only the body tail. | docs/retrieval.md |
| `test_hash_reranker_deterministic` | core | Hash-mode reranker becomes nondeterministic or scores empty text above zero, breaking hermetic rank tests and dev runs. | docs/retrieval.md |
| `test_rerank_candidates_reorders_and_attaches_score` | core | Cross-encoder scores stop reordering candidates or stop being attached to hits. | docs/retrieval.md |
| `test_rerank_candidates_applies_top_k` | core | top_k truncation after rerank is ignored, returning more hits than requested. | docs/retrieval.md |
| `test_rerank_alpha_regimes` | core | Fusion alpha blending breaks: alpha 1 stops following the cross-encoder, 0 stops keeping RRF, intermediate or out-of-range values misrank. | docs/retrieval.md |
| `test_rerank_alpha_zero_rrf_tie_ignores_ce` | core | At alpha 0 the cross-encoder reorders RRF ties, breaking the ablation guarantee. | docs/retrieval.md |
| `test_rerank_alpha_flat_leg_abstains` | core | A constant score leg dominates blending instead of abstaining, producing arbitrary order. | docs/retrieval.md |
| `test_search_rerank_alpha_zero_keeps_rrf_order` | core | The rerank_fusion_alpha setting stops reaching search ranking. | docs/retrieval.md |
| `test_rerank_candidates_raises_on_length_mismatch` | core | A scorer returning the wrong score count silently misaligns scores with candidates instead of failing. | docs/retrieval.md |
| `test_rerank_flag_off_is_byte_identical` | core | With reranking disabled, search changes behavior or reports rerank scores. | docs/retrieval.md |
| `test_http_reranker_score_leg_sorts_by_index_and_sends_auth_only_when_keyed` | core | Out-of-order scores are misassigned, or the gateway key is missing when set or sent as an empty header when unset. | AGENTS.md; docs/agent.md#http-model-contract |
| `test_http_reranker_malformed_200_missing_data_falls_back` | core | A 200 response without data raises out of the reranker instead of using the fallback endpoint. | docs/agent.md#http-model-contract |
| `test_http_reranker_length_mismatch_raises` | core | Both endpoints returning wrong-length results are accepted as valid scores, misranking results. | docs/agent.md#http-model-contract |
| `test_http_reranker_rerank_first_falls_back_to_score` | core | With gateway endpoint order, a dead /v1/rerank fails the search instead of falling back to /v1/score. | docs/agent.md#http-model-contract |
| `test_http_reranker_rerank_first_both_spent_fails_closed` | core | When both endpoints are unusable, the reranker returns empty scores instead of failing closed. | docs/agent.md#http-model-contract |
| `test_http_reranker_4xx_both_legs_retries_batch_with_cut_passages` | core | A batch rejected for window length is not re-scored with cut passages, so long chunks fail the whole search. | docs/agent.md#http-model-contract |
| `test_http_reranker_4xx_retry_only_affects_failing_batch` | core | The cut-passage retry rewrites passages of batches that were not rejected. | docs/agent.md#http-model-contract |
| `test_http_reranker_normal_requests_byte_identical` | core | Normal-length passages are cut or re-sent, changing scores for healthy requests. | docs/agent.md#http-model-contract |
| `test_http_reranker_not_retried_with_cut_text` | core | 5xx, transport and one-leg 4xx failures are retried with cut text, masking outages and breaking the retry contract. | docs/agent.md#http-model-contract |
| `test_http_reranker_retry_also_failing_raises_as_before` | core | A failing retry loops or is swallowed instead of raising after exactly one retry. | docs/agent.md#http-model-contract |
| `test_build_reranker_dispatch` | core | Settings resolve to the wrong reranker (hash vs HTTP vs none) or a misconfigured deployment does not fail loudly. | AGENTS.md; docs/agent.md#http-model-contract |
| `test_reranker_config_key_covers_every_build_field` | core | Changing a reranker setting reuses a stale memoized reranker built for the old values. | issue #156 |
| `test_reranker_memo_reuses_equal_values_across_instances` | core | The reranker is rebuilt on every Settings object, or a stale one is reused after a value change. | issue #156 |
| `test_diversify_hits_respects_rerank_scores` | core | Diversification backfills by RRF score and ignores rerank scores, undoing the rerank. | docs/retrieval.md |
| `test_trap_query_bypasses_explicit_reranker` | core | Trap queries are reranked, letting trap-worded chunks outrank honest ones and costing an extra fetch. | issue #113; docs/retrieval.md |
| `test_trap_query_bypasses_flag_built_reranker` | core | The trap bypass does not apply when the reranker is built from settings. | issue #113; docs/retrieval.md |
| `test_identifier_query_bypasses_reranker` | core | Exact-code queries are reranked, so the cross-encoder promotes confident definitions of the wrong message. | issue #117; docs/retrieval.md |
| `test_answerable_query_still_reranks` | core | The trap/identifier gate silently disables reranking for normal questions. | issue #113; docs/retrieval.md |
| `test_probe_reranker_success` | core | Startup probe fails a healthy rerank endpoint. | docs/retrieval.md |
| `test_probe_reranker_failure_reports_error_type_only` | core | The startup probe logs upstream exception text or secrets, violating the logs contract. | AGENTS.md |

## `tests/test_review_tooling.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `TestProfileClassification::test_changed_paths_select_profile_and_services` | infra | A changed-path set is reviewed in a weaker profile or with missing services, so a storage/http/tracing/deploy change gets docs-level verification. | docs/live-stack.md#verification-minimums |
| `TestProfileClassification::test_unmapped_unreadable_or_shared_changes_fail_closed_to_full` | infra | An empty diff, unknown file or shared test fixture is downgraded to a docs review instead of full verification. | docs/testing.md#evidence-design |
| `TestProfileClassification::test_markdown_is_prose_unless_it_is_an_instruction_or_under_tests` | infra | Instruction files such as AGENTS.md pass as prose-only, or docs under service trees start live services. | docs/testing.md#evidence-design |
| `TestProfileClassification::test_evaluation_package_and_delegates_select_executable_tooling_lanes` | infra | Changes to the eval package merge without the simulation, gate_l1, hazards and unit lanes all passing. | docs/live-stack.md#verification-minimums |
| `TestProfileClassification::test_moved_evaluation_and_relocated_contracts_keep_every_obligation` | infra | A rename of eval code or a relocated chart/policy file drops its HA, packaging or eval_retrieval obligations from the mixed diff. | docs/live-stack.md#verification-minimums |
| `TestProfileClassification::test_real_git_rename_retains_both_paths_and_literal_whitespace` | infra | A renamed file with whitespace in its path loses its old path, so the old owner category is never selected for verification. | docs/live-stack.md#verification-minimums |
| `TestReviewResultValidator::test_valid_payload_is_ready_in_every_accepted_embedding` | infra | A valid reviewer payload in a comment block or json fence is rejected, or a clean one is not marked ready for the maintainer. | docs/agent-workflow.md#review-handoff |
| `TestReviewResultValidator::test_missing_malformed_or_forged_payloads_fail_closed` | gate-control | A missing, malformed, enum-invalid or forged-ready review payload is accepted as ready for the maintainer. | docs/agent-workflow.md#review-handoff |
| `TestReviewResultValidator::test_unresolved_or_author_declined_findings_block_readiness` | infra | A candidate with an unresolved or author-declined material finding is reported ready for merge. | AGENTS.md#code-review-rules |
| `TestReviewResultValidator::test_obsolete_head_or_dirty_tree_is_not_current` | infra | A review of an old head or dirty tree is accepted for the current candidate. | docs/agent-workflow.md#review-handoff |
| `TestCandidateAcceptanceSummary::test_lane_evaluation_states` | infra | A skipped or missing required lane is reported as passed, or an unselected lane blocks acceptance. | docs/live-stack.md#verification-minimums |
| `TestCandidateAcceptanceSummary::test_selected_lane_that_did_not_pass_blocks_acceptance` | gate-control | A required lane that was skipped, missing or failed (including agent probes with no CI producer) still lets acceptance report all prerequisites met. | docs/live-stack.md#verification-minimums |
| `TestCandidateAcceptanceSummary::test_unselected_lane_never_blocks_a_docs_only_acceptance` | infra | Docs-only PRs can never reach acceptance because path-filtered lanes with no producer are demanded. | docs/live-stack.md#verification-minimums |
| `TestReviewToolingCLI::test_cli_profile_writes_manifest_lanes_and_workflow_outputs` | infra | The workflow reads the wrong profile, lane list or needs_* outputs from the selector CLI and runs the wrong jobs. | docs/live-stack.md#verification-minimums |
| `TestReviewToolingCLI::test_cli_validate_review_exit_codes` | gate-control | The reviewer job passes with a missing, unparseable or forged payload, or fails on a valid changes_required verdict. | docs/testing.md#evidence-design |
| `TestReviewToolingCLI::test_cli_summarize_acceptance_check_flag` | infra | summarize-acceptance --check exits 0 with failed technical evidence, or requires a human review file. | docs/testing.md#evidence-design |
| `TestExecutionAttribution::test_wrong_clean_checkout_is_unverified_in_process_and_through_the_cli` | gate-control | A review claiming the newer candidate while checked out at the base commit is accepted as attributed. | docs/agent-workflow.md#review-handoff |
| `TestExecutionAttribution::test_head_checkout_or_head_checkout_with_test_merge_execution_passes` | infra | The legitimate PR-head checkout with a test-merge execution commit is wrongly refused, blocking every reviewer run. | docs/agent-workflow.md#review-handoff |
| `TestExecutionAttribution::test_head_must_be_ancestor_of_execution` | gate-control | An execution commit that does not contain the reviewed head is accepted. | docs/agent-workflow.md#review-handoff |
| `TestReadinessProbes::test_probe_verdict_depends_on_status_and_agent_contract` | infra | A degraded agent (503 or status degraded with qdrant true) counts as ready and the review runs against a broken service. | docs/testing.md#evidence-design |
| `TestAcceptanceUnionAndReviewerAuthority::test_full_profile_keeps_packaging_when_deploy_present` | infra | A cross-layer change including deploy files merges without the packaging lane. | docs/live-stack.md#verification-minimums |
| `TestAcceptanceUnionAndReviewerAuthority::test_human_review_neither_changes_nor_replaces_technical_verification` | gate-control | A review approval or job success replaces missing technical evidence, or a not-ready review changes the technical verdict. | AGENTS.md#code-review-rules |
| `TestCiUnitPartition::test_native_coverage_refuses_canary_narrowing_and_collection_hooks` | gate-control | A shard run narrowed by -k, PYTEST_ADDOPTS or a collection hook yields green unit evidence without running the full suite. | docs/testing.md#evidence-design |
| `TestCiUnitPartition::test_shards_cover_each_selected_case_once_and_preserve_failure` | infra | The four-VM unit split drops or duplicates a parametrized case, or a failing case no longer fails its shard. | docs/testing.md#evidence-design |
| `TestCiUnitPartition::test_four_native_shards_preserve_exact_union_and_reject_partial_or_forged_coverage` | gate-control | Three of four shards, a duplicated shard or a shrunken forged record is accepted as complete unit coverage. | docs/testing.md#evidence-design |
| `TestTaskCiConsumers::test_task_edits_trigger_context_and_load_on_push_and_pull_request` | infra | Editing Taskfiles or the runner does not trigger the context and load workflows, so dispatch changes merge unchecked. | docs/task-runner.md |
| `TestTaskCiConsumers::test_four_unit_vms_preserve_a_fail_closed_test_status` | gate-control | A failed, cancelled or skipped unit shard leaves the aggregate test status green. | docs/testing.md#evidence-design |
| `TestTaskCiConsumers::test_load_step_preserves_failure_skip_and_no_tests_guards` | gate-control | The load tier reports success after a skipped test, a "no tests ran" result or a nonzero Task status. | docs/live-stack.md#verification-minimums |
| `TestTaskCiConsumers::test_offline_unit_job_requires_local_archive_before_running_tests` | infra | The GitLab offline unit job runs tests without verified local Task and Helm archives or ignores a failing doctor. | AGENTS.md#hard-boundaries |
| `TestNativeExecutionEvidence::test_junit_counts_actual_records_not_declared_totals` | gate-control | Declared suite totals or a DOCTYPE report inflate the executed-test count used as acceptance evidence. | docs/testing.md#evidence-design |
| `TestNativeExecutionEvidence::test_real_pytest_xfail_and_skip_are_not_passes` | gate-control | An xfail, skip or strict-xpass run is recorded as passing evidence. | docs/testing.md#evidence-design |
| `TestNativeExecutionEvidence::test_unittest_expected_failure_and_skip_are_not_passes` | gate-control | unittest expectedFailure or skip results are counted as a clean context-check pass. | docs/testing.md#evidence-design |
| `TestNativeExecutionEvidence::test_native_command_requires_nonzero_fresh_passing_results` | gate-control | A zero-test, skipped, errored or stale-report run is recorded as passed evidence and a retry reuses the old report. | docs/testing.md#evidence-design |
| `TestNativeExecutionEvidence::test_identity_binds_real_merge_parents_event_and_clean_checkout` | gate-control | Evidence is issued for a test merge that does not bind the PR head and base, or from a dirty policy checkout. | docs/agent-workflow.md#review-handoff |
| `TestNativeExecutionEvidence::test_identity_receipt_cannot_claim_results_and_identity_movement_cannot_pass` | gate-control | An identity-only receipt carries test results, or a job whose execution commit moved mid-run reports passed. | docs/agent-workflow.md#review-handoff |
| `TestNativeEvidenceConsumer::test_four_native_job_receipts_require_the_complete_raw_junit_union` | gate-control | Unit acceptance succeeds from receipts whose shard union is incomplete or disagrees with the raw junit. | docs/testing.md#evidence-design |
| `TestNativeEvidenceConsumer::test_unit_layout_comes_from_approved_workflow_not_receipts` | gate-control | A candidate receipt chooses its own shard layout instead of the approved workflow, weakening unit coverage. | docs/testing.md#evidence-design |
| `TestNativeEvidenceConsumer::test_collector_requires_all_native_shards_and_never_reuses_an_older_green_run` | gate-control | Acceptance reuses an older green run, a forged producer, or a changed verifier input instead of the latest attempt. | docs/testing.md#evidence-design |
| `TestNativeEvidenceConsumer::test_unit_receipt_rejects_incomplete_selection_and_same_count_substitutions` | gate-control | A receipt with narrowed selection, swapped test ids of equal count or an unexpected plugin is accepted. | docs/testing.md#evidence-design |
| `TestNativeEvidenceConsumer::test_native_agent_probes_require_all_named_transport_witnesses` | gate-control | Agent-probe acceptance passes with a missing, duplicated or foreign-module transport witness test. | docs/live-stack.md#verification-minimums |
| `TestNativeEvidenceConsumer::test_rejects_wrong_actor_run_attempt_workflow_candidate_and_failed_execution` | gate-control | A receipt from the wrong actor, attempt, workflow, candidate or a failed job is accepted as evidence. | docs/agent-workflow.md#review-handoff |
| `TestNativeEvidenceConsumer::test_missing_zero_skipped_or_different_raw_test_evidence_cannot_pass` | gate-control | A green receipt covers different, empty or skipped raw junit than it claims. | docs/testing.md#evidence-design |
| `TestNativeEvidenceConsumer::test_xfail_shard_report_cannot_pass_even_with_an_honest_or_forged_receipt` | gate-control | A shard whose junit contains an xfail passes even with honest or forged zero-skip counts. | docs/testing.md#evidence-design |
| `TestNativeEvidenceConsumer::test_candidate_cannot_substitute_workflow_or_a_different_merge_with_same_parents` | gate-control | A candidate-supplied workflow or a different merge with the same parents passes as the tested execution. | docs/agent-workflow.md#review-handoff |
| `TestNativeEvidenceConsumer::test_regenerated_merge_requires_api_tree_and_exact_parents_retaining_tested_sha` | gate-control | A regenerated GitHub merge is accepted without an identical tree and exact ordered parents. | docs/agent-workflow.md#review-handoff |
| `TestNativeEvidenceConsumer::test_hazard_receipts_require_each_approved_behavioral_kill` | gate-control | The hazards lane passes though a hazard survived, was killed by the wrong cause, or the catalogue was incomplete. | tests/hazards/critical.json |
| `TestNativeEvidenceConsumer::test_native_pagination_reads_later_pages_and_refuses_inconsistent_results` | gate-control | Jobs or artifacts beyond the first API page are ignored or an inconsistent page set is trusted. | docs/agent-workflow.md#review-handoff |
| `TestNativeEvidenceConsumer::test_artifact_tampering_paths_duplicates_and_symlinks_are_rejected` | gate-control | A tampered, path-traversing, duplicated or symlinked evidence archive is read by the acceptance collector. | docs/agent-workflow.md#review-handoff |
| `TestAcceptanceSnapshot::test_paginated_pr_files_preserve_rename_bytes_and_refuse_truncation` | infra | A PR with more than one page of files or a renamed file is classified from a truncated path list. | docs/live-stack.md#verification-minimums |
| `TestAcceptanceSnapshot::test_recheck_refuses_candidate_and_attempt_movement` | gate-control | A result computed for an old head, base, merge commit or run attempt is published after the candidate moved. | docs/agent-workflow.md#review-handoff |
| `TestAcceptanceSnapshot::test_bulk_publisher_reports_candidate_failures_without_failing_reconciliation` | infra | One failing candidate fails the whole bulk reconciliation, or a single-PR publish failure exits 0. | docs/agent-workflow.md#review-handoff |
| `TestAcceptanceSnapshot::test_read_only_cli_never_emits_success_after_recheck_failure` | gate-control | The read-only acceptance CLI reports success after the recheck failed, or leaks upstream error text. | AGENTS.md#hard-boundaries |
| `TestAcceptanceSnapshot::test_native_summary_ignores_human_comments_and_draft_state` | infra | Human comments, review state or draft status change the technical acceptance verdict. | AGENTS.md#code-review-rules |
| `TestAcceptanceSnapshot::test_publisher_pending_precedes_collection_and_stale_result_is_failure` | infra | A check shows success before evidence is collected, or a stale result is published as passed. | docs/agent-workflow.md#review-handoff |
| `TestAcceptanceSnapshot::test_publisher_workflow_keeps_candidate_data_out_of_privileged_execution` | infra | The privileged acceptance workflow executes candidate-controlled data or runs outside approved main with write tokens. | AGENTS.md#hard-boundaries |
| `TestAcceptanceSnapshot::test_gitlab_l1_note_appends_literal_body_without_claiming_marker_ownership` | infra | The GitLab gate-l1 note injects a literal @file form body or claims the marker owned by another publisher. | docs/testing.md#evidence-design |
| `ReviewTemplateTests::test_template_binds_current_merge_without_approving_or_writing` | infra | A generated review template binds a moved head or base, or arrives pre-approved. | docs/agent-workflow.md#review-handoff |
| `ReviewTemplateTests::test_live_base_requires_exact_commit_ref_and_encodes_branch` | gate-control | A base branch that moved, is a tag, or has special characters passes the live-base check. | docs/agent-workflow.md#review-handoff |
| `ReviewTemplateTests::test_template_cli_refuses_publication_and_bulk_selection` | infra | The template CLI publishes comments or runs across all open PRs and writes human review surface. | AGENTS.md#hard-boundaries |
| `ReviewTemplateTests::test_failed_acceptance_still_exports_identity_template_and_check_link` | infra | A failed acceptance run loses the reviewer identity template and the check link, or exports a template for a moved head. | docs/agent-workflow.md#review-handoff |
| `ReviewTemplateTests::test_moved_template_is_not_exported_for_old_check_head` | infra | A template for a different head than the published check is exported. | docs/agent-workflow.md#review-handoff |
| `ReviewTemplateTests::test_template_comments_append_deduplicate_and_preserve_human_text` | infra | Template comments overwrite human text or get duplicated on re-run. | docs/agent-workflow.md#review-handoff |
| `ReviewTemplateTests::test_comment_write_boundary_cannot_edit_reviews_or_other_repositories` | infra | The publisher token path can edit reviews, patch comments or write to another repository. | AGENTS.md#hard-boundaries |
| `TestVerifierUpdateDecision::test_approved_exact_bytes_and_ordinary_unchanged_input` | gate-control | A changed verifier input is trusted without the maintainer approval, or an unchanged one demands one. | docs/testing.md#evidence-design |
| `TestVerifierUpdateDecision::test_wrong_or_stale_native_decision_cannot_grant_trust` | gate-control | A decision from the wrong actor, branch, run, candidate or an outdated PR state grants verifier trust. | AGENTS.md#hard-boundaries |
| `TestVerifierUpdateDecision::test_decision_survives_regenerated_merge_only_with_identical_tree_and_parents` | gate-control | An approval is lost after a benign merge regeneration, or reused for a different tree or parents. | docs/agent-workflow.md#review-handoff |
| `TestVerifierUpdateDecision::test_latest_revoke_or_pending_decision_blocks_old_approval_and_recheck` | gate-control | An old approval keeps working after a newer revoke, pending or cancelled decision. | docs/agent-workflow.md#review-handoff |
| `TestVerifierUpdateDecision::test_selector_requires_exact_approval_and_catalogue_remains_excluded` | gate-control | A changed review selector merges on a stale approval, or the hazard catalogue is approvable through the ordinary decision. | tests/hazards/critical.json |
| `TestVerifierUpdateDecision::test_producer_artifact_round_trip_and_no_failed_evidence_waiver` | gate-control | An approved verifier change waives failed native evidence or lets candidate policy code execute. | docs/testing.md#evidence-design |
| `TestVerifierUpdateDecision::test_workflow_runs_only_approved_main_with_read_only_token` | infra | The verifier-update workflow runs from a non-main ref, for another actor, or with secrets and write permissions. | AGENTS.md#hard-boundaries |
| `TestVerifierUpdateDecision::test_publisher_explains_required_decision_without_recommending_a_policy_bypass` | infra | The publisher hides the required maintainer decision or offers a bypass link for the unsupported catalogue change. | docs/agent-workflow.md#review-handoff |
| `TestCatalogueAnchorDecision::test_actual_anchor_record_round_trips_through_collector_and_acceptance` | gate-control | An approved anchor change cannot pass the collector and acceptance, or an unadopted catalogue passes. | tests/hazards/critical.json |
| `TestCatalogueAnchorDecision::test_mixed_catalogue_and_implementation_changes_need_both_distinct_decisions` | gate-control | A PR changing both the catalogue and the runner merges on only one of the two maintainer decisions. | tests/hazards/critical.json |
| `TestCatalogueAnchorDecision::test_catalogue_only_collection_refuses_a_complete_ordinary_decision` | gate-control | An ordinary verifier decision is accepted for a catalogue-only change. | tests/hazards/critical.json |
| `TestCatalogueAnchorDecision::test_actual_publisher_refuses_anchor_revoked_between_collection_and_final_write` | gate-control | An anchor revoked after collection still publishes a success check. | tests/hazards/critical.json |
| `TestCatalogueAnchorDecision::test_historical_approval_and_native_oracle_survive_repository_anchor_adoption` | gate-control | After the repository adopts the anchors, historical approvals stop validating or a weakened assertion passes. | tests/hazards/critical.json |
| `TestCatalogueAnchorDecision::test_anchor_decision_refuses_structural_edits_even_when_python_values_compare_equal` | gate-control | A catalogue edit (types, extra fields, reorders, duplicates, whitespace) slips through as the approved anchor change. | tests/hazards/critical.json |
| `TestCatalogueAnchorDecision::test_distinct_anchor_decision_approves_exact_bytes_but_ordinary_approval_stays_excluded` | gate-control | The anchor decision approves other bytes, or the ordinary approval path can approve the catalogue. | tests/hazards/critical.json |
| `TestCatalogueAnchorDecision::test_anchor_decision_rejects_stale_malformed_or_mixed_provenance` | gate-control | A stale, wrong-kind or wrong-run anchor decision is accepted. | tests/hazards/critical.json |
| `TestCatalogueAnchorDecision::test_new_anchor_revoke_pending_or_failure_after_collection_blocks_publication` | gate-control | A newer revoked, pending or failed anchor decision does not block the published acceptance. | tests/hazards/critical.json |
| `TestCatalogueAnchorDecision::test_native_receipt_binds_exact_adopted_catalogue_and_all_ten_obligations` | gate-control | The hazard receipt passes against the old catalogue, a missing or duplicated challenge, or a self-chosen catalogue. | tests/hazards/critical.json |
| `TestCatalogueAnchorDecision::test_native_policy_cannot_replace_approved_obligations_with_self_consistent_candidate` | gate-control | A candidate that weakens the catalogue passes with a self-consistent hash and results. | tests/hazards/critical.json |
| `TestEvalRetrievalProducer::test_eval_retrieval_receipt_normalizes_to_success` | infra | The eval_retrieval receipt is mis-bound to another lane, so the lane is never satisfied or is satisfied by the wrong job. | docs/live-stack.md#verification-minimums |
| `TestEvalRetrievalProducer::test_collect_native_maps_eval_retrieval_job_to_lane_success` | infra | The collector does not map the eval-retrieval and gate-l1 jobs to their lanes, leaving required lanes missing. | docs/live-stack.md#verification-minimums |

## `tests/test_rewrite.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_glossary_keys_sorted_uppercase_unique` | core | The acronym glossary develops unsorted, duplicate, lowercase or missing two-letter entries, making expansions unreviewable or wrong. | issue #82; docs/retrieval.md |
| `test_ambiguous_tokens_excluded_never_guessed` | core | Ambiguous tokens (DSN, MAP, CA) are expanded, adding wrong meanings to the retrieval query. | issue #82; docs/retrieval.md |
| `test_known_acronyms_expand_and_nothing_else_moves` | core | Known acronyms stop expanding (slashed, two-letter, any case) or other text is altered. | issue #82; docs/retrieval.md |
| `test_non_acronym_and_identifier_tokens_never_move` | core | Identifiers or plain tokens are altered by rewriting, diluting exact-code queries. | issue #82; docs/retrieval.md |
| `test_should_rewrite_bypasses_identifiers` | core | Identifier-bearing queries are rewritten, diluting the exact-code path. | issue #82; docs/retrieval.md |
| `test_should_rewrite_refuses_trap_queries` | core | A trap query with an acronym is rewritten, altering the text the screen and refusal path see. | issue #157; docs/retrieval.md |
| `test_search_embeds_original_for_trap_with_acronym` | core | Trap queries reach retrieval legs with expanded text instead of the operator own words. | issue #157; docs/retrieval.md |
| `test_golden_sweep_identifiers_byte_identical` | core | Some real identifier-bearing golden query is changed by expansion. | issue #82; docs/retrieval.md |
| `test_search_embeds_expanded_when_flag_on` | core | The acronym expansion flag does not reach the embed legs, so enabling it does nothing. | issue #82; docs/retrieval.md |
| `test_search_embeds_original_when_flag_off_or_identifier_query` | core | Expansion applies with the flag off or to identifier queries. | issue #82; docs/retrieval.md |

## `tests/test_rules_version.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_version_is_stable_and_covers_all_payload_producers` | core | The extraction-rules version is computed from the full set of payload-producing modules, so a rule change always changes the version and triggers re-ingest. | issue 124 |
| `test_rule_modules_import_only_rule_modules` | core | The hashed rule-module set is closed under package imports, so payload-shaping code outside it cannot change stored payloads without changing the version. | docs/ingest.md |
| `test_stored_payload_is_owned_by_a_rule_module` | core | The stored payload mapping and context normalization live in hashed modules, so adding a stored field elsewhere cannot bypass the rules version. | issue 124 |
| `test_hash_changes_when_rules_change` | core | Rule file content and name both feed the hash, so edits or renames change the version. | issue 124 |
| `test_should_skip_gates_on_rules_version` | core | An unchanged file is re-ingested when the rules version changed or the record predates versioning, and --reingest never skips, so stale payloads are not kept. | issue 124 |
| `test_reingest_flag_reextracts_matching_sha` | core | --reingest after a rules change passes the startup gate and deletes and re-upserts a sha-equal doc, so operators can recover stale payloads. | issue 124 |
| `test_manifest_records_rules_version` | core | Run manifests record the extraction rules version, so a run can be tied to the payload rules that produced it. | issue 124 |
| `test_qdrant_skip_gates_on_rules_version` | core | A sha-equal doc with stale stored rules is re-upserted by a plain rerun while a verified completion skips, so neither stale payloads persist nor work is repeated. | issue 124 |

## `tests/test_run_ingest.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_dry_run_ingest` | core | A dry run parses and chunks the corpus, writes a typed inventory record, skips unchanged files on rerun and reports summary counters, so operators can plan ingests safely. | docs/ingest.md |
| `test_worker_qdrant_client_is_built_once_from_ingest_settings` | core | The ingest worker builds its Qdrant client once from the ingest settings, so each document does not reconnect or use the wrong endpoint or credentials. | docs/ingest.md |
| `test_resolve_workers_is_a_cap` | infra | The worker count is a capped, at-least-one value, so a bad setting cannot start zero or an unbounded number of parse processes. | docs/ingest.md |
| `test_corrupt_pdf_writes_typed_error_record` | core | One corrupt PDF yields a typed error record and a failure exit and is counted, without killing the run for other documents. | docs/ingest.md |
| `test_upsert_path_counters_with_fake_qdrant` | core | The non-dry path upserts the parsed chunks, counts them, and a warm rerun with a verified completion skips without upserts or deletes, so ingest is idempotent. | issue 359 |
| `test_builder_refuses_a_real_run_without_a_staging_build` | core | The builder refuses a real run outside a publication staging generation before any lock or write, so nothing can write into the serving collection. | issue 360 |
| `test_embed_failfast_upserts_nothing` | core | A document whose embedding fails is recorded as an error and never upserted, so the collection never holds a half-embedded document. | docs/ingest.md |
| `test_doc_locks_are_per_doc_id` | core | Same-revision upserts take the same lock while different revisions do not contend, so concurrent check-delete-upsert sequences cannot corrupt one document. | issue 361 |
| `test_worker_embed_batches_by_batch_size` | core | Chunks are embedded in slices no larger than batch_size, so large documents do not exceed the embed endpoint request limits. | docs/ingest.md |
| `test_bulk_load_disables_and_restores_indexing` | core | Indexing is disabled before the first upsert and restored after the last, so bulk loads are fast and the collection returns to the server default. | docs/ingest.md |
| `test_parse_one_error_isolation_and_picklability` | core | Worker errors become plain picklable data that cross the process boundary, so a failing document cannot crash the parse pool. | docs/testing.md |
| `test_revision_change_blocks_skip_and_force_reembeds` | core | A changed embed model revision refuses a normal rerun and --reingest re-embeds every doc to the new generation, so A vectors are never served as B. | issue 391 |
| `test_interrupted_migration_stays_pending_until_forced_resume` | core | A crash after the pending declaration leaves a contract that refuses plain reruns and resumes to committed with --reingest, so interrupted migrations cannot look compatible. | issue 391 |
| `test_partial_migration_failure_leaves_pending_then_resume_commits` | core | A failure after partial document work never certifies the new representation contract and a forced rerun completes it, so half-migrated data is not served as committed. | issue 391 |
| `test_stale_generation_marker_blocks_commit` | core | A source walk that no longer covers every stored doc cannot commit the new contract while old markers remain, so stale vectors are not certified. | issue 391 |
| `test_limit_is_refused_on_every_real_run` | core | --limit is refused on real runs before any write, so a partial corpus walk can never reach a collection. | issue 360 |
| `test_unmarked_searchable_point_blocks_migration_commit` | core | The scope proof checks actual searchable points, so an unmarked old-rules point blocks the commit and is preserved rather than certified. | docs/ingest.md |
| `test_upsert_failure_log_carries_error_type_not_exception_text` | core | A failing upsert logs only the exception class, so upstream bodies and secrets never reach logs. | AGENTS.md hard boundary: logs contain no secrets |
| `test_document_bounds_refuse_before_embedding_with_an_error_record` | core | Over-bound PDFs, pages, chunks or embed inputs become typed error records with counts only and no embedding, so oversize documents cannot exhaust the pipeline. | issue 374 |
| `test_oversize_document_is_never_upserted_or_completed_and_next_run_succeeds` | core | A refused oversize document writes no points or completion and the forced rerun ingests it once the bound is lifted, so refusal is recoverable. | issue 374 |
| `test_failed_preflight_starts_no_workers_and_writes_nothing` | core | Every preflight refusal happens before the parse pool exists and leaves storage and inventory untouched, so a misconfigured run cannot partially mutate the collection. | issue 583 |
| `test_combined_in_flight_window_bounds_parse_and_upsert_work` | core | Parse plus upsert futures never exceed the in-flight window, so slow upserts cannot make the parent hold unbounded parsed documents in memory. | issue 583 |

## `tests/test_run_local_vllm.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_role_derived_from_model_name_without_make` | infra | Without it direct invocation of an embed model launches the reasoning server shape. | docs/live-stack.md |
| `test_explicit_role_selects_server_not_name` | infra | Without it a model name containing embed overrides the operator's explicit role. | docs/live-stack.md |
| `test_explicit_env_wins_over_budget` | infra | Without it operator GPU_MEM, MAX_LEN and SEQS overrides are ignored or the safe serving shape is dropped. | docs/live-stack.md |
| `test_budget_failure_fails_closed_before_docker` | infra | Without it a budget deficit still launches a container that OOMs the GPU. | docs/agent.md#http-model-contract |
| `test_local_model_path_is_one_mount_argument_and_token_stays_off_argv` | infra | Without it a model path with spaces splits into arguments or HF_TOKEN appears in the process argv. | AGENTS.md hard boundaries (secrets) |
| `test_prefix_cache_flag_follows_budget_resolution` | infra | Without it the launcher stops reflecting the budget resolver's prefix-cache and utilization result. | docs/live-stack.md |
| `test_container_name_is_one_runtime_argument` | infra | Without it a container name with shell metacharacters executes commands. | AGENTS.md hard boundaries (secrets and safe execution) |
| `test_host_mem_mb_env_overrides_budget_cap` | infra | Without it the operator cannot lower the container memory cap. | docs/live-stack.md (local host resource guard) |
| `test_invalid_host_mem_mb_fails_closed_before_docker` | infra | Without it an invalid memory cap starts an uncapped container. | docs/live-stack.md (local host resource guard) |
| `test_refuses_when_available_ram_below_cap_plus_headroom` | infra | Without it a vLLM container starts when host RAM cannot hold it and thrashes the host. | docs/live-stack.md (local host resource guard) |
| `test_admits_at_exactly_cap_plus_headroom` | infra | Without it the RAM guard is off by one and blocks hosts that fit, or the cap is not applied. | docs/live-stack.md (local host resource guard) |
| `test_refuses_on_high_memory_or_io_pressure` | infra | Without it vLLM starts under memory or IO pressure. | docs/live-stack.md (local host resource guard) |
| `test_pressure_just_below_threshold_is_admitted` | infra | Without it the pressure threshold is mis-set and healthy hosts are refused or strict thresholds ignored. | docs/live-stack.md (local host resource guard) |
| `test_missing_pressure_files_skip_that_check_with_notice` | infra | Without it hosts without PSI files fail to launch or skip silently. | docs/live-stack.md (local host resource guard) |
| `test_force_start_skips_admission_but_keeps_the_cap` | infra | Without it FORCE_START either cannot override admission or also removes the memory cap. | docs/live-stack.md (local host resource guard) |
| `test_stub_resolver_without_cap_requires_host_mem_mb` | infra | Without it a resolver that yields no memory cap launches an uncapped container. | docs/live-stack.md (local host resource guard) |

## `tests/test_sanitize.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_sanitize_page_text_drops_controls_and_keeps_content` | core | Control, escape, bidi and zero-width characters are stripped while whitespace alignment and unicode stay byte-identical, so injection characters never reach chunks or prompts. | issue 87 |
| `test_extract_page_texts_sanitizes_and_preserves_labels` | core | Extraction sanitizes page text and preserves page labels, so the wiring and not just the helper protects stored text. | issue 87 |
| `test_parse_pdf_sanitizes_metadata_title` | core | The PDF metadata title is sanitized in the parse path, so hostile titles cannot reach payloads, embed headers or prompts. | issue 87 |

## `tests/test_screen.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_trap_shapes_screen_as_trap` | core | Injection or exfiltration queries (override, disclosure, transformation, wrapped) are classed answerable and get reranked. | issue #87; issue #134; docs/retrieval.md |
| `test_legitimate_queries_stay_answerable` | core | Legitimate operator questions are flagged as traps, silently dropping rerank gains. | issue #87; issue #176; docs/retrieval.md |
| `test_golden_sweep_only_known_traps_flag` | core | A golden query is misclassified: a false positive costs rerank, a miss opens the gate. | issue #87; docs/retrieval.md |

## `tests/test_serving_gate.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_resolve_binds_physical_and_reads_its_own_metadata` | core | Without it validation could certify a generation from a stale alias-named collection instead of the physical one actually served. | docs/agent.md#serving-contract |
| `test_resolve_refuses_physical_drift_even_when_alias_metadata_is_compatible` | core | Without it a compatible alias-named record could vouch for a physical collection built with a different model. | docs/agent.md#serving-contract |
| `test_resolve_flat_legacy_empty_and_dangling` | core | Without it legacy, empty and dangling-alias stores would be classified wrongly and served or refused incorrectly. | docs/agent.md#serving-contract |
| `test_async_manifest_read_missing_collection_is_absent_without_retrieve` | core | Without it a fresh install would read as an unreadable store instead of empty. | docs/agent.md#serving-contract |
| `test_gate_caches_within_ttl_and_revalidates` | core | Without it every request would revalidate the store, or a stale generation would be served past its TTL. | docs/agent.md#serving-contract |
| `test_gate_zero_ttl_validates_every_request_and_caches_refusals` | core | Without it the paranoid zero-TTL mode would cache, and refusals would be treated as servable. | docs/agent.md#serving-contract |
| `test_serving_requires_full_published_build_pair` | core | Without it a half-published, tampered or mismatched build pair would be served or would mint evidence references. | docs/agent.md#serving-contract |
| `test_publication_during_validation_preserves_resolved_reader` | core | Without it an alias swap mid-validation would let a request read a different generation than the one it validated. | docs/agent.md#serving-contract |
| `test_legacy_corpus_name_with_build_substring_remains_readable` | core | Without it a legacy corpus whose name contains the build marker would be misread as a build alias and become unservable. | docs/agent.md#serving-contract |
| `test_serving_read_surface_accepts_real_clients_and_rejects_writes` | infra | Without it serving code could gain a write call on a Qdrant reader without the type check failing. | AGENTS.md hard boundary: serving credentials read-only; issue #369 |

## `tests/test_sim_qdrant_sh.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `SimQdrantScriptTests::test_sim_helper_rejects_existing_wrong_image_without_mutation` | gate-control | Without it qa:sim and the CI sim tier could pass against a Qdrant image other than the approved pinned digest. | docs/live-stack.md#verification-minimums |

## `tests/test_split.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_versus_splits_into_entity_paths` | core | Comparative questions (A versus B) no longer split into one retrieval path per entity, so one side is under-retrieved. | issue #214; docs/retrieval.md |
| `test_multiword_entities_split_into_exact_paths` | core | Multiword entities are cut mid-name, giving legs that retrieve the wrong topic. | issue #214; docs/retrieval.md |
| `test_marker_forms_split_into_one_path_per_entity` | core | vs, difference-between and narrow-and compare forms stop splitting, or leak the other entity into a leg. | issue #214; docs/retrieval.md |
| `test_bare_and_does_not_split_diagnostics` | core | Diagnostic questions containing a bare and get shredded into meaningless legs. | issue #214; docs/retrieval.md |
| `test_slash_pairs_never_split` | core | Slashed names (SMP/E, TCP/IP, DSALIMIT/EDSALIMIT) are split, measurably hurting holdout retrieval. | issue #214; docs/retrieval.md |
| `test_three_entities_cap_at_two_paths` | core | N-way comparisons spawn more than two retrievals, breaking the cost cap. | issue #214; docs/retrieval.md |
| `test_trap_query_never_splits` | core | A trap/injection query is split and processed as a comparative, bypassing the trap gate. | issue #214; docs/retrieval.md |
| `test_exact_anchor_comparatives_bypass_split` | core | Comparatives on exact message ids or doc numbers split into two legs instead of keeping the single filtered exact retrieval. | issue #214; docs/retrieval.md |
| `test_member_only_comparatives_split` | core | Member names are treated as exact anchors, wrongly disabling splitting for comparatives that mention them. | issue #214; docs/retrieval.md |
| `test_diagnostic_dual_path_keeps_original_plus_stripped` | core | Diagnostic questions lose their symptom-plus-cause dual path, missing the procedure page behind an error message. | issue #214; docs/retrieval.md |
| `test_diagnostic_stays_single_without_a_distinct_cause_leg` | core | Questions with nothing to strip or a noise-only stripped leg pay for a duplicate second leg. | issue #214; docs/retrieval.md |
| `test_strip_identifiers_removes_all_families_including_folded_members` | core | Stripped cause legs still carry identifiers (including lowercase members), so they do not broaden the search. | issue #133; docs/retrieval.md |
| `test_comparative_marker_shapes` | core | Wrapped, case-folded, link or top-placed comparative markers stop being detected. | issue #214; docs/retrieval.md |
| `test_single_path_shapes` | core | Factoids, plain NL, or questions with or-markers are wrongly split. | issue #214; docs/retrieval.md |
| `test_empty_and_whitespace_stay_single` | core | Empty or whitespace queries crash or yield bogus paths. | issue #214; docs/retrieval.md |

## `tests/test_split_retrieval.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_comparative_split_covers_both_entities` | core | A comparative question retrieves only the shared page and misses one entity docs; embed fan-out cost changes unnoticed. | issue #214; docs/retrieval.md |
| `test_diagnostic_dual_path_covers_symptom_and_cause` | core | Diagnostic questions fail to merge the message page and the procedure page. | issue #214; docs/retrieval.md |
| `test_split_legs_share_original_filter` | core | A stripped cause leg drops the identifier filter and surfaces documents the symptom filter excluded. | issue #214; docs/retrieval.md |
| `test_trap_and_exact_anchor_comparatives_stay_single` | core | Trap and exact-code comparatives spend extra legs, or member-only comparatives stop splitting. | issue #214; docs/retrieval.md |
| `test_max_fusion_prefers_focused_rank1_over_shared_noise` | core | Shared-context noise outranks an entity-focused top hit, flipping holdout recall at 1; dedupe and limit also break. | issue #214; docs/retrieval.md |
| `test_merge_diagnostic_weights_favor_symptom_leg` | core | Diagnostic merge stops favoring the symptom leg, or duplicates, empty legs or the limit are mishandled. | issue #214; docs/retrieval.md |

## `tests/test_stream_truncation.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_achat_failed_stream_discards_partial_and_recovers_via_post` | core | Without it a dropped or error-framed stream would ship its partial text instead of recovering one complete answer via a single POST. | docs/agent.md#http-model-contract |
| `test_chat_sync_truncated_stream_falls_back_to_complete_post` | core | Without it the sync chat path would return a truncated stream as a complete answer. | docs/agent.md#http-model-contract |
| `test_chat_stream_explicit_non_stop_finish_is_classified_not_truncated` | core | Without it a stream properly finished by length would be treated as truncated and replayed, or vice versa. | docs/agent.md#http-model-contract |
| `test_chat_stream_error_frame_text_never_reaches_logs` | core | Without it upstream error text would reach logs. | AGENTS.md hard boundary: logs contain no secrets/manual text |
| `test_chat_stream_failure_after_content_raises_without_replay` | core | Without it a stream failing after visible content would be replayed or end as complete, or leak upstream text. | docs/agent.md#http-model-contract |
| `test_every_malformed_frame_shape_fails_closed_after_content` | core | Without it a structurally wrong frame would be folded into the answer instead of failing the stream. | docs/agent.md#http-model-contract |
| `test_chat_stream_pre_output_error_frame_failed_fallback_raises` | core | Without it a failed fallback without a finish reason would ship a synthesized stop. | docs/agent.md#http-model-contract |
| `test_achat_missing_finish_on_both_legs_raises_not_synthesized_stop` | core | Without it a missing finish on both stream and fallback would produce an answer labelled stop. | docs/agent.md#http-model-contract |
| `test_buffered_payload_requires_explicit_finish` | core | Without it a non-streaming completion with no finish, an error, or non-string content would be accepted as complete. | docs/agent.md#http-model-contract |
| `test_chat_stream_pre_output_failure_recovers_via_one_post` | core | Without it an empty or malformed-first stream would fail the request or emit rejected text instead of recovering with one POST. | docs/agent.md#http-model-contract |
| `test_chat_stream_malformed_first_frame_failed_fallback_fails_closed` | core | Without it a malformed first frame with a failed fallback would retry in a loop or leak upstream text. | docs/agent.md#http-model-contract |
| `test_buffered_payload_rejects_every_other_misshapen_terminal` | core | Without it misshapen non-streaming completions such as empty choices or bad message types would be accepted. | docs/agent.md#http-model-contract |
| `test_buffered_payload_preserves_explicit_finish` | core | Without it the parser could rewrite a real finish reason such as length into stop. | docs/agent.md#http-model-contract |
| `test_answer_body_state_real_client_all_surfaces` | core | Without it citation-only bodies could read as accepted answers, or the verification state could differ across answer, chat and completions routes. | docs/agent.md#http-model-contract |
| `test_reasoning_only_empty_content_never_reads_as_an_answer` | core | Without it a reply with only reasoning text and empty content would be shown as an answer. | docs/agent.md#http-model-contract |
| `test_length_finish_mid_citation_keeps_body_drops_fragment` | core | Without it a length-cut citation fragment would be accepted as a citation or the answer marked accepted. | docs/agent.md#http-model-contract |
| `test_v1_answer_real_client_error_frame_emits_fixed_error_without_leak` | core | Without it an upstream error frame would end the SSE answer as complete or leak upstream text. | docs/agent.md#http-model-contract |
| `test_v1_chat_real_client_error_frame_is_incomplete_no_success_chunk` | core | Without it the chat stream would emit a success finish chunk after a failed generation. | docs/agent.md#http-model-contract |
| `test_v1_answer_real_client_missing_finish_both_legs_is_502` | core | Without it an incomplete completion would return 200 with an answer instead of a fixed 502. | tests/hazards/critical.json |
| `test_v1_chat_json_real_client_missing_finish_is_502` | core | Without it chat JSON would return a stop answer for an incomplete completion. | docs/agent.md#http-model-contract |
| `test_v1_chat_stream_real_client_missing_finish_emits_error_and_done` | core | Without it a chat stream with no finish would end as success instead of an error frame and DONE. | docs/agent.md#http-model-contract |
| `test_v1_chat_json_real_client_buffered_error_is_502` | core | Without it a buffered completion carrying an error would return its text or leak the private error. | docs/agent.md#http-model-contract |
| `test_answer_stream_disconnect_records_generation_incomplete` | core | Without it a client disconnect before the terminal frame would go unrecorded and look like success. | docs/agent.md#http-model-contract |
| `test_answer_disconnect_after_error_frame_is_not_a_second_abort` | core | Without it a disconnect after an error frame would double-count the request as a disconnect. | docs/agent.md#http-model-contract |
| `test_chat_stream_disconnect_records_generation_incomplete` | core | Without it chat disconnects would go unrecorded. | docs/agent.md#http-model-contract |
| `test_truncation_alert_detail_is_fixed_vocabulary` | core | Without it alert details could carry exception bodies, chunk counts or response text. | AGENTS.md hard boundary: logs contain no secrets/manual text |
| `test_v1_answer_stream_post_header_exception_single_observation` | core | Without it an exception after headers would send a final event, record several outcomes, or leak the exception text into telemetry. | docs/agent.md#http-model-contract |

## `tests/test_taskfile_contracts.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `TaskContractsTests::test_controlled_runner_ignores_inherited_controls_and_private_dotenv` | infra | The pinned runner honors inherited TASK_* controls or reads a private .env, changing what verification executes. | docs/task-runner.md |
| `TaskContractsTests::test_controlled_discovery_and_literal_focused_selection` | infra | Discovery writes workspace state, reads private files, or a focused qa:unit argument is shell-interpreted. | docs/task-runner.md |
| `TaskContractsTests::test_controlled_runner_rejects_corruption_config_and_path_overrides` | infra | A corrupt, overridden or config-injected Task binary is executed by the controlled launcher. | docs/task-runner.md |
| `TaskContractsTests::test_multi_family_selection_does_not_leak_eval_defaults` | infra | Selecting eval and airgap tasks together leaks the hash embed default into air-gap deployment. | AGENTS.md#hard-boundaries |
| `TaskContractsTests::test_controlled_cancellation_reaches_foreground_owner_and_cleanup` | infra | Ctrl-C or SIGTERM does not reach the foreground owner, leaving an orphaned local resource. | docs/task-runner.md |
| `TaskContractsTests::test_verification_fails_closed_without_venv` | infra | Verification tasks create a venv or run effects without the development environment. | docs/task-runner.md |
| `TaskContractsTests::test_unit_default_argv_runs_once` | infra | qa:unit skips its doctor prerequisite or runs pytest more than once with the wrong default selection. | docs/task-runner.md |
| `TaskContractsTests::test_hazard_paths_reach_runner_without_shell_interpretation` | infra | A hazard output path with shell syntax executes or reaches the runner wrongly. | docs/task-runner.md |
| `TaskContractsTests::test_check_runs_lint_typecheck_unit_in_order` | infra | qa:check skips a stage or runs them in the wrong scope or order. | docs/task-runner.md |
| `TaskContractsTests::test_lint_failure_stops_check_before_typecheck` | infra | A lint failure no longer stops qa:check and later stages hide it. | docs/task-runner.md |
| `TaskContractsTests::test_failed_prerequisite_never_starts_pytest` | infra | pytest starts after the doctor prerequisite failed. | docs/task-runner.md |
| `TaskContractsTests::test_profile_values_reach_doctor_exactly` | infra | dev:doctor receives a rewritten or dropped profile value, such as empty or false. | docs/task-runner.md |
| `TaskContractsTests::test_wheelhouse_preparation_failure_stops_image_build` | infra | An image build proceeds after wheelhouse preparation failed. | docs/deploy.md#deployment-policy |
| `TaskContractsTests::test_bm25_model_selection_reaches_fetcher` | infra | The BM25 model selection is not forwarded literally to the weights fetcher. | docs/deploy.md#deployment-policy |
| `TaskContractsTests::test_chart_check_fails_closed_then_passes` | infra | artifacts:chart-check passes without the pinned Qdrant chart archive. | docs/deploy.md#deployment-policy |
| `TaskContractsTests::test_helm_render_verifies_chart_first_with_fixed_sets` | infra | helm-render uses different placeholders or sets than the packaging contract. | docs/deploy.md#deployment-policy |
| `TaskContractsTests::test_helm_render_refuses_without_chart` | infra | Helm runs before the chart archive is verified. | docs/deploy.md#deployment-policy |
| `TaskContractsTests::test_images_builds_preparations_then_images_in_order` | infra | Images are built before wheelhouse and BM25 preparation, or with a different bundle directory or tag. | docs/deploy.md#deployment-policy |
| `TaskContractsTests::test_single_image_reverifies_transferred_bytes_and_transports_literal_inputs` | infra | An image is built from corrupted transferred wheels or weights, or literal input paths are shell-evaluated. | docs/deploy.md#deployment-policy |
| `TaskContractsTests::test_single_image_refuses_unknown_or_missing_role_before_any_build` | infra | An unknown or injected image role triggers a build. | docs/deploy.md#deployment-policy |
| `TaskContractsTests::test_retrieval_transports_required_operation_and_literal_output` | infra | eval tasks use the wrong suite or operation, or a literal output path is shell-interpreted. | docs/eval.md |
| `TaskContractsTests::test_holdout_transports_forced_rc_venue` | infra | CLI or ambient VENUE=dev overrides the holdout task's forced rc venue. | docs/eval.md |
| `TaskContractsTests::test_answer_eval_agent_url_is_optional_and_roundtrips_exactly` | infra | The answer eval sends a mangled or default agent URL instead of the exact selected, ambient or absent one. | docs/eval.md |
| `TaskContractsTests::test_bench_verify_and_load_carry_no_mode_exports` | infra | Bench, verify and load tasks inherit a hash mode or venue that distorts their measurements. | docs/eval.md |
| `TaskContractsTests::test_capture_pool_transports_bundle_and_explicit_paths` | infra | capture-pool ignores explicit out, golden or depth inputs or invents defaults. | docs/eval.md |
| `TaskContractsTests::test_harness_l3_transports_only_explicit_baseline` | infra | The L3 harness silently picks a baseline file that was not explicitly selected. | docs/testing.md#harness-invariants-all-harness-tiers-l1l2l3l4 |
| `TaskContractsTests::test_local_acceptance_and_repair_dispatch_exact_arguments` | infra | local:check and local:repair-staging receive altered arguments or evaluate literal path input. | docs/live-stack.md |
| `TaskContractsTests::test_query_optional_flags_and_literal_values` | infra | local:query forwards empty flags or evaluates a query containing shell metacharacters. | docs/task-runner.md |
| `TaskContractsTests::test_vllm_launcher_env_exact` | infra | Local model launch receives ambient values instead of CLI inputs or loses its role and budget interpreter. | docs/task-runner.md |
| `TaskContractsTests::test_local_model_served_name_round_trip` | infra | A served model name with spaces or shell syntax is altered between Task input and launcher. | docs/task-runner.md |
| `TaskContractsTests::test_gateway_down_ignores_absent_resources` | infra | Stopping an absent local gateway fails, or stops the wrong container and network. | docs/task-runner.md |
| `TaskContractsTests::test_qdrant_tasks_delegate_to_helper` | infra | Local Qdrant up/down ignores CLI container and port inputs or accepts an empty container name. | docs/task-runner.md |
| `TaskContractsTests::test_stack_forwards_only_set_vars` | infra | local:stack forwards unset variables or lets ambient values override explicit CLI ones. | docs/task-runner.md |
| `TaskContractsTests::test_stack_explicit_empty_progress_overrides_ambient_to_the_default` | infra | An explicitly empty progress path keeps an old ambient inventory file. | docs/task-runner.md |
| `TaskContractsTests::test_stack_forwards_reingest_only_when_selected` | infra | A reingest flag leaks from ambient state and silently reingests the collection. | AGENTS.md#hard-boundaries |
| `TaskContractsTests::test_qdrant_forward_bridges_explicit_inputs_and_requires_a_namespace` | infra | The Qdrant port forward starts without a namespace or alters kubeconfig and context values. | docs/task-runner.md |
| `TaskContractsTests::test_agent_stream_port_and_ui` | infra | local:agent drops streaming, enables the console unintentionally, or ignores the selected port. | docs/task-runner.md |
| `TaskContractsTests::test_sim_and_load_use_fixed_tiers` | infra | qa:sim, qa:load and qa:ha run another tier's selection or skip the profile doctor. | docs/live-stack.md#verification-minimums |
| `TaskContractsTests::test_operator_empty_stays_unset` | infra | An empty CLI operator value overrides the airgap.env value instead of staying unset. | docs/task-runner.md |
| `TaskContractsTests::test_operator_unset_stays_empty_no_task_defaults` | infra | Task invents operator defaults such as the embed mode when airgap.env sets none. | AGENTS.md#hard-boundaries |
| `TaskContractsTests::test_bridge_set_matches_operator_keys` | infra | An operator key is dropped or mangled between Task input and any air-gap stage, or CLI/ambient precedence is wrong. | docs/task-runner.md |
| `TaskContractsTests::test_operator_precedence_three_way_conflict` | infra | CLI, ambient and file values resolve in the wrong order for an air-gap stage. | docs/task-runner.md |
| `TaskContractsTests::test_airgap_env_selector_cli_and_ambient_precedence` | infra | The AIRGAP_ENV selector picks the wrong env file. | docs/task-runner.md |
| `TaskContractsTests::test_airgap_env_missing_file_fails_task_invocation` | infra | A missing selected env file lets an air-gap stage run with defaults. | AGENTS.md#hard-boundaries |
| `TaskContractsTests::test_dryrun_fixed_params_win` | infra | airgap:dryrun can be pointed at a real registry or namespace by operator input. | AGENTS.md#hard-boundaries |
| `TaskContractsTests::test_dev_setup_completion_stamp` | infra | A half-finished setup is treated as complete, or a completed one is rebuilt. | docs/task-runner.md |
| `TaskContractsTests::test_dev_clean_bounded_retention` | infra | dev:clean deletes archives, airgap.env or tracked sources, or leaves caches behind. | docs/task-runner.md |

## `tests/test_telemetry_privacy.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_error_type_never_exports_bodies` | core | Logged error classification could start exporting exception message bodies containing query or manual text. | AGENTS.md hard boundaries (logs contain no secrets or manual text); #529 |
| `test_success_hides_canaries_from_logs_and_spans` | core | Query text, header values or document ids would leak into logs and span attributes on a successful request. | AGENTS.md hard boundaries; #529 |
| `test_failure_hides_canaries_but_keeps_error_type` | core | A failing request would export the upstream exception text to logs and spans, or lose the error type operators triage by. | AGENTS.md hard boundaries; #529 |
| `test_attachment_failures_hide_canaries` | core | An escaping failure on any answer, chat or console route (sync or awaited retrieval or condensation) could leak exception, query or header canaries into spans, logs or metric labels before SDK export. | #563; AGENTS.md hard boundaries |
| `test_safe_attachment_preserves_parent_and_lifetime` | core | The safe span attachment could end the root early, orphan child spans, or record exception messages on spans. | #563 |
| `test_metric_labels_hide_canaries_and_stay_finite` | core | Metric labels could carry query text or exception text and grow unbounded cardinality. | docs/metric-contract.md; #529 |
| `test_exception_frames_carry_no_message_or_source` | core | Formatted exception log records could include the exception message or source frames carrying secrets. | AGENTS.md hard boundaries; #529 |
| `test_attachment_privacy_negative_control` | gate-control | The attachment-failure canary could become vacuous: it must fail when SDK attachment defaults are restored and leak the canary into spans. | #563 |

## `tests/test_timeouts.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_answer_client_uses_setting_and_never_retries` | core | Without it the answer call could ignore its configured timeout or retry connections, replaying a long generation. | docs/agent.md#http-model-contract |
| `test_chat_after_close_fails_loudly` | core | Without it a chat after shutdown would silently rebuild a connection pool instead of failing. | docs/agent.md#http-model-contract |
| `test_llm_client_closes_its_own_pools_but_never_a_borrowed_one` | core | Without it pools would leak at shutdown, or closing the client would kill a pool owned by another component. | docs/agent.md#http-model-contract |
| `test_answer_chat_retries_nothing_on_connect_error` | core | Without it a connect error on the answer call would be retried and duplicate a non-idempotent model request. | docs/agent.md#http-model-contract |
| `test_agent_lifespan_timeouts_and_retries` | core | Without it configured Qdrant and embed timeouts and connect retries would not reach the live clients, leaving calls unbounded. | docs/agent.md#http-model-contract |
| `test_agent_qdrant_client_requests_uncompressed_responses` | core | Without it Qdrant gzip responses return and add roughly 40 ms stalls to every payload read. | issue #697 |
| `test_connect_retries_setting_is_bounded` | core | Without it an operator could set unbounded connect retries that stall requests. | docs/agent.md#http-model-contract |

## `tests/test_tokenizer.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_fallback_estimator_counts_text_and_messages` | core | The fallback estimator returns zero or undercounts, so budgets are wrongly certified when /tokenize is unavailable. | docs/agent.md#http-model-contract |
| `test_vllm_tokenizer_calls_origin_root` | core | Tokenize requests go to /v1/tokenize (404) instead of the server root, silently degrading all budget verification. | docs/agent.md#http-model-contract |
| `test_tokenize_auth_header_follows_the_reasoning_key` | core | A gateway-guarded /tokenize gets no key (or keyless setups get a bogus header), failing every count. | docs/agent.md#http-model-contract |
| `test_failed_tokenize_downgrades_permanently_with_one_warning` | core | A failing /tokenize is silently ignored, re-attempted on every call, or still reported as remote-confirmed. | docs/agent.md#http-model-contract |
| `test_build_tokenizer_dispatch` | core | The wrong tokenizer is built for the configured LLM endpoint. | docs/agent.md#http-model-contract |
| `test_malformed_count_cannot_verify_even_with_tokens` | core | A malformed count field is accepted, certifying a prompt as verified. | docs/agent.md#http-model-contract |
| `test_chat_tokenizer_matches_inference_template_flags` | core | Chat counts omit the generation prompt or special-token flags the inference call uses. | docs/agent.md#http-model-contract |
| `test_chat_token_count_uses_selected_request_template` | core | Counting ignores the request's reasoning effort so a low-effort prompt is verified against another template. | docs/agent.md#http-model-contract |
| `test_chat_token_counts_do_not_retain_another_requests_effort` | core | One request's reasoning effort leaks into a later request's count. | docs/agent.md#http-model-contract |
| `test_effort_specific_count_failure_still_pins_unverified_estimation` | core | After an effort-specific count failure the tokenizer reports verified or retries. | docs/agent.md#http-model-contract |

## `tests/test_tracing.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_request_cancellation_ends_root_before_headers` | core | A client disconnect before response headers would leave the root span open, double-finish it, or miscount the request instead of recording one client_disconnect. | docs/agent.md observability; docs/metric-contract.md |
| `test_console_stream_cancel_during_postheader_retrieval` | core | A console stream cancelled during post-header retrieval would leak the root span or record the wrong outcome and verification state. | docs/agent.md observability |
| `test_stream_cancellation_finishes_root_and_closes_owned_io` | core | A stream close, cancel, disconnect or send failure could leave the model response open, end the root more than once, or skip the single client_disconnect count even when cleanup itself fails. | docs/agent.md observability; AGENTS.md lifecycle rule |
| `test_retrieval_failure_terminal_logs_join_recording_root` | core | Terminal failure logs on any route could lose the trace and span ids of their request, breaking log to trace pivots. | docs/metric-contract.md (join keys) |
| `test_terminal_log_contract_across_transports` | core | Success, empty-result and admission-refusal terminal logs would stop carrying the root span ids, or roots would end with the wrong status or count. | docs/metric-contract.md (join keys, error attribution) |
| `test_generation_terminal_logs_join_specific_root` | core | Budget, model-error, non-stop and finalize failures would log without their root ids or leave a stream showing a final frame or accepted state. | docs/agent.md answer contract; docs/metric-contract.md |
| `test_condense_failure_terminal_log_joins_root` | core | A condensation failure escaping the chat turn would lose log correlation or the single request count. | docs/metric-contract.md (join keys) |
| `test_nonrecording_failures_never_invent_correlation` | core | With tracing disabled or sampled out, logs would invent trace ids no backend stores, misreporting sampling as backend loss. | docs/metric-contract.md (join keys) |
| `test_stream_response_failure_before_iteration_finishes_root` | core | A transport failure before the first stream frame would leak the root span or skip the request count. | docs/agent.md observability |
| `test_overlapping_awaited_requests_keep_independent_roots` | core | Concurrent requests could end or borrow each other's root span, mixing trace ids and request counts. | docs/agent.md observability |
| `test_overlapping_streams_do_not_end_or_borrow_other_root` | core | Cancelling one concurrent stream could end the other stream's root or log under the wrong trace. | docs/agent.md observability |
| `test_trace_disabled_without_endpoint` | infra | Tracing could start exporting or touching a provider when no OTLP endpoint is configured. | docs/deploy.md observability; #83 |
| `test_setup_tracing_builds_provider_and_appends_traces_path` | infra | Spans would be sent to the wrong collector URL, the provider not registered globally, or exporters stacked on repeated setup. | docs/deploy.md observability; #83 |
| `test_settings_knobs_reach_batch_processor` | infra | The documented queue and timeout settings would silently stop tuning the exporter. | #137 |
| `test_lifecycle_flush_and_swallows_errors` | infra | A collector failure at flush or shutdown could crash agent shutdown, or shutdown could leave the provider installed. | docs/deploy.md observability |
| `test_setup_tracing_resource_identity_and_service_name_precedence` | infra | Spans would lack image version and environment identity or carry empty values, and ingest could lose its service name override. | docs/metric-contract.md (Jaeger service name) |
| `test_search_request_renders_one_root_span` | core | A search request would lose its root span, request id join key, or put raw query text in span attributes. | docs/metric-contract.md trace contract; #529 |
| `test_answer_json_trace_tree` | core | The answer trace tree would lose prompt.build and llm.chat spans, their kinds and parentage, or model and citation attributes dashboards read. | docs/metric-contract.md trace contract |
| `test_chat_caller_model_ignored_on_span_and_response` | core | A caller-supplied model name would appear on the llm span while a different model actually ran. | #323 |
| `test_answer_stream_same_trace_id` | core | A streamed answer's retrieval, prompt and model spans could split across several trace ids. | docs/metric-contract.md trace contract |
| `test_answer_stream_failure_ends_root_span` | core | A mid-stream model failure would leave the root span open and not marked ERROR. | docs/agent.md observability; #137 |
| `test_retrieval_failure_marks_span_error` | core | A 502 retrieval failure would not mark the root ERROR with an exception event, hiding it from the error-hunt lookup. | docs/metric-contract.md (error attribution) |
| `test_disabled_tracing_records_nothing` | infra | Disabled tracing would still emit spans. | docs/deploy.md observability |
| `test_unhandled_error_handler_marks_current_span` | core | An unhandled 500 would not mark the active span ERROR, and span-marking with no active span could raise. | docs/metric-contract.md (error attribution); #83 |
| `test_search_stage_tree_identifier_bypass` | core | The retrieve span tree for identifier queries would lose its stages, parentage or rerank bypass reason, or put query text in attributes. | docs/metric-contract.md trace contract; #529 |
| `test_search_stage_tree_nl_with_rerank` | core | The rerank leg could be missing from natural-language traces, or its client kind and parent wrong. | docs/metric-contract.md trace contract |
| `test_search_stage_tree_split_reports_paths_and_mode` | core | Comparative split searches would stop reporting split paths and mode on both sync and async twins, hiding dual-path behavior from traces. | docs/metric-contract.md trace contract; #214 |
| `test_search_stage_tree_rerank_reports_bounded_alpha` | core | The rerank fusion alpha would stop being exported or leave its 0 to 1 range on either search twin. | docs/metric-contract.md trace contract |
| `test_search_stage_tree_trap_bypass` | core | Trap queries could stop bypassing rerank without a recorded reason. | docs/metric-contract.md trace contract |
| `test_async_search_stage_tree_matches_sync` | core | The async and sync search paths could drift in span names, attributes or hits, so serving traces would differ from tested behavior. | docs/agent.md observability; #83 |
| `test_span_attributes_bounded` | core | A span could carry free text such as the raw query, document ids or exception bodies instead of the enumerated bounded attributes. | #529; AGENTS.md hard boundaries |
| `test_current_trace_ids_follow_recording_spans_only` | core | Logs could carry trace ids when no span is active or when the span is unsampled, or omit them under a recording span. | docs/metric-contract.md (join keys); #185 |
| `test_parent_context_headers` | core | A valid upstream traceparent would not be adopted, or a malformed one would raise instead of starting a new root. | docs/metric-contract.md trace contract |
| `test_upstream_trace_join_or_root` | core | Requests carrying an upstream traceparent would not join the caller's trace, or untraced requests would not start a parentless root. | docs/metric-contract.md trace contract |
| `test_bearer_headers_carry_traceparent_only_inside_span_and_trim_key` | core | Outbound model calls would lose trace propagation, send it without a span, mishandle keyless legs, or send a whitespace-padded or blank bearer key. | docs/agent.md#http-model-contract |
| `test_search_root_covers_length_rejection` | core | Over-length query rejections would escape tracing or be marked ERROR although they are client faults. | docs/metric-contract.md (error attribution) |
| `test_serving_refusal_marks_server_span` | core | A 503 from the serving gate would leave the root unmarked, hiding refusals from the error lookup. | docs/metric-contract.md (error attribution) |
| `test_final_logs_join_the_trace` | core | Final search and answer log lines (JSON and SSE) would not carry the root span's trace id. | docs/metric-contract.md (join keys) |
| `test_lifespan_cycle_rebinds_without_duplicates` | infra | After a lifespan restart module tracers could stay no-op or the exporter could be duplicated. | docs/deploy.md observability |

## `tests/test_venue.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_venue_unset_is_dev` | core | An unset or blank VENUE selects RC, exposing the holdout and real corpus. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_venue_rc_case_and_whitespace_folded` | core | A correctly declared RC venue is not recognized. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_venue_typo_fails_closed` | core | A mistyped VENUE falls back to dev or RC instead of failing. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_default_golden_is_dev_only` | core | Default eval inputs include the frozen holdout. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_rc_default_adds_frozen_holdout` | core | RC runs do not include the holdout by default, or explicit selections get entries appended. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_explicit_holdout_refused_in_dev` | core | An explicit holdout path is accepted in dev. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_explicit_holdout_allowed_in_rc` | core | RC cannot select the holdout. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_real_corpus_collection_refused_in_dev` | core | Dev runs can read the real corpus collection. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_real_corpus_allowed_in_rc_and_dev_collections_without` | core | RC is refused the real corpus, or dev collections require RC. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_eval_answers_refuses_holdout_without_rc` | core | The answer eval entry point evaluates the holdout in dev. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_eval_chat_refuses_holdout_without_rc` | core | The chat A/B entry point evaluates the holdout in dev. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_harness_l2_refuses_real_corpus_without_rc` | core | The L2 harness runs against the real corpus in dev. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_gate_l1_refuses_holdout_without_rc` | core | The L1 gate evaluates the holdout in dev. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_replay_sweep_refuses_holdout_without_rc` | core | The replay sweep reads the holdout in dev. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_holdout_copy_elsewhere_still_requires_rc` | core | Copying the holdout to another directory bypasses the RC requirement. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_holdout_symlink_alias_resolves_consistently` | core | A symlink alias bypasses the holdout venue rule. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_holdout_pin_fails_closed` | core | A holdout with a missing, mismatched, misnamed or unreadable pin is parsed. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_holdout_venue_refuses_before_read` | core | The holdout or its pin is read before RC authorization. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_holdout_verified_bytes_are_the_parsed_bytes_and_next_read_revalidates` | core | Bytes swapped after verification are parsed (TOCTOU), or the next read skips revalidation. | docs/testing.md#evaluation (venue rule, issue #268) |
| `test_all_holdout_entry_points_refuse_bad_pin_before_effects` | core | Some eval or harness entry point reaches storage, embedder or output files before validating the holdout pin. | docs/testing.md#evaluation (venue rule, issue #268) |

## `tests/test_verify_golden.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_verify_entry_clean_pass` | core | A valid golden entry is failed or warned by verify-golden, blocking authoring. | docs/testing.md#evaluation (eval:verify-golden) |
| `test_verify_entry_fails_on_unknown_docs` | core | Golden entries pointing at documents absent from the corpus verify as clean, and duplicates go unnoticed. | docs/testing.md#evaluation (eval:verify-golden) |
| `test_verify_entry_fails_on_missing_heading_and_page` | core | Expected headings or pages that do not exist pass verification. | docs/testing.md#evaluation (eval:verify-golden) |
| `test_verify_entry_message_id_must_be_in_expected_doc_payload` | core | A message-id entry whose expected doc does not carry that id passes. | docs/testing.md#evaluation (eval:verify-golden) |
| `test_verify_entry_broken_sibling_trap_fails` | core | A trap whose expected doc lacks the query id (unwinnable) passes. | docs/testing.md#evaluation (eval:verify-golden) |
| `test_verify_entry_same_volume_sibling_trap_allowed` | core | A legitimate same-volume sibling trap is rejected. | docs/testing.md#evaluation (eval:verify-golden) |
| `test_verify_entry_nonexistent_sibling_trap_fails` | core | A trap against a nonexistent message id passes. | docs/testing.md#evaluation (eval:verify-golden) |
| `test_find_duplicate_queries` | core | Duplicate queries in the golden set are not detected. | docs/testing.md#evaluation (eval:verify-golden) |
| `test_load_entries_collects_per_line_errors` | core | Bad lines abort verification or are skipped silently. | docs/testing.md#evaluation (eval:verify-golden) |
| `test_verify_entry_system_code_binds_doc_and_page` | core | A system-code lookup entry is not bound to the doc and page that carry the code. | docs/testing.md#evaluation (issue #591) |
| `test_verify_entry_code_mention_outside_lookup_rows_is_not_bound` | core | A diagnostic question that merely mentions a code is wrongly bound to code pages. | docs/testing.md#evaluation (issue #591) |

## `tests/test_vllm_server_contract.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_stream_request_shape_targets_completions_with_usage_ask` | infra | Without it a trial server not honouring the stream usage request passes and token accounting silently becomes zero. | docs/agent.md#http-model-contract |
| `test_stream_tolerates_sse_keepalives_and_done_spacing` | core | Without it SSE keepalives or [DONE] spacing from vLLM break stream termination or content. | docs/agent.md#http-model-contract |
| `test_stream_tolerates_usage_only_and_finish_only_frames` | core | Without it valid usage-only and finish-only frames are treated as malformed and answers fail. | docs/agent.md#http-model-contract |
| `test_stream_without_usage_chunk_yields_zero_usage` | core | Without it a server that omits usage raises or retries instead of degrading to zero usage as documented. | docs/agent.md#http-model-contract |
| `test_nonstream_post_shape_and_explicit_finish` | infra | Without it the non-stream request shape or its explicit finish handling drifts from what servers expect. | docs/agent.md#http-model-contract |
| `test_nonstream_payload_without_explicit_finish_fails_closed` | core | Without it a non-stream reply with no finish, an error field or non-string content is shown as a complete answer. | docs/agent.md#http-model-contract |
| `test_nonstream_payload_healthy_variants` | core | Without it healthy replies with empty content, null error or length finish are rejected or mislabelled. | docs/agent.md#http-model-contract |
| `test_nonstream_client_sync_rejects_error_payload` | core | Without it an upstream error body is accepted or its text leaks into the exception. | docs/agent.md#http-model-contract |
| `test_nonstream_client_async_rejects_error_payload` | core | Without it the async path accepts an upstream error body or leaks its text. | docs/agent.md#http-model-contract |
| `test_nonstream_client_async_rejects_object_content` | core | Without it the async path stringifies object content into answer prose. | docs/agent.md#http-model-contract |
| `test_nonstream_nested_reasoning_tokens_mapped` | core | Without it reasoning token usage nested by vLLM is lost from cost and observability accounting. | docs/agent.md#http-model-contract |
| `test_stream_and_nonstream_bodies_agree_on_model_and_messages` | core | Without it the recovery POST asks for a different model or messages than the stream. | docs/agent.md#http-model-contract |
| `test_reasoning_effort_and_temperature_sent_only_when_set` | core | Without it effort routing breaks or unset params are sent as nulls that strict servers reject. | docs/agent.md#http-model-contract |
| `test_trailing_slash_base_url_builds_clean_completions_url` | core | Without it a trailing slash in LLM_BASE_URL produces a doubled slash that some servers 404. | docs/agent.md#http-model-contract |
| `test_bearer_header_sent_on_stream_leg_when_key_set` | core | Without it a gateway-guarded endpoint rejects the streaming leg for lack of the virtual key. | docs/agent.md#http-model-contract |
| `test_bearer_header_sent_on_nonstream_post_when_key_set` | core | Without it the non-stream and fallback requests are sent without the virtual key. | docs/agent.md#http-model-contract |
| `test_no_auth_header_sent_on_nonstream_post_when_key_unset` | core | Without it keyless setups send a bogus Authorization header. | docs/agent.md#http-model-contract |
| `test_chat_stream_empty_fallback_posts_without_stream_flag` | core | Without it an empty stream is not recovered by a plain POST or the POST still asks to stream. | docs/agent.md#http-model-contract |
| `test_chat_stream_fallback_sse_bytes_raise_without_fabricated_done` | core | Without it a fallback that returns SSE bytes is treated as a complete reply with an invented done. | docs/agent.md#http-model-contract |
| `test_chat_stream_empty_fallback_result_raises_without_token_or_done` | core | Without it an empty fallback result yields a success with no tokens. | docs/agent.md#http-model-contract |
| `test_chat_stream_rejected_fallback_propagates_without_done` | core | Without it a rejected fallback request is swallowed and a done is fabricated. | docs/agent.md#http-model-contract |

## `tests/test_webui.py`

| Test | Class | Guarantee | Owner |
|---|---|---|---|
| `test_ui_disabled_blocks_stream_and_history_with_zero_model_calls` | core | With the console disabled, any /ui entry point (shell, health, static, form, stream) could serve content or trigger model or retrieval calls instead of a 404. | #479; docs/adr/0004-operator-console-htmx.md |
| `test_ui_shell_has_strict_csp_and_no_remote_assets` | core | The console shell could ship without strict CSP and framing headers or load remote scripts, fonts or assets in the air-gap. | docs/adr/0004-operator-console-htmx.md; AGENTS.md hard boundaries (air-gapped) |
| `test_ui_chat_form_flow_renders_full_page_with_answer` | core | The no-JS form post would not render the answer, citations and history field. | docs/agent.md console sections |
| `test_ui_chat_htmx_returns_fragment_with_oob_history` | core | HTMX posts would return a full page or drop the out-of-band history update. | docs/agent.md console sections |
| `test_ui_chat_bad_history_json_is_ignored` | core | Malformed hidden history from the browser would break the turn instead of being ignored. | docs/agent.md console sections |
| `test_ui_chat_stream_sse_token_final_contract` | core | The console stream could lack token and single final frames, proxy-buffering headers, citations, hits or verification state. | docs/agent.md answer contract (SSE) |
| `test_ui_chat_stream_final_matches_answer_stream_final` | core | The console and /v1/answer streams could diverge in their final payload for the same evidence. | docs/agent.md answer contract |
| `test_ui_chat_stream_error_emits_error_event_without_final` | core | A mid-stream failure could emit a final frame or show upstream exception text to the browser. | AGENTS.md hard boundaries (client errors fixed); docs/agent.md#http-model-contract |
| `test_ui_chat_stream_parser_failure_carries_incomplete_state` | core | An upstream error frame after partial content could end the stream as accepted or expose the upstream error text. | #365; AGENTS.md hard boundaries |
| `test_ui_chat_retrieval_failure_renders_fixed_banner` | core | Retrieval failures on the form and HTMX paths could expose internal error text or silently swap only the user turn. | AGENTS.md hard boundaries (client errors fixed); docs/agent.md console sections |
| `test_ui_chat_body_cap_counts_splunk_context` | core | An oversized attached Splunk context could bypass the console body limit that /v1/chat enforces. | docs/agent.md console sections |
| `test_ui_healthz_badge_reflects_agent_status` | core | The console health badge could show Online while the agent is unready, or fail instead of showing Offline. | docs/agent.md console sections |
| `test_ui_chat_refused_when_generation_not_servable` | core | The console could reach retrieval or the model while the serving generation is not servable. | #391; docs/agent.md serving gate |
| `test_ui_static_serves_pinned_assets_and_blocks_traversal` | core | Static serving could fail for vendored assets or expose files outside the static root through path traversal. | docs/adr/0004-operator-console-htmx.md |
| `test_vendor_assets_match_pinned_sha256sums` | infra | Vendored console JavaScript and fonts could drift from their pinned checksums unnoticed. | docs/adr/0004-operator-console-htmx.md; AGENTS.md hard boundaries (vendor by pinned copy) |
| `test_vendored_fonts_are_local_pinned_and_served` | infra | Console fonts could load from a remote host or be unpinned or unserved, breaking the air-gapped console. | docs/adr/0004-operator-console-htmx.md; AGENTS.md hard boundaries (air-gapped) |
| `test_markdown_subset_renders_blocks_and_inline` | core | Model answers with headings, lists, emphasis, code and mainframe symbols could render as raw markers or mangled text in the console. | docs/agent.md console sections |
| `test_markdown_subset_renders_gfm_pipe_tables` | core | Pipe tables from the reasoning model would render as one run-on paragraph. | #749 |
| `test_markdown_subset_table_edge_cases` | core | Ragged rows, escaped pipes and incomplete table syntax could render broken tables or swallow text. | #749 |
| `test_markdown_subset_neutralizes_hostile_markup` | core | Retrieved or model text containing script, img, or javascript links could execute in the operator's browser (XSS). | docs/adr/0004-operator-console-htmx.md; AGENTS.md hard boundaries |
| `test_ui_chat_fragment_renders_markdown_not_markers` | core | HTMX answers could show raw markdown markers, or the operator's own markup could render as HTML. | docs/agent.md console sections |
| `test_markdown_subset_highlights_jcl` | core | JCL code blocks in answers would lose comment, keyword, string and number highlighting. | #337 |
| `test_markdown_subset_unlabeled_fence_content_sniffing` | core | Unlabeled code fences would be misclassified, so JCL or REXX would lose highlighting or prose would be styled as code. | #337 |
| `test_markdown_subset_code_adversarial` | core | Hostile markup inside code fences, unclosed or empty fences or unknown languages could execute markup or break rendering. | #337 |
| `test_ui_chat_passes_reasoning_effort` | core | The reasoning effort chosen in the console form would not reach the model call. | docs/agent.md console sections |
| `test_ui_invalid_reasoning_effort_rejected` | core | An unknown reasoning effort would be accepted by the stream or form instead of a 422. | docs/agent.md console sections |
| `test_console_offers_only_served_efforts_and_refuses_others_before_the_model` | core | The console could offer or send a reasoning effort the deployed model template does not serve, or present the refusal as a model outage. | #748 |
| `test_ui_fragment_verification_state_presentation` | core | Unverified drafts or refusals could be shown with a verified-citations title, or accepted scripts lose their review badge. | docs/agent.md answer contract; #365 |
| `test_ui_stream_missing_user_is_fixed_validation_error` | core | A stream request with no user turn would trigger retrieval or a model call instead of a fixed 422. | docs/agent.md answer contract |
| `test_ui_stream_retrieval_and_prompt_use_same_active_user` | core | Retrieval and the model prompt could use different user questions, or a trailing assistant or system turn could leak into the prompt. | tests/hazards/critical.json |
| `test_ui_stream_invalid_active_user_refuses_before_work` | core | Blank or over-length user messages would reach retrieval or the model instead of a fixed 422. | docs/agent.md answer contract |
| `test_ui_stream_unsearchable_active_user_refuses_before_work` | core | Control-character questions would reach retrieval or the model. | #579 |
| `test_ui_form_unsearchable_message_refuses_before_work` | core | Blank or control-character form messages would do retrieval or model work instead of showing the fixed banner. | #579 |
| `test_ui_form_normalizes_current_message_with_real_history` | core | The form could search with untrimmed text or lose real conversation history in the prompt. | docs/agent.md console sections |
| `test_ui_shell_declares_retention_notice_clear_control_and_status_region` | core | The console could drop its browser-storage retention notice, clear control or accessible status region. | docs/agent.md console browser contract (#372) |
| `test_console_turn_text_never_reaches_server_logs` | core | Operator questions and Splunk context, or excerpt text, could be written to server logs. | AGENTS.md hard boundaries (logs contain no secrets or manual text) |
| `test_ui_stream_final_carries_supplied_evidence_projection` | core | The final frame could omit the supplied excerpts the operator needs, or expose internal chunk ids. | #635 |
| `test_ui_fragment_renders_supplied_excerpt_as_data` | core | Hostile excerpt markup would render as HTML, or an unknown source version would be replaced by the request's. | #635; docs/adr/0004-operator-console-htmx.md |
| `test_ui_history_payload_never_carries_excerpt_text` | core | Excerpt text would be carried in browser history, persisting manual content beyond the current response. | #635 |
| `test_console_js_keeps_excerpts_out_of_persistence` | core | Console JavaScript could persist or export excerpt text, or insert it into the DOM as HTML. | #635; docs/agent.md console browser contract |

## Addendum: guarantee restoration after #770

#770 removed the only unit coverage of some lines in `src/` and `scripts/`. A follow-up
restored 73 mutation-verified collected items. Each new test failed when the line it protects was
broken. Every line that lost unit coverage is now one of three things: covered again, covered by
a named non-unit lane, or justified below as carrying no guarantee.

| Lines | Decision | Reason or where it is proven |
|---|---|---|
| `scripts/mock_vllm.py` (165), `src/mainframe_rag/mcp/mock.py` (112), `scripts/init_mock_zos.py` (30) | test tooling | Stand-ins for the model server and z/OS. They are exercised by the sim lane (`tests/test_integration_sim.py`, `tests/test_mcp_sim.py`), the load lane, agent-probes and the e2e kind lanes, all of which fail if a stand-in misbehaves. Production refusal of `MCP_MOCK_DIR` is the hygiene gate in `tests/test_mcp_agent.py`. |
| `scripts/build_golden_corpus.py` (188) | owning boundary elsewhere | Offline re-freeze builder. Its committed output is held by `tests/test_eval_gate.py::test_repo_golden_set_is_sha_pinned` and `::test_repo_golden_rows_opt_into_acceptance_states`. |
| `scripts/qdrant_cluster.py` 50-52, 67-68, 73, 89-95, 202-236 | HA lane | Container start/stop, port and image plumbing reached by the `cluster` fixture of `tests/test_ha_cluster.py`. The readiness, placement and exit-code helpers have unit tests again. |
| `src/mainframe_rag/eval/load.py` 144, 165-166 | load lane | Stage aggregation asserted by `tests/test_load_tier.py::test_search_load_has_zero_errors_and_full_timings`. |
| `src/mainframe_rag/eval/load.py` 71-73 | no guarantee | `nvidia-smi` absent fallback; the VRAM figure is informational. |
| `src/mainframe_rag/eval/answers.py` 192-193 | no guarantee | argparse already maps the bare `ValueError` to the same exit 2; only message text differs. |
| `src/mainframe_rag/eval/chat.py` 254-278, `src/mainframe_rag/eval/performance.py` 147, `scripts/query_demo.py` 52-85 | no guarantee | Human-readable layout that no caller or gate parses. The machine contracts (JSON output, HTML escaping, verdicts) are tested. |
| `src/mainframe_rag/eval/retrieval.py` 312-313 | no guarantee | Intermediate dict nodes for nested report keys; every caller passes flat keys. |
| `src/mainframe_rag/serve/profiles.py` 205 | no guarantee | `list_profiles` is re-exported by `serve/__init__.py` but has no in-repo caller; it returns `sorted(PROFILES)`. |
| `scripts/review_tooling.py` 254-263 | no guarantee | Advisory test-focus text for an LLM prompt; no verdict or exit code reads it. |
| `scripts/verify_golden.py` 161, 171 | no guarantee | WARN lines; the exit code depends only on failures. |
| `scripts/colab_runtime.py` 865 | no guarantee | Redundant type guard: the next statement raises the same `TypeError`, so it is an equivalent mutant. |
