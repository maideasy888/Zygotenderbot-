import argparse, csv, json, os, re, smtplib, ssl, sys, time
import urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

API = "https://ocds-api.etenders.gov.za/api/OCDSReleases"
OUT = Path("tender_output")
SEEN_FILE = OUT / "seen.json"

SECTORS = {
    "Security": ["security services", "guarding", "armed response", "cctv", "access control",
                 "alarm", "surveillance", "patrol", "psira", "monitoring"],
    "Home & Cleaning": ["cleaning", "janitorial", "hygiene", "fumigation", "pest control",
                        "laundry", "domestic", "gardening", "grounds maintenance", "waste removal"],
    "General Goods Supply": ["supply and delivery", "supply of", "procurement of", "stationery",
                             "uniforms", "ppe", "furniture", "consumables", "equipment supply",
                             "electronics", "led", "lighting", "toiletries"],
    "Health & Safety": ["health and safety", "occupational health", "ohs", "first aid", "fire extinguisher",
                        "fire fighting", "safety signage", "risk assessment", "medical waste", "sanitiser"],
}
EXCLUDE = ["construction of", "civil works", "tarring", "bridge", "consulting engineers"]
MIN_SCORE = 1

COMPANY = os.getenv("COMPANY_NAME", "ZYGO")
CONTACT = os.getenv("CONTACT_NAME", "[Your Name]")
PHONE = os.getenv("CONTACT_PHONE", "[Phone]")


def get(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "zygo-tender-bot/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"  retry {i+1}: {e}", file=sys.stderr)
            time.sleep(3 * (i + 1))
    return None


def fetch(days):
    to = datetime.now(timezone.utc).date()
    frm = to - timedelta(days=days)
    q = urllib.parse.urlencode({"PageNumber": 1, "PageSize": 100,
                                "dateFrom": frm.isoformat(), "dateTo": to.isoformat()})
    url, releases, pages = f"{API}?{q}", [], 0
    while url and pages < 200:
        data = get(url)
        if not data:
            break
        releases += data.get("releases", [])
        url = (data.get("links") or {}).get("next")
        pages += 1
    print(f"Fetched {len(releases)} releases over {pages} page(s)")
    return releases


def norm(r):
    t = r.get("tender", {}) or {}
    period = t.get("tenderPeriod", {}) or {}
    docs = [d.get("url") for d in (t.get("documents") or []) if d.get("url")]
    contact = (r.get("buyer", {}) or {}).get("contactPoint", {}) or {}
    return {
        "ocid": r.get("ocid"),
        "title": (t.get("title") or "").strip(),
        "description": (t.get("description") or "").strip(),
        "buyer": (r.get("buyer", {}) or {}).get("name", ""),
        "category": t.get("mainProcurementCategory", ""),
        "status": t.get("status", ""),
        "closing": period.get("endDate", ""),
        "value": (t.get("value", {}) or {}).get("amount", ""),
        "docs": docs,
        "contact": f"{contact.get('name','')} {contact.get('email','')} {contact.get('telephone','')}".strip(),
    }


def is_open(t):
    if not t["closing"]:
        return True
    try:
        end = datetime.fromisoformat(t["closing"].replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return end > datetime.now(timezone.utc)
    except Exception:
        return True


def match(t):
    text = f"{t['title']} {t['description']}".lower()
    if any(x in text for x in EXCLUDE):
        return {}
    hits = {}
    for sector, kws in SECTORS.items():
        n = sum(1 for k in kws if re.search(r"\b" + re.escape(k), text))
        if n >= MIN_SCORE:
            hits[sector] = n
    return hits


def draft(t, sectors):
    slug = re.sub(r"[^A-Za-z0-9]+", "_", t["ocid"] or t["title"])[:60]
    path = OUT / "drafts" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = "\n- PSIRA registration certificate (company + directors)" if "Security" in sectors else ""
    extra += "\n- OHS/First-aid/fire competency certificates for staff" if "Health & Safety" in sectors else ""
    path.write_text(f"""# Bid Draft: {t['title']}
Buyer: {t['buyer']}  |  Closing: {t['closing']}  |  Ref: {t['ocid']}
Sectors matched: {', '.join(sectors)}
Docs: {chr(10).join(t['docs']) or 'see eTenders portal'}
Buyer contact: {t['contact'] or 'see tender document'}

## Cover letter
To: The Bid Committee, {t['buyer']}

Dear Sir/Madam,

RE: {t['title']} ({t['ocid']})

{COMPANY} hereby submits its bid for the above requirement. We confirm that we have read and
accept the bid conditions and specifications, and that our offer is valid for the period stated
in the bid document. Our company is registered on the Central Supplier Database and holds a valid
tax compliance status.

Please find our completed bid forms, compliance documents and pricing schedule attached.

Yours faithfully,
{CONTACT}
{COMPANY} | {PHONE}

## Compliance checklist
- [ ] CSD registration summary report (MAAA number)
- [ ] SARS tax compliance status PIN
- [ ] B-BBEE certificate / sworn affidavit
- [ ] CIPC registration docs, directors' IDs
- [ ] SBD 1 (invitation), SBD 4 (declaration of interest), SBD 6.1 (preference points)
- [ ] Letter of good standing (COIDA)
- [ ] Proof of municipal rates / lease (if requested){extra}
- [ ] Priced schedule, signed and initialled on every page
- [ ] Compulsory briefing attended? (check the tender document)

## Pricing / notes
(fill in)
""", encoding="utf-8")
    return path


def send_email(matches):
    user, pw = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    if not (user and pw):
        print("Email skipped (set GMAIL_USER and GMAIL_APP_PASSWORD).")
        return
    lines = []
    for t, s in matches:
        lines.append(f"[{', '.join(s)}] {t['title']}\n  {t['buyer']} | closes {t['closing']} | R{t['value'] or 'n/a'}\n  {t['ocid']}\n")
    msg = EmailMessage()
    msg["Subject"] = f"ZYGO Tender Bot: {len(matches)} new matching tender(s)"
    msg["From"], msg["To"] = user, os.getenv("ALERT_TO", user)
    msg.set_content("\n".join(lines))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.send_message(msg)
    print("Alert emailed.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    seen = set() if a.all or not SEEN_FILE.exists() else set(json.loads(SEEN_FILE.read_text()))

    tenders = {}
    for r in fetch(a.days):
        t = norm(r)
        if t["ocid"]:
            tenders[t["ocid"]] = t

    matches = []
    for t in tenders.values():
        if t["ocid"] in seen or not is_open(t):
            continue
        s = match(t)
        if s:
            matches.append((t, sorted(s, key=s.get, reverse=True)))

    for t, s in matches:
        draft(t, s)

    if matches:
        with open(OUT / f"matches_{datetime.now():%Y%m%d_%H%M}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ocid", "sectors", "title", "buyer", "closing", "value", "contact", "docs"])
            for t, s in matches:
                w.writerow([t["ocid"], "|".join(s), t["title"], t["buyer"], t["closing"],
                            t["value"], t["contact"], " ".join(t["docs"])])
        send_email(matches)

    SEEN_FILE.write_text(json.dumps(sorted(seen | {t["ocid"] for t, _ in matches})))
    print(f"{len(matches)} new matches. Output in ./{OUT}/ (CSV + drafts/)")


if __name__ == "__main__":
    main()
