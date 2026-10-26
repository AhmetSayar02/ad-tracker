import os
import csv
import json
import sqlite3
import re
from datetime import datetime
from io import BytesIO, StringIO
from urllib.parse import unquote, urlparse, parse_qs
import urllib.request

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from zoneinfo import ZoneInfo
import pandas as pd

# Konfigürasyon ve Güvenlik Ayarları
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "ads.db"))
EXPORT_DIR = os.getenv("EXPORT_DIR", os.path.join(BASE_DIR, "exports"))
STATIC_DIR = os.getenv("STATIC_DIR", os.path.join(BASE_DIR, "static"))

os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

TZ_GMT3 = ZoneInfo("Europe/Istanbul")

app = FastAPI(
    title="Meta Ad Tracker & Analytics Engine",
    description="Automated scraping, batch processing and Google Sheets pipeline for Meta Ads",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/exports", StaticFiles(directory=EXPORT_DIR), name="exports")

PANEL_USER = os.getenv("PANEL_USER", "admin")
PANEL_PASS = os.getenv("PANEL_PASS", "YOUR_PANEL_PASSWORD")
API_KEY = os.getenv("API_KEY", "YOUR_API_SECRET_TOKEN")

TR_MONTHS = {
    "oca": 1, "şub": 2, "sub": 2, "mar": 3, "nis": 4, "may": 5, "haz": 6,
    "tem": 7, "ağu": 8, "agu": 8, "eyl": 9, "eki": 10, "kas": 11, "ara": 12
}

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT NOT NULL,
        country TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_scanned_at TEXT,
        status TEXT DEFAULT 'IDLE',
        UNIQUE(keyword, country)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ads (
        id TEXT PRIMARY KEY,
        page_name TEXT,
        keyword TEXT,
        country TEXT,
        status TEXT,
        platforms TEXT,
        format TEXT,
        cta_text TEXT,
        days_active INTEGER DEFAULT 1,
        copies_count INTEGER DEFAULT 1,
        landing_url TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        target_url TEXT,
        media_url TEXT,
        first_seen TEXT,
        last_seen TEXT,
        ad_text TEXT
    )
    """)
    new_cols = [
        ("days_active", "INTEGER DEFAULT 1"),
        ("copies_count", "INTEGER DEFAULT 1"),
        ("landing_url", "TEXT DEFAULT ''"),
        ("phone", "TEXT DEFAULT ''")
    ]
    for col_name, col_type in new_cols:
        try:
            cur.execute(f"ALTER TABLE ads ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass
    try:
        cur.execute("ALTER TABLE targets ADD COLUMN status TEXT DEFAULT 'IDLE'")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE targets ADD COLUMN last_scanned_at TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

init_db()

def is_authorized(request: Request) -> bool:
    if request.cookies.get("auth_session") == "authenticated":
        return True
    if request.headers.get("X-API-Key") == API_KEY:
        return True
    if request.query_params.get("key") == API_KEY:
        return True
    return False

def clean_redirect_url(url: str) -> str:
    if not url: return ""
    if "l.facebook.com/l.php" in url or "lm.facebook.com/l.php" in url:
        try:
            qs = parse_qs(urlparse(url).query)
            if "u" in qs and qs["u"]:
                return unquote(qs["u"][0])
        except Exception:
            pass
    return url

def extract_phone_number(text: str, landing_url: str = "") -> str:
    combined = f"{text or ''} {landing_url or ''}"
    wa_match = re.search(r'(?:wa\.me/|phone=)(\+?905\d{9}|05\d{9}|5\d{9})', combined)
    if wa_match:
        return wa_match.group(1)
    phone_match = re.search(r'(?:\+90|0)?\s*5\d{2}[\s\-\.\)]*\d{3}[\s\-\.]*\d{2}[\s\-\.]*\d{2}', combined)
    if phone_match:
        raw = re.sub(r'\D', '', phone_match.group(0))
        if raw.startswith("90") and len(raw) == 12: return "+" + raw
        elif raw.startswith("5") and len(raw) == 10: return "0" + raw
        return raw
    return ""

def calculate_days_active(text: str, start_ts=None) -> tuple[int, str]:
    today = datetime.now(TZ_GMT3).replace(tzinfo=None)
    today_str = today.strftime("%Y-%m-%d")
    if start_ts and isinstance(start_ts, (int, float)) and start_ts > 1000000000:
        try:
            start_dt = datetime.fromtimestamp(start_ts, TZ_GMT3).replace(tzinfo=None)
            return max(1, (today - start_dt).days + 1), start_dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    match = re.search(r'(\d{1,2})\s+([A-Za-zÇĞIİÖŞÜçğıöşü]{3})\s+(\d{4})\s+tarihinde', text or "")
    if match:
        day, month_str, year = int(match.group(1)), match.group(2).lower(), int(match.group(3))
        month = TR_MONTHS.get(month_str)
        if month:
            try:
                start_dt = datetime(year, month, day)
                return max(1, (today - start_dt).days + 1), start_dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
    return 1, today_str

def get_query_df(keyword: str, country: str):
    conn = get_db()
    df = pd.read_sql_query("""
    SELECT
        id as [Kütüphane Kodu],
        page_name as [Sayfa Adı],
        status as [Durum],
        copies_count as [Kopya / Varyasyon Sayısı],
        days_active as [Yayında Kaldığı Gün],
        platforms as [Platformlar],
        format as [Format],
        cta_text as [Buton (CTA)],
        phone as [Telefon / WhatsApp],
        landing_url as [Yönlendirilen Site / Link],
        first_seen as [Başlangıç Tarihi],
        last_seen as [Son Görülme],
        target_url as [Reklam Kütüphanesi Linki],
        media_url as [Medya Görsel URL],
        ad_text as [Reklam Metni]
    FROM ads
    WHERE LOWER(keyword) = LOWER(?) AND UPPER(country) = UPPER(?)
    ORDER BY copies_count DESC, days_active DESC, id DESC
    """, conn, params=(keyword.strip(), country.strip()))
    conn.close()
    return df

def update_local_files_and_sheets(keyword: str, country: str):
    try:
        df = get_query_df(keyword, country)
        safe_name = f"reklamlar_{keyword.lower().replace(' ', '_')}_{country.upper()}"

        xlsx_path = os.path.join(EXPORT_DIR, f"{safe_name}.xlsx")
        with pd.ExcelWriter(xlsx_path, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Reklam Analizi')

        csv_path = os.path.join(EXPORT_DIR, f"{safe_name}.csv")
        df_clean = df.copy()
        if "Reklam Metni" in df_clean.columns:
            df_clean["Reklam Metni"] = df_clean["Reklam Metni"].astype(str).str.replace(r'[\r\n]+', ' | ', regex=True)
        df_clean.to_csv(csv_path, index=False, encoding="utf-8-sig")

        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key = 'sheets_webhook_url'").fetchone()
        conn.close()

        if row and row[0] and row[0].startswith("https://script.google.com/"):
            webhook_url = row[0].strip()
            headers = df_clean.columns.tolist()
            rows_data = df_clean.fillna("").values.tolist()
            payload = json.dumps({
                "secret": os.getenv("SHEETS_SECRET", "YOUR_SHEETS_SECRET_KEY"),
                "sheetName": f"{keyword.upper()} ({country.upper()})",
                "headers": headers,
                "rows": rows_data
            }).encode("utf-8")
            req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[Dosya/Sheets Güncelleme Hatası]: {e}", flush=True)

@app.get("/api/ext/init")
def ext_init(keyword: str, country: str = "TR", request: Request = None):
    if not is_authorized(request): raise HTTPException(401)
    kw, cty = keyword.strip().lower(), country.strip().upper()
    conn = get_db()
    cur = conn.cursor()
    now_str = datetime.now(TZ_GMT3).strftime("%Y-%m-%d %H:%M")

    existing_target = cur.execute(
        "SELECT id FROM targets WHERE LOWER(keyword) = ? AND UPPER(country) = ?",
        (kw, cty)
    ).fetchone()

    if existing_target:
        cur.execute("UPDATE targets SET status = 'TARANIYOR' WHERE id = ?", (existing_target[0],))
    else:
        cur.execute("INSERT INTO targets (keyword, country, created_at, status) VALUES (?, ?, ?, 'TARANIYOR')", (kw, cty, now_str))

    conn.commit()
    existing_ids = [r[0] for r in cur.execute("SELECT id FROM ads WHERE LOWER(keyword) = ? AND UPPER(country) = ?", (kw, cty)).fetchall()]
    conn.close()
    return {"status": "ok", "existing_ids": existing_ids, "total_existing": len(existing_ids)}

@app.post("/api/ext/batch")
async def ext_save_batch(request: Request):
    if not is_authorized(request): raise HTTPException(401)
    data = await request.json()
    kw = data.get("keyword", "").strip().lower()
    cty = data.get("country", "TR").strip().upper()
    items = data.get("ads", [])
    today_str = datetime.now(TZ_GMT3).strftime("%Y-%m-%d")

    conn = get_db()
    cur = conn.cursor()
    for c in items:
        ad_id = str(c.get("adId", "")).strip()
        if not ad_id: continue
        full_text = c.get("fullText", "")
        days_act, first_seen_dt = calculate_days_active(full_text, c.get("startTs"))
        clean_land = clean_redirect_url(c.get("landingUrl", ""))
        phone_num = extract_phone_number(full_text, clean_land)
        copies_cnt = max(1, int(c.get("copiesCount", 1) or 1))
        plat = c.get("platforms", "Meta (IG/FB)")
        media_u = c.get("mediaUrl", "")

        row = cur.execute("SELECT days_active, copies_count, landing_url, phone, media_url FROM ads WHERE id = ?", (ad_id,)).fetchone()
        if row:
            old_days, old_copies, old_land, old_phone, old_media = row
            cur.execute("""
                UPDATE ads SET
                    status = 'ACTIVE',
                    last_seen = ?,
                    days_active = ?,
                    copies_count = ?,
                    landing_url = ?,
                    phone = ?,
                    media_url = ?
                WHERE id = ?
            """, (
                today_str,
                max(old_days or 1, days_act),
                max(old_copies or 1, copies_cnt),
                clean_land[:500] if clean_land else (old_land or ""),
                phone_num if phone_num else (old_phone or ""),
                media_u if media_u else (old_media or ""),
                ad_id
            ))
        else:
            cur.execute("""
                INSERT INTO ads (
                    id, page_name, keyword, country, status,
                    platforms, format, cta_text, days_active, copies_count,
                    landing_url, phone, target_url, media_url, first_seen, last_seen, ad_text
                ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ad_id, c.get("pageName", "İşletme Sayfası")[:100], kw, cty,
                plat, c.get("format", "Görsel / Resim"),
                c.get("ctaText", "Detay Gör")[:40], days_act, copies_cnt,
                clean_land[:500], phone_num, f"https://www.facebook.com/ads/library/?id={ad_id}",
                media_u, first_seen_dt, today_str, full_text[:900]
            ))
    conn.commit()
    total_now = cur.execute("SELECT COUNT(*) FROM ads WHERE LOWER(keyword) = ? AND UPPER(country) = ?", (kw, cty)).fetchone()[0]
    conn.close()
    return {"status": "ok", "total_saved": total_now}

@app.post("/api/ext/finish")
async def ext_finish_scan(request: Request):
    if not is_authorized(request): raise HTTPException(401)
    data = await request.json()
    kw = data.get("keyword", "").strip().lower()
    cty = data.get("country", "TR").strip().upper()
    reached_end = data.get("reached_end", False)
    seen_ids = set(str(x) for x in data.get("seen_ids", []))

    conn = get_db()
    cur = conn.cursor()
    now_str = datetime.now(TZ_GMT3).strftime("%Y-%m-%d %H:%M")
    cur.execute("UPDATE targets SET status = 'TAMAMLANDI', last_scanned_at = ? WHERE LOWER(keyword) = ? AND UPPER(country) = ?", (now_str, kw, cty))

    if reached_end and len(seen_ids) > 10:
        rows = cur.execute("SELECT id FROM ads WHERE LOWER(keyword) = ? AND UPPER(country) = ? AND status = 'ACTIVE'", (kw, cty)).fetchall()
        unseen = [r[0] for r in rows if r[0] not in seen_ids]
        if unseen:
            cur.executemany("UPDATE ads SET status = 'PASİF' WHERE id = ?", [(uid,) for uid in unseen])
    conn.commit()
    conn.close()

    update_local_files_and_sheets(kw, cty)
    return {"status": "ok"}

@app.get("/api/settings")
def get_settings(request: Request):
    if not is_authorized(request): raise HTTPException(401)
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = 'sheets_webhook_url'").fetchone()
    conn.close()
    return {"sheets_webhook_url": row[0] if row else ""}

@app.post("/api/settings")
async def save_settings(request: Request):
    if not is_authorized(request): raise HTTPException(401)
    data = await request.json()
    url = data.get("sheets_webhook_url", "").strip()
    conn = get_db()
    cur = conn.cursor()
    exists = cur.execute("SELECT 1 FROM settings WHERE key = 'sheets_webhook_url'").fetchone()
    if exists:
        cur.execute("UPDATE settings SET value = ? WHERE key = 'sheets_webhook_url'", (url,))
    else:
        cur.execute("INSERT INTO settings (key, value) VALUES ('sheets_webhook_url', ?)", (url,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/login", response_class=HTMLResponse)
def login_page():
    login_file = os.path.join(STATIC_DIR, "login.html")
    if os.path.exists(login_file):
        with open(login_file, "r", encoding="utf-8") as f: return f.read()
    return "<h3>Login Page (Template not found in static folder)</h3>"

@app.post("/login")
async def login_action(request: Request):
    form = await request.form()
    if form.get("username") == PANEL_USER and form.get("password") == PANEL_PASS:
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("auth_session", "authenticated", httponly=True, max_age=86400 * 30)
        return resp
    return RedirectResponse(url="/login?error=1", status_code=303)

@app.get("/logout")
def logout_action():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("auth_session")
    return resp

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not is_authorized(request): return RedirectResponse(url="/login", status_code=303)
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f: return f.read()
    return "<h3>Dashboard (Template not found in static folder)</h3>"

@app.get("/api/targets")
def get_targets(request: Request):
    if not is_authorized(request): raise HTTPException(401)
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute("SELECT id, keyword, country, created_at, last_scanned_at, status FROM targets ORDER BY id DESC").fetchall()
    result = []
    for t_id, kw, cty, c_at, l_at, st in rows:
        total = cur.execute("SELECT COUNT(*) FROM ads WHERE LOWER(keyword) = LOWER(?) AND UPPER(country) = UPPER(?)", (kw, cty)).fetchone()[0]
        result.append({
            "id": t_id, "keyword": kw, "country": cty,
            "created_at": c_at, "last_scanned_at": l_at or "-",
            "status": st or "IDLE", "total_ads": total
        })
    conn.close()
    return result

@app.delete("/api/targets/{target_id}")
def delete_target(target_id: int, request: Request):
    if not is_authorized(request): raise HTTPException(401)
    conn = get_db()
    cur = conn.cursor()
    target = cur.execute("SELECT keyword, country FROM targets WHERE id = ?", (target_id,)).fetchone()
    if target:
        kw, cty = target
        cur.execute("DELETE FROM ads WHERE LOWER(keyword) = LOWER(?) AND UPPER(country) = UPPER(?)", (kw, cty))
        cur.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/export")
def export_file(keyword: str, country: str, fmt: str = "xlsx", request: Request = None):
    if not is_authorized(request): raise HTTPException(401)
    df = get_query_df(keyword, country)
    safe_name = f"reklamlar_{keyword.lower().replace(' ', '_')}_{country.upper()}"

    if fmt.lower() == "csv":
        df_clean = df.copy()
        if "Reklam Metni" in df_clean.columns:
            df_clean["Reklam Metni"] = df_clean["Reklam Metni"].astype(str).str.replace(r'[\r\n]+', ' | ', regex=True)
        stream = StringIO()
        df_clean.to_csv(stream, index=False, encoding="utf-8-sig")
        response = StreamingResponse(iter([stream.getvalue().encode("utf-8-sig")]), media_type="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename={safe_name}.csv"
        return response

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Reklam Analizi')
        ws = writer.sheets['Reklam Analizi']
        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 3, 12), 48)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={safe_name}.xlsx"}
    )