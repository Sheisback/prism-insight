"""Turn deterministic research calculations into a source-bound review supplement."""

import json

from prism_core.oneil_auto_review import PASS, VERSION, evaluate_auto_review
from prism_core.oneil_setup_inputs import build_setup_input, text_hash, _time


def build_review_bundle(snapshot, *, reviewed_at):
    reviewed_at = _time(reviewed_at).isoformat()
    assessment = evaluate_auto_review(snapshot, as_of=reviewed_at)
    text = (
        "# Automated research supplement (not a BUY recommendation)\n"
        f"Rule: {VERSION}\nInput hash: {assessment['input_hash']}\n"
        f"Reviewed at: {reviewed_at}\n"
        f"Sources: {json.dumps(assessment['source_refs'], sort_keys=True)}\n"
        f"Available at: {json.dumps(assessment['data_as_of'], sort_keys=True)}\n"
    )
    spans = {}
    pivot_span = None
    for name in ("base", "leadership"):
        start = len(text)
        component = assessment[name]
        text += f"\n## {name}\nStatus: {component['status']}\n"
        if name == "base" and component.get("pivot") is not None:
            text += "Pivot USD: "
            pivot_start = len(text)
            text += component["pivot"]
            pivot_span = dict(
                start=pivot_start, end=len(text), sha256=text_hash(component["pivot"])
            )
            text += "\n"
        text += "Metrics: " + json.dumps(component["metrics"], sort_keys=True) + "\n"
        text += "Reasons: " + json.dumps(component["reason_codes"]) + "\n"
        spans[name] = dict(start=start, end=len(text), sha256=text_hash(text[start:]))
    review = dict(
        contract_version="oneil-setup-review-v1",
        market="US",
        symbol=snapshot["symbol"],
        decision_ref=snapshot["decision_ref"],
        price_basis_ref=snapshot["price_basis_ref"],
        report_sha256=text_hash(text),
        report_observed_at=reviewed_at,
        reviewed_at=reviewed_at,
        reviewer_kind="VALIDATED_RULE_OUTPUT",
        reviewer_ref=VERSION + ":" + str(assessment["input_hash"]),
        criteria_ref=VERSION,
        approved=assessment["status"] in {PASS, "REJECTED"},
    )
    for name in ("base", "leadership"):
        status = assessment[name]["status"]
        review[name] = dict(
            status="CONFIRMED"
            if status == PASS
            else "REJECTED"
            if status == "REJECTED"
            else "MISSING",
            criteria_ref=VERSION + ":" + name,
            data_as_of=reviewed_at,
            evidence_spans=[spans[name]],
        )
    if pivot_span:
        review["base"].update(pivot=assessment["base"]["pivot"], pivot_span=pivot_span)
    setup = build_setup_input(
        report_text=text,
        review=review,
        symbol=snapshot["symbol"],
        decision_ref=snapshot["decision_ref"],
        as_of=reviewed_at,
        price_basis_ref=snapshot["price_basis_ref"],
    )
    return dict(
        contract_version="oneil-auto-review-bundle-v1",
        assessment=assessment,
        report_text=text,
        review=review,
        setup_input=setup,
        policy_executed=False,
        live_ready=False,
        broker_execution=False,
        scope="REGISTERED_NUMERIC_SUBSET_NOT_COMPLETE_CANSLIM_OR_INDUSTRY_RANK",
    )
