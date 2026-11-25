# -*- coding: utf-8 -*-
"""
🧠 비서실장 대시보드  (1인 사업용 · Streamlit 초안)

구조
    🧠 비서실장(총괄)  : 한 줄 지시 → 업무 분석(JSON) → 확인 질문 → 직원 3인 배정 → 최종 브리핑
    🔍 시장조사·리서치  : Tavily 웹 검색(키가 있을 때) 또는 LLM 자체 지식으로 조사 보고서
    📄 사업계획·문서    : 지원사업 계획서 초안 / B2B 제안서 / 손익·예산표
    ✉️ 영업·콘텐츠      : 영문·국문 영업 메일 / 팔로업 메일 / 블로그·유튜브 대본

실행
    pip install -r requirements.txt
    streamlit run app.py

API 키
    사이드바에 직접 입력하거나, .streamlit/secrets.toml / Streamlit Cloud Secrets 에
        OPENAI_API_KEY    = "sk-..."
        ANTHROPIC_API_KEY = "sk-ant-..."
        TAVILY_API_KEY    = "tvly-..."     (선택)
    을 넣어 두면 자동으로 불러옵니다.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime

import streamlit as st

# ---------------------------------------------------------------------------
# 1. 기본 설정
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="🧠 비서실장 대시보드",
    page_icon="🧠",
    layout="wide",
)

DEFAULT_KRW_PER_USD = 1400.0  # 브리핑 기준 환율 (사이드바에서 바꿀 수 있음)

# 모델별 단가 (USD / 100만 토큰).  ※ 2026년 기준 공개 단가, 바뀌면 여기만 고치면 됩니다.
MODELS: dict[str, dict[str, dict[str, float]]] = {
    "OpenAI": {
        "gpt-4o-mini":    {"in": 0.15,  "out": 0.60},
        "gpt-4.1-mini":   {"in": 0.40,  "out": 1.60},
        "gpt-4o":         {"in": 2.50,  "out": 10.00},
        "gpt-4.1":        {"in": 2.00,  "out": 8.00},
    },
    "Anthropic (Claude)": {
        "claude-haiku-4-5":  {"in": 1.00,  "out": 5.00},
        "claude-sonnet-4-5": {"in": 3.00,  "out": 15.00},
        "claude-opus-4-1":   {"in": 15.00, "out": 75.00},
    },
}

DEFAULT_MODEL = {
    "OpenAI": "gpt-4o-mini",
    "Anthropic (Claude)": "claude-sonnet-4-5",
}

EMPLOYEES: dict[str, dict[str, str]] = {
    "research": {
        "emoji": "🔍",
        "name": "시장조사·리서치 담당",
        "desc": "지원사업 공고, 시장 규모, 경쟁사·가격, 규제·유통 동향을 출처와 함께 정리",
    },
    "business_plan": {
        "emoji": "📄",
        "name": "사업계획·문서 담당",
        "desc": "지원사업 계획서 초안, B2B 제안서, 손익·예산표 작성",
    },
    "sales_content": {
        "emoji": "✉️",
        "name": "영업·콘텐츠 담당",
        "desc": "영문·국문 영업 메일, 팔로업 메일, 블로그·유튜브 대본",
    },
}

ORG_CHART = [
    ("🧠", "비서실장 (총괄)", "한 줄 지시 분석 · 업무 배분 · 확인 질문 · 최종 브리핑"),
    ("🔍", "시장조사·리서치", "공고·시장·경쟁사 조사 (웹 검색 연동)"),
    ("📄", "사업계획·문서", "계획서·제안서·손익표 작성"),
    ("✉️", "영업·콘텐츠", "영업 메일·팔로업·대본 작성"),
]


# ---------------------------------------------------------------------------
# 2. 프롬프트
# ---------------------------------------------------------------------------
CHIEF_SYSTEM = """당신은 Berry 님(1인 사업자)의 '총괄 비서실장'입니다.
Berry 님은 지원사업 신청, B2B 제안·총판 협업, 온라인 콘텐츠·외주 사업을 준비하고 있습니다.
사용자의 한 줄 지시를 받으면 아래 3명의 AI 직원에게 일을 나눠 배정하고, 마지막에 결과를 종합합니다.

[직원]
1) research        🔍 시장조사·리서치 담당 — 시장 규모, 정부·지자체 지원사업 공고, 경쟁사·가격, 규제, 유통·외주 수요 조사
2) business_plan   📄 사업계획·문서 담당 — 지원사업 계획서 초안, B2B 제안서, 손익·예산표
3) sales_content   ✉️ 영업·콘텐츠 담당 — 영문/국문 영업 메일, 팔로업 메일, 블로그·유튜브 대본, 홍보 문구

[출력 규칙]
반드시 아래 형식의 JSON 객체 하나만 출력하세요. 설명 문장, 인사말, 코드블록 표시(```)를 붙이지 마세요.

{
  "needs_clarification": false,
  "questions": [],
  "summary": "지시를 한 문장으로 요약",
  "tasks": {
    "research": {"assign": true, "instruction": "이 직원에게 내릴 구체적 지시"},
    "business_plan": {"assign": false, "instruction": ""},
    "sales_content": {"assign": false, "instruction": ""}
  },
  "final_briefing_focus": "최종 브리핑에서 특히 강조할 점"
}

[판단 기준]
- 답에 따라 결과가 크게 달라지는 핵심 조건이 비어 있을 때만 needs_clarification을 true로 하고,
  questions에 최대 3개까지 한국어 질문을 담습니다. (예: 예산 규모, 목표 시장 국내/해외, 마감일, 사업 아이템의 범위)
- 이미 충분히 구체적인 지시라면 needs_clarification은 false, questions는 빈 배열([])로 둡니다.
- 꼭 필요하지 않은 직원은 assign을 false로 둡니다. 지시가 아주 모호하면 research만이라도 배정합니다.
- instruction은 그 직원이 바로 일을 시작할 수 있도록 한국어로 구체적으로 씁니다.
- 질문은 예/아니오보다 짧은 서술로 답할 수 있는 형태로 씁니다."""

QUERY_SYSTEM = """당신은 시장조사 담당 AI 직원의 '검색어 설계자'입니다.
주어진 업무 지시를 조사하기 위해 실제 웹 검색에 넣을 한국어 검색어를 3개 만드세요.
각 검색어는 서로 다른 각도(예: 지원사업 공고 / 시장·경쟁사 / 규제·유통)를 다뤄야 합니다.
반드시 아래 JSON만 출력하세요: {"queries": ["검색어1", "검색어2", "검색어3"]}"""

RESEARCH_SYSTEM = """당신은 Berry 님의 '시장조사·리서치 담당' AI 직원입니다.
주어진 지시와 (있다면) 웹 검색 결과를 바탕으로, 사업 판단에 바로 쓸 수 있는 리서치 보고서를 한국어 마크다운으로 작성하세요.

[출력 형식]
## 🔍 리서치 요약
- 핵심 결론 3줄 (각 줄은 '~입니다'로 끝나는 완결 문장)

## 📊 한눈에 보는 표
| 항목 | 내용 | 근거·출처 |
|---|---|---|
| 시장·수요 | ... | [1] |
| 경쟁·가격 | ... | [2] |
| 지원사업·공고 | ... | [3] |
| 규제·유통 | ... | - |
(표는 4~6행, 모르는 칸은 '확인 필요'로 표시)

## 📌 상세 내용
- 항목별로 2~4줄씩 설명. 웹 검색 근거가 있으면 문장 끝에 [1], [2] 번호를 붙입니다.

## 💡 Berry 님 사업에 주는 시사점
- 2~3개

## ⚠️ 자료의 한계 / 확인이 필요한 점
- 웹 검색 결과 없이 자체 지식으로 작성했다면 '최신 공고·가격은 달라질 수 있어 원문 확인이 필요합니다'를 반드시 명시
- 확인이 필요한 항목을 2~3개

## 🔗 출처
- 검색 결과를 썼을 때만 [1] 제목 — URL 형식으로 나열
- 검색 결과가 없으면 '웹 검색 미사용(자체 지식 기반)'이라고만 적습니다.

[주의]
- 확인되지 않은 숫자를 사실처럼 쓰지 말고 '추정' 또는 '확인 필요'로 표시하세요.
- 표와 목록 위주로, 길게 늘어놓지 마세요."""

BUSINESS_SYSTEM = """당신은 Berry 님의 '사업계획·문서 담당' AI 직원입니다.
리서치 보고서와 비서실장 지시를 바탕으로, 실제로 제출·발송할 수 있는 수준의 초안 문서를 한국어 마크다운으로 작성하세요.
지시에 맞춰 아래 중 필요한 것만 만드세요.

[지원사업 계획서 초안]
## 📄 사업계획서 초안 — (사업명)
1. 사업 개요 (한 문단)
2. 문제 정의와 필요성
3. 솔루션·제품 소개
4. 시장 규모와 목표 고객
5. 차별성·경쟁 우위
6. 실행 계획 (3~6개월 로드맵 표)
7. 예산 계획 (표: 항목 / 금액 / 산출 근거)
8. 기대 효과 (정량·정성)
9. 🔲 확인 필요 (공고문에서 확인해야 할 자격·양식·마감)

[B2B 제안서]
## 🤝 제안서 초안 — (제안 대상)
1. 제안 배경 (상대의 이익 관점)
2. 제안 내용 (범위·산출물)
3. 가격·일정 (표)
4. 기대 효과
5. 다음 단계

[손익·예산 시뮬레이션]
## 💰 손익·예산표
| 항목 | 월/건 | 단가 | 금액 | 비고 |
|---|---|---|---|---|
...
- 손익분기점, 3가지 시나리오(보수/기본/공격)를 표로
- 모든 숫자 아래에 '가정'을 한 줄로 명시

[주의]
- 숫자는 반드시 '가정'임을 표시하고, 확인되지 않은 수치는 '확인 필요'로 남깁니다.
- 문장은 짧게, 실무자가 바로 고쳐 쓸 수 있게 씁니다. 모르는 내용을 지어내지 않습니다."""

SALES_SYSTEM = """당신은 Berry 님의 '영업·콘텐츠 담당' AI 직원입니다.
리서치·사업계획 결과를 바탕으로 실제로 보낼 수 있는 영업 메일과 콘텐츠 초안을 한국어/영어로 작성하세요.
지시에 맞춰 필요한 것만 만드세요.

[영업 메일]
## ✉️ 영업 메일 (국문)
**제목:** ...
본문: 인사 → 용건(첫 문장) → Berry 님이 제공하는 가치 2~3줄 → 제안(짧은 미팅) → 마무리
서명: Berry

## ✉️ Sales Email (English)
**Subject:** ...
Body: 3~5 short paragraphs, no hype, one clear ask.
Signature: Berry

[팔로업 메일]
## 🔁 팔로업 메일
- 1차(3일 후) / 2차(7일 후) 두 가지 버전, 각 3~5문장

[콘텐츠]
## 📝 블로그 글 초안 — (제목)
- 도입 / 본문 3개 소제목 / 마무리 CTA, 800~1,200자
## 🎬 유튜브 대본 (3~5분)
- 오프닝 훅(15초) / 본문 3개 챕터 / 클로징 CTA, 말하듯이

[주의]
- 과장·허위 실적 금지. 확인되지 않은 수치는 쓰지 않습니다.
- 받는 사람이 30초 안에 용건을 알 수 있게 씁니다."""

FINAL_SYSTEM = """당신은 Berry 님의 '총괄 비서실장'입니다.
직원 3명의 보고서를 종합해, Berry 님이 5분 안에 판단하고 움직일 수 있는 최종 브리핑을 한국어 마크다운으로 쓰세요.

[출력 형식]
## 🧠 최종 브리핑
### ✅ 결론 (3줄 이내)
### 🗂️ 직원별 핵심
- 🔍 리서치: (2줄)
- 📄 사업계획: (2줄)
- ✉️ 영업·콘텐츠: (2줄)
### ▶️ 다음 액션 (체크리스트)
- [ ] 할 일 — 담당 / 기한(가능하면)
### ❓ 결정이 필요한 것
- Berry 님이 정해야 할 것 1~3개 (각각 선택지와 추천)
### 💰 이번 작업 비용
- (전달받은 비용 정보를 그대로 한 줄)

[주의]
- 직원 보고서에 없는 내용을 새로 지어내지 않습니다.
- 실패한 직원이 있으면 '해당 보고서는 생성되지 않았습니다'라고 분명히 적습니다."""


# ---------------------------------------------------------------------------
# 3. 공통 도구
# ---------------------------------------------------------------------------
def secret(name: str, default: str = "") -> str:
    """Streamlit Secrets 에서 값을 안전하게 읽습니다(파일이 없어도 오류 없음)."""
    try:
        value = st.secrets.get(name, default)  # type: ignore[attr-defined]
        return str(value) if value else default
    except Exception:
        return default


class CostTracker:
    """API 호출마다 토큰을 기록하고 원/달러 비용을 계산합니다."""

    def __init__(self, krw_per_usd: float = DEFAULT_KRW_PER_USD) -> None:
        self.krw_per_usd = float(krw_per_usd)
        self.calls: list[dict] = []

    def add(self, employee: str, provider: str, model: str,
            input_tokens: int, output_tokens: int) -> float:
        price = MODELS.get(provider, {}).get(model, {"in": 0.0, "out": 0.0})
        usd = (input_tokens / 1_000_000) * price["in"] + (output_tokens / 1_000_000) * price["out"]
        self.calls.append({
            "담당": employee,
            "모델": model,
            "입력 토큰": int(input_tokens),
            "출력 토큰": int(output_tokens),
            "비용(USD)": round(usd, 6),
            "비용(원)": round(usd * self.krw_per_usd, 2),
        })
        return usd

    def totals(self) -> dict:
        usd = sum(c["비용(USD)"] for c in self.calls)
        tokens = sum(c["입력 토큰"] + c["출력 토큰"] for c in self.calls)
        return {
            "usd": usd,
            "krw": usd * self.krw_per_usd,
            "tokens": tokens,
            "calls": len(self.calls),
        }

    # Streamlit 은 실행할 때마다 스크립트를 새로 읽어 클래스도 새로 정의되므로,
    # 세션에는 객체 대신 '순수 dict' 로 저장해 이어 씁니다.
    def to_dict(self) -> dict:
        return {"krw_per_usd": self.krw_per_usd, "calls": [dict(c) for c in self.calls]}

    @classmethod
    def from_dict(cls, data: dict) -> "CostTracker":
        tracker = cls(float(data.get("krw_per_usd", DEFAULT_KRW_PER_USD)))
        tracker.calls = [dict(c) for c in (data.get("calls") or [])]
        return tracker


def friendly_error(exc: Exception) -> str:
    """API 오류를 Berry 님이 읽을 수 있는 한국어 안내로 바꿉니다."""
    msg = str(exc)
    low = msg.lower()
    if "api key" in low or "authentication" in low or "invalid_api_key" in low or "401" in low:
        return ("API 키 인증에 실패했습니다. 사이드바의 키가 정확한지, 결제(크레딧)가 충전된 계정인지 확인한 뒤 "
                "다시 실행해 주세요.")
    if "quota" in low or "insufficient" in low or "billing" in low or "credit" in low:
        return ("API 사용 한도나 크레딧이 부족합니다. OpenAI/Anthropic 콘솔에서 결제 정보와 잔액을 확인해 주세요.")
    if "rate" in low and "limit" in low or "429" in low:
        return "요청이 잠시 몰렸습니다(속도 제한). 30초쯤 뒤에 다시 실행해 주세요."
    if "model" in low and ("not found" in low or "does not exist" in low or "invalid" in low):
        return "선택한 모델을 사용할 수 없습니다. 사이드바에서 다른 모델을 골라 주세요."
    if "connection" in low or "timed out" in low or "timeout" in low:
        return "인터넷 연결이 불안정합니다. 잠시 뒤 다시 시도해 주세요."
    return f"오류가 발생했습니다: {msg[:400]}"


def extract_json(text: str):
    """LLM 응답에서 JSON 객체를 최대한 안전하게 꺼냅니다."""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
    cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start:end + 1]
        for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                return json.loads(attempt)
            except Exception:
                continue
    return None


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n…(길이 관계로 이하 생략)"


def fmt_won(value: float) -> str:
    """금액 표시: 100원 미만은 소수 첫째 자리까지 보여 줍니다."""
    value = float(value or 0)
    if value >= 100:
        return f"{value:,.0f}원"
    return f"{value:,.1f}원"


def render_cost_area() -> dict:
    """사이드바의 실시간 비용 위젯을 만들고 placeholder 를 돌려줍니다."""
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 💰 이번 작업 예상 비용")
    col1, col2 = st.sidebar.columns(2)
    ph_krw = col1.empty()
    ph_usd = col2.empty()
    ph_meta = st.sidebar.empty()
    ph_krw.metric("원 (KRW)", "0원")
    ph_usd.metric("달러 (USD)", "$0.0000")
    ph_meta.caption("아직 API 호출이 없습니다.")
    return {"krw": ph_krw, "usd": ph_usd, "meta": ph_meta}


def refresh_cost(area: dict, tracker: CostTracker) -> None:
    t = tracker.totals()
    area["krw"].metric("원 (KRW)", fmt_won(t["krw"]))
    area["usd"].metric("달러 (USD)", f"${t['usd']:.4f}")
    area["meta"].caption(
        f"호출 {t['calls']}회 · 토큰 {t['tokens']:,}개 · 환율 {tracker.krw_per_usd:,.0f}원/$"
    )


# ---------------------------------------------------------------------------
# 4. LLM 호출 (OpenAI / Anthropic / 데모)
# ---------------------------------------------------------------------------
def _demo_text(role: str, user_prompt: str, cfg: dict) -> str:
    """데모 모드: API 를 쓰지 않고 화면 흐름을 보여 주는 예시 응답."""
    stamp = "※ 데모 모드 예시 데이터입니다. 실제 조사·문서가 아닙니다."
    if role == "planner":
        # 지시가 아주 짧으면 '확인 질문' 단계를 한 번 보여 줍니다(데모).
        found = re.search(r"\[Berry 님의 지시\]\s*\n(.+)", user_prompt)
        instruction = (found.group(1) if found else user_prompt).strip()
        if len(instruction) < 15 and "추가 답변" not in user_prompt:
            return json.dumps({
                "needs_clarification": True,
                "questions": ["이번 작업의 예산 규모는 어느 정도로 잡을까요?",
                              "목표 시장은 국내인가요, 해외인가요?"],
                "summary": "지시가 짧아 핵심 조건을 먼저 확인합니다. (데모)",
                "tasks": {
                    "research": {"assign": True, "instruction": "관련 시장·공고 조사"},
                    "business_plan": {"assign": True, "instruction": "계획서 초안 작성"},
                    "sales_content": {"assign": False, "instruction": ""},
                },
                "final_briefing_focus": "다음 액션",
            }, ensure_ascii=False)
        payload = {
            "needs_clarification": False,
            "questions": [],
            "summary": "지원사업 공고 조사 → 계획서 초안 → 영업 메일까지 3명에게 배분했습니다. (데모)",
            "tasks": {
                "research": {"assign": True, "instruction": "관련 지원사업 공고 3건과 경쟁사 가격 동향을 표로 정리"},
                "business_plan": {"assign": True, "instruction": "2027년 지원사업 신청용 계획서 초안과 6개월 예산표 작성"},
                "sales_content": {"assign": True, "instruction": "국내 유통사 2곳에 보낼 제안 메일과 영문 버전 작성"},
            },
            "final_briefing_focus": "마감일과 다음 액션",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if role == "research_queries":
        return json.dumps({"queries": ["2027 예비창업패키지 공고 일정", "AI 문서 자동화 외주 단가", "1인 사업 지원사업 자격"]},
                          ensure_ascii=False)
    if role == "research":
        return f"""## 🔍 리서치 요약
- 2027년 지원사업은 상반기 공고가 몰려 있어 12월에 서류를 미리 준비해야 합니다.
- 소규모 B2B 문서 자동화 외주 단가는 건당 30만~120만 원대에 형성되어 있습니다.
- 1인 사업자도 신청 가능한 공고가 다수이나, 사업자등록·4대보험 가입 여부 확인이 필요합니다.

## 📊 한눈에 보는 표
| 항목 | 내용 | 근거·출처 |
|---|---|---|
| 지원사업·공고 | 예비창업패키지·지역 창업지원 사업 상반기 집중 (예시) | [1] |
| 시장·수요 | 중소기업 문서 자동화 수요 증가 (예시) | [2] |
| 경쟁·가격 | 외주 단가 건당 30만~120만 원 (예시) | [2] |
| 규제·유통 | 개인정보 처리 위탁 시 계약서 필요 (확인 필요) | - |

## 📌 상세 내용
- 지원사업: 공고문마다 자격·양식이 달라 최소 2주 전 준비가 안전합니다. [1]
- 가격: 단가 편차가 커서 산출물 범위를 먼저 정하는 것이 유리합니다. [2]

## 💡 Berry 님 사업에 주는 시사점
- 공고 3건을 미리 추려 두면 마감 직전에 몰리지 않습니다.
- 가격표는 '기본형/확장형' 2단 구성이 협상에 유리합니다.

## ⚠️ 자료의 한계 / 확인이 필요한 점
- {stamp}
- 실제 공고문 원문과 최신 단가 확인이 필요합니다.

## 🔗 출처
- (데모) 웹 검색 미사용 — 예시 데이터"""
    if role == "business_plan":
        return f"""## 📄 사업계획서 초안 — 1인 사업 AI 문서 자동화 (데모)
1. **사업 개요** 소상공인·중소기업의 반복 문서 업무를 AI로 자동화하는 1인 사업입니다.
2. **문제 정의** 견적서·보고서 작성에 주당 5~10시간이 소모됩니다.
3. **솔루션** 업종별 템플릿 + AI 초안 생성 + 검수 대행.
4. **시장** 국내 소상공인 약 700만 사업체 중 문서 자동화 도입률은 낮습니다(추정).
5. **차별성** 1인 담당자 밀착형, 납기 3일.
6. **실행 계획**

| 기간 | 할 일 |
|---|---|
| 1개월 | 샘플 3건 제작·가격표 확정 |
| 2~3개월 | 거래처 5곳 확보 |
| 4~6개월 | 월 매출 300만 원 안정화 |

7. **예산 계획**

| 항목 | 금액(월) | 산출 근거 |
|---|---|---|
| API·툴 | 15만 원 | 월 10건 기준(가정) |
| 광고 | 20만 원 | 소액 테스트(가정) |
| 합계 | 35만 원 | - |

8. **기대 효과** 월 300만 원 매출, 주 10시간 절감(가정).
9. **🔲 확인 필요** 공고문 자격 요건, 제출 양식, 마감일.

{stamp}"""
    if role == "sales_content":
        return f"""## ✉️ 영업 메일 (국문)
**제목:** 견적서·보고서 작성, 3일 안에 끝내 드립니다

안녕하세요, ○○ 담당자님. Berry입니다.
반복되는 견적서·보고서 작성에 매주 몇 시간을 쓰고 계신다면, 초안 작성과 서식 정리를 대신 맡겠습니다.
첫 건은 샘플로 먼저 보여 드리고, 마음에 드실 때 진행하겠습니다.
이번 주 20분 통화 가능하실까요?

감사합니다.
Berry

## ✉️ Sales Email (English)
**Subject:** Cut document prep time to 3 days

Hello, this is Berry.
I help small teams turn quotes and reports around in three days instead of three weeks.
Happy to send a free sample first — would a 20-minute call this week work?

Best,
Berry

## 🔁 팔로업 메일
- 1차(3일 후): 지난 메일 짧게 상기 + 샘플 1건 첨부
- 2차(7일 후): "지금은 필요 없으시면 6개월 뒤에 다시 연락드리겠습니다"로 부담 없이 마무리

## 📝 블로그 글 초안 — 견적서 3일 컷, 어떻게 가능할까
(도입) 문서 업무에 쓰는 시간을 줄이면 영업 시간이 늘어납니다. / (본문 3개 소제목) 템플릿, AI 초안, 검수 / (마무리) 무료 샘플 신청 CTA

{stamp}"""
    if role == "final":
        return f"""## 🧠 최종 브리핑 (데모)
### ✅ 결론
- 상반기 지원사업 공고에 맞춰 12월 안에 서류를 준비하는 것이 가장 빠릅니다.
- 문서 자동화 외주는 '기본형/확장형' 2단 가격표로 시작하는 것이 안전합니다.
- 영업은 거래처 5곳에 샘플 1건과 함께 제안하는 방식이 반응이 좋습니다.

### 🗂️ 직원별 핵심
- 🔍 리서치: 공고는 상반기 집중, 외주 단가는 건당 30만~120만 원대(예시).
- 📄 사업계획: 6개월 로드맵과 월 35만 원 예산안 초안을 만들었습니다.
- ✉️ 영업·콘텐츠: 국문·영문 제안 메일과 팔로업 2종, 블로그 초안을 만들었습니다.

### ▶️ 다음 액션
- [ ] 공고 3건 원문 확인 — Berry / 12월 1주
- [ ] 가격표 2단 구성 확정 — Berry / 12월 2주
- [ ] 거래처 5곳에 제안 메일 발송 — Berry / 12월 2주

### ❓ 결정이 필요한 것
- 목표 시장을 국내로 좁힐지, 영문 제안까지 병행할지 (추천: 국내 먼저)

### 💰 이번 작업 비용
- (아래 표 참고)

{stamp}"""
    return "(데모 응답)"


def _real_call(role: str, system: str, user: str, cfg: dict,
               max_tokens: int, json_mode: bool) -> tuple[str, int, int]:
    provider = cfg["provider"]
    model = cfg["model"]
    api_key = cfg["api_key"]

    if provider == "OpenAI":
        from openai import OpenAI  # 지연 임포트: 필요한 제공사만 로드

        client = OpenAI(api_key=api_key, max_retries=1, timeout=120.0)
        kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            # 최신 추론형 모델은 max_tokens 대신 max_completion_tokens 를 씁니다.
            if "max_tokens" in str(exc) and "max_completion_tokens" in str(exc):
                kwargs.pop("max_tokens", None)
                kwargs["max_completion_tokens"] = max_tokens
                resp = client.chat.completions.create(**kwargs)
            else:
                raise
        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
        return text, in_tok, out_tok

    # Anthropic (Claude)
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, max_retries=1, timeout=120.0)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.3,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    parts = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    text = "".join(parts).strip()
    usage = getattr(resp, "usage", None)
    in_tok = int(getattr(usage, "input_tokens", 0) or 0)
    out_tok = int(getattr(usage, "output_tokens", 0) or 0)
    return text, in_tok, out_tok


def call_llm(role: str, system: str, user: str, cfg: dict, tracker: CostTracker,
             cost_area: dict, max_tokens: int = 2000, json_mode: bool = False) -> str:
    """한 번의 API 호출을 수행하고, 사용량을 비용에 기록한 뒤 화면을 갱신합니다."""
    if not cfg["demo"] and not cfg.get("api_key"):
        raise RuntimeError("API 키가 없습니다. 사이드바에 API 키를 입력하거나 데모 모드를 켜 주세요.")
    if cfg["demo"]:
        text = _demo_text(role, user, cfg)
        in_tok = max(50, len(system + user) // 4)
        out_tok = max(50, len(text) // 4)
    else:
        text, in_tok, out_tok = _real_call(role, system, user, cfg, max_tokens, json_mode)

    tracker.add(role, cfg["provider"], cfg["model"], in_tok, out_tok)
    st.session_state["cost_data"] = tracker.to_dict()
    refresh_cost(cost_area, tracker)
    return text


# ---------------------------------------------------------------------------
# 5. 웹 검색 (Tavily)
# ---------------------------------------------------------------------------
def tavily_search(api_key: str, queries: list[str], max_results: int = 5) -> list[dict]:
    from tavily import TavilyClient

    client = TavilyClient(api_key=api_key)
    bundle: list[dict] = []
    for q in queries:
        try:
            result = client.search(query=q, max_results=max_results, search_depth="basic")
            bundle.append({"query": q, "results": result.get("results", []) or [], "error": None})
        except Exception as exc:  # 검색 1건 실패가 전체를 막지 않게
            bundle.append({"query": q, "results": [], "error": str(exc)[:200]})
    return bundle


def format_sources(bundle: list[dict], limit: int = 9000) -> tuple[str, int]:
    lines: list[str] = []
    idx = 1
    for item in bundle:
        if item.get("error"):
            lines.append(f"(검색 실패: {item['query']} — {item['error']})")
            continue
        for r in item["results"]:
            title = (r.get("title") or "제목 없음").strip()
            url = (r.get("url") or "").strip()
            content = re.sub(r"\s+", " ", (r.get("content") or "")).strip()
            lines.append(f"[{idx}] {title} — {url}\n{content[:700]}")
            idx += 1
    return "\n\n".join(lines)[:limit], idx - 1


def collect_source_links(bundle: list[dict]) -> list[dict]:
    links: list[dict] = []
    for item in bundle:
        for r in item["results"]:
            links.append({"title": (r.get("title") or "").strip(),
                          "url": (r.get("url") or "").strip()})
    return links


# ---------------------------------------------------------------------------
# 6. 비서실장: 업무 분석
# ---------------------------------------------------------------------------
def normalize_plan(raw, instruction: str) -> dict:
    if not isinstance(raw, dict):
        return {
            "needs_clarification": False,
            "questions": [],
            "summary": instruction[:120],
            "tasks": {k: {"assign": True, "instruction": instruction} for k in EMPLOYEES},
            "final_briefing_focus": "다음 액션 정리",
            "fallback": True,
        }

    tasks_raw = raw.get("tasks") if isinstance(raw.get("tasks"), dict) else {}
    tasks: dict[str, dict] = {}
    for key in EMPLOYEES:
        item = tasks_raw.get(key)
        if isinstance(item, dict):
            assign = bool(item.get("assign", False))
            inst = str(item.get("instruction") or "").strip()
        elif isinstance(item, str) and item.strip():
            assign, inst = True, item.strip()
        else:
            assign, inst = False, ""
        tasks[key] = {"assign": assign, "instruction": inst or instruction}

    if not any(t["assign"] for t in tasks.values()):
        tasks["research"]["assign"] = True
        tasks["research"]["instruction"] = instruction

    questions = [str(q).strip() for q in (raw.get("questions") or []) if str(q).strip()][:3]
    needs = bool(raw.get("needs_clarification")) and len(questions) > 0

    return {
        "needs_clarification": needs,
        "questions": questions,
        "summary": str(raw.get("summary") or instruction)[:300],
        "tasks": tasks,
        "final_briefing_focus": str(raw.get("final_briefing_focus") or ""),
        "fallback": False,
    }


def plan_tasks(cfg: dict, tracker: CostTracker, cost_area: dict,
               instruction: str, answers_text: str = "") -> dict:
    user = f"[Berry 님의 지시]\n{instruction}\n"
    if answers_text.strip():
        user += f"\n[비서실장이 확인한 추가 답변 — 이 내용을 반영해 업무를 다시 배분하세요]\n{answers_text}\n"
    user += "\n위 지시에 대한 업무 분배 JSON을 출력하세요."

    raw_text = call_llm("planner", CHIEF_SYSTEM, user, cfg, tracker, cost_area,
                        max_tokens=1200, json_mode=True)
    parsed = extract_json(raw_text)
    plan = normalize_plan(parsed, instruction)
    if parsed is None:
        plan["raw"] = raw_text[:500]
    return plan


# ---------------------------------------------------------------------------
# 7. 직원별 실행
# ---------------------------------------------------------------------------
def run_research(cfg: dict, tracker: CostTracker, cost_area: dict,
                 user_instruction: str, task_instruction: str,
                 answers_text: str) -> tuple[str, list[dict], str | None]:
    meta = EMPLOYEES["research"]
    label = f"{meta['emoji']} {meta['name']} 작업 중…"
    report, sources, error = "", [], None

    with st.status(label, expanded=True) as status:
        try:
            status.write("① 검색어를 설계하는 중…")
            q_user = (f"[지시]\n{task_instruction}\n\n[전체 맥락]\n{user_instruction}\n"
                      f"{answers_text}\n\n위 지시를 조사할 검색어 3개를 JSON으로 출력하세요.")
            q_text = call_llm("research_queries", QUERY_SYSTEM, q_user, cfg, tracker,
                              cost_area, max_tokens=400, json_mode=True)
            parsed = extract_json(q_text) or {}
            queries = [str(q).strip() for q in (parsed.get("queries") or []) if str(q).strip()][:3]
            if not queries:
                queries = [task_instruction[:60]]
            status.write("② 검색어: " + " / ".join(queries))

            source_text = ""
            if cfg["demo"]:
                status.write("③ 데모 모드 — 실제 웹 검색을 건너뜁니다.")
            elif cfg.get("tavily_key"):
                status.write("③ Tavily 웹 검색 실행 중…")
                bundle = tavily_search(cfg["tavily_key"], queries)
                source_text, n = format_sources(bundle)
                sources = collect_source_links(bundle)
                if n:
                    status.write(f"④ 검색 결과 {n}건을 수집했습니다.")
                else:
                    status.write("④ 검색 결과가 없어 자체 지식으로 작성합니다.")
            else:
                status.write("③ Tavily 키가 없어 LLM 자체 지식으로 작성합니다(최신 공고·가격은 원문 확인 필요).")

            status.write("⑤ 리서치 보고서를 작성하는 중…")
            user = (
                f"[Berry 님의 원래 지시]\n{user_instruction}\n\n"
                f"[비서실장이 내린 지시]\n{task_instruction}\n\n"
                f"{answers_text}\n\n"
                f"[웹 검색 결과]\n{source_text if source_text else '(웹 검색 결과 없음 — 자체 지식으로 작성하고 한계를 명시)'}\n\n"
                "위 내용으로 리서치 보고서를 작성하세요."
            )
            report = call_llm("research", RESEARCH_SYSTEM, user, cfg, tracker,
                              cost_area, max_tokens=2600)
            status.update(label=f"{meta['emoji']} {meta['name']} 완료", state="complete",
                          expanded=False)
        except Exception as exc:
            error = friendly_error(exc)
            status.update(label=f"{meta['emoji']} {meta['name']} 실패", state="error")
            st.error(error)
    return report, sources, error


def run_business_plan(cfg: dict, tracker: CostTracker, cost_area: dict,
                      user_instruction: str, task_instruction: str,
                      answers_text: str, research_report: str) -> tuple[str, str | None]:
    meta = EMPLOYEES["business_plan"]
    report, error = "", None
    with st.status(f"{meta['emoji']} {meta['name']} 작업 중…", expanded=True) as status:
        try:
            status.write("① 리서치 결과를 정리하는 중…")
            user = (
                f"[Berry 님의 원래 지시]\n{user_instruction}\n\n"
                f"[비서실장이 내린 지시]\n{task_instruction}\n\n"
                f"{answers_text}\n\n"
                f"[리서치 담당 보고서]\n{clip(research_report, 7000) if research_report else '(리서치 보고서 없음 — 일반적인 가정으로 작성하고 가정을 명시)'}\n\n"
                "지시에 맞는 문서 초안을 작성하세요."
            )
            status.write("② 계획서·제안서·손익표를 작성하는 중…")
            report = call_llm("business_plan", BUSINESS_SYSTEM, user, cfg, tracker,
                              cost_area, max_tokens=3000)
            status.update(label=f"{meta['emoji']} {meta['name']} 완료", state="complete",
                          expanded=False)
        except Exception as exc:
            error = friendly_error(exc)
            status.update(label=f"{meta['emoji']} {meta['name']} 실패", state="error")
            st.error(error)
    return report, error


def run_sales_content(cfg: dict, tracker: CostTracker, cost_area: dict,
                      user_instruction: str, task_instruction: str,
                      answers_text: str, research_report: str,
                      plan_report: str) -> tuple[str, str | None]:
    meta = EMPLOYEES["sales_content"]
    report, error = "", None
    with st.status(f"{meta['emoji']} {meta['name']} 작업 중…", expanded=True) as status:
        try:
            status.write("① 영업 대상과 메시지를 정하는 중…")
            user = (
                f"[Berry 님의 원래 지시]\n{user_instruction}\n\n"
                f"[비서실장이 내린 지시]\n{task_instruction}\n\n"
                f"{answers_text}\n\n"
                f"[리서치 요약]\n{clip(research_report, 4000) if research_report else '(없음)'}\n\n"
                f"[사업계획 요약]\n{clip(plan_report, 4000) if plan_report else '(없음)'}\n\n"
                "지시에 맞는 영업 메일·콘텐츠 초안을 작성하세요."
            )
            status.write("② 메일·대본을 작성하는 중…")
            report = call_llm("sales_content", SALES_SYSTEM, user, cfg, tracker,
                              cost_area, max_tokens=2600)
            status.update(label=f"{meta['emoji']} {meta['name']} 완료", state="complete",
                          expanded=False)
        except Exception as exc:
            error = friendly_error(exc)
            status.update(label=f"{meta['emoji']} {meta['name']} 실패", state="error")
            st.error(error)
    return report, error


def run_final_briefing(cfg: dict, tracker: CostTracker, cost_area: dict,
                       instruction: str, plan: dict, reports: dict,
                       errors: dict, answers_text: str) -> tuple[str, str | None]:
    error = None
    with st.status("🧠 비서실장이 최종 브리핑을 작성하는 중…", expanded=True) as status:
        try:
            blocks = []
            for key, meta in EMPLOYEES.items():
                if errors.get(key):
                    blocks.append(f"[{meta['emoji']} {meta['name']}] — 실패: {errors[key]}")
                elif reports.get(key):
                    blocks.append(f"[{meta['emoji']} {meta['name']}]\n{clip(reports[key], 5000)}")
            if not blocks:
                blocks.append("(직원 보고서 없음)")

            cost = tracker.totals()
            cost_line = (f"이번 작업 예상 비용 {fmt_won(cost['krw'])} "
                         f"(${cost['usd']:.4f}, API 호출 {cost['calls']}회, 토큰 {cost['tokens']:,}개)")
            user = (
                f"[Berry 님의 지시]\n{instruction}\n\n"
                f"[비서실장의 업무 분배 요약]\n{plan.get('summary', '')}\n"
                f"강조점: {plan.get('final_briefing_focus', '')}\n\n"
                f"{answers_text}\n\n"
                f"[직원 보고서]\n" + "\n\n".join(blocks) + "\n\n"
                f"[비용]\n{cost_line}\n\n"
                "위 내용을 종합해 최종 브리핑을 작성하세요."
            )
            status.write("① 직원 보고서를 읽는 중…")
            status.write("② 결론·다음 액션을 정리하는 중…")
            text = call_llm("final", FINAL_SYSTEM, user, cfg, tracker,
                            cost_area, max_tokens=2200)
            status.update(label="🧠 최종 브리핑 완료", state="complete", expanded=False)
            return text, None
        except Exception as exc:
            error = friendly_error(exc)
            status.update(label="🧠 최종 브리핑 실패", state="error")
            st.error(error)
    return "", error


# ---------------------------------------------------------------------------
# 8. 결과 문서 만들기
# ---------------------------------------------------------------------------
def build_markdown(result: dict) -> str:
    lines: list[str] = []
    lines.append("# 🧠 비서실장 작업 보고서")
    lines.append("")
    lines.append(f"- **지시:** {result['instruction']}")
    lines.append(f"- **작성:** {result['finished']}  (시작 {result['started']})")
    lines.append(f"- **모델:** {result['provider']} · {result['model']}"
                 + (" · 데모 모드" if result.get("demo") else ""))
    cost = result.get("cost", {})
    lines.append(f"- **예상 비용:** {fmt_won(cost.get('krw', 0))} (${cost.get('usd', 0):.4f}) "
                 f"· 호출 {cost.get('calls', 0)}회 · 토큰 {cost.get('tokens', 0):,}개")
    lines.append("")
    lines.append("---")
    lines.append("")
    if result.get("answers_text"):
        lines.append("## 🗨️ 확인한 추가 답변")
        lines.append("")
        lines.append(result["answers_text"])
        lines.append("")
    lines.append("## 🧠 최종 브리핑")
    lines.append("")
    lines.append(result.get("final") or "(최종 브리핑이 생성되지 않았습니다.)")
    lines.append("")
    for key, meta in EMPLOYEES.items():
        task = ((result.get("plan") or {}).get("tasks") or {}).get(key) or {}
        if not task.get("assign", True):
            continue
        lines.append("---")
        lines.append("")
        lines.append(f"## {meta['emoji']} {meta['name']} 보고서")
        lines.append("")
        if result["errors"].get(key):
            lines.append(f"> ⚠️ 이 보고서는 생성되지 않았습니다: {result['errors'][key]}")
        else:
            lines.append(result["reports"].get(key) or "(내용 없음)")
        links = result["sources"].get(key) or []
        if links:
            lines.append("")
            lines.append("### 🔗 수집한 링크")
            for i, link in enumerate(links, start=1):
                lines.append(f"{i}. {link['title']} — {link['url']}")
        lines.append("")
    if result.get("call_rows"):
        lines.append("---")
        lines.append("")
        lines.append("## 💰 API 호출 내역")
        lines.append("")
        lines.append("| 담당 | 모델 | 입력 토큰 | 출력 토큰 | 비용(USD) | 비용(원) |")
        lines.append("|---|---|---|---|---|---|")
        for row in result["call_rows"]:
            lines.append(f"| {row['담당']} | {row['모델']} | {row['입력 토큰']:,} | "
                         f"{row['출력 토큰']:,} | ${row['비용(USD)']:.4f} | {fmt_won(row['비용(원)'])} |")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_이 보고서는 Streamlit 비서실장 대시보드 초안으로 생성되었습니다._")
    return "\n".join(lines)


def build_zip(result: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("00_최종브리핑.md", result.get("final") or "")
        for key, meta in EMPLOYEES.items():
            task = ((result.get("plan") or {}).get("tasks") or {}).get(key) or {}
            if not task.get("assign", True):
                continue
            body = result["reports"].get(key) or f"(생성되지 않음: {result['errors'].get(key, '')})"
            zf.writestr(f"{key}_{meta['name']}.md", body)
        zf.writestr("99_전체보고서.md", build_markdown(result))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 9. 화면
# ---------------------------------------------------------------------------
def render_header() -> None:
    st.title("🧠 비서실장 대시보드")
    st.caption("한 줄 지시 → 🧠 비서실장이 업무를 나누고 → AI 직원 3인이 일한 뒤 → 최종 브리핑과 비용을 보여 줍니다.")
    cols = st.columns(4)
    for col, (emoji, name, desc) in zip(cols, ORG_CHART):
        with col:
            st.markdown(f"**{emoji} {name}**")
            st.caption(desc)
    st.divider()


def render_sidebar() -> dict:
    with st.sidebar:
        st.title("⚙️ 설정")
        provider = st.selectbox("AI 제공사", list(MODELS.keys()))
        model_options = list(MODELS[provider].keys())
        default_model = DEFAULT_MODEL.get(provider, model_options[0])
        default_index = model_options.index(default_model) if default_model in model_options else 0
        model = st.selectbox("모델", model_options, index=default_index)
        price = MODELS[provider][model]
        st.caption(f"단가: 입력 ${price['in']:.2f} / 출력 ${price['out']:.2f} (100만 토큰당)")

        secret_name = "OPENAI_API_KEY" if provider == "OpenAI" else "ANTHROPIC_API_KEY"
        api_key = st.text_input(
            f"{provider} API 키",
            value=secret(secret_name),
            type="password",
            help="Streamlit Secrets에 넣어 두면 자동으로 채워집니다.",
        )
        tavily_key = st.text_input(
            "Tavily API 키 (선택)",
            value=secret("TAVILY_API_KEY"),
            type="password",
            help="있으면 실제 웹 검색 결과로 리서치합니다. 무료 월 1,000회.",
        )
        krw_per_usd = st.number_input(
            "환율 (원/USD)", min_value=500.0, max_value=3000.0,
            value=float(secret("KRW_PER_USD", str(DEFAULT_KRW_PER_USD)) or DEFAULT_KRW_PER_USD),
            step=10.0,
        )
        demo = st.checkbox("🧪 데모 모드 (API 키 없이 화면 미리보기)", value=False)
        st.caption("데모 모드에서는 실제 API를 부르지 않고 예시 결과를 보여 줍니다.")

        st.markdown("---")
        st.markdown("### 🗂️ 직원 소개")
        for key, meta in EMPLOYEES.items():
            st.caption(f"{meta['emoji']} **{meta['name']}** — {meta['desc']}")

        st.markdown("---")
        st.caption("※ 표시되는 비용은 토큰 사용량 × 단가로 계산한 **예상치**입니다. 실제 청구액과 조금 다를 수 있습니다.")

    return {
        "provider": provider,
        "model": model,
        "api_key": api_key.strip(),
        "tavily_key": tavily_key.strip(),
        "krw_per_usd": float(krw_per_usd),
        "demo": bool(demo),
    }


def show_plan(plan: dict) -> None:
    with st.expander("🧠 비서실장의 업무 분배 보기", expanded=False):
        st.markdown(f"**요약:** {plan.get('summary', '')}")
        for key, meta in EMPLOYEES.items():
            task = plan["tasks"][key]
            if task["assign"]:
                st.markdown(f"- ✅ **{meta['emoji']} {meta['name']}** — {task['instruction']}")
            else:
                st.markdown(f"- ⏸️ **{meta['emoji']} {meta['name']}** — 이번 작업에는 배정하지 않음")
        if plan.get("final_briefing_focus"):
            st.markdown(f"**최종 브리핑 초점:** {plan['final_briefing_focus']}")
        if plan.get("fallback"):
            st.warning("비서실장 응답을 JSON으로 읽지 못해, 지시를 3명에게 그대로 배정했습니다. 다시 실행하면 보통 정상으로 돌아옵니다.")


def execute_and_render(cfg: dict, tracker: CostTracker, cost_area: dict,
                       instruction: str, plan: dict, answers: dict) -> None:
    """직원들을 순서대로 실행하고 결과를 세션에 저장한 뒤 화면에 그립니다."""
    answers_text = ""
    if answers:
        pairs = [f"- Q: {q}\n  A: {a.strip()}" for q, a in answers.items() if a and a.strip()]
        if pairs:
            answers_text = "[Berry 님이 확인 질문에 답한 내용]\n" + "\n".join(pairs)

    started = datetime.now().strftime("%Y-%m-%d %H:%M")
    reports: dict[str, str] = {}
    sources: dict[str, list] = {}
    errors: dict[str, str] = {}

    research_report = ""
    if plan["tasks"]["research"]["assign"]:
        research_report, src, err = run_research(
            cfg, tracker, cost_area, instruction,
            plan["tasks"]["research"]["instruction"], answers_text)
        reports["research"] = research_report
        sources["research"] = src
        if err:
            errors["research"] = err
    else:
        sources["research"] = []

    plan_report = ""
    if plan["tasks"]["business_plan"]["assign"]:
        plan_report, err = run_business_plan(
            cfg, tracker, cost_area, instruction,
            plan["tasks"]["business_plan"]["instruction"], answers_text, research_report)
        reports["business_plan"] = plan_report
        if err:
            errors["business_plan"] = err
    else:
        sources["business_plan"] = []

    if plan["tasks"]["sales_content"]["assign"]:
        sales_report, err = run_sales_content(
            cfg, tracker, cost_area, instruction,
            plan["tasks"]["sales_content"]["instruction"], answers_text,
            research_report, plan_report)
        reports["sales_content"] = sales_report
        sources["sales_content"] = []
        if err:
            errors["sales_content"] = err
    else:
        sources["sales_content"] = []

    final_text, final_err = run_final_briefing(
        cfg, tracker, cost_area, instruction, plan, reports, errors, answers_text)
    if final_err:
        errors["final"] = final_err

    result = {
        "instruction": instruction,
        "started": started,
        "finished": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "provider": cfg["provider"],
        "model": cfg["model"],
        "demo": cfg["demo"],
        "answers_text": answers_text,
        "plan": plan,
        "reports": reports,
        "sources": sources,
        "errors": errors,
        "final": final_text,
        "cost": tracker.totals(),
        "call_rows": list(tracker.calls),
    }
    st.session_state["result"] = result
    st.session_state["stage"] = "done"
    st.rerun()  # 진행 로그 대신 깔끔한 결과 화면으로 다시 그립니다.


def render_result() -> None:
    result = st.session_state.get("result")
    if not result:
        st.info("아직 결과가 없습니다.")
        return

    cost = result.get("cost", {})
    st.subheader("📊 이번 작업 결과")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("💰 예상 비용", fmt_won(cost.get("krw", 0)))
    m2.metric("💵 달러", f"${cost.get('usd', 0):.4f}")
    m3.metric("🔁 API 호출", f"{cost.get('calls', 0)}회")
    m4.metric("🔤 토큰", f"{cost.get('tokens', 0):,}개")
    st.caption(f"모델 {result['provider']} · {result['model']}"
               + (" · 🧪 데모 모드" if result.get("demo") else ""))

    st.markdown("---")
    st.markdown(result.get("final") or "_최종 브리핑이 생성되지 않았습니다._")

    st.markdown("---")
    st.subheader("🗂️ 직원별 보고서")
    for key, meta in EMPLOYEES.items():
        task = result["plan"]["tasks"][key]
        if not task["assign"]:
            continue
        title = f"{meta['emoji']} {meta['name']}"
        with st.expander(title, expanded=(key == "research")):
            if result["errors"].get(key):
                st.error(f"이 보고서는 생성되지 않았습니다: {result['errors'][key]}")
            else:
                st.markdown(result["reports"].get(key) or "_내용 없음_")
            links = result["sources"].get(key) or []
            if links:
                st.markdown("**🔗 수집한 링크**")
                for i, link in enumerate(links, start=1):
                    st.markdown(f"{i}. [{link['title'] or link['url']}]({link['url']})")

    if result.get("call_rows"):
        with st.expander("💰 API 호출 내역 (토큰·비용)", expanded=False):
            st.dataframe(result["call_rows"], width="stretch")

    st.markdown("---")
    markdown_doc = build_markdown(result)
    stamp = datetime.now().strftime("%Y%m%d")
    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            "⬇️ 전체 보고서 내려받기 (.md)",
            data=markdown_doc.encode("utf-8"),
            file_name=f"비서실장_보고서_{stamp}.md",
            mime="text/markdown",
            type="primary",
            width="stretch",
        )
    with d2:
        st.download_button(
            "🗜️ 직원별 파일 한 번에 (.zip)",
            data=build_zip(result),
            file_name=f"비서실장_보고서_{stamp}.zip",
            mime="application/zip",
            width="stretch",
        )

    col_a, col_b = st.columns([1, 1])
    with col_a:
        if st.button("🆕 새 작업 시작", width="stretch"):
            for key in ("plan", "questions", "answers", "result", "cost_data"):
                st.session_state.pop(key, None)
            for key in list(st.session_state.keys()):
                if key.startswith("answer_") or key in ("instruction_input", "pending_example"):
                    st.session_state.pop(key, None)
            st.session_state["stage"] = "idle"
            st.rerun()
    with col_b:
        st.caption("내려받은 .md 파일은 노션·구글 문서에 그대로 붙여넣을 수 있습니다.")


def render_idle(cfg: dict, tracker: CostTracker, cost_area: dict) -> None:
    # 예시 버튼은 다음 실행에서 위젯이 만들어지기 전에 값을 채워야 합니다.
    pending = st.session_state.pop("pending_example", None)
    if pending:
        st.session_state["instruction_input"] = pending

    st.subheader("📝 비서실장에게 한 줄로 지시하세요")
    st.text_area(
        "지시 내용",
        key="instruction_input",
        height=110,
        placeholder="예) 2027년 예비창업패키지 지원사업 신청용 계획서 초안을 만들어 줘. 예산은 5천만 원 규모야.",
        label_visibility="collapsed",
    )
    st.caption("예시로 채워 보기")
    ex_cols = st.columns(3)
    examples = [
        "2027년 예비창업패키지 지원사업 신청용 계획서 초안을 만들어 줘. 예산은 5천만 원 규모야.",
        "중소기업 5곳에 보낼 AI 문서 자동화 외주 제안서와 영업 메일을 준비해 줘.",
        "미국 로봇회사 문워커스 에어로의 국내 총판 수익성을 조사하고 제안 메일을 써 줘.",
    ]
    for col, example in zip(ex_cols, examples):
        if col.button(example.split(" ")[0] + " 예시", width="stretch"):
            st.session_state["pending_example"] = example
            st.rerun()

    instruction = (st.session_state.get("instruction_input") or "").strip()

    missing_key = (not cfg["demo"]) and (not cfg["api_key"])
    if missing_key:
        st.warning("사이드바에 API 키를 입력하거나, Streamlit Secrets에 키를 넣어 주세요. "
                   "키가 없다면 '🧪 데모 모드'를 켜서 화면을 먼저 둘러볼 수 있습니다.")

    if st.button("🚀 실행", type="primary", disabled=(not instruction or missing_key)):
        with st.spinner("🧠 비서실장이 지시를 분석하는 중…"):
            try:
                plan = plan_tasks(cfg, tracker, cost_area, instruction)
            except Exception as exc:
                st.error(friendly_error(exc))
                st.stop()
        st.session_state["instruction"] = instruction
        st.session_state["plan"] = plan

        if plan["needs_clarification"]:
            st.session_state["questions"] = plan["questions"]
            st.session_state["stage"] = "clarify"
            st.rerun()
        else:
            execute_and_render(cfg, tracker, cost_area, instruction, plan, {})


def render_clarify(cfg: dict, cost_area: dict) -> None:
    plan = st.session_state.get("plan") or {}
    questions = st.session_state.get("questions") or []
    instruction = st.session_state.get("instruction") or ""
    tracker = CostTracker.from_dict(st.session_state.get("cost_data") or {})
    if not tracker.calls:
        tracker.krw_per_usd = cfg["krw_per_usd"]

    st.subheader("🗨️ 시작 전에 몇 가지만 확인할게요")
    st.info("🧠 비서실장: 답을 주시면 그에 맞춰 직원들에게 일을 나눠 실행하겠습니다. 비워 두셔도 그대로 진행합니다.")
    answers: dict[str, str] = {}
    for i, question in enumerate(questions):
        answers[question] = st.text_input(f"Q{i + 1}. {question}", key=f"answer_{i}")

    show_plan(plan)

    if st.button("✅ 답변 반영해서 실행", type="primary"):
        answers_text = "\n".join(f"- Q: {q}\n  A: {a.strip()}"
                                 for q, a in answers.items() if a and a.strip())
        final_plan = plan
        if answers_text:
            with st.spinner("🧠 비서실장이 답변을 반영해 업무를 다시 나누는 중…"):
                try:
                    final_plan = plan_tasks(cfg, tracker, cost_area, instruction,
                                            "[Berry 님의 답변]\n" + answers_text)
                except Exception as exc:
                    st.error(friendly_error(exc))
                    st.stop()
            # 답변까지 반영했으므로 추가 질문은 한 번 더 받지 않고 바로 실행합니다.
            final_plan["needs_clarification"] = False
        st.session_state["plan"] = final_plan
        st.session_state["answers"] = answers
        execute_and_render(cfg, tracker, cost_area, instruction, final_plan, answers)

    if st.button("건너뛰고 그대로 실행"):
        st.session_state["answers"] = {}
        execute_and_render(cfg, tracker, cost_area, instruction, plan, {})


def main() -> None:
    st.session_state.setdefault("stage", "idle")

    cfg = render_sidebar()
    cost_area = render_cost_area()

    cost_data = st.session_state.get("cost_data")
    if isinstance(cost_data, dict):
        tracker = CostTracker.from_dict(cost_data)
        if not tracker.calls:  # 아직 호출 전이면 바꿔 넣은 환율을 반영합니다.
            tracker.krw_per_usd = cfg["krw_per_usd"]
        refresh_cost(cost_area, tracker)
    else:
        tracker = CostTracker(cfg["krw_per_usd"])
        st.session_state["cost_data"] = tracker.to_dict()

    render_header()

    stage = st.session_state.get("stage", "idle")
    if stage == "done":
        render_result()
    elif stage == "clarify":
        render_clarify(cfg, cost_area)
    else:
        render_idle(cfg, tracker, cost_area)

    st.markdown("---")
    st.caption("🧠 비서실장 대시보드 · Streamlit 초안 · 비용은 토큰 단가 기준 예상치입니다.")


if __name__ == "__main__":
    main()
