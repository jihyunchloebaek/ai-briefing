import os
import json
import asyncio
import re
from datetime import datetime
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

# ── 캐시 ─────────────────────────────────────────────────────────────
cache: dict = {
    "briefing": None,
    "generated_at": None,
    "last_error": None,
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CACHE_FILE = Path("briefing_cache.json")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [기반] Claude API 호출 함수 (변경 없음)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def call_claude(system: str, user: str, max_tokens: int = 1200) -> str:
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
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
    result = re.sub(r'<cite[^>]*>(.*?)</cite>', r'\1', result, flags=re.DOTALL)
    print(f"API 응답 미리보기: {result[:200]}")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [신규] JSON 파싱 공통 함수
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def parse_json(raw: str) -> dict | list:
    """Claude 응답에서 JSON을 안전하게 추출"""
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
# [Worker 1] 통신3사 뉴스 수집
# 역할: SKT, KT, LGU+ 전용 수집만 담당
# 패턴: Reflection + 시간 자동 확장
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def worker_telco(today_str: str, weekday: str) -> dict:
    """
    [Reflection 적용]
    - 24시간 수집 시도
    - 각 통신사별 기사가 부족하면 시간 범위 자동 확장 (24h → 48h)
    - 비용 절감을 위해 최대 48시간까지만 확장
    """
    time_ranges = [24, 48]  # 토큰/비용 절감을 위해 확장 단계 축소

    for time_range in time_ranges:
        period_str = f"최근 {time_range}시간" if time_range <= 48 else f"최근 {time_range//24}일"
        print(f"[Worker 1: 통신3사] {period_str} 기준 수집 시도...")

        SYSTEM = """당신은 통신사 전문 뉴스 수집 AI입니다.
반드시 유효한 JSON만 출력하고 다른 텍스트는 절대 포함하지 마세요.

⚠️ 중요 규칙:
- SKT(SK텔레콤), KT, LG유플러스(LGU+) 관련 기사만 수집하세요.
- 네이버, 카카오, 삼성 등 다른 IT기업 기사는 절대 포함하지 마세요.
- 품질/AI/신사업 관련 기사를 우선 수집하세요."""

        USER = f"""{today_str}({weekday}) 기준 {period_str} 이내 통신3사 뉴스를 수집하세요.

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

동향 없으면 "관련 동향 없음". quality 없으면 type을 "해당없음"으로."""

        raw = await call_claude(SYSTEM, USER, max_tokens=900)
        result = parse_json(raw)

        # [Reflection] 코드가 직접 내용 확인 (Claude의 article_count 믿지 않음)
        # "관련 동향 없음"이 아닌 실제 내용이 있는지 코드가 직접 판단
        telco = result.get("telco", {})
        NO_NEWS = "관련 동향 없음"

        def has_news(company: str) -> bool:
            """해당 통신사의 3개 항목 중 하나라도 실제 내용이 있으면 True"""
            co = telco.get(company, {})
            return any(
                co.get(field, NO_NEWS) not in ("", NO_NEWS)
                for field in ["품질", "AI", "신사업"]
            )

        lgu_ok = has_news("LGU")
        kt_ok  = has_news("KT")
        skt_ok = has_news("SKT")

        if lgu_ok and kt_ok and skt_ok:
            print(f"[Worker 1: 통신3사] ✅ 전사 수집 완료 ({period_str})")
            return result
        else:
            missing = [c for c, ok in [("LGU", lgu_ok), ("KT", kt_ok), ("SKT", skt_ok)] if not ok]
            print(f"[Worker 1: 통신3사] ⚠️ {missing} 내용 없음 → 시간 범위 확장")

    print(f"[Worker 1: 통신3사] ❌ 최대 범위(48시간)에서도 부족. 있는 것만 반환")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Worker 2] 국내 IT기업 뉴스 수집
# 역할: 통신3사 제외한 IT기업만 담당
# 패턴: Routing(통신사 차단) + Reflection(시간 자동 확장)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def worker_companies(today_str: str, weekday: str) -> dict:
    """
    [Routing 적용]
    - 시스템 프롬프트에서 통신3사 기사 명시적 차단
    - Worker 1과 완전히 독립된 영역 담당

    [Reflection 적용]
    - 기업 수 부족 시 시간 범위 자동 확장
    """
    time_ranges = [24, 48]

    for time_range in time_ranges:
        period_str = f"최근 {time_range}시간" if time_range <= 48 else f"최근 {time_range//24}일"
        print(f"[Worker 2: 국내IT] {period_str} 기준 수집 시도...")

        SYSTEM = """당신은 국내 IT기업 전문 뉴스 수집 AI입니다.
반드시 유효한 JSON만 출력하고 다른 텍스트는 절대 포함하지 마세요.

⚠️ 핵심 규칙 (Routing):
- 네이버, 카카오, 쿠팡, 당근, 토스, 크래프톤 등 IT기업 기사만 수집하세요.
- SKT, KT, LG유플러스(LGU+) 관련 기사는 절대 포함하지 마세요.
- 통신사가 IT기업과 협력한 기사라도 → IT기업 관점 내용이 없으면 제외하세요."""

        USER = f"""{today_str}({weekday}) 기준 {period_str} 이내 국내 IT기업 뉴스를 수집하세요.
(통신3사 SKT/KT/LGU+ 제외)

아래 JSON 형식으로 반환:
{{
  "companies": [
    {{
      "name": "기업명",
      "summary": "오늘 뉴스 핵심 내용을 2~3문장으로 통합 요약",
      "url": "해당 기업 관련 뉴스 기사 URL (없으면 빈 문자열)"
    }}
  ]
}}

최대한 많은 기업 포함. 동향 없는 기업은 제외."""

        raw = await call_claude(SYSTEM, USER, max_tokens=1100)
        result = parse_json(raw)

        # [Routing 보정] 코드가 직접 통신사 제거
        # Claude가 프롬프트 지시를 무시하고 통신사를 포함시켰을 경우 사후 차단
        TELCOS = ["SKT", "KT", "LGU+", "LG유플러스", "SK텔레콤"]
        companies_raw = result.get("companies", [])
        companies = [
            c for c in companies_raw
            if not any(t in c.get("name", "") for t in TELCOS)
        ]
        removed = len(companies_raw) - len(companies)
        if removed > 0:
            print(f"[Worker 2: 국내IT] ⚠️ 통신사 {removed}개 제거됨 (Routing 보정)")
        result["companies"] = companies

        # [Reflection] 코드가 직접 리스트 길이 확인 (Claude의 article_count 믿지 않음)
        count = len(companies)  # Python이 직접 셈!
        if count >= 2:
            print(f"[Worker 2: 국내IT] ✅ {count}개 기업 수집 완료 ({period_str})")
            return result
        else:
            print(f"[Worker 2: 국내IT] ⚠️ {count}개 기업만 수집 → 시간 범위 확장")

    print(f"[Worker 2: 국내IT] ❌ 최대 범위(48시간)에서도 부족. 있는 것만 반환")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Worker 3] 국내외 AI 뉴스 수집
# 역할: AI 산업 전반 뉴스만 담당
# 패턴: Reflection(시간 자동 확장)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def worker_ai_news(today_str: str, weekday: str) -> dict:
    """
    [Reflection 적용]
    - AI 뉴스 3건 미만이면 시간 범위 자동 확장
    """
    time_ranges = [24, 48]

    for time_range in time_ranges:
        period_str = f"최근 {time_range}시간" if time_range <= 48 else f"최근 {time_range//24}일"
        print(f"[Worker 3: AI뉴스] {period_str} 기준 수집 시도...")

        SYSTEM = """당신은 AI 산업 전문 뉴스 수집 AI입니다.
반드시 유효한 JSON만 출력하고 다른 텍스트는 절대 포함하지 마세요.
에이전틱 AI, LLM, AI 정책, AI 연구 등 AI 산업 전반의 중요 뉴스를 수집하세요."""

        USER = f"""{today_str}({weekday}) 기준 {period_str} 이내 국내외 AI 산업 뉴스를 수집하세요.

아래 JSON 형식으로 반환:
{{
  "ai_news": [
    {{
      "title": "뉴스 제목",
      "bullets": ["🔑 핵심 포인트 1", "📊 핵심 포인트 2", "💡 핵심 포인트 3"],
      "source": "출처 (예: MIT Technology Review)",
      "url": "해당 뉴스 기사의 실제 URL (없으면 빈 문자열)"
    }}
  ]
}}

3~5건 수집 목표."""

        raw = await call_claude(SYSTEM, USER, max_tokens=1300)
        result = parse_json(raw)

        # [Reflection] 코드가 직접 리스트 길이 확인 (Claude의 article_count 믿지 않음)
        ai_news = result.get("ai_news", [])
        count = len(ai_news)  # Python이 직접 셈!
        if count >= 3:
            print(f"[Worker 3: AI뉴스] ✅ {count}건 수집 완료 ({period_str})")
            return result
        else:
            print(f"[Worker 3: AI뉴스] ⚠️ {count}건만 수집 → 시간 범위 확장")

    print(f"[Worker 3: AI뉴스] ❌ 최대 범위(48시간)에서도 부족. 있는 것만 반환")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Worker 4] 해외 빅테크 뉴스 수집
# 역할: Google, MS, OpenAI 등 해외 빅테크만 담당
# 패턴: Reflection(시간 자동 확장)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def worker_bigtech(today_str: str, weekday: str) -> dict:
    """
    [Reflection 적용]
    - 빅테크 뉴스 2건 미만이면 시간 범위 자동 확장
    """
    time_ranges = [24, 48]

    for time_range in time_ranges:
        period_str = f"최근 {time_range}시간" if time_range <= 48 else f"최근 {time_range//24}일"
        print(f"[Worker 4: 빅테크] {period_str} 기준 수집 시도...")

        SYSTEM = """당신은 해외 빅테크 전문 뉴스 수집 AI입니다.
반드시 유효한 JSON만 출력하고 다른 텍스트는 절대 포함하지 마세요.
Google, Microsoft, OpenAI, Meta, Apple, Amazon, NVIDIA 등 해외 빅테크 동향을 수집하세요."""

        USER = f"""{today_str}({weekday}) 기준 {period_str} 이내 해외 빅테크 뉴스를 수집하세요.

아래 JSON 형식으로 반환:
{{
  "bigtech": [
    {{
      "name": "기업명",
      "summary": "핵심 뉴스 2~3문장 요약",
      "url": "기사 URL (없으면 빈 문자열)"
    }}
  ]
}}"""

        raw = await call_claude(SYSTEM, USER, max_tokens=1000)
        result = parse_json(raw)

        # [Reflection] 코드가 직접 리스트 길이 확인 (Claude의 article_count 믿지 않음)
        bigtech = result.get("bigtech", [])
        count = len(bigtech)  # Python이 직접 셈!
        if count >= 2:
            print(f"[Worker 4: 빅테크] ✅ {count}건 수집 완료 ({period_str})")
            return result
        else:
            print(f"[Worker 4: 빅테크] ⚠️ {count}건만 수집 → 시간 범위 확장")

    print(f"[Worker 4: 빅테크] ❌ 최대 범위(48시간)에서도 부족. 있는 것만 반환")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Worker 5] 키워드 + 전체 요약 생성
# 역할: 4개 Worker 결과를 받아 키워드와 요약문 생성
# 패턴: Chaining (앞 단계 결과를 입력으로 받음)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def worker_summary(all_data: dict) -> dict:
    """
    [Chaining 적용]
    - Worker 1~4의 결과물을 입력으로 받아서
    - 오늘의 키워드 5개 + 전체 요약 3문장 생성
    """
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

    raw = await call_claude(SYSTEM, USER, max_tokens=500)
    result = parse_json(raw)
    print(f"[Worker 5: 요약] ✅ 완료")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Orchestrator] 전체 브리핑 생성 총괄
# 역할: Worker 1~5를 조율하여 최종 결과 조합
# 패턴: Parallelization(Worker 1~4 동시 실행) + Chaining(Worker 5)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def generate_briefing() -> dict:
    now = now_kst()
    today_str = now.strftime("%Y년 %m월 %d일")
    weekday = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]

    print(f"\n{'='*50}")
    print(f"[Orchestrator] 브리핑 생성 시작: {today_str} ({weekday})")
    print(f"{'='*50}")

    # ── Step 1: Worker 1~4 동시 실행 (Parallelization) ──────────────
    # 4개 영역을 순서대로 하지 않고 동시에 실행!
    # 통신3사(Worker1)와 국내IT(Worker2)는 완전히 독립된 프롬프트
    # → Routing으로 중복 차단
    print(f"\n[Orchestrator] Step 1: 4개 영역 동시 수집 시작 (Parallelization)")
    async def run_worker(label: str, fn, fallback: dict) -> dict:
        try:
            return await fn
        except Exception as e:
            print(f"[{label}] ❌ 수집 실패: {e}")
            return fallback

    telco_result, companies_result, ai_result, bigtech_result = await asyncio.gather(
        run_worker("Worker 1: 통신3사", worker_telco(today_str, weekday), {"telco": {}, "quality": []}),
        run_worker("Worker 2: 국내IT", worker_companies(today_str, weekday), {"companies": []}),
        run_worker("Worker 3: AI뉴스", worker_ai_news(today_str, weekday), {"ai_news": []}),
        run_worker("Worker 4: 빅테크", worker_bigtech(today_str, weekday), {"bigtech": []}),
    )
    print(f"[Orchestrator] Step 1 완료 ✅")

    # ── Step 2: Worker 5로 요약 생성 (Chaining) ─────────────────────
    # Step 1 결과물을 입력으로 받아 키워드 + 요약 생성
    print(f"\n[Orchestrator] Step 2: 전체 요약 생성 (Chaining)")
    all_collected = {
        "telco": telco_result.get("telco", {}),
        "companies": companies_result.get("companies", [])[:5],
        "ai_news": ai_result.get("ai_news", [])[:5],
        "bigtech": bigtech_result.get("bigtech", [])[:5],
    }
    try:
        summary_result = await worker_summary(all_collected)
    except Exception as e:
        print(f"[Worker 5: 요약] ❌ 생성 실패: {e}")
        summary_result = {"keywords": [], "summary": ["요약 생성에 실패했습니다. 원본 기사 링크를 확인해주세요."]}
    print(f"[Orchestrator] Step 2 완료 ✅")

    # ── Step 3: 최종 결과 조합 ───────────────────────────────────────
    print(f"\n[Orchestrator] Step 3: 최종 결과 조합")
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

    print(f"[Orchestrator] ✅ 브리핑 생성 완료!")
    print(f"{'='*50}\n")
    return briefing


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 이하 스케줄러, FastAPI 라우트는 기존과 동일
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
            {
                "status": "generating",
                "message": "브리핑 생성 중입니다. 잠시 후 새로고침해주세요.",
                "last_error": cache["last_error"],
            },
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
