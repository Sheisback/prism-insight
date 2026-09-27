# 오닐 참고 적응형 목표 비중: 초기 검증

2026-09-27. 판정: CONTINUE_CAPTURE. 수익성·운영 승격 근거가 아닌 기능 검증이다.

## 구현

- `prism_core/oneil_adaptive_policy.py`: 별도 연구 평가기. 기존 v1을 수정하지 않는다.
- `tools/evaluate_oneil_adaptive_cases.py`: 명시적 합성 입력만 받는 무주문 CLI.
- `tests/test_oneil_adaptive_policy.py`, `tests/test_oneil_adaptive_ledger.py`:
  입력·시간·출처·조건·위험 제한과 기존 원장 산술/중복 처리를 검증한다.
- 계획은 해시로 동결한다. 시장/기존 허가/베이스/주도주/가격 범위/거래량을
  모두 확인한 뒤 가장 높은 목표를 선택한다. 손절값 자체는 변경하지 않는다.

## 실행한 고정 반례 (실제 종목이나 수익률이 아님)

P=100인 합성 입력이며, 각 행의 현재 위험 여유는 명시적으로 제공한 값이다.

| 입력 | 결과 |
| --- | --- |
| 신규 포지션, 적격 돌파 | 최초50% 목표 |
| 기존10%, 가격102·두 확정봉·거래량 조건 충족 | 한 번에80% 목표 |
| 기존10%, 가격104·두 확정봉·거래량 조건 충족 | 한 번에100% 목표 |
| 위100% 조건이지만 원래 보호값에서 위험 여유 부족 |75.80%로 하향 제한 |
| 가격106 |5% 추격 상한 초과로 보류 |
| 동시간 거래량 없음 |MISSING, 증액 보류 |
| 시장CORRECTION |확인된 조건 불충족, 증액 보류 |
| 같은 확정봉을 반복 제시 |재증액 보류 |
| 보호 stop 이탈 + 증액조건 |보호 필요를 우선 반환 |

75.80%는 특정 합성 원장/손절/비용 조합의 계산 결과이며 일반 권장 비중이 아니다.
이 평가기는 부정확한 손절을 인위적으로 높여100%를 맞추지 않는다. 갭 손실이나
전체 포트폴리오 위험까지 제한한다고 주장하지 않는다.

실제 기존 StrategyLedger에서10→80/100을 각각 단일 추가leg로 기록했고,
같은 event 재생 시 추가leg가 생기지 않음을 확인했다. 실제 broker fill은0건이다.
편도10bp/25bp 모두 원장 비용과 계산한 위험 상한이 일치함을 시험했다.
CLI 재실행 결과 및 입력hash도 동일하다. CLI는 DB·네트워크·스케줄러를 호출하지 않는다.

초기 기능·원장 연결·CLI 검증 64개, 관련 기존 회귀를 포함한325개 검사를 통과했다.
Ruff·compileall·diff check도 통과했다. 새 테스트를 CI 목록에 추가했으나 원격CI와
운영 배포는 하지 않았다. 이 숫자는 거래 표본 수가 아니라 소프트웨어 테스트 수다.
5% 매수 상한은 추가매수만 제한하며, 이미 오른 보유 종목을 자동 익절시키지 않는
반례도 포함한다. 기존 모듈에서 새 정책을 import하는 운영 호출자는 없다.

## 근거 자료의 한계

참조 Packet `d95c13c49ab668c8ba475810`, entry-quality-harness-v2/schema3,
as-of2026-09-27T00:45:14.055487Z, prospective 시작2026-09-01T14:52:42.118675Z.
73후보/17날짜/5종료 원장 기록이다. 기존 Packet capture100%·leakage0이지만,
setup quality는73건 모두 MISSING이다. schema에 새 가설용 적정베이스와 동시간
20거래일 누적량 입력이 없으므로 이 Packet으로 새로운 가설의 실제 성과를 계산하지 않았다.
DB 원문을 임의 조회하거나 현재 가격/시나리오로 결측을 메우지 않았다.

기존 부족 코드: PROSPECTIVE_DATES_LT_20, PROSPECTIVE_CANDIDATES_LT_100,
CAPTURED_CANDIDATES_LT_100, STRATEGY_CLOSED_TRADES_LT_30.
새 가설의 추가 결측: VERIFIED_BASE_UNAVAILABLE, MATCHED_INTRADAY_VOLUME_UNAVAILABLE,
FUNDAMENTAL_LEADER_PROVENANCE_UNAVAILABLE, HOLDOUT_NOT_STARTED.

setup와 캘린더 출처는 현재 호출자의 선언이다. 휴장일 포함 직전20거래일의 진위,
베이스 적정성, 펀더멘털 사실을 순수 함수가 외부 검증한 것은 아니다.
현재 watchlist의20일 고가와 선형 시간보정 RVOL을 대신 넣으면 안 된다.
첫 진입 기준E도 pivot~pivot+5% 범위에 한정했다. 그 밖의 진입 형태는 이 가설 범위가 아니다.

## 다음 작업

먼저 원본 시나리오/가격 자료에서 적정 베이스·주도주 근거와 동시간 누적 거래량을
생성·검증하는 입력 경계를 만든다. 그 뒤에 동일 후보·동일 stop/청산을 고정한
전진 SHADOW로 비중 결정만 비교한다. 트레일링 개선을 섞어 원인을 흐리지 않는다.
기존 캡처/원장/정책·운영flag·cron·BUY/SELL prompt·주문은 이번에 변경하지 않았다.
