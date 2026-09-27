# 기계적 초분할: 실제 가격 경로 재생 진단

2026-09-27. 이전 validation 문서의 실제 재생0건 상태를 갱신한다.
판정: CONTINUE_CAPTURE. 운영 소스/cron/설정/주문 변경 없이 서버 외부 임시 연구
디렉터리에서 실행했다. 배포/CI/첫 정규 배치 검증을 수행한 작업이 아니다.

## 증거

- Packet `d95c13c49ab668c8ba475810`, entry-quality-harness-v2/schema3.
  정확히 연결된 exit.executed의 sell_price를 허용목록으로 추가했다.
  원본 exit event의 해시 참조를 보존하고 수익률 역산/체결 추정을 하지 않았다.
- Study `c0663cf89e9d6c67f40a8652a3cbcec074048db0c4cd89e5181f1d0cb25e2af1`.
  저장한 동일 공급자 응답으로 두 번 실행해 study_id가 일치했다.
- 입력: US 전략 종료5건, 2026-09-01~23 진입/청산,7개 요청 구간,
  정규장/시간외를 포함한 원시5분봉2700개. 캘린더로 정규장만 골라 검사했다.
- 서버 artifacts: `/tmp/prism-mechanical-verify-5WkzR3If/`.
  로컬 sanitized Packet/공개가격 artifact: `.omx/evidence/mechanical-entry-20260927.json`,
  `.omx/evidence/mechanical-study-frozen-20260927.json`.
- 원래 prospective boundary 2026-09-01T14:52:42.118675Z,
  as-of2026-09-27T00:45:14Z,73후보/17날짜. 이미 본 자료이므로 holdout이 아니다.

## 데이터 품질과 범위

원 Packet의 capture100%, leakage0, 확정 broker fill0. 종료 원장5건은 broker 상태와
무관하게 입력 후보로 유지했다. 정확히 연결된 가격에 대해 양수/유한값 검증,
시장 캘린더,5분봉 누락/충돌,OHLC 순서,진입 봉 가격 대조,기업행사 값 검사를 수행했다.
공급자가 반환한 기업행사 필드 검사이지 모든 가격조정 방식의 독립 감사는 아니다.

NVDA/SNDK는 `ENTRY_PRICE_BASIS_UNVERIFIED`로 제외했다.
NVDA 원장226.529 vs 해당5분봉 고가226.4700,
SNDK 원장1794.175 vs 고가1792.9056이다. 차이는 작지만 quote 시각/공급자 차이인지
확정하지 못해 허용오차를 사후 완화하지 않았다. 원장 오류라고 판정한 것이 아니다.

실제 신규 단일진입/파일럿 여부와 최초 LLM 시나리오 변경 이력의 전체 확인은
미완료다. 따라서 아래 결과는 세 가격 경로의 **조건부 반사실 진단**이지
전체 운영 전략의 검증 완료가 아니다. 프로그램도 historical_validation=False를 유지한다.

## 결과: 편도10bp, 정상화 슬롯 대비 순손익

| 종목·트리거·정책 | 레짐 | 최초100% 기준선 | 기계적 증액 | 증액+trailing | 증액 |
| --- | --- | ---: | ---: | ---: | --- |
| SPGI / Closing Strength Top / 736b3397365e | moderate_bull | -6.0742% | -0.5676% | -0.5676% | 0회 |
| HPE / Gap Up Momentum Top / 5e24fdfce381 | moderate_bull | -7.6922% | -0.7692% | -0.7692% | 0회 |
| DGX / Closing Strength Top / 264fd487fb6f | moderate_bull | -1.7384% | -0.1738% | -0.1738% | 0회 |

SPGI는 가상 기계 청산이 원래 청산보다 먼저 발생했다. HPE/DGX는 원래 청산을 사용했다.
편도25bp에서도 모든 거래의 기계적 증액과 trailing 결과가 같았다.
실제 체결/계좌 수익률이 아니며 서로 다른 정책/트리거를 합쳐 평균 우월성을 주장하지 않는다.

세 거래 모두 초기10%만 유지했고 trailing stop도 올라가지 않았다.
따라서 손실 노출이 작아진 것은 대부분 비중 축소 효과이며, **추가 진입 또는 trailing의
수익 개선 효과는 관측되지 않았다.** 승자0건이므로 승자 포착/최고승자 제거는 산출 불가다.
틱 기준 MAE만 계산했으며 전체 봉 내 MAE·포트폴리오MDD·현금 재배분을 검증하지 않았다.

부족 코드: PROSPECTIVE_DATES_LT_20, PROSPECTIVE_CANDIDATES_LT_100,
CAPTURED_CANDIDATES_LT_100, STRATEGY_CLOSED_TRADES_LT_30,
ENTRY_PRICE_BASIS_UNVERIFIED(2건), WINNERS_LT_10, NO_ADD_TRANSITIONS,
NO_TRAILING_ACTIVATIONS, SCENARIO_HISTORY_UNVERIFIED.

## 구현 및 검증 범위

- 공식 Packet 생성기의 exit_price_evidence 확장, 같은 position/시장/종목/시간 연결
  유지 및 결측/부적절 이벤트/잘못된 가격의 회귀 테스트를 추가했다.
- 새 오프라인 runner는 기존 bounded 공급자 fetch를 재사용하고 원본 응답 hash,
  Packet hash, 코드 hash,calendar version을 결과와 함께 보존한다.
- 기존 런타임에 import/호출자를 추가하지 않았다. LLM JSON/동시 주문/부분체결/복구는
  여전히 미검증이다. 새 runner의 공개 가격 수집은 데이터 비용/네트워크만 발생시키며
  broker·DB·메시지는 호출하지 않는다.

다음에 필요한 증거는 상승 후 증액 문턱에 도달한 정확히 연결된 거래 및 시나리오 이력이다.
현재 손실 사례만으로 조건을 낮추거나 운영 승격을 결정하지 않는다.
