# 🤖 AI 뉴스 브리핑 — Render 배포 가이드

Claude AI가 매일 오전 9시에 통신·IT·AI 뉴스를 자동 수집·요약하여 웹 페이지로 제공합니다.

## 📁 프로젝트 구조

```
ai-briefing/
├── main.py              # FastAPI 서버 + 스케줄러 + Claude API 호출
├── templates/
│   └── index.html       # 신문 스타일 브리핑 페이지
├── requirements.txt
├── render.yaml          # Render 자동 배포 설정
└── README.md
```

## 🚀 배포 방법 (10분 소요)

### 1단계 — GitHub 레포 생성
```bash
git init
git add .
git commit -m "initial: AI 뉴스 브리핑"
git remote add origin https://github.com/YOUR_ID/ai-news-briefing.git
git push -u origin main
```

### 2단계 — Render 연결
1. [render.com](https://render.com) 로그인
2. **New → Web Service** 클릭
3. GitHub 레포 선택 → **Connect**
4. 설정은 `render.yaml`이 자동으로 채워줌

### 3단계 — 환경변수 설정 (중요!)
Render 대시보드 → **Environment** 탭:
```
ANTHROPIC_API_KEY = sk-ant-xxxxxxxxxxxxxxxx
```

### 4단계 — 배포 완료
- Render가 자동 빌드 후 URL 발급: `https://ai-news-briefing.onrender.com`
- 이 URL을 Teams 채널에 고정 링크로 공유

---

## ⏰ 동작 방식

| 시간 | 동작 |
|------|------|
| 서버 시작 | 캐시가 없으면 즉시 브리핑 생성 |
| 매일 09:00 KST | Claude가 웹 검색으로 뉴스 수집 + 요약 자동 실행 |
| 사용자 접속 | 캐시된 HTML을 즉시 반환 (빠른 응답) |

---

## 🔧 수동 갱신 (필요 시)

```bash
curl -X POST https://ai-news-briefing.onrender.com/api/refresh
```

---

## ⚠️ Render 무료 플랜 주의사항

무료 플랜은 **15분 비활성 시 슬립** 됩니다.
슬립 방지를 원하면 아래 중 하나를 선택:
- Render **Starter 플랜** ($7/월)으로 업그레이드
- 또는 [UptimeRobot](https://uptimerobot.com) 무료 서비스로 14분마다 ping 설정
  - URL: `https://ai-news-briefing.onrender.com/health`
  - 모니터링 주기: 14분

---

## 📡 API 엔드포인트

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /` | 브리핑 웹 페이지 |
| `GET /api/briefing` | 브리핑 JSON 데이터 |
| `POST /api/refresh` | 수동 갱신 트리거 |
| `GET /health` | 서버 상태 확인 |
