# Agent Structured Output

Native provider schema requests and local validation for generic LumiBot agents.

Last Updated: 2026-11-07
Status: Strict required-field adaptation implemented locally; real-provider rerun pending
Audience: Agent runtime maintainers and strategy authors

## Overview

`output_schema` on agent creation or a run accepts JSON Schema or a Pydantic
model. The manager keeps the normalized schema on `RuntimeRequest`, alongside
the existing prompt instruction. Final answers still pass local parsing and
validation before `result.parsed` becomes available.

## Generic Contract

Direct `deepseek/deepseek-flash` and `deepseek/deepseek-v4-pro` calls with an
output schema use the provider's native `/responses` endpoint and `text.format`
JSON Schema. They keep their selected model IDs. Provider protocol conversion belongs to
`deepseek_llm.py`; no application, account or strategy identity selects behavior.
Calls without a schema, other DeepSeek model IDs and other providers retain
their existing routes.

Before sending, the adapter creates a separate strict wire schema. Every
declared object property is required, including Pydantic fields with defaults
and fields inside referenced definitions, arrays and unions. Objects with
declared properties receive `additionalProperties: false` when unspecified;
explicit map settings are preserved and may still be rejected by the provider.
Types, constraints, default annotations and literal values are retained. The
caller schema and local validator stay unchanged. Defaulted fields must be
emitted explicitly with valid values; Python does not fill in missing model
answers or introduce nullable types to make a request pass.

The adapter uses the selected provider's explicit `DEEPSEEK_API_KEY` and honors
the SDK's existing optional `DEEPSEEK_API_BASE`. It never borrows OpenAI
credentials or endpoints. An endpoint or schema rejection is a provider error;
it does not activate a silent prompt-only fallback. The caller's tools and
permissions, reasoning effort, output limit and request timeout stay in force.
The existing outer runtime owns retries and whole-run timeouts.

DeepSeek's stateless conversation includes original plain reasoning text with
assistant function calls and paired tool results. Each response is buffered
until completion; incomplete or failed responses cannot expose partial JSON as
a completed answer. Reported input, output, cached-input and reasoning tokens
are retained, with reasoning counted inside output tokens only once. Local
schema validation remains required; a schema cannot prove evidence, dates,
citations or an investment claim correct.

Native DeepSeek schema runs add a protocol revision to their replay-cache key.
Revision `deepseek_responses_json_schema_v2` separates this strict wire contract
from both prompt-only and the earlier unadapted native-schema entries. No cache
is deleted.

## Configuration Classes And Negative Controls

The contract applies to research and trading agents, optional application roles,
live and historical execution, and both creation-time and per-run schemas. It
does not narrow ordinary research tools or alter broker permissions.

Deterministic tests cover both Flash and Pro HTTP payloads, real ADK tool
continuations, original reasoning and call-ID replay, multimodal input
conversion, token/cache accounting, schema failures, incomplete responses,
provider endpoint selection, missing credentials and replay-key separation.
An independent mock-provider check first rejects schemas with optional
properties, then accepts the adapted request through the ADK tool loop. All
four optional application role schemas are checked for both model IDs, with
the original caller schemas and local validation preserved.
Existing provider, permission, historical-evidence and application regressions
remain separate controls.

## Standalone Provider Diagnostic

Run the standalone script from the deployed checkout with the same Python
environment and provider settings as the service. It tests both Flash and Pro
against the application's actual `Research` schema, including `summary` and
`evidence`. Each model must first request a synthetic read-only evidence tool,
then return plain JSON that passes both original and wire JSON Schema checks
and Pydantic validation. It must copy the tool's actual evidence. It does not connect to a broker, read a
customer account or place orders.

A real-provider run returned HTTP 400 for both models on their first request.
A subsequent Flash diagnostic identified a strict object rule: `required`
must contain every property. The original Pydantic `Research` schema omitted
defaulted fields such as `ideas`, including defaults in nested definitions.
The sender now adapts this rule generically for both models. Successful real
provider qualification is still pending; local mock passes do not establish
that the remote service accepts every part of the corrected request.

The default is offline HTTP simulation, with no provider credentials needed:

```bash
.venv/bin/python scripts/check_deepseek_schema.py --offline
```

For an explicit real-provider integration check, supply `DEEPSEEK_API_KEY` in
the process environment, or select an untracked secret file explicitly:

```bash
.venv/bin/python scripts/check_deepseek_schema.py --real-models
.venv/bin/python scripts/check_deepseek_schema.py --real-models --env-file "<your-secret-file>"
```

Real mode creates DeepSeek usage: at most four provider requests per selected
model, eight for the default `--model both`, without automatic retries. A
model may read the tool again before answering, as production agents may: up
to three tool rounds pass and are recorded as `tool_rounds`; a fourth request
for the tool fails as `too_many_tool_rounds` without another provider request. Use
`--model flash` or `--model pro` to select one model. Each request defaults to
4,096 maximum output tokens and a 120-second wall/request timeout, adjustable
with `--max-output-tokens` and `--request-timeout-seconds` (at most 300 seconds).
`--reasoning-effort` defaults to `high` and accepts `none`, `low`, `high`, `max`.
No `.env` is discovered automatically. Process values take precedence over the
explicit secret file, and other providers' keys are not loaded from it.

`--schemas` selects the cases (comma-separated, or `all`); the default
`research` keeps the original tool round-trip check above:

| Case | Requests per model | What it checks |
|---|---|---|
| `research` | 2-4 | `Research` with a synthetic read-only tool round trip (one to three tool rounds) |
| `decision` | 1 | `Decision`, whose price levels add `exclusiveMinimum` (absent from `Research`); the times must be copied |
| `critique` | 1 | `Critique` |
| `review` | 1 | `PostmarketReview`; `evidence_refs` must come from the supplied list, as in production |
| `constraints` | 2 | Asks on purpose for an empty `summary`, an empty `evidence`, a weight of 1.5 and an `entry_limit` of 0 |

`all` makes at most 9 requests per model, 18 for both. The constraint probe
reports `held` or `not_enforced` per constraint and fails the case on any
`not_enforced`: that value reached us although the schema forbids it, so the
provider does not enforce it and local validation can still reject a
production answer of that kind. `held` cannot tell provider enforcement from a
model that ignored the instruction; it is evidence, not proof. `entry_limit`
is nullable, so a `null` answer in place of the forbidden 0 is `held_null`;
`untested` means the field was missing or not a number. The values the model
returned are recorded under `constraint_observed` (a non-numeric value only by
its type) and printed with the findings.

```bash
.venv/bin/python scripts/check_deepseek_schema.py --real-models --schemas all
```

The console shows each model's schema/tool outcome and reported token/cache
usage. A unique report under `artifacts/verification/deepseek-schema-smoke/`
records mode, source fingerprints, original and wire schema fingerprints, SDK
versions, per-request status,
available usage and the synthetic final answer. `--output` selects a new file;
existing files are never overwritten. A started request is checkpointed before
inference; interruption and timeout leave unreported usage unknown. Provider
exception messages, keys and private endpoint values are not copied into the
report. Exit codes are 0 for success, 1 for a failed model check, 2 for setup
failure and 130 for interruption.

An offline pass does not establish provider support. A real pass checks this
small request/tool path in the current Python process, not a complete strategy,
every role schema, repeated-run reliability or whether a running service has
reloaded its code. This diagnostic is independent of semantic/release evals.

### Diagnosing A First-Request HTTP 400

The report and console now retain safe `provider_problem` categories rather
than only `http_status: 400`. They can distinguish a reported unavailable schema
format, rejected schema keywords or required-field rules, an invalid schema
definition, and a schema/tool or schema/reasoning combination rejection. The
all-properties-required error is recognized even without the word `schema`.
`provider_request_rejected` means the available message does not match a known
category; it must not be relabeled as proven schema support or non-support.

Known public provider error codes and API parameter names are retained.
`diagnostic_terms` contains only allowlisted public API/schema vocabulary and
`message_sha256` identifies the message without storing it. Provider messages,
input values, secrets, private endpoints and request IDs are not copied. These
categories explain evidence; they do not select a provider, retry a request,
change a schema or silently downgrade the requested output mode.

Connection errors separately identify DNS or certificate verification failures
from exception types, without copying hostnames or exception messages. After
updating the adapter and diagnostic together in the selected environment,
repeat the explicit real-provider command. A new rejection still needs its
specific evidence; no fallback or blanket support claim follows from a 400.

## Source To Served Evidence

Tests use synthetic credentials, injected market/tool data and HTTP mock
transports; no paid provider inference or customer workload is used. Local
protocol tests establish what the candidate sends and accepts, not that a
remote provider will always comply. A provider may still reject a schema, fail,
return an empty answer or reach its output limit. This candidate does not
publish a package, update a running service or qualify a downstream deployment.

The maintained API references are DeepSeek's
[Responses API](https://api-docs.deepseek.com/api/create-response/) and
[compatibility guide](https://api-docs.deepseek.com/guides/responses_api/).
