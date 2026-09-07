# Quilr MCP and LLM gateway assessment

Four independently runnable services built for the Quilr Forward Deployed Engineer
assessment:

1. **MCP server** — customer lookup and simulated refund tools over stdio, with
   strict validation and standard JSON-RPC errors.
2. **MCP security gateway** — an HTTP/JSON-RPC reverse proxy that authenticates a
   bearer role and authorizes `admin_*` tool calls before forwarding.
3. **Streaming PII guardrail** — redacts emails, SSNs and card numbers across
   streamed response deltas without buffering the full response.
4. **Model router** — a token-aware sliding-window rate limiter persisted in on-disk
   SQLite, with automatic failover from a primary to a secondary provider.

Every upstream is a deterministic local mock, so the demos and the whole test suite
run offline with no paid services and no API keys.

## Architecture

| Task | Responsibility | Main technology |
| --- | --- | --- |
| 1 | Customer lookup and simulated refund tools over stdio | Official `mcp` SDK, Pydantic |
| 2 | Authenticate, authorize tool calls, forward HTTP/JSON-RPC | FastAPI, httpx |
| 3 | Redact PII across text deltas while streaming | asyncio, FastAPI, httpx |
| 4 | Admit per-tenant token reservations; route with timeout/429 fallback | SQLite, asyncio, httpx, FastAPI |

One Python distribution holds four independent task packages. Deterministic local
upstreams in `mocks/` back every demo and test. Utilities are shared only where a
concrete need appeared, such as the tokenizer used by both the Task 4 router and its
mock providers.

## Structure

```text
src/quilr_assessment/
  task1_mcp_server/
  task2_mcp_gateway/
  task3_stream_guardrail/
  task4_model_router/
  mocks/
tests/                       # One directory per task, mirroring the source layout
docs/requirements.md          # Requirements -> components -> tests
docs/architecture.md          # Design decisions, assumptions and boundaries
.env.example                  # Task 2–4 settings; no credentials
pyproject.toml                # Packaging, dependencies, pytest and Ruff
uv.lock                       # Resolved dependencies for repeatable local setup
```

## Development setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). From this directory:

```bash
uv sync --locked --extra dev --no-editable
source .venv/bin/activate
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

The suite covers all four tasks. After changing source, rebuild the installed
package before rerunning tests:

```bash
uv sync --locked --extra dev --no-editable --reinstall-package quilr-assessment
```

The non-editable install avoids environments where an editable install's `.pth` file
is hidden and therefore skipped; the reinstall flag stops uv reusing a cached build
after source-only edits. Omit `--no-editable` for automatic source updates. Standard
pip also accepts `python -m pip install '.[dev]'` in an existing virtual environment,
but does not use `uv.lock`.

## Task 1: customer/refund MCP server

After installation and environment activation:

```bash
python -m quilr_assessment.task1_mcp_server
```

The process waits for newline-delimited MCP messages on stdin. Its startup
diagnostic goes to stderr; stdout is reserved for protocol responses. An MCP host
can launch the virtual environment's Python executable with arguments
`["-m", "quilr_assessment.task1_mcp_server"]`. No environment variables, HTTP server,
database or external service are needed for this task.

To invoke both tools through the official SDK, run this separate client:

```bash
python - <<'PY'
import asyncio
import sys
from mcp import Client, MCPError
from mcp.client.stdio import StdioServerParameters

async def main():
    process = StdioServerParameters(
        command=sys.executable,
        args=["-m", "quilr_assessment.task1_mcp_server"],
    )
    async with asyncio.timeout(15):
        async with Client(process, mode="legacy", read_timeout_seconds=5) as client:
            print([tool.name for tool in (await client.list_tools()).tools])
            record = await client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
            print(record.structured_content)
            refund = await client.call_tool("trigger_refund", {
                "customer_id": "CUST-12345", "amount": 12.5, "reason": "Duplicate payment",
            })
            print(refund.structured_content)
            try:
                await client.call_tool("get_customer_record", {"customer_id": "cust-12345"})
            except MCPError as error:
                print(error.code, error.message)

asyncio.run(main())
PY
```

These prints belong to the client. Expected results include the two tool names,
a synthetic customer, a `simulated` refund with a stable `MOCK-` identifier, and
`-32602 Invalid params` for the invalid call.

```bash
python -m pytest tests/task1_mcp_server -q
python -m pytest tests/task1_mcp_server -m integration -q
```

Task 1 behavior and decisions:

- IDs are exactly `CUST-` followed by five ASCII digits, with no trimming or coercion.
- Amounts accept positive finite JSON numbers, including integers; booleans and
  numeric strings are rejected. This simulation uses floats, not a currency ledger.
- Reasons must be strings. Surrounding whitespace is removed and each internal
  whitespace run becomes one space, then a 10-character minimum is enforced.
  `Valid text` passes; `a        b` fails. Reasons are not echoed or logged.
- Both models reject additional fields. Advertised input schemas come from the
  Pydantic models used for runtime validation.
- Only `CUST-12345` exists. Either tool returns a normal `not_found` result for other
  valid IDs. Refunds are deterministic simulations: no payments, storage or real
  idempotency guarantees. Results include matching JSON text and structured content.
- The official low-level SDK owns MCP framing, initialization and dispatch. Invalid
  arguments/unknown tools raise JSON-RPC `-32602`; unknown methods use `-32601`.
  Validation error data includes only known field names. A supported middleware
  maps unexpected request failures to sanitized `-32603` errors and preserves
  cancellation. Logs contain fixed diagnostics on stderr.
- **SDK limitation:** in the pinned 2.1.1 stdio transport, invalid raw JSON or invalid
  JSON-RPC envelopes are dropped without a `-32700`/`-32600` reply. Recovery is tested.
  Invalid tool arguments inside valid requests do receive `-32602`. No custom
  protocol parser replaces the SDK to change its raw-frame behavior.

## Task 2: MCP security gateway

`Agent -> FastAPI gateway -> local mock MCP service`. Authentication resolves an
opaque bearer token to `admin` or `viewer`; a separate policy protects tool names
beginning exactly with `admin_`. The downstream HTTP request is made only after
authentication, JSON-RPC validation and authorization succeed.

Use the activated environment in each terminal. Start the downstream mock:

```bash
python -m uvicorn quilr_assessment.mocks.mcp:create_app --factory --host 127.0.0.1 --port 9000 --no-access-log
```

In another terminal, configure and start the gateway. These two values are public
demo credentials, not production secrets:

```bash
export QUILR_ADMIN_TOKEN=local-admin-demo
export QUILR_VIEWER_TOKEN=local-viewer-demo
export QUILR_MCP_DOWNSTREAM_URL=http://127.0.0.1:9000/mcp
export QUILR_MCP_TIMEOUT_SECONDS=5
python -m uvicorn quilr_assessment.task2_mcp_gateway.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

From a third terminal, discover tools as viewer; the result includes `admin_reset_key`:

```bash
curl -sS http://127.0.0.1:8000/mcp -H 'Content-Type: application/json' -H 'Authorization: Bearer local-viewer-demo' \
  -d '{"jsonrpc":"2.0","id":"list-1","method":"tools/list"}'
```

Invoke a normal tool as viewer:

```bash
curl -sS http://127.0.0.1:8000/mcp -H 'Content-Type: application/json' -H 'Authorization: Bearer local-viewer-demo' \
  -d '{"jsonrpc":"2.0","id":"normal-1","method":"tools/call","params":{"name":"get_status","arguments":{}}}'
```

Invoke a protected tool as admin:

```bash
curl -sS http://127.0.0.1:8000/mcp -H 'Content-Type: application/json' -H 'Authorization: Bearer local-admin-demo' \
  -d '{"jsonrpc":"2.0","id":"admin-1","method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

The same tool is rejected for a viewer, without reaching downstream:

```bash
curl -sS http://127.0.0.1:8000/mcp -H 'Content-Type: application/json' -H 'Authorization: Bearer local-viewer-demo' \
  -d '{"jsonrpc":"2.0","id":"blocked-1","method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

Expected HTTP 200 body:

```json
{"jsonrpc":"2.0","id":"blocked-1","error":{"code":-32001,"message":"Unauthorized Tool Call"}}
```

Run Task 2 tests:

```bash
python -m pytest tests/task2_mcp_gateway -q
```

The integration test uses real loopback sockets and requires local bind permission.
Unit/route tests use HTTP transports with request spies. The mock additionally
counts every `/mcp` request in `app.state.request_count`; its tools are `get_status`,
`admin_reset_key` and `demo_error` (a deterministic tool failure).

Task 2 boundary choices:

| Condition | HTTP response | JSON-RPC behavior |
| --- | --- | --- |
| Missing/malformed/unknown bearer credential | 401 + `WWW-Authenticate: Bearer` | HTTP error object; body is not parsed first |
| Invalid JSON, envelope or tool parameters | 400 | `-32700`, `-32600` or `-32602`, with original valid ID where available |
| Authenticated forbidden tool request | 200 | Exact `-32001 Unauthorized Tool Call` with original ID |
| Downstream HTTP/connection/protocol failure | 502 | `-32002 Downstream unavailable` |
| Downstream deadline | 504 | `-32003 Downstream timeout` |
| Unexpected gateway failure | 500 | `-32603 Internal error` |

Only single UTF-8 JSON-RPC messages are supported. Batches, duplicate JSON members,
nonfinite numbers and unpaired Unicode surrogates are rejected. Other methods pass
through after authentication/envelope validation. Notifications have no JSON-RPC
response; allowed/denied notifications return empty HTTP 204, while downstream
failures can return an empty 502/504. An explicit `id: null` remains a request.

Accepted request bytes and successful downstream response bytes are preserved.
Outbound application headers are limited to JSON content type/accept and identity
encoding; HTTPX supplies transport headers. Caller credentials, cookies and custom
headers are not forwarded. Downstream cookies are neither returned nor stored;
redirects and environment proxy settings are disabled. Downstream JSON-RPC error
messages/data and MCP tool-error content are replaced with generic messages while
retaining their error codes or tool-error flag. Successful content is trusted
downstream data, not inspected for PII.

Requests are limited to 64 KiB with a five-second body-read deadline; responses to
256 KiB with a total downstream deadline (default five seconds, configurable up to
30). Compressed bodies are rejected. This is a stateless assessment proxy, not a
full MCP session/transport implementation. The mock is unauthenticated and must
remain on the trusted side of the gateway; its protected action is only simulated.

## Task 3: streaming PII guardrail

`Client -> /generate gateway -> /stream mock provider`. The gateway parses upstream
SSE records, redacts assistant text across deltas, and emits SSE as generation
continues. The mock returns fixed synthetic text and never echoes the prompt.

Start the mock in an activated environment:

```bash
python -m uvicorn quilr_assessment.mocks.llm:create_app --factory --host 127.0.0.1 --port 9001 --no-access-log
```

In another terminal, start the gateway:

```bash
export QUILR_LLM_STREAM_URL=http://127.0.0.1:9001/stream
export QUILR_LLM_TIMEOUT_SECONDS=10
python -m uvicorn quilr_assessment.task3_stream_guardrail.app:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log
```

Stream the mixed scenario with one-character provider deltas:

```bash
curl -N -sS http://127.0.0.1:8001/generate -H 'Content-Type: application/json' \
  -d '{"prompt":"Demonstrate redaction","scenario":"mixed","chunk_size":1,"delay_ms":20}'
```

The concatenated `choices[0].delta.content` is:

```text
Welcome! Café support: [REDACTED]; SSN: [REDACTED]; card: [REDACTED]. Thank you.
```

Requests require a nonblank UTF-8 string `prompt` of at most 4,000 characters.
Optional fields are `scenario` (default `mixed`), integer `chunk_size` (1–256,
default 7), and integer `delay_ms` (0–1,000, default 0). Additional fields and type
coercions are rejected. Scenarios are `ordinary`, `email`, `ssn`, `card`, `mixed`,
`slow`, `http_error`, `malformed`, and `disconnect`; `slow` uses a 100 ms delay when
no nonzero delay is supplied. These controls belong to the local demonstration;
a real provider adapter would map the request to its generation API.

```bash
python -m pytest tests/task3_stream_guardrail -q
python -m pytest tests/task3_stream_guardrail -m integration -q
```

Redaction and buffering decisions:

- The pure redactor retains at most **512 unresolved characters**, releasing text
  when a candidate can be classified. Character 513 triggers one `[REDACTED]`
  marker and discards that candidate's continuation until a boundary. This can
  redact long harmless tokens, but never releases a sensitive prefix to save space.
- Supported emails use ASCII letters/digits and `_ % + -` in dot-separated local
  parts (maximum 64 characters), DNS-style labels (1–63 characters), a domain of
  at most 253 characters, and an alphabetic final label of 2–63 characters.
  Leading/trailing sentence periods are handled. Quoted and Unicode email
  addresses are outside this grammar.
- SSNs use the ASCII `123-45-6789` shape. Card candidates contain 13–19 ASCII digits
  with optional single spaces/hyphens; no Luhn check is required. Longer separated
  numeric aggregates are conservatively redacted because they can contain adjacent
  cards. Contiguous 20+ digit runs are outside the card grammar; the overflow rule
  still applies. Double separators are unsupported. Overlapping detections are
  merged so a card match cannot expose part of a recognized email.
- The SSE parser retains one event of at most **64 KiB of normalized bytes**,
  including ignored fields; CRLF counts as one newline. It handles UTF-8 code
  points and SSE records split across network chunks. Memory does not grow with
  the complete response. Waiting for candidate boundaries trades some text latency
  for safe cross-chunk classification.

The supported upstream format is OpenAI-style SSE with one choice at index 0 and
assistant text content. Known bounded metadata and numeric usage fields are
preserved; unknown metadata is dropped. Metadata comes from a trusted provider
and is **not inspected for PII**. Tool calls, refusals, non-null log probabilities
and additional choices are rejected rather than exposed as alternate text channels.

A `finish_reason` finalizes text; a following usage-only event is allowed. `[DONE]`
marks normal completion and can also finalize text without a preceding finish
event. Malformed data, premature EOF without `[DONE]`, or a read failure produces
a fixed `event: error` and closes the stream without a success marker or flushing
unresolved text. Failures before response headers use sanitized HTTP 502/504;
invalid generation inputs use a fixed HTTP 422 error.

The gateway limits input to 32 KiB with a five-second body-read deadline. The
configured timeout bounds opening the provider response and each subsequent idle
read, not total generation duration. Requests use a lifespan-owned async client;
backpressure is awaited and disconnects close the upstream response. Caller
headers/cookies, redirects and environment proxies are not propagated. Real HTTP
tests gate provider completion to prove early output and cancellation cleanup.

## Task 4: rate-limiting and model fallback router

`Client -> /v1/completions router -> primary provider, else secondary provider`. Every
request is admitted against a per-tenant token quota persisted in on-disk SQLite
before any provider is contacted. The primary attempt runs under a 3000 ms deadline;
HTTP 429 or that deadline triggers exactly one secondary attempt.

Start the two mock providers in an activated environment:

```bash
python -m uvicorn quilr_assessment.mocks.completion:create_app --factory --host 127.0.0.1 --port 9002 --no-access-log
```

In another terminal, configure and start the router:

```bash
export QUILR_PRIMARY_MODEL_URL=http://127.0.0.1:9002/primary/completions
export QUILR_SECONDARY_MODEL_URL=http://127.0.0.1:9002/secondary/completions
export QUILR_RATE_LIMIT_DB_PATH=./var/rate_limit.sqlite3
python -m uvicorn quilr_assessment.task4_model_router.app:create_app --factory --host 127.0.0.1 --port 8002 --no-access-log
```

The bearer token is the tenant API key. It identifies who is metered; it is not a
verified credential in this demo (see the limitation below).

Normal completion served by the primary:

```bash
curl -sS http://127.0.0.1:8002/v1/completions -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer tenant-demo-key' \
  -d '{"prompt":"Explain retry budgets","max_output_tokens":128}'
```

```json
{"provider":"primary","fallback_used":false,"text":"Primary model reply: ...",
 "usage":{"prompt_tokens":6,"completion_tokens":21,"total_tokens":27,
          "reserved_tokens":134,"charged_tokens":27}}
```

Primary returns 429, so the secondary answers:

```bash
curl -sS http://127.0.0.1:8002/v1/completions -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer tenant-demo-key' \
  -d '{"prompt":"Explain retry budgets","demo":{"primary_scenario":"rate_limited"}}'
```

Primary exceeds 3000 ms, so the secondary answers after roughly three seconds:

```bash
time curl -sS http://127.0.0.1:8002/v1/completions -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer tenant-demo-key' \
  -d '{"prompt":"Explain retry budgets","demo":{"primary_delay_ms":5000}}'
```

Both providers fail, so the router returns its standardized payload:

```bash
curl -sS http://127.0.0.1:8002/v1/completions -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer tenant-demo-key' \
  -d '{"prompt":"Explain retry budgets","demo":{"primary_scenario":"rate_limited","secondary_scenario":"server_error"}}'
```

```json
{"error":{"code":"UPSTREAM_UNAVAILABLE","message":"Unable to complete the request using the available model providers."}}
```

To watch quota rejection quickly, restart the router with a small budget
(`export QUILR_RATE_LIMIT_TOKENS=500`) and ask for more output than the whole window:

```bash
curl -sS http://127.0.0.1:8002/v1/completions -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer tenant-demo-key' \
  -d '{"prompt":"Explain retry budgets","max_output_tokens":600}'
```

```json
{"error":{"code":"RATE_LIMIT_EXCEEDED","message":"Token quota exceeded for this API key. Retry after the current window."}}
```

```bash
python -m pytest tests/task4_model_router -q
python -m pytest tests/task4_model_router -m integration -q
```

Request fields are `prompt` (nonblank, at most 8,000 characters) and optional
`max_output_tokens` (1–4,096, default 256). The optional `demo` block
(`primary_scenario`, `secondary_scenario`, `primary_delay_ms`, `secondary_delay_ms`)
only steers the local mock providers so failure paths are demonstrable without
external services; a real provider adapter would ignore it. Scenarios are `ok`,
`rate_limited`, `slow`, `server_error`, `bad_request`, `unauthorized` and `malformed`.

Rate limiting and token accounting:

- The quota is **50,000 tokens per 60 seconds per tenant API key**, both configurable.
  The window is half-open: events in `(now - 60s, now]` count, and an event exactly
  60 seconds old has left the window.
- Tokens are counted by an explicit local approximation — roughly four characters per
  alphanumeric run, one token per symbol — not a model tokenizer. Client-supplied
  token counts are never trusted. A real adapter needs the provider's own tokenizer.
- Admission **reserves** `prompt_tokens + max_output_tokens` before any provider call.
  After a successful completion the reservation is **reconciled down** to the usage the
  provider reported. It is never raised above the reserve, so a misreporting provider
  cannot inflate a tenant's recorded consumption.
- A request that fails or times out keeps its full reservation until it expires.
  Usage after a timeout is unknown, so the conservative reserve stands rather than a
  guess. One logical request is charged once, including when it falls back.
- A request whose reservation exceeds the entire window budget is rejected outright.
  Rejected requests never reach a provider.

SQLite and concurrency:

- The database is a real on-disk file at `QUILR_RATE_LIMIT_DB_PATH`, in WAL mode.
  Runtime database files are ignored by Git.
- Admission is one `BEGIN IMMEDIATE` transaction that evicts globally expired rows,
  sums the tenant's active tokens, and conditionally inserts the new reservation.
  Taking the write lock at transaction start gives each admission a single serialized
  attempt instead of a read snapshot that must later upgrade and can fail busy.
  Eviction is committed whether or not the request was admitted.
- Every call opens, uses and closes its own connection inside `asyncio.to_thread`, so
  a cancelled await never leaves a transaction open. **No transaction is ever held
  while a provider request is in flight.**
- Expired rows are evicted on every admission, across all tenants including inactive
  ones. An idle database keeps expired rows until the next admission.

Routing and fallback policy:

| Primary outcome | Router behavior |
| --- | --- |
| Success | Return it; the secondary is never contacted |
| HTTP 429 | One secondary attempt |
| Exceeds the 3000 ms deadline | One secondary attempt |
| HTTP 400/401/403/404/5xx | No fallback; standardized `UPSTREAM_UNAVAILABLE` |
| Unparsable or oversized payload | No fallback |
| Connection/transport failure | No fallback |
| Caller cancellation | Propagates; no fallback attempt |
| Quota rejection | No provider is contacted at all |

Only rate limiting and timeouts fall back, because those are the two signals the
assessment names and the two where a second provider plausibly succeeds. A 400 would
fail identically on the backup, and 401/403 indicates a configuration problem that
retrying elsewhere would only mask. The deadline covers connection, request and full
body read; the shared HTTP client timeout is set to the longer of the two provider
deadlines so it can never preempt them.

Error payloads use a stable `{"error": {"code", "message"}}` envelope with the codes
`RATE_LIMIT_EXCEEDED`, `UPSTREAM_UNAVAILABLE`, `QUOTA_STORAGE_UNAVAILABLE`,
`INVALID_REQUEST`, `UNAUTHORIZED` and `INTERNAL_ERROR`. Upstream status codes, bodies,
URLs, filesystem paths and exception text never reach the client. The mock providers
deliberately return realistic leaky error bodies so tests can prove none of it is
relayed. If quota storage is unavailable the router fails closed with 503 rather than
silently serving unmetered traffic.

Tenant identity and storage:

- The database stores a BLAKE2b fingerprint of the API key, never the raw key, and
  never prompts or completions. Setting `QUILR_TENANT_FINGERPRINT_KEY` makes the
  digest keyed, so disclosure of the database does not permit offline guessing of
  low-entropy keys. Leaving it blank uses an unkeyed digest, which is weaker.
- Requests limited to 32 KiB with a five-second body deadline; provider responses to
  256 KiB. Caller credentials, cookies and custom headers are not forwarded, redirects
  and environment proxies are disabled, and logs carry fixed diagnostics only.

## Configuration and security boundaries

Tasks 2–4 read process environment settings listed in `.env.example`; they do not
auto-load `.env`. Both Task 2 role tokens must be nonempty, distinct Bearer-compatible
ASCII values. Settings hide credentials in representations and errors, and invalid
configuration raises a fixed message rather than echoing the offending value. The
unmodified assessment PDF is private local reference material excluded from Git
discovery and package artifacts.

Task 1 reserves MCP stdout for protocol messages, logs fixed diagnostics to stderr,
and sanitizes tool/request failures. Task 2 separates demo authentication and tool
authorization, uses constant-time token comparisons, and never logs credentials
or complete payloads. Task 3 logs fixed diagnostics without prompts or raw deltas
and protects only the documented text patterns. Task 4 stores tenant fingerprints
rather than API keys, fails closed when quota storage is unavailable, and returns a
fixed error envelope for every failure path. No task logs a Python traceback: a fixed
diagnostic is logged instead, so no exception frame can carry request data into logs.
That trades some debuggability for a stronger guarantee about log contents.

## Assumptions

- `CUST-XXXXX` means exactly five ASCII digits; refunds are simulations with no ledger.
- Task 2 uses configured opaque demo tokens mapped to roles, one JSON-RPC message per
  request, and treats the downstream mock as trusted network.
- Task 3 supports OpenAI-style SSE with one choice at index 0 and assistant text.
  Provider metadata is trusted and not scanned for PII.
- Task 4 treats a well-formed bearer key as an opaque tenant identity. Token counts
  come from a documented local approximation, not a model tokenizer, and the sliding
  window relies on a reasonably synchronized host clock.
- The five-task overview and the four specified tasks are reconciled below.

## Known limitations

- **Task 4 tenant keys are not verified by default.** Any well-formed bearer key is
  accepted as a distinct tenant, so a caller could evade its quota by rotating keys.
  Setting `QUILR_TENANT_API_KEYS` restricts metering to a known set; a real deployment
  must verify the key against a credential store before metering.
- **Token accounting is approximate.** It is a logical request budget, not a provider
  billing ledger. A cancelled or timed-out upstream attempt may still have consumed
  provider tokens that this gateway cannot observe.
- **An abandoned provider request is not stopped remotely.** The router closes its own
  connection at the deadline and never uses a late response, but it cannot make a
  remote server stop working — which is also true of real providers.
- **Quota state is per-process storage.** SQLite on one disk suits this assessment;
  routing across hosts would need a shared store.
- **Task 3 redaction covers only the documented grammars** (ASCII emails, `123-45-6789`
  SSNs, 13–19 digit card-like runs). It is a demonstration guardrail, not a complete
  PII policy, and no Luhn check is applied.
- **Task 1 SDK behavior:** in the pinned `mcp==2.1.1` stdio transport, invalid raw JSON
  or invalid JSON-RPC envelopes are dropped without a `-32700`/`-32600` reply.
- Expired quota rows are evicted on admission, so an idle database retains expired
  rows until the next request or an explicit purge.

## Production improvements

- Verified identity (JWT/OIDC claims, a real tenant credential store) and managed
  secrets instead of environment-supplied demo tokens; TLS everywhere.
- Provider-specific tokenizers and reconciliation against real usage reports, with
  the quota store moved to a shared, replicated backend if routing spans hosts.
- Structured logging with request correlation IDs, metrics for admission rates,
  fallback rates and provider latency percentiles, plus alerting on quota-storage
  failures and sustained fallback.
- A broader PII policy for Task 3 with layered detection and a documented review
  process, and provider compatibility tests against real streaming APIs.
- Circuit breaking and adaptive routing rather than one static primary/secondary pair,
  and retry budgets so fallback cannot amplify load during an incident.

## The five-task/four-specification discrepancy

The assessment PDF's overview says it "consists of 5 practical technical tasks" and
mentions troubleshooting zero-trust network deployments among the focus areas, but the
document supplies detailed problem statements, requirements and evaluation criteria
for only four: the MCP server, the MCP security gateway, the streaming PII guardrail
and the model fallback router. No fifth specification appears anywhere in its three
pages. This repository implements the four specified tasks rather than inventing a
fifth from the overview sentence. If a fifth task exists, please share it and I will
complete it the same way.

See [requirements and assumptions](docs/requirements.md),
[architecture decisions](docs/architecture.md) and [test plan](tests/README.md).
