import os
import json
import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

KST = ZoneInfo("Asia/Seoul")

def now_kst():
    return datetime.now(KST)

import httpx
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ── 캐시 ─────────────────────────────────────────────────────────────
cache: dict = {
    "briefing": None,
    "generated_at": None,
    "last_error": None,
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CACHE_FILE = Path("briefing_cache.json")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [기반] Claude API 호출 함수
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def call_claude(system: str, user: str, max_tokens: int = 1200) -> str:
    """RSS로 수집한 기사를 Claude에게 요약만 시킴 (웹서치 없음 → 비용 절감)"""
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
        )
        if not resp.is_success:
            print(f"API 오류: {resp.status_code} - {resp.text}")
        resp.raise_for_status()
        data = resp.json()

    texts = [blk["text"] for blk in data.get("content", []) if blk.get("type") == "text"]
    result = "\n".join(texts).strip()
    result = re.sub(r'<cite[^>]*>(.*?)</cite>', r'\1', result, flags=re.DOTALL)
    print(f"API 응답 미리보기: {result[:150]}")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [기반] JSON 파싱 공통 함수
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def parse_json(raw: str) -> dict | list:
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
    return json.loads(cleaned)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [기반] Google News RSS 수집
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def fetch_google_news(query: str, limit: int = 12, hours: int = 48) -> list[dict]:
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as e:
        print(f"[RSS] 수집 실패 ({query}): {e}")
        return []

    now = now_kst()
    cutoff = now - timedelta(hours=hours)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        source = (item.findtext("source") or "").strip()
        if not title or not link:
            continue
        try:
            pub_dt = parsedate_to_datetime(pub_date_raw).astimezone(KST) if pub_date_raw else now
        except Exception:
            pub_dt = now
        if pub_dt < cutoff:
            continue
        items.append({
            "title": title,
            "url": link,
            "source": source,
            "published_at": pub_dt.isoformat(),
        })
        if len(items) >= limit:
            break
    return items


def compact_article_lines(items: list[dict], max_items: int = 10) -> str:
    if not items:
        return "[]"
    trimmed = items[:max_items]
    return json.dumps(
        [{"title": i["title"], "url": i["url"], "source": i.get("source", "")} for i in trimmed],
        ensure_ascii=False,
        separators=(",", ":"),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Worker 1] 통신3사 뉴스 수집
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def worker_telco(today_str: str, weekday: str) -> dict:
    print(f"[Worker 1: 통신3사] RSS 수집 시작...")
    articles = await fetch_google_news("LG유플러스 OR KT OR SKT 통신 AI", limit=14, hours=48)
    print(f"[Worker 1: 통신3사] {len(articles)}건 수집됨")

    SYSTEM = """당신은 통신사 전문 뉴스 수집 AI입니다.
반드시 유효한 JSON만 출력하고 다른 텍스트는 절대 포함하지 마세요.
SKT, KT, LG유플러스 관련 기사만 수집하세요."""

    USER = f"""{today_str}({weekday}) 기준 최근 48시간 기사 후보만 근거로 통신3사 뉴스를 정리하세요.
후보 기사:
{compact_article_lines(articles, max_items=12)}

아래 JSON 형식으로 반환:
{{
  "telco": {{
    "LGU": {{"품질": "...", "품질_url": "", "AI": "...", "AI_url": "", "신사업": "...", "신사업_url": ""}},
    "KT":  {{"품질": "...", "품질_url": "", "AI": "...", "AI_url": "", "신사업": "...", "신사업_url": ""}},
    "SKT": {{"품질": "...", "품질_url": "", "AI": "...", "AI_url": "", "신사업": "...", "신사업_url": ""}}
  }},
  "quality": [
    {{"type": "품질개선|보안사고|장애사고|해당없음", "company": "기업명", "desc": "설명"}}
  ]
}}

동향 없으면 "관련 동향 없음". quality 없으면 type을 "해당없음"으로. 반드시 후보 기사 내용만 사용하고 과거 사례 사용 금지."""

    try:
        raw = await call_claude(SYSTEM, USER, max_tokens=900)
        result = parse_json(raw)
    except Exception as e:
        print(f"[Worker 1: 통신3사] ❌ 실패: {e}")
        result = {"telco": {}, "quality": []}

    # Reflection: 내용 확인
    telco = result.get("telco", {})
    NO_NEWS = "관련 동향 없음"
    def has_news(company):
        co = telco.get(company, {})
        return any(co.get(f, NO_NEWS) not in ("", NO_NEWS) for f in ["품질", "AI", "신사업"])

    missing = [c for c in ["LGU", "KT", "SKT"] if not has_news(c)]
    if missing:
        print(f"[Worker 1: 통신3사] ⚠️ {missing} 내용 없음 (RSS 기사 부족)")
    else:
        print(f"[Worker 1: 통신3사] ✅ 완료")

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Worker 2] 국내 IT기업 뉴스 수집
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def worker_companies(today_str: str, weekday: str) -> dict:
    print(f"[Worker 2: 국내IT] RSS 수집 시작...")
    articles = await fetch_google_news("네이버 OR 카카오 OR 쿠팡 OR 토스 OR 당근 OR 삼성전자 OR 현대차 IT", limit=14, hours=48)
    print(f"[Worker 2: 국내IT] {len(articles)}건 수집됨")

    SYSTEM = """당신은 국내 IT기업 전문 뉴스 수집 AI입니다.
반드시 유효한 JSON만 출력하고 다른 텍스트는 절대 포함하지 마세요.
SKT, KT, LG유플러스는 절대 포함하지 마세요."""

    USER = f"""{today_str}({weekday}) 기준 최근 48시간 기사 후보만 근거로 국내 IT기업 뉴스를 정리하세요.
(통신3사 SKT/KT/LGU+ 제외)
후보 기사:
{compact_article_lines(articles, max_items=12)}

아래 JSON 형식으로 반환:
{{
  "companies": [
    {{
      "name": "기업명",
      "summary": "핵심 내용 2~3문장 요약",
      "url": "기사 URL (없으면 빈 문자열)"
    }}
  ]
}}

최대한 많은 기업 포함. 동향 없는 기업은 제외. 반드시 후보 기사 내용만 사용."""

    try:
        raw = await call_claude(SYSTEM, USER, max_tokens=900)
        result = parse_json(raw)
    except Exception as e:
        print(f"[Worker 2: 국내IT] ❌ 실패: {e}")
        result = {"companies": []}

    # Routing 보정: 통신사 사후 제거
    TELCOS = ["SKT", "KT", "LGU+", "LG유플러스", "SK텔레콤"]
    companies_raw = result.get("companies", [])
    companies = [c for c in companies_raw if not any(t in c.get("name", "") for t in TELCOS)]
    removed = len(companies_raw) - len(companies)
    if removed > 0:
        print(f"[Worker 2: 국내IT] ⚠️ 통신사 {removed}개 제거됨")
    result["companies"] = companies

    print(f"[Worker 2: 국내IT] ✅ {len(companies)}개 기업 완료")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Worker 3] 국내외 AI 뉴스 수집
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def worker_ai_news(today_str: str, weekday: str) -> dict:
    print(f"[Worker 3: AI뉴스] RSS 수집 시작...")
    articles = await fetch_google_news("AI LLM 생성형AI OpenAI Anthropic Gemini 에이전트", limit=16, hours=48)
    print(f"[Worker 3: AI뉴스] {len(articles)}건 수집됨")

    SYSTEM = """당신은 AI 산업 전문 뉴스 수집 AI입니다.
반드시 유효한 JSON만 출력하고 다른 텍스트는 절대 포함하지 마세요.
에이전틱 AI, LLM, AI 정책, AI 연구 등 AI 산업 전반의 중요 뉴스를 수집하세요."""

    USER = f"""{today_str}({weekday}) 기준 최근 48시간 기사 후보만 근거로 AI 산업 뉴스를 정리하세요.
후보 기사:
{compact_article_lines(articles, max_items=14)}

아래 JSON 형식으로 반환:
{{
  "ai_news": [
    {{
      "title": "뉴스 제목",
      "bullets": ["🔑 핵심 포인트 1", "📊 핵심 포인트 2", "💡 핵심 포인트 3"],
      "source": "출처",
      "url": "기사 URL (없으면 빈 문자열)"
    }}
  ]
}}

3~5건 수집. 반드시 후보 기사 내용만 사용."""

    try:
        raw = await call_claude(SYSTEM, USER, max_tokens=1000)
        result = parse_json(raw)
    except Exception as e:
        print(f"[Worker 3: AI뉴스] ❌ 실패: {e}")
        result = {"ai_news": []}

    count = len(result.get("ai_news", []))
    print(f"[Worker 3: AI뉴스] ✅ {count}건 완료")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Worker 4] 해외 빅테크 뉴스 수집
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def worker_bigtech(today_str: str, weekday: str) -> dict:
    print(f"[Worker 4: 빅테크] RSS 수집 시작...")
    articles = await fetch_google_news("Google OR Microsoft OR Meta OR Apple OR NVIDIA OR OpenAI", limit=14, hours=48)
    print(f"[Worker 4: 빅테크] {len(articles)}건 수집됨")

    SYSTEM = """당신은 해외 빅테크 전문 뉴스 수집 AI입니다.
반드시 유효한 JSON만 출력하고 다른 텍스트는 절대 포함하지 마세요.
Google, Microsoft, OpenAI, Meta, Apple, NVIDIA 등 해외 빅테크 동향을 수집하세요."""

    USER = f"""{today_str}({weekday}) 기준 최근 48시간 기사 후보만 근거로 해외 빅테크 뉴스를 정리하세요.
후보 기사:
{compact_article_lines(articles, max_items=12)}

아래 JSON 형식으로 반환:
{{
  "bigtech": [
    {{
      "name": "기업명",
      "summary": "핵심 내용 2~3문장 요약",
      "url": "기사 URL (없으면 빈 문자열)"
    }}
  ]
}}

3~5건 수집. 반드시 후보 기사 내용만 사용."""

    try:
        raw = await call_claude(SYSTEM, USER, max_tokens=800)
        result = parse_json(raw)
    except Exception as e:
        print(f"[Worker 4: 빅테크] ❌ 실패: {e}")
        result = {"bigtech": []}

    count = len(result.get("bigtech", []))
    print(f"[Worker 4: 빅테크] ✅ {count}건 완료")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Worker 5] 키워드 + 전체 요약 생성
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def worker_summary(all_data: dict) -> dict:
    print(f"[Worker 5: 요약] 키워드 및 전체 요약 생성 중...")

    SYSTEM = """당신은 뉴스 요약 전문 AI입니다.
반드시 유효한 JSON만 출력하고 다른 텍스트는 절대 포함하지 마세요."""

    USER = f"""아래 수집된 뉴스 데이터를 바탕으로 오늘의 키워드와 전체 요약을 작성하세요.

수집된 데이터:
{json.dumps(all_data, ensure_ascii=False, separators=(",", ":"))}

아래 JSON 형식으로 반환:
{{
  "keywords": ["#키워드1", "#키워드2", "#키워드3", "#키워드4", "#키워드5"],
  "summary": [
    "오늘 뉴스 전체를 관통하는 핵심 트렌드 문장 1",
    "핵심 트렌드 문장 2",
    "핵심 트렌드 문장 3"
  ]
}}"""

    try:
        raw = await call_claude(SYSTEM, USER, max_tokens=500)
        result = parse_json(raw)
    except Exception as e:
        print(f"[Worker 5: 요약] ❌ 실패: {e}")
        result = {"keywords": [], "summary": ["요약 생성에 실패했습니다."]}

    print(f"[Worker 5: 요약] ✅ 완료")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Orchestrator] 전체 브리핑 생성 총괄
# Worker 1~4 동시 실행 (Parallelization) → Worker 5 (Chaining)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def generate_briefing() -> dict:
    now = now_kst()
    today_str = now.strftime("%Y년 %m월 %d일")
    weekday = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]

    print(f"\n{'='*50}")
    print(f"[Orchestrator] 브리핑 생성 시작: {today_str} ({weekday})")
    print(f"{'='*50}")

    # Step 1: Worker 1~4 동시 실행
    print(f"\n[Step 1] 4개 영역 동시 수집 (Parallelization)")

    async def run_worker(label, coro, fallback):
        try:
            return await coro
        except Exception as e:
            print(f"[{label}] ❌ 실패: {e}")
            return fallback

    telco_result, companies_result, ai_result, bigtech_result = await asyncio.gather(
        run_worker("Worker 1: 통신3사", worker_telco(today_str, weekday),     {"telco": {}, "quality": []}),
        run_worker("Worker 2: 국내IT",  worker_companies(today_str, weekday), {"companies": []}),
        run_worker("Worker 3: AI뉴스",  worker_ai_news(today_str, weekday),   {"ai_news": []}),
        run_worker("Worker 4: 빅테크",  worker_bigtech(today_str, weekday),   {"bigtech": []}),
    )
    print(f"[Step 1] 완료 ✅")

    # Step 2: Worker 5로 요약 생성
    print(f"\n[Step 2] 전체 요약 생성 (Chaining)")
    all_collected = {
        "telco": telco_result.get("telco", {}),
        "companies": companies_result.get("companies", [])[:5],
        "ai_news": ai_result.get("ai_news", [])[:5],
        "bigtech": bigtech_result.get("bigtech", [])[:5],
    }
    summary_result = await worker_summary(all_collected)
    print(f"[Step 2] 완료 ✅")

    # Step 3: 최종 결과 조합
    briefing = {
        "date": f"{today_str} ({weekday})",
        "keywords": summary_result.get("keywords", []),
        "telco": telco_result.get("telco", {}),
        "summary": summary_result.get("summary", []),
        "companies": companies_result.get("companies", []),
        "quality": telco_result.get("quality", []),
        "ai_news": ai_result.get("ai_news", []),
        "bigtech": bigtech_result.get("bigtech", []),
        "generated_at": now_kst().isoformat(),
    }

    print(f"\n[Orchestrator] ✅ 브리핑 생성 완료!")
    print(f"{'='*50}\n")
    return briefing


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 스케줄러 & FastAPI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def scheduled_task():
    print(f"[{now_kst()}] 브리핑 생성 시작...")
    try:
        briefing = await generate_briefing()
        cache["briefing"] = briefing
        cache["generated_at"] = now_kst()
        cache["last_error"] = None
        CACHE_FILE.write_text(json.dumps(briefing, ensure_ascii=False, indent=2))
        print(f"[{now_kst()}] 브리핑 생성 완료 ✅")
    except Exception as e:
        cache["last_error"] = str(e)
        print(f"[{datetime.now()}] 브리핑 생성 실패 ❌: {e}")


scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

@asynccontextmanager
async def lifespan(app: FastAPI):
    if CACHE_FILE.exists():
        try:
            cache["briefing"] = json.loads(CACHE_FILE.read_text())
            print("파일 캐시에서 브리핑 복구 완료")
        except Exception:
            pass

    if os.environ.get("BRIEFING_ENABLED", "false").lower() == "true":
        scheduler.add_job(scheduled_task, CronTrigger(hour=9, minute=0, timezone="Asia/Seoul"))
        print("브리핑 스케줄러 활성화 ✅ (매일 오전 9시)")
        if cache["briefing"] is None:
            asyncio.create_task(scheduled_task())
    else:
        print("브리핑 스케줄러 비활성화 ⏸ (BRIEFING_ENABLED=false)")

    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="AI 뉴스 브리핑", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)

@app.head("/")
async def index_head():
    return JSONResponse({"status": "ok"})

@app.get("/api/briefing")
async def get_briefing():
    if cache["briefing"] is None:
        return JSONResponse(
            {"status": "generating", "message": "브리핑 생성 중입니다.", "last_error": cache["last_error"]},
            status_code=202,
        )
    return JSONResponse(cache["briefing"])

@app.post("/api/refresh")
async def refresh_briefing(background_tasks: BackgroundTasks):
    background_tasks.add_task(scheduled_task)
    return {"message": "브리핑 갱신을 시작했습니다."}

@app.get("/health")
@app.head("/health")
async def health():
    return {"status": "ok", "cached": cache["briefing"] is not None}
