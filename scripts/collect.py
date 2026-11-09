"""유튜버 부동산 영상 요약 수집기

channels.json에 적힌 유튜브 채널의 영상을 Gemini로 요약해 docs/videos.json에 저장한다.
- 기존 영상: 채널의 전체 영상 목록(yt-dlp)을 data/backlog.json 대기열에 넣고 차례로 요약한다.
- 새 영상: 채널 RSS로 찾아 대기열보다 먼저 요약한다.
GitHub Actions에서 하루 1번 실행하고, PC에서 직접 실행해도 된다.

    python scripts/collect.py            # 새 영상 + 대기열 요약
    python scripts/collect.py --list     # 채널 영상 목록만 다시 받기 (요약 안 함)
    python scripts/collect.py --keyword 반포   # 제목에 '반포'가 든 영상만 먼저 요약

환경 변수 (PC에서는 gemini_key.txt 파일로도 키를 읽는다)
    GEMINI_API_KEY : Gemini API 키 (필수)
    GEMINI_MODEL   : 사용할 모델 (기본 gemini-3.5-flash)
    MAX_PER_RUN    : 실행 1회에 요약할 영상 수 (기본 20)
    MAX_MINUTES    : 실행 1회에 요약할 영상 길이 합계(분) (기본 420)
    PRIORITY_KEYWORD : 제목에 이 낱말이 든 영상을 먼저 요약 (선택)
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS = os.path.join(ROOT, "channels.json")
OUT = os.path.join(ROOT, "docs", "videos.json")
SEEN = os.path.join(ROOT, "data", "seen.json")
IDS = os.path.join(ROOT, "data", "channel_ids.json")
BACKLOG = os.path.join(ROOT, "data", "backlog.json")
KEYFILE = os.path.join(ROOT, "gemini_key.txt")

MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL") or "gemini-3.1-flash-lite"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN") or 20)
# Gemini 무료 등급은 하루에 유튜브 영상 8시간까지만 넣을 수 있어 여유를 두고 7시간으로 잡는다
# (출처: Gemini API 문서 "Video understanding", https://ai.google.dev/gemini-api/docs/video-understanding)
MAX_MINUTES = int(os.environ.get("MAX_MINUTES") or 420)
UNKNOWN_MINUTES = 30   # 길이를 모르는 영상(RSS)은 30분으로 친다
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (realestate-youtube-summary)"

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def log(*a):
    print(*a, flush=True)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def kst_iso(dt):
    return dt.astimezone(KST).isoformat(timespec="minutes")


def base_item(vid, channel, title, published, minutes=None, desc=""):
    return {
        "id": vid,
        "channel": channel,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        "published": published,
        "minutes": minutes,
        "_desc": desc[:3000],
    }


# ── 채널 주소 → 채널 ID ─────────────────────────────
def channel_id(url, cache):
    m = re.search(r"/channel/(UC[\w-]{22})", url)
    if m:
        return m.group(1)
    if url in cache:
        return cache[url]
    try:
        page = http_get(url)
    except Exception as e:
        log(f"[채널 페이지 열기 실패] {url} {e}")
        return None
    m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
         or re.search(r'"externalId":"(UC[\w-]{22})"', page)
         or re.search(r'"channelId":"(UC[\w-]{22})"', page))
    if not m:
        log(f"[채널 ID를 찾지 못함] {url}")
        return None
    cache[url] = m.group(1)
    return m.group(1)


def resolve(c, ids):
    """채널 또는 재생목록 주소 → (식별자, RSS 주소, yt-dlp 목록 주소들)"""
    m = re.search(r"[?&]list=([\w-]+)", c["url"])
    if m:
        pid = m.group(1)
        return (pid, f"https://www.youtube.com/feeds/videos.xml?playlist_id={pid}",
                [f"https://www.youtube.com/playlist?list={pid}"])
    cid = channel_id(c["url"], ids)
    if not cid:
        return None
    tabs = ["videos"] + (["streams"] if c.get("include_streams") else [])   # 쇼츠는 뺀다
    return (cid, f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
            [f"https://www.youtube.com/channel/{cid}/{t}" for t in tabs])


# ── 새 영상: 채널·재생목록 RSS(최근 15개) ─────────────
def read_feed(feed_url, name):
    root = ET.fromstring(http_get(feed_url))
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", "", NS)
        pub = datetime.fromisoformat(e.findtext("a:published", "", NS).replace("Z", "+00:00"))
        group = e.find("media:group", NS)
        desc = group.findtext("media:description", "", NS) if group is not None else ""
        author = e.findtext("a:author/a:name", "", NS)
        out.append(base_item(vid, name or author or root.findtext("a:title", "", NS),
                             e.findtext("a:title", "", NS), kst_iso(pub), None, desc))
    return out


# ── 기존 영상: yt-dlp로 전체 목록 ─────────────────────
def ytdlp_list(url, extractor_arg, name):
    cmd = [sys.executable, "-m", "yt_dlp", "--flat-playlist", "--dump-json", "--no-warnings",
           "--extractor-args", extractor_arg, url]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600)
    except Exception as e:
        log(f"[목록 받기 실패] {name} {e}")
        return []
    if res.returncode and not res.stdout:
        log(f"[목록 받기 실패] {name} {res.stderr.strip()[:200]}")
        return []
    rows = []
    for line in res.stdout.splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def list_videos(urls, name):
    out = []
    for url in urls:
        # 날짜(approximate_date)와 한국어 원래 제목(lang=ko)을 한 번에 받을 수 없어 두 번 받아 합친다.
        # lang=ko 없이 받으면 유튜브가 자동 번역한 영어 제목이 온다.
        rows = ytdlp_list(url, "youtubetab:approximate_date", name)
        ko = {d.get("id"): d.get("title") for d in ytdlp_list(url, "youtube:lang=ko", name)}
        for d in rows:
            if ko.get(d.get("id")):
                d["title"] = ko[d["id"]]
            ts = d.get("timestamp")
            pub = kst_iso(datetime.fromtimestamp(ts, timezone.utc)) if ts else ""
            mins = round(d["duration"] / 60) if d.get("duration") else None
            if not d.get("id") or not d.get("title") or d["title"] in ("[Private video]", "[Deleted video]"):
                continue
            out.append(base_item(d["id"], name or d.get("channel") or "", d.get("title", ""), pub, mins,
                                 d.get("description") or ""))
    return out


# ── Gemini 요약 ─────────────────────────────────────
# 요약 지침 버전. 지침을 바꾸면 올리고, 이전 버전 요약은 대기열에 다시 넣어 새 지침으로 요약한다.
PROMPT_VERSION = 2

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "related": {"type": "BOOLEAN"},
        "headline": {"type": "STRING"},
        "points": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"time": {"type": "STRING"}, "text": {"type": "STRING"}},
            "required": ["time", "text"]}},
        "criteria": {"type": "ARRAY", "items": {"type": "STRING"}},
        "regions": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["related", "headline", "points", "criteria", "regions"],
}

# 요약 지침 (사용자 요청, 2026-11-09)
# - 목적: 이 유튜버의 투자 시각을 배우는 것. 사실 정리가 아니라 유튜버의 주관적 의견이 중심이다.
# - 유튜버 의견은 지나가듯 한 말까지 한마디도 놓치지 않고 자세히 적는다.
# - 세대수·준공연도 같은 기본 사실은 적지 않는다. 의견의 근거로 쓰일 때만 짧게 덧붙인다.
# - "개인 의견이니 확인이 필요하다" 같은 방어적인 문구는 넣지 않는다. 사용자는 이미 주관적 시각임을 안다.
PROMPT = """다음 유튜브 영상을 요약해 줘. 이 요약의 목적은 이 유튜버의 부동산 투자 시각을 배우는 것이다.
사실 정보가 아니라 유튜버가 어떻게 생각하는지가 가장 중요하다.

작성 원칙
- 유튜버의 주관적인 의견·평가·판단·감상·예측·추천·비추천을 한마디도 놓치지 말고 자세히 적는다.
  특히 단지나 동네를 보면서 지나가듯 던지는 한마디(장점·단점, "나라면 ~", "여기는 ~가 아쉽다",
  "이 가격이면 ~", "사람들이 ~를 모르는데" 같은 말)가 가장 중요하다. 짧게 스친 말도 따로 항목으로 살린다.
- 유튜버가 왜 그렇게 생각하는지, 그 판단의 이유와 논리를 함께 적는다.
- 세대수·준공연도·평형·주차대수 같은 기본 사실은 적지 않는다. 유튜버가 의견의 근거로 든 경우에만
  그 의견 문장 안에 짧게 덧붙인다.
- "개인 의견이다", "확인이 필요하다", "투자에 주의하라" 같은 방어적인 문구는 쓰지 않는다.
- 유튜버의 말투와 표현을 살려 "~라고 본다", "~가 아쉽다고 함"처럼 유튜버의 생각으로 적는다.
- 영상에서 하지 않은 말은 지어내지 않는다.

항목
- related: 부동산(매매·전월세·분양·청약·재건축·정책·금리와 집값, 동네·단지 탐방 등)과 관련된 영상이면 true
- headline: 이 영상에서 유튜버가 말하려는 핵심 주장이나 시각을 한 문장으로
- points: 유튜버의 의견을 영상 흐름 순서대로 빠짐없이. 개수 제한은 없다(보통 10~25개).
  각 항목에 그 말이 나오는 시각(time, "분:초" 형식, 예 "12:34")과 내용(text)을 적는다.
  어느 단지·동네에 대한 의견인지 문장 안에 밝힌다
- criteria: 이 영상에서 드러난 유튜버의 판단 기준이나 투자 원칙 (예: "대장 아파트는 신축보다 대단지 군집성이 결정한다").
  다른 영상에도 적용할 수 있는 일반적인 시각으로 2~6개
- regions: 유튜버가 의견을 말한 지역이나 단지 이름 (없으면 빈 배열)

제목: {title}
채널: {channel}
"""


class QuotaError(Exception):
    pass


def gemini_call(parts, key, use_video):
    cfg = {
        "responseMimeType": "application/json",
        "responseSchema": SCHEMA,
        "temperature": 0.3,
        "maxOutputTokens": 16384,
    }
    if use_video:
        cfg["mediaResolution"] = "MEDIA_RESOLUTION_LOW"  # 화면 토큰을 줄인다 (말 내용이 중심이라 충분)
    data = json.dumps({"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}).encode("utf-8")
    for model, waits in ((MODEL, (0, 40)), (FALLBACK_MODEL, (0,))):
        for wait in waits:
            time.sleep(wait)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json", "x-goog-api-key": key})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    res = json.loads(r.read().decode("utf-8"))
                out = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"])
                log(f"  요약 모델: {model}")
                return json.loads(out)
            except urllib.error.HTTPError as e:
                msg = json.loads(e.read().decode("utf-8", "replace") or "{}").get("error", {}).get("message", "")
                log(f"  [Gemini 오류 {e.code} · {model}] {msg[:200]}")
                if e.code == 429:
                    raise QuotaError(msg)
                if e.code not in (500, 503):
                    return None   # 400 등: 영상을 읽지 못함 → 설명란으로 다시 시도
            except Exception as e:
                log(f"  [Gemini 응답 처리 실패 · {model}] {e}")
                return None
    return None


def summarize(v, key):
    text = PROMPT.format(title=v["title"], channel=v["channel"])
    # 1차: 영상 자체를 보고 요약 (Gemini의 유튜브 주소 입력 기능)
    res = gemini_call([{"file_data": {"file_uri": v["url"]}}, {"text": text}], key, True)
    if res:
        res["source"] = "영상"
        return res
    # 2차: 영상을 읽지 못하면 제목과 설명란으로 요약
    if not v["_desc"].strip():
        return None
    res = gemini_call([{"text": text + "\n설명란:\n" + v["_desc"]}], key, False)
    if res:
        res["source"] = "설명란"
    return res


def get_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key and os.path.exists(KEYFILE):
        with open(KEYFILE, encoding="utf-8-sig") as f:
            key = f.read().strip()
    return key


def main():
    only_list = "--list" in sys.argv
    keyword = sys.argv[sys.argv.index("--keyword") + 1] if "--keyword" in sys.argv else ""
    channels = [c for c in load_json(CHANNELS, []) if c.get("url")]
    data = load_json(OUT, {"items": []})
    seen = load_json(SEEN, {})
    ids = load_json(IDS, {})
    backlog = load_json(BACKLOG, {"listed": {}, "queue": {}})
    now = datetime.now(KST)
    items = {it["id"]: it for it in data.get("items", [])}

    if not channels:
        log("channels.json에 채널이 없습니다. 채널을 추가한 뒤 다시 실행하세요.")
        save_json(OUT, {"updated": now.isoformat(timespec="minutes"), "items": list(items.values())})
        return
    key = get_key()
    if not key and not only_list:
        log("GEMINI_API_KEY가 없습니다.")
        sys.exit(1)

    # 1) 기존 영상 목록: 처음 보는 채널·재생목록(또는 --list)만 받는다
    for c in channels:
        src = resolve(c, ids)
        if not src or not c.get("backfill", True):
            continue
        cid, _, urls = src
        if cid in backlog["listed"] and not only_list:
            continue
        found = list_videos(urls, c.get("name") or "")
        added = 0
        for v in found:
            if v["id"] not in seen and v["id"] not in items and v["id"] not in backlog["queue"]:
                backlog["queue"][v["id"]] = v
                added += 1
        if found:
            backlog["listed"][cid] = now.isoformat(timespec="minutes")
        log(f"[목록] {c.get('name') or c['url']}: 영상 {len(found)}개, 대기열에 {added}개 추가")
    save_json(IDS, ids)
    save_json(BACKLOG, backlog)
    if only_list:
        total = sum((v.get("minutes") or UNKNOWN_MINUTES) for v in backlog["queue"].values())
        log(f"대기열 {len(backlog['queue'])}개, 합계 약 {total // 60}시간 {total % 60}분")
        return

    # 2) 새 영상(RSS)을 먼저, 그다음 대기열(최신 영상부터)
    fresh = []
    for c in channels:
        src = resolve(c, ids)
        if not src:
            continue
        try:
            fresh += [v for v in read_feed(src[1], c.get("name")) if v["id"] not in seen and v["id"] not in items]
        except Exception as e:
            log(f"[RSS 읽기 실패] {c.get('name') or c['url']} {e}")
    fresh_ids = {v["id"] for v in fresh}
    for v in fresh:   # 대기열에 길이 정보가 있으면 함께 쓴다
        if v["id"] in backlog["queue"]:
            v["minutes"] = backlog["queue"][v["id"]].get("minutes")
    # 이전 지침으로 요약한 영상은 대기열에 다시 넣는다 (새 요약이 나올 때까지 이전 요약은 그대로 보인다)
    redo = 0
    for k, it in items.items():
        if it.get("v", 1) < PROMPT_VERSION and k not in backlog["queue"]:
            backlog["queue"][k] = {**{f: it.get(f) for f in ("id", "channel", "title", "url", "thumb",
                                                            "published", "minutes")}, "_desc": ""}
            seen.pop(k, None)
            redo += 1
    if redo:
        log(f"이전 지침으로 요약한 영상 {redo}개를 다시 요약 대기열에 넣음")
    queue = [v for k, v in backlog["queue"].items() if k not in fresh_ids and k not in seen]
    order = os.environ.get("BACKLOG_ORDER", "newest")
    queue.sort(key=lambda v: v["published"] or "", reverse=(order != "oldest"))
    todo = sorted(fresh, key=lambda v: v["published"], reverse=True) + queue
    # 우선 키워드(환경 변수 PRIORITY_KEYWORD): 제목에 키워드가 든 영상을 맨 앞으로 보낸다 (나머지는 그 뒤에 그대로)
    prio = os.environ.get("PRIORITY_KEYWORD", "").strip()
    if prio and not keyword:
        todo = [v for v in todo if prio in v["title"]] + [v for v in todo if prio not in v["title"]]
    # 키워드 우선 요약: 제목에 키워드가 든 영상만 요약한다 (python scripts/collect.py --keyword 반포)
    if keyword:
        todo = [v for v in todo if keyword in v["title"]]
        log(f"키워드 '{keyword}'가 제목에 든 영상 {len(todo)}개만 요약")
    log(f"새 영상 {len(fresh)}개, 대기열 {len(queue)}개")

    used_min, done = 0, 0
    for v in todo:
        mins = v.get("minutes") or UNKNOWN_MINUTES
        if done >= MAX_PER_RUN:
            break
        if used_min + mins > MAX_MINUTES:
            continue   # 남은 한도에 들어가는 더 짧은 영상을 찾는다
        log(f"- [{mins}분] {v['channel']} | {v['title']}")
        try:
            res = summarize(v, key)
        except QuotaError:
            log("하루 무료 한도에 걸려 이번 실행을 멈춥니다. 남은 영상은 다음 실행 때 처리합니다.")
            break
        used_min += mins
        if res is None:
            continue   # 다음 실행 때 다시 시도
        done += 1
        seen[v["id"]] = now.isoformat(timespec="minutes")
        backlog["queue"].pop(v["id"], None)
        if not res.get("related"):
            log("  부동산과 관련 없는 영상이라 건너뜀")
            if v["id"] in items:
                items[v["id"]]["v"] = PROMPT_VERSION   # 이전 요약은 남기고 다시 대기열에 넣지 않는다
            continue
        it = {k: val for k, val in v.items() if not k.startswith("_")}
        it.pop("caution", None)
        it.update({k: res.get(k) for k in ("headline", "points", "criteria", "regions", "source")})
        it["v"] = PROMPT_VERSION
        it["summarized"] = now.isoformat(timespec="minutes")
        items[v["id"]] = it
        # 중간에 멈춰도 결과가 남도록 영상마다 저장
        save_all(now, items, seen, backlog)

    save_all(now, items, seen, backlog)
    log(f"이번 실행 요약 {done}개 (영상 약 {used_min}분) · 남은 대기열 {len(backlog['queue'])}개 · 페이지 영상 {len(items)}개")


def save_all(now, items, seen, backlog):
    kept = sorted(items.values(), key=lambda it: it["published"] or "", reverse=True)
    save_json(OUT, {"updated": now.isoformat(timespec="minutes"), "queue": len(backlog["queue"]), "items": kept})
    save_json(SEEN, seen)
    save_json(BACKLOG, backlog)


if __name__ == "__main__":
    main()
