"""
CGV 예매 오픈 감시 봇 v4 — 매 확인마다 디스코드에 무조건 메시지가 오는 버전

- 확인할 때마다(기본 3분) 성공이든 차단이든 오류든 디스코드에 메시지가 옵니다.
- 로그가 바로바로 Actions 화면에 찍힙니다 (이전엔 로그가 통째로 비어 보이는 문제가 있었어요).
- 웹훅이 죽었으면 시작하자마자 로그에 크게 표시하고, 시작 중에 죽으면 💥 메시지를 보냅니다.
- (v3 유지) 서버 동시 감시, 차단 시 즉시 갈아타기, 프록시 전환, 날짜 줄 스크린샷
- (v5) 서버 A·B·C는 직접 접속, 서버 D는 무료 한국 프록시 목록에서 CGV가 열리는 것을 찾아 그걸로 접속 (실험용)
- (v6) D가 통과하는 프록시를 찾으면 C → B → A 순서로 나눠 주고, 받은 서버가 프록시로 2번 막히면 다시 직접 접속으로 되돌아감
       (서버끼리는 저장소의 proxy-bus 가지에 작은 파일을 주고받는 방식으로 대화)
"""
import os, re, sys, json, time, random, base64, datetime as dt
from urllib.parse import urlparse, unquote
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

try:   # 출력을 줄 단위로 바로 내보냄 (Actions 로그가 비어 보이지 않도록)
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

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
SLOT_OFFSET = {"a": 0, "b": INTERVAL // 4, "c": INTERVAL // 2, "d": INTERVAL * 3 // 4, "pc": INTERVAL // 8}.get(WORKER, 0)   # 서버끼리 1/4 박자씩 엇갈리게 (180초면 0/45/90/135초)
SEND_START = os.getenv("SEND_START_MESSAGE", "false") == "true"
HEARTBEAT_EVERY = int(os.getenv("HEARTBEAT_EVERY", "10"))  # 연속 N번 성공마다 정상 알림. 0이면 끔
GAP_ALERT_MIN = int(os.getenv("GAP_ALERT_MIN", "60"))      # 이 시간 넘게 확인 못 하면 알림
ERR_NOTICE_MIN = int(os.getenv("ERR_NOTICE_MIN", "0"))     # 문제(🚫/⚠️) 알림 최소 간격(분). 0 = 확인할 때마다 매번 알림
MAX_BLOCKED = int(os.getenv("MAX_BLOCKED", "1"))           # GitHub에서 연속 N번 차단되면 종료 → 새 서버로 재시작 (1 = 즉시)
BLOCK_BACKOFF = int(os.getenv("BLOCK_BACKOFF_SEC", "30"))  # 차단→재시작이 연속될 때만 늘어나는 대기(초). 첫 차단은 대기 없음
SHOT_MODE = os.getenv("SHOT_MODE", "each")                 # each: 확인마다 새 메시지(기본) / edit: 한 메시지를 계속 수정 / change: 상태 바뀔 때만 / off: 안 보냄
MENTION = os.getenv("MENTION", "")                         # 예매 오픈 알림에 붙일 멘션. 예: "@everyone"
HUMAN_ENTRY = os.getenv("HUMAN_ENTRY", "true") == "true"   # 메인 페이지를 먼저 열고 검색 화면으로 가기 (사람처럼). false면 검색 주소로 직행
LIGHT_MODE = os.getenv("LIGHT_MODE", "true") == "true"     # 이미지/폰트/추적 스크립트 안 받아서 빠르게
ON_GITHUB = os.getenv("GITHUB_ACTIONS") == "true"
PROXY_URL = (os.getenv("PROXY_URL") or "").strip()         # 예: http://아이디:비번@주소:포트 (한국 IP 프록시). 비어 있으면 프록시를 쓰지 않음


def _proxy_conf(url):
    if not url:
        return None
    try:
        u = urlparse(url)
        conf = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            conf["username"] = unquote(u.username)
            conf["password"] = unquote(u.password or "")
        return conf
    except Exception:
        print("PROXY_URL 형식이 올바르지 않아 프록시를 쓰지 않습니다 (예: http://아이디:비번@주소:포트)")
        return None


PROXY_FREE = (os.getenv("PROXY_MODE") or "").strip().lower() == "free"   # 서버 D: 무료 프록시를 찾아서 사용
PROXY_CONF = None if PROXY_FREE else _proxy_conf(PROXY_URL)               # D는 유료 프록시 설정을 쓰지 않음
PROXY_TRIES = int(os.getenv("PROXY_TRIES", "5"))                          # 한 번 확인할 때 프록시를 최대 몇 개까지 바꿔 볼지
PROXY_SEARCH_SEC = int(os.getenv("PROXY_SEARCH_SEC", "90"))               # 프록시 후보를 시험하는 제한 시간(초)
PROXY_ALLOW_ANY = os.getenv("PROXY_ALLOW_ANY", "false") == "true"         # 한국 목록에서 못 찾으면 전 세계 목록도 시험할지
PROXY_LIST_URLS = [u.strip() for u in os.getenv("PROXY_LIST_URLS", ",".join([
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/KR/data.txt",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&country=KR&timeout=8000&ssl=all&anonymity=all",
])).split(",") if u.strip()]
PROXY_LIST_URLS_ANY = [u.strip() for u in os.getenv("PROXY_LIST_URLS_ANY", ",".join([
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
])).split(",") if u.strip()]
NAV_TIMEOUT = 25000 if PROXY_FREE else 45000                              # 프록시는 느리거나 죽은 경우가 많아서 일찍 포기
PROXY_NOW = ""                                                            # 지금 쓰는 프록시 (메시지에 표시)
BAD_PROXIES = set()                                                       # 이번 실행에서 안 된 프록시

# 서버끼리 프록시 주고받기 (D → C, B, A)
GH_TOKEN = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
REPO = os.getenv("GITHUB_REPOSITORY", "")
BUS_BRANCH = os.getenv("BUS_BRANCH", "proxy-bus")                         # 주고받는 파일을 두는 가지(브랜치). 없으면 자동으로 만듦
PROXY_SHARE = os.getenv("PROXY_SHARE", "true") == "true"                  # false면 서로 안 주고받고 D 혼자 실험
BUS_ON = ON_GITHUB and PROXY_SHARE and bool(GH_TOKEN) and bool(REPO)
SHARED = BUS_ON and WORKER in ("a", "b", "c") and not PROXY_FREE          # D에게서 프록시를 받을 수 있는 서버
ORDER = ("c", "b", "a")                                                   # 프록시를 나눠 주는 순서
PROXY_MAX_STRIKES = int(os.getenv("PROXY_MAX_STRIKES", "2"))              # 받은 프록시로 이만큼 막히면 직접 접속으로 복귀
REASSIGN_COOLDOWN_MIN = int(os.getenv("REASSIGN_COOLDOWN_MIN", "10"))     # 복귀한 서버에 다시 프록시를 주기 전 쉬는 시간(분)
PROXY_SEARCH_GAP_SEC = int(os.getenv("PROXY_SEARCH_GAP_SEC", "100"))      # 프록시 찾기를 너무 자주 하지 않도록 최소 간격
BUS_SHA = {}
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


class NoProxy(Exception):
    """통과하는 무료 프록시를 하나도 못 찾은 경우 (서버 D)"""


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


# ───────── 디스코드 (모든 메시지 앞에 [A]/[B]와 보낸 시각(초 포함) 표시) ─────────
def stamp(msg):
    """메시지 앞에 '[A] 17:41:23 ' 형태로 서버와 시각(한국 시간)을 붙인다"""
    via = f"🛡️{PROXY_NOW} " if PROXY_NOW else ""
    return f"{TAG}{dt.datetime.now(KST).strftime('%H:%M:%S')} {via}{msg}"


def _log_fail(what, r):
    """디스코드가 거절하면 로그에 이유를 남긴다 (이전엔 조용히 사라져서 원인을 알 수 없었음)"""
    if r is not None and not r.ok:
        print(f"⚠️ 디스코드 {what} 실패: HTTP {r.status_code} {r.text[:150]}")


def notify(msg):
    try:
        r = requests.post(WEBHOOK, json={"content": stamp(msg)[:1900]}, timeout=15)
        _log_fail("전송", r)
    except Exception as e:
        print("discord error:", e)


def post_with_image(msg, img, name="shot.jpg"):
    """새 메시지 + 이미지. 성공하면 메시지 id 반환"""
    try:
        r = requests.post(WEBHOOK, params={"wait": "true"}, data={"content": stamp(msg)[:1900]},
                          files={"file": (name, img, "image/jpeg")}, timeout=30)
        _log_fail("이미지 전송", r)
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
                data={"payload_json": json.dumps({"content": stamp(msg)[:1900], "attachments": []})},
                files={"files[0]": ("shot.jpg", img, "image/jpeg")}, timeout=30)
            if r.ok:
                return
            _log_fail("메시지 수정", r)
        except Exception as e:
            print("discord edit error:", e)
    new = post_with_image(msg, img)
    if new:
        set_state(shot_msg=new)


def send_shot(msg, img):
    """성공한 확인의 상태 메시지. 이미지를 못 찍었어도 글자 메시지는 보낸다"""
    if SHOT_MODE == "off":
        return
    if not img:
        notify(msg + "\n(스크린샷을 찍지 못했어요)")
        return
    if SHOT_MODE == "each":
        post_with_image(msg, img)
    elif SHOT_MODE == "change":
        pass  # 상태가 바뀐 경우는 on_success에서 처리
    else:
        edit_with_image(msg, img)


def check_webhook():
    """시작할 때 웹훅이 살아 있는지 확인. 삭제됐거나 주소가 틀리면 False"""
    try:
        r = requests.get(WEBHOOK, timeout=10)
        name = ""
        try:
            name = r.json().get("name", "")   # 주소(토큰)는 로그에 찍지 않음
        except Exception:
            pass
        print(f"웹훅 확인: HTTP {r.status_code} {name}")
        if r.status_code in (401, 404):
            print("❌ 웹훅이 없어졌거나 주소가 틀려요. 디스코드에서 새 웹훅을 만들어 GitHub Secret(DISCORD_WEBHOOK_URL)에 넣어 주세요.")
            return False
    except Exception as e:
        print("웹훅 확인 중 오류(무시하고 계속):", e)
    return True


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


def launch_browser(p, proxy=None):
    channel = os.getenv("BROWSER_CHANNEL") or None   # "chrome" = 러너에 깔린 Chrome 사용 (브라우저 설치 생략)
    kw = {"proxy": proxy} if proxy else {}
    return p.chromium.launch(channel=channel, headless=True,
                             args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"], **kw)


def use_proxy(holder):
    """직접 접속이 막혔을 때: 프록시를 낀 새 브라우저로 갈아탄다 (이번 실행 동안 유지)"""
    try:
        holder["browser"].close()
    except Exception:
        pass
    holder["browser"] = launch_browser(holder["pw"], PROXY_CONF)
    holder["proxy_on"] = True
    print("직접 접속이 막혀 프록시로 전환합니다")
    notify("🛡️ 직접 접속이 막혀서 프록시로 전환했어요")


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


# ───────── 서버 D: 무료 한국 프록시 찾기 ─────────
_HTML_BLOCK_RE = re.compile(r"비정상적으로|이용이 제한|access denied", re.I)


def _fetch_list(urls):
    """프록시 목록 주소들에서 ip:port만 뽑는다 (socks 종류는 제외)"""
    found = []
    for u in urls:
        try:
            r = requests.get(u, timeout=15)
            if not r.ok:
                print(f"프록시 목록 실패 HTTP {r.status_code}: {u[:70]}")
                continue
            n0 = len(found)
            for line in r.text.splitlines():
                line = line.strip().lower()
                if line.startswith(("socks4", "socks5")):
                    continue
                m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})", line)
                if m:
                    found.append(f"{m.group(1)}:{m.group(2)}")
            print(f"프록시 목록 {len(found) - n0}개: {u[:70]}")
        except Exception as e:
            print("프록시 목록 오류:", str(e)[:80], "|", u[:70])
    return found


def _test_proxy(addr):
    """이 프록시로 CGV 메인이 정상으로 열리는지 가볍게 시험 (브라우저는 안 씀)"""
    px = f"http://{addr}"
    try:
        r = requests.get(BASE + "/", proxies={"http": px, "https": px}, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                                  "Accept-Language": "ko-KR,ko;q=0.9"})
        if r.status_code != 200 or len(r.text) < 2000 or _HTML_BLOCK_RE.search(r.text[:4000]):
            return None
        return addr
    except Exception:
        return None


def find_working_proxies():
    """(통과한 프록시 목록, 시험한 후보 수)"""
    tiers = [PROXY_LIST_URLS] + ([PROXY_LIST_URLS_ANY] if PROXY_ALLOW_ANY else [])
    tested = 0
    for urls in tiers:
        cands = [c for c in dict.fromkeys(_fetch_list(urls)) if c not in BAD_PROXIES]
        random.shuffle(cands)
        cands = cands[:400]
        tested += len(cands)
        print(f"프록시 후보 {len(cands)}개를 시험합니다 (최대 {PROXY_SEARCH_SEC}초)")
        good = []
        ex = ThreadPoolExecutor(max_workers=60)
        futs = [ex.submit(_test_proxy, c) for c in cands]
        try:
            for f in as_completed(futs, timeout=PROXY_SEARCH_SEC):
                r = f.result()
                if r:
                    good.append(r)
                    if len(good) >= 6:
                        break
        except Exception:
            pass   # 제한 시간 끝
        ex.shutdown(wait=False, cancel_futures=True)
        print(f"통과한 프록시 {len(good)}개: {good}")
        if good:
            return good, tested
    return [], tested


def refill_pool(holder, force=False):
    """무료 프록시 후보를 시험해서 통과한 것을 holder['pool']에 보관 (너무 자주 하지 않도록 간격 제한)"""
    now = time.time()
    if not force and now - holder["last_search"] < PROXY_SEARCH_GAP_SEC:
        return
    holder["last_search"] = now
    stage("무료 프록시 찾기")
    good, holder["tested"] = find_working_proxies()
    for g in good:
        if g not in holder["pool"] and g not in BAD_PROXIES:
            holder["pool"].append(g)


def _avail(holder):
    """통과했지만 아직 아무에게도 안 나눠 준 프록시"""
    used = {a["proxy"] for a in holder["assign"].values()}
    return [p for p in holder["pool"] if p not in BAD_PROXIES and p not in used]


def ensure_free_proxy(holder):
    """D 자신이 쓸 프록시를 고르고, 그 프록시로 브라우저를 켠다. 하나도 없으면 None.
    다른 서버에 안 나눠 준 것을 먼저 쓰고, 없으면 나눠 준 것 중 하나를 같이 쓴다."""
    global PROXY_NOW
    if holder.get("proxy"):
        return holder["proxy"]
    pool = [p for p in holder["pool"] if p not in BAD_PROXIES]
    if not pool:
        refill_pool(holder)
        pool = [p for p in holder["pool"] if p not in BAD_PROXIES]
    if not pool:
        PROXY_NOW = ""
        return None
    used = {a["proxy"] for a in holder["assign"].values()}
    proxy = ([p for p in pool if p not in used] or pool)[0]
    try:
        holder["browser"].close()
    except Exception:
        pass
    holder["browser"] = launch_browser(holder["pw"], {"server": f"http://{proxy}"})
    reset_browser(holder["browser"], holder)
    holder["proxy"] = proxy
    PROXY_NOW = proxy
    print("프록시 사용:", proxy)
    return proxy


def one_check_free(holder):
    """서버 D의 확인 1회: 프록시가 안 되면 다음 프록시로 바꿔 가며 최대 PROXY_TRIES번"""
    last = None
    for _ in range(PROXY_TRIES):
        proxy = ensure_free_proxy(holder)
        if not proxy:
            raise NoProxy(f"통과하는 무료 한국 프록시를 못 찾았어요 (후보 {holder.get('tested', 0)}개 시험)")
        try:
            return one_check(holder["browser"], holder, attempts=(1,))
        except Exception as e:
            last = e
            print(f"프록시 {proxy} 실패 → 다음 후보로: {str(e)[:100]}")
            BAD_PROXIES.add(proxy)
            holder["proxy"] = None
    raise last


# ───────── 서버끼리 프록시 주고받기 (저장소의 proxy-bus 가지에 작은 파일로) ─────────
# bus/d.json  : D가 씀. {"seq", "assign": {"c": {"proxy", "seq"}, ...}, "bad": [...]}
# bus/a|b|c.json : 각 서버가 자기 것만 씀. {"mode", "proxy", "released_seq", "bad": [...]}
# 파일은 서버마다 따로라서 두 서버가 같은 파일을 동시에 고치는 일이 없다. 프록시 주소와 숫자만 들어가고 비밀은 넣지 않는다.
def _gh(method, path, **kw):
    return requests.request(method, f"https://api.github.com/repos/{REPO}/{path}",
                            headers={"Authorization": f"Bearer {GH_TOKEN}",
                                     "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"}, timeout=15, **kw)


def bus_init():
    """proxy-bus 가지가 없으면 지금 커밋에서 만든다 (4대가 동시에 만들려 해도 문제없음)"""
    try:
        r = _gh("GET", f"git/ref/heads/{BUS_BRANCH}")
        if r.status_code == 200:
            print(f"proxy-bus 가지 확인: 있음")
            return True
        r = _gh("POST", "git/refs", json={"ref": f"refs/heads/{BUS_BRANCH}", "sha": os.getenv("GITHUB_SHA", "")})
        print(f"proxy-bus 가지 만들기: HTTP {r.status_code}", "" if r.status_code in (201, 422) else r.text[:150])
        return r.status_code in (201, 422)
    except Exception as e:
        print("proxy-bus 준비 오류:", str(e)[:100])
        return False


def bus_read(name):
    """파일 내용(dict). 파일이 아직 없으면 {}, 읽기 실패면 None"""
    try:
        r = _gh("GET", f"contents/bus/{name}.json", params={"ref": BUS_BRANCH})
        if r.status_code == 404:
            BUS_SHA.pop(name, None)
            return {}
        if not r.ok:
            print(f"bus 읽기 실패 {name}: HTTP {r.status_code}")
            return None
        j = r.json()
        BUS_SHA[name] = j["sha"]
        return json.loads(base64.b64decode(j["content"]).decode("utf-8"))
    except Exception as e:
        print("bus 읽기 오류:", str(e)[:100])
        return None


def bus_write(name, data):
    body = {"message": f"bus {name}", "branch": BUS_BRANCH,
            "content": base64.b64encode(json.dumps(data, ensure_ascii=False).encode("utf-8")).decode()}
    for attempt in (1, 2):
        try:
            b = dict(body)
            if BUS_SHA.get(name):
                b["sha"] = BUS_SHA[name]
            r = _gh("PUT", f"contents/bus/{name}.json", json=b)
            if r.ok:
                BUS_SHA[name] = r.json()["content"]["sha"]
                return True
            if r.status_code in (409, 422) and attempt == 1:
                bus_read(name)   # 최신 sha를 받아서 한 번 더
                continue
            print(f"bus 쓰기 실패 {name}: HTTP {r.status_code} {r.text[:120]}")
            return False
        except Exception as e:
            print("bus 쓰기 오류:", str(e)[:100])
            return False
    return False


# --- D(프록시를 찾는 서버) 쪽 ---
def scout_init(holder):
    d = bus_read("d") or {}
    holder["assign"] = d.get("assign") or {}
    holder["seq"] = d.get("seq", 0)
    BAD_PROXIES.update(d.get("bad", []))
    print(f"D 시작: 기존 배정 {holder['assign']}")


def scout_sync(holder):
    """매 확인 전에: ① 서버들의 상태를 읽어 '직접 접속으로 복귀'한 서버의 배정을 거두고
    ② 프록시가 필요한 서버(C → B → A 순서)에 통과한 프록시를 배정해서 파일에 쓴다"""
    now = time.time()
    asg, changed = holder["assign"], False
    for w in ORDER:
        st = bus_read(w)
        if st is None:
            continue
        BAD_PROXIES.update(st.get("bad", []))
        if w in asg and st.get("released_seq", 0) >= asg[w]["seq"]:
            print(f"{w.upper()} 서버가 프록시 {asg[w]['proxy']}를 버리고 직접 접속으로 복귀 → 배정 회수")
            BAD_PROXIES.add(asg[w]["proxy"])
            del asg[w]
            holder["cool"][w] = now + REASSIGN_COOLDOWN_MIN * 60
            changed = True
    need = [w for w in ORDER if w not in asg and now >= holder["cool"].get(w, 0)]
    if need:
        if len(_avail(holder)) < len(need):
            refill_pool(holder)
        avail, given = _avail(holder), []
        for w in need:
            if not avail:
                break
            p = avail.pop(0)
            holder["seq"] += 1
            asg[w] = {"proxy": p, "seq": holder["seq"]}
            given.append(f"{w.upper()}←{p}")
            changed = True
        if given:
            notify("📤 프록시 배정: " + ", ".join(given))
    if changed:
        bus_write("d", {"updated": int(now), "seq": holder["seq"], "assign": asg, "bad": sorted(BAD_PROXIES)[-100:]})


# --- A, B, C(프록시를 받는 서버) 쪽 ---
def worker_init(holder):
    holder["my_status"] = bus_read(WORKER) or {}


def _write_my_status(holder, **extra):
    st = holder["my_status"]
    st.update(extra)
    st.update({"updated": int(time.time()), "mode": "proxy" if holder.get("proxy") else "direct",
               "proxy": holder.get("proxy") or ""})
    bus_write(WORKER, st)


def apply_shared_proxy(holder, asg):
    """D가 배정한 프록시로 브라우저를 새로 켠다"""
    global PROXY_NOW
    proxy = asg["proxy"]
    try:
        holder["browser"].close()
    except Exception:
        pass
    holder["browser"] = launch_browser(holder["pw"], {"server": f"http://{proxy}"})
    reset_browser(holder["browser"], holder)
    holder.update(proxy=proxy, strikes=0, seq_applied=asg["seq"])
    PROXY_NOW = proxy
    print(f"D가 배정한 프록시로 전환: {proxy} (seq {asg['seq']})")
    notify(f"🛡️ D가 찾은 프록시를 받아서 쓰기 시작해요 ({proxy})")
    _write_my_status(holder)


def release_shared_proxy(holder, why, mark_bad=True):
    """프록시를 놓고 직접 접속으로 되돌아간다. mark_bad면 이 프록시는 다시 안 쓴다"""
    global PROXY_NOW
    proxy = holder.get("proxy")
    if mark_bad and proxy:
        BAD_PROXIES.add(proxy)
    holder["proxy"] = None
    PROXY_NOW = ""
    try:
        holder["browser"].close()
    except Exception:
        pass
    holder["browser"] = launch_browser(holder["pw"])
    reset_browser(holder["browser"], holder)
    extra = {"released_seq": holder.get("seq_applied", 0)}
    if mark_bad and proxy:
        extra["bad"] = (holder["my_status"].get("bad", []) + [proxy])[-20:]
    print(f"직접 접속으로 복귀: {why}")
    notify(f"↩️ {why} — 직접 접속으로 돌아갑니다")
    _write_my_status(holder, **extra)


def worker_pull(holder):
    """매 확인 전에: D의 파일에서 내 배정을 읽어 새 프록시가 있으면 갈아타고, 배정이 사라졌으면 직접 접속으로"""
    d = bus_read("d")
    if d is None:
        return
    BAD_PROXIES.update(d.get("bad", []))
    asg = (d.get("assign") or {}).get(WORKER)
    released = holder["my_status"].get("released_seq", 0)
    if asg and asg["seq"] > released and asg["seq"] != holder.get("seq_applied") and asg["proxy"] not in BAD_PROXIES:
        apply_shared_proxy(holder, asg)
    elif holder.get("proxy") and not asg:
        release_shared_proxy(holder, "D가 프록시 배정을 거두었어요", mark_bad=False)


def one_check_shared(holder):
    """받은 프록시로 확인. PROXY_MAX_STRIKES번 막히면 프록시를 버리고 직접 접속으로 같은 확인을 이어서 한다"""
    for _ in range(PROXY_MAX_STRIKES):
        try:
            res = one_check(holder["browser"], holder, attempts=(1,))
            holder["strikes"] = 0
            return res
        except Exception as e:
            holder["strikes"] = holder.get("strikes", 0) + 1
            print(f"받은 프록시 {holder['proxy']} 실패 {holder['strikes']}/{PROXY_MAX_STRIKES}: {str(e)[:100]}")
    release_shared_proxy(holder, f"받은 프록시가 {PROXY_MAX_STRIKES}번 막혀서")
    return one_check(holder["browser"], holder)


def safe_call(fn, *a):
    try:
        return fn(*a)
    except Exception as e:   # 서버끼리 대화가 실패해도 감시는 계속
        print(f"({fn.__name__} 오류, 무시하고 계속):", str(e)[:120])


# ───────── 예매 화면 열기 ─────────
def enter_search_like_human(page):
    """사람처럼: 메인 페이지 → 잠깐 머무름 → 검색 화면으로 이동.
    단계 이름이 달라서, 어디서 막히는지(메인부터 막힘 / 검색에서만 막힘) 메시지만 봐도 알 수 있다."""
    stage("메인 페이지 접속")
    resp = page.goto(BASE, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    if resp is not None and resp.status in (403, 429):
        raise Blocked(f"메인 페이지부터 HTTP {resp.status}")
    page.wait_for_timeout(1000)   # 화면이 그려질 시간 (아직 비어 있어도 빈 화면으로 오해하지 않도록 차단 문구만 확인)
    if BLOCK_RE.search(body_text(page)):
        raise Blocked(f"메인 페이지 접속 제한: {body_text(page)[:80]}")

    stage("메인 페이지 둘러보기")
    try:
        page.mouse.move(400 + random.randint(0, 300), 250 + random.randint(0, 200), steps=8)
        page.wait_for_timeout(random.randint(1500, 3000))
        page.mouse.wheel(0, random.randint(150, 400))
        page.wait_for_timeout(random.randint(500, 1200))
    except Exception:
        pass

    stage("검색 화면으로 이동")
    # 1) 메인 화면의 검색 링크/버튼을 눌러 이동
    for make in (lambda: page.locator("a[href*='itgrSrch']").first,
                 lambda: page.get_by_role("link", name=re.compile("검색")).first,
                 lambda: page.get_by_role("button", name=re.compile("검색")).first):
        try:
            make().click(timeout=4000)
            page.wait_for_url(re.compile(r"itgrSrch|search", re.I), timeout=8000)
            return
        except Exception:
            continue
    # 2) 못 찾으면 주소로 이동하되, 메인에서 온 것처럼 이전 페이지(referer)를 알려줌
    resp = page.goto(f"{BASE}/tme/itgrSrch", wait_until="domcontentloaded", timeout=NAV_TIMEOUT, referer=BASE)
    if resp is not None and resp.status in (403, 429):
        raise Blocked(f"검색 페이지에서 HTTP {resp.status} (메인은 통과)")


def open_via_search(page):
    """처음부터: 통합검색 → 작품 → 예매하기 (느리지만 확실한 경로)"""
    if HUMAN_ENTRY:
        enter_search_like_human(page)
    else:
        stage("검색 페이지 접속")
        resp = page.goto(f"{BASE}/tme/itgrSrch", wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
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


def one_check(browser, holder, attempts=(1, 2, 3)):
    """확인 1회. 실패하면 단계적으로 다시 시도:
       1차) 열려 있는 페이지로 빠른 확인
       2차) 브라우저를 새로 만들고 처음부터(검색 경로)
       3차) 잠깐 쉬고 한 번 더. 그래도 안 되면 실패 순간의 화면을 holder['fail_shot']에 남김
       접속 제한(403/429, 차단 문구)처럼 보여도 새 브라우저로 처음부터 한 번 더 확인해서(약 10초)
       그래도 막히면 그때 Blocked로 올려보낸다. '빈 화면'은 3차까지 재시도한 뒤 차단으로 본다."""
    last = None
    for attempt in attempts:
        try:
            if attempt > 1 or not holder.get("page") or holder["page"].is_closed():
                reset_browser(holder["browser"], holder)
                if attempt > 1:
                    time.sleep(3 * attempt)
            return check(holder["page"], force_full=(attempt > 1))
        except Blocked as e:
            holder["fail_shot"] = snap(holder["page"], full_page=False)
            holder["fail_stage"] = STAGE
            if attempt < (3 if e.soft else 2):   # 차단 판정 전에 새 브라우저로 한 번 더 확인 (빈 화면은 3번까지)
                last = e
                print(f"blocked? (try {attempt}, 단계: {STAGE}):", e)
                if not e.soft and PROXY_CONF and not holder.get("proxy_on") and len(attempts) > 1:
                    use_proxy(holder)   # 직접 접속이 막혔으니 다음 시도부터 한국 IP 프록시로
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
    """문제 알림 + 그 순간 화면. 기본(ERR_NOTICE_MIN=0)은 확인할 때마다 매번 보낸다"""
    now = time.time()
    set_state(problem=True)
    if ERR_NOTICE_MIN > 0 and now - STATE.get("last_err_notice", 0) < ERR_NOTICE_MIN * 60:
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
        notify(f"✅ 다시 정상적으로 확인되고 있어요 | 날짜: {status}")
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

    # 날짜 화면 스크린샷 (확인할 때마다)
    near = [d for d in enabled if d >= CUTOFF]
    caption = (f"📅 날짜 상태: {status}\n{CUTOFF} 이후 열림: "
               + (", ".join(d.strftime("%m/%d") for d in near) if near else "없음")
               + f" | 연속 {streak}회 정상")
    if SHOT_MODE == "change":
        if changed or "shot_sent" not in STATE:
            post_with_image(caption, img) if img else notify(caption)
            set_state(shot_sent=True)
    else:
        send_shot(caption, img)

    # 정상 작동 알림: 연속 HEARTBEAT_EVERY번 성공할 때마다
    if HEARTBEAT_EVERY and streak % HEARTBEAT_EVERY == 0:
        minutes = streak * INTERVAL // 60
        notify(f"🔄 정상 작동 중 — 연속 {streak}회 성공(약 {minutes}분) | 날짜: {status} | "
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
    print(f"CGV 감시 시작 — 서버 {WORKER.upper() or '단독'}, 간격 {INTERVAL}초, 모드 {SHOT_MODE}, "
          f"GitHub={ON_GITHUB}, 브라우저={os.getenv('BROWSER_CHANNEL') or '기본'}")
    if not WEBHOOK:
        print("DISCORD_WEBHOOK_URL 환경변수(GitHub Secret)가 없습니다. 디스코드 웹훅 주소를 등록해 주세요.")
        sys.exit(1)
    if not check_webhook():
        if ON_GITHUB:
            time.sleep(300)   # 웹훅이 죽은 채로 재시작이 계속 반복되며 시간을 쓰지 않도록
        sys.exit(1)
    if SEND_START:
        notify(f"✅ CGV 모니터링 시작: '{KEYWORD}' / {THEATER} / {CUTOFF} 이후 날짜 감시 "
               f"({INTERVAL // 60}분 간격{', 서버 ' + WORKER.upper() if WORKER else ''})")
    if "last_ok" not in STATE:
        set_state(last_ok=time.time())
    blocked_streak, streak, give_up = 0, 0, False
    end = time.time() + LOOP_MINUTES * 60
    with sync_playwright() as p:
        holder = {"pw": p, "proxy_on": False, "proxy": None, "pool": [], "assign": {}, "seq": 0, "cool": {},
                  "last_search": 0, "tested": 0, "strikes": 0, "seq_applied": 0, "my_status": {}}
        if PROXY_FREE:
            holder["browser"] = None   # 프록시를 찾은 뒤에 브라우저를 켠다
        else:
            holder["browser"] = launch_browser(p)
            reset_browser(holder["browser"], holder)
        if BUS_ON:
            safe_call(bus_init)
            if PROXY_FREE:
                safe_call(scout_init, holder)
            elif SHARED:
                safe_call(worker_init, holder)
        print("브라우저 준비 완료 — 첫 확인을 시작합니다")
        while time.time() < end and not give_up:
            t0, pause = time.time(), None
            holder["fail_shot"] = None
            if BUS_ON:   # 확인 전에 서버끼리 프록시 주고받기
                safe_call(scout_sync if PROXY_FREE else worker_pull, holder) if (PROXY_FREE or SHARED) else None
            try:
                if PROXY_FREE:
                    enabled, status, img = one_check_free(holder)
                elif holder.get("proxy"):
                    enabled, status, img = one_check_shared(holder)
                else:
                    enabled, status, img = one_check(holder["browser"], holder)
                blocked_streak = 0
                streak += 1
                on_success(enabled, status, img, streak)
            except NoProxy as e:
                streak = 0
                print("proxy:", e)
                notify(f"🔎 {e} — 다음 확인 때 다시 찾아볼게요")
            except Blocked as e:
                streak = 0
                blocked_streak += 1
                print("blocked:", e)
                if ON_GITHUB and not PROXY_FREE:
                    set_state(block_exits=STATE.get("block_exits", 0) + 1)
                    give_up = blocked_streak >= MAX_BLOCKED   # 종료하면 워크플로가 새 서버(새 IP)로 재시작
                    alert_problem("🚫 CGV가 접속을 막았어요" + (" → 새 서버로 바로 갈아탑니다" if give_up else ""),
                                  str(e), holder)
                    pause = 20
                else:
                    alert_problem("🚫 CGV가 접속을 막았어요", str(e), holder)
                    pause = None if PROXY_FREE else 600   # 서버 D는 프록시를 바꿔 가며 다음 확인 때 다시
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
        try:
            holder["browser"].close()
        except Exception:
            pass
    if give_up:
        # 첫 차단은 대기 없이 바로 재시작. 차단→재시작이 연속될 때만 조금씩 쉬어서 CGV를 계속 두드리지 않게 함
        n = STATE.get("block_exits", 1)
        time.sleep(min(BLOCK_BACKOFF * max(0, n - 1), 120))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # 시작 중에 죽어도 조용히 사라지지 않도록 디스코드에 알리고, 재시작이 쉴 새 없이 반복되지 않게 잠깐 쉰다
        print("💥 치명적 오류:", repr(e))
        notify(f"💥 봇이 중간에 멈췄어요: {str(e)[:300]}")
        if ON_GITHUB:
            time.sleep(60)
        raise
