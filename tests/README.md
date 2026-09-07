# Test plan

Task 1 has validation unit tests (`test_schemas.py`), business/SDK handler tests
(`test_handlers.py`) and real subprocess stdio tests (`test_stdio.py`). Task 2 has
auth/config/policy, JSON-RPC, route and mock unit tests plus real loopback integration.
Task 3 has pure redactor, SSE parser, provider/route and mock tests, followed by real
HTTP streaming and cancellation checks. Task 4 has tokenizer and on-disk limiter unit
tests, provider-boundary and routing-policy tests, HTTP route tests and real loopback
integration. The suite is 724 tests: 107, 212, 231 and 174 per task. From the
activated environment:

```bash
python -m pytest tests/task1_mcp_server -q
python -m pytest tests/task2_mcp_gateway -q
python -m pytest tests/task3_stream_guardrail -q
python -m pytest tests/task3_stream_guardrail -m integration -q
python -m pytest tests/task4_model_router -q
python -m pytest tests/task4_model_router -m integration -q
python -m pytest -q
```

Tests marked `integration` use real subprocesses and loopback sockets, so they need
local bind permission; every I/O and cleanup wait is bounded. Task 1's exercise the
installed entry point through the official SDK client and raw pipe exchanges. See the
README for the installation refresh step after source changes.

| Directory | Coverage |
| --- | --- |
| `task1_mcp_server/` | Pydantic boundaries, mock results and official SDK stdio protocol/errors/log isolation |
| `task2_mcp_gateway/` | authentication, authorization before forwarding, JSON-RPC, HTTP errors, headers, cookies, limits and deadlines |
| `task3_stream_guardrail/` | exhaustive PII splits, overlap/overflow, UTF-8/SSE framing, sanitized failures, header isolation, idle deadlines, live output and cancellation |
| `task4_model_router/` | token approximation, on-disk sliding window with injected clocks, barrier-aligned concurrent writers, eviction and persistence, provider deadlines and failure classification, fallback policy, reservation/reconciliation accounting, error and log sanitization |

Conventions: synthetic data only, injected clocks and controlled async gates instead
of sleeps, `tmp_path` for SQLite files, independent connections in concurrency tests,
and mocks that exercise the real gateway logic rather than stubbing it out. Streaming
and timeout behavior is tested over real local HTTP wherever an in-process client
would hide it. No test requires external network access or a paid provider.

Task 3's gated HTTP test observes safe output before allowing the provider to
finish. A separate gated disconnect test verifies the blocked provider generator
closes. Parser tests split UTF-8 and SSE framing independently of redactor delta
tests; mock fixture tests use an independent SSE encoder.

Task 4's concurrency test aligns sixteen threads on a `threading.Barrier`, each with
its own SQLite connection, and asserts the granted total equals the budget exactly
with no storage errors — reordering the admission into a read-then-write deferred
transaction makes it fail. Provider deadlines are injected so tests never wait three
seconds repeatedly; a separate test asserts the default is exactly 3000 ms. Timeout
behavior that depends on real socket timeouts is tested over loopback HTTP, because
`ASGITransport` ignores httpx timeouts entirely. The mock providers return
deliberately leaky error bodies so sanitization assertions have something to catch,
and log assertions filter to `quilr_assessment` records so third-party loggers do not
mask a real leak.

See [the requirement checklist](../docs/requirements.md) for the requirement-to-test map.
