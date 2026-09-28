# 최초 최대50% 전환형 SHADOW 운영 절차

## 범위

사용자 요청: LIVE 전환 가능한 SHADOW 완성 후 운영 배포. 이번 활성화는 **SHADOW만**이다.
최초 최대50%, 이후 조건부80/100%이며 기존10% 연구군과 섞지 않는다.
기술적 실행 준비와 수익성/전진 검증/LIVE 승인을 구분한다.

## 구현

- `oneil_execution.py`: 모드 고정, 계좌·종목 소유권, 원자적 IntentStore 예약,
  실제 확인 수량/금액, 부분체결·UNKNOWN·취소·재시작·청산 상태.
- `oneil_broker.py`: 실제 KIS 전체 페이지 조회와 정확한 주문번호/거래일/종목/방향
  대사, 기존 ExecutionService의 사전 예약 주문. 수수료 미제공은 실제 비용 UNKNOWN.
- `oneil_dispatcher.py`: SHADOW/LIVE 공통 실행. UNKNOWN을 재전송하지 않고,
  미결 BUY가 있어도 확인 보유분을 보호한다. 오래된 SELL은 취소 확정 뒤에만 재가격한다.
- `oneil_routing.py`: US 최초 주문을 기존 전액 BUY 이전에 가로채고, 정확한 전략 행을
  한 번만 연결한다. 같은 캠페인의 legacy 추가/전량 SELL을 차단한다.
- `oneil_service.py`: 청산/보호 우선, 느린 증액 자료 수집 중에도 보호 점검,
  worker 손절 후 전략 이력 정리 및 중단 복구. SHADOW는 기존 보유DB를 수정하지 않는다.
- `oneil_config.py`: OFF/SHADOW/LIVE, 명시적 계좌 범위, 구현 해시와 유효기간에 묶인
  LIVE 승인. 설정 변경은 CAS·백업·원자적 교체를 사용한다.

이전의 영구 `NOT_IMPLEMENTED` 차단은 제거했다. 일반 환경변수만으로 LIVE를
활성화할 수 없으며, 승인 없는 LIVE 매수는 실제 전송 전에 거부한다.

## SHADOW 활성화

검증된 commit을 CI 통과 후 병합하고 clean db-server에서 ff-only 배포한다.
서버의 실제 경로와 계정 설정을 확인한 뒤 다음 도구를 사용한다. 자격증명은 고치지 않는다.

```bash
python tools/configure_oneil_execution.py --mode SHADOW --from-configured-us
python tools/run_oneil_execution.py --check
python tools/run_oneil_execution.py --once
```

설정은 `runtime/oneil-execution.json`, 상태는 `runtime/oneil-health.json`이다.
처음에는 설정 파일이 없어 OFF이며, 도구가 기존 US 활성 계좌 이름을 읽어 SHADOW 범위를
명시한다. 출력에는 계좌번호/자격증명이 아닌 개수와 해시만 남긴다.
설정은 최초 캡처·보고서 setup sidecar·원래 청산 tape도 활성화한다. `.env` 변경은 없다.

`deploy/systemd/prism-oneil-shadow.service`는 독립 worker를 실행한다. 기본60초 간격이며
정규장 밖에는 자료 조회·주문 없이 상태를 남긴다. 정규 매매/기존 보호 cron은 유지한다.
첫 서비스 주기가 장외였다면 첫 장중 평가 또는 첫 거래를 완료했다고 부르지 않는다.

## 향후 LIVE 전환

먼저 성과/운영 증거와 사용자 승인을 확인한다. 임의 자동 승격은 금지한다.
승인 JSON은 다음 필드를 가진 별도의 운용자 기록이다.

- `policy=oneil-adaptive-v2`, `initial_arm=INITIAL_POLICY_50` (v2 이후 v1 승인은 LIVE에 쓸 수 없다)
- `scope=NEW_CAMPAIGNS_ONLY`, 정확한 `accounts` 이름 목록
- `approved_by`, 시간대가 있는 `approved_at`/`expires_at`
- `max_unit_budget_usd`: 기존 계좌 예산을 높이지 않는 승인 상한
- `implementation_hash`: 현재 정책/입력/주문/보호 코드 해시
- `approval_hash`: approval_hash를 제외한 canonical JSON SHA256

코드가 바뀌면 이전 승인은 무효다. 승인 파일은 이 도구가 자동 생성하지 않는다.
승인 후 다음과 같이 기존 설정의 정확한 SHA256으로 전환한다.

```bash
python tools/configure_oneil_execution.py --mode LIVE \
  --approval-file /secure/path/approved-oneil.json \
  --expected-hash <현재설정파일-SHA256> --confirm-new-campaigns
```

전환 도구는 기존 US 신규 진입 배치가 실행 중이면 거부한다. SHADOW 캠페인을
실보유로 채택하지 않고, 새 캠페인만 LIVE로 시작한다. 기존 보유 또는 같은 종목의
미결 주문이 있으면 신규 LIVE 소유권을 얻지 못한다. 기존 사용자 예산을 바꾸지 않는다.

## 중단 및 불확실한 주문

```bash
python tools/configure_oneil_execution.py --mode OFF --expected-hash <현재설정파일-SHA256>
```

OFF/승인 만료는 새 매수·증액만 중단한다. 기존 LIVE 소유 포지션은 같은 worker가
대사·보호한다. 실제 포지션이 남아 있으면 worker 서비스를 함부로 중지하지 않는다.
주문 UNKNOWN은 “실패”가 아니다. 브로커 확인 전 예약을 해제하거나 재전송하지 않는다.
미결 BUY/SELL·계좌 불일치가 해결되지 않으면 수동 운용 확인이 필요하다.

## 검증 기록

배포 전 핵심 경로303개, IntentStore/ExecutionService·기존 계좌96개,
실제 US 매매/주문 예산/보호 경로150개, 합계549개 검사를 통과했다.
Ruff 새 위반0, 컴파일·diff 검사를 통과했다.
앞서 지적된 배포 차단 결함에 대한 제한 재검토도 통과했다.
실주문을 시험용으로 제출하지 않았다. 상세 CI/배포 commit/서버 스모크/첫 주기 결과는
배포 완료 후 이 절에 추가한다.

취소 상태는 확인된 상세 취소 행과 미체결 부재가 일치할 때만 확정한다. 실제 응답의
미지원 상태는 UNKNOWN으로 남긴다. 비용 모형은 위험 계산용이며 실제 수수료/PnL 증명이 아니다.

## v2 (2026-09-28)

사용자 승인에 따라 B3 규칙을 새 정책 버전 `oneil-adaptive-v2`(증거 계약
`oneil-adaptive-evidence-v2`)로 추가했다. **SHADOW 전용**이며 LIVE 승인은 없다.

규칙 변경:

1. 거래량 확인(누적 거래량 ≥ 20일 매칭 평균 1.5배) 게이트를 최초 진입과 증액 모두에서
   제거했다. 거래량 증거는 기록(evidence hash)될 뿐 차단·필수 조건이 아니다.
2. 최초 비중 = clip(0.5 × 0.07 / stop_proxy, 0.30, 0.80),
   stop_proxy = clip(1.5 × ATR14 / entry_reference, 0.04, 0.10).
   ATR14는 계획 생성일(뉴욕 날짜) 이전에 완료된 14거래일의 평균 true range이며,
   보고서 setup 리뷰가 계산해 `atr14`/`atr14_source_ref`/`atr14_as_of`/`atr14_last_trade_date`로
   계획 해시에 고정한다. ATR이 없거나 무효이거나 당일 관측이 아니면 계획 생성이 실패하며
   기본 비중으로 대체하지 않는다.
3. 증액 사다리는 pivot이 아니라 entry_reference 기준이다. 최근 완료 5분봉 종가와 현재가가
   모두 +2% 이상이면 0.8, +4% 이상이면 1.0. 증액은 entry×1.10 이하에서만 허용한다.
   최초 비중이 이미 0.8 이상이면 1.0 단계만 적용한다. 최초 진입은 기존 pivot~pivot×1.05
   매수 구간을 유지한다. 두 개 완료봉 pivot 상회, 수익 중 조건, 같은 봉 반복 금지,
   봉당 1단계, 시장 게이트, 보호 손절 우선, 초기 손절 기준 위험 한도 clip은 그대로다.
4. 최초 진입 이후 증액에는 추세 증거가 필요하다. 최근 완료 일봉 종가 > 직전 완료
   20거래일 종가 단순평균(MA20, 당일 제외). 같은 5분봉 입력 생성기가 각 완료 세션의
   마지막 정규 5분봉 종가로 계산하며, 없으면 `MISSING_TREND_EVIDENCE`로 증액하지 않는다.
   최초 진입에는 필요 없다.
5. 계획 만료는 10거래일이다. 계획에 거래소 달력이 없으므로 14 달력일로 계산한다.

재생 근거(사전 등록, 7년 일봉 근사, 개발+검증 구간): 슬롯당 현행 대비
+0.18%p [0.08, 0.28] (2023-26), +0.29%p [0.19, 0.38] (2019-23), profit factor도 높았다.
현행 v1 증액 규칙은 거래의 약 3%에서만 발동해 사실상 고정 50% 포지션과 같았다.
운영은 5분봉과 검증된 pivot을 쓰므로 이 수치는 방향성 참고치이며 성과 증명이 아니다.

호환성: 기존 v1 계획·캠페인·원장 행은 다시 쓰지 않는다. v1 계획은 원래 해시와 v1 규칙
(거래량 게이트, 5일 만료)으로 계속 평가되고, 새 SHADOW 캠페인만 v2 계획을 고정한다.
원장 소유자/코호트는 계획 버전을 따른다(`oneil-adaptive-v1:` / `oneil-adaptive-v2:`).
두 버전 모두 SHADOW 소유자 외 직접 target/sell/mark를 거부한다. 기존 설정 파일의
`policy=oneil-adaptive-v1`은 OFF/SHADOW에서 그대로 유효하지만, LIVE는 설정과 승인 모두
`oneil-adaptive-v2`와 현재 구현 해시를 요구한다.

### v2 리뷰 반영 및 알려진 제약

- 실효 최초 비중 범위는 **0.35~0.80**이다. stop_proxy 상한이 10%이므로
  0.5×0.07/0.10 = 0.35가 최솟값이며, 공식의 0.30 하한은 실제로 적용되지 않는다(공식은 변경하지 않음).
- 5분봉 입력 생성기의 매칭 거래량 블록은 v2에서 best-effort다. 반일장 등으로 이전 세션이
  짧거나, 이전 구간 거래량 합이 0이거나, 이전 구간 봉이 빠지면 `volume=None`
  (`MATCHED_VOLUME_UNAVAILABLE`)이고 상태는 OK로 유지된다. 당일 구간 봉은 여전히 엄격하다.
  v1 계획은 입력 브리지에서 매칭 거래량을 계속 요구한다(`MATCHED_VOLUME_REQUIRED_FOR_V1`).
- `create_plan`(v2)은 ATR 관측이 계획 생성보다 1일 넘게 오래되었거나, ATR 마지막 거래일이
  계획의 뉴욕 날짜보다 5 달력일 넘게 앞서면 거부한다.
- 보호 전용 설정 로드(`protection_only`)는 `mode=LIVE`이면서 `policy=oneil-adaptive-v1`인 파일도
  계속 읽는다. 신규 위험(LIVE 매수·증액)만 v2 설정과 v2 승인을 요구한다.
- LIVE 승인 구현 해시에 ATR·추세·소유권 모듈(`oneil_auto_review*`, `oneil_setup_inputs`,
  `oneil_intraday_inputs`, `oneil_runtime`, `strategy_ledger`, `oneil_input_bridge`)을 포함했다.
- (L1) 배포는 US 배치 시간 밖에서 한다. 운영 db-server crontab은 CRON_TZ=America/New_York 10:15·14:30 ET(서머타임 중 23:15·03:30 KST, 해제 후 00:15·04:30 KST)이며 배치는 약 1시간 걸린다. 배포 전에 작성된 보고서 sidecar는
  volatility 절이 없어 재계산 번들과 일치하지 않으므로, 같은 배치의 캡처에는 적응형 계획이 생기지 않는다.
- (L7) 연구/레거시 보조 도구는 v2를 완전히 지원하지 않는다. paired replay는 적응형 arm을 10%에서
  시작하므로 v2 틱이 모두 증액이 되어 추세 증거가 필요하고, 만료 후 틱은 스키마 probe를 하지 않는다.
  `oneil_live_boundary.project_live_candidate`(운영 미사용)는 최초 50% 초과 v2 진입을 차단(fail-closed)한다.
  `INITIAL_POLICY_50` 이름은 v2의 0.35~0.80 최초 비중과 맞지 않지만 식별자로 유지한다.
- (L8) `run_oneil_execution --check`와 health 출력은 운용자 설정 파일의 `policy`를 그대로 보여 주므로,
  배포 후 설정을 다시 쓰기 전까지 v1로 표시된다. 새 캠페인은 설정과 무관하게 v2 계획을 고정한다.
  배포 후 `python tools/configure_oneil_execution.py --mode SHADOW --from-configured-us`를 다시 실행하면
  `policy=oneil-adaptive-v2`로 기록된다.
