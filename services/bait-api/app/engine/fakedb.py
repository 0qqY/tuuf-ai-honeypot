"""Seed'li sahte e-ticaret veritabanı. Gerçek kişisel veri YOK.
Çalıştır: python -m app.engine.fakedb  (dosya varsa atlar)"""
import hashlib
import os
import random
import sqlite3

from faker import Faker

from ..config import settings

SEED = 1337
N_MERCHANTS, N_CUSTOMERS, N_ORDERS = 300, 8000, 20000
PROVIDERS = ["iyzico", "paytr", "stripe", "param"]
PLANS = ["starter", "growth", "pro", "enterprise"]
STATUSES = ["paid", "shipped", "delivered", "refunded", "cancelled"]
SAFE_DOMAINS = ["example.com", "example.net", "example.org"]


def _key(prefix: str, *parts) -> str:
    return prefix + hashlib.sha256(("|".join(map(str, (SEED, *parts)))).encode()).hexdigest()[:24]


def build(path: str) -> None:
    fake = Faker("tr_TR")
    Faker.seed(SEED)
    rng = random.Random(SEED)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE merchants(id INTEGER PRIMARY KEY, name TEXT, domain TEXT, plan TEXT,
        payment_provider TEXT, api_key TEXT, webhook_secret TEXT, created_at TEXT);
    CREATE TABLE customers(id INTEGER PRIMARY KEY, merchant_id INT, full_name TEXT, email TEXT,
        phone TEXT, city TEXT, created_at TEXT);
    CREATE TABLE orders(id INTEGER PRIMARY KEY, merchant_id INT, customer_id INT, total REAL,
        currency TEXT, status TEXT, card_last4 TEXT, created_at TEXT);
    """)
    for i in range(1, N_MERCHANTS + 1):
        name = fake.unique.company().split()[0] + " " + rng.choice(["Butik", "Store", "Market", "Shop"])
        slug = "".join(ch for ch in name.lower() if ch.isalnum())
        db.execute("INSERT INTO merchants VALUES(?,?,?,?,?,?,?,?)", (
            i, name, f"{slug}.sepetly.example", rng.choice(PLANS), rng.choice(PROVIDERS),
            _key(settings.PAYMENT_KEY_PREFIX, "m", i), _key("whsec_", "w", i),
            fake.date_between("-3y", "-30d").isoformat()))
    for i in range(1, N_CUSTOMERS + 1):
        n = fake.name()
        user = "".join(ch for ch in n.lower().replace(" ", ".") if ch.isalnum() or ch == ".")
        db.execute("INSERT INTO customers VALUES(?,?,?,?,?,?,?)", (
            i, rng.randint(1, N_MERCHANTS), n, f"{user}{rng.randint(1, 99)}@{rng.choice(SAFE_DOMAINS)}",
            f"+90 555 {rng.randint(100, 999)} {rng.randint(10, 99)} {rng.randint(10, 99)}",
            fake.city(), fake.date_between("-2y", "today").isoformat()))
    for i in range(100001, 100001 + N_ORDERS):
        db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)", (
            i, rng.randint(1, N_MERCHANTS), rng.randint(1, N_CUSTOMERS), round(rng.uniform(49, 9500), 2),
            "TRY", rng.choice(STATUSES), f"{rng.randint(0, 9999):04d}",
            fake.date_time_between("-1y", "now").isoformat(timespec="seconds")))
    db.commit()
    db.close()


if __name__ == "__main__":
    if not os.path.exists(settings.FAKEDB_PATH):
        build(settings.FAKEDB_PATH)
        print("fakedb built:", settings.FAKEDB_PATH)
