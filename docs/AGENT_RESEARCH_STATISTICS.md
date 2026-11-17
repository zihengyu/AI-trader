# Agent Research Statistics

Optional, parameterized calculations and schema feedback for loaded research tables.

**Last Updated:** 2026-11-17
**Status:** Implemented; deterministic/native checks passed; real-model qualification pending an authorized ChatGPT connection
**Audience:** LumiBot contributors and strategy authors

## Overview

The shared owner is `lumibot/components/agents/duckdb_tools.py`, with fixed
statistical templates in `research_statistics.py` and public bindings in
`builtins.py`. The tools serve any strategy/account using loaded research
tables. No strategy, broker, asset, ticker, network route or customer receives
special treatment. Explicit trading authority and source permissions are
unchanged.

The engineering measures are successful research-query fraction, failed-query
count, correctness against independent calculations, and time/tokens spent
correcting queries. No investment return or model-fee improvement is claimed
without observation. The tools add no provider calls or model inference, but
metadata and tool schemas do consume input tokens.
Query counters include fixed helpers' internal SQL; tool traces and model usage,
rather than a raw engine-query ratio alone, measure agent correction overhead.

## Contract

Table loads add actual DuckDB types and nullable schema metadata. The describe
tool lists registered public tables without exposing native backing frames or
their future source values. SQL errors preserve the original failure, query
hash and bounded current schema feedback. Direct layer queries still raise
native exceptions; the bound agent tool returns the normal `tool_error` shape.
Failure counters remain distinct from successful query counters.

The optional analysis tool calculates simple backward/forward returns, rolling
sample volatility, Pearson correlation and beta. Window calculations are staged
and arithmetic operands are approximate doubles. Exact ledger values are not
cast, rounded or overwritten. Positive price/level series are supported; other
research can use free SQL. Invalid values remain gaps, not zero observations.

Grouping and ordering use explicit columns and unique `(group,time)` keys.
Horizon steps use the union of recorded timestamps, not a claimed exchange
calendar. A missing group endpoint on that grid is not skipped. Entirely
missing timestamps remain a limitation. Rolling statistics require full valid
windows; a constant benchmark does not establish zero correlation/beta.
Annualization is opt-in with a supplied frequency.

Backtest-only clock clipping is applied to the loaded data. Aware timestamps
compare as instants; naive timestamps need explicit or recorded timezone
metadata. Date-only comparisons disclose their strategy-date basis and cannot
prove intraday availability. Existing source visibility and document-publication
contracts continue to apply. Live data has no backtest-only date restrictions.

## Independent Research Fallback

Both tools are additional capabilities. `duckdb_query` remains available with
its original research permissions, including arbitrary complete queries and
custom CTEs. Agents can directly select it when a question exceeds helper
coverage; an unsupported helper response also identifies that route.

No rule silently guesses a column, changes a formula, rewrites SQL, substitutes
a mean for a median, fills unknowns or grants trading authority. A failure of
the optional helper does not disable SQL or require an operator mode switch.
If an owner explicitly excludes SQL, the fallback does not re-enable it; native
runtime inventory supports an accurate availability hint. Programmatic calls
without that inventory report unknown availability.
This contract is tested through actual bound tool calls: unsupported helper,
visible failed outcome, then a successful independently submitted query.

## Verification And Limits

Deterministic tests cover exact schema types, high-precision source preservation,
independent known return/volatility/beta calculations, missing endpoints, invalid
values, duplicate keys, zero benchmark variance, multiple groups/accounts,
timezones, historical cutoffs, live dates, truncation, and the fallback route.
Native market/document loading and runtime permission tests must also pass.
The existing eval fixture's separate FRED cache is assigned to its artifact
directory, preserving credential stripping and discovery without a host home.

The two new eval cases cover schema/statistic interpretation and free-SQL
fallback. A deterministic pre-change API failure is recorded separately from
real-model evidence. It must never be relabeled as an actor/judge run. Until the
authorized ChatGPT subscription is available, model behavior, required consecutive
semantic passes and release qualification remain unverified. API-funded
fallback is prohibited. No package release or server deployment is authorized
by this implementation task.
