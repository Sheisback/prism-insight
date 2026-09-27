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

- `policy=oneil-adaptive-v1`, `initial_arm=INITIAL_POLICY_50`
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
