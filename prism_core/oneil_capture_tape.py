"""Append-only local research evidence, never execution or prospective proof."""
from contextlib import closing
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from prism_core.oneil_adaptive_policy import _num, _ref, _time, _validate
from prism_core.oneil_paired_replay import _identity, _validate_campaign, digest
from prism_core.scenario_shadow_policy import _validate_plan

VERSION = "oneil-capture-tape-v1"
_APP_ID = 1330533456
_SCHEMA = (
    "CREATE TABLE campaigns (campaign_id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
    "CREATE TABLE records (event_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id), kind TEXT NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(campaign_id, occurred_at))",
    "CREATE TABLE gaps (gap_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id), payload TEXT NOT NULL)",
    "CREATE INDEX records_campaign_kind ON records(campaign_id, kind)",
    "CREATE INDEX gaps_campaign ON gaps(campaign_id, gap_id)",
    "CREATE TRIGGER campaigns_update BEFORE UPDATE ON campaigns BEGIN SELECT RAISE(ABORT, 'immutable tape'); END",
    "CREATE TRIGGER campaigns_delete BEFORE DELETE ON campaigns BEGIN SELECT RAISE(ABORT, 'immutable tape'); END",
    "CREATE TRIGGER records_update BEFORE UPDATE ON records BEGIN SELECT RAISE(ABORT, 'immutable tape'); END",
    "CREATE TRIGGER records_delete BEFORE DELETE ON records BEGIN SELECT RAISE(ABORT, 'immutable tape'); END",
    "CREATE TRIGGER gaps_update BEFORE UPDATE ON gaps BEGIN SELECT RAISE(ABORT, 'immutable tape'); END",
    "CREATE TRIGGER gaps_delete BEFORE DELETE ON gaps BEGIN SELECT RAISE(ABORT, 'immutable tape'); END",
    "CREATE TRIGGER campaigns_replace BEFORE INSERT ON campaigns WHEN EXISTS (SELECT 1 FROM campaigns WHERE campaign_id=NEW.campaign_id) BEGIN SELECT RAISE(ABORT, 'immutable tape'); END",
    "CREATE TRIGGER records_replace BEFORE INSERT ON records WHEN EXISTS (SELECT 1 FROM records WHERE event_id=NEW.event_id) BEGIN SELECT RAISE(ABORT, 'immutable tape'); END",
    "CREATE TRIGGER gaps_replace BEFORE INSERT ON gaps WHEN EXISTS (SELECT 1 FROM gaps WHERE gap_id=NEW.gap_id) BEGIN SELECT RAISE(ABORT, 'immutable tape'); END",
)


def _json(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > 2_000_000:
        raise ValueError("bounded evidence required")
    return encoded


def _check(connection):
    if (connection.execute("PRAGMA application_id").fetchone()[0] != _APP_ID
            or connection.execute("PRAGMA user_version").fetchone()[0] != 1
            or {r[0] for r in connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")} != set(_SCHEMA)):
        raise ValueError("dedicated owned research tape required")


class OneilCaptureTape:
    """Each call commits atomically; duplicate retries never overwrite originals."""

    def __init__(self, path, *, timeout=.05):
        self.path = Path(path)
        self.timeout = timeout
        if self.path.is_symlink() or self.path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
            raise ValueError("dedicated research database required")
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".oneil-tape-", dir=self.path.parent)
            os.close(fd)
            try:
                with closing(sqlite3.connect(temporary)) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    for sql in _SCHEMA:
                        conn.execute(sql)
                    conn.execute("PRAGMA application_id=1330533456")
                    conn.execute("PRAGMA user_version=1")
                try:
                    os.link(temporary, self.path)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary)
        with closing(self._connect()) as conn:
            _check(conn)

    def _connect(self):
        if self.path.is_symlink():
            raise ValueError("symlink tape rejected")
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=self.timeout)
        try:
            _check(conn)
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def campaign_id_for_position(position_id):
        return digest([VERSION, _ref(position_id)])

    def bind_event_identity(self, campaign_id, event):
        """Copy frozen identity only; never infer quote, stop or gate facts."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
        if row is None or json.loads(row[0])["plan"] is None:
            raise ValueError("adaptive plan unavailable")
        plan = json.loads(row[0])["plan"]
        bound = dict(event)
        for key, value in (("symbol", plan["symbol"]),
                           ("source_decision_ref", plan["source_decision_ref"]),
                           ("price_basis_ref", plan["setup"]["price_basis_ref"])):
            if key in bound and bound[key] != value:
                raise ValueError("identity mismatch")
            bound[key] = value
        return bound

    def ingest_capture(self, capture):
        """Accept the original scenario capture payload, including missing setups."""
        try:
            return self._ingest_capture(capture)
        except (ValueError, KeyError, TypeError, sqlite3.IntegrityError):
            if isinstance(capture, dict) and isinstance(capture.get("position_id"), str):
                self.record_gap(self.campaign_id_for_position(capture["position_id"]),
                                "REJECTED_CAPTURE", capture)
            raise

    def _ingest_capture(self, capture):
        capture = json.loads(_json(capture))
        attrs = capture["attributes"]
        if (capture["market"] != "US" or attrs.get("capture_schema_version") != 1
                or attrs.get("phase") != "POST_STRATEGY_COMMIT_PRE_BROKER"
                or attrs.get("confirmed_fill") is not False
                or attrs.get("trading_impact") != "none"):
            raise ValueError("original research capture required")
        for key in ("position_id", "decision_id", "ticker", "event_id"):
            _ref(capture[key])
        original = attrs["plan"]
        _validate_plan(original)
        at = _time(capture["event_time"])
        if (_time(original["entry_at"]) != at
                or original["source_decision_hash"] != digest(capture["decision_id"])):
            raise ValueError("original capture identity mismatch")
        adaptive = attrs.get("adaptive_setup") or {}
        plan = adaptive.get("plan") if adaptive.get("status") == "OK" else None
        if plan is not None:
            _validate(plan)
            if (plan["symbol"] != capture["ticker"]
                    or plan["source_decision_ref"] != capture["decision_id"]
                    or _time(plan["created_at"]) != at
                    or _num(plan["entry_reference"]) != _num(original["entry_price"])
                    or _num(plan["initial_stop"]) != _num(original["initial_stop"])):
                raise ValueError("adaptive capture identity mismatch")
        campaign_id = self.campaign_id_for_position(capture["position_id"])
        row = dict(campaign_id=campaign_id, plan=plan, capture_sha256=digest(capture),
                   source_capture_id=capture["event_id"], position_id=capture["position_id"],
                   status="OK" if plan else "MISSING", entry=None)
        if plan:
            row["entry"] = dict(symbol=plan["symbol"], source_decision_ref=plan["source_decision_ref"],
                                price_basis_ref=plan["setup"]["price_basis_ref"],
                                source_ref=capture["event_id"], occurred_at=at.isoformat(),
                                available_at=at.isoformat(), price=plan["entry_reference"])
        encoded = _json(row)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT payload FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
            if old and old[0] != encoded:
                raise ValueError("immutable capture conflict")
            if not old:
                conn.execute("INSERT INTO campaigns VALUES (?,?)", (campaign_id, encoded))
        return dict(campaign_id=campaign_id, status="DUPLICATE" if old else "RECORDED",
                    evidence_status=row["status"])

    def append_tick(self, campaign_id, tick):
        return self._append(campaign_id, "TICK", tick)

    def append_exit(self, campaign_id, terminal):
        return self._append(campaign_id, "EXIT", terminal)

    def append_observation(self, campaign_id, envelope):
        """Bridge tick/gap input; callers handle terminal exits via append_exit first."""
        try:
            envelope = json.loads(_json(envelope))
            content = {k: v for k, v in envelope.items() if k != "record_hash"}
            if (envelope.get("contract_version") != "oneil-current-capture-v1"
                    or envelope.get("record_hash") != digest(content)):
                raise ValueError("current observation contract or hash mismatch")
            bound = self.bind_event_identity(campaign_id, envelope)
            with closing(self._connect()) as conn:
                row = json.loads(conn.execute("SELECT payload FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()[0])
            if (bound["position_id"] != row["position_id"]
                    or bound["plan_hash"] != row["plan"]["plan_hash"]):
                raise ValueError("observation identity mismatch")
            _time(bound["occurred_at"])
            if bound.get("status") == "OK" and isinstance(bound.get("tick"), dict):
                if (bound["tick"].get("position_id") != row["position_id"]
                        or _time(bound["tick"]["occurred_at"]) != _time(bound["occurred_at"])):
                    raise ValueError("observation clock mismatch")
                return self.append_tick(campaign_id, bound["tick"])
            self.record_gap(campaign_id, "MISSING_CURRENT_OBSERVATION", envelope)
            return dict(status="RECORDED", evidence_status="MISSING_OR_INVALID")
        except (ValueError, KeyError, TypeError):
            self.record_gap(campaign_id, "REJECTED_CURRENT_OBSERVATION", envelope)
            raise

    def _append(self, campaign_id, kind, event):
        try:
            return self._append_validated(campaign_id, kind, event)
        except (ValueError, KeyError, TypeError, sqlite3.IntegrityError):
            self.record_gap(campaign_id, "REJECTED_" + kind, event)
            raise

    def record_gap(self, campaign_id, reason, evidence):
        """An observed failed collection is durable and invalidates comparison."""
        _ref(reason)
        record = dict(reason=reason, evidence_sha256=digest(evidence))
        gap_id = digest([campaign_id, record])
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone():
                return
            if not conn.execute("SELECT 1 FROM gaps WHERE gap_id=?", (gap_id,)).fetchone():
                conn.execute("INSERT INTO gaps VALUES (?,?,?)", (gap_id, campaign_id, _json(record)))

    def _append_validated(self, campaign_id, kind, event):
        event = json.loads(_json(event))
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
            if row is None:
                raise ValueError("unknown campaign")
            campaign = json.loads(row[0])
            plan = campaign["plan"]
            if plan is None:
                raise ValueError("adaptive plan unavailable")
            at = _identity(event, plan)
            if "position_id" in event and event["position_id"] != campaign["position_id"]:
                raise ValueError("position identity mismatch")
            event["occurred_at"] = at.isoformat()
            event["available_at"] = _time(event["available_at"]).isoformat()
            event_id = digest([campaign_id, kind, event["occurred_at"]])
            old = conn.execute("SELECT payload FROM records WHERE event_id=?", (event_id,)).fetchone()
            if old:
                stored = json.loads(old[0])
                if stored["event"] != event:
                    raise ValueError("immutable event conflict")
                return dict(event_id=event_id, status="DUPLICATE", evidence_status=stored["evidence_status"])
            previous = conn.execute(
                "SELECT kind,payload FROM records WHERE campaign_id=? ORDER BY occurred_at DESC LIMIT 1",
                (campaign_id,)).fetchone()
            if previous and previous[0] == "EXIT":
                raise ValueError("closed campaign")
            last = json.loads(previous[1])["event"] if previous else campaign["entry"]
            if at <= _time(last["occurred_at"]):
                raise ValueError("nonmonotonic event")
            status = "OK"
            if kind == "TICK":
                stop = _num(event["current_stop"], True)
                if stop < _num(last.get("current_stop", plan["initial_stop"]), True):
                    raise ValueError("lowered stop")
                _ref(event["stop_source_ref"])
                if _time(event["stop_available_at"]) > at:
                    raise ValueError("future stop")
                # Reuse replay's complete validation even if policy would early-return.
                probe = dict(campaign, ticks=[event], exit=dict(campaign["entry"],
                             occurred_at=(at + timedelta(seconds=1)).isoformat()))
                try:
                    _validate_campaign(probe)
                except (KeyError, ValueError, TypeError, AttributeError, ArithmeticError):
                    status = "MISSING_OR_INVALID"
            else:
                _num(event["price"], True)
            stored = _json(dict(event=event, evidence_status=status))
            conn.execute("INSERT INTO records VALUES (?,?,?,?,?)",
                         (event_id, campaign_id, kind, event["occurred_at"], stored))
        return dict(event_id=event_id, status="RECORDED", evidence_status=status)

    def export_replay(self):
        """Retain incomplete closed campaigns so replay reports unavailable, not zero."""
        campaigns, opened, missing, bad_ticks, gaps = [], 0, 0, 0, 0
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN")
            rows = conn.execute("SELECT campaign_id,payload FROM campaigns ORDER BY campaign_id LIMIT 1001").fetchall()
            if len(rows) > 1000:
                raise ValueError("bounded replay requires at most 1000 campaigns")
            for campaign_id, raw in rows:
                original = json.loads(raw)
                tick_count = conn.execute(
                    "SELECT COUNT(*) FROM records WHERE campaign_id=? AND kind='TICK'",
                    (campaign_id,)).fetchone()[0]
                if tick_count > 10000:
                    raise ValueError("bounded replay requires at most 10000 ticks per campaign")
                capture_gaps = [json.loads(r[0]) for r in conn.execute(
                    "SELECT payload FROM gaps WHERE campaign_id=? ORDER BY gap_id", (campaign_id,))]
                gaps += len(capture_gaps)
                if original["plan"] is None:
                    missing += 1
                    continue
                ticks, terminal = [], None
                for kind, record in conn.execute("SELECT kind,payload FROM records WHERE campaign_id=? ORDER BY occurred_at", (campaign_id,)):
                    item = json.loads(record)
                    if kind == "TICK":
                        ticks.append(item["event"])
                        bad_ticks += item["evidence_status"] != "OK"
                    else:
                        terminal = item["event"]
                if terminal is None:
                    opened += 1
                    continue
                campaigns.append(dict(campaign_id=campaign_id, plan=original["plan"],
                                      entry=original["entry"], ticks=ticks, exit=terminal,
                                      capture_gaps=capture_gaps))
        return dict(contract="oneil-paired-replay-input-v1", kind="HISTORICAL", campaigns=campaigns,
                    source_authentication="LOCALLY_CAPTURED_NOT_AUTHENTICATED",
                    prospective_proof=False, adaptive_arm="COMPATIBILITY_SCOUT_10",
                    capture_coverage=dict(total=len(rows), closed=len(campaigns), open=opened,
                                          missing_plan=missing, missing_or_invalid_ticks=bad_ticks,
                                          capture_gaps=gaps))
