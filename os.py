import os
import json
import logging
import httpx
import pytz
import asyncio
import google.generativeai as genai
from datetime import datetime, timedelta, time
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
TZ_PARAM = "&timezone=Asia/Ho_Chi_Minh" 

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

DATA_DIR = "/data"
if not os.path.exists(DATA_DIR):
    try: os.makedirs(DATA_DIR, exist_ok=True)
    except Exception: DATA_DIR = "." 
DATA_FILE = os.path.join(DATA_DIR, "supreme_v11_data.json")

# --- Cấu hình Gemini AI ---
genai.configure(api_key=GENAI_API_KEY)
system_prompt = (
    "Bạn là một Siêu Quản Gia AI và Cố Vấn Thể Thao Tối Cao của Ông chủ. Luôn xưng 'Dạ', 'Ông chủ', 'tôi' hoặc 'em'.\n"
    "NGUYÊN TẮC TƯ DUY:\n"
    "1. CHUYÊN GIA BÓNG ĐÁ (⚽): Hiểu rõ phong độ, chiến thuật, chấn thương.\n"
    "2. CHUYÊN GIA BÓNG RỔ & NBA (🏀/🌟): Nắm vững kiến thức sâu về NBA, cầu thủ, chiến thuật, matchup tay đôi.\n"
    "3. NHẬN XÉT SÂU SẮC: Phân tích lý do tại sao thắng/thua, yếu tố con người và cảnh báo rủi ro dựa trên [Hồ sơ Ông chủ].\n"
    "4. TỔNG KẾT: Luôn đưa ra chốt kèo rõ ràng hoặc kết luận sắc bén. Trả lời tuyệt đối không chứa quá nhiều ký tự đặc biệt làm rối mắt."
)
ai_model = genai.GenerativeModel('gemini-2.5-flash', system_instruction=system_prompt, generation_config=genai.types.GenerationConfig(temperature=0.55))

# ===== 2. QUẢN LÝ DỮ LIỆU & HÀM BỔ TRỢ =====
state = {"tasks":[], "boards": {}, "profile":[], "chat_id": None}
chat_sessions = {} 
last_api_check = 0 
client = httpx.AsyncClient(headers={"x-apisports-key": API_KEY if API_KEY else ""}, timeout=20)

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                state.update(json.load(f))
                if "profile" not in state: state["profile"] =[]
        except Exception as e: logging.error(f"Lỗi đọc file: {e}")

# FIX CỰC MẠNH: Xử lý giờ chuẩn VN chính xác 100%
def parse_match_time(date_str):
    try:
        clean_str = date_str[:19]
        dt = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
        dt = VN_TZ.localize(dt)
        return dt.strftime("%H:%M"), dt.timestamp()
    except Exception as e: 
        logging.error(f"Lỗi parse giờ: {e}")
        return "00:00", 0

def is_finished(m, sport):
    try:
        if sport == 'f': return m['fixture']['status']['short'] in['FT', 'AET', 'PEN', 'CANC', 'PST', 'ABD', 'AWD', 'WO']
        elif sport == 'n': return str(m['status']['short']) in ['3', 'FT', 'AOT', 'CANC', 'PST']
        elif sport == 'b': return str(m['status']['short']) in['FT', 'AOT', 'CANC', 'POST', 'ABD']
    except Exception: return False
    return False

def parse_date_and_query(args):
    now = datetime.now(VN_TZ)
    target_date = now.strftime("%Y-%m-%d")
    query_parts =[]
    for arg in args:
        if "/" in arg and any(c.isdigit() for c in arg):
            try:
                parts = arg.split("/")
                d, m = int(parts[0]), int(parts[1])
                y = int(parts[2]) if len(parts) > 2 else now.year
                target_date = f"{y}-{m:02d}-{d:02d}"
            except Exception: query_parts.append(arg)
        else: query_parts.append(arg)
    return target_date, " ".join(query_parts).lower()

def get_flattened_board():
    matches =[]
    for date_key, daily_matches in state["boards"].items():
        matches.extend([m for m in daily_matches if not m.get("notified")])
    matches.sort(key=lambda x: x.get("timestamp", 0))
    return matches

# FIX CỰC MẠNH: Tăng cường lấy lịch sử Bóng Đá, thêm H2H cho Bóng rổ/NBA
async def get_match_context(m):
    sport = m.get("sport", "f")
    home_id, away_id = m.get("home_id"), m.get("away_id")
    league = m.get("league", "Không rõ giải")
    
    if not home_id or not away_id:
        return league, "   + Thiếu ID đội bóng.", "   + Thiếu ID đội bóng."

    if sport == "f":
        try:
            h_task = client.get(f"https://v3.football.api-sports.io/fixtures?team={home_id}&last=3")
            a_task = client.get(f"https://v3.football.api-sports.io/fixtures?team={away_id}&last=3")
            res_h, res_a = await asyncio.gather(h_task, a_task)
            
            def fmt_f(res_data):
                try:
                    data = res_data.json().get("response",[])
                    if not data: return "   + Không có dữ liệu lịch sử gần đây."
                    lines =[]
                    for f in data:
                        date_str = f['fixture']['date'][:10]
                        h_team, a_team = f['teams']['home']['name'], f['teams']['away']['name']
                        hg, ag = f['goals']['home'], f['goals']['away']
                        score = f"{hg}-{ag}" if hg is not None and ag is not None else "?-?"
                        lines.append(f"   + {date_str}: {h_team} {score} {a_team}")
                    return "\n".join(lines)
                except: return "   + Lỗi trích xuất dữ liệu."
            
            return league, fmt_f(res_h), fmt_f(res_a)
        except Exception: 
            return league, "   + Lỗi kết nối API lịch sử.", "   + Lỗi kết nối API lịch sử."
            
    elif sport in ['n', 'b']:
        try:
            endpoint = "v2.nba.api-sports.io" if sport == 'n' else "v1.basketball.api-sports.io"
            res_h2h = await client.get(f"https://{endpoint}/games?h2h={home_id}-{away_id}")
            data = res_h2h.json().get("response",[])
            if not data:
                return league, "   + Chưa có dữ liệu đối đầu (H2H).", "   + Chưa có dữ liệu đối đầu (H2H)."
            
            lines = []
            for f in data[:3]: # Lấy 3 trận đối đầu gần nhất
                date_str = f['date'][:10] if sport == 'b' else f['date']['start'][:10]
                h_team, a_team = f['teams']['home']['name'], f['teams']['away']['name']
                if sport == 'n':
                    hg = f.get('scores', {}).get('home', {}).get('points')
                    ag = f.get('scores', {}).get('away', {}).get('points')
                else:
                    hg = f.get('scores', {}).get('home', {}).get('total')
                    ag = f.get('scores', {}).get('away', {}).get('total')
                score = f"{hg}-{ag}" if hg is not None and ag is not None else "?-?"
                lines.append(f"   + {date_str}: {h_team} {score} {a_team}")
            
            h2h_str = "\n".join(lines)
            return league, f"Lịch sử ĐỐI ĐẦU (H2H):\n{h2h_str}", f"Lịch sử ĐỐI ĐẦU (H2H):\n{h2h_str}"
        except Exception:
            return league, "   + Lỗi lấy dữ liệu đối đầu.", "   + Lỗi lấy dữ liệu đối đầu."

# ===== 3. MENU START =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["chat_id"] = update.effective_chat.id
    save_data()
    menu = (
        "🤵 **AI COMMANDER V11 - STABLE AI & BIG DATA**\n\n"
        "🧠 **[ BỘ NÃO AI ]**\n"
        " ├ 💬 *Chat tự do để hỏi lịch, chiến thuật*\n"
        " ├ 📥 `/learn [Sở thích]` : Dạy AI nhớ gu\n"
        " ├ 📋 `/profile` : Xem hồ sơ\n"
        " └ 📊 `/summary` : Tổng kết ngày\n\n"
        "⚽🏀🌟 **[ TÌM KIẾM THỂ THAO ]**\n"
        " *(Hệ thống tự động lọc bỏ các trận Đã Xong/Bị hoãn)*\n"
        " ├ 📅 `/matches [Ngày]` : Lịch thi đấu chưa đá\n"
        " ├ 🔍 `/search [Tên] [Ngày]` : Tìm Giải/Đội\n"
        " ├ ⏰ `/time [Giờ] [Ngày]` : Lọc trận theo giờ\n"
        " ├ 📊 `/board` : Bảng theo dõi Tổng\n"
        " ├ 📜 `/history` : Các trận đã xong\n"
        " ├ ℹ️ `/detail [STT]` : Xem chi tiết thủ công\n"
        " └ 🔮 `/predict [STT]` : Chuyên gia soi kèo\n\n"
        "📅 **[ NHẮC VIỆC ]**\n"
        " └ ➕ `/add` | 📜 `/list` | 📝 `/tnote`"
    )
    await update.message.reply_text(menu, parse_mode="Markdown")

# ===== 4. AI INTENT & HỌC HỎI (CÓ BỌC LỖI MARKDOWN) =====
async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = " ".join(context.args)
    if not info: return await update.message.reply_text("❌ HD: `/learn Tôi chỉ đánh kèo Ngoại Hạng Anh`")
    state["profile"].append(info)
    save_data()
    await update.message.reply_text(f"✅ Đã ghi nhớ: *{info}*", parse_mode="Markdown")

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state.get("profile"): return await update.message.reply_text("📭 Hồ sơ trống.")
    res = "🧠 **HỒ SƠ CÁ NHÂN:**\n"
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
        
        profile_info = "\n".join(state.get("profile",[])) or "Chưa có."
        tasks_info = "\n".join([f"- {t['time']}: {t['content']}" for t in state["tasks"] if not t.get("reminded") or t["date"] == today_str]) or "- Rảnh"
        
        active_boards = get_flattened_board()
        board_info = "\n".join([f"- {m.get('icon','⚽')} {m['home']} vs {m['away']} (Lúc {m.get('time')} ngày {m.get('date', today_str)})" for m in active_boards]) or "- Không"

        full_query = f"[NGỮ CẢNH ({now.strftime('%A, %d/%m %H:%M')})]\nHồ sơ:\n{profile_info}\nViệc:\n{tasks_info}\nTrận đang theo dõi:\n{board_info}\n\n💬 ÔNG CHỦ HỎI: {query}"
        
        response = await asyncio.wait_for(asyncio.to_thread(chat.send_message, full_query), timeout=30.0)
        try:
            await update.message.reply_text(f"🤖 **AI:**\n{response.text}", parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(f"🤖 AI:\n{response.text}")
    except asyncio.TimeoutError:
        await update.message.reply_text("❌ AI đang quá tải, vui lòng hỏi lại sau.")
    except Exception: 
        await update.message.reply_text("❌ Hệ thống nơ-ron đang bận.")

# ===== 5. TÌM KIẾM THÔNG MINH (CÓ LỌC TRẬN ĐÃ XONG) =====
async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_date, _ = parse_date_and_query(context.args)
    kb =[]
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        res_f = await client.get(f"https://v3.football.api-sports.io/fixtures?date={target_date}{TZ_PARAM}")
        for m in res_f.json().get("response",[]):
            if not is_finished(m, 'f'):
                kb.append([InlineKeyboardButton(f"⚽[{parse_match_time(m['fixture']['date'])[0]}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_f_{m['fixture']['id']}")])
        
        res_n = await client.get(f"https://v2.nba.api-sports.io/games?date={target_date}{TZ_PARAM}")
        for m in res_n.json().get("response",[]):
            if not is_finished(m, 'n'):
                kb.append([InlineKeyboardButton(f"🌟[{parse_match_time(m['date']['start'])[0]}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_n_{m['id']}")])
            
        res_b = await client.get(f"https://v1.basketball.api-sports.io/games?date={target_date}{TZ_PARAM}")
        for m in res_b.json().get("response",[]):
            if not is_finished(m, 'b'):
                kb.append([InlineKeyboardButton(f"🏀[{parse_match_time(m['date'])[0]}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_b_{m['id']}")])
    except: pass
    
    if not kb: return await update.message.reply_text(f"📭 Không có trận nào (chưa đá) vào ngày {target_date}.")
    
    msg = f"📅 **LỊCH THỂ THAO ({target_date}) - CHỈ TRẬN CHƯA ĐÁ:**"
    if len(kb) > 90: kb, msg = kb[:90], msg + "\n*(Hiển thị 90 trận đầu tiên)*"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_date, query = parse_date_and_query(context.args)
    if not query: return await update.message.reply_text("❌ HD: `/search MU 28/3` hoặc `/search Premier League`")
    
    kb =[]
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        res_f = await client.get(f"https://v3.football.api-sports.io/fixtures?date={target_date}{TZ_PARAM}")
        for m in res_f.json().get("response",[]):
            if not is_finished(m, 'f') and (query in m['teams']['home']['name'].lower() or query in m['teams']['away']['name'].lower() or query in m['league']['name'].lower()):
                kb.append([InlineKeyboardButton(f"⚽ [{parse_match_time(m['fixture']['date'])[0]}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_f_{m['fixture']['id']}")])
        
        res_n = await client.get(f"https://v2.nba.api-sports.io/games?date={target_date}{TZ_PARAM}")
        for m in res_n.json().get("response",[]):
            if not is_finished(m, 'n') and (query in m['teams']['home']['name'].lower() or query in m['teams']['away']['name'].lower() or "nba" in query):
                kb.append([InlineKeyboardButton(f"🌟[{parse_match_time(m['date']['start'])[0]}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_n_{m['id']}")])
                
        res_b = await client.get(f"https://v1.basketball.api-sports.io/games?date={target_date}{TZ_PARAM}")
        for m in res_b.json().get("response",[]):
            if not is_finished(m, 'b') and (query in m['teams']['home']['name'].lower() or query in m['teams']['away']['name'].lower() or query in m['league']['name'].lower()):
                kb.append([InlineKeyboardButton(f"🏀[{parse_match_time(m['date'])[0]}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_b_{m['id']}")])
    except: pass
    
    if not kb: return await update.message.reply_text(f"ℹ️ Không tìm thấy '{query.upper()}' (chưa đá) trong ngày {target_date}.")
    
    msg = f"🔍 **KẾT QUẢ CHO '{query.upper()}' ({target_date}):**"
    if len(kb) > 90: kb, msg = kb[:90], msg + "\n*(Hiển thị 90 kết quả đầu)*"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_date, target_time = parse_date_and_query(context.args)
    if not target_time: return await update.message.reply_text("❌ HD: `/time 20:30 28/3`")
    
    kb =[]
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        res_f = await client.get(f"https://v3.football.api-sports.io/fixtures?date={target_date}{TZ_PARAM}")
        for m in res_f.json().get("response",[]):
            if not is_finished(m, 'f'):
                t_str, _ = parse_match_time(m['fixture']['date'])
                if (len(target_time) <= 2 and t_str.startswith(f"{target_time}:")) or (t_str == target_time):
                    kb.append([InlineKeyboardButton(f"⚽[{t_str}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_f_{m['fixture']['id']}")])
                
        res_n = await client.get(f"https://v2.nba.api-sports.io/games?date={target_date}{TZ_PARAM}")
        for m in res_n.json().get("response",[]):
            if not is_finished(m, 'n'):
                t_str, _ = parse_match_time(m['date']['start'])
                if (len(target_time) <= 2 and t_str.startswith(f"{target_time}:")) or (t_str == target_time):
                    kb.append([InlineKeyboardButton(f"🌟[{t_str}] {m['teams']['home']['name']} vs {m['teams']['away']['name']}", callback_data=f"pk_n_{m['id']}")])
    except: pass
    
    if not kb: return await update.message.reply_text(f"ℹ️ Không có trận (chưa đá) khung giờ `{target_time}` ngày {target_date}.")
    msg = f"⏰ **KẾT QUẢ KHUNG GIỜ {target_time} ({target_date}):**"
    if len(kb) > 90: kb, msg = kb[:90], msg + "\n*(Hiển thị 90 kết quả đầu)*"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

# ===== 6. TỰ ĐỘNG BÁO CÁO KHI PICK & NÚT AI (CÓ CHỐNG CRASH) =====
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("pk_"):
        parts = data.split("_")
        sport, fid = parts[1], int(parts[2])
        
        try:
            if sport == 'f':
                res = await client.get(f"https://v3.football.api-sports.io/fixtures?id={fid}{TZ_PARAM}")
                m_data = res.json()["response"][0]
                t_str, ts = parse_match_time(m_data['fixture']['date'])
                icon, lg_name = "⚽", m_data['league']['name']
            elif sport == 'n':
                res = await client.get(f"https://v2.nba.api-sports.io/games?id={fid}{TZ_PARAM}")
                m_data = res.json()["response"][0]
                t_str, ts = parse_match_time(m_data['date']['start'])
                icon, lg_name = "🌟", "NBA"
            else:
                res = await client.get(f"https://v1.basketball.api-sports.io/games?id={fid}{TZ_PARAM}")
                m_data = res.json()["response"][0]
                t_str, ts = parse_match_time(m_data['date'])
                icon, lg_name = "🏀", m_data['league']['name']

            actual_date = datetime.fromtimestamp(ts, VN_TZ).strftime("%Y-%m-%d")
            state["boards"].setdefault(actual_date, [])
            
            if any(m['id'] == fid and m.get('sport','f') == sport for m in state["boards"][actual_date]): 
                return await query.answer("Trận này đã có sẵn trong Board!", show_alert=True)

            new_match = {
                "id": fid, "sport": sport, "icon": icon, "date": actual_date,
                "home": m_data["teams"]["home"]["name"], "away": m_data["teams"]["away"]["name"], 
                "time": t_str, "timestamp": ts, "league": lg_name, 
                "home_id": m_data['teams']['home']['id'], "away_id": m_data['teams']['away']['id'],
                "status_icon": "⏳", "note": "", "notified": False, "reminded_15m": False, "score": ""
            }
            state["boards"][actual_date].append(new_match)
            save_data()
            
            await query.answer(f"✅ Đã thêm: {new_match['home']} vs {new_match['away']}", show_alert=False)
            
            await context.bot.send_chat_action(chat_id=query.message.chat_id, action="typing")
            league, home_last, away_last = await get_match_context(new_match)
            
            active_boards = get_flattened_board()
            idx = len(active_boards) - 1 
            
            msg = (f"✅ **ĐÃ THÊM VÀO BOARD:** {icon} {new_match['home']} vs {new_match['away']} (Lúc {t_str} ngày {actual_date})\n"
                   f"🏆 **GIẢI ĐẤU:** {league}\n\n"
                   f"🛡️ **THÔNG TIN ({new_match['home']}):**\n{home_last}\n\n"
                   f"⚔️ **THÔNG TIN ({new_match['away']}):**\n{away_last}")
            
            kb = [[InlineKeyboardButton("📝 Thêm ghi chú ngay", callback_data=f"asknote_m_{idx}")],[InlineKeyboardButton("🔮 AI Phân Tích & Soi Kèo", callback_data=f"ai_predict_{idx}")]
            ]
            
            await context.bot.send_message(query.message.chat_id, msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        except Exception as e: 
            logging.error(f"Lỗi thêm trận: {e}")
            await query.answer("Lỗi thêm trận!", show_alert=True)

    elif data.startswith("asknote_"):
        _, kind, idx = data.split("_")
        await query.message.reply_text(f"👉 Gõ lệnh:\n`/tnote {idx}[Nội dung]`" if kind == "t" else f"👉 Gõ lệnh:\n`/mnote {idx} [Nội dung]`", parse_mode="Markdown")
        await query.answer()

    # FIX CỰC MẠNH: NÚT SOI KÈO AI ĐƯỢC CHỐNG LỖI VÀ TIMEOUT
    elif data.startswith("ai_predict_"):
        try:
            idx = int(data.split("_")[2])
            active_boards = get_flattened_board()
            if idx >= len(active_boards): 
                return await query.answer("Lỗi dữ liệu trận đã bị xóa!", show_alert=True)
            
            m = active_boards[idx]
            await query.answer("AI đang soi kèo... Vui lòng đợi chút!", show_alert=False)
            await context.bot.send_chat_action(chat_id=query.message.chat_id, action="typing")
            
            league, home_last, away_last = await get_match_context(m)
            prompt = (f"Phân tích trận {m.get('icon','⚽')}: {m['home']} vs {m['away']} ({league}).\nThông tin ({m['home']}):\n{home_last}\nThông tin ({m['away']}):\n{away_last}\n"
                      f"HỒ SƠ: {' '.join(state.get('profile',[]))}\n"
                      "PHẢN BIỆN CHUYÊN SÂU: Hãy mang số liệu, phong độ, matchup tay đôi ra để mổ xẻ. Chốt kèo thông minh. Cấm lạm dụng ký tự * hoặc _ làm lỗi Markdown.")
            
            chat_id = query.message.chat_id
            if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
            
            # Chống treo Bot: Timeout tối đa 30 giây
            response = await asyncio.wait_for(asyncio.to_thread(chat_sessions[chat_id].send_message, prompt), timeout=30.0)
            
            # Chống crash do Markdown Telegram:
            try:
                await context.bot.send_message(query.message.chat_id, f"🔮 **AI SOI KÈO ({m.get('icon','⚽')} {m['home']} vs {m['away']}):**\n\n{response.text}", parse_mode="Markdown")
            except Exception:
                await context.bot.send_message(query.message.chat_id, f"🔮 AI SOI KÈO ({m.get('icon','⚽')} {m['home']} vs {m['away']}):\n\n{response.text}")

        except asyncio.TimeoutError:
            await context.bot.send_message(query.message.chat_id, "❌ Lỗi: AI đang bị nghẽn mạng, Ông chủ vui lòng bấm Soi Kèo lại nhé!")
        except Exception as e:
            logging.error(f"Lỗi AI Predict Callback: {e}")
            await context.bot.send_message(query.message.chat_id, "❌ Lỗi hệ thống AI (API Key hết hạn hoặc sai cấu hình).")

# ===== 7. QUẢN LÝ BẢNG TỔNG (GLOBAL BOARD) =====
async def board_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_boards = get_flattened_board()
    if not active_boards: return await update.message.reply_text("📭 Board trống.")
    
    res = f"📊 **BẢNG THEO DÕI TỔNG:**\n"
    for i, m in enumerate(active_boards):
        res += f"{i+1}. ⏳[{m.get('time')} - {m.get('date')[5:]}] *{m.get('icon','⚽')} {m['home']} vs {m['away']}*\n"
        if m.get("note"): res += f"   └ 📝: _{m['note']}_\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def detail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(context.args[0]) - 1
        active_boards = get_flattened_board()
        m = active_boards[idx]
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        league, home_last, away_last = await get_match_context(m)
        res = (f"🏆 **GIẢI:** {league}\n{m.get('icon','⚽')} **TRẬN:** {m['home']} vs {m['away']}\n⏰ **GIỜ:** {m.get('time')} ({m.get('date')})\n\n"
               f"🛡️ **THÔNG TIN ({m['home']}):**\n{home_last}\n\n⚔️ **THÔNG TIN ({m['away']}):**\n{away_last}")
        kb = [[InlineKeyboardButton("🔮 AI Phân Tích & Soi Kèo", callback_data=f"ai_predict_{idx}")]]
        await update.message.reply_text(res, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except Exception: await update.message.reply_text("❌ Lỗi! HD: `/detail 1`")

# FIX CỰC MẠNH LỆNH /predict (Trực tiếp nhả kết quả thay vì gọi lại detail)
async def predict_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(context.args[0]) - 1
        active_boards = get_flattened_board()
        if idx >= len(active_boards) or idx < 0:
            return await update.message.reply_text("❌ Lỗi: Không tìm thấy trận đấu ở số thứ tự này!")
            
        m = active_boards[idx]
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        
        league, home_last, away_last = await get_match_context(m)
        prompt = (f"Phân tích trận {m.get('icon','⚽')}: {m['home']} vs {m['away']} ({league}).\nLịch sử {m['home']}:\n{home_last}\nLịch sử {m['away']}:\n{away_last}\n"
                  f"HỒ SƠ: {' '.join(state.get('profile',[]))}\n"
                  "PHẢN BIỆN CHUYÊN SÂU: Nếu bóng đá, đánh giá đội hình, chiến thuật. Nếu bóng rổ/NBA, hãy mang số liệu, phong độ, matchup tay đôi ra để mổ xẻ. Chốt kèo thông minh. Tránh lạm dụng Markdown.")
        
        chat_id = update.effective_chat.id
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
        
        response = await asyncio.wait_for(asyncio.to_thread(chat_sessions[chat_id].send_message, prompt), timeout=30.0)
        
        try:
            await update.message.reply_text(f"🔮 **AI SOI KÈO ({m.get('icon','⚽')} {m['home']} vs {m['away']}):**\n\n{response.text}", parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(f"🔮 AI SOI KÈO ({m.get('icon','⚽')} {m['home']} vs {m['away']}):\n\n{response.text}")
            
    except asyncio.TimeoutError:
        await update.message.reply_text("❌ Lỗi: AI đang quá tải, vui lòng thử lại sau.")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi! HD: `/predict 1`\nChi tiết: {e}")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matches =[]
    for daily_matches in state["boards"].values():
        matches.extend([m for m in daily_matches if m.get("notified")])
    matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True) 
    
    if not matches: return await update.message.reply_text("📭 Chưa có trận nào kết thúc.")
    res = "📜 **HISTORY BOARD (Đã Xong):**\n"
    for i, m in enumerate(matches[:30]): 
        res += f"{i+1}. ✅ {m.get('icon','⚽')} {m['home']} {m.get('score', '')} {m['away']} ({m.get('date')[5:]})\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def mnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx, note = int(context.args[0])-1, " ".join(context.args[1:])
        active_boards = get_flattened_board()
        m = active_boards[idx]
        m["note"] = note 
        save_data()
        await update.message.reply_text(f"✅ Đã ghi chú cho trận {m['home']} vs {m['away']}")
    except Exception: await update.message.reply_text("❌ HD: `/mnote 1 Nội dung`")

# ===== 8. NHẮC VIỆC & BÁO CÁO =====
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
        res += f"{i+1}. {status} {t['time']} - {t['content']} ({t['date'][5:]})\n"
        if t.get("note"): res += f"   └ 📝: _{t['note']}_\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def tnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx, note = int(context.args[0])-1, " ".join(context.args[1:])
        state["tasks"][idx]["note"] = note
        save_data()
        await update.message.reply_text(f"✅ Đã lưu note việc {idx+1}")
    except Exception: await update.message.reply_text("❌ HD: `/tnote 1 Nội dung`")

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        now = datetime.now(VN_TZ)
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        tasks_text = "\n".join([f"- {t['time']}: {t['content']}[{'Xong' if t.get('reminded') else 'Chưa'}]" for t in state["tasks"] if t["date"] == today_str]) or "Không có."
        boards_text = "\n".join([f"- {m.get('icon','⚽')} {m['home']} {m.get('score','?-?')} {m['away']}[{'Xong' if m.get('notified') else 'Chưa'}]" for m in state["boards"].get(today_str,[])]) or "Không có."

        lines =[]
        try:
            res_f = await client.get(f"https://v3.football.api-sports.io/fixtures?date={tomorrow_str}{TZ_PARAM}")
            for m in res_f.json().get("response",[])[:4]: lines.append(f"⚽ {parse_match_time(m['fixture']['date'])[0]}: {m['teams']['home']['name']} vs {m['teams']['away']['name']}")
            res_n = await client.get(f"https://v2.nba.api-sports.io/games?date={tomorrow_str}{TZ_PARAM}")
            for m in res_n.json().get("response",[])[:3]: lines.append(f"🌟 {parse_match_time(m['date']['start'])[0]}: {m['teams']['home']['name']} vs {m['teams']['away']['name']}")
        except: pass

        prompt = (f"Viết Báo Cáo Cuối Ngày.\n1. Task\n2. Kèo\n3. Trận HOT ngày mai\n4. Lời khuyên.\nHồ sơ: {' '.join(state.get('profile',[]))}\n"
                  f"Tasks:\n{tasks_text}\nBoard:\n{boards_text}\nNgày mai:\n{chr(10).join(lines)}")
        
        chat_id = update.effective_chat.id
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
        response = await asyncio.to_thread(chat_sessions[chat_id].send_message, prompt)
        
        try:
            await update.message.reply_text(f"📑 **BÁO CÁO NGÀY {today_str}**\n\n{response.text}", parse_mode="Markdown")
        except:
            await update.message.reply_text(f"📑 BÁO CÁO NGÀY {today_str}\n\n{response.text}")
    except: await update.message.reply_text("❌ Lỗi trích xuất báo cáo.")

async def morning_briefing(context: ContextTypes.DEFAULT_TYPE):
    chat_id = state.get("chat_id")
    if not chat_id: return
    today_str = datetime.now(VN_TZ).strftime("%Y-%m-%d")
    lines =[]
    try:
        res_f = await client.get(f"https://v3.football.api-sports.io/fixtures?date={today_str}{TZ_PARAM}")
        for m in res_f.json().get("response",[])[:5]: lines.append(f"⚽ {parse_match_time(m['fixture']['date'])[0]}: {m['teams']['home']['name']} vs {m['teams']['away']['name']}")
        res_n = await client.get(f"https://v2.nba.api-sports.io/games?date={today_str}{TZ_PARAM}")
        for m in res_n.json().get("response",[])[:4]: lines.append(f"🌟 {parse_match_time(m['date']['start'])[0]}: {m['teams']['home']['name']} vs {m['teams']['away']['name']}")
    except Exception: pass

    prompt = ("Viết BẢN TIN SÁNG 05:00. Highlight 2-3 trận đáng xem nhất và giải thích ngắn gọn.\n\n"
              f"Hồ sơ: {' '.join(state.get('profile',[]))}\nTrận:\n{chr(10).join(lines)}")
    try:
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
        response = await asyncio.to_thread(chat_sessions[chat_id].send_message, prompt)
        try:
            await context.bot.send_message(chat_id, f"🌅 **BẢN TIN THỂ THAO SÁNG**\n\n{response.text}", parse_mode="Markdown")
        except:
            await context.bot.send_message(chat_id, f"🌅 BẢN TIN THỂ THAO SÁNG\n\n{response.text}")
    except: pass

# ===== 9. MONITOR TỐI ƯU API ĐA NGÀY =====
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

    active_boards = get_flattened_board()
    
    for m in active_boards:
        if not m.get("reminded_15m") and "timestamp" in m:
            target = datetime.fromtimestamp(m["timestamp"], VN_TZ)
            if now >= (target - timedelta(minutes=15)) and now < target:
                await context.bot.send_message(state["chat_id"], f"⏰ **SẮP ĐÁ (15p nữa):** {m.get('icon','⚽')} {m['home']} vs {m['away']} lúc {m['time']}")
                m["reminded_15m"] = True
                save_data()

    live_matches =[m for m in active_boards if "timestamp" in m and now.timestamp() >= m["timestamp"]]
    
    if now.timestamp() - last_api_check >= 600 and live_matches:
        last_api_check = now.timestamp()
        dates_to_check = set(m.get("date") for m in live_matches if m.get("date"))
        
        for check_date in dates_to_check:
            matches_of_date =[m for m in live_matches if m.get("date") == check_date]
            has_f = any(m.get("sport") == "f" for m in matches_of_date)
            has_n = any(m.get("sport") == "n" for m in matches_of_date)
            has_b = any(m.get("sport") == "b" for m in matches_of_date)

            if has_f:
                try:
                    res = await client.get(f"https://v3.football.api-sports.io/fixtures?date={check_date}{TZ_PARAM}")
                    f_map = {f["fixture"]["id"]: f for f in res.json().get("response",[])}
                    for m in matches_of_date:
                        if m.get("sport") == "f" and m["id"] in f_map:
                            f_data = f_map[m["id"]]
                            if f_data["fixture"]["status"]["short"] in["FT", "AET", "PEN"]:
                                hg, ag = f_data['goals']['home'], f_data['goals']['away']
                                m["score"] = f"{hg if hg is not None else 0}-{ag if ag is not None else 0}"
                                await context.bot.send_message(state["chat_id"], f"🏁 **KẾT THÚC:** ⚽ {m['home']} {m['score']} {m['away']}")
                                m["notified"] = True
                                save_data()
                except Exception: pass

            if has_n:
                try:
                    res = await client.get(f"https://v2.nba.api-sports.io/games?date={check_date}{TZ_PARAM}")
                    n_map = {n["id"]: n for n in res.json().get("response",[])}
                    for m in matches_of_date:
                        if m.get("sport") == "n" and m["id"] in n_map:
                            n_data = n_map[m["id"]]
                            if str(n_data["status"]["short"]) in ["3", "FT", "AOT"]:
                                hg, ag = n_data['scores']['home']['points'], n_data['scores']['away']['points']
                                m["score"] = f"{hg if hg is not None else 0}-{ag if ag is not None else 0}"
                                await context.bot.send_message(state["chat_id"], f"🏁 **KẾT THÚC:** 🌟 {m['home']} {m['score']} {m['away']}")
                                m["notified"] = True
                                save_data()
                except Exception: pass

            if has_b:
                try:
                    res = await client.get(f"https://v1.basketball.api-sports.io/games?date={check_date}{TZ_PARAM}")
                    b_map = {b["id"]: b for b in res.json().get("response",[])}
                    for m in matches_of_date:
                        if m.get("sport") == "b" and m["id"] in b_map:
                            b_data = b_map[m["id"]]
                            if b_data["status"]["short"] in ["FT", "AOT"]:
                                hg, ag = b_data['scores']['home']['total'], b_data['scores']['away']['total']
                                m["score"] = f"{hg if hg is not None else 0}-{ag if ag is not None else 0}"
                                await context.bot.send_message(state["chat_id"], f"🏁 **KẾT THÚC:** 🏀 {m['home']} {m['score']} {m['away']}")
                                m["notified"] = True
                                save_data()
                except Exception: pass

# ===== 10. MAIN =====
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
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("detail", detail_cmd))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("mnote", mnote_cmd))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_chat_handler))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    if app.job_queue: 
        app.job_queue.run_repeating(monitor, interval=60, first=10)
        t = time(hour=5, minute=0, tzinfo=VN_TZ)
        app.job_queue.run_daily(morning_briefing, time=t)
        
    print("🚀 SUPREME AI COMMANDER V11.0 ĐÃ SẴN SÀNG PHỤC VỤ!")
    app.run_polling()

if __name__ == "__main__": 
    main()
