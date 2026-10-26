import os, json, re, sys, time, email.utils
import datetime as dt
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET

E = os.environ
TG_TOKEN = E["TG_TOKEN"]
TG_CHAT = E["TG_CHAT_ID"]
TEAM_ID = E.get("TEAM_ID", "")
WATCH = [w.strip().lower() for w in (E.get("WATCHLIST") or "").split(",") if w.strip()]
GKEY = E.get("GEMINI_API_KEY", "")
GMODEL = E.get("GEMINI_MODEL") or "gemini-flash-latest"
MANUAL = E.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
FPL = "https://fantasy.premierleague.com/api/"
UA = {"User-Agent": "Mozilla/5.0 (fpl-radar)"}
TRUSTED = ("sky sports", "bbc", "guardian", "athletic", "espn", "premier league",
           "fantasy football scout", "reuters", "official")
BADRE = re.compile(r"injur|doubt|ruled out|rules? out|sidelined|hamstring|knock|setback|suspen|banned|"
                   r"red card|unavailable|will not play|withdrawn|blow|fitness (test|concern)|"
                   r"not in (the )?squad|benched|rotat|misses", re.I)
GOODRE = re.compile(r"returns?\b|returned|back in training|fit again|passed fit|cleared to|"
                    r"available for|comeback", re.I)
now = dt.datetime.now(dt.timezone.utc)
today = now.strftime("%Y-%m-%d")


def P(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def http(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def tg(text):
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text[:3900],
                                       "disable_web_page_preview": "true"}).encode()
        http("https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage", data=data)
    except Exception as e:
        print("telegram error:", e)


def gnews(q):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q + " when:1d", "hl": "en-GB", "gl": "GB", "ceid": "GB:en"})
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        src = it.findtext("source") or ""
        title = it.findtext("title") or ""
        suffix = " - " + src
        if src and title.endswith(suffix):
            title = title[:-len(suffix)]
        out.append((title, it.findtext("link") or "", src, it.findtext("pubDate") or ""))
    return out


def fresh(pub):
    try:
        return (now - email.utils.parsedate_to_datetime(pub)).total_seconds() <= 12 * 3600
    except Exception:
        return False


def gemini(prompt):
    url = "https://generativelanguage.googleapis.com/v1beta/models/" + GMODEL + ":generateContent"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    h = {"Content-Type": "application/json", "x-goog-api-key": GKEY, "User-Agent": "fpl-radar"}
    r = http(url, data=body, headers=h, timeout=90)
    parts = r["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


try:
    S = json.load(open("state.json"))
except Exception:
    S = {}
for k, v in (("p", {}), ("seen", []), ("dl", []), ("fx", {}), ("recent", [])):
    S.setdefault(k, v)
first = "boot" not in S

try:
    boot = http(FPL + "bootstrap-static/")
    fixt = http(FPL + "fixtures/")
except Exception as e:
    print("FPL API unavailable:", e)
    sys.exit(0)

players = {p["id"]: p for p in boot["elements"]}
team = {t["id"]: t["short_name"] for t in boot["teams"]}
tname = {t["id"]: t["name"] for t in boot["teams"]}
nxt = next((e for e in boot["events"] if e.get("is_next")), None)
cur = next((e for e in boot["events"] if e.get("is_current")), None)

squad = set()
squad_ok = True
if TEAM_ID and cur:
    try:
        r = http(FPL + "entry/" + TEAM_ID + "/event/" + str(cur["id"]) + "/picks/")
        squad = {x["element"] for x in r["picks"]}
    except Exception as e:
        squad_ok = False
        print("picks error:", e)
watch = {i for i, p in players.items() if p["web_name"].lower() in WATCH}
mine = squad | watch


def flag(p):
    c = p.get("chance_of_playing_next_round")
    return (str(c) + "%") if c is not None else "100%"


def own(i):
    return float(players[i]["selected_by_percent"])


dl = P(nxt["deadline_time"]) if nxt else None
hrs = (dl - now).total_seconds() / 3600 if dl else 999
soon = [1 for f in fixt if f.get("kickoff_time") and not f.get("finished")
        and 0 <= (P(f["kickoff_time"]) - now).total_seconds() <= 7200]
hot = (0 <= hrs <= 4) or bool(soon)
alerts = []

# 1) Official FPL flag watcher (your players + template players owned 20%+)
for i, p in players.items():
    if not (i in mine or own(i) >= 20):
        continue
    sig = [p["status"], p.get("chance_of_playing_next_round"), p.get("news", "")]
    old = S["p"].get(str(i))
    S["p"][str(i)] = sig
    if first or old is None or old == sig:
        continue
    bad = p["status"] != "a" or (sig[1] is not None and sig[1] < 75)
    yours = i in mine
    head = ("🚨 YOURS" if bad else "✅ YOURS") if yours else ("📣 MARKET" if bad else "✅ MARKET")
    msg = (head + ": " + p["web_name"] + " (" + team[p["team"]] + ") FPL flag " + flag(p) + "\n"
           + (p.get("news") or "news cleared") + "\nOwned " + str(own(i)) + "% | £" + str(p["now_cost"] / 10))
    if bad and yours:
        msg += "\n➡️ Line up a replacement; confirm with team news before spending a hit."
    if bad and not yours:
        msg += "\n➡️ Rivals will move off him: differential chance."
    alerts.append(msg)

# 2) Free news scanner (Google News) with source counting
tmins = (now - P(S["last_news"])).total_seconds() / 60 if S.get("last_news") else 9999
if tmins >= (14 if hot else 55):
    S["last_news"] = now.isoformat()
    hits = {}

    def add(pid, title, link, src):
        cls = "good" if GOODRE.search(title) else ("bad" if BADRE.search(title) else None)
        if cls:
            h = hits.setdefault((pid, cls), {"src": set(), "title": title, "link": link})
            h["src"].add(src or "unknown")

    rx = {}
    for i in players:
        nm = players[i]["web_name"]
        if (own(i) >= 10 and len(nm) >= 4) or i in mine:
            rx[i] = re.compile(r"\b" + re.escape(nm) + r"\b", re.I)
    for i in list(mine)[:25]:
        p = players[i]
        q = ('"' + p["web_name"] + '" ' + tname[p["team"]]
             + ' (injury OR doubt OR "ruled out" OR suspended OR returns OR "team news")')
        try:
            for title, link, src, pub in gnews(q):
                if fresh(pub) and rx[i].search(title):
                    add(i, title, link, src)
        except Exception as e:
            print("news error:", e)
        time.sleep(0.6)
    for q in ("Premier League injury news", "Premier League team news press conference"):
        try:
            for title, link, src, pub in gnews(q):
                if not fresh(pub):
                    continue
                for i, rg in rx.items():
                    if rg.search(title):
                        add(i, title, link, src)
        except Exception as e:
            print("news error:", e)
        time.sleep(0.6)

    for (pid, cls), h in hits.items():
        p = players[pid]
        n = len(h["src"])
        trusted = any(t in s.lower() for s in h["src"] for t in TRUSTED)
        tier = "REPORTED" if trusted else ("LIKELY" if n >= 2 else "RUMOUR")
        yours = pid in mine
        if tier == "RUMOUR" and not yours:
            continue
        if not yours and own(pid) < 10:
            continue
        if cls == "good" and p["status"] == "a":
            continue
        key = "n|" + str(pid) + "|" + cls + "|" + tier + "|" + today
        if key in S["seen"]:
            continue
        S["seen"].append(key)
        if first:
            continue
        S["recent"].append(p["web_name"] + ": " + h["title"] + " (" + ", ".join(sorted(h["src"]))[:60] + ")")
        edge = cls == "bad" and p["status"] == "a" and p.get("chance_of_playing_next_round") in (None, 100)
        if edge:
            icon = "⚡ EDGE"
        elif yours and tier == "REPORTED" and cls == "bad":
            icon = "🚨 YOURS"
        elif yours and tier == "LIKELY":
            icon = "⚠️ YOURS"
        elif yours:
            icon = "👀 YOURS (rumour)"
        else:
            icon = "📣 MARKET"
        msg = (icon + " | " + p["web_name"] + " (" + team[p["team"]] + ") | "
               + ("ISSUE" if cls == "bad" else "RETURN") + " | " + tier + ", " + str(n) + " source(s)\n"
               + h["title"] + "\n" + h["link"] + "\nFPL flag " + flag(p) + " | Owned " + str(own(pid))
               + "% | £" + str(p["now_cost"] / 10))
        if edge:
            msg += "\n⚡ News reports an issue but the FPL flag hasn't changed: the flag may lag."
        if cls == "bad" and yours:
            msg += "\n➡️ Wait for official team news unless REPORTED by a top outlet."
            if 0 < hrs < 100:
                msg += " Deadline in " + str(round(hrs)) + "h."
        elif cls == "bad" and own(pid) >= 20:
            msg += "\n➡️ Rivals will move off him: differential chance."
        alerts.append(msg)
    S["recent"] = S["recent"][-40:]

# 3) Deadline countdown (+ optional free Gemini risk note at 3h)
if nxt and dl and hrs > 0:
    m = next((x for x in (1, 3, 24) if hrs <= x), None)
    if m and (str(nxt["id"]) + "-" + str(m)) not in S["dl"]:
        S["dl"] += [str(nxt["id"]) + "-" + str(x) for x in (1, 3, 24) if x >= m]
        flagged = [players[i] for i in squad if players[i]["status"] != "a"]
        txt = "⏰ GW" + str(nxt["id"]) + " deadline in ~" + str(m) + "h (" + dl.strftime("%a %H:%M") + " UTC)"
        if flagged:
            txt += "\nFlagged in your squad: " + ", ".join(b["web_name"] + " (" + flag(b) + ")" for b in flagged)
        if GKEY and m == 3:
            try:
                prompt = ("You are an FPL assistant. Using ONLY the data below, in under 120 words list my "
                          "players most at risk of not starting and what I should verify before the deadline. "
                          "Do not invent news.\nMy squad and FPL flags: "
                          + ", ".join(players[i]["web_name"] + " " + flag(players[i]) + " "
                                      + (players[i].get("news") or "") for i in sorted(mine))
                          + "\nRecent headlines:\n" + "\n".join(S["recent"][-25:]))
                txt += "\n\n🤖 " + gemini(prompt).strip()
            except Exception as e:
                print("gemini error:", e)
        alerts.append(txt)

# 4) Double / blank gameweek radar
cnt = {}
if nxt:
    for f in fixt:
        ev = f.get("event")
        if ev and nxt["id"] <= ev <= nxt["id"] + 8:
            cnt.setdefault(ev, {})
            for t in (f["team_h"], f["team_a"]):
                cnt[ev][t] = cnt[ev].get(t, 0) + 1
fx = {}
for ev, c in cnt.items():
    d = sorted(team[t] for t, n in c.items() if n > 1)
    b = sorted(team[t] for t in team if t not in c)
    b = b if len(b) <= 10 else []
    if d or b:
        fx[str(ev)] = {"dgw": d, "bgw": b}
yteams = {team[players[i]["team"]] for i in squad}


def fxline(ev, v):
    s = "📅 GW" + ev + ":"
    if v["dgw"]:
        s += " DOUBLE " + ",".join(v["dgw"])
    if v["bgw"]:
        s += " BLANK " + ",".join(v["bgw"])
    hit = yteams & set(v["dgw"] + v["bgw"])
    if hit:
        s += " (your squad: " + ",".join(sorted(hit)) + ")"
    return s


fxlines = [fxline(ev, v) for ev, v in fx.items()]
if not first:
    for ev, v in fx.items():
        if S["fx"].get(ev) != v:
            alerts.append(fxline(ev, v))
S["fx"] = fx

# send
S["seen"] = S["seen"][-400:]
warn = "" if squad_ok else "\n⚠️ Couldn't read your squad. Check TEAM_ID."
if first:
    S["boot"] = now.isoformat()
    tg("✅ FPL Radar online. Tracking " + str(len(mine)) + " of your players. "
       + (("Deadline in " + str(round(hrs)) + "h.") if dl else "") + warn + "\n" + "\n".join(fxlines[:6]))
elif MANUAL:
    tg("✅ Radar run OK. Tracking " + str(len(mine)) + " players. Deadline in " + str(round(hrs)) + "h." + warn)
for a in alerts[:10]:
    tg(a)
json.dump(S, open("state.json", "w"))
