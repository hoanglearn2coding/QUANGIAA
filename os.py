import os
import json
import logging
import httpx
import pytz
import asyncio
import re
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
DATA_FILE = os.path.join(DATA_DIR, "supreme_v12_data.json")

# --- Cấu hình Gemini AI ---
genai.configure(api_key=GENAI_API_KEY)
system_prompt = (
    "Bạn là một Siêu Quản Gia AI và Cố Vấn Thể Thao Tối Cao của Ông chủ. Luôn xưng 'Dạ', 'Ông chủ', 'tôi' hoặc 'em'.\n"
    "Phân tích ngắn gọn, sắc bén, đánh thẳng vào phong độ, kèo cược. Lời lẽ uy lực."
)
ai_model = genai.GenerativeModel('gemini-2.5-flash', system_instruction=system_prompt, generation_config=genai.types.GenerationConfig(temperature=0.55))

# ===== 2. QUẢN LÝ DỮ LIỆU & API CACHE =====
state = {"tasks":[], "boards": {}, "profile":[], "chat_id": None}
chat_sessions = {} 
last_api_check = 0 
client = httpx.AsyncClient(headers={"x-apisports-key": API_KEY if API_KEY else ""}, timeout=20)

# BỘ NHỚ ĐỆM BẢO VỆ API (Cache 5 phút)
api_cache = {} 

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                state.update(json.load(f))
                if "profile" not in state: state["profile"] =[]
        except Exception as e: logging.error(f"Lỗi đọc file: {e}")

def parse_match_time(date_str):
    try:
        clean_str = date_str[:19]
        dt = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
        dt = VN_TZ.localize(dt)
        return dt.strftime("%H:%M"), dt.timestamp()
    except Exception: return "00:00", 0

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

# --- CƠ CHẾ GỌI API TỐI ƯU CÓ CACHE ---
async def fetch_api_cached(sport, date_str):
    key = f"{sport}_{date_str}"
    now_ts = datetime.now().timestamp()
    if key in api_cache and now_ts - api_cache[key][0] < 300: # Cache 5 phút
        return api_cache[key][1]
    
    url_map = {
        'f': f"https://v3.football.api-sports.io/fixtures?date={date_str}{TZ_PARAM}",
        'n': f"https://v2.nba.api-sports.io/games?date={date_str}{TZ_PARAM}",
        'b': f"https://v1.basketball.api-sports.io/games?date={date_str}{TZ_PARAM}"
    }
    try:
        res = await client.get(url_map[sport])
        if res.status_code == 200:
            data = res.json().get("response",[])
            api_cache[key] = (now_ts, data)
            return data
    except Exception: pass
    return[]

async def fetch_all_matches_for_dates(dates):
    matches =[]
    for d in dates:
        f_data = await fetch_api_cached('f', d)
        n_data = await fetch_api_cached('n', d)
        b_data = await fetch_api_cached('b', d)
        
        for m in f_data:
            t_str, ts = parse_match_time(m['fixture']['date'])
            matches.append({'id': m['fixture']['id'], 'sport': 'f', 'home': m['teams']['home']['name'], 'away': m['teams']['away']['name'], 'ts': ts, 'time_str': t_str, 'league': m['league']['name']})
        for m in n_data:
            t_str, ts = parse_match_time(m['date']['start'])
            matches.append({'id': m['id'], 'sport': 'n', 'home': m['teams']['home']['name'], 'away': m['teams']['away']['name'], 'ts': ts, 'time_str': t_str, 'league': 'NBA'})
        for m in b_data:
            t_str, ts = parse_match_time(m['date'])
            matches.append({'id': m['id'], 'sport': 'b', 'home': m['teams']['home']['name'], 'away': m['teams']['away']['name'], 'ts': ts, 'time_str': t_str, 'league': m['league']['name']})
    return matches

# --- HÀM AI ĐOÁN KÈO HÀNG LOẠT (JSON) ---
async def get_ai_over_under_predictions(matches_list):
    if not matches_list: return {}
    prompt = (
        "Bạn là siêu máy tính dữ liệu thể thao. Đánh giá nhanh xu hướng kèo Tài/Xỉu (Tổng bàn thắng/điểm) của các trận sau.\n"
        "QUY TẮC ICON (BẮT BUỘC):\n"
        "- 🍌 : Xu hướng nổ TÀI (Nhiều bàn/điểm)\n"
        "- ❌ : Xu hướng nổ XỈU (Ít bàn/điểm)\n"
        "- 🥥 : Không rõ ràng / Cân bằng\n"
        "CHỈ TRẢ VỀ ĐÚNG 1 ĐỊNH DẠNG JSON hợp lệ. Key là ID trận, Value là Icon. KHÔNG GIẢI THÍCH GÌ THÊM.\n"
        "Ví dụ: {\"123\": \"🍌\", \"456\": \"❌\"}\n\nDANH SÁCH TRẬN:\n"
    )
    for m in matches_list: prompt += f"ID: {m['id']} | {m['home']} vs {m['away']} ({m['league']})\n"
    
    try:
        resp = await asyncio.to_thread(ai_model.generate_content, prompt)
        match = re.search(r'\{.*\}', resp.text, re.DOTALL)
        if match: return json.loads(match.group(0))
    except Exception as e: logging.error(f"Lỗi AI Bulk Predict: {e}")
    return {}

# --- LẤY LỊCH SỬ ĐỐI ĐẦU ĐỂ HIỂN THỊ KHI PICK ---
async def get_match_context(m):
    sport, home_id, away_id = m.get("sport", "f"), m.get("home_id"), m.get("away_id")
    league = m.get("league", "Không rõ giải")
    if not home_id or not away_id: return league, "Thiếu ID.", "Thiếu ID."

    if sport == "f":
        try:
            h_task = client.get(f"https://v3.football.api-sports.io/fixtures?team={home_id}&last=2")
            a_task = client.get(f"https://v3.football.api-sports.io/fixtures?team={away_id}&last=2")
            res_h, res_a = await asyncio.gather(h_task, a_task)
            def fmt_f(res_data):
                try:
                    data = res_data.json().get("response",[])
                    if not data: return "   + Chưa có dữ liệu."
                    return "\n".join([f"   + {f['fixture']['date'][:10]}: {f['teams']['home']['name']} {f['goals']['home'] if f['goals']['home'] is not None else '?'}-{f['goals']['away'] if f['goals']['away'] is not None else '?'} {f['teams']['away']['name']}" for f in data])
                except: return "   + Lỗi dữ liệu."
            return league, fmt_f(res_h), fmt_f(res_a)
        except: return league, "Lỗi API.", "Lỗi API."
    else:
        try:
            endpoint = "v2.nba.api-sports.io" if sport == 'n' else "v1.basketball.api-sports.io"
            res = await client.get(f"https://{endpoint}/games?h2h={home_id}-{away_id}")
            data = res.json().get("response",[])
            if not data: return league, "Chưa có H2H.", "Chưa có H2H."
            lines = []
            for f in data[:3]:
                d_str = f['date'][:10] if sport == 'b' else f['date']['start'][:10]
                hg = f.get('scores', {}).get('home', {}).get('points' if sport=='n' else 'total')
                ag = f.get('scores', {}).get('away', {}).get('points' if sport=='n' else 'total')
                lines.append(f"   + {d_str}: {f['teams']['home']['name']} {hg if hg is not None else '?'}-{ag if ag is not None else '?'} {f['teams']['away']['name']}")
            h2h_str = "\n".join(lines)
            return league, f"ĐỐI ĐẦU GẦN NHẤT:\n{h2h_str}", f"ĐỐI ĐẦU GẦN NHẤT:\n{h2h_str}"
        except: return league, "Lỗi API.", "Lỗi API."

# ===== 3. MENU START =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["chat_id"] = update.effective_chat.id
    save_data()
    menu = (
        "🤵 **AI COMMANDER V12 - ĐỈNH CAO SOI KÈO**\n\n"
        "🧠 **[ AI CỐ VẤN ]**\n"
        " ├ 💬 *Chat tự do để hỏi lịch, chiến thuật*\n"
        " ├ 📥 `/learn[Sở thích]` : Dạy AI nhớ gu\n"
        " └ 📊 `/summary` : Tổng kết ngày\n\n"
        "⚽🏀🌟 **[ QUÉT TRẬN & AI DỰ ĐOÁN ]**\n"
        " *(Tự lọc trận quá khứ. Kèm Icon: 🍌 Tài | ❌ Xỉu | 🥥 Tạm)*\n"
        " ├ 📅 `/matches` : Quét 24 Giờ tới\n"
        " ├ 🔍 `/search [Tên]` : Tìm đội/giải (Sắp đá)\n"
        " ├ ⏰ `/time [Giờ]` : Lọc trận khung giờ\n"
        " ├ 📊 `/board` : Bảng theo dõi Tổng\n"
        " └ 📜 `/history` : Các trận đã xong\n\n"
        "📅 **[ NHẮC VIỆC ]**\n"
        " └ ➕ `/add` | 📜 `/list` | 📝 `/tnote`"
    )
    await update.message.reply_text(menu, parse_mode="Markdown")

async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = " ".join(context.args)
    if not info: return await update.message.reply_text("❌ HD: `/learn Tôi thích đánh Tài góc`")
    state["profile"].append(info)
    save_data()
    await update.message.reply_text(f"✅ Đã lưu hồ sơ mật: *{info}*", parse_mode="Markdown")

async def natural_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    query = update.message.text
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat(history=[])
        chat = chat_sessions[chat_id]
        now = datetime.now(VN_TZ)
        
        prof = "\n".join(state.get("profile",[])) or "Chưa có."
        tasks = "\n".join([f"- {t['time']}: {t['content']}" for t in state["tasks"] if not t.get("reminded") and t["date"] == now.strftime("%Y-%m-%d")]) or "Rảnh"
        boards = "\n".join([f"- {m['home']} vs {m['away']} ({m.get('time')})" for m in get_flattened_board()]) or "Không"

        prompt = f"[Hệ thống {now.strftime('%A %H:%M')}]\nHồ sơ: {prof}\nViệc: {tasks}\nBoard: {boards}\n\nÔng chủ: {query}"
        resp = await asyncio.wait_for(asyncio.to_thread(chat.send_message, prompt), timeout=30.0)
        
        try: await update.message.reply_text(f"🤖 **AI:**\n{resp.text}", parse_mode="Markdown")
        except: await update.message.reply_text(f"🤖 AI:\n{resp.text}")
    except: await update.message.reply_text("❌ Hệ thống nơ-ron đang bận.")

# ===== 4. SIÊU TÌM KIẾM 24H & AI AUTO PREDICT =====
async def display_matches_with_ai(update, matches_list, title):
    msg = await update.message.reply_text("⏳ Đang quét dữ liệu và kích hoạt AI phân tích kèo... Vui lòng đợi 5s!")
    
    if not matches_list:
        return await msg.edit_text("📭 Không có trận nào thỏa mãn điều kiện hoặc các trận đã đá xong.")
        
    matches_list.sort(key=lambda x: x['ts'])
    matches_list = matches_list[:40] # Giới hạn 40 trận để AI không bị ngợp
    
    # Kêu gọi AI phân tích hàng loạt
    ai_preds = await get_ai_over_under_predictions(matches_list)
    
    kb =[]
    for m in matches_list:
        ai_icon = ai_preds.get(str(m['id']), "🥥")
        if ai_icon not in ['🍌', '❌', '🥥']: ai_icon = "🥥"
        sport_icon = '⚽' if m['sport']=='f' else ('🌟' if m['sport']=='n' else '🏀')
        
        # Nút bấm hiển thị: [🍌] 20:30 MU vs Chelsea
        btn_text = f"{ai_icon} [{m['time_str']}] {m['home']} vs {m['away']}"
        kb.append([InlineKeyboardButton(btn_text, callback_data=f"pk_{m['sport']}_{m['id']}")])
        
    final_text = f"{title}\n*(🍌 Dễ Tài | ❌ Dễ Xỉu | 🥥 Khó đoán)*"
    await msg.edit_text(final_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(VN_TZ)
    now_ts = now.timestamp()
    
    # Quét cả hôm nay và ngày mai để đảm bảo trọn vẹn 24h
    dates =[now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")]
    all_matches = await fetch_all_matches_for_dates(dates)
    
    # LỌC NGHIÊM NGẶT: Chỉ lấy trận SẮP ĐÁ trong vòng 24H tới
    upcoming =[m for m in all_matches if now_ts < m['ts'] <= now_ts + 86400]
    
    await display_matches_with_ai(update, upcoming, "📅 **LỊCH TRẬN 24 GIỜ TỚI:**")

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).lower()
    if not query: return await update.message.reply_text("❌ HD: `/search MU` hoặc `/search Premier League`")
    
    now = datetime.now(VN_TZ)
    now_ts = now.timestamp()
    dates =[now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d"), (now + timedelta(days=2)).strftime("%Y-%m-%d")]
    all_matches = await fetch_all_matches_for_dates(dates)
    
    # Chỉ lấy trận SẮP ĐÁ và khớp từ khóa
    filtered = [m for m in all_matches if m['ts'] > now_ts and (query in m['home'].lower() or query in m['away'].lower() or query in m['league'].lower() or (query=='nba' and m['sport']=='n'))]
    
    await display_matches_with_ai(update, filtered, f"🔍 **KẾT QUẢ SẮP ĐÁ CHO '{query.upper()}':**")

async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("❌ HD: `/time 20` (Tìm trận lúc 20h sắp tới)")
    target_time = context.args[0]
    
    now = datetime.now(VN_TZ)
    now_ts = now.timestamp()
    dates =[now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")]
    all_matches = await fetch_all_matches_for_dates(dates)
    
    filtered = [m for m in all_matches if m['ts'] > now_ts and (m['time_str'] == target_time or m['time_str'].startswith(target_time + ":"))]
    
    await display_matches_with_ai(update, filtered, f"⏰ **TRẬN SẮP ĐÁ KHUNG GIỜ {target_time}:**")

# ===== 5. XỬ LÝ NÚT PICK (GỌN GÀNG, BỎ NÚT AI) =====
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
            
            await query.answer(f"✅ Đã nạp vào Board: {new_match['home']}", show_alert=False)
            
            # Tự động trả về báo cáo lịch sử, loại bỏ nút AI Predict rườm rà
            league, home_last, away_last = await get_match_context(new_match)
            idx = len(get_flattened_board()) - 1 
            
            msg = (f"✅ **VÀO BOARD:** {icon} {new_match['home']} vs {new_match['away']} (Lúc {t_str} ngày {actual_date})\n"
                   f"🏆 **GIẢI ĐẤU:** {league}\n\n"
                   f"🛡️ **THÔNG TIN ({new_match['home']}):**\n{home_last}\n\n"
                   f"⚔️ **THÔNG TIN ({new_match['away']}):**\n{away_last}")
            
            kb = [[InlineKeyboardButton("📝 Thêm ghi chú kèo", callback_data=f"asknote_m_{idx}")]]
            await context.bot.send_message(query.message.chat_id, msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        except Exception as e: 
            logging.error(f"Lỗi Pick: {e}")
            await query.answer("Lỗi thêm trận!", show_alert=True)

    elif data.startswith("asknote_"):
        _, kind, idx = data.split("_")
        await query.message.reply_text(f"👉 Gõ lệnh:\n`/tnote {idx}[Nội dung]`" if kind == "t" else f"👉 Gõ lệnh:\n`/mnote {idx} [Nội dung]`", parse_mode="Markdown")
        await query.answer()

# ===== 6. QUẢN LÝ BẢNG TỔNG =====
async def board_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = get_flattened_board()
    if not active: return await update.message.reply_text("📭 Board trống.")
    res = f"📊 **BẢNG THEO DÕI TỔNG:**\n"
    for i, m in enumerate(active):
        res += f"{i+1}. ⏳[{m.get('time')} - {m.get('date')[5:]}] *{m.get('icon','⚽')} {m['home']} vs {m['away']}*\n"
        if m.get("note"): res += f"   └ 📝: _{m['note']}_\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def detail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(context.args[0]) - 1
        m = get_flattened_board()[idx]
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        lg, h_last, a_last = await get_match_context(m)
        res = f"🏆 **GIẢI:** {lg}\n{m.get('icon','⚽')} **TRẬN:** {m['home']} vs {m['away']}\n⏰ **GIỜ:** {m.get('time')}\n\n🛡️ **{m['home']}:**\n{h_last}\n\n⚔️ **{m['away']}:**\n{a_last}"
        await update.message.reply_text(res, parse_mode="Markdown")
    except: await update.message.reply_text("❌ Lỗi! HD: `/detail 1`")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matches =[m for d_m in state["boards"].values() for m in d_m if m.get("notified")]
    matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True) 
    if not matches: return await update.message.reply_text("📭 Chưa có trận nào kết thúc.")
    res = "📜 **HISTORY BOARD (Đã Xong):**\n"
    for i, m in enumerate(matches[:30]): 
        res += f"{i+1}. ✅ {m.get('icon','⚽')} {m['home']} {m.get('score', '')} {m['away']} ({m.get('date')[5:]})\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def mnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx, note = int(context.args[0])-1, " ".join(context.args[1:])
        m = get_flattened_board()[idx]
        m["note"] = note 
        save_data()
        await update.message.reply_text(f"✅ Đã ghi chú cho trận {m['home']} vs {m['away']}")
    except: await update.message.reply_text("❌ HD: `/mnote 1 Nội dung`")

# ===== 7. CÁC LỆNH CÒN LẠI & MONITOR =====
async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        t_str, c = context.args[0], " ".join(context.args[1:])
        state["tasks"].append({"time": t_str, "content": c, "reminded": False, "note": "", "date": datetime.now(VN_TZ).strftime("%Y-%m-%d")})
        save_data()
        await update.message.reply_text(f"➕ Đã thêm: *{c}*", parse_mode="Markdown")
    except: await update.message.reply_text("❌ HD: `/add 08:00 Việc`")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state["tasks"]: return await update.message.reply_text("📭 Trống.")
    res = "📜 **CÔNG VIỆC:**\n"
    for i, t in enumerate(state["tasks"]):
        res += f"{i+1}. {'✅' if t.get('reminded') else '🕒'} {t['time']} - {t['content']} ({t['date'][5:]})\n"
        if t.get("note"): res += f"   └ 📝: _{t['note']}_\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def tnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx, note = int(context.args[0])-1, " ".join(context.args[1:])
        state["tasks"][idx]["note"] = note
        save_data()
        await update.message.reply_text(f"✅ Đã lưu note việc {idx+1}")
    except: await update.message.reply_text("❌ HD: `/tnote 1 Nội dung`")

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        now = datetime.now(VN_TZ)
        t_str = now.strftime("%Y-%m-%d")
        
        t_txt = "\n".join([f"- {t['time']}: {t['content']}" for t in state["tasks"] if t["date"] == t_str]) or "Không."
        b_txt = "\n".join([f"- {m.get('icon','⚽')} {m['home']} {m.get('score','?-?')} {m['away']}" for m in state["boards"].get(t_str,[])]) or "Không."

        prompt = f"Viết Báo Cáo Cuối Ngày. Dựa vào Hồ sơ: {' '.join(state.get('profile',[]))}\nTask:\n{t_txt}\nKèo:\n{b_txt}"
        chat_id = update.effective_chat.id
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat()
        resp = await asyncio.to_thread(chat_sessions[chat_id].send_message, prompt)
        await update.message.reply_text(f"📑 **BÁO CÁO NGÀY {t_str}**\n\n{resp.text}", parse_mode="Markdown")
    except: await update.message.reply_text("❌ Lỗi báo cáo.")

async def morning_briefing(context: ContextTypes.DEFAULT_TYPE):
    chat_id = state.get("chat_id")
    if not chat_id: return
    now_ts = datetime.now(VN_TZ).timestamp()
    try:
        matches = await fetch_all_matches_for_dates([datetime.now(VN_TZ).strftime("%Y-%m-%d")])
        live = [m for m in matches if m['ts'] > now_ts][:10]
        prompt = f"Viết Bản Tin Sáng 05:00. Chọn 2 trận đáng xem nhất giải thích ngắn gọn.\nHồ sơ: {' '.join(state.get('profile',[]))}\nTrận:\n" + "\n".join([f"{m['sport']} {m['time_str']} {m['home']} vs {m['away']}" for m in live])
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat()
        resp = await asyncio.to_thread(chat_sessions[chat_id].send_message, prompt)
        await context.bot.send_message(chat_id, f"🌅 **BẢN TIN SÁNG**\n\n{resp.text}", parse_mode="Markdown")
    except: pass

async def monitor(context: ContextTypes.DEFAULT_TYPE):
    global last_api_check
    now = datetime.now(VN_TZ)
    if not state.get("chat_id"): return
    
    for t in state["tasks"]:
        if not t.get("reminded") and t.get("date") == now.strftime("%Y-%m-%d"):
            try:
                target = datetime.strptime(t["time"], "%H:%M").replace(year=now.year, month=now.month, day=now.day, tzinfo=VN_TZ)
                if now >= (target - timedelta(minutes=15)) and now < target:
                    await context.bot.send_message(state["chat_id"], f"⏰ **NHẮC VIỆC (15p nữa):** {t['content']}")
                    t["reminded"] = True
                    save_data()
            except: continue

    active = get_flattened_board()
    for m in active:
        if not m.get("reminded_15m") and "timestamp" in m:
            target = datetime.fromtimestamp(m["timestamp"], VN_TZ)
            if now >= (target - timedelta(minutes=15)) and now < target:
                await context.bot.send_message(state["chat_id"], f"⏰ **SẮP ĐÁ (15p nữa):** {m.get('icon')} {m['home']} vs {m['away']}")
                m["reminded_15m"] = True
                save_data()

    live_m = [m for m in active if "timestamp" in m and now.timestamp() >= m["timestamp"]]
    if now.timestamp() - last_api_check >= 600 and live_m:
        last_api_check = now.timestamp()
        for d in set(m.get("date") for m in live_m if m.get("date")):
            m_d =[m for m in live_m if m.get("date") == d]
            if any(m.get("sport") == "f" for m in m_d):
                try:
                    res = await client.get(f"https://v3.football.api-sports.io/fixtures?date={d}{TZ_PARAM}")
                    f_map = {f["fixture"]["id"]: f for f in res.json().get("response",[])}
                    for m in m_d:
                        if m.get("sport") == "f" and m["id"] in f_map:
                            f_data = f_map[m["id"]]
                            if f_data["fixture"]["status"]["short"] in["FT", "AET", "PEN"]:
                                hg, ag = f_data['goals']['home'], f_data['goals']['away']
                                m["score"] = f"{hg if hg is not None else 0}-{ag if ag is not None else 0}"
                                await context.bot.send_message(state["chat_id"], f"🏁 **KẾT THÚC:** ⚽ {m['home']} {m['score']} {m['away']}")
                                m["notified"] = True
                                save_data()
                except: pass
            if any(m.get("sport") in ['n','b'] for m in m_d):
                try:
                    res = await client.get(f"https://v1.basketball.api-sports.io/games?date={d}{TZ_PARAM}")
                    b_map = {b["id"]: b for b in res.json().get("response",[])}
                    for m in m_d:
                        if m.get("sport") in ['n','b'] and m["id"] in b_map:
                            b_data = b_map[m["id"]]
                            if b_data["status"]["short"] in ["FT", "AOT"]:
                                hg, ag = b_data['scores']['home']['total'], b_data['scores']['away']['total']
                                m["score"] = f"{hg if hg is not None else 0}-{ag if ag is not None else 0}"
                                await context.bot.send_message(state["chat_id"], f"🏁 **KẾT THÚC:** {m.get('icon')} {m['home']} {m['score']} {m['away']}")
                                m["notified"] = True
                                save_data()
                except: pass

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
    app.add_handler(CommandHandler("mnote", mnote_cmd))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_chat_handler))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    if app.job_queue: 
        app.job_queue.run_repeating(monitor, interval=60, first=10)
        app.job_queue.run_daily(morning_briefing, time=time(hour=5, minute=0, tzinfo=VN_TZ))
        
    print("🚀 SUPREME AI COMMANDER V12.0 ĐÃ SẴN SÀNG!")
    app.run_polling()

if __name__ == "__main__": main()
