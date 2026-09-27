"""Operational adaptive campaign worker; real orders exist only for LIVE owners."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from observability.trading_context import execution_profile_ref
from prism_core.oneil_adaptive_policy import _hash, _time
from prism_core.oneil_config import load, require_live_approval
from prism_core.oneil_dispatcher import drive
from prism_core.oneil_execution import OneilExecution
from prism_core.oneil_routing import collect_initial_envelope, finalize_owned_strategy, materialize_initial
from prism_core.oneil_runtime import OneilRuntime
from prism_core.oneil_runtime_inputs import IntradayProvider, fetch_quote, fetch_market_snapshot
from prism_core.oneil_shadow_runner import read_captures, read_terminal, readonly


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def regular_session(now):
    import pandas as pd
    import pandas_market_calendars as calendars
    at = pd.Timestamp(_time(now))
    date = at.tz_convert("America/New_York").date()
    schedule = calendars.get_calendar("NYSE").schedule(date, date)
    return bool(len(schedule) and schedule.iloc[0]["market_open"] <= at < schedule.iloc[0]["market_close"])


def configured_accounts():
    # Same normalized account definitions as the existing US tracking agent.
    from trading import kis_auth
    mode = str(kis_auth.getEnv().get("default_mode", "demo")).strip().lower()
    return kis_auth.get_configured_accounts(svr="vps" if mode == "demo" else "prod", market="us")


class OneilService:
    def __init__(self, config, accounts, *, clock=utc_now, collector=collect_initial_envelope,
                 quote_provider=fetch_quote, market_provider=fetch_market_snapshot,
                 intraday_factory=IntradayProvider, driver=drive, live_agent_factory=None,
                 config_path=None):
        self.config, self.clock, self.collector, self.driver = config, clock, collector, driver
        self.accounts = {a["account_key"]: a for a in accounts}
        if len(self.accounts) != len(accounts):
            raise ValueError("duplicate configured accounts")
        self.profiles = {execution_profile_ref(k): a for k, a in self.accounts.items()}
        self.quote_provider, self.market_provider = quote_provider, market_provider
        self.intraday_factory, self.intraday, self.market_cache = intraday_factory, {}, None
        self.live_agent_factory = live_agent_factory
        self.config_path = config_path

    def _market(self):
        at = _time(self.clock())
        if self.market_cache and 0 <= (at - _time(self.market_cache["observed_at"])).total_seconds() < 60:
            return self.market_cache
        self.market_cache = self.market_provider()
        return self.market_cache

    def _reader(self, account):
        connection = readonly(self.config["holdings_db"])
        import sqlite3
        connection.row_factory = sqlite3.Row
        return SimpleNamespace(conn=connection, cursor=connection.cursor(), max_slots=self.config["max_slots"],
                               MAX_SAME_SECTOR=3, SECTOR_CONCENTRATION_RATIO=.3,
                               active_account=account)

    def _shadow_ingest(self, execution, runtime):
        path = Path(self.config["capture_db"])
        if not path.exists():
            return dict(status="AWAITING_FIRST_CAPTURE", loaded=0, unavailable=0)
        captures = read_captures(path, self.config["capture_since"])
        loaded, unavailable = 0, 0
        for capture in captures:
            try:
                if self._shadow_candidate(execution, runtime, capture):
                    loaded += 1
                else:
                    unavailable += 1
            except Exception:
                unavailable += 1
        return dict(status="CAPTURES_READ", loaded=loaded, unavailable=unavailable)

    def _shadow_candidate(self, execution, runtime, capture):
        attrs = capture["attributes"]
        account = self.profiles.get(attrs.get("execution_profile_ref"))
        if (account is None or account["name"] not in self.config["accounts"]
                or (attrs.get("adaptive_setup") or {}).get("status") != "OK"
                or not attrs.get("initial_underwriting")
                or _time(capture["event_time"]) > _time(self.clock())):
            return False
        plan = attrs["adaptive_setup"]["plan"]
        cid = _hash(["oneil-owned-execution-v1", "SHADOW", account["account_key"],
                     capture["position_id"], plan["plan_hash"]])
        try:
            existing = execution.snapshot(cid)
            context = existing["context"]
        except ValueError:
            metadata = self.quote_provider(capture["ticker"])
            if metadata.get("symbol") != capture["ticker"] or metadata.get("currency") != "USD":
                return False
            exchange = {"NMS": "NASD", "NGM": "NASD", "NCM": "NASD", "NYQ": "NYSE", "ASE": "AMEX"}.get(metadata.get("exchange"))
            if exchange is None:
                return False
            context = dict(account_name=account["name"], scenario=attrs["initial_underwriting"],
                company_name=capture["ticker"], source_capture_id=capture["event_id"],
                source_position_id=capture["position_id"], exchange=exchange,
                entry_slot_context={"max_slots": self.config["max_slots"]})
        state = execution.claim_campaign(account_id=account["account_key"], position_id=capture["position_id"],
            plan=plan, unit_budget=account["buy_amount_usd"], now=self.clock(), context=context,
            account_snapshot=dict(status="OK", account_id=account["account_key"], symbol=capture["ticker"],
                quantity=0, open_orders_status="OK", open_orders_count=0, observed_at=self.clock(),
                source_ref="SHADOW_EMPTY_INITIAL_ACCOUNT"))
        execution.link_strategy_position(state["campaign_id"], capture["position_id"])
        runtime.open_capture(capture)
        return True

    async def _live_agent(self, account):
        if self.live_agent_factory is not None:
            return await self.live_agent_factory(account)
        import sys
        us = str(Path(__file__).resolve().parents[1] / "prism-us")
        if us not in sys.path:
            sys.path.insert(0, us)
        from us_stock_tracking_agent import USStockTrackingAgent
        agent = USStockTrackingAgent(db_path=self.config["holdings_db"])
        await agent.initialize(skip_llm_agent=True)
        agent._set_active_account(account)
        return agent

    async def _observation(self, campaign, account, *, protection_only=False):
        # Original strategy exit has independent authority even if the holding
        # row is already deleted. Use exact captured position, never ticker join.
        terminal = None
        terminal_unavailable = False
        position = campaign.get("strategy_position_id") or campaign["position_id"]
        if campaign["mode"] == "SHADOW":
            try:
                terminal = read_terminal(self.config["tape_db"], dict(position_id=position))
                if terminal is not None:
                    expected = dict(source_decision_ref=campaign["plan"]["source_decision_ref"],
                                    symbol=campaign["symbol"], price_basis_ref=campaign["plan"]["setup"]["price_basis_ref"])
                    if any(terminal.get(k) != value for k, value in expected.items()):
                        raise ValueError("terminal source identity mismatch")
                    terminal["position_id"] = campaign["position_id"]
            except Exception:
                terminal = None
                terminal_unavailable = True
        agent = self._reader(account)
        try:
            if campaign["campaign_id"] not in self.intraday:
                self.intraday[campaign["campaign_id"]] = self.intraday_factory()
            observation = await self.collector(agent, campaign, source="mechanical", market_provider=self._market,
                quote_provider=self.quote_provider, intraday_provider=self.intraday[campaign["campaign_id"]],
                protection_only=protection_only)
            if terminal is not None:
                observation["exit_event"] = terminal
            if terminal_unavailable:
                observation.update(status="MISSING", tick=None)
                observation["reason_codes"].append("TERMINAL_SOURCE_UNAVAILABLE")
            observation["record_hash"] = _hash({k: v for k, v in observation.items() if k != "record_hash"})
            return observation
        finally:
            agent.conn.close()

    async def _finalize_strategy(self, execution, campaign):
        reference = campaign.get("strategy_exit_reference")
        if (campaign["mode"] != "LIVE" or not reference or not campaign.get("strategy_position_id")
                or campaign.get("strategy_exit_recorded")):
            return
        agent = await self._live_agent(self.accounts[campaign["account_id"]])
        try:
            applied = await finalize_owned_strategy(agent, execution, campaign["campaign_id"],
                reference["price"], campaign["exit_reason"] or "PROTECTIVE_STOP")
            latest = execution.snapshot(campaign["campaign_id"])
            if latest.get("strategy_exit_recorded"):
                return
            if not applied:
                # Recover a crash after the exact strategy history commit but
                # before the execution-journal checkpoint. Never infer a trade
                # from the mere absence of a holding row.
                agent.cursor.execute("SELECT scenario FROM us_trading_history WHERE ticker=? AND account_key=?",
                                     (campaign["symbol"], campaign["account_id"]))
                matches = 0
                for row in agent.cursor:
                    scenario = json.loads(row[0] or "{}")
                    if ((scenario.get("_oneil_execution") or {}).get("campaign_id") == campaign["campaign_id"]
                            and scenario.get("_decision_id") == campaign["plan"]["source_decision_ref"]):
                        matches += 1
                if matches != 1:
                    raise ValueError("STRATEGY_FINALIZATION_UNVERIFIED")
            execution.mark_strategy_exit(campaign["campaign_id"], at=reference["at"], source_ref=reference["source_ref"])
        finally:
            agent.conn.close()

    async def _protect_all(self, execution, rows):
        campaigns = sorted(execution.list_campaigns(), key=lambda c: c["status"] != "EXIT_PENDING")
        for campaign in campaigns:
            try:
                account = self.accounts[campaign["account_id"]]
                envelope = await self._observation(campaign, account, protection_only=True)
                driven = await self.driver(execution, campaign["campaign_id"], envelope,
                    account_name=account["name"], allow_add=False, now=self.clock)
                await self._finalize_strategy(execution, execution.snapshot(campaign["campaign_id"]))
                rows.append(dict(campaign_id=campaign["campaign_id"], mode="LIVE", phase="PROTECTION",
                                 status=driven["status"]))
            except Exception as error:
                rows.append(dict(campaign_id=campaign["campaign_id"], mode="LIVE", phase="PROTECTION",
                                 status="ERROR", error_type=type(error).__name__))

    async def _with_protection(self, awaitable, execution, rows):
        if execution is None:
            return await awaitable
        task = asyncio.create_task(awaitable)
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=30)
                if not done:
                    await self._protect_all(execution, rows)
            return await task
        finally:
            if not task.done():
                task.cancel()  # Collection only, never an in-flight broker order.
                await asyncio.gather(task, return_exceptions=True)

    async def once(self, *, session_open=None):
        now = self.clock()
        if session_open is None:
            session_open = regular_session(now)
        result = dict(contract="oneil-service-v1", mode=self.config["mode"], initial_arm="INITIAL_POLICY_50",
                      at=now, rows=[], live_activation=self.config["mode"] == "LIVE",
                      performance_validated=False, status="OUTSIDE_REGULAR_SESSION")
        if not session_open:
            return result
        result["status"] = "COMPLETED"
        modes, protection_execution = [], None
        if Path(self.config["live_db"]).exists():
            modes.append("LIVE")  # Protection continues even if new adds are OFF.
            protection_execution = OneilExecution(self.config["live_db"], mode="LIVE")
            await self._protect_all(protection_execution, result["rows"])
            for campaign in protection_execution.list_campaigns(active_only=False):
                if campaign["status"] == "CLOSED":
                    try:
                        await self._finalize_strategy(protection_execution, campaign)
                    except Exception as error:
                        result["rows"].append(dict(campaign_id=campaign["campaign_id"], mode="LIVE",
                            phase="STRATEGY_FINALIZE", status="ERROR", error_type=type(error).__name__))
        if self.config["mode"] == "SHADOW":
            modes.append("SHADOW")
        for mode in modes:
            if mode == "LIVE" and self.config["mode"] != "LIVE":
                continue
            execution = OneilExecution(self.config["shadow_db"] if mode == "SHADOW" else self.config["live_db"], mode=mode)
            runtime = OneilRuntime(self.config["runtime_db"]) if mode == "SHADOW" else None
            if mode == "SHADOW":
                result["capture"] = await self._with_protection(
                    asyncio.to_thread(self._shadow_ingest, execution, runtime), protection_execution, result["rows"])
            campaigns = execution.list_campaigns()
            if len(campaigns) > 200:
                raise ValueError("bounded active campaign limit exceeded")
            for campaign in campaigns:
                cid = campaign["campaign_id"]
                try:
                    account = self.accounts[campaign["account_id"]]
                    envelope = await self._with_protection(
                        self._observation(campaign, account), protection_execution, result["rows"])
                    allow_add = mode == "SHADOW" and self.config["mode"] == "SHADOW"
                    if mode == "LIVE" and self.config["mode"] == "LIVE":
                        try:
                            require_live_approval(self.config, now=self.clock(), account=account["name"], unit_budget=campaign["unit_budget"])
                            allow_add = True
                        except (ValueError, KeyError, TypeError):
                            allow_add = False
                    if runtime is not None:
                        runtime_id = runtime.campaign_id_for_position(campaign["position_id"])
                        snap = runtime.snapshot(runtime_id)
                        if not snap["state"]["closed"]:
                            runtime.advance(runtime_id, envelope, expected_revision=snap["revision"])
                    live_agent = None
                    try:
                        callback = None
                        if mode == "LIVE" and not campaign.get("strategy_position_id"):
                            live_agent = await self._live_agent(account)
                            async def callback(reserved):
                                return await materialize_initial(live_agent, execution, reserved)
                        def authorize():
                            current = load(self.config_path)
                            if current["mode"] != "LIVE" or current["live_db"] != self.config["live_db"]:
                                return False
                            require_live_approval(current, now=self.clock(), account=account["name"],
                                                  unit_budget=campaign["unit_budget"])
                            return True
                        driven = await self.driver(execution, cid, envelope, account_name=account["name"],
                            allow_add=allow_add, before_submit=callback, now=self.clock,
                            authorize_add=authorize if mode == "LIVE" else None)
                    finally:
                        if live_agent is not None and live_agent.conn is not None:
                            live_agent.conn.close()
                    after = execution.snapshot(cid)
                    if mode == "LIVE":
                        await self._finalize_strategy(execution, after)
                    result["rows"].append(dict(campaign_id=cid, mode=mode, status=driven["status"],
                        owned_status=after["status"], quantity=after["confirmed_quantity"],
                        target=after["last_target"], virtual=mode == "SHADOW"))
                except Exception as error:
                    result["rows"].append(dict(campaign_id=cid, mode=mode, status="ERROR", error_type=type(error).__name__))
        result["completed_at"] = self.clock()
        result["packet_id"] = _hash(result)
        return result
