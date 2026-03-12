import os
import json
import asyncio
from datetime import datetime, date
from pathlib import Path
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

def now_kst():
    return datetime.now(KST)

import httpx
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ── 캐시: 최신 브리핑 데이터를 메모리에 보관 ─────────────────────────
cache: dict = {
    "briefing": None,       # 파싱된 브리핑 dict
    "generated_at": None,   # datetime
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
CACHE_FILE = Path("briefing_cache.json")

# ── 장애 모니터링 캐시 ────────────────────────────────────────────────
monitor_cache: dict = {
    "results": None,
    "checked_at": None,
}

SERVICES = [
    {"id": "skt",       "name": "SKT",       "emoji": "🔷", "keywords": ["SKT 장애", "SKT 불통", "SKT 안됨", "SKT 오류"]},
    {"id": "kt",        "name": "KT",        "emoji": "🔴", "keywords": ["KT 장애", "KT 불통", "KT 인터넷 안됨", "KT 오류"]},
    {"id": "lgu",       "name": "LGU+",      "emoji": "💜", "keywords": ["LGU+ 장애", "LG유플러스 장애", "유플러스 불통", "유플러스 안됨"]},
    {"id": "netflix",   "name": "넷플릭스",   "emoji": "🎬", "keywords": ["넷플릭스 장애", "넷플릭스 안됨", "넷플릭스 오류"]},
    {"id": "wavve",     "name": "웨이브",     "emoji": "🌊", "keywords": ["웨이브 장애", "웨이브 안됨", "wavve 오류"]},
    {"id": "tving",     "name": "티빙",       "emoji": "📺", "keywords": ["티빙 장애", "티빙 안됨", "tving 오류"]},
    {"id": "naver",     "name": "네이버",     "emoji": "🟢", "keywords": ["네이버 장애", "네이버 안됨", "네이버 오류"]},
    {"id": "kakao",     "name": "카카오",     "emoji": "🟡", "keywords": ["카카오 장애", "카카오톡 불통", "카카오 안됨"]},
]


# ── 네이버 뉴스 검색으로 장애 감지 ──────────────────────────────────
async def check_service(service: dict) -> dict:
    """네이버 뉴스 API로 장애 키워드 검색"""
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    articles = []
    is_down = False

    for keyword in service["keywords"][:2]:  # 키워드 2개만 검색 (API 절약)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://openapi.naver.com/v1/search/news.json",
                    headers=headers,
                    params={"query": keyword, "display": 3, "sort": "date"},
                )
                if resp.is_success:
                    data = resp.json()
                    items = data.get("items", [])
                    for item in items:
                        # 1시간 이내 기사만 체크
                        pub_date = item.get("pubDate", "")
                        try:
                            from email.utils import parsedate_to_datetime
                            pub_dt = parsedate_to_datetime(pub_date)
                            age_minutes = (datetime.now(pub_dt.tzinfo) - pub_dt).total_seconds() / 60
                            if age_minutes <= 60:
                                is_down = True
                                articles.append({
                                    "title": item.get("title", "").replace("<b>","").replace("</b>",""),
                                    "link": item.get("link", ""),
                                    "pubDate": pub_date,
                                })
                        except Exception:
                            pass
        except Exception as e:
            print(f"네이버 API 오류 ({keyword}): {e}")

    return {
        "id": service["id"],
        "name": service["name"],
        "emoji": service["emoji"],
        "status": "down" if is_down else "normal",
        "articles": articles[:3],
    }


async def monitor_task():
    """전체 서비스 장애 체크"""
    print(f"[{now_kst()}] 장애 모니터링 시작...")
    results = []
    for service in SERVICES:
        result = await check_service(service)
        results.append(result)

    monitor_cache["results"] = results
    monitor_cache["checked_at"] = now_kst().isoformat()
    down_list = [r["name"] for r in results if r["status"] == "down"]
    print(f"[{now_kst()}] 장애 모니터링 완료 ✅ 이상 감지: {down_list if down_list else '없음'}")



async def call_claude(system: str, user: str) -> str:
    """Claude claude-sonnet-4-20250514 에 웹서치 도구와 함께 요청을 보내고 텍스트 응답을 반환."""
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 8000,
        "system": system,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": user}],
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
        )
        if not resp.is_success:
            print(f"API 오류 상세: {resp.status_code} - {resp.text}")
        resp.raise_for_status()
        data = resp.json()

    # content 블록 중 text 만 합치기
    texts = [blk["text"] for blk in data.get("content", []) if blk.get("type") == "text"]
    result = "\n".join(texts).strip()
    print(f"API 응답 미리보기: {result[:200]}")
    return result


# ── 브리핑 생성 핵심 로직 ─────────────────────────────────────────────
async def generate_briefing() -> dict:
    now = now_kst()
    today_str = now.strftime("%Y년 %m월 %d일")
    weekday = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]

    SYSTEM = """당신은 국내 통신·IT 업계 전문 뉴스 큐레이터입니다.
웹 검색으로 오늘의 최신 뉴스를 수집하고, 아래 JSON 스키마에 맞게 정확히 반환하세요.
반드시 유효한 JSON만 출력하고 다른 텍스트는 절대 포함하지 마세요.
마크다운 코드블록(```json)도 사용하지 마세요."""

    USER = f"""오늘({today_str} {weekday}요일) 오전 9시 기준, 최근 48시간 이내 발행된 뉴스만 웹 검색으로 수집·요약해주세요. 48시간 이내 기사가 없는 항목은 "관련 동향 없음"으로 표시하세요.

수집 영역:
1. 통신 3사 (LG유플러스, KT, SKT) – 품질/AI/신사업/마케팅/제휴/파트너십
2. 국내외 주요 기업 동향
   국내: 네이버, 카카오, 카카오페이, 토스, 배달의민족, 당근, 쿠팡, 삼성전자, LG전자, 현대차, 기아, 롯데, 신세계, 라인
   해외: 구글, 메타, 애플, 마이크로소프트, 엔비디아, OpenAI, Anthropic
3. 국내외 AI 산업 전반 – 아래 출처 우선 참고:
   TechCrunch, AI Business Daily, CB Insights, MIT Technology Review, The Miilk, 인공지능신문
   업계 동향뿐 아니라 AI 기술 발전, 흥미로운 연구·사례도 포함
4. 해외 빅테크 동향 (Google, Microsoft, OpenAI, Meta, Apple 등)

출력 스키마 (JSON):
{{
  "date": "{today_str} ({weekday})",
  "keywords": ["#키워드1", "#키워드2", "#키워드3", "#키워드4", "#키워드5"],
  "telco": {{
    "LGU": {{"품질": "...", "품질_url": "", "AI": "...", "AI_url": "", "신사업": "...", "신사업_url": ""}},
    "KT":  {{"품질": "...", "품질_url": "", "AI": "...", "AI_url": "", "신사업": "...", "신사업_url": ""}},
    "SKT": {{"품질": "...", "품질_url": "", "AI": "...", "AI_url": "", "신사업": "...", "신사업_url": ""}}
  }},
  "summary": [
    "요약 문장 1",
    "요약 문장 2",
    "요약 문장 3"
  ],
  "companies": [
    {{
      "name": "기업명",
      "summary": "오늘 뉴스 핵심 내용을 2~3문장으로 통합 요약 (품질·AI 구분 없이 자연스럽게)",
      "url": "해당 기업 관련 뉴스 기사 URL (없으면 빈 문자열)"
    }}
  ],
  "quality": [
    {{"type": "품질개선|보안사고|장애사고", "company": "기업명", "desc": "설명"}}
  ],
  "ai_news": [
    {{
      "title": "뉴스 제목",
      "bullets": ["핵심 포인트 1 (이모지 포함)", "핵심 포인트 2", "핵심 포인트 3"],
      "source": "출처 (예: MIT Technology Review)",
      "url": "해당 뉴스 기사의 실제 URL (없으면 빈 문자열)"
    }}
  ]
}}

ai_news 5~8건, companies·telco의 url은 실제 기사 URL로 채우고 없으면 빈 문자열.
companies는 네이버, 카카오, 카카오페이, 토스, 배달의민족, 당근, 쿠팡, 삼성전자, LG전자, 현대차, 기아, 롯데, 신세계, 라인, 구글, 메타, 애플, 마이크로소프트, 엔비디아, OpenAI, Anthropic 중 오늘 뉴스에 등장한 기업만 포함. 없으면 빈 배열.
quality 사례 없으면 type을 "해당없음"으로. 동향 없으면 "관련 동향 없음"."""

    raw = await call_claude(SYSTEM, USER)

    # cite 태그 제거
    import re
    raw = re.sub(r'<cite[^>]*>(.*?)</cite>', r'\1', raw, flags=re.DOTALL)

    # JSON 블록만 추출 (앞뒤 설명 텍스트 제거)
    cleaned = raw.strip()
    # ```json ... ``` 블록 추출 시도
    json_match = re.search(r'```json\s*([\s\S]*?)```', cleaned)
    if json_match:
        cleaned = json_match.group(1).strip()
    else:
        # ``` ... ``` 블록 추출 시도
        code_match = re.search(r'```\s*([\s\S]*?)```', cleaned)
        if code_match:
            cleaned = code_match.group(1).strip()
        else:
            # { 로 시작하는 JSON 부분만 추출
            json_start = cleaned.find('{')
            json_end = cleaned.rfind('}')
            if json_start != -1 and json_end != -1:
                cleaned = cleaned[json_start:json_end+1]

    briefing = json.loads(cleaned)
    briefing["generated_at"] = now_kst().isoformat()
    return briefing


# ── 스케줄러 작업 ─────────────────────────────────────────────────────
async def scheduled_task():
    print(f"[{now_kst()}] 브리핑 생성 시작...")
    try:
        briefing = await generate_briefing()
        cache["briefing"] = briefing
        cache["generated_at"] = now_kst()
        # 파일 캐시 저장 (서버 재시작 시 복구용)
        CACHE_FILE.write_text(json.dumps(briefing, ensure_ascii=False, indent=2))
        print(f"[{datetime.now()}] 브리핑 생성 완료 ✅")
    except Exception as e:
        print(f"[{datetime.now()}] 브리핑 생성 실패 ❌: {e}")


# ── 앱 시작/종료 ──────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 파일 캐시 복구
    if CACHE_FILE.exists():
        try:
            cache["briefing"] = json.loads(CACHE_FILE.read_text())
            print("파일 캐시에서 브리핑 복구 완료")
        except Exception:
            pass

    # 스케줄러: 격일 오전 9시 브리핑 (KST) - 48시간마다
    # BRIEFING_ENABLED=true 일 때만 스케줄러 동작
    if os.environ.get("BRIEFING_ENABLED", "false").lower() == "true":
        scheduler.add_job(scheduled_task, CronTrigger(hour=9, minute=0, day="*/2", timezone="Asia/Seoul"))
        print("브리핑 스케줄러 활성화 ✅")
    else:
        print("브리핑 스케줄러 비활성화 (BRIEFING_ENABLED=false) ⏸")
    # 스케줄러: 30분마다 장애 모니터링
    scheduler.add_job(monitor_task, "interval", minutes=30)
    scheduler.start()

    # 캐시가 없고 BRIEFING_ENABLED=true 일 때만 즉시 생성
    if cache["briefing"] is None and os.environ.get("BRIEFING_ENABLED", "false").lower() == "true":
        asyncio.create_task(scheduled_task())
    # 모니터링 즉시 한 번 실행
    asyncio.create_task(monitor_task())

    yield
    scheduler.shutdown()


app = FastAPI(title="AI 뉴스 브리핑", lifespan=lifespan)


# ── 라우트 ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/briefing")
async def get_briefing():
    if cache["briefing"] is None:
        return JSONResponse({"status": "generating", "message": "브리핑 생성 중입니다. 잠시 후 새로고침해주세요."}, status_code=202)
    return JSONResponse(cache["briefing"])


@app.post("/api/refresh")
async def refresh_briefing(background_tasks: BackgroundTasks):
    """수동 갱신 엔드포인트 (관리자용)"""
    background_tasks.add_task(scheduled_task)
    return {"message": "브리핑 갱신을 시작했습니다."}


@app.get("/monitor", response_class=HTMLResponse)
async def monitor():
    html = Path("templates/monitor.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/monitor")
async def get_monitor():
    if monitor_cache["results"] is None:
        return JSONResponse({"status": "checking"}, status_code=202)
    return JSONResponse(monitor_cache)


@app.get("/health")
@app.head("/health")
async def health():
    return {"status": "ok", "cached": cache["briefing"] is not None}
