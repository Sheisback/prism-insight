"""KR buy evidence instructions; AST stubs avoid SDK, model, and network calls."""

import ast
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "cores/agents/trading_agents.py"


@pytest.fixture(params=["ko", "en"])
def prompt(request):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    from prism_core.sector_names import KR_SECTOR_NAMES
    namespace = {"Agent": SimpleNamespace, "buy_scenario_prompt_contract": lambda _: "", "KR_SECTOR_NAMES": KR_SECTOR_NAMES}
    exec(compile(tree, str(SOURCE), "exec"), namespace)
    return request.param, namespace["create_trading_scenario_agent"](request.param).instruction


def test_missingness_is_provenance_not_invented_gate(prompt):
    _, text = prompt
    for marker in ("NOT_IN_INPUT", "NOT_REQUESTED", "SOURCE_UNAVAILABLE", "INCOMPARABLE"):
        assert marker in text
    assert "UNKNOWN" in text
    assert "fundamental_check" in text and "rationale" in text


def test_whole_input_checked_before_one_material_supplement(prompt):
    language, text = prompt
    for marker in ("quarterly EPS", "annual EPS", "industry_leadership", "price_RS"):
        assert marker in text
    for marker in (("전체 보고서", "주입된 팩트", "이미 반환된 MCP", "통합 질의 최대 1회", "차트 이미지", "연결/별도")
                   if language == "ko" else
                   ("whole report", "injected facts", "already returned MCP", "at most ONE consolidated query", "chart images", "consolidated/separate")):
        assert marker in text


def test_time_alone_cannot_finalize_today_bar(prompt):
    language, text = prompt
    assert "BAR_FINALITY_UNKNOWN" in text
    assert "today's data is settled" not in text
    assert "당일 데이터가 확정됩니다" not in text
    assert ("시각만으로" if language == "ko" else "time alone") in text


def test_decision_rules_and_json_schema_are_byte_preserved(prompt):
    language, text = prompt
    heading = "## 도구 사용" if language == "ko" else "## Tool Usage"
    json_heading = "## JSON 응답 형식" if language == "ko" else "## JSON Response Format"
    # Refreshed stale hashes against 9203306b; volume changes preserve these bytes.
    expected = {
        "ko": ("15c3effd3918125fd241311df2398b7dfb0108320d5a79abcb349ad838c05cad", "01eb2c841a0495724b364e5a96418b1d67f1f38c4190729dce467d6308c57125"),
        "en": ("54fdf9cc49a0fd5a12bf38b09bc2c98a80916dadb18eda9359e12b6fcc987b9d", "b458120e713c714f2f29d308d1205d8fbd6ab56e6b7c8735d92e5d23ec93a771"),
    }
    assert hashlib.sha256(text.split(heading)[0].encode()).hexdigest() == expected[language][0]
    assert hashlib.sha256(text[text.index(json_heading):].encode()).hexdigest() == expected[language][1]


def test_sell_factory_matches_reviewed_volume_prompt():
    # Intentional ko/en volume interpretation and KR completed-session guidance update.
    source = SOURCE.read_text(encoding="utf-8")
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "create_sell_decision_agent")
    assert hashlib.sha256(ast.get_source_segment(source, node).encode()).hexdigest() == "70887f53a73aa2ced68f913e6dec8ca6df5bf707f61f4fd90dc2e21f62485544"
