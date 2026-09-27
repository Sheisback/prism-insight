# 기계적 추가 진입 연구 v1: 검증 결과

2026-09-27. 판정 CONTINUE_CAPTURE. 운영 변경/배포/새 SHADOW 없음.

## 구현 범위

`prism_core/mechanical_split_research.py`는 최초 고정 계획의 순수 합성 재생기다.
10분마다 제공된 가격에서 단계별 문턱/상한, 수익 중 증액, 5일 계획 만료,
비용 포함 현금/종목 위험 상한, 상승만 하는 stop, 청산 우선을 계산한다.
`tests/test_mechanical_split_research.py`에 14개 회귀를 추가했다.
기존 관련 테스트를 포함해 196 passed in 0.95s. compileall/diff check 통과.
별도 lint/typecheck는 미실행이다.

가격 tick의 동일 입력 중복 제거를 검증했지만 주문 중복 방지 검증은 아니다.
실제 broker pending/partial/rejected, crash/restart, 두 worker 동시 실행,
LLM JSON 유효성/변경/취소, 포트폴리오 위험은 구현 및 검증하지 않았다.
Tick은 정규장/연속성이 이미 검사됐다는 입력 계약이며 이를 검사하는 역사자료
어댑터는 아직 없다. 반환값은 historical_validation=False다.

## 합성 결과

E=100, S=90, 첫10%, 슬롯1. 편도10bp, 표의 수치는 슬롯 대비 순손익이다.
실제 거래나 최적화 결과가 아니라 고정한 반례 테스트다.

| 가상 경로 | A 최초100% | B 기계적 증액 | C 증액+트레일링 |
| --- | ---: | ---: | ---: |
| 95→89, 원래90 청산 | -10.180% | -1.118% | -1.118% |
| 105→110→120→130, 130 청산 | +29.740% | +12.708% | +15.515% |
| 105→110→120→130→115, 원래100 청산 | -0.200% | -3.639% | +3.028% |
| 105→110→120→109→130, 130 청산 | +29.740% | +12.708% | -1.967% |
| 150→160, 160 청산 | +59.680% | +5.968% | +5.968% |

편도25bp에서도 각 사례의 우열 방향은 같았다.
급반등 반례에서는 trailing 조기 청산이 손실을 확정한다.
급등이 한 번의 관측 사이에 발생하면 상한가/단계 순서 제한 때문에 기회를 놓친다.
고정 손절 B는 증액 후 위험 한도 때문에100%까지 못 늘어나는 경우가 있고,
C는 stop 상승으로 추가 위험 여유가 생긴다. 이를 수익 보장으로 해석하지 않는다.
관측 tick 기준 MAE는 봉 내 저가/전체 포트폴리오 MDD가 아니다.

## 실제 자료 상태

기존 US Packet c5a78c224a47011e8d5a3d61 (entry-quality-harness-v2/schema3),
as-of2026-09-27T00:45:14Z, prospective2026-09-01T14:52:42.118675Z:
73후보/17날짜, 종료 거래5개, 유효 초기R 5개, 확정 broker fill0.
capture100%, leakage0은 원 Packet 기준이며 신규 재생 입력의 검증을 대체하지 않는다.
부족: PROSPECTIVE_DATES_LT_20, PROSPECTIVE_CANDIDATES_LT_100,
CAPTURED_CANDIDATES_LT_100, STRATEGY_CLOSED_TRADES_LT_30.

공식 기존 collector의 bounded fetch로 DGX 2026-09-22T00:00:00Z~24T00:00:00Z
5분봉187개를 받았다. retrieved_at2026-09-27T06:21:57.698172Z,
exchange NYQ, yfinance1.4.1, auto_adjust=False, back_adjust=False, repair=False,
actions=True, prepost=True. 이187개는 정규장 연속성 검증 전 원시 가격 개수다.
자료를 받을 수 있다는 연결 확인이며 frozen manifest나 실제 재생 결과는 아니다.
이전 날짜만 전달한 시도는 timezone 누락 오류였고 UTC 명시로 수정했다.

원 Packet은 원래 청산시각/수익률은 있지만 원본 청산가격을 내보내지 않는다.
청산가격을 수익률에서 역산하지 않았다. 실제 성과 재생 건수는0이며
MISSING_ORIGINAL_EXIT_PRICE, UNVERIFIED_PRICE_PATH, MISSING_FROZEN_MANIFEST다.
DGX 외4건의 가격 수집/검증도 아직 하지 않았다. 실제 성과 검증 완료로 표시하지 않는다.

## 다음 최소 작업

공식 Evidence Packet의 허용목록에 정확히 연결된 원본 청산가격 provenance를
추가하고 회귀 검증한 뒤,5개 거래의 가격 자료를 동결하여 캘린더/기업행사/연속성
어댑터로 재생한다. 손실5건뿐인 탐색 표본으로 우월성/승격을 결정할 수는 없다.
그 뒤에야 신규 시나리오 JSON과 공통 실행원장의 pending/confirmed 상태를 연결하는
설계를 검토한다. 현재 계산기를 live loop에 연결해서는 안 된다.
