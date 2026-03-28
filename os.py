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
DATA_FILE = os.path.join(DATA_DIR, "supreme_v13_data.json")

# --- Cấu hình Gemini AI ---
genai.configure(api_key=GENAI_API_KEY)
system_prompt = (
    "Bạn là Siêu Quản Gia AI và Cố Vấn Thể Thao Tối Cao. Luôn xưng 'Dạ', 'Ông chủ', 'em'. "
    "Phân tích ngắn gọn, đánh thẳng vào phong độ. "
    "Bạn kiêm luôn vai trò TRỌNG TÀI: Dựa vào Tỷ số thực tế và Dự đoán của Ông chủ để phán xử Thắng/Thua công bằng."
)
ai_model = genai.GenerativeModel('gemini-2.5-flash', system_instruction=system_prompt, generation_config=genai.types.GenerationConfig(temperature=0.4))

# ===== 2. QUẢN LÝ DỮ LIỆU & VÍ CHUỐI =====
state = {"tasks":[], "boards": {}, "profile":[], "chat_id": None, "wallet": {"bananas": 10, "last_week": 0}}
chat_sessions = {} 
last_api_check = 0 
client = httpx.AsyncClient(headers={"x-apisports-key": API_KEY if API_KEY else ""}, timeout=20)
api_cache = {} 

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                state.update(json.load(f))
                if "wallet" not in state: state["wallet"] = {"bananas": 10, "last_week": 0}
                if "profile" not in state: state["profile"] =[]
        except Exception as e: logging.error(f"Lỗi đọc file: {e}")

# HỆ THỐNG TRỢ CẤP CHUỐI (Thứ 2 hàng tuần)
def check_weekly_allowance():
    now = datetime.now(VN_TZ)
    current_week = now.isocalendar()[1] # Lấy số thứ tự của tuần trong năm
    if state["wallet"].get("last_week") != current_week:
        state["wallet"]["last_week"] = current_week
        # Bơm 10 chuối. Nếu đang nghèo (<10) thì reset lên 10. Nếu đang giàu thì cộng thêm 10.
        state["wallet"]["bananas"] = max(10, state["wallet"]["bananas"] + 10)
        save_data()
        return True
    return False

def parse_match_time(date_str):
    try:
        clean_str = date_str[:19]
        dt = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
        dt = VN_TZ.localize(dt)
        return dt.strftime("%H:%M"), dt.timestamp()
    except Exception: return "00:00", 0

def get_flattened_board():
    matches =[]
    for date_key, daily_matches in state["boards"].items():
        matches.extend([m for m in daily_matches if not m.get("notified")])
    matches.sort(key=lambda x: x.get("timestamp", 0))
    return matches

# --- HÀM AI ĐOÁN KÈO HÀNG LOẠT (CHỐNG CRASH) ---
async def get_ai_over_under_predictions(matches_list):
    if not matches_list: return {}
    prompt = (
        "Chỉ trả về 1 chuỗi JSON duy nhất, không giải thích. Key là ID, Value là Icon.\n"
        "Icon: 🍌 (Dễ nổ TÀI), ❌ (Dễ nổ XỈU), 🥥 (Khó đoán/Cân bằng).\n"
        "Ví dụ: {\"123\": \"🍌\", \"456\": \"❌\"}\nDANH SÁCH:\n"
    )
    for m in matches_list: prompt += f"ID: {m['id']} | {m['home']} vs {m['away']}\n"
    
    try:
        resp = await asyncio.wait_for(asyncio.to_thread(ai_model.generate_content, prompt), timeout=15.0)
        cleaned = resp.text.replace("```json", "").replace("```", "").strip()
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if match: return json.loads(match.group(0))
    except Exception as e: logging.error(f"Lỗi AI Bulk Predict: {e}")
    return {}

# --- AI TRỌNG TÀI QUYẾT ĐỊNH THẮNG THUA ---
async def ai_referee(match_data):
    try:
        prompt = (
            f"Trận đấu: {match_data['home']} {match_data['score']} {match_data['away']}.\n"
            f"Người chơi đã đặt cược: '{match_data['bet']['prediction']}'.\n"
            "Dựa vào tỷ số trên, dự đoán của người chơi là ĐÚNG hay SAI? "
            "Chỉ trả lời 1 chữ duy nhất: 'THẮNG' hoặc 'THUA'."
        )
        resp = await asyncio.wait_for(asyncio.to_thread(ai_model.generate_content, prompt), timeout=15.0)
        return "THẮNG" in resp.text.upper() or "ĐÚNG" in resp.text.upper()
    except Exception: return False # Nếu AI lỗi, mặc định cho thua (nhà cái luôn có lợi :v)

async def fetch_api_cached(sport, date_str):
    key = f"{sport}_{date_str}"
    now_ts = datetime.now().timestamp()
    if key in api_cache and now_ts - api_cache[key][0] < 300: return api_cache[key][1]
    
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
        for sport, key_prefix, league_key in[('f', 'fixture', 'league'), ('n', 'date', 'NBA'), ('b', 'date', 'league')]:
            data = await fetch_api_cached(sport, d)
            for m in data:
                if sport == 'f':
                    if m['fixture']['status']['short'] in['FT', 'AET', 'PEN', 'CANC', 'PST']: continue
                    t_str, ts = parse_match_time(m['fixture']['date'])
                    matches.append({'id': m['fixture']['id'], 'sport': 'f', 'home': m['teams']['home']['name'], 'away': m['teams']['away']['name'], 'ts': ts, 'time_str': t_str, 'league': m['league']['name']})
                else:
                    if str(m['status']['short']) in ['3', 'FT', 'AOT', 'CANC', 'PST', 'POST']: continue
                    t_str, ts = parse_match_time(m['date']['start'] if sport=='n' else m['date'])
                    matches.append({'id': m['id'], 'sport': sport, 'home': m['teams']['home']['name'], 'away': m['teams']['away']['name'], 'ts': ts, 'time_str': t_str, 'league': 'NBA' if sport=='n' else m['league']['name']})
    return matches

# Lịch sử đối đầu gọn nhẹ
async def get_match_context(m):
    sport, h_id, a_id = m.get("sport", "f"), m.get("home_id"), m.get("away_id")
    lg = m.get("league", "Không rõ")
    if not h_id or not a_id: return lg, "Thiếu ID.", "Thiếu ID."

    if sport == "f":
        try:
            h_res = await client.get(f"https://v3.football.api-sports.io/fixtures?team={h_id}&last=2")
            a_res = await client.get(f"https://v3.football.api-sports.io/fixtures?team={a_id}&last=2")
            def fmt(r):
                try:
                    d = r.json().get("response",[])
                    if not d: return "   + Chưa có dữ liệu."
                    return "\n".join([f"   + {f['fixture']['date'][:10]}: {f['teams']['home']['name']} {f['goals']['home'] if f['goals']['home'] is not None else '?'}-{f['goals']['away'] if f['goals']['away'] is not None else '?'} {f['teams']['away']['name']}" for f in d])
                except: return "   + Lỗi dữ liệu."
            return lg, fmt(h_res), fmt(a_res)
        except: return lg, "Lỗi API.", "Lỗi API."
    else:
        try:
            ep = "v2.nba.api-sports.io" if sport == 'n' else "v1.basketball.api-sports.io"
            res = await client.get(f"https://{ep}/games?h2h={h_id}-{a_id}")
            d = res.json().get("response",[])
            if not d: return lg, "Chưa có H2H.", "Chưa có H2H."
            lines = []
            for f in d[:2]:
                d_str = f['date'][:10] if sport == 'b' else f['date']['start'][:10]
                hg = f.get('scores', {}).get('home', {}).get('points' if sport=='n' else 'total')
                ag = f.get('scores', {}).get('away', {}).get('points' if sport=='n' else 'total')
                lines.append(f"   + {d_str}: {f['teams']['home']['name']} {hg if hg is not None else '?'}-{ag if ag is not None else '?'} {f['teams']['away']['name']}")
            return lg, f"ĐỐI ĐẦU:\n{chr(10).join(lines)}", f"ĐỐI ĐẦU:\n{chr(10).join(lines)}"
        except: return lg, "Lỗi API.", "Lỗi API."


# ===== 3. COMMANDS CƠ BẢN =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["chat_id"] = update.effective_chat.id
    check_weekly_allowance()
    save_data()
    menu = (
        "🤵 **AI COMMANDER V13 - CASINO CHUỐI 🍌**\n\n"
        f"💰 **SỐ DƯ HIỆN TẠI:** {state['wallet']['bananas']} 🍌 (Lệnh: `/wallet`)\n\n"
        "⚽🏀🌟 **[ QUÉT TRẬN 24H & AI AUTO TÀI/XỈU ]**\n"
        " ├ 📅 `/matches` : Danh sách 24 Giờ tới\n"
        " ├ 🔍 `/search [Tên]` : Tìm đội/giải (Sắp đá)\n"
        " ├ ⏰ `/time [Giờ]` : Lọc trận khung giờ\n"
        " ├ 📊 `/board` : Bảng theo dõi Tổng\n"
        " └ 📜 `/history` : Lịch sử cược & kết quả\n\n"
        "🎲 **[ ĐẶT CƯỢC MÔ PHỎNG ]**\n"
        " └ 💵 `/bet[STT_Board] [Số_Chuối] [Kèo]`\n"
        "   *(VD: /bet 1 2 MU thắng)*\n\n"
        "🧠 **[ BỘ NÃO AI ]**\n"
        " ├ 💬 *Chat tự do để hỏi chiến thuật*\n"
        " ├ 📥 `/learn [Sở thích]` | 📋 `/profile`\n"
        " └ 📊 `/summary` : Báo cáo cuối ngày"
    )
    await update.message.reply_text(menu, parse_mode="Markdown")

async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    check_weekly_allowance()
    b = state['wallet']['bananas']
    await update.message.reply_text(f"💰 **VÍ CỦA ÔNG CHỦ:** {b} 🍌\n*(Lưu ý: Hệ thống sẽ tự động cấp thêm 10 🍌 vào sáng Thứ 2 hàng tuần)*", parse_mode="Markdown")

async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = " ".join(context.args)
    state["profile"].append(info)
    save_data()
    await update.message.reply_text(f"✅ Đã lưu hồ sơ mật: *{info}*", parse_mode="Markdown")

async def natural_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    query = update.message.text
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat()
        chat = chat_sessions[chat_id]
        
        prof = "\n".join(state.get("profile",[]))
        tasks = "\n".join([f"- {t['time']}: {t['content']}" for t in state["tasks"] if not t.get("reminded") and t["date"] == datetime.now(VN_TZ).strftime("%Y-%m-%d")])
        boards = "\n".join([f"- {m['home']} vs {m['away']}" for m in get_flattened_board()])

        prompt = f"[Ngữ cảnh]\nHồ sơ: {prof}\nViệc: {tasks}\nBoard: {boards}\n\nÔng chủ: {query}"
        resp = await asyncio.wait_for(asyncio.to_thread(chat.send_message, prompt), timeout=30.0)
        
        try: await update.message.reply_text(f"🤖 **AI:**\n{resp.text}", parse_mode="Markdown")
        except: await update.message.reply_text(f"🤖 AI:\n{resp.text}")
    except: await update.message.reply_text("❌ Hệ thống nơ-ron đang bận.")

# ===== 4. SIÊU TÌM KIẾM 24H (AI GẮN ICON) =====
async def display_matches_with_ai(update, matches_list, title):
    msg = await update.message.reply_text("⏳ Đang quét dữ liệu và kích hoạt AI phân tích Tài/Xỉu...")
    if not matches_list: return await msg.edit_text("📭 Không có trận nào sắp diễn ra thỏa mãn.")
        
    matches_list.sort(key=lambda x: x['ts'])
    matches_list = matches_list[:40] 
    ai_preds = await get_ai_over_under_predictions(matches_list)
    
    kb =[]
    for m in matches_list:
        ai_icon = ai_preds.get(str(m['id']), "🥥")
        if ai_icon not in['🍌', '❌', '🥥']: ai_icon = "🥥"
        
        btn_text = f"{ai_icon} [{m['time_str']}] {m['home']} vs {m['away']}"
        kb.append([InlineKeyboardButton(btn_text, callback_data=f"pk_{m['sport']}_{m['id']}")])
        
    final_text = f"{title}\n*(🍌: Dễ Tài | ❌: Dễ Xỉu | 🥥: Khó đoán)*"
    await msg.edit_text(final_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(VN_TZ)
    dates =[now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")]
    all_matches = await fetch_all_matches_for_dates(dates)
    upcoming =[m for m in all_matches if now.timestamp() < m['ts'] <= now.timestamp() + 86400]
    await display_matches_with_ai(update, upcoming, "📅 **LỊCH TRẬN 24 GIỜ TỚI:**")

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).lower()
    if not query: return await update.message.reply_text("❌ HD: `/search MU`")
    now = datetime.now(VN_TZ)
    dates =[now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")]
    all_matches = await fetch_all_matches_for_dates(dates)
    filtered =[m for m in all_matches if m['ts'] > now.timestamp() and (query in m['home'].lower() or query in m['away'].lower() or query in m['league'].lower() or (query=='nba' and m['sport']=='n'))]
    await display_matches_with_ai(update, filtered, f"🔍 **KẾT QUẢ CHO '{query.upper()}':**")

async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("❌ HD: `/time 20`")
    target_time = context.args[0]
    now = datetime.now(VN_TZ)
    dates =[now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")]
    all_matches = await fetch_all_matches_for_dates(dates)
    filtered = [m for m in all_matches if m['ts'] > now.timestamp() and (m['time_str'] == target_time or m['time_str'].startswith(target_time + ":"))]
    await display_matches_with_ai(update, filtered, f"⏰ **TRẬN SẮP ĐÁ KHUNG GIỜ {target_time}:**")

# ===== 5. XỬ LÝ PICK & ĐẶT CƯỢC =====
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("pk_"):
        parts = data.split("_")
        sport, fid = parts[1], int(parts[2])
        
        try:
            url_map = {'f': f"https://v3.football.api-sports.io/fixtures?id={fid}{TZ_PARAM}", 'n': f"https://v2.nba.api-sports.io/games?id={fid}{TZ_PARAM}", 'b': f"https://v1.basketball.api-sports.io/games?id={fid}{TZ_PARAM}"}
            res = await client.get(url_map[sport])
            m_data = res.json()["response"][0]
            
            if sport == 'f': t_str, ts = parse_match_time(m_data['fixture']['date']); icon, lg_name = "⚽", m_data['league']['name']
            elif sport == 'n': t_str, ts = parse_match_time(m_data['date']['start']); icon, lg_name = "🌟", "NBA"
            else: t_str, ts = parse_match_time(m_data['date']); icon, lg_name = "🏀", m_data['league']['name']

            actual_date = datetime.fromtimestamp(ts, VN_TZ).strftime("%Y-%m-%d")
            state["boards"].setdefault(actual_date, [])
            if any(m['id'] == fid and m.get('sport','f') == sport for m in state["boards"][actual_date]): 
                return await query.answer("Trận này đã có sẵn trong Board!", show_alert=True)

            new_match = {
                "id": fid, "sport": sport, "icon": icon, "date": actual_date,
                "home": m_data["teams"]["home"]["name"], "away": m_data["teams"]["away"]["name"], 
                "time": t_str, "timestamp": ts, "league": lg_name, 
                "home_id": m_data['teams']['home']['id'], "away_id": m_data['teams']['away']['id'],
                "notified": False, "reminded_15m": False, "score": "", "bet": None # Khởi tạo ô chứa cược
            }
            state["boards"][actual_date].append(new_match)
            save_data()
            
            await query.answer(f"✅ Đã nạp vào Board!", show_alert=False)
            
            league, home_last, away_last = await get_match_context(new_match)
            idx = len(get_flattened_board()) 
            
            msg = (f"✅ **VÀO BOARD:** {icon} {new_match['home']} vs {new_match['away']}\n"
                   f"⏰ **GIỜ:** {t_str} ngày {actual_date[8:]}\n"
                   f"🛡️ **THÔNG TIN ({new_match['home']}):**\n{home_last}\n"
                   f"⚔️ **THÔNG TIN ({new_match['away']}):**\n{away_last}\n\n"
                   f"👉 ĐỂ CƯỢC TRẬN NÀY GÕ:\n`/bet {idx} [Số_Chuối] [Kèo dự đoán]`\n*(VD: /bet {idx} 2 Bắt cửa Home)*")
            
            await context.bot.send_message(query.message.chat_id, msg, parse_mode="Markdown")
        except Exception as e: 
            logging.error(f"Lỗi Pick: {e}")
            await query.answer("Lỗi lấy dữ liệu!", show_alert=True)

# LỆNH ĐẶT CƯỢC 🍌
async def bet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        idx = int(context.args[0]) - 1
        amount = int(context.args[1])
        prediction = " ".join(context.args[2:])
        
        if amount <= 0: return await update.message.reply_text("❌ Số chuối cược phải lớn hơn 0.")
        if state["wallet"]["bananas"] < amount:
            return await update.message.reply_text(f"❌ Kẻ nghèo hèn! Ông chủ chỉ còn {state['wallet']['bananas']} 🍌 trong ví.")
        if not prediction:
            return await update.message.reply_text("❌ Phải nhập nội dung kèo cược. VD: `/bet 1 2 Tài góc`")

        active = get_flattened_board()
        if idx >= len(active) or idx < 0: return await update.message.reply_text("❌ Số thứ tự trận không đúng (Kiểm tra lại /board).")
        
        m = active[idx]
        if m.get("bet"): return await update.message.reply_text("❌ Trận này Ông chủ đã cược rồi! Mỗi trận chỉ cược 1 lần.")
        
        # Tìm và update đúng trận trong state
        for b_match in state["boards"][m["date"]]:
            if b_match["id"] == m["id"] and b_match.get("sport") == m.get("sport"):
                b_match["bet"] = {"amount": amount, "prediction": prediction, "status": "pending"}
                state["wallet"]["bananas"] -= amount
                save_data()
                await update.message.reply_text(f"🎲 **ĐÃ CHỐT KÈO!**\n- Trận: {m['home']} vs {m['away']}\n- Cược: {amount} 🍌 vào kèo *'{prediction}'*\n💰 Số dư còn: {state['wallet']['bananas']} 🍌", parse_mode="Markdown")
                return
    except Exception:
        await update.message.reply_text("❌ Lỗi cú pháp! HD: `/bet[STT] [Số_Chuối] [Kèo]`\nVD: `/bet 1 2 Đội khách thắng`", parse_mode="Markdown")

# ===== 6. QUẢN LÝ BẢNG TỔNG & LỊCH SỬ =====
async def board_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = get_flattened_board()
    if not active: return await update.message.reply_text("📭 Board trống.")
    res = f"📊 **BẢNG THEO DÕI TỔNG:**\n"
    for i, m in enumerate(active):
        res += f"{i+1}. ⏳[{m.get('time')}] *{m.get('icon','⚽')} {m['home']} vs {m['away']}*\n"
        if m.get("bet"): res += f"   └ 🎲 Cược: {m['bet']['amount']} 🍌 -> _{m['bet']['prediction']}_\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matches =[m for d_m in state["boards"].values() for m in d_m if m.get("notified")]
    matches.sort(key=lambda x: x.get("timestamp", 0), reverse=True) 
    if not matches: return await update.message.reply_text("📭 Chưa có trận nào kết thúc.")
    res = "📜 **LỊCH SỬ KẾT QUẢ & CƯỢC:**\n"
    for i, m in enumerate(matches[:20]): 
        res += f"{i+1}. ✅ {m.get('icon')} {m['home']} {m.get('score')} {m['away']}\n"
        if m.get("bet"):
            stt = "🟢 THẮNG" if m['bet'].get('status') == 'win' else "🔴 THUA"
            res += f"   └ Kèo: '{m['bet']['prediction']}' -> {stt}\n"
    await update.message.reply_text(res, parse_mode="Markdown")

# ===== 7. NHẮC VIỆC & BÁO CÁO =====
async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        t_str, c = context.args[0], " ".join(context.args[1:])
        state["tasks"].append({"time": t_str, "content": c, "reminded": False, "note": "", "date": datetime.now(VN_TZ).strftime("%Y-%m-%d")})
        save_data()
        await update.message.reply_text(f"➕ Đã thêm việc: *{c}*", parse_mode="Markdown")
    except: await update.message.reply_text("❌ HD: `/add 08:00 Việc`")

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        now = datetime.now(VN_TZ)
        t_str = now.strftime("%Y-%m-%d")
        
        # Thống kê cá cược trong ngày (Trận đã đá xong hôm nay)
        today_matches = state["boards"].get(t_str,[])
        bet_stats = ""
        for m in today_matches:
            if m.get("notified") and m.get("bet"):
                bet_stats += f"- {m['home']} vs {m['away']} | Cược: {m['bet']['prediction']} -> {m['bet']['status']}\n"

        prompt = f"Viết Báo Cáo Cuối Ngày. Tài sản hiện tại: {state['wallet']['bananas']} Chuối.\nHồ sơ: {' '.join(state.get('profile',[]))}\nThống kê cược hôm nay:\n{bet_stats or 'Không cá cược.'}"
        
        chat_id = update.effective_chat.id
        if chat_id not in chat_sessions: chat_sessions[chat_id] = ai_model.start_chat()
        resp = await asyncio.to_thread(chat_sessions[chat_id].send_message, prompt)
        await update.message.reply_text(f"📑 **BÁO CÁO NGÀY & KIỂM KÊ TÀI SẢN**\n\n{resp.text}", parse_mode="Markdown")
    except: await update.message.reply_text("❌ Lỗi báo cáo.")

# ===== 8. MONITOR (XỬ LÝ KẾT QUẢ TRẬN ĐẤU & TRẢ THƯỞNG) =====
async def monitor(context: ContextTypes.DEFAULT_TYPE):
    global last_api_check
    if not state.get("chat_id"): return
    now = datetime.now(VN_TZ)
    
    # Bơm chuối Thứ 2 hàng tuần (Nếu check thành công thì thông báo)
    if check_weekly_allowance() and now.weekday() == 0 and now.hour == 8 and now.minute == 0:
        await context.bot.send_message(state["chat_id"], f"💸 **TING TING!**\nĐầu tuần chúc Ông chủ rực rỡ! Hệ thống đã bơm tiền, số dư hiện tại của Ông chủ là {state['wallet']['bananas']} 🍌.")

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
                await context.bot.send_message(state["chat_id"], f"⏰ **SẮP ĐÁ (15p):** {m.get('icon')} {m['home']} vs {m['away']}")
                m["reminded_15m"] = True
                save_data()

    live_m =[m for m in active if "timestamp" in m and now.timestamp() >= m["timestamp"]]
    if now.timestamp() - last_api_check >= 600 and live_m:
        last_api_check = now.timestamp()
        for d in set(m.get("date") for m in live_m if m.get("date")):
            m_d =[m for m in live_m if m.get("date") == d]
            
            # Hàm xử lý kết quả và thanh toán cược
            async def process_finished_match(m, f_data, hg, ag):
                m["score"] = f"{hg}-{ag}"
                msg = f"🏁 **KẾT THÚC:** {m.get('icon')} {m['home']} {m['score']} {m['away']}"
                
                # NẾU CÓ CƯỢC -> GỌI AI LÀM TRỌNG TÀI
                if m.get("bet") and m["bet"]["status"] == "pending":
                    is_win = await ai_referee(m)
                    if is_win:
                        win_amount = m["bet"]["amount"] * 2
                        state["wallet"]["bananas"] += win_amount
                        m["bet"]["status"] = "win"
                        msg += f"\n\n🟢 **CHÚC MỪNG!** AI Trọng Tài phán quyết kèo '{m['bet']['prediction']}' đã THẮNG.\nThu về: +{win_amount} 🍌. Số dư: {state['wallet']['bananas']} 🍌."
                    else:
                        m["bet"]["status"] = "lose"
                        msg += f"\n\n🔴 **RẤT TIẾC!** Kèo '{m['bet']['prediction']}' đã THUA.\nTrắng tay. Số dư: {state['wallet']['bananas']} 🍌."
                
                m["notified"] = True
                save_data()
                await context.bot.send_message(state["chat_id"], msg)

            if any(m.get("sport") == "f" for m in m_d):
                try:
                    res = await client.get(f"https://v3.football.api-sports.io/fixtures?date={d}{TZ_PARAM}")
                    f_map = {f["fixture"]["id"]: f for f in res.json().get("response",[])}
                    for m in m_d:
                        if m.get("sport") == "f" and m["id"] in f_map:
                            f_data = f_map[m["id"]]
                            if f_data["fixture"]["status"]["short"] in["FT", "AET", "PEN"]:
                                hg, ag = f_data['goals']['home'] or 0, f_data['goals']['away'] or 0
                                await process_finished_match(m, f_data, hg, ag)
                except Exception as e: logging.error(f"Lỗi Monitor F: {e}")

            if any(m.get("sport") in ['n','b'] for m in m_d):
                try:
                    ep = "v2.nba.api-sports.io" if any(m.get("sport")=='n' for m in m_d) else "v1.basketball.api-sports.io"
                    res = await client.get(f"https://{ep}/games?date={d}{TZ_PARAM}")
                    b_map = {b["id"]: b for b in res.json().get("response",[])}
                    for m in m_d:
                        if m.get("sport") in ['n','b'] and m["id"] in b_map:
                            b_data = b_map[m["id"]]
                            stat_code = str(b_data["status"]["short"])
                            if stat_code in["3", "FT", "AOT"]:
                                key = 'points' if m.get("sport") == 'n' else 'total'
                                hg = b_data['scores']['home'].get(key) or 0
                                ag = b_data['scores']['away'].get(key) or 0
                                await process_finished_match(m, b_data, hg, ag)
                except Exception as e: logging.error(f"Lỗi Monitor B/N: {e}")

# ===== 9. MAIN =====
def main():
    load_data()
    app = ApplicationBuilder().token(BOT_TOKEN).defaults(Defaults(tzinfo=VN_TZ)).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("wallet", wallet_cmd)) 
    app.add_handler(CommandHandler("bet", bet_cmd)) 
    app.add_handler(CommandHandler("learn", learn_cmd)) 
    app.add_handler(CommandHandler("summary", summary_cmd)) 
    app.add_handler(CommandHandler("add", add_task))
    app.add_handler(CommandHandler("matches", matches_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("time", time_cmd)) 
    app.add_handler(CommandHandler("board", board_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_chat_handler))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    if app.job_queue: app.job_queue.run_repeating(monitor, interval=60, first=10)
    print("🚀 SUPREME AI COMMANDER V13.0 (CASINO 🍌) ĐÃ SẴN SÀNG!")
    app.run_polling()

if __name__ == "__main__": main()
