"""Source-bound US SHADOW runner. Production databases are read-only inputs."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from prism_core.oneil_adaptive_policy import _hash, _time
from prism_core.oneil_current_capture import capture_current_record
from prism_core.oneil_live_boundary import readiness
from prism_core.oneil_runtime import OWNER, OWNERS, OneilRuntime
from prism_core.oneil_runtime_inputs import (
    IntradayProvider, current_gates, fetch_market_snapshot, fetch_quote, quote_input,
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def readonly(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("existing nonsymlink input database required")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)
    connection.execute("PRAGMA query_only=ON")
    return connection


def read_captures(path, since):
    from observability.scenario_shadow import _validate_db
    boundary = _time(since)
    with closing(readonly(path)) as connection:
        _validate_db(connection)
        # The input registry is bounded before decoding its payloads.
        rows = connection.execute("SELECT payload FROM captures LIMIT 1001").fetchall()
    if len(rows) > 1000:
        raise ValueError("capture registry requires explicit archival before scanning")
    captures = [json.loads(row[0]) for row in rows]
    return sorted((c for c in captures if _time(c["event_time"]) >= boundary),
                  key=lambda c: (c["event_time"], c["event_id"]))


def read_portfolio(path, capture, max_slots, clock=utc_now):
    """Read a current strategy holding, not broker fills or account cash."""
    match = re.fullmatch(r"legacy:US:([1-9][0-9]*)", capture["position_id"])
    if not match or type(max_slots) is not int or not 1 <= max_slots <= 10:
        raise ValueError("exact legacy US position and bounded slot cap required")
    with closing(readonly(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT id,ticker,account_key,scenario,stop_loss FROM us_stock_holdings WHERE id=?",
            (int(match[1]),)).fetchone()
        if row is None or row["ticker"] != capture["ticker"] or not row["account_key"]:
            raise ValueError("current holding unavailable")
        scenario = json.loads(row["scenario"])
        if scenario.get("_decision_id") != capture["decision_id"]:
            raise ValueError("holding decision mismatch")
        rows = connection.execute(
            "SELECT id,ticker,account_key,scenario FROM us_stock_holdings WHERE account_key=? LIMIT 101",
            (row["account_key"],)).fetchall()
        if len(rows) > 100:
            raise ValueError("portfolio snapshot too large")
        positions = [dict(position_id=f"legacy:US:{r['id']}", symbol=r["ticker"],
                          account_key=r["account_key"],
                          source_decision_ref=json.loads(r["scenario"]).get("_decision_id")) for r in rows]
    at = clock()
    portfolio = dict(observed_at=at, account_key=row["account_key"], positions=positions,
                     slots_used=len(rows), max_slots=max_slots)
    portfolio["source_ref"] = _hash(portfolio)
    # Preserve original underwriting arithmetic; the adaptive policy separately
    # uses the current authoritative stop column, exactly as hardstop does.
    return scenario, portfolio, row["stop_loss"]


def read_terminal(path, capture):
    if path is None:
        return None
    from prism_core.oneil_capture_tape import VERSION, _check
    with closing(readonly(path)) as connection:
        _check(connection)
        row = connection.execute(
            "SELECT payload FROM records WHERE campaign_id=? AND kind='EXIT'",
            (_hash([VERSION, capture["position_id"]]),)).fetchone()
    if row is None:
        return None
    event = json.loads(row[0])["event"]
    # Runtime validates symbol/decision/basis again; position was the exact join.
    return dict(event, position_id=capture["position_id"])


class ShadowRunner:
    def __init__(self, *, runtime_db, capture_db, holdings_db, since, max_slots,
                 tape_db=None, quote_provider=fetch_quote, market_provider=fetch_market_snapshot,
                 intraday_factory=IntradayProvider, clock=utc_now):
        _time(since)
        sources = [Path(p).resolve() for p in (capture_db, holdings_db, tape_db) if p is not None]
        target = Path(runtime_db)
        if (target.is_symlink() or target.resolve() in sources
                or (target.exists() and any(p.exists() and target.samefile(p) for p in sources))):
            raise ValueError("runtime must be separate from all input databases")
        if type(max_slots) is not int or not 1 <= max_slots <= 10:
            raise ValueError("explicit existing portfolio slot cap required")
        self.runtime = OneilRuntime(target)
        self.capture_db, self.holdings_db, self.tape_db = capture_db, holdings_db, tape_db
        self.since, self.max_slots, self.clock = since, max_slots, clock
        self.quote_provider, self.market_provider = quote_provider, market_provider
        self.intraday_factory, self.intraday = intraday_factory, {}
        self.market_cache = None
        with self.runtime.ledger._transaction() as db:
            self.runtime.ledger._event(db, "oneil:runner:config", dict(
                mode="SHADOW", initial_arm=self.runtime.initial_arm, since=since, max_slots=max_slots,
                sources_hash=_hash([str(p) for p in sources])))

    def once(self, *, source="mechanical", calendar="AUTO"):
        if source not in {"regular", "mechanical"} or calendar not in {"AUTO", "NYSE", "NASDAQ"}:
            raise ValueError("explicit supported source/calendar required")
        started = self.clock()
        cycle_id = "oneil:cycle:" + _hash([started, source, calendar])
        with self.runtime.ledger._transaction() as db:
            completed = db.execute("SELECT payload FROM events WHERE id=?", (cycle_id + ":done",)).fetchone()
            if completed:
                return json.loads(completed[0])
            pending = db.execute(
                "SELECT a.id FROM events a LEFT JOIN events b ON b.id=a.id || ':done' "
                "WHERE a.id LIKE 'oneil:cycle:%' AND a.id NOT LIKE '%:done' AND b.id IS NULL LIMIT 1"
            ).fetchone()
            self.runtime.ledger._event(db, cycle_id, dict(kind="runner_cycle", started_at=started,
                                                        source=source, calendar=calendar))
        rows = []
        try:
            captures = read_captures(self.capture_db, self.since)
            for capture in captures:
                # Each campaign keeps the owner of its frozen plan version.
                frozen = (capture.get("attributes", {}).get("adaptive_setup") or {}).get("plan") or {}
                version = frozen.get("policy_version") if frozen.get("policy_version") in OWNERS else OWNER
                cid = self.runtime.campaign_id_for_position(capture["position_id"], version)
                if _time(capture["event_time"]) > _time(self.clock()):
                    rows.append(dict(campaign_id=cid, status="ERROR", error_type="FUTURE_CAPTURE"))
                    continue
                if (capture.get("attributes", {}).get("adaptive_setup") or {}).get("status") != "OK":
                    rows.append(dict(campaign_id=cid, status="INPUT_UNAVAILABLE",
                                     reason="FROZEN_ADAPTIVE_SETUP_UNAVAILABLE"))
                    continue
                try:
                    snapshot = self.runtime.open_capture(capture)
                    if snapshot["state"]["closed"]:
                        self.intraday.pop(capture["position_id"], None)
                        rows.append(dict(campaign_id=cid, status="CLOSED"))
                        continue
                    observation = self._collect(capture, source, calendar)
                    outcome = self.runtime.advance(cid, observation, expected_revision=snapshot["revision"])
                    rows.append(dict(campaign_id=cid, status="RECORDED", decision=outcome.get("decision"),
                                     revision=outcome["revision"],
                                     missing_observations=outcome["state"]["missing_observations"]))
                except Exception as error:
                    # No arbitrary provider/scenario/SQL error strings in outputs.
                    rows.append(dict(campaign_id=cid, status="ERROR", error_type=type(error).__name__))
            result = dict(contract="oneil-shadow-run-v1", mode="SHADOW", source=source, calendar=calendar,
                          initial_arm=self.runtime.initial_arm,
                          started_at=started, completed_at=self.clock(), rows=rows,
                          prior_incomplete_cycle=pending is not None,
                          capture_completeness="UNKNOWN_SCHEDULER_AND_SOURCE_COVERAGE",
                          live_readiness=readiness(),
                          performance_validated=False, live_ready=False, broker_execution=False)
            result["packet_id"] = _hash(result)
            with self.runtime.ledger._transaction() as db:
                self.runtime.ledger._event(db, cycle_id + ":done", result)
            return result
        except Exception:
            # A durable start without done makes interruptions visible next run.
            raise

    def _collect(self, capture, source, calendar):
        plan, position = capture["attributes"]["adaptive_setup"]["plan"], capture["position_id"]
        quote = gates = stop = intraday = None
        reasons = []
        try:
            terminal = read_terminal(self.tape_db, capture)
        except Exception:
            terminal = None
            reasons.append("TERMINAL_SOURCE_UNAVAILABLE")
        if terminal is None:
            # Slow acquisition happens in this independent runner, never a sell loop.
            at = _time(self.clock())
            as_of = at.replace(minute=at.minute // 5 * 5, second=0, microsecond=0).isoformat()
            try:
                if calendar == "AUTO":
                    from tools.collect_trend_replay_data import EXCHANGES
                    metadata = self.quote_provider(plan["symbol"])
                    if metadata.get("symbol") != plan["symbol"] or metadata.get("currency") != "USD":
                        raise ValueError("calendar source identity mismatch")
                    calendar = EXCHANGES[metadata["exchange"]]
                if position not in self.intraday:
                    self.intraday[position] = self.intraday_factory()
                provider = self.intraday[position]
                intraday = provider(plan["symbol"], as_of, calendar)
            except Exception:
                reasons.append("INTRADAY_COLLECTION_UNAVAILABLE")
            try:
                cached = self.market_cache
                if (isinstance(cached, dict) and 0 <= (
                        _time(self.clock()) - _time(cached["observed_at"])).total_seconds() < 60):
                    market = cached
                else:
                    market = self.market_provider()
                    self.market_cache = market
            except Exception:
                market = None
                reasons.append("MARKET_COLLECTION_UNAVAILABLE")
            try:
                raw_quote = self.quote_provider(plan["symbol"])
                quote = quote_input(plan=plan, position_id=position, response=raw_quote, now=self.clock())
            except Exception:
                reasons.append("QUOTE_COLLECTION_UNAVAILABLE")
            try:
                scenario, portfolio, price = read_portfolio(self.holdings_db, capture, self.max_slots, self.clock)
                identity = dict(symbol=plan["symbol"], position_id=position,
                                source_decision_ref=plan["source_decision_ref"],
                                price_basis_ref=plan["setup"]["price_basis_ref"])
                stop = dict(identity, current_stop=price, source_ref=portfolio["source_ref"],
                            available_at=portfolio["observed_at"])
                gates = current_gates(plan=plan, position_id=position, scenario=scenario,
                                      quote=quote, portfolio=portfolio, market=market, now=self.clock())
            except Exception:
                reasons.append("CURRENT_PORTFOLIO_OR_GATES_UNAVAILABLE")
        if "TERMINAL_SOURCE_UNAVAILABLE" in reasons:
            # Do not add while original exit state is uncertain; protection is
            # still evaluated independently with the best verified stop.
            gates = None
        now = self.clock()
        result = capture_current_record(
            plan=plan, position_id=position, setup_input=dict(status="OK", setup=plan["setup"]),
            intraday_input=intraday, quote=quote, gates=gates, stop=stop, now=now,
            exit_event=terminal, source=source)
        result["reason_codes"].extend(reasons)
        result["record_hash"] = _hash({k: v for k, v in result.items() if k != "record_hash"})
        return result
