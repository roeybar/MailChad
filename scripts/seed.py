"""One-shot seed: demo template + test company settings + test contacts."""
import sys
sys.path.insert(0, "/app")

from app import db, settings
import hashlib, json
from datetime import datetime, timezone

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def email_hash(email):
    return hashlib.sha256(email.lower().encode()).hexdigest()

def template_hash(subject, from_name, html_body):
    return hashlib.sha256(f"{subject}\n{from_name}\n{html_body}".encode()).hexdigest()

db.init()

# Company settings
company = {
    "company_name":        "Test Co",
    "support_email":       "support@testco.example",
    "email_from":          "hello@testco.example",
    "email_footer_address": "123 Main St, Tel Aviv, Israel",
    "public_host":         "http://localhost:8443",
}
for k, v in company.items():
    settings.set(k, v, updated_by="seed")
    print(f"  set {k}={v!r}")

# Demo template
with open("/demo-template.json") as f:
    t = json.load(f)

th = template_hash(t["subject"], t["from_name"], t["html_body"])
with db.conn() as c:
    c.execute(
        "INSERT OR IGNORE INTO templates "
        "(name, subject, from_name, html_body, text_body, template_hash, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (t["name"], t["subject"], t["from_name"],
         t["html_body"], t.get("text_body", ""), th, now(), now()),
    )
    c.commit()
    tid = c.execute("SELECT id FROM templates WHERE template_hash=?", (th,)).fetchone()[0]
print(f"  template id={tid} name={t['name']!r}")

# Test contacts
contacts = [
    ("alice@testco.example",   "Alice Cohen",   "test,vip"),
    ("bob@testco.example",     "Bob Levi",      "test"),
    ("carol@testco.example",   "Carol Mizrahi", "test"),
    ("dan@testco.example",     "Dan Shapiro",   "test"),
    ("eve@testco.example",     "Eve Peretz",    "test,vip"),
]
with db.conn() as c:
    for email, name, tags in contacts:
        c.execute(
            "INSERT OR IGNORE INTO contacts "
            "(email, name, tags, consent_ts, consent_source) VALUES (?, ?, ?, ?, ?)",
            (email, name, tags, now(), "seed"),
        )
    c.commit()
print(f"  {len(contacts)} contacts seeded")

print("\nDone.")
