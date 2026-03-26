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
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        DATA_DIR = "." 
DATA_FILE = os.path.join(DATA_DIR, "supreme_v2_data.json")

# --- Cấu hình Gemini AI 2.5 ---
genai.configure(api_key=GENAI_API_KEY)
system_prompt = (
    "Bạn là một quản gia AI trung thành, thông minh, tinh tế và có chút hài hước của Ông chủ. "
    "Luôn xưng hô là 'Dạ', 'Ông chủ' và xưng 'tôi' hoặc 'em'. "
    "Tuyệt đối trả lời ngắn gọn, tự nhiên như người thật, không lan man dài dòng. "
    "ĐẶC BIỆT CHÚ Ý: Bạn có quyền truy cập vào 'Danh sách công việc' (Tasks) và 'Bảng theo dõi bóng đá' (Boards) của Ông chủ thông qua[Dữ liệu hệ thống]. "
    "Hãy dựa vào những dữ liệu này để báo cáo, nhắc nhở, hoặc nhận xét khi Ông chủ hỏi thăm lịch trình, công việc hay các trận bóng đá."
)
ai_model = genai.GenerativeModel(
    'gemini-2.5-flash',
    system_instruction=system_prompt,
    generation_config=genai.types.GenerationConfig(temperature=0.6)
)

# ===== 2. QUẢN LÝ DỮ LIỆU & BIẾN GLOBAL =====
state = {"tasks":[], "boards": {}, "chat_id": None}
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

def parse_match_time(utc_date_str):
    dt = datetime.strptime(utc_date_str.split('+')[0], "%Y-%m-%dT%H:%M:%S")
    dt = dt.replace(tzinfo=pytz.UTC).astimezone(VN_TZ)
    return dt.strftime("%H:%M"), dt.timestamp()

# Hàm nội bộ lấy lịch sử 2 trận gần nhất
async def get_match_context(m):
    home_id = m.get("home_id")
    away_id = m.get("away_id")
    league = m.get("league", "Không rõ giải đấu")
    
    # Vá lỗi cho các trận cũ chưa lưu ID
    if not home_id:
        try:
            res = await client.get(f"https://v3.football.api-sports.io/fixtures?id={m['id']}")
            data = res.json().get("response",[])
            if data:
                fix_data = data[0]
                home_id = fix_data["teams"]["home"]["id"]
                away_id = fix_data["teams"]["away"]["id"]
                league = fix_data["league"]["name"]
                m["home_id"] = home_id
                m["away_id"] = away_id
                m["league"] = league
                save_data()
            else:
                return league, "Không có dữ liệu", "Không có dữ liệu"
        except Exception:
            return league, "Lỗi tải dữ liệu", "Lỗi tải dữ liệu"

    try:
        # Gọi API lấy 2 trận gần nhất của mỗi đội
        res_home = await client.get(f"https://v3.football.api-sports.io/fixtures?team={home_id}&last=2")
        res_away = await client.get(f"https://v3.football.api-sports.io/fixtures?team={away_id}&last=2")
        
        def format_last_matches(data):
            lines = []
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
    except Exception as e:
        logging.error(f"Lỗi lấy lịch sử: {e}")
        return league, "Lỗi kết nối API", "Lỗi kết nối API"


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
        " ├ ℹ️ `/detail[STT]` : Chi tiết & Lịch sử\n"
        " ├ 🔮 `/predict [STT]` : AI Soi kèo\n"
        " ├ 📜 `/history` : Trận đã xong\n"
        " └ 📝 `/mnote [STT] [Ghi chú]`\n\n"
        "🤖 **[ TRỢ LÝ AI ]**\n"
        " └ 💬 `/ai [Câu hỏi]` : Dạ, Ông chủ dùng gì?"
    )
    await update.message.reply_text(menu, parse_mode="Markdown")

# ===== 4. XỬ LÝ AI CHUNG =====
async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    query = " ".join(context.args)
    if not query:
        return await update.message.reply_text("🤖 Dạ, Ông chủ cần em kiểm tra lịch trình hay hỏi gì ạ?")
        
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        if chat_id not in chat_sessions:
            chat_sessions[chat_id] = ai_model.start_chat(history=[])
        chat = chat_sessions[chat_id]
        now = datetime.now(VN_TZ)
        today_str = now.strftime("%Y-%m-%d")
        
        # Công việc
        tasks_info = "📋 CÔNG VIỆC:\n"
        active_tasks = [t for t in state["tasks"] if not t.get("reminded") or t["date"] == today_str]
        if not active_tasks: tasks_info += "- Rảnh.\n"
        else:
            for t in active_tasks:
                status = "Xong" if t.get("reminded") else "Chưa làm"
                tasks_info += f"- {t['date']} {t['time']}: {t['content']} [{status}]\n"

        # Bóng đá
        board_info = f"⚽ TRẬN THEO DÕI ({today_str}):\n"
        today_board = state["boards"].get(today_str,[])
        if not today_board: board_info += "- Không có.\n"
        else:
            for m in today_board:
                status = "Đã xong" if m.get("notified") else f"Chờ ({m.get('time', 'N/A')})"
                score = f" (Tỷ số: {m['score']})" if m.get("score") else ""
                board_info += f"- {m['home']} vs {m['away']} [{status}]{score}\n"

        now_str = now.strftime("%A, %d/%m/%Y %H:%M:%S")
        full_query = f"[Hệ thống lúc {now_str}]\n{tasks_info}\n{board_info}\nÔng chủ hỏi: {query}"
        
        response = await asyncio.to_thread(chat.send_message, full_query)
        await update.message.reply_text(f"🤖 **Quản gia AI:**\n{response.text}")
        
    except Exception as e: 
        logging.error(f"Lỗi AI: {e}")
        await update.message.reply_text("❌ Xin lỗi Ông chủ, hệ thống đang mất kết nối.")

# ===== 5. NHÓM TASK (CÔNG VIỆC) =====
async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        time_str, content = context.args[0], " ".join(context.args[1:])
        state["tasks"].append({
            "time": time_str, "content": content, "reminded": False, "note": "", 
            "date": datetime.now(VN_TZ).strftime("%Y-%m-%d")
        })
        save_data()
        idx = len(state["tasks"])
        kb = [[InlineKeyboardButton("📝 Thêm ghi chú", callback_data=f"asknote_t_{idx}")]]
        await update.message.reply_text(f"➕ Đã thêm: *{content}* (Báo trước 15p)", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("❌ HD: `/add 08:00 Việc cần làm`")

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
        await update.message.reply_text(f"✅ Đã lưu note việc {idx+1}")
    except Exception: await update.message.reply_text("❌ HD: `/tnote 1 Nội dung`")


# ===== 6. NHÓM BÓNG ĐÁ =====
async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    try:
        res = await client.get(f"https://v3.football.api-sports.io/fixtures?date={today}")
        data = res.json().get("response",[])
        if not data: return await update.message.reply_text("📭 Không có trận đấu nào hôm nay.")
        kb =[]
        for m in data[:12]:
            time_str, _ = parse_match_time(m['fixture']['date'])
            kb.append([InlineKeyboardButton(f"⚽ [{time_str}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_{m['fixture']['id']}")])
        await update.message.reply_text("📅 **CHỌN TRẬN ĐỂ THEO DÕI:**", reply_markup=InlineKeyboardMarkup(kb))
    except Exception: await update.message.reply_text("❌ Lỗi tải lịch.")

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
    except Exception: await update.message.reply_text("❌ Lỗi tìm kiếm.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")

    if data.startswith("pk_"):
        fid = int(data.split("_")[1])
        state["boards"].setdefault(today,[])
        if any(m['id'] == fid for m in state["boards"][today]):
            return await query.answer("Trận này đã có trong Board!", show_alert=True)

        try:
            res = await client.get(f"https://v3.football.api-sports.io/fixtures?id={fid}")
            m_data = res.json()["response"][0]
            time_str, timestamp = parse_match_time(m_data['fixture']['date'])
            
            state["boards"][today].append({
                "id": fid, "home": m_data["teams"]["home"]["name"], "away": m_data["teams"]["away"]["name"], 
                "time": time_str, "timestamp": timestamp, 
                "league": m_data['league']['name'], 
                "home_id": m_data['teams']['home']['id'], "away_id": m_data['teams']['away']['id'],
                "status_icon": "⏳", "note": "", "notified": False, "reminded_15m": False, "score": ""
            })
            save_data()
            idx = len(state["boards"][today])
            kb = [[InlineKeyboardButton("📝 Thêm ghi chú", callback_data=f"asknote_m_{idx}")]]
            await query.edit_message_text(f"✅ Đã pick: {m_data['teams']['home']['name']} vs {m_data['teams']['away']['name']}", reply_markup=InlineKeyboardMarkup(kb))
        except Exception: await query.answer("Lỗi thêm trận!", show_alert=True)

    elif data.startswith("asknote_"):
        _, kind, idx = data.split("_")
        cmd = "/tnote" if kind == "t" else "/mnote"
        await query.message.reply_text(f"👉 Gõ lệnh:\n`{cmd} {idx} [Nội dung]`", parse_mode="Markdown")
        await query.answer()

    # Nút bấm Soi Kèo AI
    elif data.startswith("ai_predict_"):
        idx = int(data.split("_")[2])
        if today not in state["boards"] or idx >= len(state["boards"][today]):
            return await query.answer("Trận đấu đã cũ hoặc không tồn tại!", show_alert=True)
            
        m = state["boards"][today][idx]
        await query.answer("AI đang phân tích...", show_alert=False)
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action="typing")
        
        league, home_last, away_last = await get_match_context(m)
        prompt = (
            f"Phân tích trận đấu bóng đá:\n- Giải: {league}\n"
            f"- Trận: {m['home']} vs {m['away']}\n"
            f"- Phong độ 2 trận gần nhất ({m['home']}):\n{home_last}\n"
            f"- Phong độ 2 trận gần nhất ({m['away']}):\n{away_last}\n\n"
            "Dựa vào thông tin trên, đóng vai chuyên gia bóng đá phân tích phong độ, dự đoán tỷ số và chốt kèo. "
            "Trả lời súc tích, chuyên nghiệp và hơi hài hước."
        )
        
        chat_id = query.message.chat_id
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
        response = await asyncio.to_thread(chat_sessions[chat_id].send_message, prompt)
        
        await query.message.reply_text(f"🔮 **AI SOI KÈO ({m['home']} vs {m['away']}):**\n\n{response.text}")

async def board_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    matches = [m for m in state["boards"].get(today,[]) if not m.get("notified")]
    if not matches: return await update.message.reply_text("📭 Board trống.")
    
    res = f"📊 **BOARD {today}:**\n"
    for i, m in enumerate(matches):
        res += f"{i+1}. ⏳ [{m.get('time', 'N/A')}] *{m['home']} vs {m['away']}*\n"
        if m.get("note"): res += f"   └ 📝: _{m['note']}_\n"
    await update.message.reply_text(res, parse_mode="Markdown")

# CHỨC NĂNG MỚI: CHI TIẾT TRẬN (Lịch sử)
async def detail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        idx = int(context.args[0]) - 1
        m = state["boards"][today][idx]
        
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        league, home_last, away_last = await get_match_context(m)
        
        res = (
            f"🏆 **GIẢI ĐẤU:** {league}\n"
            f"⚽ **TRẬN:** {m['home']} vs {m['away']}\n"
            f"⏰ **THỜI GIAN:** {m.get('time', 'N/A')}\n\n"
            f"🛡️ **LỊCH SỬ ĐẤU ({m['home'].upper()}):**\n{home_last}\n\n"
            f"⚔️ **LỊCH SỬ ĐẤU ({m['away'].upper()}):**\n{away_last}"
        )
        # Nút bấm nhờ AI Soi kèo trực tiếp
        kb = [[InlineKeyboardButton("🔮 Nhờ AI Soi Kèo", callback_data=f"ai_predict_{idx}")]]
        await update.message.reply_text(res, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("❌ Lỗi! HD: `/detail 1` (Số 1 là thứ tự trong Board)")

# CHỨC NĂNG MỚI: AI SOI KÈO (Bằng lệnh)
async def predict_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        idx = int(context.args[0]) - 1
        m = state["boards"][today][idx]
        
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        league, home_last, away_last = await get_match_context(m)
        
        prompt = (
            f"Phân tích trận đấu bóng đá:\n- Giải: {league}\n"
            f"- Trận: {m['home']} vs {m['away']}\n"
            f"- Phong độ 2 trận gần nhất ({m['home']}):\n{home_last}\n"
            f"- Phong độ 2 trận gần nhất ({m['away']}):\n{away_last}\n\n"
            "Dựa vào thông tin trên, đóng vai chuyên gia bóng đá phân tích phong độ, dự đoán tỷ số và chốt kèo. "
            "Trả lời súc tích, chuyên nghiệp và hơi hài hước."
        )
        
        chat_id = update.effective_chat.id
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
        response = await asyncio.to_thread(chat_sessions[chat_id].send_message, prompt)
        
        await update.message.reply_text(f"🔮 **AI SOI KÈO ({m['home']} vs {m['away']}):**\n\n{response.text}")
    except Exception:
        await update.message.reply_text("❌ Lỗi! HD: `/predict 1` (Số 1 là thứ tự trong Board)")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    matches = [m for m in state["boards"].get(today,[]) if m.get("notified")]
    if not matches: return await update.message.reply_text("📭 Chưa có trận nào kết thúc hôm nay.")
    res = "📜 **HISTORY BOARD:**\n"
    for i, m in enumerate(matches): res += f"{i+1}. ✅ {m['home']} {m.get('score', '')} {m['away']}\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def mnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        idx, note = int(context.args[0])-1, " ".join(context.args[1:])
        state["boards"][today][idx]["note"] = note
        save_data()
        await update.message.reply_text(f"✅ Đã ghi chú cho trận số {idx+1}")
    except Exception: await update.message.reply_text("❌ HD: `/mnote 1 Nội dung`")


# ===== 7. MONITOR TỰ ĐỘNG (NHẮC CHÍNH XÁC & TỐI ƯU API) =====
async def monitor(context: ContextTypes.DEFAULT_TYPE):
    global last_api_check
    now = datetime.now(VN_TZ)
    today = now.strftime("%Y-%m-%d")
    if not state["chat_id"]: return
    
    # Báo công việc trước 15p
    for t in state["tasks"]:
        if not t.get("reminded") and t.get("date") == today:
            try:
                target = datetime.strptime(t["time"], "%H:%M").replace(year=now.year, month=now.month, day=now.day, tzinfo=VN_TZ)
                if now >= (target - timedelta(minutes=15)) and now < target:
                    await context.bot.send_message(state["chat_id"], f"⏰ **SẮP ĐẾN GIỜ (15p nữa):** {t['content']}")
                    t["reminded"] = True
                    save_data()
            except ValueError: continue

    # Báo bóng đá trước 15p
    if today in state["boards"]:
        for m in state["boards"][today]:
            if not m.get("reminded_15m") and "timestamp" in m:
                target = datetime.fromtimestamp(m["timestamp"], VN_TZ)
                if now >= (target - timedelta(minutes=15)) and now < target:
                    await context.bot.send_message(
                        state["chat_id"], f"⏰ **SẮP ĐÁ (15p nữa):** ⚽ {m['home']} vs {m['away']} lúc {m['time']}"
                    )
                    m["reminded_15m"] = True
                    save_data()

    # Cập nhật kết quả (Mỗi 10 phút)
    if now.timestamp() - last_api_check >= 600:
        last_api_check = now.timestamp()
        if today in state["boards"]:
            unnotified_matches =[m for m in state["boards"][today] if not m.get("notified")]
            if unnotified_matches:
                match_ids = "-".join(str(m['id']) for m in unnotified_matches)
                try:
                    res = await client.get(f"https://v3.football.api-sports.io/fixtures?ids={match_ids}")
                    for f in res.json().get("response",[]):
                        if f["fixture"]["status"]["short"] in ["FT", "AET", "PEN"]:
                            for m in state["boards"][today]:
                                if m["id"] == f["fixture"]["id"] and not m.get("notified"):
                                    home_g = f['goals']['home'] if f['goals']['home'] is not None else 0
                                    away_g = f['goals']['away'] if f['goals']['away'] is not None else 0
                                    m["score"] = f"{home_g}-{away_g}"
                                    await context.bot.send_message(state["chat_id"], f"🏁 **KẾT THÚC:** {m['home']} {m['score']} {m['away']}")
                                    m["notified"] = True
                                    save_data()
                except Exception as e: logging.error(f"Lỗi Monitor: {e}")

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
    
    # 2 Lệnh mới khai sinh
    app.add_handler(CommandHandler("detail", detail_cmd))
    app.add_handler(CommandHandler("predict", predict_cmd))
    
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("mnote", mnote_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    if app.job_queue: 
        app.job_queue.run_repeating(monitor, interval=60, first=10)
        
    print("🚀 SUPREME AI COMMANDER V3.0 ĐÃ SẴN SÀNG PHỤC VỤ!")
    app.run_polling()

if __name__ == "__main__": 
    main()
