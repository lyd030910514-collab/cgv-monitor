"""
CGV 예매 오픈 감시 봇 v3 — 2개 서버 동시 감시 + 차단 시 즉시 갈아타기

- GitHub에서 서버 A, B 두 개가 동시에 감시합니다. 서버(IP)가 서로 달라서 둘이 한꺼번에 막힐 확률이 낮아요.
- 확인 시각을 엇갈리게 맞춥니다. A는 0초, B는 간격의 절반(180초 기준 90초)이라 실질 확인 간격이 약 90초가 됩니다.
- 접속 제한(403 등)을 만나면 재시도나 대기 없이 바로 종료합니다. 워크플로가 새 서버(새 IP)로 다시 시작해요.
- (v2 유지) 날짜 줄 스크린샷, 정상 작동 알림, 단계별 자가 복구, 실패 순간의 화면 전송
"""
import os, re, sys, json, time, datetime as dt
import requests
from playwright.sync_api import sync_playwright

# ───────── 설정 (환경변수로 바꿀 수 있음) ─────────
WEBHOOK = (os.getenv("DISCORD_WEBHOOK_URL") or "https://discord.com/api/webhooks/1562771316803903560/2ra2oMnp9HWoyRodHR_C1mL4KvhK5vFRYuRfNpg1yjQ4nRU7WtoB2m5vYNme8dhAo7pR").strip()   # Secret이 있으면 그걸 우선 사용
WORKER = (os.getenv("WORKER") or "").strip().lower()         # "a" / "b" / 빈값(혼자 실행)
TAG = f"[{WORKER.upper()}] " if WORKER else ""               # 디스코드 메시지 앞에 붙는 서버 표시
BASE = os.getenv("CGV_BASE", "https://cgv.co.kr")
KEYWORD = os.getenv("KEYWORD", "요루시카")
MOVIE_TITLE = os.getenv("MOVIE_TITLE", "도작")            # 검색 결과 중 이 글자가 들어간 작품
THEATER = os.getenv("THEATER", "홍대")
CUTOFF = dt.date.fromisoformat(os.getenv("CUTOFF_DATE", "2026-10-30"))   # 이 날짜 '이후(당일 포함)'
LOOP_MINUTES = int(os.getenv("LOOP_MINUTES", "330"))
INTERVAL = int(os.getenv("INTERVAL_SEC", "180"))          # 서버 1대의 확인 간격(초)
SLOT_OFFSET = INTERVAL // 2 if WORKER == "b" else 0       # B는 A와 반 박자 엇갈리게
SEND_START = os.getenv("SEND_START_MESSAGE", "false") == "true"
HEARTBEAT_EVERY = int(os.getenv("HEARTBEAT_EVERY", "10"))  # 연속 N번 성공마다 정상 알림. 0이면 끔
GAP_ALERT_MIN = int(os.getenv("GAP_ALERT_MIN", "60"))      # 이 시간 넘게 확인 못 하면 알림
ERR_NOTICE_MIN = int(os.getenv("ERR_NOTICE_MIN", "10"))    # 문제 알림 최소 간격(분)
MAX_BLOCKED = int(os.getenv("MAX_BLOCKED", "1"))           # GitHub에서 연속 N번 차단되면 종료 → 새 서버로 재시작 (1 = 즉시)
BLOCK_BACKOFF = int(os.getenv("BLOCK_BACKOFF_SEC", "30"))  # 차단→재시작이 연속될 때만 늘어나는 대기(초). 첫 차단은 대기 없음
SHOT_MODE = os.getenv("SHOT_MODE", "edit")                 # edit: 한 메시지를 계속 수정 / each: 매번 새 메시지 / change: 상태 바뀔 때만 / off: 안 보냄
MENTION = os.getenv("MENTION", "")                         # 예매 오픈 알림에 붙일 멘션. 예: "@everyone"
LIGHT_MODE = os.getenv("LIGHT_MODE", "true") == "true"     # 이미지/폰트/추적 스크립트 안 받아서 빠르게
ON_GITHUB = os.getenv("GITHUB_ACTIONS") == "true"
STATE_FILE = "state.json"
KST = dt.timezone(dt.timedelta(hours=9))
BLOCK_RE = re.compile(r"비정상|제한되었|접속이 제한|access denied|forbidden|Reference #", re.I)
TRACKERS = ("google-analytics", "googletagmanager", "doubleclick", "facebook.net", "criteo",
            "naver.com/wcs", "wcs.naver", "kakao.com/track", "adservice", "hotjar", "amplitude")


class Blocked(Exception):
    """CGV가 이 서버(IP)의 접속을 막은 경우. soft=True는 '빈 화면'처럼 확실하지 않은 경우"""

    def __init__(self, msg="", soft=False):
        super().__init__(msg)
        self.soft = soft


class Changed(Exception):
    """화면 구조가 달라져서 요소를 못 찾은 경우"""


STAGE = ""


def stage(s):
    global STAGE
    STAGE = s


# ───────── 상태 저장 (실행이 바뀌어도 중복 알림 방지) ─────────
def _load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


STATE = _load()


def set_state(**kw):
    if not any(STATE.get(k) != v for k, v in kw.items()):
        return
    STATE.update(kw)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False)
        open("state.dirty", "w").write("1")
    except Exception as e:
        print("state save error:", e)


# ───────── 디스코드 (모든 메시지 앞에 [A]/[B] 표시) ─────────
def notify(msg):
    try:
        requests.post(WEBHOOK, json={"content": (TAG + msg)[:1900]}, timeout=15)
    except Exception as e:
        print("discord error:", e)


def post_with_image(msg, img, name="shot.jpg"):
    """새 메시지 + 이미지. 성공하면 메시지 id 반환"""
    try:
        r = requests.post(WEBHOOK, params={"wait": "true"}, data={"content": (TAG + msg)[:1900]},
                          files={"file": (name, img, "image/jpeg")}, timeout=30)
        return r.json().get("id") if r.ok else None
    except Exception as e:
        print("discord image error:", e)
        return None


def edit_with_image(msg, img):
    """이전에 보낸 메시지를 최신 내용+이미지로 교체. 실패하면 새로 보냄 (서버별로 각자 메시지 1개)"""
    mid = STATE.get("shot_msg")
    if mid:
        try:
            r = requests.patch(
                f"{WEBHOOK}/messages/{mid}",
                data={"payload_json": json.dumps({"content": (TAG + msg)[:1900], "attachments": []})},
                files={"files[0]": ("shot.jpg", img, "image/jpeg")}, timeout=30)
            if r.ok:
                return
        except Exception as e:
            print("discord edit error:", e)
    new = post_with_image(msg, img)
    if new:
        set_state(shot_msg=new)


def send_shot(msg, img):
    if SHOT_MODE == "off" or not img:
        return
    if SHOT_MODE == "each":
        post_with_image(msg, img)
    elif SHOT_MODE == "change":
        pass  # 상태가 바뀐 경우는 on_success에서 처리
    else:
        edit_with_image(msg, img)


# ───────── 날짜 줄 읽기 (활성/비활성 판단) ─────────
DATE_PRESENT_JS = r"""
() => /(오늘|[월화수목금토일])\s*\d{1,2}/.test(document.body.innerText || '')
"""

READY_JS = r"""
(t) => { const s = document.body.innerText || '';
  return s.includes('선택 된 극장이 없습니다') || s.includes(t); }
"""

DATE_JS = r"""
() => {
  const re = /^(오늘|[월화수목금토일])\s*(\d{1,2})$/;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const nums = c => (c.match(/[\d.]+/g) || []).map(Number);
  const lum = c => { const [r,g,b] = nums(c); return (0.299*r + 0.587*g + 0.114*b) / 255; };
  const isColored = c => { const n = nums(c); if (n.length < 3) return false;
    const a = n.length > 3 ? n[3] : 1; if (a < 0.05) return false;
    return (Math.max(n[0],n[1],n[2]) - Math.min(n[0],n[1],n[2])) > 60; };
  const all = Array.from(document.querySelectorAll('li, button, a, div, span'));
  const hits = all.filter(el => re.test(norm(el.innerText)));
  const leaf = hits.filter(el => !hits.some(o => o !== el && el.contains(o)));
  return leaf.map(el => {
    const day = parseInt(norm(el.innerText).match(re)[2], 10);
    const numEl = Array.from(el.querySelectorAll('*')).find(e => /^\d{1,2}$/.test(norm(e.innerText))) || el;
    let op = 1, e = numEl;
    while (e && e !== el.parentElement) { op *= parseFloat(getComputedStyle(e).opacity || '1'); e = e.parentElement; }
    const l = lum(getComputedStyle(numEl).color);
    const effLum = l * op + (1 - op);
    const group = [el, el.parentElement, ...Array.from(el.querySelectorAll('*'))].filter(Boolean);
    const selected = group.some(g => isColored(getComputedStyle(g).backgroundColor));
    const dis = !!el.closest('[disabled],[aria-disabled="true"]');
    const b = el.getBoundingClientRect();
    return { day, selected, dis, effLum: Math.round(effLum*100)/100,
             enabled: selected || (!dis && effLum < 0.6),
             rect: { x: b.x, y: b.y, w: b.width, h: b.height } };
  });
}
"""


def real_dates(day_numbers):
    """화면의 일(day) 숫자 목록 -> 실제 날짜(월 넘어감 처리)"""
    today = dt.datetime.now(KST).date()
    out, cur = [], None
    for d in day_numbers:
        if cur is None:
            y, m = today.year, today.month
            if d < today.day - 1:
                m += 1
                if m == 13:
                    y, m = y + 1, 1
            cur = dt.date(y, m, d)
        else:
            nxt = cur + dt.timedelta(days=1)
            if nxt.day == d:
                cur = nxt
            else:
                y, m = (cur.year, cur.month + 1) if cur.month < 12 else (cur.year + 1, 1)
                cur = dt.date(y, m, d)
        out.append(cur)
    return out


# ───────── 브라우저 ─────────
def make_context(browser):
    ctx = browser.new_context(
        locale="ko-KR", timezone_id="Asia/Seoul", viewport={"width": 1400, "height": 1000},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    if LIGHT_MODE:
        def handler(route):
            req = route.request
            if req.resource_type in ("image", "media", "font") or any(t in req.url for t in TRACKERS):
                return route.abort()
            return route.continue_()
        ctx.route("**/*", handler)
    return ctx


def reset_browser(browser, holder):
    """컨텍스트+페이지를 새로 만든다 (자가 복구)"""
    for k in ("page", "ctx"):
        try:
            holder[k].close()
        except Exception:
            pass
    holder["ctx"] = make_context(browser)
    holder["page"] = holder["ctx"].new_page()
    holder["page"].set_default_timeout(20000)


def body_text(page):
    try:
        return (page.inner_text("body", timeout=3000) or "")[:300].replace("\n", " ")
    except Exception:
        return ""


def detect_block(page):
    body = body_text(page)
    if BLOCK_RE.search(body):
        raise Blocked(f"접속 제한: {body[:80]}")
    if len(body.strip()) < 20:
        # 빈 화면은 일시적일 수도 있어서 'soft'로 표시 (재시도를 먼저 해 봄)
        raise Blocked(f"빈 화면: {body[:80]}", soft=True)


def click_any(label, candidates, timeout=6000):
    """여러 후보 중 먼저 되는 것을 클릭 (대체 경로)"""
    last = None
    for make in candidates:
        try:
            make().click(timeout=timeout)
            return
        except Exception as e:
            last = e
    raise Changed(f"'{label}' 요소를 못 찾음: {str(last)[:120]}")


def snap(page, **kw):
    try:
        return page.screenshot(type="jpeg", quality=65, **kw)
    except Exception:
        return None


# ───────── 예매 화면 열기 ─────────
def open_via_search(page):
    """처음부터: 통합검색 → 작품 → 예매하기 (느리지만 확실한 경로)"""
    stage("검색 페이지 접속")
    resp = page.goto(f"{BASE}/tme/itgrSrch", wait_until="domcontentloaded", timeout=45000)
    if resp is not None and resp.status in (403, 429):
        raise Blocked(f"HTTP {resp.status}")
    box = page.locator("input[placeholder*='검색어'], input[type=search]").first
    try:
        box.wait_for(timeout=20000)
    except Exception:
        detect_block(page)
        raise Changed(f"검색창이 없음: {body_text(page)[:100]}")

    stage("작품 검색")
    box.fill(KEYWORD)
    box.press("Enter")
    try:   # 고정 2.5초 대신, 결과에 '예매하기'가 보이면 바로 진행
        page.get_by_text("예매하기", exact=True).first.wait_for(timeout=10000)
    except Exception:
        pass

    stage("예매하기 클릭")
    click_any("예매하기", [
        lambda: page.locator("li, div").filter(has_text=MOVIE_TITLE)
                    .filter(has=page.get_by_text("예매하기", exact=True)).last.get_by_text("예매하기", exact=True).first,
        lambda: page.get_by_text("예매하기", exact=True).first,
        lambda: page.get_by_role("button", name=re.compile("예매")).first,
    ], timeout=8000)
    try:
        page.wait_for_url(re.compile(r"movieBook"), timeout=20000)
        page.get_by_text(MOVIE_TITLE).first.wait_for(timeout=10000)   # 엉뚱한 작품 감시 방지
    except Exception:
        detect_block(page)
        raise Changed(f"예매 화면/작품({MOVIE_TITLE}) 확인 실패")
    set_state(book_url=page.url)   # 다음부터는 이 주소로 바로 간다


def open_fast(page):
    """빠른 경로: 예매 화면이면 새로고침, 아니면 저장된 주소로 바로 이동"""
    stage("예매 화면 새로고침")
    if "movieBook" in page.url:
        resp = page.reload(wait_until="domcontentloaded", timeout=30000)
    else:
        resp = page.goto(STATE["book_url"], wait_until="domcontentloaded", timeout=30000)
    if resp is not None and resp.status in (403, 429):
        raise Blocked(f"HTTP {resp.status}")
    try:
        page.get_by_text(MOVIE_TITLE).first.wait_for(timeout=12000)
    except Exception:
        detect_block(page)
        raise Changed("빠른 경로에서 작품을 못 찾음")


def ensure_theater(page):
    stage("극장 확인")
    try:   # 극장 정보가 화면에 뜰 때까지만 기다림 (뜨면 즉시 통과)
        page.wait_for_function(READY_JS, arg=THEATER, timeout=8000)
    except Exception:
        pass
    empty = page.get_by_text(re.compile("선택 된 극장이 없습니다")).first
    try:
        need_pick = empty.is_visible()
    except Exception:
        need_pick = False
    if not need_pick:
        need_pick = page.get_by_text(THEATER, exact=True).count() == 0
    if not need_pick:
        return

    stage("극장 선택")
    try:
        empty.click(timeout=8000)
    except Exception:
        raise Changed("극장 선택 버튼을 못 찾음")
    try:
        page.get_by_text("지역별", exact=True).first.wait_for(timeout=4000)
    except Exception:
        try:
            empty.locator("xpath=..").locator("button, [role=button]").last.click(timeout=5000)
            page.get_by_text("지역별", exact=True).first.wait_for(timeout=4000)
        except Exception:
            raise Changed("극장 목록이 안 열림")
    try:
        page.get_by_text(re.compile(r"^서울")).first.click(timeout=2000)
    except Exception:
        pass
    click_any(f"극장({THEATER})", [lambda: page.get_by_text(THEATER, exact=True).first], timeout=8000)
    click_any("극장선택 버튼", [lambda: page.get_by_text("극장선택", exact=True).first,
                              lambda: page.get_by_role("button", name=re.compile("극장선택")).first], timeout=8000)


def read_dates(page):
    stage("날짜 줄 읽기")
    try:
        page.wait_for_function(DATE_PRESENT_JS, timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(400)   # 렌더링 마무리 (색 변화 반영)
    res = page.evaluate(DATE_JS)
    if not res:
        page.wait_for_timeout(2500)
        res = page.evaluate(DATE_JS)
    seen, rows = set(), []
    for r in res:
        if r["day"] not in seen:
            seen.add(r["day"])
            rows.append(r)
    if not rows:
        raise Changed("날짜 목록을 못 찾음")
    dates = real_dates([r["day"] for r in rows])
    status = " ".join(f"{d.day}{'✓' if r['enabled'] else '✗'}" for d, r in zip(dates, rows))
    enabled = [d for d, r in zip(dates, rows) if r["enabled"]]

    # 날짜 줄 주변만 잘라서 스크린샷 (실패하면 화면 전체)
    img = None
    try:
        xs0 = min(r["rect"]["x"] for r in rows)
        ys0 = min(r["rect"]["y"] for r in rows)
        xs1 = max(r["rect"]["x"] + r["rect"]["w"] for r in rows)
        ys1 = max(r["rect"]["y"] + r["rect"]["h"] for r in rows)
        x0, y0 = max(0, xs0 - 30), max(0, ys0 - 120)
        x1, y1 = min(1400, xs1 + 30), min(1000, ys1 + 420)
        if x1 - x0 > 50 and y1 - y0 > 50:
            img = snap(page, clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})
    except Exception:
        img = None
    if img is None:
        img = snap(page)
    return enabled, status, img


def check(page, force_full):
    if not force_full and STATE.get("book_url"):
        try:
            open_fast(page)
        except Exception as e:   # 차단처럼 보여도 '저장된 주소로 바로 접속'만 막힌 걸 수 있어서, 검색 경로로 한 번 더 시도
            print("fast path failed → full path:", e)
            open_via_search(page)
    else:
        open_via_search(page)
    ensure_theater(page)
    return read_dates(page)


def one_check(browser, holder):
    """확인 1회. 실패하면 단계적으로 다시 시도:
       1차) 열려 있는 페이지로 빠른 확인
       2차) 브라우저를 새로 만들고 처음부터(검색 경로)
       3차) 잠깐 쉬고 한 번 더. 그래도 안 되면 실패 순간의 화면을 holder['fail_shot']에 남김
       접속 제한(403/429, 차단 문구)처럼 보여도 새 브라우저로 처음부터 한 번 더 확인해서(약 10초)
       그래도 막히면 그때 Blocked로 올려보낸다. '빈 화면'은 3차까지 재시도한 뒤 차단으로 본다."""
    last = None
    for attempt in (1, 2, 3):
        try:
            if attempt > 1 or not holder.get("page") or holder["page"].is_closed():
                reset_browser(browser, holder)
                if attempt > 1:
                    time.sleep(3 * attempt)
            return check(holder["page"], force_full=(attempt > 1))
        except Blocked as e:
            holder["fail_shot"] = snap(holder["page"], full_page=False)
            holder["fail_stage"] = STAGE
            if attempt < (3 if e.soft else 2):   # 차단 판정 전에 새 브라우저로 한 번 더 확인 (빈 화면은 3번까지)
                last = e
                print(f"blocked? (try {attempt}, 단계: {STAGE}):", e)
                continue
            raise
        except Exception as e:
            last = e
            print(f"error (try {attempt}, 단계: {STAGE}):", e)
            holder["fail_shot"] = snap(holder["page"], full_page=True) or snap(holder["page"])
            holder["fail_stage"] = STAGE
    raise last


# ───────── 알림 로직 ─────────
def alert_problem(kind, detail, holder):
    """문제 알림 (ERR_NOTICE_MIN분에 한 번) + 그 순간 화면"""
    now = time.time()
    set_state(problem=True)
    if now - STATE.get("last_err_notice", 0) < ERR_NOTICE_MIN * 60:
        return
    set_state(last_err_notice=now)
    msg = f"{kind} (단계: {holder.get('fail_stage') or STAGE})\n{detail[:300]}"
    img = holder.get("fail_shot")
    if img:
        try:
            open("debug.jpg", "wb").write(img)
        except Exception:
            pass
        post_with_image(msg, img, "fail.jpg")
    else:
        notify(msg + "\n(화면을 찍지 못했어요)")


def on_success(enabled, status, img, streak):
    now = time.time()
    t = dt.datetime.now(KST).strftime("%H:%M:%S")
    today = dt.datetime.now(KST).date()
    print(t, "status:", status, f"(연속 {streak}회)")

    if STATE.get("problem"):
        notify(f"✅ [{t}] 다시 정상적으로 확인되고 있어요 | 날짜: {status}")
        set_state(problem=False)
    set_state(last_ok=now, block_exits=0)

    # 기준일 이후 열린 날짜 (한 번 알린 날짜는 다시 안 알림. 서버 A/B는 각자 따로 알려서 2번 올 수 있음)
    notified = set(STATE.get("notified", []))
    hit = [d for d in enabled if d >= CUTOFF and d.isoformat() not in notified]
    if hit:
        txt = (f"{MENTION} " if MENTION else "") + f"🎬 {KEYWORD} {THEATER} 예매 오픈! " \
              + ", ".join(d.strftime("%m/%d(%a)") for d in hit) + f"\n{BASE}"
        if img:
            post_with_image(txt, img)
        else:
            notify(txt)
        set_state(notified=sorted(notified | {d.isoformat() for d in hit}))

    # 기준일 전이라도 새로 열린 날짜는 참고용 알림
    prev = {dt.date.fromisoformat(x) for x in STATE.get("enabled", [])}
    changed = False
    if "enabled" in STATE:
        fresh = [d for d in enabled if d not in prev and today <= d < CUTOFF]
        if fresh:
            notify("🔔 새로 열린 날짜(기준일 전): " + ", ".join(d.strftime("%m/%d(%a)") for d in fresh))
        changed = {d.isoformat() for d in enabled} != set(STATE.get("enabled", []))
    set_state(enabled=sorted(d.isoformat() for d in enabled))

    # 날짜 화면 스크린샷
    near = [d for d in enabled if d >= CUTOFF]
    caption = (f"📅 [{t}] 날짜 상태: {status}\n{CUTOFF} 이후 열림: "
               + (", ".join(d.strftime("%m/%d") for d in near) if near else "없음")
               + f" | 연속 {streak}회 정상")
    if SHOT_MODE == "change":
        if changed or "shot_sent" not in STATE:
            post_with_image(caption, img)
            set_state(shot_sent=True)
    else:
        send_shot(caption, img)

    # 정상 작동 알림: 연속 HEARTBEAT_EVERY번 성공할 때마다
    if HEARTBEAT_EVERY and streak % HEARTBEAT_EVERY == 0:
        minutes = streak * INTERVAL // 60
        notify(f"🔄 [{t}] 정상 작동 중 — 연속 {streak}회 성공(약 {minutes}분) | 날짜: {status} | "
               f"{CUTOFF} 이후 열림: " + (", ".join(d.strftime("%m/%d") for d in near) if near else "없음"))


def gap_watch():
    now = time.time()
    gap = now - STATE.get("last_ok", now)
    if gap >= GAP_ALERT_MIN * 60 and now - STATE.get("last_gap_notice", 0) >= GAP_ALERT_MIN * 60:
        notify(f"⏳ {int(gap // 60)}분째 CGV를 확인하지 못하고 있어요 (접속 제한 중일 수 있음). "
               "그동안 열린 날짜는 못 봤을 수 있습니다.")
        set_state(last_gap_notice=now)


def sleep_until_slot():
    """다음 확인 시각까지 대기. 시각을 시계에 맞춰서(A: 0초, B: 간격의 절반) 두 서버가 항상 엇갈리게 한다.
    재시작이 있어도 엇갈림이 유지된다."""
    now = time.time()
    phase = (now - SLOT_OFFSET) % INTERVAL
    nxt = now - phase + INTERVAL
    time.sleep(max(1, nxt - now))


def main():
    if not WEBHOOK:
        print("DISCORD_WEBHOOK_URL 환경변수(GitHub Secret)가 없습니다. 디스코드 웹훅 주소를 등록해 주세요.")
        sys.exit(1)
    if SEND_START:
        notify(f"✅ CGV 모니터링 시작: '{KEYWORD}' / {THEATER} / {CUTOFF} 이후 날짜 감시 "
               f"({INTERVAL // 60}분 간격{', 서버 ' + WORKER.upper() if WORKER else ''})")
    if "last_ok" not in STATE:
        set_state(last_ok=time.time())
    blocked_streak, streak, give_up = 0, 0, False
    end = time.time() + LOOP_MINUTES * 60
    with sync_playwright() as p:
        channel = os.getenv("BROWSER_CHANNEL") or None   # "chrome" = 러너에 깔린 Chrome 사용 (브라우저 설치 생략)
        browser = p.chromium.launch(channel=channel, headless=True,
                                    args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"])
        holder = {}
        reset_browser(browser, holder)
        while time.time() < end and not give_up:
            t0, pause = time.time(), None
            holder["fail_shot"] = None
            try:
                enabled, status, img = one_check(browser, holder)
                blocked_streak = 0
                streak += 1
                on_success(enabled, status, img, streak)
            except Blocked as e:
                streak = 0
                blocked_streak += 1
                print("blocked:", e)
                if ON_GITHUB:
                    set_state(block_exits=STATE.get("block_exits", 0) + 1)
                    give_up = blocked_streak >= MAX_BLOCKED   # 종료하면 워크플로가 새 서버(새 IP)로 재시작
                    alert_problem("🚫 CGV가 접속을 막았어요" + (" → 새 서버로 바로 갈아탑니다" if give_up else ""),
                                  str(e), holder)
                    pause = 20
                else:
                    alert_problem("🚫 CGV가 접속을 막았어요", str(e), holder)
                    pause = 600
                gap_watch()
            except Exception as e:
                streak = 0
                print("error:", e)
                alert_problem("⚠️ 확인에 실패했어요 (자동 재시도 3번 모두 실패)", str(e), holder)
                gap_watch()
            print(f"  이번 확인 소요 {time.time() - t0:.1f}초")
            if give_up:
                break
            if pause:
                time.sleep(pause)
            else:
                sleep_until_slot()
        for k in ("page", "ctx"):
            try:
                holder[k].close()
            except Exception:
                pass
        browser.close()
    if give_up:
        # 첫 차단은 대기 없이 바로 재시작. 차단→재시작이 연속될 때만 조금씩 쉬어서 CGV를 계속 두드리지 않게 함
        n = STATE.get("block_exits", 1)
        time.sleep(min(BLOCK_BACKOFF * max(0, n - 1), 120))


if __name__ == "__main__":
    main()
