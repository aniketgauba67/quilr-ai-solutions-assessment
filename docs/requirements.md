# Requirement-to-test checklist

Traceability from each assessment requirement to the component that implements it
and the tests that verify it. Component paths are relative to
`src/quilr_assessment/`, test paths to `tests/`.

The assessment's overview mentions five tasks, but its three pages give concrete
specifications for only Tasks 1–4; no fifth task was invented. See the README for
the full note on that discrepancy.

## PDF requirements and evaluation criteria

| ID / page | Requirement or evaluation focus | Component | Behavioral verification |
| --- | --- | --- | --- |
| T1.1 / 1 | Runnable Python or TypeScript server using the official MCP SDK | `task1_mcp_server/server.py` | Official SDK client completes initialization and tool discovery over stdio. |
| T1.2 / 1 | `get_customer_record`: string `customer_id` in `CUST-XXXXX` format | `task1_mcp_server/schemas.py`, `tools.py` | Valid identifier accepted; wrong type, missing field, bad prefix and suffix length rejected. The suffix is read as five ASCII digits (A1). |
| T1.3 / 1 | `trigger_refund`: customer ID, positive float amount, reason string of minimum length 10 | Same schemas and tools | Valid call; reasons of length 9/10; zero, negative and invalid amounts; missing fields. |
| T1.4 / 1 | Strict Zod/Pydantic validation; standard MCP JSON-RPC errors for invalid formats; protocol execution flow | `task1_mcp_server/server.py`, `schemas.py` | Assert wire error code and request ID, rather than only a tool `isError` result; unknown method/tool and malformed inputs. |
| T1.5 / 1 | Stdio transport; stdout only JSON-RPC; system/debug logs only stderr | `task1_mcp_server/__main__.py`, `server.py` | Subprocess captures both streams; every stdout line parses as a protocol message, including during invalid calls and logging. |
| T2.1 / 2 | Lightweight HTTP/JSON-RPC reverse proxy to a downstream mock MCP server | `task2_mcp_gateway/app.py`, `proxy.py`; `mocks/mcp.py` | HTTP client sends JSON-RPC through gateway to the local mock; request IDs and downstream response preserved. |
| T2.2 / 2 | Read Bearer token and extract user role | `task2_mcp_gateway/auth.py` | Valid admin/viewer credentials; missing, malformed and unknown token rejected before forwarding. |
| T2.3 / 2 | Forward `tools/list` transparently | `task2_mcp_gateway/proxy.py` | Original request and response preserved; no filtering of advertised tools. |
| T2.4 / 2 | For `tools/call`, inspect `params.name`; names beginning `admin_` require admin | `task2_mcp_gateway/auth.py`, `proxy.py` | Viewer may call ordinary tools; admin may call protected tools; prefix is checked on the parsed tool name. |
| T2.5 / 2 | Block unauthorized protected calls with code `-32001`, message `Unauthorized Tool Call`; never call downstream | Same authorization boundary | Exact JSON-RPC error with original ID; downstream spy records zero calls. |
| T2.6 / 2 | Correct JSON-RPC structures, HTTP forwarding, fine-grained authorization and clean errors | `task2_mcp_gateway/rpc.py`, `proxy.py` | Invalid JSON/envelopes/params; malformed names; valid notifications; batches according to A4; upstream failure sanitization. |
| T3.1 / 2 | Proxy generation requests to a provider and intercept response chunks in real time | `task3_stream_guardrail/app.py`, `provider.py`; `mocks/llm.py` | Local streaming provider observed through gateway; safe output reaches client before upstream finishes. |
| T3.2 / 2 | Detect sensitive patterns such as emails, SSNs and credit cards; replace with `[REDACTED]` | `task3_stream_guardrail/redactor.py` | Each supported pattern whole, split at every position, in one-character deltas and adjacent to other PII; exact replacement marker. |
| T3.3 / 3 | Responsive streaming without full-response accumulation; minimize TTFT and latency | `task3_stream_guardrail/redactor.py`, `provider.py` | Gated producer proves incremental release without timing-flaky sleeps; long stream proves retained state stays bounded; final suffix flushed safely. |
| T3.4 / 3 | Efficient async chunking/state, partial-text matching and memory use | Same redactor and streaming adapter | Network splits inside UTF-8/SSE records; long unterminated candidates; empty deltas; disconnect and provider cleanup. |
| T4.1 / 3 | Token-aware sliding window per tenant API key; example 50,000 tokens/minute | `task4_model_router/limiter.py`, `tokens.py` | `test_limiter.py` covers below/at/above budget, the exact one-token boundary, independent tenants and a request larger than the whole budget; `test_tokens.py` pins the documented approximation; `test_app.py::test_tenants_hold_independent_quotas_over_http`. |
| T4.2 / 3 | On-disk SQLite; accurate eviction/token tracking; concurrency | `task4_model_router/limiter.py` | `test_limiter.py` asserts the on-disk file, WAL mode and schema, half-open window cutoff, sliding (not resetting) behavior, eviction of inactive tenants, eviction committed on rejection, reopening the database, and 16 barrier-aligned writers on independent connections granting exactly the budget; `test_integration.py` repeats persistence and concurrency over real HTTP. |
| T4.3 / 3 | Secondary fallback when primary returns HTTP 429 or times out after 3000 ms | `task4_model_router/router.py`, `providers.py`; `mocks/completion.py` | `test_router.py` asserts 429 and deadline each cause exactly one secondary attempt, primary success never contacts the secondary, and eight other statuses do not fall back; `test_integration.py::test_the_default_primary_deadline_is_exactly_3000_milliseconds` pins the default; `test_providers.py` proves the deadline cancels the in-flight attempt. |
| T4.4 / 3 | Standardized gateway error payload, no raw stack traces/internal details | `task4_model_router/errors.py`, `app.py` | `mocks/completion.py` returns deliberately leaky upstream bodies (traceback, `/opt/models` path, host:port, `sk-` credential); `test_app.py` and `test_integration.py` assert none of it appears in the response, and that a handler defect still yields the `INTERNAL_ERROR` envelope; log assertions filter to `quilr_assessment` records. |
| T4.5 / 3 | Async concurrency, timeout races and graceful fallback | `task4_model_router/router.py` | `test_providers.py` covers slow-body and connect deadlines and that caller cancellation is not converted into a provider failure; `test_router.py::test_caller_cancellation_propagates_without_a_fallback`; `test_integration.py` asserts the abandoned primary is not awaited and the shared client timeout does not preempt a longer secondary deadline. |

## Additional engineering checks

These are self-imposed quality constraints, not additional claims about the PDF.

| Constraint | Verification |
| --- | --- |
| Reject unexpected fields/coercions; strict schemas and meaningful type hints | Extra fields, boolean/string numeric inputs, NaN/infinities, whitespace boundaries. |
| Local deterministic mocks; no paid infrastructure | Tests run offline; mocks exercise the actual proxy/router paths. |
| Protect tokens, API keys and PII | Capture logs/errors for sentinel values; disk stores fingerprints, never raw tenant keys. |
| Build algorithms separately before HTTP integration | Redactor and limiter tested as pure/standalone units first, then gateway integration tests. |
| No sensitive reference material in the submission | Ignore-rule checks and inspection of built distribution contents. |

## Task 1 implemented verification

Tests live in `tests/task1_mcp_server/`. All tests are deterministic and offline;
subprocess I/O, SDK requests and cleanup have finite timeouts.

| Requirement | Implementation | Executable coverage |
| --- | --- | --- |
| T1.1 official SDK and runnable server | `server.create_server`, `__main__.run/main` | `test_stdio.py`: `test_official_client_initializes_discovers_and_calls_tools` |
| T1.2 customer lookup and ID validation | `schemas.CustomerRecordInput`, `tools.get_customer_record` | `test_schemas.py`: valid/invalid IDs, missing ID, extras; `test_handlers.py`: lookup/not-found and matching MCP result content |
| T1.3 refund validation and execution | `schemas.RefundInput`, `tools.trigger_refund` | `test_schemas.py`: amount/reason boundaries, normalization, missing/extra fields; `test_handlers.py`: deterministic refund and unknown customer |
| T1.4 strict schemas and protocol errors | `server.execute_tool`, `sanitize_errors` | `test_handlers.py`: execution blocked on validation failure, SDK errors, sanitized internal failures and cancellation; `test_stdio.py`: error codes, IDs and recovery |
| T1.5 stdio isolation | `__main__.main`, official `stdio_server` | `test_stdio.py`: `test_wire_errors_preserve_ids_and_stdout_contains_only_protocol`; stderr sentinels and clean shutdown |

`test_sdk_drops_malformed_frames_and_recovers_without_leaking_payloads` records an
upstream SDK limitation: malformed raw JSON/envelopes are discarded without a
parse/envelope error response. This is distinct from tool argument validation,
which is implemented and tested to produce `-32602` protocol errors.

## Task 2 implemented verification

Tests live in `tests/task2_mcp_gateway/` and exercise the real authorization/proxy
logic with HTTP-boundary spies, plus a separate loopback integration test.

| Requirement | Implementation | Executable coverage |
| --- | --- | --- |
| T2.1 lightweight HTTP/JSON-RPC gateway and mock | `task2_mcp_gateway/app.py`, `proxy.py`; `mocks/mcp.py` | `test_integration.py`: both Uvicorn factories and actual HTTP forwarding; `test_mock.py`: deterministic tools and counter |
| T2.2 Bearer role extraction | `auth.py`, `config.py` | `test_auth.py`: both roles, scheme/token casing, malformed/missing/duplicate headers, constant-time comparisons, required distinct credentials |
| T2.3 transparent discovery | `app.py`, `proxy.py` | `test_gateway.py`: original request/response bytes, admin tools still visible to viewer; real HTTP comparison with direct downstream |
| T2.4 exact `admin_` role policy | `policy.py`, `app.py` | `test_auth.py`: exact-prefix cases; `test_gateway.py`: normal tools for both roles, admin protected success, escaped Unicode name rejection |
| T2.5 exact rejection with no downstream call | `app.py` before `forward` | `test_forbidden_call_has_exact_error_and_zero_downstream_requests`: HTTP transport spy remains empty across protected tools and ID types; exact code/message/ID assertions |
| T2.6 correct parsing/forwarding/clean errors | `rpc.py`, `proxy.py` | `test_rpc.py`: envelopes/IDs/params, ambiguous JSON and response validation; route tests: header/cookie isolation, bounds, deadlines/cancellation, sanitized infrastructure/RPC/tool failures |

The gateway's explicit HTTP policy, notification scope and limits are documented
in the root README. Malformed, unauthenticated and forbidden messages cannot enter
the downstream HTTP transport. No Task 1 source or test behavior was changed.

## Task 3 implemented verification

Tests live in `tests/task3_stream_guardrail/`. The pure redactor was tested before
HTTP integration. Parser tests vary network boundaries independently of logical
text deltas; real HTTP tests use async gates to establish streaming and cleanup.

| Requirement | Implementation | Executable coverage |
| --- | --- | --- |
| T3.1 proxy generation and intercept chunks in real time | `app.py`, `provider.open_provider/sanitized_events`; `mocks/llm.py` | `test_gateway.py`: `test_mock_through_gateway_redacts_all_supported_pii`; `test_integration.py`: `test_documented_factories_stream_ordinary_and_mixed_mock_scenarios` |
| T3.2 redact emails, SSNs and card candidates with `[REDACTED]` | `redactor.StreamingRedactor`, overlapping-match union | `test_redactor.py`: `test_all_two_way_splits_and_fixed_sizes_are_invariant`, `test_no_candidate_prefix_is_emitted_before_classification`; covers exact replacement, punctuation, adjacency and overlapping detections |
| T3.3 responsive output without full-response retention | Bounded redactor candidate, lazy SSE parser, awaited output | `test_safe_text_is_released_before_finish_and_pii_stays_pending`, `test_long_stream_retains_bounded_state_without_historical_output`, `test_overlong_candidates_are_discarded_once_with_bounded_state`; real HTTP `test_real_http_emits_safe_text_while_provider_completion_is_gated` |
| T3.4 async chunk/state handling and memory efficiency | `stream.SSEParser/parse_delta`, `provider.sanitized_events`, `app.ProviderStreamingResponse` | `test_stream.py`: `test_every_network_split_preserves_unicode_and_sse_framing`, event/ignored-field byte bounds and lazy parsing; `test_gateway.py`: idle deadline, truncated stream and caller cancellation; real HTTP `test_real_http_client_disconnect_closes_blocked_provider_generator` |

Gateway tests also verify sanitized opening/stream errors, strict request rejection
before provider calls, metadata policy, and caller-header/cookie isolation. No
universal PII detection or full OpenAI API compatibility is claimed. The supported
grammar, terminal-event policy and limits are documented in the README and A5.

## Task 4 implemented verification

Tests live in `tests/task4_model_router/`. The limiter was tested against real
on-disk SQLite before HTTP integration. Concurrency uses a shared barrier and
independent connections rather than sleeps; provider deadlines are injected so
tests do not repeatedly wait three seconds, and the 3000 ms default is asserted
separately.

| Requirement | Implementation | Executable coverage |
| --- | --- | --- |
| T4.1 token-aware sliding window per tenant key | `limiter.TokenWindowLimiter`, `tokens.count_tokens`, `router.ModelRouter._admit` | `test_limiter.py`: `test_exactly_at_the_limit_is_admitted_and_one_more_token_is_not`, `test_tenants_have_independent_budgets`, `test_a_single_request_larger_than_the_budget_is_rejected`; `test_tokens.py`: documented counts and monotonicity; `test_router.py`: `test_reservation_covers_prompt_plus_requested_output` |
| T4.2 on-disk SQLite, eviction and concurrency | `limiter` schema, `BEGIN IMMEDIATE` admission, `asyncio.to_thread` | `test_limiter.py`: `test_database_is_created_on_disk_with_the_expected_schema`, `test_window_boundary_is_exact_and_expired_tokens_free_capacity`, `test_the_window_slides_rather_than_resetting`, `test_admission_evicts_stale_rows_of_other_inactive_tenants`, `test_state_survives_reopening_the_database`, `test_simultaneous_writers_on_separate_connections_cannot_oversubscribe`; `test_integration.py`: `test_quota_is_enforced_and_persists_across_gateway_restarts`, `test_concurrent_http_requests_cannot_exceed_the_budget` |
| T4.3 fallback on primary 429 or 3000 ms timeout | `router.ModelRouter.complete`, `providers.FALLBACK_FAILURES` | `test_router.py`: `test_primary_success_never_contacts_the_secondary`, `test_primary_rate_limiting_falls_back_once`, `test_primary_timeout_falls_back_once`, `test_other_primary_failures_do_not_fall_back`; `test_integration.py`: `test_primary_429_fails_over_to_the_secondary`, `test_primary_deadline_fails_over_without_waiting_for_the_primary`, `test_the_default_primary_deadline_is_exactly_3000_milliseconds` |
| T4.4 standardized error payload without upstream detail | `errors.GatewayError/error_payload`, `app.unexpected_failure` | `test_app.py`: `test_upstream_error_bodies_never_reach_the_client`, `test_unexpected_failures_return_the_standardized_payload`, `test_logs_never_contain_the_api_key_prompt_or_upstream_body`; `test_providers.py`: `test_failure_logs_stay_fixed_and_carry_no_upstream_text`; `test_integration.py`: `test_both_providers_failing_returns_the_standardized_payload` |
| T4.5 async concurrency, timeout races, graceful fallback | `providers.complete` under `asyncio.timeout`, single shared client | `test_providers.py`: `test_deadline_covers_the_whole_attempt_and_closes_the_connection`, `test_slow_body_also_hits_the_total_deadline`, `test_caller_cancellation_is_not_converted_into_a_provider_failure`; `test_router.py`: `test_caller_cancellation_propagates_without_a_fallback`, `test_secondary_timeout_after_primary_timeout_is_still_bounded`; `test_integration.py`: `test_a_longer_secondary_deadline_is_not_preempted_by_the_client` |

Tenant and accounting behavior is verified separately: raw API keys never reach the
database or the logs (`test_raw_tenant_keys_are_never_written_to_the_database`,
`test_the_database_file_holds_fingerprints_and_no_raw_keys`,
`test_stored_rows_never_contain_the_raw_api_key`), a rate-limited request contacts no
provider (`test_quota_rejection_contacts_no_provider`), quota storage failure fails
closed with 503 (`test_storage_failure_fails_closed_without_provider_calls`), a failed
request retains its reservation (`test_a_failed_request_keeps_its_reservation_until_expiry`),
a fallback is charged once (`test_a_fallback_request_is_charged_once`) and reported
usage cannot exceed the authorized reserve
(`test_reported_usage_cannot_exceed_the_authorized_reservation`). Token counting is an
explicit approximation, not a provider billing ledger; see A7.

## Implementation assumptions

- **A1 — IDs, numbers and reasons:** `CUST-XXXXX` is read as exactly five ASCII
  digits (`CUST-12345`); the PDF does not define the suffix alphabet. IDs are neither
  trimmed nor coerced. Extra fields, booleans, nonfinite/zero/negative amounts and
  numeric strings are rejected; positive finite JSON numbers, including integers, are
  accepted. Reason length is measured after trimming and collapsing whitespace runs
  with `str.split()`, then requiring at least 10 characters. Non-string reasons are
  not coerced and their semantic content is not assessed.
- **A2 — Tool business behavior:** Customer records and refunds are deterministic
  synthetic simulations. No actual payments, customer integrations or durable refund
  ledger are specified.
- **A3 — Authentication:** Configured opaque demo tokens map to roles; trust is never
  derived from an unsigned or unverified token claim. Missing/invalid credentials
  produce HTTP 401; an authenticated forbidden tool call produces the required
  JSON-RPC `-32001` response with HTTP 200. Both configured tokens are required and
  distinct; no working credentials default in source. Header scheme comparison is
  case-insensitive, credentials are case-sensitive, duplicate Authorization headers
  are rejected. Provider URLs are configuration, never caller input.
- **A4 — Gateway scope:** One small POST endpoint accepts one JSON-RPC message per
  request. The envelope is validated before method-specific authorization, batch
  arrays are rejected explicitly, and other authenticated methods pass through after
  validation.
  Valid notifications receive no JSON-RPC response; a denied protected notification
  is not forwarded and uses an empty HTTP response. Preserve IDs for requests.
  This is the HTTP/JSON-RPC proxy requested, with a stateless local downstream mock;
  a complete remote MCP session/transport implementation is outside this task.
  Task 2 preserves string/finite-number/null IDs; boolean/object/array IDs are invalid.
  Duplicate JSON keys and non-UTF-8-safe/nonfinite JSON are rejected before forwarding.
  Accepted success bodies remain byte-for-byte; downstream error messages/data are
  sanitized. Only controlled JSON headers cross the proxy; no caller bearer tokens,
  cookies or custom headers. Request/response limits are 64/256 KiB; body-read timeout
  is five seconds, downstream total timeout defaults to five seconds.
- **A5 — PII and transport:** Redaction covers ASCII email addresses with a
  dot-separated local part of at most 64 characters (letters/digits, `_ % + -`),
  DNS-style domain labels up to 63 characters, total domain length up to 253, and
  an alphabetic final label of 2–63 characters. Quoted/Unicode email forms are
  unsupported. SSNs use three-two-four ASCII digits with hyphens. Cards use 13–19
  ASCII digits with optional single spaces/hyphens and no Luhn requirement; double
  separators are unsupported. Longer separated numeric aggregates are conservatively
  redacted to cover adjacent values; contiguous 20+ digit runs are outside the card
  grammar. Overlapping detections are unioned before replacement.
  Retain at most 512 unresolved characters; character 513 emits one marker and
  discards that candidate's continuation until a boundary. This can over-redact
  harmless text. The SSE parser permits at most 64 KiB of normalized event bytes,
  including ignored fields, with CRLF counted as one newline. Network chunks need
  not align with UTF-8, SSE or PII boundaries.
  Support one assistant text choice at index 0. Preserve known bounded metadata
  and numeric usage from the trusted provider without PII inspection; drop unknown
  metadata and reject tool/refusal/logprob channels. A finish reason finalizes text,
  usage may follow, and `[DONE]` completes a normal stream. Premature EOF or malformed
  data emits a sanitized stream error without flushing unresolved text or emitting
  `[DONE]`. Opening/idle-read timeouts default to ten seconds; there is no full-stream
  deadline. These are explicit demonstration limits, not universal PII recognition.
- **A6 — Quota:** 50,000 tokens over 60 seconds is the configurable default. Events
  in `(now - window, now]` count; an event exactly `window` old has left the window.
  Timestamps are persisted UTC epoch seconds, so state survives restarts; tests inject
  a clock. This relies on a reasonably synchronized host clock. Tenant API keys are
  fingerprinted with BLAKE2b (optionally keyed by `QUILR_TENANT_FINGERPRINT_KEY`)
  before storage; raw keys are never written or logged. A well-formed bearer key is
  treated as an opaque tenant identity unless `QUILR_TENANT_API_KEYS` restricts the
  set, so quota evasion by key rotation is possible in the open demo mode.
- **A7 — Token accounting:** Prompt tokens plus the requested output allowance are
  reserved atomically before provider I/O. `tokens.count_tokens` is an explicit local
  approximation (about four characters per alphanumeric run, one token per symbol)
  shared with the mock providers so reported usage is verifiable; it is not a
  real-model token count. Client-supplied counts are never trusted. One logical
  request is charged once, including when it falls back. On success the reservation is
  reconciled down to the reported usage and never raised above the reserve, so a
  misreporting provider cannot inflate a tenant's consumption. On failure, timeout or
  cancellation the full reservation stands until it expires, because actual usage is
  unknown; this can underutilize quota. A real provider adapter needs its own
  tokenizer and usage reconciliation.
- **A8 — Fallback and timeout:** A 3.0-second total deadline covers connection,
  request and full body read of the primary attempt. Only primary HTTP 429 and that
  deadline trigger one secondary attempt; local quota rejection, other HTTP statuses,
  invalid or oversized provider responses, transport failures and caller cancellation
  do not. The secondary has its own finite deadline, and the shared HTTP client
  timeout is the longer of the two so it cannot preempt either. The router closes its
  own connection when a deadline expires and never uses a late response, but it cannot
  stop a remote server that keeps working. A cancelled upstream attempt may still have
  consumed provider tokens; logical quota is not a provider billing ledger. Quota
  storage failure fails closed with a sanitized 503 rather than serving unmetered.

See [architecture.md](architecture.md) for component boundaries and SDK verification.
