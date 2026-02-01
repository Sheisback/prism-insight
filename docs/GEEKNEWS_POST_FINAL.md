제목: PRISM-INSIGHT - 14개 AI 에이전트가 한국+미국 주식을 분석하는 오픈소스 시스템
링크: prism-insight-landing.vercel.app

---

제가 만든 오픈소스 AI 주식분석 시스템입니다.
지난 8월에 긱뉴스에서 처음 공유한 후, 6개월간 많은 변화가 있었습니다.
550명 이상이 텔레그램 채널을 구독 중이고, 모든 매매 내역을 실시간으로 공개하고 있습니다.


[지난 공유 이후 변화]

- AI 에이전트: 11개 → 14개
- AI 모델: GPT-4.1 → GPT-5
- 시장 지원: 한국만 → 한국 + 미국
- 구독자: 100명+ → 550명+
- 실계좌 운용: 미운용 → 운용 중


[어떻게 동작하나?]

1. 급등주 자동 감지
시장 데이터를 분석해 비정상적인 거래량/가격 움직임을 포착합니다.

2. AI 분석 리포트 자동 생성
14개의 전문화된 AI 에이전트가 협업하여 애널리스트급 PDF 리포트를 생성합니다.
- 기술분석가, 거래흐름분석가, 재무분석가, 산업분석가
- 정보분석가, 시장분석가, 투자전략가
- 요약최적화전문가, 품질평가자
- 매수전문가, 매도전문가, 포트폴리오컨설턴트, 대화관리자
- 매매일지 에이전트 (선택): 장기기억 보관, 직관 기반 매매 보조

3. 자동 매매 실행
GPT-5가 AI 분석 결과를 바탕으로 매매 여부를 결정하고, 한국투자증권 API로 실제 주문을 실행합니다.

4. 실시간 투명 공개
모든 거래 내역을 대시보드에서 공개합니다. 성공 거래만 선별하지 않습니다.


[기술 구성]

- AI 모델: OpenAI GPT-5 (분석), Claude Sonnet 4.5 (메시지)
- 에이전트: 14개 전문화된 AI 에이전트
- 데이터: KRX API, Yahoo Finance, SEC 공시, Perplexity (뉴스 검색), Firecrawl (웹 크롤링)
- 매매 API: 한국투자증권 KIS API
- 코드: Python 3.10+, 70개 파일, 16,000+ 줄


[실제 성과] (시즌2, 2025년 9월~)

- 거래 건수: 54건
- 승률: 44.44%
- 실계좌 수익률: +14.71%

솔직히 말하면, 같은 기간 코스피/코스닥이 30~50% 상승한 것에 비하면 아쉬운 성과입니다. 그래도 모든 거래를 라이브 대시보드(analysis.stocksimulation.kr)에서 투명하게 공개하고 있습니다.

※ 과거 성과는 미래 성과를 보장하지 않습니다. 모든 투자 판단과 책임은 개인에게 있습니다.


[60초 안에 시작하기]

OpenAI API 키만 있으면 바로 체험할 수 있습니다.

git clone https://github.com/dragon1086/prism-insight.git
cd prism-insight
./quickstart.sh YOUR_OPENAI_API_KEY

Apple의 AI 분석 리포트가 바로 생성됩니다:

python3 demo.py MSFT  # Microsoft
python3 demo.py NVDA  # NVIDIA
python3 demo.py TSLA --language ko  # Tesla (한국어)

Docker로도 실행 가능합니다:

export OPENAI_API_KEY=sk-your-key-here
docker-compose -f docker-compose.quickstart.yml up -d
docker exec -it prism-quickstart python3 demo.py NVDA


[함께 만들어가는 오픈소스]

지난 6개월간 커뮤니티 피드백으로 다음을 추가했습니다:

- 미국 주식 지원 (NYSE/NASDAQ)
- 60초 퀵스타트
- Docker 지원
- SEC 공시 분석
- 텔레그램 실시간 알림
- 매매일지 에이전트 (장기기억 + 직관)

GitHub에서 스타 주고, Issues에서 의견을 공유해주세요. PR도 환영합니다.


[링크]

- 랜딩페이지: prism-insight-landing.vercel.app
- 라이브 대시보드: analysis.stocksimulation.kr
- GitHub: github.com/dragon1086/prism-insight
- Telegram (한국): t.me/stock_ai_agent
- Telegram (영어): t.me/prism_insight_global_en
