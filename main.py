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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ── 캐시: 최신 브리핑 데이터를 메모리에 보관 ─────────────────────────
cache: dict = {
    "briefing": None,
    "generated_at": None,
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CACHE_FILE = Path("briefing_cache.json")


# ── Claude API 호출 (웹서치 포함) ────────────────────────────────────
async def call_claude(system: str, user: str) -> str:
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

    texts = [blk["text"] for blk in data.get("content", []) if blk.get("type") == "text"]
    result = "\n".join(texts).strip()
    print(f"API 응답 미리보기: {result[:200]}")
    return result


# ── 브리핑 생성 핵심 로직 ────────────────────────────────────────────
async def generate_briefing() -> dict:
    now = now_kst()
    today_str = now.strftime("%Y년 %m월 %d일")
    weekday = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]

    SYSTEM = """당신은 국내 통신·IT 업계 전문 뉴스 큐레이터입니다.
웹 검색으로 오늘의 최신 뉴스를 수집하고, 아래 JSON 스키마에 맞게 정확히 반환하세요.
반드시 유효한 JSON만 출력하고 다른 텍스트는 절대 포함하지 마세요.
마크다운 코드블록(```json)도 사용하지 마세요."""

    USER = f"""오늘({today_str} {weekday}요일) 기준으로 최근 7일 이내 아래 영역의 주요 뉴스를 웹 검색으로 수집·요약해주세요.

수집 영역:
1. 통신 3사 (LG유플러스, KT, SKT) – 품질/AI/신사업
2. 국내 IT 기업 (네이버, 카카오, 당근, 쿠팡 등)
3. 국내외 AI 산업 전반 (에이전틱 AI, LLM, 정책 등)
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
      "summary": "오늘 뉴스 핵심 내용을 2~3문장으로 통합 요약",
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
quality 사례 없으면 type을 "해당없음"으로. 동향 없으면 "관련 동향 없음"."""

    raw = await call_claude(SYSTEM, USER)

    import re
    raw = re.sub(r'<cite[^>]*>(.*?)</cite>', r'\1', raw, flags=re.DOTALL)

    cleaned = raw.strip()
    json_match = re.search(r'```json\s*([\s\S]*?)```', cleaned)
    if json_match:
        cleaned = json_match.group(1).strip()
    else:
        code_match = re.search(r'```\s*([\s\S]*?)```', cleaned)
        if code_match:
            cleaned = code_match.group(1).strip()
        else:
            json_start = cleaned.find('{')
            json_end = cleaned.rfind('}')
            if json_start != -1 and json_end != -1:
                cleaned = cleaned[json_start:json_end+1]

    briefing = json.loads(cleaned)
    briefing["generated_at"] = now_kst().isoformat()
    return briefing


# ── 스케줄 작업 ──────────────────────────────────────────────────────
async def scheduled_task():
    print(f"[{now_kst()}] 브리핑 생성 시작...")
    try:
        briefing = await generate_briefing()
        cache["briefing"] = briefing
        cache["generated_at"] = now_kst()
        CACHE_FILE.write_text(json.dumps(briefing, ensure_ascii=False, indent=2))
        print(f"[{now_kst()}] 브리핑 생성 완료 ✅")
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

    # BRIEFING_ENABLED=true 일 때만 스케줄러 동작
    if os.environ.get("BRIEFING_ENABLED", "false").lower() == "true":
        scheduler.add_job(scheduled_task, CronTrigger(day_of_week="mon", hour=9, minute=0, timezone="Asia/Seoul"))
        print("브리핑 스케줄러 활성화 ✅ (매주 월요일 오전 9시)")
        if cache["briefing"] is None:
            asyncio.create_task(scheduled_task())
    else:
        print("브리핑 스케줄러 비활성화 ⏸ (BRIEFING_ENABLED=false)")

    scheduler.start()
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
    background_tasks.add_task(scheduled_task)
    return {"message": "브리핑 갱신을 시작했습니다."}


@app.get("/health")
@app.head("/health")
async def health():
    return {"status": "ok", "cached": cache["briefing"] is not None}
