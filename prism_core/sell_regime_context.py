"""LIVE market regime block for the KR AI sell prompt.

The deterministic exit loop (cores.oneil_fallback TIER2/TIER3) already trails on the
system regime every cycle. The AI sell decision must use the same regime and bands so
its stop-loss adjustment matches the rule that actually protects the position.
"""

from cores.oneil_fallback import BULL_REGIMES, TRAIL_DROP_BULL, TRAIL_DROP_WEAK

_KO_LABELS = {
    "parabolic": "폭주 강세장", "strong_bull": "강한 강세장", "moderate_bull": "보통 강세장",
    "sideways": "횡보장", "moderate_bear": "보통 약세장", "strong_bear": "강한 약세장",
}


def kr_live_regime_block(regime, context=None, language="ko"):
    """Return the prompt block; regime None means the system value is unavailable."""
    ko = language == "ko"
    if not regime:
        return ("### 현재 시장 국면 (시스템 계산 불가)\n"
                "시스템 국면을 계산하지 못했습니다. 0단계 기준으로 직접 판단하세요.\n" if ko else
                "### Current Market Regime (system value unavailable)\n"
                "The system regime could not be computed. Judge it yourself using Step 0.\n")
    bull = regime in BULL_REGIMES
    drop = TRAIL_DROP_BULL if bull else TRAIL_DROP_WEAK
    summary = (context or {}).get("index_summary") or {}
    facts = []
    if summary.get("kospi_current") is not None and summary.get("kospi_20d_ma") is not None:
        facts.append(f"KOSPI {summary['kospi_current']} / 20일선 {summary['kospi_20d_ma']}" if ko else
                     f"KOSPI {summary['kospi_current']} vs 20MA {summary['kospi_20d_ma']}")
    if summary.get("kospi_2w_change_pct") is not None:
        facts.append(f"2주 변화 {summary['kospi_2w_change_pct']}%" if ko else
                     f"2w change {summary['kospi_2w_change_pct']}%")
    facts = (" · ".join(facts) + "\n") if facts else ""
    if ko:
        mode = "A) 강세장 모드" if bull else "B) 약세장/횡보장 모드"
        return (f"### 현재 시장 국면 (시스템 실시간 계산 — 우선 적용)\n"
                f"{_KO_LABELS.get(regime, regime)}\n{facts}"
                f"0단계 시장 환경은 이 값을 그대로 따르고 {mode}를 적용하세요. 매수 시점 시나리오의 시장 상황보다 우선합니다.\n"
                f"트레일링 스탑은 진입 후 최고가 × {drop:.2f}로 계산하세요. 자동 청산 규칙과 같은 기준이므로 "
                f"이보다 좁게 잡지 마세요.\n")
    mode = "A) bull-market mode" if bull else "B) bear/sideways mode"
    return (f"### Current Market Regime (LIVE, system-computed — authoritative)\n"
            f"{regime}\n{facts}"
            f"Use this value for Step 0 and apply {mode}; it overrides the buy-time scenario.\n"
            f"Compute the trailing stop as highest price since entry × {drop:.2f}. This matches the automatic "
            f"exit rule, so do not set it tighter.\n")
