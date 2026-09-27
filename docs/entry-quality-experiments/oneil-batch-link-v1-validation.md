# 배치 연결과 쌍대 성과 검증 결과

2026-09-27. 코드 연결 및 기능 검증과 실제 수익성 근거를 구분한다.

## 연결한 경로

- `prism_core/oneil_batch_setup.py`와 US prefetch/analysis/orchestrator:
  기존 종목 시세·실적 자료 재사용 → 별도 수치 검토 → Markdown/PDF hash 연결.
  켠 경우에만 보고서 단계에서 제한된 SPY 조회를 추가한다. LLM prompt에 새 입력을 넣지 않는다.
- `prism-us/us_stock_tracking_agent.py`, `observability/scenario_shadow.py`:
  기존 전략 진입이 확정된 뒤 정확한 결정/포지션 ID로 별도 적응형 계획을 동결한다.
  원래 scenario-shadow-v1 최초 계획과 BUY/SELL·브로커 흐름은 보존한다.
- `prism_core/oneil_paired_replay.py`, `tools/evaluate_oneil_paired_replay.py`:
  실제 적응형 평가기와 기존 StrategyLedger 회계를 재사용한다.
  임의의 간이 증액 공식이나 별도 회계를 만들지 않는다.

보고서 연결은 `ONEIL_AUTO_REVIEW_CAPTURE_ENABLED`, 최초 계획 기록은 기존
`SCENARIO_SHADOW_CAPTURE_ENABLED`도 필요하다. 기본 OFF이며 이번에 활성화하지 않았다.
이 연결만으로 장중 평가·새 게이트 수집·원장 ingestion이 자동 실행되지는 않는다.

## 소프트웨어 검증

- 입력·정책·원장·캡처·재생 관련425개 테스트 통과.
- 실제 US tracking 및 prefetch/analysis 경로49개 테스트 통과.
- KR/US 시세 형식·배치와 보고서 prefetch 관련94개 테스트 통과. 합계568개다.
- 실제 오전/오후 orchestrator의 OFF/ON, private 입력의 prompt 제외, PDF 변조,
  미래 자료, 다계정 포지션, 브로커 거절에서도 기존 전략 기록 보존을 검증했다.
- 원자적 no-clobber sidecar 저장의 실패·경합·기존 파일 보존을 검증했다.
- 신규 모듈 Ruff/compileall/diff check 통과. 기존 변경 파일의 lint49건은
  HEAD 기준선과 같고 새 위반0건이다. Pydantic 의존성의 기존 deprecated 경고가 남아 있다.
- KR/US 테스트를 한 프로세스로 섞으면 두 `cores` 패키지가 충돌해 수집에 실패하므로
  각 테스트 그룹을 별도 프로세스로 실행했다. 이 실패를 통과 건수에 포함하지 않았다.
- 별도 검토에서 발견한 중간 쓰기 실패 문제를 수정하고 재검토했다.
  제공된 LSP는 TypeScript용이므로 Python 타입 검증 완료라고 주장하지 않는다.
  원격 CI·배포 후 스모크는 수행하지 않았다.

## 실제 자료 조회

운영 서버의 clean commit `50f65846883afdafcccc9b9368b379d52247b91b`에서 공식
`build_entry_quality_evidence_packet.py`를 실행했다. 원본 JSONL은 서버에 두고
허용 필드만 있는 Packet을 받았다. 운영 DB·설정·cron·주문은 변경하지 않았다.

- 계약 `entry-quality-harness-v2`, schema3, US.
- 원본 Packet `5c4638ca5573215356c8cfe7`.
- as-of `2026-09-27T00:45:14.055487Z`, prospective 시작 `2026-09-01T14:52:42.118675Z`.
  이는 기존 CAPTURE 경계이며 새 적응형 정책의 holdout 경계가 아니다.
- 후보73건, 판단일17일, 정확히 연결된 전략 청산5건. 전략 청산5건은 모두 손실이다.
  브로커 REJECTED1건/SUBMITTED_ONLY4건도 유효한 전략 결과에서 제외하지 않았다.
  이 기록은 실제 계좌 실현손익을 증명하지 않는다.
- 후보 결정 ID 연결100%, capture100%, 미래정보 누수 제외0건.
  중복 event ID49건은 공식 생성기가 중복 제거했고 중복 후보 decision은0건이다.
  일봉·주봉 setup은 각73건 모두 MISSING이다.
- 전략 정책 버전·트리거가 다른5건을 하나의 새 정책 효과로 합치지 않았다.
  상승한 원장 사례가 없어 승자 이익 보존/최고 승자 제거 검증은 불가능하다.

원본 부족 코드:
`PROSPECTIVE_DATES_LT_20`, `PROSPECTIVE_CANDIDATES_LT_100`,
`CAPTURED_CANDIDATES_LT_100`, `STRATEGY_CLOSED_TRADES_LT_30`.

새 쌍대 재생 부족 코드:
`ADAPTIVE_PLAN_UNAVAILABLE`, `TICK_TAPE_UNAVAILABLE`,
`ORIGINAL_STOP_PATH_UNAVAILABLE`, `EXIT_TAPE_UNAVAILABLE`.
현재 자료로 과거 자동 검토·게이트·동시간 거래량을 채워 넣지 않았다.

파생 coverage Packet:
`d0a97d9c945cbe921d90c9cf23fbc2b2b33107cf885dbf36f06412bf37571499`.
`.omx/evidence/oneil-paired-coverage-20260927.json`에 보존했다.
실제 쌍대 평가0건, 결과 `INPUT_UNAVAILABLE`. 실제 순수익 개선 수치는 제시하지 않는다.

## 해석과 다음 단계

비교기는100% 기준선과10% 선진입 후 적응형 증액을 같은 최초 가격·청산·stop 경로,
편도10bp 및25bp 비용으로 비교한다. 최초50% 진입 전체 전략이나 새 trailing은 제외한다.
합성 경로는 산술·동작 시험일 뿐 수익성 증거가 아니다. 과거 원본 자료를 주더라도
EXPLORATORY_ONLY이며 원본 시점 인증 없는 prospective 입력은 받지 않는다.
같은 호가에 즉시 가상 체결하는 가정이므로 지연·스프레드·충격·부분체결은 별도 검증이 필요하다.

다음은 동결된 최초 계획과 실제 장중 quote/동시간 거래량/현재 gate/stop·청산 기록을
하나의 캠페인 원장에 연결하는 것이다. 이 입력과 실행 경계가 검증된 뒤 관측을 시작해야 한다.
최소30종료 쌍·20진입 날짜·기준선 승자10건, 비용 민감도와 승자90% 보존을 확인하고,
규칙 동결 후 별도90일 holdout을 평가한다. 이 최소값 자체가 수익성 증명은 아니다.

판정: **CONTINUE_CAPTURE**. 수익성 입증·SHADOW 운영 완료·LIVE 승격 아님.
배포, 운영 flag/cron 변경 및 첫 예정 배치 관측은 하지 않았다.
