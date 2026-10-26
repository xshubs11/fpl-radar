import os, json, re, sys, time, unicodedata, email.utils
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
UA = {"User-Agent": "Mozilla/5.0 (compatible; fpl-radar/1.0)"}
TRUSTED = ("sky sports", "bbc", "guardian", "athletic", "espn", "premier league", "fantasy football scout",
           "all about fpl", "fantasy football 247", "fantasy football reports", "reuters", "official")
FEEDS = [
    ("Sky Sports", "https://www.skysports.com/rss/12040"),
    ("The Guardian", "https://www.theguardian.com/football/premierleague/rss"),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("Fantasy Football Scout", "https://www.fantasyfootballscout.co.uk/feed/"),
    ("All About FPL", "https://allaboutfpl.com/feed"),
    ("Fantasy Football 247", "https://www.fantasyfootball247.co.uk/feed"),
    ("FPL Updates", "https://fplupdates.com/feed"),
    ("The FPL Fan", "https://thefplfan.com/feed"),
    ("Fantasy Football Reports", "https://fantasyfootballreports.com/feed"),
    ("FPL Hints", "https://fplhints.com/blog-feed.xml"),
    ("Reddit r/FantasyPL", "https://www.reddit.com/r/FantasyPL/new/.rss"),
]
BADRE = re.compile(r"injur|doubt|ruled out|rules? out|sidelined|hamstring|knock|setback|suspen|banned|"
                   r"red card|unavailable|will not play|withdrawn|blow|fitness (test|concern)|"
                   r"not in (the )?squad|benched|rotat|misses", re.I)
GOODRE = re.compile(r"returns?\b|returned|back in training|fit again|passed fit|cleared to|"
                    r"available for|comeback", re.I)
now = dt.datetime.now(dt.timezone.utc)
today = now.strftime("%Y-%m-%d")


def plain(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def short(s):
    t = s.split(".")[-1].strip()
    return t if len(t) >= 3 else s


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


def age_ok(pub, hmax=14):
    try:
        d = email.utils.parsedate_to_datetime(pub)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except Exception:
            return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return (now - d).total_seconds() <= hmax * 3600


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


def feed(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        root = ET.fromstring(r.read())
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title = link = pub = ""
        for c in el:
            t = c.tag.split("}")[-1]
            if t == "title":
                title = (c.text or "").strip()
            elif t == "link":
                link = c.get("href") or (c.text or "")
            elif t in ("pubDate", "published", "updated", "date") and not pub:
                pub = c.text or ""
        out.append((title, link, pub))
    return out


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

# Telegram commands
want = set()
try:
    ur = http("https://api.telegram.org/bot" + TG_TOKEN + "/getUpdates?timeout=0&offset=" + str(S.get("tg_offset", 0)))
    for u in ur.get("result", []):
        S["tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(TG_CHAT):
            continue
        c = (m.get("text") or "").strip().lower()
        if c.startswith(("/buy", "/transfer", "/suggest", "/ideas")):
            want.add("ideas")
        elif c.startswith(("/lineup", "/team", "/captain", "/xi")):
            want.add("lineup")
        elif c.startswith("/predict"):
            want.add("predict")
        elif c.startswith("/status"):
            want.add("status")
        elif c.startswith(("/help", "/start")):
            want.add("help")
except Exception as e:
    print("updates error:", e)

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
nid = nxt["id"] if nxt else 0

# Your squad incl. pending transfers, bank, buy prices
squad, buy, bank = set(), {}, 0
squad_ok = True
if TEAM_ID and cur:
    try:
        r = http(FPL + "entry/" + TEAM_ID + "/event/" + str(cur["id"]) + "/picks/")
        squad = {x["element"] for x in r["picks"]}
    except Exception as e:
        squad_ok = False
        print("picks error:", e)
    if squad_ok:
        try:
            bank = http(FPL + "entry/" + TEAM_ID + "/").get("last_deadline_bank") or 0
        except Exception as e:
            print("entry error:", e)
        try:
            trs = http(FPL + "entry/" + TEAM_ID + "/transfers/")
            for t in sorted(trs, key=lambda t: t["time"]):
                buy[t["element_in"]] = t["element_in_cost"]
                if nxt and t["event"] == nid:
                    squad.discard(t["element_out"])
                    squad.add(t["element_in"])
                    bank += t["element_out_cost"] - t["element_in_cost"]
        except Exception as e:
            print("transfers error:", e)
WATCHP = [plain(w).lower() for w in WATCH]
watch = {i for i, p in players.items()
         if plain(p["web_name"]).lower() in WATCHP or plain(short(p["web_name"])).lower() in WATCHP}
mine = squad | watch
sqcnt = {}
for i in squad:
    sqcnt[players[i]["team"]] = sqcnt.get(players[i]["team"], 0) + 1


def flag(p):
    c = p.get("chance_of_playing_next_round")
    return (str(c) + "%") if c is not None else "100%"


def own(i):
    return float(players[i]["selected_by_percent"])


def fnum(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


fixl = {t: [] for t in team}
for f in fixt:
    ev = f.get("event")
    if ev and nxt and ev >= nid and not f.get("finished"):
        fixl[f["team_h"]].append((ev, f["team_h_difficulty"], "H", f["team_a"]))
        fixl[f["team_a"]].append((ev, f["team_a_difficulty"], "A", f["team_h"]))
for t in fixl:
    fixl[t].sort()
    fixl[t] = fixl[t][:4]
gws = max(1, cur["id"] if cur else 1)


def fxt(tid):
    return " ".join(team[o] + "(" + ha + ")" for _, _, ha, o in fixl.get(tid, [])) or "no fixtures"


# ---------- Prediction model ----------
HIST = {}
GP = {1: 6, 2: 6, 3: 5, 4: 4}
CSP = {1: 4, 2: 4, 3: 1, 4: 0}
DCT = {2: 10.0, 3: 12.0, 4: 12.0}


def hist(i):
    if i in HIST:
        return HIST[i]
    h = None
    try:
        r = http(FPL + "element-summary/" + str(i) + "/")
        rows = r.get("history", [])[-5:]
        if rows:
            mins = sum(x.get("minutes", 0) for x in rows)
            xg = sum(fnum(x.get("expected_goals")) for x in rows)
            xa = sum(fnum(x.get("expected_assists")) for x in rows)
            h = {"start": sum(1 for x in rows if x.get("minutes", 0) >= 60) / float(len(rows)),
                 "xg90": (xg / mins * 90) if mins >= 90 else None,
                 "xa90": (xa / mins * 90) if mins >= 90 else None}
    except Exception as e:
        print("hist error:", i, e)
    time.sleep(0.25)
    HIST[i] = h
    return h


def per90(p, key, tot):
    v = p.get(key)
    if v not in (None, ""):
        return fnum(v)
    m = fnum(p.get("minutes"))
    return fnum(p.get(tot)) / m * 90 if m > 0 else 0.0


def xp(i, deep=False):
    p = players[i]
    pos = p["element_type"]
    if not nxt:
        return 0.0
    fls = [f for f in fixl.get(p["team"], []) if f[0] == nid]
    if not fls or p["status"] in ("i", "s", "u", "n"):
        return 0.0
    c = p.get("chance_of_playing_next_round")
    av = 1.0 if (p["status"] == "a" and c in (None, 100)) else (c if c is not None else 50) / 100.0
    share = min(1.0, fnum(p.get("minutes")) / (90.0 * gws))
    start = share
    xg90 = per90(p, "expected_goals_per_90", "goals_scored")
    xa90 = per90(p, "expected_assists_per_90", "assists")
    if deep:
        h = hist(i)
        if h:
            start = 0.7 * h["start"] + 0.3 * share
            if h["xg90"] is not None:
                xg90 = 0.6 * h["xg90"] + 0.4 * xg90
            if h["xa90"] is not None:
                xa90 = 0.6 * h["xa90"] + 0.4 * xa90
    p60 = min(1.0, start) * av
    mf = p60 * 0.9 + max(0.0, av - p60) * 0.2
    dc90 = per90(p, "defensive_contribution_per_90", "defensive_contribution")
    bon90 = per90(p, "x_bonus", "bonus")
    sav90 = per90(p, "saves_per_90", "saves")
    total = 0.0
    for ev, d, ha, opp in fls:
        atk = (0.8 + 0.1 * (5 - d)) * (1.05 if ha == "H" else 0.97)
        cs = max(0.05, 0.42 - 0.075 * (d - 1)) * (1.08 if ha == "H" else 0.95)
        pts = 2 * p60 + 0.8 * max(0.0, av - p60)
        pts += mf * (xg90 * GP[pos] + xa90 * 3) * atk
        pts += p60 * cs * CSP[pos]
        if pos in (1, 2):
            pts -= 0.6 * (1 - cs) * p60
        if pos == 1:
            pts += mf * sav90 / 3.0
        if pos in DCT:
            pts += 2 * min(1.0, max(0.0, (dc90 / DCT[pos] - 0.55) / 0.7)) * mf
        pts += bon90 * mf
        total += pts
    return 0.55 * total + 0.45 * fnum(p.get("ep_next"))


def sell(i):
    p = players[i]
    nc = p["now_cost"]
    b = buy.get(i, nc - p.get("cost_change_start", 0))
    return b + (nc - b) // 2 if nc > b else nc


def pl(i):
    q = players[i]
    return q["web_name"] + " (" + team[q["team"]] + " £" + str(q["now_cost"] / 10) + ", " + str(own(i)) + "%)"


def replacements(out, k=2):
    p = players[out]
    budget = (bank + sell(out)) if out in squad else p["now_cost"]
    res = []
    for i, q in players.items():
        if i in squad or q["element_type"] != p["element_type"] or q["now_cost"] > budget:
            continue
        if q["status"] != "a" or q.get("chance_of_playing_next_round") not in (None, 100):
            continue
        if sqcnt.get(q["team"], 0) - (1 if q["team"] == p["team"] else 0) >= 3:
            continue
        s = xp(i)
        if s > 0:
            res.append((s, i))
    res.sort(reverse=True)
    top = sorted(((xp(i, True), i) for s, i in res[:5]), reverse=True)
    return top[:k]


def rep_text(out):
    rs = replacements(out, 2)
    if not rs:
        return ""
    ox = xp(out, True)
    return "\n🛒 Replace with: " + " | ".join(
        pl(i) + " +" + str(round(s - ox, 1)) + " xP (" + fxt(players[i]["team"]) + ")" for s, i in rs)


def ideas():
    if not squad:
        return "🛒 Couldn't read your squad. Check TEAM_ID."
    rows, used = [], set()
    for out in sorted(squad, key=lambda i: xp(i))[:5]:
        ox = xp(out, True)
        for s, i in replacements(out, 2):
            if i in used:
                continue
            gain = s - ox
            if gain >= 0.8 or players[out]["status"] != "a":
                rows.append((gain, out, i))
                used.add(i)
                break
    rows.sort(reverse=True)
    lines = ["🛒 TRANSFER IDEAS (bank £" + str(round(bank / 10, 1)) + "; free transfers unknown)"]
    for gain, out, i in rows[:4]:
        tag = "HIT-worthy" if gain >= 2.0 else ("FT-worthy" if gain >= 1.0 else "marginal")
        dia = " 💎" if own(i) < 10 else ""
        lines.append("OUT " + pl(out) + " " + flag(players[out]) + "\nIN  " + pl(i) + dia + "\n+"
                     + str(round(gain, 1)) + " xP/GW [" + tag + "] " + fxt(players[i]["team"]))
    if len(lines) == 1:
        lines.append("No swap clearly beats your current players. Rolling the transfer is reasonable.")
    tgt = sorted((i for i in players if i not in squad), key=lambda i: -xp(i))[:4]
    lines.append("🔥 Top targets: " + "; ".join(pl(i) for i in tgt))
    return "\n".join(lines)


FORMS = [(3, 4, 3), (3, 5, 2), (4, 4, 2), (4, 3, 3), (4, 5, 1), (5, 3, 2), (5, 4, 1), (5, 2, 3)]


def lineup():
    if not squad or not nxt:
        return "📋 Couldn't read your squad. Check TEAM_ID."
    xpv = {i: xp(i, True) for i in squad}
    by = {1: [], 2: [], 3: [], 4: []}
    for i in squad:
        by[players[i]["element_type"]].append(i)
    for k in by:
        by[k].sort(key=lambda i: -xpv[i])
    best = None
    if by[1]:
        for d, m, f in FORMS:
            if len(by[2]) < d or len(by[3]) < m or len(by[4]) < f:
                continue
            xi = [by[1][0]] + by[2][:d] + by[3][:m] + by[4][:f]
            tot = sum(xpv[i] for i in xi)
            if best is None or tot > best[0]:
                best = (tot, (d, m, f), xi)
    if not best:
        return "📋 Squad incomplete (need 15 players)."
    tot, form, xi = best
    bench = [i for i in by[1] if i != xi[0]] + sorted(
        (i for i in squad if i not in xi and players[i]["element_type"] != 1), key=lambda i: -xpv[i])
    ordered = sorted(xi, key=lambda i: -xpv[i])
    cap = ordered[0]
    vc = next((i for i in ordered[1:] if players[i]["team"] != players[cap]["team"]), ordered[1])

    def fmt(i):
        h = HIST.get(i)
        mark = ""
        if players[i]["status"] != "a" or (h and h["start"] < 0.6):
            mark += "⚠️"
        if sum(1 for f in fixl.get(players[i]["team"], []) if f[0] == nid) >= 2:
            mark += "⭐"
        return players[i]["web_name"] + " " + str(round(xpv[i], 1)) + mark

    lines = ["📋 LINEUP GW" + str(nid) + " (" + "-".join(str(x) for x in form) + ") predicted "
             + str(round(tot, 1)) + " xP"]
    for pos, lab in ((1, "GK"), (2, "DEF"), (3, "MID"), (4, "FWD")):
        lines.append(lab + ": " + ", ".join(fmt(i) for i in xi if players[i]["element_type"] == pos))
    lines.append("👑 C: " + players[cap]["web_name"] + " (xP " + str(round(xpv[cap], 1)) + ", "
                 + fxt(players[cap]["team"]) + ")")
    lines.append("🅥 VC: " + players[vc]["web_name"] + " (xP " + str(round(xpv[vc], 1)) + ")"
                 + (" covers if C misses out" if players[vc]["team"] != players[cap]["team"] else ""))
    alt = next((i for i in ordered[1:5] if own(i) < 15 and xpv[i] >= 0.8 * xpv[cap]), None)
    if alt:
        lines.append("💎 Differential captain: " + players[alt]["web_name"] + " (" + str(own(alt)) + "% owned)")
    lines.append("🪑 Bench: " + " | ".join(str(n + 1) + ") " + fmt(i) for n, i in enumerate(bench)))
    for pos in (2, 3, 4):
        xs = [i for i in xi if players[i]["element_type"] == pos]
        bs = [i for i in bench if players[i]["element_type"] == pos]
        if xs and bs:
            lo = min(xs, key=lambda i: xpv[i])
            hi = max(bs, key=lambda i: xpv[i])
            if xpv[lo] - xpv[hi] < 0.4:
                lines.append("🤏 Close call: " + players[lo]["web_name"] + " vs " + players[hi]["web_name"])
                break
    n2 = sum(1 for i in xi if sum(1 for f in fixl.get(players[i]["team"], []) if f[0] == nid) >= 2)
    if n2:
        lines.append("⭐ " + str(n2) + " starters have a double gameweek")
    lines.append("⚠️ = rotation or fitness risk. Model estimate: verify team news before the deadline.")
    return "\n".join(lines)


def predict():
    if not nxt:
        return "🔮 No upcoming gameweek."
    light = sorted(players, key=lambda i: -xp(i))[:14]
    deep = sorted(((xp(i, True), i) for i in light), reverse=True)[:8]
    lines = ["🔮 TOP PREDICTED SCORERS GW" + str(nid)]
    for n, (s, i) in enumerate(deep):
        lines.append(str(n + 1) + ". " + pl(i) + " xP " + str(round(s, 1)) + " | " + fxt(players[i]["team"]))
    return "\n".join(lines)


def pressure():
    tp = max(boot.get("total_players") or 1, 1)
    out = []
    for i in squad:
        p = players[i]
        net = (p["transfers_in_event"] - p["transfers_out_event"]) / tp * 100
        if abs(net) >= 0.4:
            out.append(p["web_name"] + (" ↑" if net > 0 else " ↓") + " " + str(round(net, 2)) + "%")
    return ("💰 Price pressure (not a guarantee): " + ", ".join(out)) if out else ""


dl = P(nxt["deadline_time"]) if nxt else None
hrs = (dl - now).total_seconds() / 3600 if dl else 999
soon = [1 for f in fixt if f.get("kickoff_time") and not f.get("finished")
        and 0 <= (P(f["kickoff_time"]) - now).total_seconds() <= 7200]
hot = (0 <= hrs <= 4) or bool(soon)
alerts = []

# 1) Official FPL flag watcher
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
        if i in squad:
            msg += rep_text(i)
    if bad and not yours:
        msg += "\n➡️ Rivals will move off him: differential chance."
    alerts.append(msg)

# 2) News scanner: direct feeds (news, FPL blogs, Reddit) + Google News
tmins = (now - P(S["last_news"])).total_seconds() / 60 if S.get("last_news") else 9999
if tmins >= (14 if hot else 55):
    S["last_news"] = now.isoformat()
    hits = {}

    def add(pid, title, link, src):
        cls = "good" if GOODRE.search(title) else ("bad" if BADRE.search(title) else None)
        if cls:
            h = hits.setdefault((pid, cls), {"src": set(), "links": set(), "title": title, "link": link})
            h["src"].add(src or "unknown")
            h["links"].add(link or title)

    rx = {}
    for i in players:
        nm = players[i]["web_name"]
        if (own(i) >= 10 and len(short(nm)) >= 4) or i in mine:
            rx[i] = re.compile(r"\b" + re.escape(plain(short(nm))) + r"\b", re.I)
    for name, url in FEEDS:
        try:
            for title, link, pub in feed(url):
                if not age_ok(pub):
                    continue
                pt = plain(title)
                for i, rg in rx.items():
                    if rg.search(pt):
                        add(i, title, link, name)
        except Exception as e:
            print("feed error:", name, e)
        time.sleep(0.3)
    for i in list(mine)[:25]:
        p = players[i]
        q = ('"' + short(p["web_name"]) + '" ' + tname[p["team"]]
             + ' (injury OR doubt OR "ruled out" OR suspended OR returns OR "team news")')
        try:
            for title, link, src, pub in gnews(q):
                if age_ok(pub) and rx[i].search(plain(title)):
                    add(i, title, link, src)
        except Exception as e:
            print("news error:", e)
        time.sleep(0.5)
    for q in ("Premier League injury news", "Premier League team news press conference", "FPL injury news",
              "FPL press conference team news", 'site:fantasyfootballhub.co.uk injury OR "team news"'):
        try:
            for title, link, src, pub in gnews(q):
                if not age_ok(pub):
                    continue
                pt = plain(title)
                for i, rg in rx.items():
                    if rg.search(pt):
                        add(i, title, link, src)
        except Exception as e:
            print("news error:", e)
        time.sleep(0.5)

    for (pid, cls), h in hits.items():
        p = players[pid]
        n = len(h["src"])
        posts = len(h["links"])
        trusted = any(t in s.lower() for s in h["src"] for t in TRUSTED)
        tier = "REPORTED" if trusted else ("LIKELY" if (n >= 2 or posts >= 3) else "RUMOUR")
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
               + ("ISSUE" if cls == "bad" else "RETURN") + " | " + tier + ", " + str(n) + " source(s), "
               + str(posts) + " post(s)\n" + h["title"] + "\n" + h["link"] + "\nFPL flag " + flag(p)
               + " | Owned " + str(own(pid)) + "% | £" + str(p["now_cost"] / 10))
        if edge:
            msg += "\n⚡ News reports an issue but the FPL flag hasn't changed: the flag may lag."
        if cls == "bad" and yours:
            msg += "\n➡️ Wait for official team news unless REPORTED by a top outlet."
            if 0 < hrs < 100:
                msg += " Deadline in " + str(round(hrs)) + "h."
            if pid in squad and tier != "RUMOUR":
                msg += rep_text(pid)
        elif cls == "bad" and own(pid) >= 20:
            msg += "\n➡️ Rivals will move off him: differential chance."
        alerts.append(msg)
    S["recent"] = S["recent"][-40:]

# 3) Deadline countdown + lineup + ideas (+ optional Gemini second opinion)
if nxt and dl and hrs > 0:
    mk = next((x for x in (1, 3, 24) if hrs <= x), None)
    if mk and (str(nid) + "-" + str(mk)) not in S["dl"]:
        S["dl"] += [str(nid) + "-" + str(x) for x in (1, 3, 24) if x >= mk]
        flagged = [players[i] for i in squad if players[i]["status"] != "a"]
        txt = "⏰ GW" + str(nid) + " deadline in ~" + str(mk) + "h (" + dl.strftime("%a %H:%M") + " UTC)"
        if flagged:
            txt += "\nFlagged in your squad: " + ", ".join(b["web_name"] + " (" + flag(b) + ")" for b in flagged)
        alerts.append(txt)
        if squad:
            lu = lineup()
            alerts.append(lu)
            if mk in (24, 3):
                idea = ideas()
                alerts.append(idea)
                pr = pressure()
                if pr:
                    alerts.append(pr)
                if GKEY and mk == 3:
                    try:
                        prompt = ("You are an FPL assistant. Using ONLY the data below, in under 120 words: say "
                                  "which lineup or transfer suggestions look risky given the headlines and what to "
                                  "verify before the deadline. Do not invent news.\nMy squad and flags: "
                                  + ", ".join(players[i]["web_name"] + " " + flag(players[i]) + " "
                                              + (players[i].get("news") or "") for i in sorted(mine))
                                  + "\nHeadlines:\n" + "\n".join(S["recent"][-25:]) + "\n" + lu + "\n" + idea)
                        alerts.append("🤖 " + gemini(prompt).strip())
                    except Exception as e:
                        print("gemini error:", e)

# 4) Double / blank gameweek radar
cnt = {}
if nxt:
    for f in fixt:
        ev = f.get("event")
        if ev and nid <= ev <= nid + 8:
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

# Replies to your commands
if "help" in want:
    alerts.append("Commands:\n/lineup (best XI, captain, vice, bench)\n/buy (transfer ideas)\n/predict (top predicted "
                  "scorers)\n/status\n/help\nI reply on my next run (about 10-15 min). Predictions are model estimates.")
if "lineup" in want:
    alerts.append(lineup())
if "ideas" in want:
    alerts.append(ideas())
if "predict" in want:
    alerts.append(predict())
if "status" in want or MANUAL:
    alerts.append("✅ Radar OK. Tracking " + str(len(mine)) + " players, squad " + str(len(squad)) + ", bank £"
                  + str(round(bank / 10, 1)) + ". Deadline in " + str(round(hrs)) + "h."
                  + ("" if squad_ok else "\n⚠️ Couldn't read your squad. Check TEAM_ID."))

# send
S["seen"] = S["seen"][-400:]
if first:
    S["boot"] = now.isoformat()
    tg("✅ FPL Radar online. Tracking " + str(len(mine)) + " players. Deadline in " + str(round(hrs)) + "h.\n"
       + "\n".join(fxlines[:6]))
for a in alerts[:14]:
    tg(a)
json.dump(S, open("state.json", "w"))
