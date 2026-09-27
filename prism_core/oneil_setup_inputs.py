"""Bind explicit setup reviews to their report, not an automatic stock classifier.

Source binding verifies bytes/identity/time only. Reviewer authorization and the
truth of financial/pattern judgments remain external responsibilities.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json


def text_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _time(value):
    if not isinstance(value, str):
        raise ValueError("aware clock required")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("aware clock required")
    return stamp.astimezone(timezone.utc)


def _ref(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reference required")


def _span(text, span):
    a, b = span["start"], span["end"]
    if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(text):
        raise ValueError("invalid span")
    content = text[a:b]
    if text_hash(content) != span["sha256"]:
        raise ValueError("span hash mismatch")
    return content


def build_setup_input(
    *, report_text, review, symbol, decision_ref, as_of, price_basis_ref
):
    """Return policy setup only for explicitly approved, source-bound reviews."""
    base = dict(
        contract_version="oneil-setup-input-v1",
        setup=None,
        verification_scope="SOURCE_BOUND_REVIEW_NOT_INDEPENDENT_FACT_VERIFICATION",
        reviewer_authority="CALLER_ATTESTED_NOT_AUTHENTICATED",
    )

    def result(status, reason, **extra):
        return dict(base, status=status, reason_codes=[reason], **extra)

    if (
        not isinstance(report_text, str)
        or not report_text
        or not isinstance(review, dict)
    ):
        return result("MISSING", "REPORT_OR_REVIEW_MISSING")
    try:
        _ref(symbol)
        _ref(decision_ref)
        _ref(price_basis_ref)
        now = _time(as_of)
        if (
            review["contract_version"] != "oneil-setup-review-v1"
            or review["market"] != "US"
            or review["symbol"] != symbol
            or review["decision_ref"] != decision_ref
            or review["price_basis_ref"] != price_basis_ref
        ):
            return result("INVALID", "REVIEW_IDENTITY_MISMATCH")
        report_digest = text_hash(report_text)
        if review["report_sha256"] != report_digest:
            return result("INVALID", "REPORT_HASH_MISMATCH")
        if review["reviewer_kind"] not in {"HUMAN_REVIEW", "VALIDATED_RULE_OUTPUT"}:
            return result("MISSING", "REVIEW_AUTHORITY_UNCONFIRMED")
        _ref(review["reviewer_ref"])
        _ref(review["criteria_ref"])
        reviewed = _time(review["reviewed_at"])
        if not _time(review["report_observed_at"]) <= reviewed <= now:
            return result("INVALID", "REVIEW_CLOCK_INVALID")
        if review["approved"] is not True:
            return result("MISSING", "REVIEW_NOT_APPROVED")
        for name in ("base", "leadership"):
            claim = review[name]
            _ref(claim["criteria_ref"])
            if _time(claim["data_as_of"]) > reviewed:
                return result("INVALID", "FUTURE_CLAIM")
            if claim["status"] not in {"CONFIRMED", "REJECTED", "MISSING"}:
                return result("INVALID", "UNKNOWN_CLAIM_STATUS")
            if claim["status"] == "MISSING":
                return result("MISSING", "CLAIM_MISSING")
            spans = claim["evidence_spans"]
            if not isinstance(spans, list) or not 1 <= len(spans) <= 20:
                return result("MISSING", "SOURCE_SPAN_MISSING")
            for span in spans:
                _span(report_text, span)
        if any(review[name]["status"] == "REJECTED" for name in ("base", "leadership")):
            return result("REJECTED", "REVIEW_REJECTED")
        pivot_span = review["base"]["pivot_span"]
        pivot_text = _span(report_text, pivot_span)
        if not any(
            s["start"] <= pivot_span["start"] < pivot_span["end"] <= s["end"]
            for s in review["base"]["evidence_spans"]
        ):
            return result("INVALID", "PIVOT_NOT_IN_BASE_EVIDENCE")
        pivot = Decimal(pivot_text.replace(",", ""))
        declared = review["base"]["pivot"]
        if (
            isinstance(declared, bool)
            or not pivot.is_finite()
            or pivot <= 0
            or pivot != Decimal(str(declared))
        ):
            return result("INVALID", "PIVOT_VALUE_MISMATCH")
        review_hash = text_hash(
            json.dumps(review, sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
        setup = dict(
            proper_base="VERIFIED",
            pivot=str(pivot),
            as_of=reviewed.isoformat(),
            source_ref="review:" + review_hash,
            price_basis_ref=price_basis_ref,
            fundamental_leader=True,
            fundamental_source_ref="review:" + review_hash,
            fundamental_as_of=reviewed.isoformat(),
        )
        # VERIFIED is an approved reviewer attestation, not machine proof of CAN SLIM.
        return result(
            "OK",
            "REVIEW_SOURCE_BOUND",
            setup=setup,
            report_sha256=report_digest,
            review_sha256=review_hash,
        )
    except KeyError:
        return result("MISSING", "REQUIRED_REVIEW_FIELD_MISSING")
    except (ValueError, TypeError, AttributeError, InvalidOperation, OverflowError):
        return result("INVALID", "MALFORMED_REVIEW")
