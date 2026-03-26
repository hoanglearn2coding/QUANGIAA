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
        full_query = (
            f"[NGỮ CẢNH ({now_str})]\n👤 Hồ sơ:\n{profile_info}\n\n📋 Việc:\n{tasks_info}\n\n"
            f"⚽🏀 Thể thao:\n{board_info}\n\n💬 ÔNG CHỦ NÓI: {query}"
        )
        response = await asyncio.to_thread(chat.send_message, full_query)
        await update.message.reply_text(f"🤖 **Cố Vấn AI:**\n{response.text}")
    except Exception: await update.message.reply_text("❌ Xin lỗi Ông chủ, hệ thống nơ-ron đang quá tải.")

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        now = datetime.now(VN_TZ)
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        today_tasks =[t for t in state["tasks"] if t["date"] == today_str]
        tasks_text = "Không có." if not today_tasks else "\n".join([f"- {t['time']}: {t['content']}[{'Xong' if t.get('reminded') else 'Chưa'}]" for t in today_tasks])

        today_boards = state["boards"].get(today_str,[])
        boards_text = "Không có." if not today_boards else "\n".join([f"- {m.get('icon','⚽')} {m['home']} {m.get('score','?-?')} {m['away']} [{'Xong' if m.get('notified') else 'Chưa/Đang đá'}] (Note: {m.get('note', 'Không')})" for m in today_boards]
        )

        lines =[]
        try:
            res_f = await client.get(f"https://v3.football.api-sports.io/fixtures?date={tomorrow_str}")
            for m in res_f.json().get("response",[])[:5]: lines.append(f"⚽ {parse_match_time(m['fixture']['date'])[0]}: {m['teams']['home']['name']} vs {m['teams']['away']['name']}")
        except: pass
        try:
            res_b = await client.get(f"https://v1.basketball.api-sports.io/games?date={tomorrow_str}")
            for m in res_b.json().get("response",[])[:5]: lines.append(f"🏀 {parse_match_time(m['date'])[0]}: {m['teams']['home']['name']} vs {m['teams']['away']['name']}")
        except: pass
        tomorrow_matches = "\n".join(lines) if lines else "Không có dữ liệu."

        prompt = (
            f"Viết Báo Cáo Tổng Kết Ngày.\n1. Task\n2. Kèo thể thao\n3. Trận đáng xem ngày mai\n4. Nhận xét thông minh.\n\n"
            f"Hồ sơ: {' '.join(state.get('profile',[]))}\nTasks:\n{tasks_text}\nBoard:\n{boards_text}\nNgày mai:\n{tomorrow_matches}"
        )

        chat_id = update.effective_chat.id
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
        response = await asyncio.to_thread(chat_sessions[chat_id].send_message, prompt)
        await update.message.reply_text(f"📑 **BÁO CÁO NGÀY {today_str}**\n\n{response.text}")
    except Exception: await update.message.reply_text("❌ Lỗi trích xuất báo cáo.")

# ===== 5. THỂ THAO TỔNG HỢP (BÓNG ĐÁ + BÓNG RỔ) =====
async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    kb =[]
    try:
        res_f = await client.get(f"https://v3.football.api-sports.io/fixtures?date={today}")
        for m in res_f.json().get("response",[])[:10]:
            kb.append([InlineKeyboardButton(f"⚽[{parse_match_time(m['fixture']['date'])[0]}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_f_{m['fixture']['id']}")])
    except: pass
    try:
        res_b = await client.get(f"https://v1.basketball.api-sports.io/games?date={today}")
        for m in res_b.json().get("response",[])[:10]:
            kb.append([InlineKeyboardButton(f"🏀 [{parse_match_time(m['date'])[0]}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_b_{m['id']}")])
    except: pass
    
    if not kb: return await update.message.reply_text("📭 Không có sự kiện thể thao nào hôm nay.")
    await update.message.reply_text("📅 **LỊCH THỂ THAO HÔM NAY:**", reply_markup=InlineKeyboardMarkup(kb))

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).lower()
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    kb =[]
    try:
        res_f = await client.get(f"https://v3.football.api-sports.io/fixtures?date={today}")
        for m in res_f.json().get("response",[]):
            if query in m['teams']['home']['name'].lower() or query in m['teams']['away']['name'].lower():
                kb.append([InlineKeyboardButton(f"⚽ [{parse_match_time(m['fixture']['date'])[0]}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_f_{m['fixture']['id']}")])
        res_b = await client.get(f"https://v1.basketball.api-sports.io/games?date={today}")
        for m in res_b.json().get("response", []):
            if query in m['teams']['home']['name'].lower() or query in m['teams']['away']['name'].lower():
                kb.append([InlineKeyboardButton(f"🏀 [{parse_match_time(m['date'])[0]}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_b_{m['id']}")])
    except: pass
    if not kb: return await update.message.reply_text("ℹ️ Không tìm thấy.")
    await update.message.reply_text(f"🔍 Kết quả cho '{query}':", reply_markup=InlineKeyboardMarkup(kb[:15]))

async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("❌ HD: `/time 20` hoặc `/time 20:30`")
    target_time = context.args[0]
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    kb =[]
    try:
        res_f = await client.get(f"https://v3.football.api-sports.io/fixtures?date={today}")
        for m in res_f.json().get("response",[]):
            t_str, _ = parse_match_time(m['fixture']['date'])
            if (len(target_time) <= 2 and t_str.startswith(f"{target_time}:")) or (t_str == target_time):
                kb.append([InlineKeyboardButton(f"⚽ [{t_str}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_f_{m['fixture']['id']}")])
        res_b = await client.get(f"https://v1.basketball.api-sports.io/games?date={today}")
        for m in res_b.json().get("response",[]):
            t_str, _ = parse_match_time(m['date'])
            if (len(target_time) <= 2 and t_str.startswith(f"{target_time}:")) or (t_str == target_time):
                kb.append([InlineKeyboardButton(f"🏀 [{t_str}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_b_{m['id']}")])
    except: pass
    if not kb: await update.message.reply_text(f"ℹ️ Không có trận khung giờ `{target_time}`.")
    else: await update.message.reply_text(f"⏰ **KẾT QUẢ KHUNG GIỜ {target_time}:**", reply_markup=InlineKeyboardMarkup(kb[:15]))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")

    if data.startswith("pk_"):
        parts = data.split("_")
        sport = parts[1]
        fid = int(parts[2])
        state["boards"].setdefault(today,[])
        if any(m['id'] == fid and m.get('sport','f') == sport for m in state["boards"][today]): 
            return await query.answer("Đã có trong Board!", show_alert=True)
        
        try:
            if sport == 'f':
                res = await client.get(f"https://v3.football.api-sports.io/fixtures?id={fid}")
                m_data = res.json()["response"][0]
                t_str, ts = parse_match_time(m_data['fixture']['date'])
                icon = "⚽"
            else:
                res = await client.get(f"https://v1.basketball.api-sports.io/games?id={fid}")
                m_data = res.json()["response"][0]
                t_str, ts = parse_match_time(m_data['date'])
                icon = "🏀"

            state["boards"][today].append({
                "id": fid, "sport": sport, "icon": icon,
                "home": m_data["teams"]["home"]["name"], "away": m_data["teams"]["away"]["name"], 
                "time": t_str, "timestamp": ts, "league": m_data['league']['name'], 
                "home_id": m_data['teams']['home']['id'], "away_id": m_data['teams']['away']['id'],
                "status_icon": "⏳", "note": "", "notified": False, "reminded_15m": False, "score": ""
            })
            save_data()
            idx = len(state["boards"][today])
            kb = [[InlineKeyboardButton("📝 Thêm ghi chú", callback_data=f"asknote_m_{idx}")]]
            await query.edit_message_text(f"✅ Đã pick: {icon} {m_data['teams']['home']['name']} vs {m_data['teams']['away']['name']}", reply_markup=InlineKeyboardMarkup(kb))
        except Exception: await query.answer("Lỗi thêm trận!", show_alert=True)

    elif data.startswith("asknote_"):
        _, kind, idx = data.split("_")
        await query.message.reply_text(f"👉 Gõ lệnh:\n`/tnote {idx}[Nội dung]`" if kind == "t" else f"👉 Gõ lệnh:\n`/mnote {idx} [Nội dung]`", parse_mode="Markdown")
        await query.answer()

    elif data.startswith("ai_predict_"):
        idx = int(data.split("_")[2])
        if today not in state["boards"] or idx >= len(state["boards"][today]): return await query.answer("Lỗi dữ liệu!", show_alert=True)
        m = state["boards"][today][idx]
        await query.answer("Đang phân tích...", show_alert=False)
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action="typing")
        
        league, home_last, away_last = await get_match_context(m)
        prompt = (f"Phân tích trận {m.get('icon','⚽')}: {m['home']} vs {m['away']} ({league}). Phong độ {m['home']}:\n{home_last}\nPhong độ {m['away']}:\n{away_last}\n"
                  f"HỒ SƠ: {' '.join(state.get('profile',[]))}\n"
                  "Hãy phản biện: 1. Cơ hội 2. Rủi ro 3. Chốt kèo thông minh.")
        
        chat_id = query.message.chat_id
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
        response = await asyncio.to_thread(chat_sessions[chat_id].send_message, prompt)
        await query.message.reply_text(f"🔮 **AI SOI KÈO ({m.get('icon','⚽')} {m['home']} vs {m['away']}):**\n\n{response.text}")

# ===== 6. QUẢN LÝ BẢNG & CHI TIẾT =====
async def board_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    matches = [m for m in state["boards"].get(today,[]) if not m.get("notified")]
    if not matches: return await update.message.reply_text("📭 Board trống.")
    res = f"📊 **BOARD {today}:**\n"
    for i, m in enumerate(matches):
        res += f"{i+1}. ⏳[{m.get('time', 'N/A')}] *{m.get('icon','⚽')} {m['home']} vs {m['away']}*\n"
        if m.get("note"): res += f"   └ 📝: _{m['note']}_\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def detail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        idx = int(context.args[0]) - 1
        m = state["boards"][today][idx]
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        league, home_last, away_last = await get_match_context(m)
        res = (f"🏆 **GIẢI:** {league}\n{m.get('icon','⚽')} **TRẬN:** {m['home']} vs {m['away']}\n⏰ **GIỜ:** {m.get('time', 'N/A')}\n\n"
               f"🛡️ **LỊCH SỬ ({m['home']}):**\n{home_last}\n\n⚔️ **LỊCH SỬ ({m['away']}):**\n{away_last}")
        kb = [[InlineKeyboardButton("🔮 AI Soi Kèo", callback_data=f"ai_predict_{idx}")]]
        await update.message.reply_text(res, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except Exception: await update.message.reply_text("❌ Lỗi! HD: `/detail 1`")

async def predict_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        idx = int(context.args[0]) - 1
        m = state["boards"][today][idx]
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        league, home_last, away_last = await get_match_context(m)
        prompt = (f"Phân tích trận {m.get('icon','⚽')}: {m['home']} vs {m['away']} ({league}). Phong độ {m['home']}:\n{home_last}\nPhong độ {m['away']}:\n{away_last}\n"
                  f"HỒ SƠ: {' '.join(state.get('profile',[]))}\n"
                  "Hãy phản biện: 1. Cơ hội 2. Rủi ro 3. Chốt kèo thông minh.")
        chat_id = update.effective_chat.id
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
        response = await asyncio.to_thread(chat_sessions[chat_id].send_message, prompt)
        await update.message.reply_text(f"🔮 **AI SOI KÈO ({m.get('icon','⚽')} {m['home']} vs {m['away']}):**\n\n{response.text}")
    except Exception: await update.message.reply_text("❌ Lỗi! HD: `/predict 1`")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    matches =[m for m in state["boards"].get(today,[]) if m.get("notified")]
    if not matches: return await update.message.reply_text("📭 Chưa có trận nào kết thúc hôm nay.")
    res = "📜 **HISTORY:**\n"
    for i, m in enumerate(matches): res += f"{i+1}. ✅ {m.get('icon','⚽')} {m['home']} {m.get('score', '')} {m['away']}\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def mnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        idx, note = int(context.args[0])-1, " ".join(context.args[1:])
        state["boards"][today][idx]["note"] = note
        save_data()
        await update.message.reply_text(f"✅ Đã ghi chú trận số {idx+1}")
    except Exception: await update.message.reply_text("❌ HD: `/mnote 1 Nội dung`")

# ===== TASK (GIỮ NGUYÊN) =====
async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        time_str, content = context.args[0], " ".join(context.args[1:])
        state["tasks"].append({"time": time_str, "content": content, "reminded": False, "note": "", "date": datetime.now(VN_TZ).strftime("%Y-%m-%d")})
        save_data()
        idx = len(state["tasks"])
        kb = [[InlineKeyboardButton("📝 Thêm ghi chú", callback_data=f"asknote_t_{idx}")]]
        await update.message.reply_text(f"➕ Đã thêm: *{content}*", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except Exception: await update.message.reply_text("❌ HD: `/add 08:00 Việc cần làm`")

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

# ===== 7. MONITOR TỰ ĐỘNG (GỘP API) =====
async def monitor(context: ContextTypes.DEFAULT_TYPE):
    global last_api_check
    now = datetime.now(VN_TZ)
    today = now.strftime("%Y-%m-%d")
    if not state.get("chat_id"): return
    
    for t in state["tasks"]:
        if not t.get("reminded") and t.get("date") == today:
            try:
                target = datetime.strptime(t["time"], "%H:%M").replace(year=now.year, month=now.month, day=now.day, tzinfo=VN_TZ)
                if now >= (target - timedelta(minutes=15)) and now < target:
                    await context.bot.send_message(state["chat_id"], f"⏰ **NHẮC VIỆC (15p nữa):** {t['content']}")
                    t["reminded"] = True
                    save_data()
            except ValueError: continue

    if today in state["boards"]:
        unnotified = [m for m in state["boards"][today] if not m.get("notified")]
        
        # Nhắc trước 15 phút (Cả 2 môn)
        for m in unnotified:
            if not m.get("reminded_15m") and "timestamp" in m:
                target = datetime.fromtimestamp(m["timestamp"], VN_TZ)
                if now >= (target - timedelta(minutes=15)) and now < target:
                    await context.bot.send_message(state["chat_id"], f"⏰ **SẮP ĐÁ (15p nữa):** {m.get('icon','⚽')} {m['home']} vs {m['away']} lúc {m['time']}")
                    m["reminded_15m"] = True
                    save_data()

        # Update kết quả Tối ưu (10 phút 1 lần)
        if now.timestamp() - last_api_check >= 600 and unnotified:
            last_api_check = now.timestamp()
            has_f = any(m.get("sport", "f") == "f" for m in unnotified)
            has_b = any(m.get("sport", "f") == "b" for m in unnotified)

            if has_f:
                try:
                    res = await client.get(f"https://v3.football.api-sports.io/fixtures?date={today}")
                    f_map = {f["fixture"]["id"]: f for f in res.json().get("response",[])}
                    for m in state["boards"][today]:
                        if m.get("sport", "f") == "f" and not m.get("notified") and m["id"] in f_map:
                            f_data = f_map[m["id"]]
                            if f_data["fixture"]["status"]["short"] in["FT", "AET", "PEN"]:
                                hg, ag = f_data['goals']['home'], f_data['goals']['away']
                                m["score"] = f"{hg if hg is not None else 0}-{ag if ag is not None else 0}"
                                await context.bot.send_message(state["chat_id"], f"🏁 **KẾT THÚC:** ⚽ {m['home']} {m['score']} {m['away']}")
                                m["notified"] = True
                                save_data()
                except Exception as e: logging.error(f"Lỗi Monitor F: {e}")

            if has_b:
                try:
                    res = await client.get(f"https://v1.basketball.api-sports.io/games?date={today}")
                    b_map = {b["id"]: b for b in res.json().get("response",[])}
                    for m in state["boards"][today]:
                        if m.get("sport", "f") == "b" and not m.get("notified") and m["id"] in b_map:
                            b_data = b_map[m["id"]]
                            if b_data["status"]["short"] in ["FT", "AOT"]:
                                hg, ag = b_data['scores']['home']['total'], b_data['scores']['away']['total']
                                m["score"] = f"{hg if hg is not None else 0}-{ag if ag is not None else 0}"
                                await context.bot.send_message(state["chat_id"], f"🏁 **KẾT THÚC:** 🏀 {m['home']} {m['score']} {m['away']}")
                                m["notified"] = True
                                save_data()
                except Exception as e: logging.error(f"Lỗi Monitor B: {e}")

# ===== 8. MAIN =====
def main():
    load_data()
    app = ApplicationBuilder().token(BOT_TOKEN).defaults(Defaults(tzinfo=VN_TZ)).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("learn", learn_cmd)) 
    app.add_handler(CommandHandler("profile", profile_cmd)) 
    app.add_handler(CommandHandler("summary", summary_cmd)) 
    app.add_handler(CommandHandler("add", add_task))
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("tnote", tnote_cmd))
    app.add_handler(CommandHandler("matches", matches_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("time", time_cmd)) 
    app.add_handler(CommandHandler("board", board_cmd))
    app.add_handler(CommandHandler("detail", detail_cmd))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("mnote", mnote_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_chat_handler))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    if app.job_queue: app.job_queue.run_repeating(monitor, interval=60, first=10)
    print("🚀 SUPREME AI COMMANDER V6.0 (FOOTBALL + BASKETBALL) ĐÃ SẴN SÀNG!")
    app.run_polling()

if __name__ == "__main__": main()
