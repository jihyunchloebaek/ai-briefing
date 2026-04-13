import os
import re
import json
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

def now_kst():
    return datetime.now(KST)

import httpx
from email.utils import parsedate_to_datetime
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
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
CACHE_FILE = Path("briefing_cache.json")

# ── 장애 모니터 ──────────────────────────────────────────────────────
SERVICES = [
    {"id": "skt",     "name": "SKT",    "emoji": "🔷", "keywords": ["SKT 불통", "SKT 먹통", "SKT 접속 안됨", "SKT 장애 발생"]},
    {"id": "kt",      "name": "KT",     "emoji": "🔴", "keywords": ["KT 불통", "KT 먹통", "KT 접속 안됨", "KT 장애 발생"]},
    {"id": "lgu",     "name": "LGU+",   "emoji": "💜", "keywords": ["유플러스 불통", "유플러스 먹통", "유플러스 접속 안됨", "유플러스 장애 발생"]},
    {"id": "netflix", "name": "넷플릭스","emoji": "🎬", "keywords": ["넷플릭스 불통", "넷플릭스 먹통", "넷플릭스 접속 안됨"]},
    {"id": "wavve",   "name": "웨이브",  "emoji": "🌊", "keywords": ["웨이브 불통", "웨이브 먹통", "웨이브 접속 안됨"]},
    {"id": "tving",   "name": "티빙",   "emoji": "📺", "keywords": ["티빙 불통", "티빙 먹통", "티빙 접속 안됨"]},
    {"id": "naver",   "name": "네이버", "emoji": "🟢", "keywords": ["네이버 불통", "네이버 먹통", "네이버 접속 안됨"]},
    {"id": "kakao",   "name": "카카오", "emoji": "🟡", "keywords": ["카카오톡 불통", "카카오 먹통", "카카오 접속 안됨"]},
]

monitor_cache: dict = {
    "results": None,
    "checked_at": None,
}

async def check_service(service: dict) -> dict:
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    articles = []
    is_down = False
    for keyword in service["keywords"][:2]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://openapi.naver.com/v1/search/news.json",
                    headers=headers,
                    params={"query": keyword, "display": 3, "sort": "date"},
                )
                if resp.is_success:
                    for item in resp.json().get("items", []):
                        try:
                            title = item.get("title", "").replace("<b>","").replace("</b>","")
                            if not any(kw in title for kw in service["keywords"]):
                                continue
                            pub_dt = parsedate_to_datetime(item.get("pubDate", ""))
                            age_min = (datetime.now(pub_dt.tzinfo) - pub_dt).total_seconds() / 60
                            if age_min <= 60:
                                is_down = True
                                articles.append({"title": title, "link": item.get("link", ""), "pubDate": item.get("pubDate", "")})
                        except Exception:
                            pass
        except Exception as e:
            print(f"네이버 API 오류 ({keyword}): {e}")
    return {
        "id": service["id"], "name": service["name"], "emoji": service["emoji"],
        "status": "down" if is_down else "normal",
        "articles": articles[:3],
    }

async def monitor_task():
    print(f"[{now_kst()}] 장애 모니터링 시작...")
    results = [await check_service(s) for s in SERVICES]
    monitor_cache["results"] = results
    monitor_cache["checked_at"] = now_kst().isoformat()
    down = [r["name"] for r in results if r["status"] == "down"]
    print(f"[{now_kst()}] 모니터링 완료 ✅ 이상 감지: {down if down else '없음'}")

# ── Claude API 호출 (웹서치 없이 요약 전용) ──────────────────────────
async def call_claude(system: str, user: str) -> str:
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 12000,
        "system": system,
        # 웹서치 tools 제거 — 네이버 API로 기사를 직접 제공하므로 불필요
        "messages": [{"role": "user", "content": user}],
    }
    print("Claude API 호출 중...")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=90, write=15, pool=5)
    ) as client:
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
    result = re.sub(r'<cite[^>]*>(.*?)</cite>', r'\1', result, flags=re.DOTALL)
    print(f"API 응답 미리보기: {result[:200]}")
    return result

# ── 네이버 API로 기사 수집 (날짜 필터 Python 코드로 정확히 처리) ─────
async def fetch_naver_news(query: str, days: int = 14) -> list[dict]:
    """
    네이버 뉴스 API로 키워드 검색 후 days 이내 기사만 반환.
    날짜 필터는 프롬프트가 아닌 Python 코드로 처리 → 100% 정확.
    """
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    # ★ 핵심: cutoff를 Python datetime으로 계산 → 코드 레벨 날짜 차단
    cutoff = now_kst() - timedelta(days=days)
    results = []

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers=headers,
                params={"query": query, "display": 10, "sort": "date"},
            )
            if not resp.is_success:
                print(f"네이버 API 오류 ({query}): {resp.status_code}")
                return []

            for item in resp.json().get("items", []):
                try:
                    pub_dt = parsedate_to_datetime(item.get("pubDate", ""))
                    # ★ 날짜 비교: cutoff 이전 기사는 여기서 완전 차단
                    if pub_dt.replace(tzinfo=None) < cutoff.replace(tzinfo=None):
                        continue
                    title = item.get("title", "").replace("<b>", "").replace("</b>", "")
                    desc  = item.get("description", "").replace("<b>", "").replace("</b>", "")
                    results.append({
                        "title":   title,
                        "link":    item.get("originallink") or item.get("link", ""),
                        "pubDate": item.get("pubDate", ""),
                        "description": desc,
                    })
                except Exception:
                    continue
    except Exception as e:
        print(f"네이버 API 오류 ({query}): {e}")

    print(f"  [{query}] 수집: {len(results)}건 (14일 이내)")
    return results


def format_articles(articles: list[dict]) -> str:
    """수집된 기사를 Claude 프롬프트용 텍스트로 변환"""
    if not articles:
        return "관련 기사 없음"
    lines = []
    for a in articles[:5]:
        lines.append(
            f"- [{a['pubDate']}] {a['title']}\n"
            f"  URL: {a['link']}\n"
            f"  내용: {a['description']}"
        )
    return "\n".join(lines)

# ── 브리핑 생성 핵심 로직 ────────────────────────────────────────────
async def generate_briefing() -> dict:
    now = now_kst()
    today_str = now.strftime("%Y년 %m월 %d일")
    weekday = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]

    # ── Step 1: 네이버 API로 분야별 기사 수집 ──────────────────────
    print(f"[{now_kst()}] 네이버 API 기사 수집 시작...")
    queries = {
        "lgu_품질":  "LG유플러스 품질",
        "lgu_ai":    "LG유플러스 AI",
        "lgu_신사업": "LG유플러스 신사업",
        "kt_품질":   "KT 품질",
        "kt_ai":     "KT AI",
        "kt_신사업":  "KT 신사업",
        "skt_품질":  "SKT 품질",
        "skt_ai":    "SKT AI",
        "skt_신사업": "SKT 신사업",
        "naver_it":  "네이버 IT 서비스",
        "kakao_it":  "카카오 IT 서비스",
        "coupang":   "쿠팡 IT",
        "dangeun":   "당근마켓 서비스",
        "ai_llm":    "AI 인공지능 LLM 에이전트",
        "bigtech":   "구글 마이크로소프트 오픈AI 메타 애플",
    }

    # 순차 수집 (네이버 API 부하 방지)
    collected = {}
    for key, query in queries.items():
        collected[key] = await fetch_naver_news(query)

    print(f"[{now_kst()}] 기사 수집 완료 ✅")

    # ── Step 2: 수집된 기사를 Claude에게 넘겨 요약 요청 ────────────
    SYSTEM = """당신은 국내 통신·IT 업계 전문 뉴스 큐레이터입니다.
아래에 제공된 기사 데이터만 사용해서 요약하세요.
제공된 기사에 없는 내용은 절대 만들거나 추측하지 마세요.
아래 JSON 스키마에 맞춰 유효한 JSON 객체만 출력하세요.
다른 텍스트, 설명, 마크다운 코드블록(```json) 출력은 금지합니다.
<cite> 태그, XML 태그, HTML 태그를 절대 사용하지 마세요. 순수 텍스트와 JSON만 출력하세요."""

    USER = f"""오늘은 {today_str} ({weekday}요일)입니다.

아래는 네이버 뉴스 API로 수집한 최근 14일 이내 실제 기사들입니다.
※ 날짜 필터는 이미 코드에서 처리됐으므로 모든 기사는 14일 이내입니다.
이 기사들만 사용해 요약하세요.

=== LGU+ 품질 ===
{format_articles(collected['lgu_품질'])}

=== LGU+ AI ===
{format_articles(collected['lgu_ai'])}

=== LGU+ 신사업 ===
{format_articles(collected['lgu_신사업'])}

=== KT 품질 ===
{format_articles(collected['kt_품질'])}

=== KT AI ===
{format_articles(collected['kt_ai'])}

=== KT 신사업 ===
{format_articles(collected['kt_신사업'])}

=== SKT 품질 ===
{format_articles(collected['skt_품질'])}

=== SKT AI ===
{format_articles(collected['skt_ai'])}

=== SKT 신사업 ===
{format_articles(collected['skt_신사업'])}

=== 국내 IT 기업 ===
{format_articles(collected['naver_it'] + collected['kakao_it'] + collected['coupang'] + collected['dangeun'])}

=== AI / LLM 뉴스 ===
{format_articles(collected['ai_llm'])}

=== 해외 빅테크 ===
{format_articles(collected['bigtech'])}

[섹션 규칙]
- telco 섹션은 통신 3사(LGU+, KT, SKT) 전용.
- companies 섹션은 통신 3사 제외 (LG유플러스, KT, SKT 이름 포함 금지).
- companies는 국내 IT/플랫폼/커머스 + 해외 IT/빅테크 중심.

[품질 기준]
- url 필드는 위에서 제공된 기사의 실제 URL만 사용. 없으면 빈 문자열 "".
- 기사가 없는 항목은 억지로 생성하지 말고 "관련 동향 없음"으로 처리.

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
      "name": "기업명 (통신3사 제외)",
      "summary": "오늘 뉴스 핵심 내용을 2~3문장으로 통합 요약",
      "url": "해당 기업 관련 뉴스 기사 URL (없으면 빈 문자열)"
    }}
  ],
  "quality": [
    {{"type": "품질개선|보안사고|장애사고|해당없음", "company": "기업명", "desc": "설명"}}
  ],
  "ai_news": [
    {{
      "title": "뉴스 제목",
      "bullets": ["핵심 포인트 1 (이모지 포함)", "핵심 포인트 2", "핵심 포인트 3"],
      "source": "출처 (예: 전자신문)",
      "url": "해당 뉴스 기사의 실제 URL (없으면 빈 문자열)"
    }}
  ]
}}

출력 규칙(강제):
- ai_news는 3~5건.
- companies는 3~5개 기업, 통신3사(LGU+/KT/SKT) 제외.
- companies·telco의 모든 *_url/url은 위에서 제공된 실제 기사 URL만 입력, 없으면 "".
- quality 사례가 없으면 [{{"type":"해당없음","company":"","desc":"관련 동향 없음"}}]로 반환.
- 해당 섹션에 기사가 부족하면 "관련 동향 없음"으로 명시.
- 최종 출력은 JSON 객체 하나만 반환.
- 설명 텍스트, 사과 문구, 마크다운은 절대 출력 금지."""

    try:
        raw = await asyncio.wait_for(call_claude(SYSTEM, USER), timeout=100)
    except asyncio.TimeoutError:
        print("❌ Claude API 타임아웃 (100초 초과)")
        raise

    # JSON 파싱
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        clean = raw[start:end]
        return json.loads(clean)
    except ValueError:
        print(f"JSON 블록 없음 - 원문:\n{raw[:800]}")
        raise RuntimeError("Claude가 JSON을 반환하지 않았습니다.")
    except json.JSONDecodeError as e:
        print(f"JSON 파싱 실패: {e}\n원문: {raw[:500]}")
        raise

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
        print(f"[{now_kst()}] 브리핑 생성 실패 ❌: {e}")

# ── 앱 시작/종료 ──────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) 파일 캐시 복구 시도
    if CACHE_FILE.exists():
        try:
            cache["briefing"] = json.loads(CACHE_FILE.read_text())
            print("파일 캐시에서 브리핑 복구 완료")
        except Exception:
            pass

    # 2) 캐시가 없으면 서버 시작 시 즉시 브리핑 생성 (백그라운드)
    if cache["briefing"] is None:
        print("캐시 없음 → 브리핑 즉시 생성 시작 (백그라운드)...")
        asyncio.create_task(scheduled_task())

    # 3) 매주 월요일 09:00 스케줄 등록
    if os.environ.get("BRIEFING_ENABLED", "false").lower() == "true":
        scheduler.add_job(
            scheduled_task,
            CronTrigger(day_of_week="mon", hour=9, minute=0, timezone="Asia/Seoul")
        )
        print("브리핑 스케줄러 활성화 ✅ (매주 월요일 오전 9시)")
    else:
        print("브리핑 스케줄러 비활성화 ⏸ (BRIEFING_ENABLED=false)")

    # 4) 장애 모니터 30분마다 실행 + 시작 시 즉시 1회
    scheduler.add_job(monitor_task, "interval", minutes=30)
    asyncio.create_task(monitor_task())
    print("장애 모니터 스케줄러 활성화 ✅ (30분마다)")

    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(title="AI 뉴스 브리핑", lifespan=lifespan)

# ── 라우트 ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
@app.head("/")
async def index():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)

@app.get("/api/briefing")
async def get_briefing():
    if cache["briefing"] is None:
        return JSONResponse(
            {"status": "generating", "message": "브리핑 생성 중입니다. 잠시 후 새로고침해주세요."},
            status_code=202
        )
    return JSONResponse(cache["briefing"])

@app.post("/api/refresh")
async def refresh_briefing(background_tasks: BackgroundTasks):
    background_tasks.add_task(scheduled_task)
    return {"message": "브리핑 갱신을 시작했습니다."}

@app.get("/api/monitor")
async def get_monitor():
    if monitor_cache["results"] is None:
        return JSONResponse({"status": "checking"}, status_code=202)
    return JSONResponse(monitor_cache)

@app.get("/health")
@app.head("/health")
async def health():
    return {"status": "ok", "cached": cache["briefing"] is not None}
