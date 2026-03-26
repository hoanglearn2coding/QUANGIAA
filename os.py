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
    CallbackQueryHandler, Defaults
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
DATA_FILE = os.path.join(DATA_DIR, "supreme_v4_data.json")

# --- Cấu hình Gemini AI (TƯ DUY PHẢN BIỆN CAO CẤP) ---
genai.configure(api_key=GENAI_API_KEY)
system_prompt = (
    "Bạn là một Siêu Quản Gia AI và Cố Vấn Chiến Lược cá nhân của Ông chủ. "
    "Luôn xưng 'Dạ', 'Ông chủ', 'tôi' hoặc 'em'. Trả lời súc tích, đanh thép, chuyên nghiệp.\n\n"
    "TƯ DUY PHẢN BIỆN & LẬP LUẬN:\n"
    "- Tuyệt đối không hùa theo Ông chủ mù quáng. Nếu thấy rủi ro (đặc biệt trong cá cược bóng đá, tài chính, lịch trình quá sức), bạn PHẢI cảnh báo và phản biện lại bằng logic, dữ liệu sắc bén.\n"
    "- Phân tích mọi vấn đề theo góc nhìn Đa Chiều (Cơ hội / Rủi ro / Biến số bất ngờ).\n\n"
    "CÁ NHÂN HÓA (HỌC HỎI):\n"
    "- Bạn có quyền truy cập [Hồ sơ cá nhân] của Ông chủ. Hãy sử dụng những thông tin này để tư vấn đúng gu, đúng sở thích của Ông chủ nhất.\n\n"
    "QUYỀN TRUY CẬP:\n"
    "- Bạn có thể đọc [Dữ liệu hệ thống] (Công việc, Bóng đá, Hồ sơ) để đưa ra báo cáo theo thời gian thực."
)
ai_model = genai.GenerativeModel(
    'gemini-2.5-flash',
    system_instruction=system_prompt,
    generation_config=genai.types.GenerationConfig(temperature=0.4) # Nhiệt độ 0.4 giúp AI tư duy logic, bớt "ngáo" và nói nhảm
)

# ===== 2. QUẢN LÝ DỮ LIỆU =====
# Đã thêm "profile" để AI ghi nhớ sở thích cá nhân
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
    home_id = m.get("home_id")
    away_id = m.get("away_id")
    league = m.get("league", "Không rõ giải đấu")
    
    if not home_id: return league, "Thiếu dữ liệu ID", "Thiếu dữ liệu ID"
    try:
        res_home = await client.get(f"https://v3.football.api-sports.io/fixtures?team={home_id}&last=2")
        res_away = await client.get(f"https://v3.football.api-sports.io/fixtures?team={away_id}&last=2")
        
        def format_last_matches(data):
            lines =[]
            for f in data.json().get("response",[]):
                date = f["fixture"]["date"][:10]
                home = f["teams"]["home"]["name"]
                away = f["teams"]["away"]["name"]
                goals_home = f['goals']['home']
                goals_away = f['goals']['away']
                score = f"{goals_home}-{goals_away}" if goals_home is not None else "?-?"
                lines.append(f"   + {date}: {home} {score} {away}")
            return "\n".join(lines) if lines else "   + Không có dữ liệu"

        return league, format_last_matches(res_home), format_last_matches(res_away)
    except Exception: return league, "Lỗi API", "Lỗi API"


# ===== 3. MENU START =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["chat_id"] = update.effective_chat.id
    save_data()
    menu = (
        "🤵 **AI COMMANDER V4 - CỐ VẤN TỐI CAO**\n\n"
        "🧠 **[ BỘ NÃO AI ]**\n"
        " ├ 💬 `/ai [Câu hỏi]` : Hỏi / Xin tư vấn\n"
        " ├ 📥 `/learn [Sở thích]` : Dạy AI nhớ gu của bạn\n"
        " └ 📋 `/profile` : Xem hồ sơ AI đã học\n\n"
        "📅 **[ LỊCH TRÌNH ]**\n"
        " ├ ➕ `/add [Giờ] [Việc]` | 📜 `/list`\n"
        " └ 📝 `/tnote [STT] [Ghi chú]`\n\n"
        "⚽ **[ BÓNG ĐÁ ]**\n"
        " ├ 📅 `/matches` | 🔍 `/search [Tên]`\n"
        " ├ ⏰ `/time[Giờ]` : Tìm trận theo giờ (VD: /time 20:30)\n"
        " ├ 📊 `/board` | 📜 `/history`\n"
        " ├ ℹ️ `/detail [STT]` : Xem lịch sử 2 đội\n"
        " └ 🔮 `/predict [STT]` : Chuyên gia AI Soi kèo"
    )
    await update.message.reply_text(menu, parse_mode="Markdown")

# ===== 4. XỬ LÝ AI & CÁ NHÂN HÓA =====
async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = " ".join(context.args)
    if not info: return await update.message.reply_text("❌ HD: `/learn Tôi rất ghét cược đội cửa dưới`")
    state["profile"].append(info)
    save_data()
    await update.message.reply_text(f"✅ Dạ thưa Ông chủ, em đã ghi nhớ: *{info}*", parse_mode="Markdown")

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state.get("profile"): return await update.message.reply_text("📭 Hồ sơ trống. Ông chủ hãy dùng `/learn` để dạy em.")
    res = "🧠 **HỒ SƠ CÁ NHÂN ÔNG CHỦ:**\n"
    for i, p in enumerate(state["profile"]): res += f"{i+1}. {p}\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    query = " ".join(context.args)
    if not query: return await update.message.reply_text("🤖 Dạ, Ông chủ cần em phân tích gì ạ?")
        
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
        chat = chat_sessions[chat_id]
        now = datetime.now(VN_TZ)
        today_str = now.strftime("%Y-%m-%d")
        
        # Bơm Profile Cá Nhân
        profile_info = "\n".join(state.get("profile",[]))
        if not profile_info: profile_info = "Chưa có thông tin."
        
        # Công việc
        tasks_info = "\n".join([f"- {t['time']}: {t['content']}" for t in state["tasks"] if not t.get("reminded") or t["date"] == today_str]) or "- Rảnh"
        # Bóng đá
        board_info = "\n".join([f"- {m['home']} vs {m['away']} (Lúc {m.get('time','N/A')})" for m in state["boards"].get(today_str,[]) if not m.get("notified")]) or "- Không có"

        now_str = now.strftime("%A, %d/%m/%Y %H:%M:%S")
        
        # TỔNG HỢP NGỮ CẢNH
        full_query = (
            f"[DỮ LIỆU HỆ THỐNG ({now_str})]\n"
            f"👤 Hồ sơ Ông chủ:\n{profile_info}\n\n"
            f"📋 Lịch trình:\n{tasks_info}\n\n"
            f"⚽ Bảng theo dõi bóng đá:\n{board_info}\n\n"
            f"💬 ÔNG CHỦ HỎI: {query}"
        )
        
        response = await asyncio.to_thread(chat.send_message, full_query)
        await update.message.reply_text(f"🤖 **AI Cố Vấn:**\n{response.text}")
    except Exception as e: 
        logging.error(f"Lỗi AI: {e}")
        await update.message.reply_text("❌ Xin lỗi Ông chủ, hệ thống nơ-ron đang bị quá tải.")


# ===== 5. TÌM TRẬN THEO GIỜ (CHỨC NĂNG MỚI) =====
async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❌ HD: `/time 20` (Tìm trận lúc 20h) hoặc `/time 20:30`")
    
    target_time = context.args[0]
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        res = await client.get(f"https://v3.football.api-sports.io/fixtures?date={today}")
        data = res.json().get("response",[])
        if not data: return await update.message.reply_text("📭 Hôm nay không có bóng đá.")
        
        kb =[]
        for m in data:
            time_str, _ = parse_match_time(m['fixture']['date'])
            # Logic: Nếu user gõ "20", tìm tất cả trận có giờ bắt đầu bằng "20:"
            # Nếu user gõ "20:30", tìm chính xác "20:30"
            if (len(target_time) <= 2 and time_str.startswith(f"{target_time}:")) or (time_str == target_time):
                kb.append([InlineKeyboardButton(f"⚽[{time_str}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_{m['fixture']['id']}")])
                
        if not kb:
            await update.message.reply_text(f"ℹ️ Không có trận nào diễn ra vào khung giờ `{target_time}` hôm nay.")
        else:
            # Chỉ hiện tối đa 15 trận để tránh lỗi Telegram
            await update.message.reply_text(f"⏰ **KẾT QUẢ KHUNG GIỜ {target_time}:**", reply_markup=InlineKeyboardMarkup(kb[:15]))
            
    except Exception as e:
        logging.error(f"Lỗi Time Search: {e}")
        await update.message.reply_text("❌ Lỗi quét lịch thi đấu.")


# ===== 6. CÁC TÍNH NĂNG CÒN LẠI (GIỮ NGUYÊN TỪ V3) =====
async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        time_str, content = context.args[0], " ".join(context.args[1:])
        state["tasks"].append({"time": time_str, "content": content, "reminded": False, "note": "", "date": datetime.now(VN_TZ).strftime("%Y-%m-%d")})
        save_data()
        idx = len(state["tasks"])
        kb = [[InlineKeyboardButton("📝 Thêm ghi chú", callback_data=f"asknote_t_{idx}")]]
        await update.message.reply_text(f"➕ Đã thêm: *{content}* (Báo trước 15p)", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except Exception: await update.message.reply_text("❌ HD: `/add 08:00 Việc cần làm`")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state
