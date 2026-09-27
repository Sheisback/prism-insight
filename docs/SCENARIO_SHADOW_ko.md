# 시나리오 SHADOW 실행 코어

## 현재 완료 범위

US 전용 `scenario-shadow-v1`의 계획 생성/제한적 개정/기계·정규 공통 평가,
별도 SQLite 원장의 원자적 CAS, 입력 파일 실행기를 구현했다.
기존50%파일럿,초기10%projection,운영 BUY/SELL/주문/환경/cron은 변경하지 않았다.
LIVE mode/주문 adapter는 없으며 **실시간 prospective 수집은 아직 연결하지 않았다**.
이 코어를 배포된 정교한 SHADOW나 LIVE 전환 준비 완료로 표현하지 않는다.

구현 계약과 경제적 차이는
`entry-quality-experiments/scenario-shadow-v1.md`에 있다.
비중은 평가액이 아니라 누적투입원금 기준 논리 슬롯이다. 새 cohort이며 과거
mechanical 연구 결과와 소급 합산하지 않는다. 입력의 출처·정규장·게이트는 현재
caller attestation이지 생산자 인증이나 시세의 외부 검증 완료가 아니다.

## 실행

```bash
python3 tools/run_scenario_shadow.py \
  --input /absolute/path/sanitized-scenario-inputs.jsonl \
  --ledger /absolute/path/dedicated-scenario-shadow.sqlite \
  --book-id scenario-shadow-us-v1
```

입력 경로와 출력 DB는 분리한다. 운영 stock_tracking DB/unknown schema DB는
거부한다. 도구는 브로커·LLM·메시지·cron을 호출하지 않는다.
새 DB 파일은 기존 StrategyLedger 형식이며 raw계좌/토큰/원문보고서를 입력하지 않는다.
입력은 시간순 JSONL이다. 한 줄의 실패는 그 줄의 state/leg/event를 모두 rollback하며,
이전 성공 줄은 유지한다. 같은 입력을 다시 실행하면 성공한 event는 no-op이다.
같은event ID의 다른payload는 충돌이다. 오류 후 ID를 바꿔 임의 재주문하지 않는다.

### 최초 계획

```json
{"operation":"open","event_id":"fixture-open-1","campaign_id":"fixture-campaign-1","symbol":"TEST","plan_inputs":{"entry_price":"100","initial_stop":"90","entry_at":"2026-09-28T14:00:00Z","source_decision_ref":"fixture-decision-1","entry_eligible":true}}
```

이는 합성 예시이며 실제 적격 판정을 대체하지 않는다. `source=regular`만 최초계획을
만들 수 있다. 최초R·정책비중·비용·위험한도·만료를 고정하고 hash를 남긴다.
현재 가격 문턱은 결정론적 연구 profile에서 도출한다. LLM이 자유롭게 지정한
추가매수 시나리오를 해석하는 모델 adapter는 아직 없다.

### 정규/기계 공통 평가

```json
{"operation":"tick","event_id":"fixture-tick-1","campaign_id":"fixture-campaign-1","expected_revision":0,"occurred_at":"2026-09-28T14:10:00Z","evidence":{"source":"mechanical","quote":{"price":"105","observed_at":"2026-09-28T14:10:00Z"},"bar":{"open_at":"2026-09-28T14:05:00Z","close_at":"2026-09-28T14:10:00Z","observed_at":"2026-09-28T14:10:00Z","completed":true,"close":"105"},"session":{"open_at":"2026-09-28T13:30:00Z","close_at":"2026-09-28T20:00:00Z","verified":true,"source_ref":"fixture-calendar"},"gates":{"risk":true,"regime":true,"sector":true,"slot":true,"observed_at":"2026-09-28T14:10:00Z","source_ref":"fixture-gates"}}}
```

quote/게이트120초,확정5분봉 종료10분 이내. 위 source를regular로 바꿔도 같은 정책을
사용한다. 보호 stop은 새 추가매수 게이트·계획 만료·취소보다 우선한다.
가상 체결은 입력 quote에서 즉시 이뤄지는 모델이며 pending/partial brokerfill이 아니다.
같은 봉으로 여러 단계 증액 불가. 다른 source가 같은 revision으로 경쟁하면 하나만
반영된다. WAIT도 유효quote로 평가액을 갱신하고 book 시간순서를 검사한다.

### 정규 배치 개정

```json
{"operation":"revise","event_id":"fixture-revise-1","campaign_id":"fixture-campaign-1","expected_revision":1,"occurred_at":"2026-09-28T14:20:00Z","changes":{"source":"regular","expected_version":0,"cancel":true}}
```

`expected_revision`은 원장 state CAS이며 tick도 증가시킨다.
`expected_version`은 plan revision이며 계획 개정만 증가시킨다.
기계source의 개정·취소,손절하향,만료연장,기집행 단계 변경,위험한도 확대는 거절한다.
취소·만료는 추가투입 권한에도 반영되어 실행투영에 잘못된 잔여비중을 보이지 않는다.
시나리오 고점/stop/state와 실제계좌 체결 overlay는 별개다.

## 리뷰와 검증

소유권 없는 기존 target/sell로 새cohort를 우회할 수 없도록 차단했다.
기존 sell 산술을 transaction 내부 공통 함수로 추출해 정책 state와 청산leg를
동시에 commit한다. 별도의 중복 회계 엔진/신규 DB schema를 만들지 않았다.
초기10%에도 비용 포함 위험 검사를 적용한다.

통합회귀: 정규/기계 경쟁,CAS충돌 rollback,재시작·동일event replay,
직접target/sell우회차단,취소후손절,청산후재진입금지,WAITmark,
만료투영,복수캠페인 역순book clock을 포함한다.

검증: 관련 439개 테스트, Ruff 및 compileall을 통과했다. CLI를 별도 프로세스로
실행하는 격리 smoke도 포함한다. 초기에는 `mcp_agent` 부재로 일부 통합 검사를
실행하지 못했으나, 아래 패키지 확정 단계에서 별도 환경으로 해소했다.
실시간 생산자와 브로커 adapter의 통합까지 검증한 것은 아니다.

### 코드 패키지 확정 (2026-09-27)

- PR #803을 포함한 `c9b3b1e2` 기준의 별도 브랜치로 옮겼다. 다른 작업의 변경과
  이번 변경 파일은 직접 겹치지 않았으며 기존 운영 코드를 되돌리지 않았다.
- 연구 도구·사전등록·과거 재생 결과는 `fc27c906` 커밋으로 분리했다.
  SHADOW 코어·원장 연결·실행기는 후속 커밋으로 구분한다.
- 프로젝트 requirements와 기존 테스트 도구만 설치한 임시 Python 3.11.15 환경에서
  관련 439개 검사를 다시 통과했다. 원장 연동·격리 런타임·메시지·전달 안전성의
  별도 123개 검사도 통과했다. 두 묶음에는 중복 테스트가 있으므로 합계를 고유 개수로
  해석하지 않는다. 의존성의 Pydantic 사용 중단 예고 경고 3개가 있으나 실패는 없다.
- 새 연구/SHADOW 회귀와 기존 원장 연동 검사를 CI workflow에 추가했다.
  원격 CI 실행, 운영 배포, 첫 예정 실행은 아직 하지 않았다.
- 저장된 공급자 응답으로 두 번 재생한 연구 ID는
  `a759262e25e70416eb3079011081aeeb01bd96e8e809631b7bd60b64a2c36f39`로 일치했다.
  이전 기록과 비교해 공급자 입력과 모든 거래 결과도 동일했다. 코드 형식 수정으로
  구현 해시가 달라졌으므로 이전 연구 artifact는 덮어쓰지 않았다.

다음 작업은 최초 시나리오 캡처 연결이다. 이번 패키지 확정 단계에서는 새로운
스케줄러, 운영 관측 시작점, LIVE 전환 기능을 추가하지 않았다.

## 과거 DB 인벤토리 (2026-09-27 읽기 전용)

공식 새 aggregate 전용 도구 `build_history_scenario_inventory.py`를 source DB에
mode=ro/query_only/컬럼 authorizer로 실행했다. 원문 시나리오·계좌·종목ID는 출력하지 않았다.

| 시장 | 청산기록 | 기록상 수익/손실/0 | JSON객체/숫자양수stop | 청산기간UTC |
| --- | ---: | --- | --- | --- |
| US | 127 | 42 / 85 / 0 | 127 / 127 | 2026-01-30~09-23 |
| KR | 210 | 85 / 123 / 2 | 210 / 210 | 2025-10-14~2026-09-16 |

contract history-scenario-inventory-v1. 현재DB 전체기록,기존frozen cutoff 도구와 별개다.
naive시각은현재서버/쓰기코드에근거해Asia/Seoul로명시해석했다. 과거 import/서버TZ
변경까지입증하지못했다. 각건수는중복없는전략거래수/브로커실현손익이아니다.
stored scenario는수정됐을수있으므로초기stop원본의증거로단정하지않는다.
season1별도DB는이번집계에합치지않았다.

## LIVE 검토까지 남은 연결

1. 기존적격 신규진입에서 최초계획을 freeze하는 명시적 prospective capture.
   기존 BUY/SELL prompt/score/주문 동일성 통합시험 필요.
2. 실제 캘린더·동일가격기준·stale/error처리와 신뢰되는 게이트 입력을 공급하는
   읽기전용10분/정규배치 adapter 및 첫 예정 실행 확인.
3. 최초baseline과 SHADOW를 같은캠페인으로 정확히 연결해 승자/실패/증액/trailing
   통계를 내는Evidence Packet. 독립holdout은그이후시작한다.
4. 정수주·예약금·부분체결·재시작·양배치중복·보호수량을 검증하는 execution adapter.
5. 사전등록성과기준/운영안전검토와사용자승인. 승인후에도제한LIVE가첫단계다.

같은평가기와원장을재사용하되, LIVE를 단순flag전환이라고약속하지않는다.
