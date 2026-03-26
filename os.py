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
API_KEY = os.getenv("API_KEY") # API của API-Sports
GENAI_API_KEY = os.getenv("GENAI_API_KEY") # API của Gemini

VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- Cấu hình Thư mục Data (Cho Railway Volume) ---
DATA_DIR = "/data"
if not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        DATA_DIR = "." # Fallback lưu tại thư mục hiện tại nếu chạy local trên máy tính

DATA_FILE = os.path.join(DATA_DIR, "supreme_v2_data.json")

# --- Cấu hình Gemini AI 2.5 ---
genai.configure(api_key=GENAI_API_KEY)
system_prompt = (
    "Bạn là một quản gia AI trung thành, thông minh, tinh tế và có chút hài hước của Ông chủ. "
    "Luôn xưng hô là 'Dạ', 'Ông chủ' và xưng 'tôi' hoặc 'em'. "
    "Tuyệt đối trả lời ngắn gọn, tự nhiên như người thật, không lan man dài dòng. "
    "ĐẶC BIỆT CHÚ Ý: Bạn có quyền truy cập vào 'Danh sách công việc' (Tasks) và 'Bảng theo dõi bóng đá' (Boards) của Ông chủ thông qua [Dữ liệu hệ thống]. "
    "Hãy dựa vào những dữ liệu này để báo cáo, nhắc nhở, hoặc nhận xét khi Ông chủ hỏi thăm lịch trình, công việc hay các trận bóng đá."
)
ai_model = genai.GenerativeModel(
    'gemini-2.5-flash',
    system_instruction=system_prompt,
    generation_config=genai.types.GenerationConfig(temperature=0.5)
)

# ===== 2. QUẢN LÝ DỮ LIỆU & BIẾN GLOBAL =====
state = {"tasks":[], "boards": {}, "chat_id": None}
chat_sessions = {} # Trí nhớ của AI
last_api_check = 0 # Thời điểm gọi API bóng đá cuối cùng
client = httpx.AsyncClient(headers={"x-apisports-key": API_KEY}, timeout=20)

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))

def parse_match_time(utc_date_str):
    """Dịch giờ API (UTC) sang giờ Việt Nam và lấy Timestamp"""
    dt = datetime.strptime(utc_date_str.split('+')[0], "%Y-%m-%dT%H:%M:%S")
    dt = dt.replace(tzinfo=pytz.UTC).astimezone(VN_TZ)
    return dt.strftime("%H:%M"), dt.timestamp()


# ===== 3. MENU START =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["chat_id"] = update.effective_chat.id
    save_data()
    menu = (
        "🤵 **AI COMMANDER - THỰC ĐƠN PHỤC VỤ**\n\n"
        "🧠 **[ NHẮC VIỆC ]**\n"
        " ├ ➕ `/add [Giờ] [Việc]` (VD: /add 08:00 Mua cafe)\n"
        " ├ 📜 `/list` : Xem danh sách\n"
        " └ 📝 `/tnote [STT] [Ghi chú]`\n\n"
        "⚽ **[ TRẬN ĐẤU ]**\n"
        " ├ 📅 `/matches` : Lịch hôm nay\n"
        " ├ 🔍 `/search [Tên]` : Tìm trận\n"
        " ├ 📊 `/board` : Trận đang theo dõi\n"
        " ├ 📜 `/history` : Trận đã xong\n"
        " └ 📝 `/mnote [STT] [Ghi chú]`\n\n"
        "🤖 **[ TRỢ LÝ AI ]**\n"
        " └ 💬 `/ai [Câu hỏi]` : Dạ, Ông chủ dùng gì?"
    )
    await update.message.reply_text(menu, parse_mode="Markdown")


# ===== 4. XỬ LÝ AI (GEMINI) =====
async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    query = " ".join(context.args)
    
    if not query:
        await update.message.reply_text("🤖 Dạ, Ông chủ cần em kiểm tra lịch trình hay hỏi gì ạ?")
        return
        
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    
    try:
        # 1. Khởi tạo trí nhớ
        if chat_id not in chat_sessions:
            chat_sessions[chat_id] = ai_model.start_chat(history=[])
        chat = chat_sessions[chat_id]
        
        # 2. Lấy dữ liệu bơm vào ngữ cảnh cho AI
        now = datetime.now(VN_TZ)
        today_str = now.strftime("%Y-%m-%d")
        
        # Công việc
        tasks_info = "📋 CÔNG VIỆC CẦN LÀM:\n"
        active_tasks = [t for t in state["tasks"] if not t.get("reminded") or t["date"] == today_str]
        if not active_tasks:
            tasks_info += "- Hiện tại Ông chủ đang rảnh, không có việc nào cần làm.\n"
        else:
            for t in active_tasks:
                status = "Đã xong/Đã nhắc" if t.get("reminded") else "Chưa làm"
                note = f" (Note: {t['note']})" if t.get("note") else ""
                tasks_info += f"- Ngày {t['date']} lúc {t['time']}: {t['content']} [{status}]{note}\n"

        # Bóng đá
        board_info = f"⚽ TRẬN BÓNG ĐANG THEO DÕI ({today_str}):\n"
        today_board = state["boards"].get(today_str,[])
        if not today_board:
            board_info += "- Hôm nay Ông chủ không theo dõi trận nào.\n"
        else:
            for m in today_board:
                score_text = f" (Tỷ số: {m['score']})" if m.get("score") else ""
                note = f" (Note: {m['note']})" if m.get("note") else ""
                status = "Đã đá xong" if m.get("notified") else f"Chưa đá (lúc {m.get('time', 'N/A')})"
                board_info += f"- {m['home']} vs {m['away']} [{status}]{score_text}{note}\n"

        # 3. Gửi câu hỏi kèm dữ liệu cho AI
        now_str = now.strftime("%A, ngày %d/%m/%Y, %H:%M:%S")
        system_context = f"[Dữ liệu hệ thống lúc {now_str}]\n{tasks_info}\n{board_info}\n"
        full_query = f"{system_context}\nÔng chủ hỏi: {query}"
        
        response = await asyncio.to_thread(chat.send_message, full_query)
        await update.message.reply_text(f"🤖 **Quản gia AI:**\n{response.text}")
        
    except Exception as e: 
        logging.error(f"Lỗi AI: {e}")
        if "API_KEY" in str(e):
            await update.message.reply_text("❌ Lỗi: API Key Gemini của Ông chủ không hợp lệ!")
        else:
            await update.message.reply_text("❌ Xin lỗi Ông chủ, hệ thống suy nghĩ của em đang mất kết nối.")


# ===== 5. NHÓM TASK (CÔNG VIỆC) =====
async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        time_str, content = context.args[0], " ".join(context.args[1:])
        new_task = {
            "time": time_str, "content": content, 
            "reminded": False, "note": "", 
            "date": datetime.now(VN_TZ).strftime("%Y-%m-%d")
        }
        state["tasks"].append(new_task)
        save_data()
        
        idx = len(state["tasks"])
        kb = [[InlineKeyboardButton("📝 Thêm ghi chú ngay", callback_data=f"asknote_t_{idx}")]]
        await update.message.reply_text(f"➕ Đã thêm: *{content}* (Báo trước 15p)", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text("❌ HD: `/add 08:00 Việc cần làm` (Gõ đúng định dạng giờ có dấu hai chấm)")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state["tasks"]: return await update.message.reply_text("📭 Danh sách trống.")
    res = "📜 **DANH SÁCH VIỆC:**\n"
    for i, t in enumerate(state["tasks"]):
        status = "✅" if t.get("reminded") else "🕒"
        res += f"{i+1}. {status} {t['time']} - {t['content']} ({t['date']})\n"
        if t["note"]: res += f"   └ 📝: _{t['note']}_\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def tnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx, note = int(context.args[0])-1, " ".join(context.args[1:])
        state["tasks"][idx]["note"] = note
        save_data()
        await update.message.reply_text(f"✅ Đã lưu note cho việc {idx+1}")
    except Exception: 
        await update.message.reply_text("❌ HD: `/tnote 1 Nội dung ghi chú`")


# ===== 6. NHÓM BÓNG ĐÁ =====
async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    try:
        res = await client.get(f"https://v3.football.api-sports.io/fixtures?date={today}")
        data = res.json().get("response",[])
        if not data: return await update.message.reply_text("📭 Không có trận đấu nào hôm nay.")
        
        kb = []
        for m in data[:12]:
            time_str, _ = parse_match_time(m['fixture']['date'])
            kb.append([InlineKeyboardButton(f"⚽ [{time_str}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_{m['fixture']['id']}")])
            
        await update.message.reply_text("📅 **CHỌN TRẬN ĐỂ THEO DÕI:**", reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        await update.message.reply_text("❌ Lỗi tải lịch thi đấu.")

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).lower()
    try:
        res = await client.get(f"https://v3.football.api-sports.io/fixtures?date={datetime.now(VN_TZ).strftime('%Y-%m-%d')}")
        data = [m for m in res.json().get("response", []) if query in m['teams']['home']['name'].lower() or query in m['teams']['away']['name'].lower()]
        if not data: return await update.message.reply_text("ℹ️ Không tìm thấy đội bóng này hôm nay.")
        
        kb =[]
        for m in data:
            time_str, _ = parse_match_time(m['fixture']['date'])
            kb.append([InlineKeyboardButton(f"⚽[{time_str}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_{m['fixture']['id']}")])
            
        await update.message.reply_text(f"🔍 Kết quả cho '{query}':", reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        await update.message.reply_text("❌ Lỗi tìm kiếm trận đấu.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")

    if data.startswith("pk_"):
        fid = int(data.split("_")[1])
        state["boards"].setdefault(today,[])
        
        if any(m['id'] == fid for m in state["boards"][today]):
            await query.answer("Trận này đã có trong Board!", show_alert=True)
            return

        try:
            res = await client.get(f"https://v3.football.api-sports.io/fixtures?id={fid}")
            m = res.json()["response"][0]
            
            time_str, timestamp = parse_match_time(m['fixture']['date'])
            
            state["boards"][today].append({
                "id": fid, "home": m["teams"]["home"]["name"], 
                "away": m["teams"]["away"]["name"], 
                "time": time_str, "timestamp": timestamp, 
                "status_icon": "⏳", "note": "", 
                "notified": False, "reminded_15m": False, 
                "score": ""
            })
            save_data()
            
            idx = len(state["boards"][today])
            kb = [[InlineKeyboardButton("📝 Thêm ghi chú trận đấu", callback_data=f"asknote_m_{idx}")]]
            await query.edit_message_text(f"✅ Đã pick trận lúc {time_str}: {m['teams']['home']['name']} vs {m['teams']['away']['name']}", reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            logging.error(f"Lỗi Pick Trận: {e}")
            await query.answer("Lỗi thêm trận đấu!", show_alert=True)

    elif data.startswith("asknote_"):
        _, kind, idx = data.split("_")
        cmd = "/tnote" if kind == "t" else "/mnote"
        await query.message.reply_text(f"👉 Ông chủ gõ lệnh sau để thêm ghi chú:\n`{cmd} {idx}[Nội dung]`", parse_mode="Markdown")
        await query.answer()

async def board_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    matches = [m for m in state["boards"].get(today,[]) if not m.get("notified")]
    if not matches: return await update.message.reply_text("📭 Board trống.")
    
    res = f"📊 **BOARD {today}:**\n"
    for i, m in enumerate(matches):
        res += f"{i+1}. ⏳ [{m.get('time', 'N/A')}] *{m['home']} vs {m['away']}*\n"
        if m.get("note"): res += f"   └ 📝: _{m['note']}_\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    matches = [m for m in state["boards"].get(today,[]) if m.get("notified")]
    if not matches: return await update.message.reply_text("📭 Chưa có trận nào kết thúc hôm nay.")
    
    res = "📜 **HISTORY BOARD:**\n"
    for i, m in enumerate(matches):
        res += f"{i+1}. ✅ {m['home']} {m.get('score', '')} {m['away']}\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def mnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        idx, note = int(context.args[0])-1, " ".join(context.args[1:])
        state["boards"][today][idx]["note"] = note
        save_data()
        await update.message.reply_text(f"✅ Đã ghi chú cho trận số {idx+1}")
    except Exception: 
        await update.message.reply_text("❌ HD: `/mnote 1 Nội dung`")


# ===== 7. MONITOR TỰ ĐỘNG (NHẮC CHÍNH XÁC & TỐI ƯU API) =====
async def monitor(context: ContextTypes.DEFAULT_TYPE):
    global last_api_check
    now = datetime.now(VN_TZ)
    today = now.strftime("%Y-%m-%d")
    if not state["chat_id"]: return
    
    # 1. QUÉT CÔNG VIỆC (Báo trước 15 phút) - Chạy Offline không tốn API
    for t in state["tasks"]:
        if not t.get("reminded") and t.get("date") == today:
            try:
                target = datetime.strptime(t["time"], "%H:%M").replace(year=now.year, month=now.month, day=now.day, tzinfo=VN_TZ)
                if now >= (target - timedelta(minutes=15)) and now < target:
                    await context.bot.send_message(state["chat_id"], f"⏰ **SẮP ĐẾN GIỜ (15p nữa):** {t['content']}")
                    t["reminded"] = True
                    save_data()
            except ValueError:
                continue

    # 2. QUÉT BÓNG LĂN (Báo trước 15 phút) - Chạy Offline không tốn API
    if today in state["boards"]:
        for m in state["boards"][today]:
            if not m.get("reminded_15m") and "timestamp" in m:
                target = datetime.fromtimestamp(m["timestamp"], VN_TZ)
                if now >= (target - timedelta(minutes=15)) and now < target:
                    await context.bot.send_message(
                        state["chat_id"], 
                        f"⏰ **SẮP ĐÁ (15p nữa):** ⚽ {m['home']} vs {m['away']} lúc {m['time']}"
                    )
                    m["reminded_15m"] = True
                    save_data()

    # 3. QUÉT KẾT QUẢ BÓNG ĐÁ (Chỉ gọi API mỗi 10 phút/lần = 600s để bảo vệ Quota API)
    if now.timestamp() - last_api_check >= 600:
        last_api_check = now.timestamp()
        
        if today in state["boards"]:
            unnotified_matches = [m for m in state["boards"][today] if not m.get("notified")]
            if unnotified_matches:
                match_ids = "-".join(str(m['id']) for m in unnotified_matches)
                try:
                    res = await client.get(f"https://v3.football.api-sports.io/fixtures?ids={match_ids}")
                    data = res.json().get("response",[])
                    
                    for f in data:
                        fixture_id = f["fixture"]["id"]
                        status = f["fixture"]["status"]["short"]
                        
                        if status in ["FT", "AET", "PEN"]:
                            for m in state["boards"][today]:
                                if m["id"] == fixture_id and not m.get("notified"):
                                    # Lấy tỷ số an toàn
                                    home_goal = f['goals']['home'] if f['goals']['home'] is not None else 0
                                    away_goal = f['goals']['away'] if f['goals']['away'] is not None else 0
                                    m["score"] = f"{home_goal}-{away_goal}"
                                    
                                    await context.bot.send_message(
                                        state["chat_id"], 
                                        f"🏁 **KẾT THÚC:** {m['home']} {m['score']} {m['away']}"
                                    )
                                    m["notified"] = True
                                    save_data()
                except Exception as e:
                    logging.error(f"Lỗi Monitor Bóng Đá: {e}")


# ===== 8. MAIN =====
def main():
    load_data()
    app = ApplicationBuilder().token(BOT_TOKEN).defaults(Defaults(tzinfo=VN_TZ)).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(CommandHandler("add", add_task))
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("tnote", tnote_cmd))
    app.add_handler(CommandHandler("matches", matches_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("board", board_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("mnote", mnote_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Đặt vòng lặp kiểm tra là 60 giây (1 phút) để nhắc việc chính xác tuyệt đối
    if app.job_queue: 
        app.job_queue.run_repeating(monitor, interval=60, first=10)
        
    print("🚀 SUPREME AI COMMANDER V2.5 ĐÃ SẴN SÀNG PHỤC VỤ!")
    app.run_polling()

if __name__ == "__main__": 
    main()