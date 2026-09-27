from copy import deepcopy
import json

import pytest

from prism_core.oneil_setup_inputs import build_setup_input, text_hash
from prism_core.oneil_adaptive_policy import create_plan

TEXT = "Base review: pivot 100, chart evidence reviewed. Growth leadership reviewed with earnings and sales evidence."


def span(a, b):
    return dict(start=a, end=b, sha256=text_hash(TEXT[a:b]))


def review():
    return dict(
        contract_version="oneil-setup-review-v1",
        market="US",
        symbol="TEST",
        decision_ref="d1",
        price_basis_ref="unadjusted-v1",
        report_sha256=text_hash(TEXT),
        reviewer_kind="HUMAN_REVIEW",
        reviewer_ref="private-reviewer-canary",
        criteria_ref="base-and-leader-review-v1",
        approved=True,
        reviewed_at="2026-09-25T13:29:00Z",
        report_observed_at="2026-09-25T13:00:00Z",
        base=dict(
            status="CONFIRMED",
            criteria_ref="base-rules",
            data_as_of="2026-09-24T20:00:00Z",
            pivot="100",
            pivot_span=span(19, 22),
            evidence_spans=[span(0, 47)],
        ),
        leadership=dict(
            status="CONFIRMED",
            criteria_ref="growth-review",
            data_as_of="2026-09-24T20:00:00Z",
            evidence_spans=[span(48, len(TEXT))],
        ),
    )


def build(record=None):
    return build_setup_input(
        report_text=TEXT,
        review=review() if record is None else record,
        symbol="TEST",
        decision_ref="d1",
        as_of="2026-09-25T13:30:00Z",
        price_basis_ref="unadjusted-v1",
    )


def test_source_bound_setup_feeds_policy_without_exporting_report():
    r = review()
    original = deepcopy(r)
    out = build(r)
    assert out["status"] == "OK" and r == original
    assert "canary" not in json.dumps(out) and TEXT not in json.dumps(out)
    frozen = create_plan(
        symbol="TEST",
        entry_reference="100",
        initial_stop="95",
        source_decision_ref="d1",
        created_at="2026-09-25T13:30:00Z",
        setup=out["setup"],
        entry_eligible=True,
    )
    assert frozen["authority"] == "CALLER_ATTESTED_NOT_AUTHENTICATED"
    assert out == build(r)


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("market", "KR", "INVALID"),
        ("symbol", "OTHER", "INVALID"),
        ("decision_ref", "other", "INVALID"),
        ("price_basis_ref", "adjusted", "INVALID"),
        ("report_sha256", "wrong", "INVALID"),
        ("reviewer_kind", "MODEL_ONLY", "MISSING"),
        ("approved", False, "MISSING"),
        ("reviewed_at", "2026-09-25T13:31:00Z", "INVALID"),
        ("report_observed_at", "2026-09-25T13:29:30Z", "INVALID"),
    ],
)
def test_bad_identity_authority_or_clock(field, value, status):
    r = review()
    r[field] = value
    assert build(r)["status"] == status


def test_missing_is_not_rejected():
    r = review()
    r["base"]["status"] = "MISSING"
    assert build(r)["status"] == "MISSING"
    r["base"]["status"] = "REJECTED"
    assert build(r)["status"] == "REJECTED"


@pytest.mark.parametrize(
    "change", ["hash", "range", "outside", "pivot", "future", "missing"]
)
def test_claim_links_cannot_be_fabricated(change):
    r = review()
    if change == "hash":
        r["base"]["pivot_span"]["sha256"] = "wrong"
    elif change == "range":
        r["base"]["pivot_span"]["end"] = 99999
    elif change == "outside":
        r["base"]["evidence_spans"] = [span(0, 18)]
    elif change == "pivot":
        r["base"]["pivot"] = "101"
    elif change == "future":
        r["base"]["data_as_of"] = "2026-09-26T00:00:00Z"
    else:
        del r["leadership"]["evidence_spans"]
    assert build(r)["status"] in {"MISSING", "INVALID"}
    assert build(r)["setup"] is None


def test_no_review_does_not_infer_from_report_words():
    out = build_setup_input(
        report_text=TEXT,
        review=None,
        symbol="TEST",
        decision_ref="d1",
        as_of="2026-09-25T13:30:00Z",
        price_basis_ref="unadjusted-v1",
    )
    assert out["status"] == "MISSING" and out["setup"] is None
