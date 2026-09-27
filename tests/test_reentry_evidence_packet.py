from tools import build_reentry_evidence_packet as P


def _watch(enrollment, source, kind, ret, check=True, control=None, status="CLOSED"):
    return {"enrollment": enrollment, "source": source, "status": status, "status_final": "SIGNALLED",
            "control": control or {},
            "events": [{"kind": kind, "date": "2026-09-01", "market_check": {"ok": check},
                        "trade": {"status": "CLOSED", "ret": ret} if ret is not None else {"status": "PENDING"}}]}


def test_packet_separates_enrollment_and_never_fills_pending():
    state = {"market": "KR", "watches": [
        _watch("PROSPECTIVE", "LOCATION_SKIP", "PULLBACK", 0.05,
               control={"kind": "IMMEDIATE_NEXT_OPEN", "status": "CLOSED", "ret": 0.02}),
        _watch("PROSPECTIVE", "LOCATION_SKIP", "PULLBACK", None),
        _watch("LATE", "STOP_EXIT", "RECLAIM", -0.07, check=False,
               control={"kind": "HOLD_WITHOUT_STOP", "status": "CLOSED", "ret": -0.1}),
    ]}
    packet = P.build([state])
    trades = packet["trades"]
    assert trades["KR|PROSPECTIVE|LOCATION_SKIP|PULLBACK|all"]["n"] == 1
    assert trades["KR|LATE|STOP_EXIT|RECLAIM|market_weak"]["mean"] == -0.07
    assert packet["paired_vs_immediate"]["KR|PROSPECTIVE|LOCATION_SKIP|PULLBACK"]["mean"] == 0.03
    assert packet["controls"]["KR|LATE|STOP_EXIT|HOLD_WITHOUT_STOP"]["n"] == 1
    assert packet["status_counts"]["KR|PROSPECTIVE|LOCATION_SKIP|PULLBACK"] == {"CLOSED": 1, "PENDING": 1}
    assert packet["sufficiency"]["KR"]["verdict"] == "CONTINUE_CAPTURE"
