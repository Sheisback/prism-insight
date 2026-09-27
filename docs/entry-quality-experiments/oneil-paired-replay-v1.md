# O'Neil paired replay v1 preregistration

Registered 2026-09-27 before evaluating campaign outcomes. Research only.

Compare equal, full campaign budgets: original entry at 100% versus
`COMPATIBILITY_SCOUT_10` (10% initial entry, then actual `evaluate_target`).
This does not validate the separate 50% initial-entry hypothesis. Both arms use
the same original entry, observed quote tape, monotonic original stop path and
terminal exit. Protective exit takes priority; no new trailing stop or inferred
intrabar fill. Execution is simulated at observed quotes, never at an ideal stop.
Use existing StrategyLedger accounting, fees of 10 bps per side primarily and
25 bps per side as a fixed stress case. Slippage is not separately modeled.

Input contract `oneil-paired-replay-input-v1`: kind SYNTHETIC or HISTORICAL,
campaigns with unique campaign_id, canonical plan, entry, nonempty ticks, exit.
Every entry/tick/exit has source_ref, source_decision_ref, symbol,
price_basis_ref, occurred_at and available_at. Entry/exit have price; each tick
has original evidence plus current_stop, stop_source_ref and stop_available_at.
Plan must already exist at entry; its entry price must match. Source identities
and availability at each decision are required. Tick quotes, gate snapshots and
all bars must be contemporaneous, complete and accepted by the actual policy.
Stops cannot fall. Incomplete/invalid inputs make the entire campaign
INPUT_UNAVAILABLE, not a zero return or a confirmed non-add decision.

These caller-supplied references are NOT authenticated captures. SYNTHETIC is
FUNCTIONAL_ONLY and HISTORICAL is EXPLORATORY_ONLY. PROSPECTIVE and claimed proof
are not accepted. A future authenticated original-capture adapter is prerequisite
for forward evidence; never promote from this tool.

Report paired net returns against full campaign budget, mean/median deltas,
profit factors (undefined when no losses), win rates, and removal of the best
baseline winner using the same campaign IDs in both arms. Preserve missing
coverage against all supplied campaigns. At least 30 closed pairs on 20 distinct
entry dates, including at least 10 profitable baseline campaigns, is a minimum
descriptive sample gate, not profitability proof. Report adaptive profit retained
on those same baseline winners (sum adaptive / sum baseline), with 90% retention
as the fixed guard, and worst individual campaign return, not portfolio MDD.
No portfolio drawdown/CAGR/Sharpe claim without a synchronized portfolio tape.
Selection, original screening misses, overnight gaps, liquidity, partial fills,
opportunity cost, tax and omitted ticks remain unmodeled. No threshold tuning.
Quotes are simultaneous ideal execution references: spread, latency, market
impact and gaps between observations are not simulated. The 25bps cost case is
not full realistic execution validation.

`--entry-quality-packet` accepts canonical schema3/harnessv2 sanitized US Packets
for coverage only. It preserves valid strategy outcomes independent of fills and
all canonical insufficiency reasons, but returns INPUT_UNAVAILABLE for paired
replay. Summary returns cannot reconstruct original adaptive plans, quote/volume
and gate tapes, stop paths or exit evidence. Its verdict is CONTINUE_CAPTURE.
