# Broker Market Clock Timezones

Correct the live broker's selection of today's opening session on non-UTC hosts.

**Last Updated:** 2026-11-02

**Status:** Fixed; 221 deterministic regression cases pass locally

**Audience:** Broker maintainers and operators of continuous local, paper and live strategies

## Overview

The shared `Broker.get_time_to_open()` passed the naive result of `datetime.now()`
to `utc_to_local()`, whose documented-by-implementation convention treats naive
input as UTC. On an Asia/Shanghai host this moved the comparison clock eight hours
forward. At 08:55 UTC on 2026-11-02 the application compared today's 14:30 UTC
opening with an incorrect 16:55 UTC clock, selected the next session, and slept
past both today's pre-open hook and opening bell. Background broker polling and
telemetry can continue throughout that wait.

## Generic contract and fix

The next opening and its pre-open offset must be the same absolute instants for
every server timezone. Pass an explicitly aware UTC wall clock into the existing
conversion. Keep calendar selection, the pre-open buffer, closed-market order
checks and backtesting clocks intact.

This fixes the common implementation used by the local simulator and live
adapters that inherit it, including Schwab, Tradier and Interactive Brokers.
Alpaca's provider-clock implementation and the backtesting broker's simulated
clock have their own overrides; retain those contracts.

## Validation and operational limits

`tests/test_broker_market_clock_timezone.py` exercises the real NYSE calendar and
actual local, Schwab, Tradier, Interactive Brokers TWS and REST adapter wait methods
with constructors bypassed to avoid authentication, network activity and orders.
Simulated host zones include UTC, Asia/Shanghai, New York and Los Angeles. Cases
cover ordinary pre-open startup, startup within the pre-open window, both sides
of the US daylight-saving transition, weekends, after-close startup, open-session
behavior and the Christmas Eve early close. UTC serves as an unaffected control.

Run the native lifecycle and autonomous-fund premarket regressions alongside the
clock matrix. Local validation passed 164 clock-matrix cases, 11 autonomous-fund
premarket cases (including native startup/restart/pre-open/open execution), three
closed-market lifecycle cases, and 43 adjacent calendar, backtesting-wait and
Alpaca cases. The modified public documentation page also built with Sphinx.
These are deterministic scheduling checks, not proof of a deployed live account
or a real-money order lifecycle.

An existing long sleep does not recalculate itself when source files change or
the independent monitor clears STOP. Restart the affected runner after deploying
the corrected source, retain the existing account directory and inspect its STOP
and outstanding-order state through the normal operational workflow. Check that
the imported `lumibot.brokers.broker` file belongs to the updated checkout.

The autonomous paper-fund application's supported modes remain local and paper;
correcting the framework's live broker clock does not add a live execution mode.
