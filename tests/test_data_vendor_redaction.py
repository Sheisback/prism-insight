from prism_core.data_vendor_redaction import redact_data_vendor_names
from prism_core.decision_input_features import prompt_contract
from prism_core.report_presentation import humanize_report_status, report_narrative_contract


def test_hold_message_rationale_drops_vendor_but_keeps_facts():
    text = ("미국 태양광 정책은 직접 수혜 기대이며 WiseFn 비교군은 업종 전체가 아닙니다. "
            "동종업계 밸류에이션(WiseFn 선정 비교군 3개, 재무 2025/12): PER 중앙값 12.3")
    out = redact_data_vendor_names(text)
    assert "WiseFn" not in out
    assert "증권정보 제공사 비교군은 업종 전체가 아닙니다" in out
    assert "(선정 비교군 3개, 재무 2025/12): PER 중앙값 12.3" in out


def test_report_prose_links_and_urls_are_removed():
    text = ("EPS는 WiseReport 제공자 기준입니다. 자세한 수치는 WiseReport 기업현황 페이지"
            "(https://comp.wisereport.co.kr/company/c1010001.aspx?cmp_cd=009830)를 참고하십시오. "
            "[기업개요](https://comp.wisereport.co.kr/company/c1020001.aspx?cmp_cd=009830) · 와이즈리포트 · "
            "DART https://dart.fss.or.kr/x")
    out = redact_data_vendor_names(text)
    for token in ("WiseReport", "wisereport", "와이즈리포트"):
        assert token not in out
    assert "증권정보 제공사 기업현황 페이지를 참고하십시오" in out
    assert "기업개요 ·" in out and "https://dart.fss.or.kr/x" in out


def test_english_and_selected_forms():
    out = redact_data_vendor_names("Peer valuation (WiseFn-selected 3 peers); source WiseReport.", "en")
    assert out == "Peer valuation (selected 3 peers); source a market data provider."


def test_publication_boundary_and_prompts():
    report = "#### 경쟁사 비교 분석\n출처: WiseReport 경쟁사분석(WiseFn 선정 비교기업) · 가격 BAR_FINALITY_UNKNOWN\n"
    out = humanize_report_status(report, "ko")
    assert "Wise" not in out and "마감 확정 여부를 확인하지 못한" in out
    for text in (prompt_contract("ko"), prompt_contract("en"), report_narrative_contract("ko"),
                 report_narrative_contract("en")):
        assert "WiseFn" not in text and "WiseReport" not in text


def test_empty_and_unrelated_text_unchanged():
    assert redact_data_vendor_names("") == ""
    assert redact_data_vendor_names(None) is None
    text = "현재가: 33,400원\n결정: 미진입 https://example.com/wise"
    assert redact_data_vendor_names(text) == text
    assert redact_data_vendor_names("a wise choice; WISE 산업분류상 기계 업종") == "a wise choice; 증권정보 제공사 산업분류상 기계 업종"
