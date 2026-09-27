"""Account-scoped KIS execution and authoritative, paginated reconciliation.

CCNL contract: official open-trading-api examples_llm/overseas_stock/inquire_ccnl.
Order acceptance and an empty unfilled list are never treated as a fill. KIS
CCNL supplies quantity/notional but no fee field: actual fees remain unknown.
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import time
from zoneinfo import ZoneInfo

from prism_core.execution_service import ExecutionService
from prism_core.oneil_adaptive_policy import _hash


def _number(value, *, integer=False):
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        raise ValueError("BROKER_NUMBER_MISSING")
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("BROKER_NUMBER_INVALID") from exc
    if not parsed.is_finite() or parsed < 0 or (integer and parsed != int(parsed)):
        raise ValueError("BROKER_NUMBER_INVALID")
    return int(parsed) if integer else parsed


def _now():
    return datetime.now(timezone.utc).isoformat()


class OneilBroker:
    def __init__(self, account_name, intent_store, exchange="NASD", *, service_factory=None):
        if not account_name or exchange not in {"NASD", "NYSE", "AMEX"}:
            raise ValueError("BROKER_SCOPE_REQUIRED")
        self.account_name, self.store, self.exchange = account_name, intent_store, exchange
        self.service = (service_factory or ExecutionService.us)(account_name=account_name, intent_store=intent_store)

    async def __aenter__(self):
        await self.service.__aenter__()
        if self.service.account_name != self.account_name or not self.service.account_key:
            await self.service.__aexit__(None, None, None)
            raise ValueError("BROKER_ACCOUNT_MISMATCH")
        return self

    async def __aexit__(self, *args):
        return await self.service.__aexit__(*args)

    def _check(self, intent):
        if intent.account_id != self.service.account_key or intent.market != "US" or intent.side not in {"BUY", "SELL"}:
            raise ValueError("BROKER_INTENT_SCOPE_MISMATCH")
        if intent.execution_mode.lower() != "live" or self.service.mode not in {"real", "demo"}:
            raise ValueError("BROKER_EXECUTION_MODE_MISMATCH")

    async def submit(self, intent, reservation, *, quote_validator=None):
        self._check(intent)
        quantity = _number(intent.quantity, integer=True)
        limit = _number(intent.limit_price)
        if quantity <= 0 or limit <= 0 or limit != limit.quantize(Decimal(".01")) or quote_validator is None:
            raise ValueError("EXACT_ORDER_OR_VALIDATOR_MISSING")
        if not self.service.is_market_open():
            raise ValueError("REGULAR_SESSION_REQUIRED")
        kwargs = dict(intent=intent, reservation=reservation, ticker=intent.symbol,
                      exchange=self.exchange, limit_price=float(limit),
                      quote_validator=quote_validator, regular_session_only=True)
        if intent.side == "BUY":
            budget = limit * quantity
            if intent.cash_amount is not None and budget > _number(intent.cash_amount):
                raise ValueError("ORDER_EXCEEDS_INTENT_BUDGET")
            ack = await self.service.execute_pre_reserved_buy(**kwargs, buy_amount=float(budget),
                                                              strict_budget=True, exact_quantity=quantity)
        else:
            ack = await self.service.execute_pre_reserved_sell(**kwargs, quantity=quantity)
        if isinstance(ack, dict) and ack.get("order_no"):
            ack = dict(ack)
            ack["broker_order_date"] = await self.discover_order_date(intent, str(ack["order_no"]))
            ack["broker_order_date_status"] = "CONFIRMED" if ack["broker_order_date"] else "UNKNOWN"
        return ack

    def _pages(self, endpoint, real_tr, demo_tr, params, output):
        params = dict(CANO=self.service.trenv.my_acct, ACNT_PRDT_CD=self.service.trenv.my_prod,
                      CTX_AREA_FK200="", CTX_AREA_NK200="", **params)
        rows, seen = [], set()
        deadline = time.monotonic() + 30
        for page in range(20):
            if time.monotonic() >= deadline:
                raise ValueError("BROKER_QUERY_DEADLINE")
            response = self.service._request("/uapi/overseas-stock/v1/trading/" + endpoint,
                                             real_tr if self.service.mode == "real" else demo_tr,
                                             params, request_cont="N" if page else "")
            if not response.isOK():
                raise ValueError("BROKER_QUERY_FAILED")
            body, header = response.getBody(), response.getHeader()
            values = getattr(body, output)
            if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
                raise ValueError("BROKER_ROWS_INVALID")
            rows.extend(values)
            continuation = getattr(header, "tr_cont")
            if continuation not in {"M", "F", "D", "E", ""}:
                raise ValueError("BROKER_CONTINUATION_INVALID")
            if continuation not in {"M", "F"}:
                return rows
            keys = (body.ctx_area_fk200, body.ctx_area_nk200)
            if keys in seen or not any(keys):
                raise ValueError("BROKER_PAGINATION_STALLED")
            seen.add(keys)
            params.update(CTX_AREA_FK200=keys[0], CTX_AREA_NK200=keys[1])
        raise ValueError("BROKER_PAGE_LIMIT")

    async def holdings(self, symbol):
        result = dict(status="UNKNOWN", account_id=self.service.account_key, symbol=symbol,
                      quantity=None, observed_at=_now())
        try:
            rows = await asyncio.to_thread(self._pages, "inquire-balance", "TTTS3012R", "VTTS3012R",
                                           dict(OVRS_EXCG_CD="NASD", TR_CRCY_CD="USD"), "output1")
            matches = [row for row in rows if row.get("ovrs_pdno") == symbol]
            if len(matches) > 1:
                raise ValueError("DUPLICATE_HOLDING")
            quantity = _number(matches[0]["ovrs_cblc_qty"], integer=True) if matches else 0
            pending = await asyncio.to_thread(self._pages, "inquire-nccs", "TTTS3018R", "VTTS3018R",
                dict(OVRS_EXCG_CD="NASD", SORT_SQN="DS"), "output")
            open_orders = [row for row in pending if row.get("pdno") == symbol
                           and _number(row.get("nccs_qty"), integer=True) > 0]
            result.update(status="OK", quantity=quantity, open_orders_status="OK", open_orders_count=len(open_orders), observed_at=_now())
        except Exception:
            result["reason"] = "HOLDINGS_UNCONFIRMED"
        result["source_ref"] = _hash(result)
        return result

    async def discover_order_date(self, intent, broker_order_id):
        """Query a bounded market-local date range; return only the broker row date."""
        self._check(intent)
        try:
            created = datetime.fromisoformat(intent.created_at.replace("Z", "+00:00"))
            if created.tzinfo is None:
                return None
            start = created.astimezone(ZoneInfo("America/New_York")).date()
            end = datetime.now(ZoneInfo("America/New_York")).date()
            if not 0 <= (end - start).days <= 3:
                return None
            rows = await asyncio.to_thread(self._pages, "inquire-ccnl", "TTTS3035R", "VTTS3035R",
                dict(PDNO="%" if self.service.mode == "real" else "", ORD_STRT_DT=start.strftime("%Y%m%d"),
                     ORD_END_DT=end.strftime("%Y%m%d"), SLL_BUY_DVSN="00", CCLD_NCCS_DVSN="00",
                     OVRS_EXCG_CD="NASD" if self.service.mode == "real" else "", SORT_SQN="DS",
                     ORD_DT="", ORD_GNO_BRNO="", ODNO=""), "output")
            matches = [row for row in rows if str(row.get("odno")) == str(broker_order_id)
                and row.get("pdno") == intent.symbol
                and row.get("sll_buy_dvsn_cd") == ("02" if intent.side == "BUY" else "01")
                and _number(row.get("ft_ord_qty"), integer=True) == intent.quantity]
            if len(matches) != 1:
                return None
            date = matches[0]["ord_dt"]
            parsed = datetime.strptime(date, "%Y%m%d").date()
            return date if start <= parsed <= end else None
        except Exception:
            return None

    async def reconcile(self, intent, broker_order_id, order_date=None):
        self._check(intent)
        result = dict(status="UNKNOWN", broker_order_id=str(broker_order_id), order_date=order_date,
                      broker_order_date=order_date, intent_id=intent.id, virtual=False,
                      account_id=intent.account_id, symbol=intent.symbol, side=intent.side,
                      filled_qty=None, filled_quantity=None, filled_notional=None, remaining_qty=None,
                      fees=None, fees_status="UNKNOWN", observed_at=_now())
        if order_date is None:
            order_date = await self.discover_order_date(intent, broker_order_id)
            if order_date is None:
                return dict(result, reason="BROKER_ORDER_DATE_UNCONFIRMED")
        if not re.fullmatch(r"\d{8}", order_date) or not str(broker_order_id).isdigit():
            raise ValueError("EXACT_ORDER_IDENTITY_REQUIRED")
        result = dict(status="UNKNOWN", broker_order_id=str(broker_order_id), order_date=order_date,
                      broker_order_date=order_date, intent_id=intent.id, virtual=False,
                      account_id=intent.account_id, symbol=intent.symbol, side=intent.side,
                      filled_qty=None, filled_quantity=None, filled_notional=None, remaining_qty=None,
                      fees=None, fees_status="UNKNOWN", observed_at=_now())
        try:
            rows = await asyncio.to_thread(self._pages, "inquire-ccnl", "TTTS3035R", "VTTS3035R",
                dict(PDNO="%" if self.service.mode == "real" else "", ORD_STRT_DT=order_date,
                     ORD_END_DT=order_date, SLL_BUY_DVSN="00", CCLD_NCCS_DVSN="00",
                     OVRS_EXCG_CD="NASD" if self.service.mode == "real" else "", SORT_SQN="DS",
                     ORD_DT="", ORD_GNO_BRNO="", ODNO=""), "output")
            matches = [row for row in rows if str(row.get("odno")) == str(broker_order_id)
                       and row.get("ord_dt") == order_date and row.get("pdno") == intent.symbol
                       and row.get("sll_buy_dvsn_cd") == ("02" if intent.side == "BUY" else "01")]
            if len(matches) != 1:
                raise ValueError("EXACT_ORDER_UNCONFIRMED")
            row = matches[0]
            ordered = _number(row["ft_ord_qty"], integer=True)
            filled = _number(row["ft_ccld_qty"], integer=True)
            remaining = _number(row["nccs_qty"], integer=True)
            notional = _number(row["ft_ccld_amt3"])
            if ordered != intent.quantity or filled + remaining > ordered or (filled > 0 and notional <= 0):
                raise ValueError("FILL_QUANTITY_INCONSISTENT")
            if row.get("tr_crcy_cd") != "USD" or row.get("ovrs_excg_cd") not in {"NASD", "NYSE", "AMEX"}:
                raise ValueError("FILL_CURRENCY_OR_EXCHANGE_UNKNOWN")
            result.update(filled_qty=filled, filled_quantity=filled, filled_notional=str(notional),
                          remaining_qty=remaining, fill_evidence_status="CONFIRMED")
            # Unfilled inquiry must also complete. Absence alone proves nothing.
            pending = await asyncio.to_thread(self._pages, "inquire-nccs", "TTTS3018R", "VTTS3018R",
                dict(OVRS_EXCG_CD="NASD", SORT_SQN="DS"), "output")
            open_rows = [r for r in pending if str(r.get("odno")) == str(broker_order_id)
                         and r.get("pdno") == intent.symbol]
            if len(open_rows) > 1 or (remaining and (len(open_rows) != 1 or
                    _number(open_rows[0]["nccs_qty"], integer=True) != remaining)) or (not remaining and open_rows):
                raise ValueError("PENDING_ORDER_INCONSISTENT")
            status = "FILLED" if filled == ordered and not remaining else "PARTIAL" if filled and remaining else "PENDING" if remaining else "UNKNOWN"
            if not remaining and filled < ordered:
                cancellations = [r for r in rows if str(r.get("orgn_odno")) == str(broker_order_id)
                    and r.get("ord_dt") == order_date and r.get("pdno") == intent.symbol
                    and r.get("sll_buy_dvsn_cd") == row["sll_buy_dvsn_cd"]
                    and r.get("rvse_cncl_dvsn") == "02" and r.get("prcs_stat_name") == "완료"]
                if len(cancellations) == 1 and _number(cancellations[0]["ft_ord_qty"], integer=True) == ordered - filled:
                    status = "CANCELLED"
            result.update(status=status, filled_qty=filled, filled_quantity=filled, filled_notional=str(notional),
                          remaining_qty=remaining, observed_at=_now())
        except Exception:
            result["reason"] = "EXACT_FILL_UNCONFIRMED"
        return result

    async def cancel(self, intent, broker_order_id, order_date):
        receipt = await self.reconcile(intent, broker_order_id, order_date)
        if receipt["status"] not in {"PARTIAL", "PENDING"} or not receipt["remaining_qty"]:
            return dict(success=False, status="UNKNOWN", reason="PENDING_QUANTITY_UNCONFIRMED")
        return await self.service.amend_or_cancel("cancel", ticker=intent.symbol,
            orgn_odno=str(broker_order_id), quantity=receipt["remaining_qty"], exchange=self.exchange)
