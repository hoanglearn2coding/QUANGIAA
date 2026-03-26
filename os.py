import os
import json
import logging
import httpx
import pytz
import asyncio
import google.generativeai as genai
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, 
    CallbackQueryHandler, Defaults, MessageHandler, filters
)

# ===== 1. CẤU HÌNH HỆ THỐNG & API =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY") 
GENAI_API_KEY = os.getenv("GENAI_API_KEY") 

VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

DATA_DIR = "/data"
if not os.path.exists(DATA_DIR):
    try: os.makedirs(DATA_DIR, exist_ok=True)
    except Exception: DATA_DIR = "." 
DATA_FILE = os.path.join(DATA_DIR, "supreme_v6_data.json")

# --- Cấu hình Gemini AI (TƯ DUY CẤP CAO) ---
genai.configure(api_key=GENAI_API_KEY)
system_prompt = (
    "Bạn là một Siêu Quản Gia AI và Cố Vấn Chiến Lược cấp cao của Ông chủ. "
    "Luôn xưng 'Dạ', 'Ông chủ', 'tôi' hoặc 'em'.\n\n"
    "NGUYÊN TẮC TƯ DUY LÕI:\n"
    "1. SUY LUẬN SÂU: Không chỉ báo cáo dữ liệu thô. Hãy tìm ra ý nghĩa đằng sau những con số đó.\n"
    "2. PHÂN TÍCH NHANH: Phân tích logic và cấu trúc vấn đề thật ngắn gọn trong đầu trước khi đưa ra kết luận.\n"
    "3. NHẬN XÉT THÔNG MINH: Đưa ra insight (góc nhìn sâu sắc), lời khuyên hoặc cảnh báo tinh tế cho Ông chủ (đặc biệt khi soi kèo bóng đá ⚽ và bóng rổ 🏀).\n"
    "4. SO SÁNH: Tự động so sánh dữ liệu để chỉ ra điểm nổi bật nhất.\n"
    "5. CÁ NHÂN HÓA: Dựa vào [Hồ sơ Ông chủ] để trả lời hợp gu nhất.\n"
)
ai_model = genai.GenerativeModel(
    'gemini-2.5-flash',
    system_instruction=system_prompt,
    generation_config=genai.types.GenerationConfig(temperature=0.5) 
)

# ===== 2. QUẢN LÝ DỮ LIỆU =====
state = {"tasks":[], "boards": {}, "profile":[], "chat_id": None}
chat_sessions = {} 
last_api_check = 0 
client = httpx.AsyncClient(headers={"x-apisports-key": API_KEY}, timeout=20)

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))
            if "profile" not in state: state["profile"] =[]

def parse_match_time(utc_date_str):
    dt = datetime.strptime(utc_date_str.split('+')[0], "%Y-%m-%dT%H:%M:%S")
    dt = dt.replace(tzinfo=pytz.UTC).astimezone(VN_TZ)
    return dt.strftime("%H:%M"), dt.timestamp()

async def get_match_context(m):
    sport = m.get("sport", "f")
    home_id, away_id = m.get("home_id"), m.get("away_id")
    league = m.get("league", "Không rõ giải")
    if not home_id: return league, "Thiếu dữ liệu", "Thiếu dữ liệu"
    
    try:
        if sport == "f":
            res_h = await client.get(f"https://v3.football.api-sports.io/fixtures?team={home_id}&last=2")
            res_a = await client.get(f"https://v3.football.api-sports.io/fixtures?team={away_id}&last=2")
            def fmt_f(data):
                lines =[f"   + {f['fixture']['date'][:10]}: {f['teams']['home']['name']} {f['goals']['home'] if f['goals']['home'] is not None else '?'}-{f['goals']['away'] if f['goals']['away'] is not None else '?'} {f['teams']['away']['name']}" for f in data.json().get("response",[])]
                return "\n".join(lines) if lines else "   + Không có"
            return league, fmt_f(res_h), fmt_f(res_a)
        else:
            # Bóng rổ API khá nhạy cảm với season, nên sẽ trả về dữ liệu trống để AI tự phân tích bằng tên đội
            return league, "Dữ liệu lịch sử Bóng Rổ tạm ẩn.", "Dữ liệu lịch sử Bóng Rổ tạm ẩn."
    except Exception: return league, "Lỗi API", "Lỗi API"

# ===== 3. MENU START =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["chat_id"] = update.effective_chat.id
    save_data()
    menu = (
        "🤵 **AI COMMANDER V6 - SIÊU THỂ THAO**\n\n"
        "🧠 **[ BỘ NÃO AI ]**\n"
        " ├ 💬 *Chỉ cần chat bình thường với bot*\n"
        " ├ 📥 `/learn [Nội dung]` : Dạy AI nhớ sở thích\n"
        " ├ 📋 `/profile` : Xem hồ sơ cá nhân\n"
        " └ 📊 `/summary` : Báo cáo tổng kết ngày\n\n"
        "📅 **[ LỊCH TRÌNH ]**\n"
        " ├ ➕ `/add[Giờ] [Việc]` | 📜 `/list`\n"
        " └ 📝 `/tnote [STT] [Ghi chú]`\n\n"
        "⚽🏀 **[ THỂ THAO TỔNG HỢP ]**\n"
        " ├ 📅 `/matches` : Danh sách hôm nay\n"
        " ├ 🔍 `/search[Tên]` : Tìm đội (Cả 2 môn)\n"
        " ├ ⏰ `/time[Giờ]` : Lọc trận theo giờ\n"
        " ├ 📊 `/board` | 📜 `/history`\n"
        " ├ ℹ️ `/detail[STT]` : Xem chi tiết\n"
        " └ 🔮 `/predict [STT]` : Chuyên gia soi kèo"
    )
    await update.message.reply_text(menu, parse_mode="Markdown")

# ===== 4. AI & CHAT TỰ NHIÊN =====
async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = " ".join(context.args)
    if not info: return await update.message.reply_text("❌ HD: `/learn Tôi thích bắt bóng rổ kèo Tài`")
    state["profile"].append(info)
    save_data()
    await update.message.reply_text(f"✅ Dạ, em đã ghi nhớ sâu vào hệ thống: *{info}*", parse_mode="Markdown")

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state.get("profile"): return await update.message.reply_text("📭 Hồ sơ trống.")
    res = "🧠 **HỒ SƠ CÁ NHÂN ĐÃ LƯU:**\n"
    for i, p in enumerate(state["profile"]): res += f"{i+1}. {p}\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def natural_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    query = update.message.text
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
        chat = chat_sessions[chat_id]
        now = datetime.now(VN_TZ)
        today_str = now.strftime("%Y-%m-%d")
        
        profile_info = "\n".join(state.get("profile",[])) or "Chưa có thông tin."
        tasks_info = "\n".join([f"- {t['time']}: {t['content']}" for t in state["tasks"] if not t.get("reminded") or t["date"] == today_str]) or "- Rảnh"
        board_info = "\n".join([f"- {m.get('icon','⚽')} {m['home']} vs {m['away']} (Lúc {m.get('time','N/A')})" for m in state["boards"].get(today_str,[]) if not m.get("notified")]) or "- Không có"

        now_str = now.strftime("%A, %d/%m/%Y %H:%M:%S")
