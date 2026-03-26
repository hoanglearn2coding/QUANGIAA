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

# ===== 1. CẤU HÌNH (RAILWAY VARIABLES) =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
GENAI_API_KEY = os.getenv("GENAI_API_KEY")
DATA_FILE = "supreme_v2_data.json"
VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# Cấu hình Gemini AI
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

chat_sessions = {}

# ===== 2. QUẢN LÝ DỮ LIỆU =====
state = {"tasks":[], "boards": {}, "chat_id": None}

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))

client = httpx.AsyncClient(headers={"x-apisports-key": API_KEY}, timeout=20)

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

# ===== 4. XỬ LÝ AI (GEMINI) - CÓ TRUY CẬP DỮ LIỆU =====
async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    query = " ".join(context.args)
    
    if not query:
        await update.message.reply_text("🤖 Dạ, Ông chủ cần em kiểm tra lịch trình hay hỏi gì ạ?")
        return
        
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    
    try:
        # 1. KHỞI TẠO TRÍ NHỚ CHAT
        if chat_id not in chat_sessions:
            chat_sessions[chat_id] = ai_model.start_chat(history=[])
        chat = chat_sessions[chat_id]
        
        # 2. LẤY DỮ LIỆU CÔNG VIỆC (Chỉ lấy việc chưa làm hoặc của hôm nay cho nhẹ)
        now = datetime.now(VN_TZ)
        today_str = now.strftime("%Y-%m-%d")
        
        tasks_info = "📋 CÔNG VIỆC CẦN LÀM:\n"
        active_tasks =[t for t in state["tasks"] if not t.get("reminded") or t["date"] == today_str]
        if not active_tasks:
            tasks_info += "- Hiện tại Ông chủ đang rảnh, không có việc nào cần làm.\n"
        else:
            for i, t in enumerate(active_tasks):
                status = "Đã xong/Đã nhắc" if t.get("reminded") else "Chưa làm"
                note = f" (Note: {t['note']})" if t.get("note") else ""
                tasks_info += f"- Ngày {t['date']} lúc {t['time']}: {t['content']} [{status}]{note}\n"

        # 3. LẤY DỮ LIỆU BÓNG ĐÁ HÔM NAY
        board_info = f"⚽ TRẬN BÓNG ĐANG THEO DÕI ({today_str}):\n"
        today_board = state["boards"].get(today_str,[])
        if not today_board:
            board_info += "- Hôm nay Ông chủ không theo dõi trận nào.\n"
        else:
            for i, m in enumerate(today_board):
                score_text = f" (Tỷ số: {m['score']})" if m.get("score") else ""
                note = f" (Note: {m['note']})" if m.get("note") else ""
                status = "Đã đá xong" if m.get("notified") else "Chưa đá/Đang chờ"
                board_info += f"- {m['home']} vs {m['away']} [{status}]{score_text}{note}\n"

        # 4. BƠM VÀO NGỮ CẢNH HỆ THỐNG TRƯỚC KHI HỎI
        now_str = now.strftime("%A, ngày %d/%m/%Y, %H:%M:%S")
        system_context = f"[Dữ liệu hệ thống lúc {now_str}]\n{tasks_info}\n{board_info}\n"
        
        # Đóng gói câu hỏi của Ông chủ
        full_query = f"{system_context}\nÔng chủ hỏi: {query}"
        
        # 5. GỬI CHO AI XỬ LÝ
        response = await asyncio.to_thread(chat.send_message, full_query)
        
        await update.message.reply_text(f"🤖 **Quản gia AI:**\n{response.text}")
        
    except Exception as e: 
        logging.error(f"Lỗi AI: {e}")
        error_msg = str(e)
        if "API_KEY_INVALID" in error_msg:
            await update.message.reply_text("❌ Lỗi: API Key Gemini của Ông chủ không hợp lệ!")
        else:
            await update.message.reply_text("❌ Xin lỗi Ông chủ, hệ thống của em đang tạm thời mất kết nối.")
            
# ===== 5. NHÓM TASK (CÓ HỎI NOTE) =====
async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        time_str, content = context.args[0], " ".join(context.args[1:])
        new_task = {"time": time_str, "content": content, "reminded": False, "note": "", "date": datetime.now(VN_TZ).strftime("%Y-%m-%d")}
        state["tasks"].append(new_task)
        save_data()
        
        idx = len(state["tasks"])
        kb = [[InlineKeyboardButton("📝 Thêm ghi chú ngay", callback_data=f"asknote_t_{idx}")]]
        await update.message.reply_text(f"➕ Đã thêm: *{content}*", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Lỗi Add Task: {e}")
        await update.message.reply_text("❌ HD: `/add 08:00 Việc cần làm` (Lưu ý gõ đúng định dạng giờ có dấu hai chấm)")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state["tasks"]: return await update.message.reply_text("📭 Danh sách trống.")
    res = "📜 **DANH SÁCH VIỆC:**\n"
    for i, t in enumerate(state["tasks"]):
        res += f"{i+1}. 🕒 {t['time']} - {t['content']}\n"
        if t["note"]: res += f"   └ 📝: _{t['note']}_\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def tnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx, note = int(context.args[0])-1, " ".join(context.args[1:])
        state["tasks"][idx]["note"] = note
        save_data()
        await update.message.reply_text(f"✅ Đã lưu note cho việc {idx+1}")
    except Exception as e: 
        logging.error(f"Lỗi TNote: {e}")
        await update.message.reply_text("❌ HD: `/tnote 1 Nội dung ghi chú`")

# ===== 6. NHÓM BÓNG ĐÁ (CÓ HỎI NOTE) =====
async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    try:
        res = await client.get(f"https://v3.football.api-sports.io/fixtures?date={today}")
        data = res.json().get("response",[])
        if not data: return await update.message.reply_text("📭 Không có trận đấu nào hôm nay.")
        kb = [[InlineKeyboardButton(f"⚽ {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_{m['fixture']['id']}")] for m in data[:12]]
        await update.message.reply_text("📅 **CHỌN TRẬN ĐỂ THEO DÕI:**", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        logging.error(f"Lỗi Matches: {e}")
        await update.message.reply_text("❌ Lỗi tải lịch thi đấu.")

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).lower()
    try:
        res = await client.get(f"https://v3.football.api-sports.io/fixtures?date={datetime.now(VN_TZ).strftime('%Y-%m-%d')}")
        data =[m for m in res.json().get("response", []) if query in m['teams']['home']['name'].lower() or query in m['teams']['away']['name'].lower()]
        if not data: return await update.message.reply_text("ℹ️ Không tìm thấy đội bóng này hôm nay.")
        kb = [[InlineKeyboardButton(f"⚽ {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_{m['fixture']['id']}")] for m in data]
        await update.message.reply_text(f"🔍 Kết quả cho '{query}':", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        logging.error(f"Lỗi Search: {e}")
        await update.message.reply_text("❌ Lỗi tìm kiếm trận đấu.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")

    if data.startswith("pk_"):
        fid = int(data.split("_")[1])
        state["boards"].setdefault(today,[])
        
        # Kiểm tra xem đã theo dõi trận này chưa
        if any(m['id'] == fid for m in state["boards"][today]):
            await query.answer("Trận này đã có trong Board!", show_alert=True)
            return

        try:
            res = await client.get(f"https://v3.football.api-sports.io/fixtures?id={fid}")
            m = res.json()["response"][0]
            state["boards"][today].append({
                "id": fid, "home": m["teams"]["home"]["name"], 
                "away": m["teams"]["away"]["name"], "status_icon": "⏳", 
                "note": "", "notified": False, "score": ""
            })
            save_data()
            
            idx = len(state["boards"][today])
            kb = [[InlineKeyboardButton("📝 Thêm ghi chú trận đấu", callback_data=f"asknote_m_{idx}")]]
            await query.edit_message_text(f"✅ Đã pick theo dõi trận: {m['teams']['home']['name']} vs {m['teams']['away']['name']}", reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            logging.error(f"Lỗi Pick Trận: {e}")
            await query.answer("Lỗi thêm trận đấu!", show_alert=True)

    elif data.startswith("asknote_"):
        _, kind, idx = data.split("_")
        cmd = "/tnote" if kind == "t" else "/mnote"
        await query.message.reply_text(f"👉 Ông chủ gõ lệnh sau để thêm ghi chú:\n`{cmd} {idx} [Nội dung]`", parse_mode="Markdown")
        await query.answer()

async def board_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    matches = [m for m in state["boards"].get(today,[]) if not m.get("notified")]
    if not matches: return await update.message.reply_text("📭 Board trống.")
    res = f"📊 **BOARD {today}:**\n"
    for i, m in enumerate(matches):
        res += f"{i+1}. ⏳ *{m['home']} vs {m['away']}*\n"
        if m["note"]: res += f"   └ 📝: _{m['note']}_\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    matches =[m for m in state["boards"].get(today,[]) if m.get("notified")]
    if not matches: return await update.message.reply_text("📭 Chưa có trận nào kết thúc hôm nay.")
    res = "📜 **HISTORY BOARD:**\n"
    for i, m in enumerate(matches):
        res += f"{i+1}. ✅ {m['home']} {m.get('score','')} {m['away']}\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def mnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        idx, note = int(context.args[0])-1, " ".join(context.args[1:])
        state["boards"][today][idx]["note"] = note
        save_data()
        await update.message.reply_text(f"✅ Đã ghi chú cho trận số {idx+1}")
    except Exception as e: 
        logging.error(f"Lỗi MNote: {e}")
        await update.message.reply_text("❌ HD: `/mnote 1 Nội dung`")

# ===== 7. MONITOR TỰ ĐỘNG (TỐI ƯU API) =====
async def monitor(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(VN_TZ)
    today = now.strftime("%Y-%m-%d")
    if not state["chat_id"]: return
    
    # 1. Quét công việc (Task)
    for t in state["tasks"]:
        if not t["reminded"] and t["date"] == today:
            try:
                target = datetime.strptime(t["time"], "%H:%M").replace(year=now.year, month=now.month, day=now.day, tzinfo=VN_TZ)
                if now >= (target - timedelta(minutes=15)) and now < target:
                    await context.bot.send_message(state["chat_id"], f"⏰ **SẮP ĐẾN GIỜ:** {t['content']}")
                    t["reminded"] = True
                    save_data()
            except ValueError:
                # Tránh sập bot nếu format thời gian bị người dùng nhập sai
                continue

    # 2. Quét Bóng đá (Football)
    if today in state["boards"]:
        unnotified_matches = [m for m in state["boards"][today] if not m.get("notified")]
        
        # Chỉ gọi API nếu thực sự có trận đấu chưa kết thúc trong board
        if unnotified_matches:
            # FIX TỐI ƯU: Gom tất cả ID lại để gọi 1 request duy nhất (VD: ids=123-456-789)
            match_ids = "-".join(str(m['id']) for m in unnotified_matches)
            try:
                res = await client.get(f"https://v3.football.api-sports.io/fixtures?ids={match_ids}")
                data = res.json().get("response", [])
                
                for f in data:
                    fixture_id = f["fixture"]["id"]
                    status = f["fixture"]["status"]["short"]
                    
                    # FT (Full time), AET (After extra time), PEN (Penalties)
                    if status in["FT", "AET", "PEN"]:
                        # Cập nhật state
                        for m in state["boards"][today]:
                            if m["id"] == fixture_id and not m.get("notified"):
                                m["score"] = f"{f['goals']['home']}-{f['goals']['away']}"
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
    
    # FIX TỐI ƯU: Tăng interval lên 600s (10 phút) để tránh cạn kiệt request quota
    if app.job_queue: 
        app.job_queue.run_repeating(monitor, interval=600, first=10)
        
    print("🚀 BOT SUPREME V2.1 ĐÃ ONLINE!")
    app.run_polling()

if __name__ == "__main__": 
    main()