# PRISM-INSIGHT Product Hunt 런칭 가이드

> **목표**: Product Hunt에서 성공적으로 런칭하여 글로벌 사용자 확보
> **예상 준비 기간**: 4-6주
> **예상 작업량**: 50-120시간

## 제품 포지셔닝

| 항목 | 내용 |
|------|------|
| **Tagline** (60자 이내) | `AI-powered Korean & US stock analysis with automated trading` |
| **One-liner** | 13개 AI 에이전트가 실시간 주식 분석, 매매 시나리오, 텔레그램 알림을 자동화 |
| **카테고리** | `Developer Tools`, `Finance`, `Artificial Intelligence` |
| **타겟 오디언스** | 개발자, 개인 투자자, 퀀트 트레이더, 핀테크 스타트업 |

---

## 준비 체크리스트

### Phase 1: 사전 준비 (런칭 4-6주 전)

#### 필수 자산

- [ ] 프로필 사진 및 완성된 Maker 프로필
- [ ] 로고 (240x240 정사각형, GIF 선택적)
- [ ] 스크린샷 5-8장 (갤러리용)
- [ ] 데모 영상 (1-2분, 핵심 기능 시연)
- [ ] Product Hunt 전용 랜딩페이지

#### 개발 작업

| 작업 | 우선순위 | 상태 |
|------|---------|------|
| README 영문화 | 🔴 높음 | [x] 완료 (이미 영문) |
| 설치 간소화 (Docker) | 🔴 높음 | [x] 완료 (quickstart.sh, docker-compose.quickstart.yml) |
| 데모 영상 제작 | 🔴 높음 | [ ] |
| 랜딩 페이지 | 🔴 높음 | [ ] |
| 라이브 데모 | 🟡 중간 | [x] 완료 (analysis.stocksimulation.kr) |
| 오픈소스 라이센스 | 🟡 중간 | [x] 완료 (AGPL-3.0) |
| 문서 사이트 | 🟢 낮음 | [ ] |

### Phase 2: 커뮤니티 빌딩 (런칭 2-4주 전)

#### Product Hunt 활동 (필수!)

- [ ] 매일 3-5개 제품에 의미 있는 댓글 달기
- [ ] 관련 제품 업보트
- [ ] 다른 런칭에 Maker로 참여
- [ ] PH 디스커션 포럼 참여
- [ ] 50-100명 팔로워 확보

> ⚠️ **중요**: Product Hunt는 마케팅 채널이 아니라 **메이커 커뮤니티**입니다.

#### 외부 채널 준비

| 채널 | 활용 방법 |
|------|----------|
| **Twitter/X** | 빌드 과정 공유, 런칭 예고 |
| **Reddit** | r/algotrading, r/stocks, r/SideProject |
| **Hacker News** | Show HN 포스트 (런칭 후) |
| **개발자 커뮤니티** | 디스콰이엇, 인디해커스 코리아 |
| **Telegram** | 기존 PRISM 채널 활용 |

### Phase 3: 런칭 자산 제작

#### 갤러리 이미지 구성 (5-8장)

1. 히어로 이미지: 핵심 가치 한 줄 + 대시보드 스크린샷
2. 문제 정의: "수동 주식 분석의 한계" 비주얼
3. 솔루션: 13개 AI 에이전트 아키텍처 다이어그램
4. 주요 기능 1: 실시간 분석 리포트 생성
5. 주요 기능 2: 텔레그램 매매 알림
6. 주요 기능 3: KR/US 듀얼 마켓 지원
7. 사용 사례: 실제 트레이딩 결과 (백테스트)
8. 시작하기: 설치 3단계 (간단함 강조)

#### First Comment (Maker's Comment) 템플릿

```markdown
👋 Hi, I'm the developer of PRISM-INSIGHT!

**Why I built this:**
Analyzing dozens of stocks every morning is time-consuming.
I asked myself: what if AI could automate this?

**Key Features:**
🤖 13 specialized AI agents (technical analysis, valuation, news, etc.)
📊 Dual market support (Korean & US stocks)
📱 Real-time Telegram trading alerts
🔌 Automated trading via KIS API

**Tech Stack:**
Python 3.10+, MCP Agent, GPT-5/Claude, SQLite

Feedback and questions are welcome! 🙏
```

### Phase 4: 런칭 실행

#### 타이밍

| 요일 | 장점 | 단점 | 권장 |
|------|------|------|------|
| 화-목 | 트래픽 높음 | 경쟁 심함 | 🟡 |
| **주말** | 개발자 비율 높음, 경쟁 적음 | 트래픽 낮음 | ✅ |

**권장**: 토요일 00:01 PT (한국시간 토요일 17:01)

#### 런칭 당일 체크리스트

**T-0 (00:01 PT)**
- [ ] 제품 페이지 라이브 확인
- [ ] First Comment 즉시 게시
- [ ] 핵심 서포터 20-50명에게 알림 (첫 1시간 중요!)

**T+1h ~ T+4h (골든 타임)**
- [ ] 모든 댓글에 10분 내 답변
- [ ] 소셜 미디어 공유 (Twitter, LinkedIn)
- [ ] 서포터 네트워크 활성화

**T+4h ~ T+24h**
- [ ] 지속적 댓글 모니터링
- [ ] 버그 리포트 즉시 대응
- [ ] 업데이트 공유

---

## Product Hunt 알고리즘 이해

- 단순 업보트 수가 아닌 **포인트** 기반 랭킹
- 검증된 활성 사용자의 투표가 더 높은 가중치
- **첫 1시간** 업보트 속도가 홈페이지 노출에 결정적
- 20-50명의 초기 서포터가 알고리즘 트리거

---

## 참고 자료

- [Product Hunt Launch Checklist - Whale](https://usewhale.io/blog/product-hunt-launch-checklist/)
- [How to launch a developer tool on Product Hunt 2026](https://hackmamba.io/developer-marketing/how-to-launch-on-product-hunt/)
- [Product Hunt Discussion: How to approach PH in 2026](https://www.producthunt.com/p/producthunt/how-would-i-approach-product-hunt-in-2026)
- [LaunchList - Product Hunt Strategy](https://getlaunchlist.com/checklists/producthunt)
- [Supademo - 6 Steps for PH Success](https://supademo.com/blog/startup/startup-checklist-to-launching-on-product-hunt/)
