#!/usr/bin/env python3
"""Daily job + drive finder -> scores against your resume -> emails you a digest.
Does NOT auto-apply (bots get accounts banned). You open the link and apply."""
import json, os, re, sys, html, smtplib, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
import requests

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text())
SEEN_FILE = BASE / "seen.json"
UA = {"User-Agent": "Mozilla/5.0 (personal job finder)"}
DRIVE_RE = re.compile(r"off[\s-]?campus|walk[\s-]?in|hiring drive|recruitment drive|mass hiring|freshers? hiring|drive", re.I)
TECH_RE = re.compile(r"software|developer|engineer|programmer|web|full[\s-]?stack|front[\s-]?end|mern|react|java|python|\bit\b|trainee|analyst|associate", re.I)


def strip(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t or ""))).strip()


def parse_date(v):
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, timezone.utc)
        s = str(v)
        try:
            d = parsedate_to_datetime(s)
        except Exception:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def domain(url):
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def on_domain(url, domains):
    h = domain(url)
    return any(h == d or h.endswith("." + d) for d in domains)


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=30, **kw)
    r.raise_for_status()
    return r


# ---------- sources: dicts {id,kind,title,company,location,url,desc,date,source,salary_lpa}
def adzuna():
    aid, key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (aid and key):
        print("Adzuna keys not set: skipping job board", file=sys.stderr); return []
    out = []
    for q in CFG["adzuna_search"]:
        for where in CFG["adzuna_where"]:
            try:
                d = get("https://api.adzuna.com/v1/api/jobs/in/search/1", params=dict(
                    app_id=aid, app_key=key, what=q, where=where, results_per_page=50,
                    max_days_old=CFG["max_age_days"], sort_by="date")).json()
            except Exception as e:
                print("adzuna failed:", e, file=sys.stderr); continue
            for j in d.get("results", []):
                lo = j.get("salary_min")
                out.append(dict(id=f"adzuna-{j['id']}", kind="job", title=strip(j["title"]),
                                company=j.get("company", {}).get("display_name", ""),
                                location=j.get("location", {}).get("display_name", "India"),
                                url=j["redirect_url"], desc=strip(j.get("description")),
                                date=parse_date(j.get("created")), source="Adzuna",
                                salary_lpa=(lo / 1e5 if lo and j.get("salary_is_predicted") != "1" else None)))
    return out


def jooble():
    """India jobs via Jooble. Free key: jooble.org/api/about -> JOOBLE_KEY."""
    key = os.getenv("JOOBLE_KEY")
    if not key:
        print("JOOBLE_KEY not set: skipping Jooble", file=sys.stderr); return []
    out = []
    for q in CFG["jooble_search"]:
        for where in CFG["jooble_where"]:
            try:
                r = requests.post(f"https://jooble.org/api/{key}", headers=UA, timeout=30,
                                  json={"keywords": q, "location": where, "page": 1})
                r.raise_for_status()
                d = r.json()
            except Exception as e:
                print("jooble failed:", e, file=sys.stderr); continue
            for j in d.get("jobs", []):
                out.append(dict(id=f"jooble-{j.get('id') or j.get('link')}", kind="job", title=strip(j.get("title")),
                                company=j.get("company", ""), location=j.get("location") or where or "India",
                                url=j.get("link", ""), desc=strip(j.get("snippet")),
                                date=parse_date(j.get("updated")), source="Jooble", salary_lpa=None))
    return out


def feeds():
    out = []
    for f in CFG["drive_feeds"]:
        try:
            root = ET.fromstring(get(f).content)
        except Exception as e:
            print(f"feed failed {f}: {e}", file=sys.stderr); continue
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            title = strip(it.findtext("title"))
            desc = strip(it.findtext("description") or "")
            enc = it.find("{http://purl.org/rss/1.0/modules/content/}encoded")
            if enc is not None and enc.text:
                desc += " " + strip(enc.text)
            out.append(dict(id=f"feed-{link}", kind="drive", title=title, company=title.split(" - ")[0].split(" | ")[0][:60],
                            location="", url=link, desc=desc, date=parse_date(it.findtext("pubDate")),
                            source=domain(f), salary_lpa=None))
    return out


# ---------- rules
def min_years(text):
    """Smallest experience the job asks for; None if not stated. '0-2 years' -> 0, '3+ years' -> 3."""
    vals = []
    for m in re.finditer(r"(\d+)\s*(?:-|to)?\s*(\d+)?\s*\+?\s*(?:years?|yrs?)\b", text):
        vals.append(int(m.group(1)))
    return min(vals) if vals else None


def batch_ok(text):
    if not re.search(r"batch|pass(?:ed)?[\s-]?out|graduat", text):
        return True
    years = {int(y) for y in re.findall(r"\b(20[12]\d)\b", text)}
    return not years or CFG["batch_year"] in years


def evaluate(j):
    title, desc = j["title"].lower(), j["desc"].lower()
    text = f"{title} {desc}"
    if any(x in title for x in CFG["exclude_title"]): return None
    if any(w in text for w in CFG["scam_words"]): return None
    if on_domain(j["url"], CFG["blocked_link_domains"]) or not j["url"].startswith("http"): return None
    if j["date"] and j["date"] < datetime.now(timezone.utc) - timedelta(days=CFG["max_age_days"]): return None
    my = min_years(text)
    if my is not None and my > CFG["max_min_years"]: return None
    if j.get("salary_lpa") and j["salary_lpa"] > CFG["max_salary_lpa"]: return None
    if not batch_ok(text): return None
    mnc = next((m.strip() for m in CFG["mnc_list"] if re.search(r"(?<![a-z])" + re.escape(m.strip()) + r"(?![a-z])", f"{j['company'].lower()} {title} {desc[:600]}")), None)
    if j["kind"] == "drive":
        if not (DRIVE_RE.search(j["title"]) and TECH_RE.search(text) and mnc): return None
        j["drive_type"] = "Walk-in drive" if re.search(r"walk[\s-]?in", text) else "Off-campus drive"
    else:
        if not any(k in title for k in CFG["title_keywords"]): return None
    loc = f"{j['location']} {text[:1500]}".lower()
    city = next((c for c in CFG["preferred_cities"] if c in loc), None)
    if j["kind"] == "job" and not city: return None
    matched = [s for s in CFG["skills"] if re.search(r"(?<![a-z])" + re.escape(s) + r"(?![a-z])", text)]
    s = 20 + min(len(matched), 8) * 6
    if any(x in title for x in ["react", "next", "full stack", "full-stack", "mern"]): s += 15
    if mnc: s += 15
    if j["kind"] == "drive": s += 10
    if re.search(r"\b(fresher|entry|junior|0-1|0-2|0 to 1|trainee)\b", text): s += 10
    if city and city not in ("india", "pan india"): s += 5
    if j.get("salary_lpa"): s += 5
    j.update(matched=matched, mnc=mnc, city=city, score=min(s, 100),
             official=on_domain(j["url"], CFG["trusted_career_domains"]))
    return j["score"] if j["score"] >= CFG["min_score"] else None


def cover_note(j):
    skills = ", ".join(j["matched"][:5]) or "React and Next.js"
    return (f"Hi {j['company'] or 'team'}, I'm a Full-Stack Developer (B.Tech CSE, 2025) with experience at 121 Greetings, "
            f"where I migrated a legacy site to Next.js (30% faster load), implemented SSR/SEO and integrated Strapi CMS. "
            f"The {j['title']} role matches my work with {skills}. Resume attached. Thank you, {CFG['name']}.")


# ---------- output
def render(items):
    today = datetime.now().strftime("%d %b %Y")
    drives = [j for j in items if j["kind"] == "drive"]
    jobs = [j for j in items if j["kind"] == "job"]

    def card(j):
        tag = "Official / known platform link" if j["official"] else "VERIFY on the company's official careers site before applying"
        sal = f"{j['salary_lpa']:.1f} LPA" if j.get("salary_lpa") else "not listed"
        kind = j.get("drive_type") or "Job"
        return (f"<div style='border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0'>"
                f"<b>{html.escape(j['title'])}</b>{' | ' + html.escape(j['mnc'].upper()) if j['mnc'] else ''} <span style='color:#0a7'>({j['score']}% match)</span><br>"
                f"{kind} | {html.escape(j['city'] or j['location'] or '-').title()} | Salary: {sal} | Source: {html.escape(j['source'])}<br>"
                f"Skills matched: {html.escape(', '.join(j['matched']) or '-')}<br>"
                f"<span style='color:{'#0a7' if j['official'] else '#c60'}'>{tag}</span><br>"
                f"<a href='{html.escape(j['url'])}'>Open and apply</a>"
                f"<details><summary>Cover note</summary><p>{html.escape(cover_note(j))}</p></details></div>")

    body = f"<h2>Job digest: {today}</h2>"
    body += f"<h3>MNC drives and walk-ins ({len(drives)})</h3>" + ("".join(map(card, drives)) or "<p>None today.</p>")
    body += f"<h3>Jobs matching your profile ({len(jobs)})</h3>" + ("".join(map(card, jobs)) or "<p>None today.</p>")
    body += ("<hr><p style='font-size:12px;color:#666'>Safety: never pay any fee for a job or drive. Check every drive on the company's own careers page "
             "before you travel or share documents. Real MNCs never ask for money or WhatsApp-only registration.</p>")
    return today, body


def send_email(subject, body):
    user = (os.getenv("GMAIL_USER") or "").strip()
    pw = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    if not (user and pw):
        print("GMAIL_USER / GMAIL_APP_PASSWORD not set: email skipped", file=sys.stderr); return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, CFG["email_to"]
    msg.attach(MIMEText(body, "html"))
    last = None
    for mode in ("ssl465", "tls587"):
        try:
            if mode == "ssl465":
                s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
            else:
                s = smtplib.SMTP("smtp.gmail.com", 587, timeout=30); s.starttls()
            s.login(user, pw); s.sendmail(user, [CFG["email_to"]], msg.as_string()); s.quit()
            print("email sent via", mode); return
        except Exception as e:
            last = e; print(f"email via {mode} failed: {e!r}", file=sys.stderr)
    raise SystemExit(f"Email failed: {last!r}. Check GMAIL_USER (full address) and GMAIL_APP_PASSWORD (16-char App Password, not your normal password).")


def main():
    pool = {}
    for fn in (jooble, adzuna, feeds):
        for j in fn():
            pool.setdefault(j["id"], j)
    try:
        seen = set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        seen = set()
    picked = [j for j in pool.values() if j["id"] not in seen and evaluate(j) is not None]
    picked.sort(key=lambda x: (-x["official"], -x["score"]))
    picked = picked[: CFG["max_items"]]
    today, body = render(picked)
    (BASE / "digest.html").write_text(body)
    print(f"{len(pool)} fetched, {len(picked)} matched")
    if picked:
        send_email(f"Job digest {today}: {len(picked)} matches", body)
    SEEN_FILE.write_text(json.dumps(sorted(seen | {j["id"] for j in picked})[-5000:]))


if __name__ == "__main__":
    main()