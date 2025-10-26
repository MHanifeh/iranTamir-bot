# =========================
# file: irantamir_bot.py
# =========================
"""
Run locally:
  python -m venv .venv && . .venv/bin/activate
  pip install -r requirements.txt
  export BOT_TOKEN='7564697686:AAG1xCd22_P0T_MLLjusQGtQr0kmgsVH_jE'
  export DATABASE_URL='postgresql://postgres:mmBjOLJYVoiygolcPpgbfDJUynAkUAUI@shuttle.proxy.rlwy.net:32895/railway'
  export ADMIN_TELEGRAM_ID='698037613'
  python irantamir_bot.py

On Railway:
  - Set Variables (recommended):
    BOT_TOKEN
    DATABASE_URL   (can reference your Postgres service variable)
    ADMIN_TELEGRAM_ID
    # optional for webhook:
    WEBHOOK_URL=https://<your-railway-app>.up.railway.app/webhook
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import JSON, Integer, String, BigInteger, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Optional Word import
try:
    from docx import Document as Docx
except Exception:
    Docx = None  # type: ignore

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
LOG = logging.getLogger("irantamir_bot")

# ---------- Config (env first, fallback to your provided values) ----------
RAW_BOT_TOKEN = os.environ.get("BOT_TOKEN", "7564697686:AAG1xCd22_P0T_MLLjusQGtQr0kmgsVH_jE").strip()
RAW_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:mmBjOLJYVoiygolcPpgbfDJUynAkUAUI@shuttle.proxy.rlwy.net:32895/railway",
).strip()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "698037613").strip())

if not RAW_BOT_TOKEN:
    raise SystemExit("Missing BOT_TOKEN")

def to_asyncpg_url(url: str) -> str:
    # Why: SQLAlchemy async Postgres requires the async driver.
    return url.replace("postgresql://", "postgresql+asyncpg://", 1) if url.startswith("postgresql://") else url

DB_URL = to_asyncpg_url(RAW_DB_URL)

# ---------- DB Model ----------
class Base(DeclarativeBase):
    pass

class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), unique=True, index=True)  # normalized key
    display_name: Mapped[str] = mapped_column(String(256), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    unit: Mapped[str] = mapped_column(String(32), default="عدد")
    meta: Mapped[dict] = mapped_column(JSON, default={})
    # Epoch seconds (SQLite vs Postgres)
    created_at: Mapped[int] = mapped_column(
        BigInteger,
        server_default=text("(strftime('%s','now'))" if DB_URL.startswith("sqlite") else "extract(epoch from now())"),
    )
    updated_at: Mapped[int] = mapped_column(
        BigInteger,
        server_default=text("(strftime('%s','now'))" if DB_URL.startswith("sqlite") else "extract(epoch from now())"),
    )

engine = create_async_engine(DB_URL, echo=False, pool_pre_ping=True, future=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ---------- Persian normalization & parsing ----------
_FA_TO_EN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_ARABIC_TO_PERSIAN = str.maketrans({"ي": "ی", "ك": "ک"})
_FA_NUM_WORDS = {
    "صفر": 0, "هیچ": 0,
    "یک": 1, "يه": 1, "یه": 1, "١": 1,
    "دو": 2, "٢": 2,
    "سه": 3, "٣": 3,
    "چهار": 4, "۴": 4, "٤": 4,
    "پنج": 5, "۵": 5, "٥": 5,
    "شش": 6, "۶": 6, "٦": 6,
    "هفت": 7, "۷": 7, "٧": 7,
    "هشت": 8, "۸": 8, "٨": 8,
    "نه": 9, "۹": 9, "٩": 9,
    "ده": 10, "۱۰": 10,
}

def normalize_text(text: str) -> str:
    t = text.strip()
    t = t.translate(_FA_TO_EN_DIGITS)
    t = t.translate(_ARABIC_TO_PERSIAN)
    t = re.sub(r"\s+", " ", t)
    return t

def normalize_key(name: str) -> str:
    t = normalize_text(name).replace("‌", " ")
    t = re.sub(r"[^\w\s\-\.\u0600-\u06FF]", "", t)
    return t.lower().strip()

def parse_number(token: str) -> Optional[int]:
    t = normalize_text(token)
    if t.isdigit():
        return int(t)
    return _FA_NUM_WORDS.get(t)

@dataclass
class ParsedIntent:
    kind: str  # add/remove/set/list/query/available/delete/unknown
    name: Optional[str] = None
    amount: Optional[int] = None

_ADD_PATTERNS = [
    r"(?P<n>\d+|\S+)\s*(?:عدد)?\s+(?P<name>.+?)\s*(?:خرید(?:اری)?\s*شد|اضافه\s*کن|افزوده\s*شد)$",
]
_REMOVE_PATTERNS = [
    r"(?P<n>\d+|\S+)\s*(?:عدد)?\s+(?P<name>.+?)\s*(?:فروخته\s*شد|کم\s*کن|کسر\s*شد|بردار)$",
]
_SET_PATTERNS = [
    r"(?P<n>\d+|\S+)\s*(?:عدد)?\s*(?:برای|به)?\s*(?P<name>.+?)\s*(?:ثبت|تنظیم|ویرایش)\s*کن$",
]
_COUNT_PATTERNS = [
    r"(?:چندتا)\s+(?P<name>.+?)\s*(?:داریم|موجود\s*است|هست)?\??$",
]
_AVAIL_PATTERNS = [
    r"(?:آیا|ایا)?\s*(?P<name>.+?)\s*(?:داریم|موجوده|موجود\s*است)\s*\??$",
]
_LIST_PATTERNS = [
    r"(?:لیست|فهرست)\s*(?:قطعات|آیتمها|آیتم‌ها)?$",
]
_DELETE_PATTERNS = [
    r"(?:حذف)\s+(?P<name>.+)$",
]

def parse_intent(text: str) -> ParsedIntent:
    tx = normalize_text(text)
    if tx.startswith(("/", "•")):
        return ParsedIntent("unknown")
    for pat in _LIST_PATTERNS:
        if re.fullmatch(pat, tx):
            return ParsedIntent("list")
    for pat in _ADD_PATTERNS:
        m = re.fullmatch(pat, tx)
        if m:
            n = parse_number(m.group("n"))
            return ParsedIntent("add", m.group("name"), n if n is not None else 1)
    for pat in _REMOVE_PATTERNS:
        m = re.fullmatch(pat, tx)
        if m:
            n = parse_number(m.group("n"))
            return ParsedIntent("remove", m.group("name"), n if n is not None else 1)
    for pat in _SET_PATTERNS:
        m = re.fullmatch(pat, tx)
        if m:
            n = parse_number(m.group("n")) or 0
            return ParsedIntent("set", m.group("name"), n)
    for pat in _COUNT_PATTERNS:
        m = re.fullmatch(pat, tx)
        if m:
            return ParsedIntent("query", m.group("name"))
    for pat in _AVAIL_PATTERNS:
        m = re.fullmatch(pat, tx)
        if m:
            return ParsedIntent("available", m.group("name"))
    for pat in _DELETE_PATTERNS:
        m = re.fullmatch(pat, tx)
        if m:
            return ParsedIntent("delete", m.group("name"))
    return ParsedIntent("unknown")

# ---------- DB Ops ----------
async def get_item(session: AsyncSession, name_key: str) -> Optional[Item]:
    res = await session.execute(select(Item).where(Item.name == name_key))
    return res.scalar_one_or_none()

async def upsert_add(session: AsyncSession, display_name: str, amount: int) -> Tuple[Item, int]:
    key = normalize_key(display_name)
    item = await get_item(session, key)
    if item is None:
        item = Item(name=key, display_name=display_name.strip(), quantity=0)
        session.add(item)
        await session.flush()
    item.quantity = max(0, (item.quantity or 0) + amount)
    item.display_name = display_name.strip()
    await session.flush()
    return item, item.quantity

async def subtract(session: AsyncSession, display_name: str, amount: int) -> Tuple[Optional[Item], int]:
    key = normalize_key(display_name)
    item = await get_item(session, key)
    if not item:
        return None, 0
    item.quantity = max(0, (item.quantity or 0) - amount)
    await session.flush()
    return item, item.quantity

async def set_quantity(session: AsyncSession, display_name: str, amount: int) -> Tuple[Item, int]:
    key = normalize_key(display_name)
    item = await get_item(session, key)
    if not item:
        item = Item(name=key, display_name=display_name.strip(), quantity=0)
        session.add(item)
        await session.flush()
    item.quantity = max(0, amount)
    item.display_name = display_name.strip()
    await session.flush()
    return item, item.quantity

async def delete_item(session: AsyncSession, display_name: str) -> bool:
    key = normalize_key(display_name)
    item = await get_item(session, key)
    if not item:
        return False
    await session.delete(item)
    await session.flush()
    return True

async def list_items(session: AsyncSession, q: Optional[str] = None) -> List[Item]:
    stmt = select(Item)
    if q:
        like = f"%{normalize_key(q)}%"
        stmt = stmt.where(Item.name.like(like))
    stmt = stmt.order_by(Item.display_name.asc())
    res = await session.execute(stmt)
    return list(res.scalars())

# ---------- Wake-word memory ----------
_WAKE: Dict[int, float] = {}  # chat_id -> expiry_ts
_WAKE_SECONDS = 90

def set_wake(chat_id: int, seconds: int = _WAKE_SECONDS) -> None:
    _WAKE[chat_id] = time.time() + seconds

def is_wake(chat_id: int) -> bool:
    t = _WAKE.get(chat_id, 0)
    return t > time.time()

# ---------- Helpers ----------
def fmt_qty(q: int, unit: str = "عدد") -> str:
    return f"{q} {unit}"

def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and int(user_id) == int(ADMIN_TELEGRAM_ID))

async def ensure_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔️ Only admin can perform this action.")
        return False
    return True

# ---------- Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "سلام 👋\n"
        "من «ربات انبار» هستم.\n"
        "مثال‌ها:\n"
        "• ربات 1 عدد پیکاپ 1102 خریداری شد\n"
        "• ربات 1 عدد پیکاپ 1102 فروخته شد\n"
        "• ربات چندتا پیکاپ 1102 داریم؟\n"
        "• ربات لیست قطعات\n\n"
        "کامندها: /add /remove /set /list /search /delete /import /export /help"
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Commands:\n"
        "/add <qty> <name> (admin)\n"
        "/remove <qty> <name> (admin)\n"
        "/set <qty> <name> (admin)\n"
        "/list [query]\n"
        "/search <text>\n"
        "/delete <name> (admin)\n"
        "/import (attach Excel/CSV/Word) (admin)\n"
        "/export (Excel) (admin)\n"
        "In groups: say «ربات» → bot: «بله؟» then send your sentence."
    )

# Admin-guarded commands
async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update): return
    if len(ctx.args) < 2:
        await update.effective_message.reply_text("Usage: /add <qty> <name>")
        return
    n = parse_number(ctx.args[0])
    if n is None:
        await update.effective_message.reply_text("Invalid quantity.")
        return
    name = " ".join(ctx.args[1:])
    async with Session() as s, s.begin():
        item, newq = await upsert_add(s, name, n)
    await update.effective_message.reply_text(
        f"{n} عدد به «{item.display_name}» اضافه شد. موجودی جدید: {fmt_qty(newq)}."
    )

async def remove_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update): return
    if len(ctx.args) < 2:
        await update.effective_message.reply_text("Usage: /remove <qty> <name>")
        return
    n = parse_number(ctx.args[0])
    if n is None:
        await update.effective_message.reply_text("Invalid quantity.")
        return
    name = " ".join(ctx.args[1:])
    async with Session() as s, s.begin():
        item, newq = await subtract(s, name, n)
    if not item:
        await update.effective_message.reply_text("Item not found.")
        return
    await update.effective_message.reply_text(
        f"{n} عدد از «{item.display_name}» کسر شد. موجودی جدید: {fmt_qty(newq)}."
    )

async def set_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update): return
    if len(ctx.args) < 2:
        await update.effective_message.reply_text("Usage: /set <qty> <name>")
        return
    n = parse_number(ctx.args[0])
    if n is None:
        await update.effective_message.reply_text("Invalid quantity.")
        return
    name = " ".join(ctx.args[1:])
    async with Session() as s, s.begin():
        item, newq = await set_quantity(s, name, n)
    await update.effective_message.reply_text(
        f"موجودی «{item.display_name}» روی {fmt_qty(newq)} تنظیم شد."
    )

async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update): return
    if not ctx.args:
        await update.effective_message.reply_text("Usage: /delete <name>")
        return
    name = " ".join(ctx.args)
    async with Session() as s, s.begin():
        ok = await delete_item(s, name)
    await update.effective_message.reply_text("حذف شد." if ok else "Item not found.")

async def import_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update): return
    msg = update.effective_message
    doc = msg.document
    if not doc:
        await msg.reply_text("Attach Excel/CSV/Word and run /import again.")
        return
    filename = (doc.file_name or "").lower()
    byts = await doc.get_file().download_as_bytearray()
    buf = io.BytesIO(byts)

    try:
        if filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(buf)
        elif filename.endswith(".csv"):
            df = pd.read_csv(buf)
        elif filename.endswith(".docx"):
            if Docx is None:
                await msg.reply_text("python-docx not installed.")
                return
            df = _parse_docx_to_df(buf)
        else:
            await msg.reply_text("Unsupported format. Use xlsx/xls/csv/docx.")
            return
    except Exception as e:
        await msg.reply_text(f"Read error: {e}")
        return

    name_col = next((c for c in df.columns if str(c).strip().lower() in {"name", "نام", "کالا", "قطعه"}), None)
    qty_col = next((c for c in df.columns if str(c).strip().lower() in {"quantity", "qty", "تعداد"}), None)
    unit_col = next((c for c in df.columns if str(c).strip().lower() in {"unit", "واحد"}), None)

    if not name_col or not qty_col:
        await msg.reply_text("Missing required columns: name/نام and quantity/تعداد.")
        return

    imported = 0
    async with Session() as s, s.begin():
        for _, row in df.iterrows():
            name = str(row[name_col]).strip()
            if not name or name.lower() in {"nan", "none"}:
                continue
            qval = row[qty_col]
            try:
                q = int(parse_number(str(qval)) or int(float(qval)))
            except Exception:
                q = 0
            unit = str(row[unit_col]).strip() if unit_col else "عدد"
            item, _ = await set_quantity(s, name, q)
            item.unit = unit
            imported += 1
    await msg.reply_text(f"Imported rows: {imported}")

def _parse_docx_to_df(buf: io.BytesIO) -> pd.DataFrame:
    d = Docx(buf)  # type: ignore
    rows: List[List[str]] = []
    for t in d.tables:
        for r in t.rows[1:] if len(t.rows) > 1 else t.rows:
            vals = [c.text.strip() for c in r.cells]
            rows.append(vals)
    for p in d.paragraphs:
        txt = normalize_text(p.text)
        m = re.match(r"(.+?)\s*[-,:–]\s*(\d+|\S+)$", txt)
        if m:
            rows.append([m.group(1), m.group(2)])
    data = []
    for r in rows:
        if not r:
            continue
        name = str(r[0]).strip()
        qty_token = str(r[1]).strip() if len(r) > 1 else "0"
        q = parse_number(qty_token) or 0
        data.append({"name": name, "quantity": q})
    return pd.DataFrame(data)

# Public commands
async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(ctx.args) if ctx.args else None
    async with Session() as s:
        items = await list_items(s, query)
    if not items:
        await update.effective_message.reply_text("No items found.")
        return
    lines = [f"• {it.display_name} — {fmt_qty(it.quantity)}" for it in items]
    await update.effective_message.reply_text("لیست قطعات:\n" + "\n".join(lines))

async def search_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.effective_message.reply_text("Usage: /search <text>")
        return
    q = " ".join(ctx.args)
    async with Session() as s:
        items = await list_items(s, q)
    if not items:
        await update.effective_message.reply_text("Nothing found.")
        return
    lines = [f"• {it.display_name} — {fmt_qty(it.quantity)}" for it in items]
    await update.effective_message.reply_text("\n".join(lines))

async def export_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update): return
    async with Session() as s:
        items = await list_items(s, None)
    if not items:
        await update.effective_message.reply_text("Inventory is empty.")
        return
    df = pd.DataFrame([{"نام": it.display_name, "تعداد": it.quantity, "واحد": it.unit} for it in items])
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Inventory")
    bio.seek(0)
    await update.effective_message.reply_document(
        document=bio, filename="inventory.xlsx", caption="Exported Excel"
    )

# ---------- Group wake word ----------
async def wake_word(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    set_wake(update.effective_chat.id)
    await update.effective_message.reply_text("بله؟")

# ---------- NL handler (with admin gate on mutating intents) ----------
async def nlu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or (msg.from_user and msg.from_user.is_bot):
        return
    text = msg.text or msg.caption or ""
    if not text.strip():
        return

    tx = normalize_text(text)
    is_group = update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    addressed = tx.startswith("ربات ")
    if is_group and not (addressed or is_wake(update.effective_chat.id) or tx.strip() == "ربات"):
        return

    if tx.strip() == "ربات":
        await wake_word(update, ctx)
        return

    if addressed:
        tx = tx[len("ربات ") :].strip()

    intent = parse_intent(tx)

    if intent.kind == "list":
        await list_cmd(update, ctx)
        return

    if intent.kind in {"add", "remove", "set", "delete"}:
        if not await ensure_admin(update):
            return

    if intent.kind == "add" and intent.name and intent.amount is not None:
        async with Session() as s, s.begin():
            item, newq = await upsert_add(s, intent.name, intent.amount)
        await msg.reply_text(
            f"{intent.amount} عدد به «{item.display_name}» اضافه شد. موجودی جدید: {fmt_qty(newq)}."
        )
        return

    if intent.kind == "remove" and intent.name and intent.amount is not None:
        async with Session() as s, s.begin():
            item, newq = await subtract(s, intent.name, intent.amount)
        if not item:
            await msg.reply_text("Item not found.")
        else:
            await msg.reply_text(
                f"{intent.amount} عدد از «{item.display_name}» کسر شد. موجودی جدید: {fmt_qty(newq)}."
            )
        return

    if intent.kind == "set" and intent.name and intent.amount is not None:
        async with Session() as s, s.begin():
            item, newq = await set_quantity(s, intent.name, intent.amount)
        await msg.reply_text(f"موجودی «{item.display_name}» روی {fmt_qty(newq)} تنظیم شد.")
        return

    if intent.kind == "query" and intent.name:
        async with Session() as s:
            it = await get_item(s, normalize_key(intent.name))
        if not it:
            await msg.reply_text("چنین آیتمی ثبت نشده است.")
        else:
            await msg.reply_text(f"{fmt_qty(it.quantity)} از «{it.display_name}» موجود است.")
        return

    if intent.kind == "available" and intent.name:
        async with Session() as s:
            it = await get_item(s, normalize_key(intent.name))
        if not it or it.quantity <= 0:
            await msg.reply_text("خیر، موجود نیست.")
        else:
            await msg.reply_text(f"بله، «{it.display_name}» موجود است ({fmt_qty(it.quantity)}).")
        return

    if intent.kind == "delete" and intent.name:
        async with Session() as s, s.begin():
            ok = await delete_item(s, intent.name)
        await msg.reply_text("حذف شد." if ok else "Item not found.")
        return

    await msg.reply_text(
        "نفهمیدم. نمونه‌ها: «1 عدد پیکاپ 1102 خریداری شد»، «چندتا پیکاپ 1102 داریم؟»، «لیست قطعات»."
    )

# ---------- App bootstrap ----------
async def main() -> None:
    try:
        import uvloop
        uvloop.install()
    except Exception:
        pass

    await init_db()

    app: Application = (
        ApplicationBuilder()
        .token(RAW_BOT_TOKEN)
        .rate_limiter(AIORateLimiter(max_retries=2))
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("import", import_cmd))
    app.add_handler(CommandHandler("export", export_cmd))

    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\s*ربات\s*$"), wake_word))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), nlu_handler))

    if WEBHOOK_URL:
        full_webhook = f"{WEBHOOK_URL.rstrip('/')}/{RAW_BOT_TOKEN}"
        LOG.info("Starting webhook: %s", full_webhook)
        await app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")),
            url_path=RAW_BOT_TOKEN,
            webhook_url=full_webhook,
        )
    else:
        LOG.info("Starting polling...")
        await app.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


# =========================
# file: requirements.txt
# =========================
python-telegram-bot>=21,<22
SQLAlchemy>=2.0,<3
asyncpg>=0.29
aiosqlite>=0.20
pandas>=2.0
openpyxl>=3.1
python-docx>=1.1
uvloop>=0.19; sys_platform != "win32"

# =========================
# file: Dockerfile
# =========================
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY irantamir_bot.py .
ENV PORT=8080
CMD ["python", "irantamir_bot.py"]

# =========================
# file: README.md
# =========================
# Inventory Bot (FA) – Telegram
## Local