# 장중 기록 연결과 비교 결과

검증일: 2026-09-27. 기준 `f1de1e9c` 이후 로컬 변경이며 운영 배포 보고가 아니다.

## 결과 요약

최초 계획→현재 입력 검증→불변 원장→기존 평가기/StrategyLedger 비교 경로를
격리 환경에서 연결했다. 정규 보유 점검·기계적 손절·추세 청산에도 기본OFF 관측
훅을 연결했다. **실제 최신 시세·현재 증액 게이트·동시간 거래량을 자동 공급하는
운영 경로는 아직 완성되지 않았다.** 현재 운영 숫자형 시세를 fresh quote로
위장하지 않으므로 이 훅의 장중 증액 입력은 MISSING이다.

수익성은 입증되지 않았다. 완전 입력을 제공한 합성 비교는 실행됐지만, 저장된
실제 거래 자료의 유효한 쌍대 비교는 여전히0건이다. 이 상태를 운영 SHADOW 준비
완료 또는 시간이 지나면 자동으로 검증 완료되는 상태로 부르지 않는다.

## 변경 파일과 재사용

- `prism_core/oneil_current_capture.py`: 시각·결정·포지션·가격 기준이 맞는 입력만
  기존 `assemble_evidence`에 전달한다. 게이트는 자신이 평가한 quote를 참조한다.
- `prism_core/oneil_capture_tape.py`: 전용 SQLite 최초본/tick/exit/gap 불변 기록,
  중복 재시도·경쟁·재시작 처리, 원본 손절 하향 금지, 기존 replay 입력 내보내기.
  최신 기록1건을 인덱스로 읽으며 캠페인1000개/tick10000개 한도를 둔다.
- `observability/oneil_capture.py`, `observability/scenario_shadow.py`, US tracking,
  `tools/hardstop_seller.py`, `tools/trend_exit_seller.py`: 기존 매매 뒤 관측만 추가한다.
  보호 루프의 청산은 메모리에 잠시 모았다가 전체 처리 후 별도 스레드에서 기록한다.
- `tools/run_oneil_capture_comparison.py`: 초기본/현재 입력/종료 import→내보내기→
  동일 조건 비교. `CURRENT`에 유효한 terminal이 있으면 증액보다 청산을 우선한다.
- `prism_core/oneil_paired_replay.py`: capture gap이 있으면 비교 불가로 처리한다.
  누락된 tick을 건너뛰고 정상 수익률을 계산하지 않는다.
- 관련 회귀 및 CI 목록을 추가했다. 새 정책·별도 회계·의존성은 추가하지 않았다.

## 사전 고정 합성 반례 결과

`oneil-capture-comparison-v1.md`에 사전 등록한8개 경로 중7개 계산, 게이트 결측1개는
비교 불가. 모두 가상의 동일 진입일이며 실제 거래 표본이 아니다.
아래 값은 전체 캠페인 예산 대비 수익률, 편도10bps 비용 차감 값이다.
비교군은 기존100% 대 **10% 선진입+적응형 증액**이다. 최초50% 전체 가설은 아니다.

| 합성 경로 | 전액 진입 | 적응형 | 의미 |
|---|---:|---:|---|
| 102에서 상승 확인, 120 청산 | +19.78% | +14.18% | 80% 직접 목표여도 상승 초반 참여가 적다 |
| 104에서 강한 확인, 120 청산 | +19.78% | +15.63% | 100%로 늘려도 더 비싸게 매입한다 |
| 원래 손절90 유지, 104 확인 | +19.78% | +11.96% | 위험 한도로 실제 목표75.80% 제한 |
| 증액 전89로 실패 | −11.19% | −1.12% | 소액 진입이 초기 실패 손실을 줄인다 |
| 104 증액 후99로 반락 | −1.20% | −4.62% | 확인 뒤 증액도 실패할 수 있다 |
| 거래량 미충족인데130까지 상승 | +29.77% | +2.98% | 증액 조건이 승자 참여를 크게 줄인다 |
| 106으로 추격 상한 초과 후130 | +29.77% | +2.98% | 급한 상승을 놓칠 수 있다 |

편도25bps에서도 위 반례의 방향은 같았다. 최고 기준선 승자1건을 제외해도 이
합성 묶음에서 적응형의 평균 차이는 음수다. 이는 고른 경로의 산술적 결과일 뿐
예상 수익률이나 전략 실패 확률 추정이 아니다. 이 결과에 맞춰 문턱을 튜닝하지 않았다.
신규 트레일링 규칙은 없으며, 손절100은 두 비교군에 공통으로 제공한 가정이다.

재현 자료(ignored local evidence):

- 입력: `.omx/evidence/oneil-capture-counterexamples-20260927-input.json`
- 결과: `.omx/evidence/oneil-capture-counterexamples-20260927-result.json`
- Packet: `b24e76a3425e9bbb446139aa9c8569a39992b5c9c8be48ee6fbda0af9d6ab40f`
- 동일 입력 재실행 결과와 Packet ID 일치 확인.

## 실제 자료의 비교 가능 범위

앞서 안전하게 생성한 canonical Packet `5c4638ca5573215356c8cfe7`을 재사용했다.
schema3 / `entry-quality-harness-v2`, as-of `2026-09-27T00:45:14.055487Z`.
이번에는 운영 원본을 새로 추출하지 않았다. 기존 CAPTURE 시작점은 새 정책 holdout이 아니다.
73후보·17진입날짜·5전략청산이며 broker 거절1/제출만4건도 전략 결과에서 제외하지 않는다.
당시 기록에서 capture/decision ID 연결100%, 중복 event49개는 canonical 도구가 제거,
중복 후보0·누수 제외0, daily/weekly setup은 각각73건 MISSING이다.

부족 사유 전체:

- `PROSPECTIVE_DATES_LT_20`
- `PROSPECTIVE_CANDIDATES_LT_100`
- `CAPTURED_CANDIDATES_LT_100`
- `STRATEGY_CLOSED_TRADES_LT_30`
- `ADAPTIVE_PLAN_UNAVAILABLE`
- `TICK_TAPE_UNAVAILABLE`
- `ORIGINAL_STOP_PATH_UNAVAILABLE`
- `EXIT_TAPE_UNAVAILABLE`

재실행 파생 Packet은 기존과 같은
`d0a97d9c945cbe921d90c9cf23fbc2b2b33107cf885dbf36f06412bf37571499`다.
결과는 INPUT_UNAVAILABLE, paired0. 서로 다른 trigger/policy의5손실을 합쳐 새
사이징의 성과라고 하지 않는다. 실제 체결 손익도 확인되지 않았다.

## 검증 및 남은 한계

- root 입력·평가기·원장·실제 보고서·보호 경로544개 통과.
- 별도 프로세스의 실제 US tracking/trend-exit59개 통과. 합계603개는 소프트웨어
  검사 수이며 거래 수가 아니다. broker→redis→gcp→tape 순서도 검증했다.
- 신규/연관 파일 Ruff 및 compileall, diff-check 통과. 기존 US lint29건은 기준선과
  같으며 새 위반0건이다. 제공 LSP는 tsc 기반이므로 Python 타입 검사로 주장하지 않는다.
- 원격 CI·배포·서버 스모크·첫 정규 실행은 하지 않았다. 기존 flag/cron/주문 불변.
- 기록은 **best-effort**다. DB 자체 잠금/장애, 프로세스 종료 전 flush 실패,
  메모리 큐1000건 초과는 로그만 남기고 기록이 누락될 수 있다. 같은 DB가 잠겼을 때
  gap도 저장할 수 없다. 따라서 gap0이 완전 수집을 뜻하지 않으며 forward 검증용이 아니다.
- 입력 hash는 무결성 검사이지 진실성 인증이 아니다. 수입한 자료는 인증되지 않은
  탐색 자료이며, 재생 결과는 이상적 관측가격 체결을 가정한다. 실제 호가·시장충격·
  부분체결·동시 포트폴리오 낙폭은 검증하지 않았다.

## 실행법과 다음 경계

```bash
python tools/run_oneil_capture_comparison.py --db /path/to/dedicated-research.sqlite \
  --input /path/to/import.json --output /path/to/new-result.json
```

import 계약은 `oneil-capture-import-v1`, 순서 있는 `operations` 배열이다.
`INITIAL`은 원본 `record`, `TICK`/`EXIT`는 `campaign_id`와 `record`, `CURRENT`는
`campaign_id`와 `capture_current_record` 인자 형태의 `inputs`를 받는다.
각 operation은 원자적이며 전체 import가 한 트랜잭션인 것은 아니다. 실패 후 같은
입력을 재시도하면 이미 저장된 동일 기록은 중복 집행하지 않는다. 입력 없이 실행하면
기존 전용 원장만 비교하고, 기존 결과 파일을 덮어쓰지 않는다.

남은 것은 단순 관측 기간이 아니라 **실제 quote 시각 보존, 현재 증액 gate의 원본
판정/가격 연결, 동시간 거래량 자동 공급, 수집 실패의 durable 재전송**이다.
이를 구현·격리 검증한 뒤 새 정책 관측 경계와 운영 활성화를 별도로 검토해야 한다.
판정: `CONTINUE_CAPTURE`.
