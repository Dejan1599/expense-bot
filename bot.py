import os
import json
import base64
import sqlite3
import tempfile
import anthropic
from datetime import datetime, date
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]  # besplatan Whisper za glasovne poruke
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
        start = (today.replace(day=today.day - today.weekday())).isoformat()
    else:
        start = today.replace(day=1).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT category, SUM(amount), currency
            FROM expenses
            WHERE date >= ?
            GROUP BY category, currency
            ORDER BY SUM(amount) DESC
        """, (start,)).fetchall()

        total = conn.execute("""
            SELECT SUM(amount), currency
            FROM expenses
            WHERE date >= ?
            GROUP BY currency
        """, (start,)).fetchall()

    return rows, total

def get_last_expenses(n=5):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("""
            SELECT amount, currency, category, description, date
            FROM expenses
            ORDER BY created_at DESC
            LIMIT ?
        """, (n,)).fetchall()

def delete_last():
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT id FROM expenses ORDER BY created_at DESC LIMIT 1").fetchone()
        if row:
            conn.execute("DELETE FROM expenses WHERE id = ?", (row[0],))
            return True
    return False

# ── Claude: parsiranje teksta ─────────────────────────────────────────────────
def parse_expense_text(text):
    prompt = f"""Izvuci podatke o trošku iz ove poruke na srpskom ili engleskom.
Vrati SAMO JSON objekat, bez ikakvog teksta pre ili posle.

Format:
{{"amount": broj, "currency": "RSD", "category": "kategorija", "description": "opis", "is_expense": true/false}}

Kategorije (odaberi najbliže): Hrana, Piće, Prevoz, Gorivo, Stanovanje, Zabava, Zdravlje, Odeca, Racuni, Ostalo

Ako poruka nije trošak (pitanje, komanda itd), vrati {{"is_expense": false}}

Valuta: ako nije navedena pretpostavi RSD. Ako piše EUR, USD, itd — koristi to.

Poruka: "{text}"
"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()
    print(f"[DEBUG] Claude: {raw}", flush=True)
    # ocisti markdown code blokove ako postoje
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
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.standard_b64encode(image_bytes).decode("utf-8")
                    }
                },
                {
                    "type": "text",
                    "text": """Sa ovog računa izvuci ukupan iznos za plaćanje.
Vrati SAMO JSON, bez ikakvog teksta pre ili posle.

Format:
{"amount": broj, "currency": "RSD", "category": "kategorija", "description": "kratak opis", "is_expense": true}

Kategorije: Hrana, Piće, Prevoz, Gorivo, Stanovanje, Zabava, Zdravlje, Odeca, Racuni, Ostalo

Ako ne možeš da prepoznaš račun, vrati {"is_expense": false}"""
                }
            ]
        }]
    )
    raw = response.content[0].text.strip()
    print(f"[DEBUG] Claude: {raw}", flush=True)
    # ocisti markdown code blokove ako postoje
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

# ── Groq Whisper: transkripcija glasovne poruke ───────────────────────────────
def transcribe_voice(ogg_bytes):
    import urllib.request

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(ogg_bytes)
        tmp_path = f.name

    try:
        boundary = "----ExpenseBotBoundary"
        with open(tmp_path, "rb") as audio_file:
            audio_data = audio_file.read()

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="voice.ogg"\r\n'
            f"Content-Type: audio/ogg\r\n\r\n"
        ).encode() + audio_data + (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"whisper-large-v3-turbo\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="language"\r\n\r\n'
            f"sr\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": f"multipart/form-data; boundary={boundary}"
            },
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            return result.get("text", "")
    finally:
        os.unlink(tmp_path)

# ── Zajednički odgovor nakon parsiranja ──────────────────────────────────────
async def respond_with_expense(update: Update, data: dict, source: str = "text"):
    if not data.get("is_expense"):
        await update.message.reply_text(
            f"🤔 Nisam uspeo da prepoznam trošak.\n"
            "Pošalji trošak tekstom (npr. *Ručak 850*), fotografiju računa, ili glasovnu poruku.",
            parse_mode="Markdown"
        )
        return

    save_expense(data["amount"], data["currency"], data["category"], data["description"])
    icons = {"photo": "📷", "voice": "🎤", "text": "✍️"}
    icon = icons.get(source, "✅")
    await update.message.reply_text(
        f"{icon} Zapisano!\n"
        f"📂 {data['category']}\n"
        f"💸 *{data['amount']:,.0f} {data['currency']}*\n"
        f"📝 {data['description']}",
        parse_mode="Markdown"
    )

# ── Handleri ──────────────────────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    return update.effective_user.id == ALLOWED_USER_ID

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 Zdravo! Ja sam tvoj bot za praćenje troškova.\n\n"
        "Možeš mi slati troškove na 3 načina:\n"
        "• ✍️ *Tekst:* Ručak 850\n"
        "• 📷 *Slika:* fotografiši račun\n"
        "• 🎤 *Glas:* snimi glasovnu poruku\n\n"
        "Komande:\n"
        "/danas — troškovi danas\n"
        "/nedelja — ova nedelja\n"
        "/mesec — ovaj mesec\n"
        "/zadnji — poslednjih 5 troškova\n"
        "/brisanje — briši poslednji unos",
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

# ── Handler: tekst ────────────────────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_chat_action("typing")
    try:
        data = parse_expense_text(update.message.text.strip())
    except Exception:
        await update.message.reply_text("❌ Greška pri parsiranju. Pokušaj ponovo.")
        return
    await respond_with_expense(update, data, source="text")

# ── Handler: slika računa ─────────────────────────────────────────────────────
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

# ── Handler: glasovna poruka ──────────────────────────────────────────────────
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
    except Exception:
        await update.message.reply_text("❌ Greška pri obradi glasovne poruke. Pokušaj ponovo.")
        return
    await respond_with_expense(update, data, source="voice")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("danas", cmd_danas))
    app.add_handler(CommandHandler("nedelja", cmd_nedelja))
    app.add_handler(CommandHandler("mesec", cmd_mesec))
    app.add_handler(CommandHandler("zadnji", cmd_zadnji))
    app.add_handler(CommandHandler("brisanje", cmd_brisanje))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    print("✅ Bot pokrenut! Podržava: tekst, slike, glasovne poruke.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
