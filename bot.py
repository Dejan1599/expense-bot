import os
import json
import base64
import sqlite3
import tempfile
import anthropic
from datetime import datetime, date, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DB_PATH = os.environ.get("DB_PATH", "data/expenses.db")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Baza ──────────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL,
                currency TEXT DEFAULT 'RSD',
                category TEXT,
                description TEXT,
                date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS budget (
                id INTEGER PRIMARY KEY,
                amount REAL NOT NULL,
                currency TEXT DEFAULT 'RSD',
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS extra_income (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL,
                currency TEXT DEFAULT 'RSD',
                description TEXT,
                month TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

def save_expense(amount, currency, category, description):
    today = date.today().isoformat()
    now = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO expenses (amount, currency, category, description, date, created_at) VALUES (?,?,?,?,?,?)",
            (amount, currency, category, description, today, now)
        )

def get_summary(period="month"):
    today = date.today()
    if period == "today":
        start = today.isoformat()
    elif period == "week":
        start = (today - timedelta(days=today.weekday())).isoformat()
    else:
        start = today.replace(day=1).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT category, SUM(amount), currency
            FROM expenses WHERE date >= ?
            GROUP BY category, currency ORDER BY SUM(amount) DESC
        """, (start,)).fetchall()
        total = conn.execute("""
            SELECT SUM(amount), currency FROM expenses
            WHERE date >= ? GROUP BY currency
        """, (start,)).fetchall()
    return rows, total

def get_monthly_details():
    today = date.today()
    start = today.replace(day=1).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("""
            SELECT amount, currency, category, description, date
            FROM expenses WHERE date >= ?
            ORDER BY category, date DESC
        """, (start,)).fetchall()

def get_last_expenses(n=5):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("""
            SELECT amount, currency, category, description, date
            FROM expenses ORDER BY created_at DESC LIMIT ?
        """, (n,)).fetchall()

def delete_last():
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT id FROM expenses ORDER BY created_at DESC LIMIT 1").fetchone()
        if row:
            conn.execute("DELETE FROM expenses WHERE id = ?", (row[0],))
            return True
    return False

def set_budget(amount, currency="RSD"):
    now = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM budget")
        conn.execute("INSERT INTO budget (id, amount, currency, updated_at) VALUES (1,?,?,?)", (amount, currency, now))

def get_budget():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT amount, currency FROM budget WHERE id=1").fetchone()

def get_monthly_total_rsd():
    today = date.today()
    start = today.replace(day=1).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT SUM(amount) FROM expenses
            WHERE date >= ? AND currency = 'RSD'
        """, (start,)).fetchone()
        return row[0] or 0.0

def add_extra_income(amount, description=""):
    month = date.today().strftime("%Y-%m")
    now = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO extra_income (amount, currency, description, month, created_at) VALUES (?,?,?,?,?)",
            (amount, "RSD", description, month, now)
        )

def get_extra_income_this_month():
    month = date.today().strftime("%Y-%m")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT SUM(amount) FROM extra_income WHERE month=?", (month,)
        ).fetchone()
        return row[0] or 0.0

def get_prev_month_range():
    today = date.today()
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev.isoformat(), first_this.isoformat(), last_prev

def get_prev_month_summary():
    start, end, _ = get_prev_month_range()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT category, SUM(amount), currency FROM expenses
            WHERE date >= ? AND date < ?
            GROUP BY category, currency ORDER BY SUM(amount) DESC
        """, (start, end)).fetchall()
        total = conn.execute("""
            SELECT SUM(amount), currency FROM expenses
            WHERE date >= ? AND date < ? GROUP BY currency
        """, (start, end)).fetchall()
    return rows, total

def get_prev_month_total_rsd():
    start, end, _ = get_prev_month_range()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT SUM(amount) FROM expenses
            WHERE date >= ? AND date < ? AND currency = 'RSD'
        """, (start, end)).fetchone()
        return row[0] or 0.0

# ── Claude: parsiranje teksta ─────────────────────────────────────────────────
def parse_expense_text(text):
    prompt = f"""Izvuci podatke o trošku iz ove poruke na srpskom ili engleskom.
Vrati SAMO JSON objekat, bez ikakvog teksta pre ili posle.

Format:
{{"amount": broj, "currency": "RSD", "category": "kategorija", "description": "opis", "is_expense": true/false}}

Kategorije (odaberi najbliže): Hrana, Piće, Prevoz, Gorivo, Stanovanje, Zabava, Zdravlje, Odeca, Racuni, Ostalo

Ako poruka nije trošak (pitanje, komanda itd), vrati {{"is_expense": false}}
Valuta: ako nije navedena pretpostavi RSD.

Poruka: "{text}"
"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()
    print(f"[DEBUG] Claude: {raw}", flush=True)
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

# ── Claude: parsiranje slike računa ──────────────────────────────────────────
def parse_expense_image(image_bytes, media_type="image/jpeg"):
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode("utf-8")}},
            {"type": "text", "text": """Sa ovog računa izvuci ukupan iznos za plaćanje.
Vrati SAMO JSON: {"amount": broj, "currency": "RSD", "category": "kategorija", "description": "kratak opis", "is_expense": true}
Kategorije: Hrana, Piće, Prevoz, Gorivo, Stanovanje, Zabava, Zdravlje, Odeca, Racuni, Ostalo
Ako ne možeš da prepoznaš račun, vrati {"is_expense": false}"""}
        ]}]
    )
    raw = response.content[0].text.strip()
    print(f"[DEBUG] Claude slika: {raw}", flush=True)
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

# ── Groq Whisper ──────────────────────────────────────────────────────────────
def transcribe_voice(ogg_bytes):
    import requests
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(ogg_bytes)
        tmp_path = f.name
    try:
        with open(tmp_path, "rb") as audio_file:
            response = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("voice.ogg", audio_file, "audio/ogg")},
                data={"model": "whisper-large-v3-turbo", "language": "sr"}
            )
        print(f"[GROQ STATUS] {response.status_code}", flush=True)
        response.raise_for_status()
        return response.json().get("text", "")
    except Exception as e:
        print(f"[TRANSCRIBE ERROR] {type(e).__name__}: {e}", flush=True)
        raise
    finally:
        os.unlink(tmp_path)

# ── Budžet upozorenje ─────────────────────────────────────────────────────────
async def check_budget_warning(update: Update):
    budget = get_budget()
    if not budget:
        return
    budget_amt, budget_cur = budget
    extra = get_extra_income_this_month()
    total_budget = budget_amt + extra
    spent = get_monthly_total_rsd()
    pct = (spent / total_budget) * 100 if total_budget > 0 else 0
    if pct >= 100:
        await update.message.reply_text(
            f"🚨 *Premašio si mesečni budžet!*\n"
            f"Potrošeno: *{spent:,.0f} {budget_cur}* od *{total_budget:,.0f} {budget_cur}*",
            parse_mode="Markdown"
        )
    elif pct >= 80:
        ostalo = total_budget - spent
        await update.message.reply_text(
            f"⚠️ *Pažnja!* Potrošio si {pct:.0f}% budžeta.\n"
            f"Ostalo: *{ostalo:,.0f} {budget_cur}*",
            parse_mode="Markdown"
        )

# ── Odgovor nakon parsiranja ──────────────────────────────────────────────────
async def respond_with_expense(update: Update, data: dict, source: str = "text"):
    if not data.get("is_expense"):
        await update.message.reply_text(
            "🤔 Nisam uspeo da prepoznam trošak.\n"
            "Pošalji trošak tekstom, sliku računa, ili glasovnu poruku.\n"
            "Ukucaj *pomoc* za listu svih komandi.",
            parse_mode="Markdown"
        )
        return
    save_expense(data["amount"], data["currency"], data["category"], data["description"])
    icons = {"photo": "📷", "voice": "🎤", "text": "✍️"}
    await update.message.reply_text(
        f"{icons.get(source, '✅')} Zapisano!\n"
        f"📂 {data['category']}\n"
        f"💸 *{data['amount']:,.0f} {data['currency']}*\n"
        f"📝 {data['description']}",
        parse_mode="Markdown"
    )
    await check_budget_warning(update)

# ── Handleri ──────────────────────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    return update.effective_user.id == ALLOWED_USER_ID

async def cmd_pomoc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "📖 *Sve komande:*\n\n"
        "*Unos troškova:*\n"
        "• Piši slobodnim tekstom: _Ručak 850_, _Gorivo 4500_\n"
        "• 📷 Pošalji sliku računa\n"
        "• 🎤 Pošalji glasovnu poruku\n\n"
        "*Pregled troškova:*\n"
        "/danas — troškovi danas\n"
        "/nedelja — troškovi ove nedelje\n"
        "/mesec — troškovi ovog meseca po kategorijama\n"
        "/detalji — svi unosi ovog meseca sa opisima\n"
        "/zadnji — poslednjih 5 unosa\n"
        "/proslimesec — troškovi prošlog meseca\n\n"
        "*Budžet:*\n"
        "/budzet 150000 — postavi mesečni budžet\n"
        "/zarada 50000 — dodaj dodatnu zaradu na budžet\n"
        "/stanje — potrošeno vs budžet + prognoza\n"
        "/stanjeprosli — stanje budžeta za prošli mesec\n\n"
        "*Ostalo:*\n"
        "/brisanje — obriši poslednji unos\n"
        "/pomoc — ova poruka",
        parse_mode="Markdown"
    )

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 Zdravo! Ja sam tvoj bot za praćenje troškova.\n\n"
        "Možeš mi slati troškove na 3 načina:\n"
        "• ✍️ *Tekst:* Ručak 850\n"
        "• 📷 *Slika:* fotografiši račun\n"
        "• 🎤 *Glas:* snimi glasovnu poruku\n\n"
        "Ukucaj *pomoc* za sve komande.",
        parse_mode="Markdown"
    )

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE, period="month"):
    if not is_allowed(update):
        return
    rows, totals = get_summary(period)
    labels = {"today": "danas", "week": "ovu nedelju", "month": "ovaj mesec"}
    if not rows:
        await update.message.reply_text(f"Nema troškova za {labels[period]}.")
        return
    lines = [f"📊 *Troškovi za {labels[period]}:*\n"]
    for cat, amt, cur in rows:
        lines.append(f"  {cat}: *{amt:,.0f} {cur}*")
    lines.append("")
    for amt, cur in totals:
        lines.append(f"💰 Ukupno: *{amt:,.0f} {cur}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_danas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_summary(update, ctx, "today")

async def cmd_nedelja(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_summary(update, ctx, "week")

async def cmd_mesec(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_summary(update, ctx, "month")

async def cmd_detalji(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    rows = get_monthly_details()
    if not rows:
        await update.message.reply_text("Nema troškova ovog meseca.")
        return
    lines = ["📋 *Detalji za ovaj mesec:*\n"]
    current_cat = None
    for amt, cur, cat, desc, d in rows:
        if cat != current_cat:
            lines.append(f"\n*{cat}*")
            current_cat = cat
        desc_text = f" — {desc}" if desc else ""
        lines.append(f"  {d} | *{amt:,.0f} {cur}*{desc_text}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_zadnji(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    rows = get_last_expenses(5)
    if not rows:
        await update.message.reply_text("Nema unetih troškova.")
        return
    lines = ["🧾 *Poslednjih 5 troškova:*\n"]
    for amt, cur, cat, desc, d in rows:
        lines.append(f"  {d} | {cat} | *{amt:,.0f} {cur}* — {desc}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_brisanje(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if delete_last():
        await update.message.reply_text("✅ Poslednji trošak je obrisan.")
    else:
        await update.message.reply_text("Nema troškova za brisanje.")

async def cmd_budzet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    args = ctx.args
    if not args or not args[0].replace(".", "").replace(",", "").isdigit():
        await update.message.reply_text("Koristi: /budzet 150000")
        return
    amount = float(args[0].replace(",", "."))
    set_budget(amount)
    await update.message.reply_text(
        f"✅ Mesečni budžet postavljen: *{amount:,.0f} RSD*",
        parse_mode="Markdown"
    )

async def cmd_zarada(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    args = ctx.args
    if not args or not args[0].replace(".", "").replace(",", "").isdigit():
        await update.message.reply_text(
            "Koristi: /zarada 50000\n"
            "Sa opisom: /zarada 50000 honorar"
        )
        return
    amount = float(args[0].replace(",", "."))
    description = " ".join(args[1:]) if len(args) > 1 else "dodatna zarada"
    add_extra_income(amount, description)
    extra_total = get_extra_income_this_month()
    budget = get_budget()
    budget_amt = budget[0] if budget else 0
    total = budget_amt + extra_total
    await update.message.reply_text(
        f"➕ Zarada dodana: *{amount:,.0f} RSD*\n"
        f"📝 {description}\n\n"
        f"🎯 Osnovni budžet: *{budget_amt:,.0f} RSD*\n"
        f"➕ Ukupno dodatno: *{extra_total:,.0f} RSD*\n"
        f"💰 Ukupno raspoloživo: *{total:,.0f} RSD*",
        parse_mode="Markdown"
    )

async def cmd_stanje(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    budget = get_budget()
    spent = get_monthly_total_rsd()
    extra = get_extra_income_this_month()
    today = date.today()
    next_month = today.replace(day=28) + timedelta(days=4)
    days_in_month = (next_month.replace(day=1) - timedelta(days=1)).day
    days_passed = today.day
    days_left = days_in_month - days_passed

    if not budget:
        await update.message.reply_text(
            f"📈 *Stanje ovog meseca:*\n\n"
            f"💸 Potrošeno: *{spent:,.0f} RSD*\n"
            f"📅 Dana prošlo: {days_passed}/{days_in_month}\n\n"
            f"_Nisi postavio budžet. Koristi /budzet 150000_",
            parse_mode="Markdown"
        )
        return

    budget_amt, budget_cur = budget
    total_budget = budget_amt + extra
    ostalo = total_budget - spent
    pct = (spent / total_budget) * 100 if total_budget > 0 else 0
    dnevni_prosek = spent / days_passed if days_passed > 0 else 0
    prognoza = dnevni_prosek * days_in_month

    if pct >= 100:
        status = "🚨 Budžet premašen!"
    elif pct >= 80:
        status = "⚠️ Pažnja, blizu limita"
    elif pct >= 50:
        status = "🟡 Na pola puta"
    else:
        status = "✅ U redu"

    bar_filled = int(pct / 10)
    bar = "█" * min(bar_filled, 10) + "░" * max(0, 10 - bar_filled)
    extra_line = f"➕ Dodatna zarada: *{extra:,.0f} {budget_cur}*\n" if extra > 0 else ""

    await update.message.reply_text(
        f"📊 *Stanje za {today.strftime('%B %Y')}:*\n\n"
        f"{status}\n"
        f"`{bar}` {pct:.0f}%\n\n"
        f"💸 Potrošeno: *{spent:,.0f} {budget_cur}*\n"
        f"🎯 Osnovni budžet: *{budget_amt:,.0f} {budget_cur}*\n"
        f"{extra_line}"
        f"💰 Ukupno raspoloživo: *{total_budget:,.0f} {budget_cur}*\n"
        f"🟢 Ostalo: *{ostalo:,.0f} {budget_cur}*\n\n"
        f"📅 Dana prošlo: {days_passed}/{days_in_month} (ostalo {days_left})\n"
        f"📈 Dnevni prosek: *{dnevni_prosek:,.0f} {budget_cur}*\n"
        f"🔮 Prognoza za mesec: *{prognoza:,.0f} {budget_cur}*",
        parse_mode="Markdown"
    )

async def cmd_proslimesec(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    rows, totals = get_prev_month_summary()
    _, _, last_day = get_prev_month_range()
    mesec_naziv = last_day.strftime("%B %Y")
    if not rows:
        await update.message.reply_text(f"Nema troškova za {mesec_naziv}.")
        return
    lines = [f"📊 *Troškovi za {mesec_naziv}:*\n"]
    for cat, amt, cur in rows:
        lines.append(f"  {cat}: *{amt:,.0f} {cur}*")
    lines.append("")
    for amt, cur in totals:
        lines.append(f"💰 Ukupno: *{amt:,.0f} {cur}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_stanje_prosli(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    budget = get_budget()
    spent = get_prev_month_total_rsd()
    _, _, last_day = get_prev_month_range()
    mesec_naziv = last_day.strftime("%B %Y")
    if not budget:
        await update.message.reply_text(
            f"📈 *Stanje za {mesec_naziv}:*\n\n"
            f"💸 Potrošeno: *{spent:,.0f} RSD*\n\n"
            f"_Nisi postavio budžet. Koristi /budzet 150000_",
            parse_mode="Markdown"
        )
        return
    budget_amt, budget_cur = budget
    ostalo = budget_amt - spent
    pct = (spent / budget_amt) * 100 if budget_amt > 0 else 0
    if pct >= 100:
        status = f"🚨 Budžet premašen za *{abs(ostalo):,.0f} {budget_cur}*"
    else:
        status = f"✅ Ostalo neutrošeno: *{ostalo:,.0f} {budget_cur}*"
    bar_filled = int(pct / 10)
    bar = "█" * min(bar_filled, 10) + "░" * max(0, 10 - bar_filled)
    await update.message.reply_text(
        f"📊 *Stanje za {mesec_naziv}:*\n\n"
        f"`{bar}` {pct:.0f}%\n\n"
        f"💸 Potrošeno: *{spent:,.0f} {budget_cur}*\n"
        f"🎯 Budžet bio: *{budget_amt:,.0f} {budget_cur}*\n\n"
        f"{status}",
        parse_mode="Markdown"
    )

# ── Handler: tekst ────────────────────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = update.message.text.strip()
    if text.lower() in ["pomoc", "pomoć", "help", "?"]:
        await cmd_pomoc(update, ctx)
        return
    await update.message.reply_chat_action("typing")
    try:
        data = parse_expense_text(text)
    except Exception:
        await update.message.reply_text("❌ Greška pri parsiranju. Pokušaj ponovo.")
        return
    await respond_with_expense(update, data, source="text")

# ── Handler: slika ────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_chat_action("upload_photo")
    try:
        photo_file = await update.message.photo[-1].get_file()
        image_bytes = await photo_file.download_as_bytearray()
        data = parse_expense_image(bytes(image_bytes))
    except Exception:
        await update.message.reply_text("❌ Greška pri čitanju slike. Pokušaj ponovo.")
        return
    await respond_with_expense(update, data, source="photo")

# ── Handler: glas ─────────────────────────────────────────────────────────────
async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_chat_action("typing")
    try:
        voice_file = await update.message.voice.get_file()
        ogg_bytes = await voice_file.download_as_bytearray()
        transcribed = transcribe_voice(bytes(ogg_bytes))
        if not transcribed:
            await update.message.reply_text("❌ Nisam razumeo glasovnu poruku. Pokušaj ponovo.")
            return
        data = parse_expense_text(transcribed)
        if not data.get("description"):
            data["description"] = transcribed
    except Exception as e:
        print(f"[VOICE ERROR] {type(e).__name__}: {e}", flush=True)
        await update.message.reply_text("❌ Greška pri obradi glasovne poruke. Pokušaj ponovo.")
        return
    await respond_with_expense(update, data, source="voice")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pomoc", cmd_pomoc))
    app.add_handler(CommandHandler("danas", cmd_danas))
    app.add_handler(CommandHandler("nedelja", cmd_nedelja))
    app.add_handler(CommandHandler("mesec", cmd_mesec))
    app.add_handler(CommandHandler("detalji", cmd_detalji))
    app.add_handler(CommandHandler("zadnji", cmd_zadnji))
    app.add_handler(CommandHandler("brisanje", cmd_brisanje))
    app.add_handler(CommandHandler("budzet", cmd_budzet))
    app.add_handler(CommandHandler("zarada", cmd_zarada))
    app.add_handler(CommandHandler("stanje", cmd_stanje))
    app.add_handler(CommandHandler("proslimesec", cmd_proslimesec))
    app.add_handler(CommandHandler("stanjeprosli", cmd_stanje_prosli))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    print("✅ Bot pokrenut! v5 — zarada komanda dodata.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
