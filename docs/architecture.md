# Architecture

Design decisions and component boundaries for the four specified tasks. The
requirement-to-test map is in [requirements.md](requirements.md).

## Package and process boundaries

One `src/quilr_assessment` distribution keeps installation simple. Each task owns
its schemas, service logic and tests. Task 1 is an independent stdio process;
Tasks 2–4 expose small FastAPI applications served by Uvicorn.
`mocks/` provides deterministic loopback HTTP upstreams. Task 2 uses its own
HTTP mock rather than making Task 1's stdio server depend on an HTTP bridge.

Provider adapters accept an injected `httpx.AsyncClient` and configured URL.
Application lifespan owns clients and closes them. Tests can replace upstream
transport without replacing the authorization, redaction or routing logic.
No shared gateway framework, generic middleware registry, ORM, message queue or
container stack is introduced: the tasks are independent and small enough that a
shared abstraction would cost more clarity than it saves. Code is shared only where
a concrete need appeared, such as the tokenizer used by both the Task 4 router and
its mock providers.

## Task 1: SDK transport and explicit validation

Use the official SDK's low-level `Server`, `stdio_server()` and Pydantic models.
Advertise schemas from the same models that validate tool arguments. Validate
before any tool action, map invalid arguments to `INVALID_PARAMS` (`-32602`),
and return only sanitized messages. Keep genuine tool execution failures separate
from malformed requests. Let the SDK own protocol framing, initialization,
capabilities and dispatch; do not handwrite an MCP server.

Configure standard logging to stderr at process startup, including the period
before stdio transport is entered. Keep package imports silent and do not log tool
arguments. Actual subprocess tests must verify wire errors, IDs and stdout isolation;
an in-process tool test cannot prove these properties.

**SDK version pin.** `mcp==2.1.1` is pinned because its handler API differs from v1
examples: `mcp.server.lowlevel.Server` takes `on_list_tools`/`on_call_tool`,
`mcp.server.stdio.stdio_server` provides the transport, and `mcp.MCPError` carries
`code`, `message` and `data`. The low-level API performs no argument validation of
its own, so the schemas are enforced explicitly. See the
[low-level server documentation](https://py.sdk.modelcontextprotocol.io/advanced/low-level-server/)
and the [versioned SDK source](https://github.com/modelcontextprotocol/python-sdk/tree/v2.1.1).
Tests cover initialization, discovery, both tools, protocol errors, request IDs,
recovery and stderr isolation through both the official client and raw stdio pipes.

The implementation uses public SDK middleware to sanitize unexpected failures
across request validation, dispatch and result serialization. This is necessary
because the pinned legacy dispatcher otherwise falls back to code `0` with raw
exception text. Application argument errors use `-32602` and expose only known
field names; unexpected failures use fixed `-32603` messages. Middleware leaves
cancellation uncaught. Tool execution remains in small deterministic functions.

The pinned SDK's stdio parser drops malformed raw JSON/envelopes without replying;
`Server.run` exposes no parse-error hook. A regression test verifies recovery and
absence of payload leaks. This known upstream limit does not affect the required
`-32602` errors for invalid tool arguments in valid requests. Keep the official
transport and dispatcher instead of introducing a custom protocol layer.

Refund reason normalization trims and collapses whitespace runs before the
10-character check, so `"a        b"` is rejected. The only customer is synthetic
`CUST-12345`; unknown IDs return a normal `not_found` result for either tool.
Refund IDs are input-derived mock identifiers, not a durable idempotency mechanism.

**Installation note.** The package is installed non-editable
(`uv sync --locked --extra dev --no-editable`), because an editable install's `.pth`
file can be skipped on systems that hide it. After source changes the package is
rebuilt explicitly with `--reinstall-package quilr-assessment` rather than adding
`sys.path` patches or pytest-only import paths to work around installation. Project
metadata supports Python 3.11+.

## Task 2: authenticate, authorize, then forward

Request flow: validate HTTP credentials -> parse JSON-RPC -> authorize tool name ->
forward allowed request -> return downstream response. Authentication resolves a
trusted role from configured opaque tokens. Authorization tests that role against
the parsed `admin_` prefix. No downstream request may be created before that decision.

Forward allowed `tools/list` requests and responses transparently, including the
unfiltered tool list. Preserve the body/ID for allowed requests, while only forwarding
appropriate HTTP headers: client authorization and hop-by-hop headers are not
downstream credentials. Apply bounded request sizes and upstream deadlines. Return
the required `-32001` / `Unauthorized Tool Call` for denied tool requests, and use
standard parse/envelope/parameter codes for malformed JSON-RPC. Valid notifications
receive no JSON-RPC reply, per the [JSON-RPC specification](https://www.jsonrpc.org/specification).
The single-message/stateless scope and HTTP authentication choices are in A3–A4.

The flow lives in `app.py`, with separate `auth.py`, `policy.py`, `rpc.py`,
`proxy.py` and `config.py`. Configuration uses Pydantic only for startup
settings; `SecretStr` values are excluded from serialization. Both role tokens are
required, distinct and compared with `hmac.compare_digest` on every lookup. No JWT
parsing or identity provider is needed for this demo. The factory reads process
environment settings; there is no automatic `.env` loader.

The parser rejects duplicate members recursively, invalid IDs, nonfinite numbers
and unpaired Unicode surrogates, avoiding ambiguous authorization decisions and
error-serialization failures. It retains the original bytes for forwarding, accepts
only one JSON-RPC message and validates the `tools/call` name before policy checks.
Other authenticated methods pass through. Explicit null IDs remain requests.

The client pool is owned/closed by FastAPI lifespan. It uses a configured URL,
disables redirects and environment proxies, and sends only controlled JSON headers.
It rejects compressed downstream responses and never propagates caller credentials
or arbitrary headers. A rejecting cookie policy prevents a shared HTTP pool from
becoming a session shared between different callers. Successful response bodies
are returned unchanged after JSON-RPC envelope/ID checks; downstream RPC error
messages/data and tool-error content are replaced with fixed messages. This is not
content inspection of successful tool results.

Reads are bounded: 64 KiB/five seconds for a request body, 256 KiB and a configurable
total deadline (default five seconds) for a downstream response. Infrastructure
failures use HTTP 502/504 and distinct JSON-RPC codes `-32002`/`-32003`. Unexpected
gateway failures use `-32603`. Notifications receive no JSON-RPC body, including
when denied or when forwarding fails. These policies are summarized in the README.

Tests prove denied calls make zero HTTP transport invocations; separate real
loopback tests launch the gateway and mock with Uvicorn. The mock has a per-process
request counter and deterministic normal/protected/error tools. It has no auth of
its own and is intended for the trusted local side of this demonstration.

## Task 3: bounded text redaction and SSE forwarding

The pure `StreamingRedactor.feed/finish` engine was built and tested before being
connected to `stream.py` framing and `provider.py` forwarding, which is what makes
exhaustive chunk-invariance testing possible. The request path is `/generate` -> configured provider `/stream` -> SSE parser -> text redactor
-> client SSE. `mocks/llm.py` independently emits deterministic synthetic events.
Network chunks, SSE events and text candidates have separate boundaries.

The redactor scans characters and retains one unresolved lexical candidate, capped
at 512 characters. A single space after a digit may continue a card candidate.
At a boundary it classifies the candidate using bounded email, SSN and number
patterns, then unions overlapping match intervals before replacement. On character
513 it emits one marker and discards the remainder of that candidate until a
boundary. This bounds retained state without releasing an unresolved prefix.
Long harmless tokens and ambiguous numeric aggregates can be over-redacted.
The ASCII grammar and unsupported formats are recorded in A5 and the README.

`SSEParser` incrementally consumes bytes, decodes completed UTF-8 lines and yields
events lazily. Its 64 KiB event budget includes ignored fields and normalized line
endings (CRLF counts as one); the caller exhausts each feed iterator before feeding
again. Malformed UTF-8/JSON, duplicate JSON members, oversized events and unsupported
event types fail with fixed errors. No response history is accumulated.

The adapter accepts one assistant text choice at index 0, preserves known bounded
metadata/numeric usage, and drops unknown metadata. Preserved metadata is trusted
provider data, outside PII inspection. Tool calls, refusals, non-null log probabilities and
multiple choices are unsupported. Safe content replaces only `delta.content`;
the adapter does not require one logical token per event. A finish reason finalizes
text, usage may follow, and `[DONE]` closes successful generation. Premature EOF
without `[DONE]` fails without flushing unresolved text. After headers, failures
emit a sanitized SSE error and close without `[DONE]`; opening failures use 502/504.

FastAPI validates strict request models after a 32 KiB/five-second bounded body
read. An async client pool belongs to application lifespan. Provider URLs are
configuration, caller headers/cookies are not passed through, and redirects,
environment proxies and cookie persistence are disabled. The default ten-second
timeout bounds opening response headers and each idle read; healthy generation has
no total duration limit. Awaited output provides backpressure. The response owns
and closes both its generator and upstream connection even if sending to the client
fails. Closing the provider has a separate five-second deadline; ordinary cleanup
failures produce only a fixed diagnostic. Cancellation propagates rather than
becoming a synthetic provider error.

Tests cover every split position of supported PII, punctuation, overlapping and
adjacent values, byte-split UTF-8/SSE, overflow, premature termination and cleanup.
Real HTTP tests hold provider completion behind an async gate and observe safe
output first, then separately disconnect during a blocked provider read. These
checks avoid relying on an in-process client that buffers the entire stream.

## Task 4: atomic quota admission, then provider I/O

Standard-library `sqlite3` on disk. One short `BEGIN IMMEDIATE` transaction deletes
globally expired events, totals the tenant's active token reservations, and
conditionally inserts the new reservation; the eviction is committed whether or not
the request is admitted. Tenant/time and expiry-time indexes support both statements.

`BEGIN IMMEDIATE` takes the write lock at transaction start. A deferred transaction
would begin as a reader and have to upgrade at the insert, which SQLite refuses once
another writer has advanced the database — the observed failure mode is spurious
`SQLITE_BUSY` errors and lost throughput, not oversubscription, since the write lock
still prevents lost updates either way. Taking the lock up front gives each admission
a single serialized attempt with no retry loop. This is verified: reordering the
statements into a read-then-write deferred transaction makes the sixteen-writer
concurrency test fail with storage errors. WAL and a finite busy timeout are enabled.
No transaction is ever held while calling a provider.

Blocking database work runs in `asyncio.to_thread`, with each connection created,
used and closed inside that worker. Cancelling an await does not stop a worker thread,
so transactions stay short and completion is accounted for explicitly. Stale rows are
evicted across all tenants on admission, including inactive ones; an idle database
retains expired rows until the next admission or an explicit purge. UTC timestamps are
persisted for restarts, tests inject a clock, and the window relies on a reasonably
synchronized host clock. Only tenant fingerprints and quota events are stored — never
raw keys, prompts or completions.

Accounting follows A6–A7: reserve prompt tokens plus the requested output allowance,
reconcile down to reported usage on success, and retain the full reservation on
failure or timeout. Reconciliation never raises a charge above the authorized reserve,
so a misreporting provider cannot inflate a tenant's consumption. The reservation is a
logical request budget, conservative relative to mock usage and not a billing total.
The prompt tokenizer is shared with the mock providers so reported usage is verifiable.

After successful admission the primary runs under `asyncio.timeout(3.0)` covering
connection, request and full non-streaming completion. On primary HTTP 429 or deadline
expiry the response is closed and the secondary is tried once under its own finite
deadline. The shared `httpx.AsyncClient` timeout is the longer of the two provider
deadlines so it cannot preempt them — an earlier version scoped it to the primary
deadline alone, which failed a slow secondary. Caller cancellation is never swallowed.
Other provider errors and local quota rejection follow A8 and do not fall back. Error
payloads use a small stable `error.code` / `error.message` envelope, with no upstream
exception strings, bodies, statuses, URLs or filesystem paths; an application-level
`Exception` handler ensures even a handler defect returns that envelope rather than a
framework default.

## Dependencies and testing approach

Runtime dependencies: `mcp`, Pydantic, FastAPI, Uvicorn and httpx. SQLite, asyncio,
logging and hashing use the standard library. Development extras: pytest,
pytest-asyncio and Ruff. PDF extraction/rendering tools live in a temporary
environment, not project dependencies. `uv.lock` records exact resolutions; the
wheel includes only the package and the sdist uses an explicit path allowlist.

Strict pytest markers/configuration and per-test asyncio loops are configured. All
four tasks have unit, handler/route and integration tests mapped to requirement IDs.
Real local streaming, subprocess and socket checks are used where an in-process client
would conceal buffering, transport or timeout behavior — Task 4's client-timeout
defect, for example, is only observable over real sockets because `ASGITransport`
ignores httpx timeouts.

Production changes would include verified identity and managed secrets, deployment
TLS, provider-specific tokenization and usage reconciliation, broader PII detection,
circuit breaking with retry budgets, and a distributed quota store if routing across
hosts. These are documented tradeoffs, not work included in this assessment's scope;
see the README's limitations section.
