
import os
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands

# =========================================================
# MJSSモン 完全部入り防御型スターター
# - 日本時間固定
# - master txt 読み込み
# - 画像チェック / 管理ログ
# - セーブ5枠 / 削除 / チェッカー
# - たまご / 孵化 / 育成ループ / ごはん / あそぶ / トレーニング / 睡眠
# - お世話ミス / ポンまで進化 / 成熟期分岐
# - 転生 / ガチャ / 図鑑 / 見た目変更 / 疑似PvP / タッグ進化画面
# =========================================================

# =========================
# 日本時間固定
# =========================
JST = timezone(timedelta(hours=9))

def now_jst() -> datetime:
    return datetime.now(JST)

def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(JST)

def dt_to_str(dt: datetime) -> str:
    return dt.astimezone(JST).isoformat()

def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")

# =========================
# 環境変数
# =========================
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0"))
CHARACTER_CHANNEL_ID = int(os.getenv("CHARACTER_CHANNEL_ID", "0"))
IMAGE_CHANNEL_ID = int(os.getenv("IMAGE_CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

SAVE_VERSION = 3
MAX_SLOTS = 5
REQUIRED_STATES = ["通常", "ごはん", "よろこび", "ねむい", "体調不良"]

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# DB
# =========================
DB_PATH = "mjss_mon.db"

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS profiles (
        user_id TEXT PRIMARY KEY,
        tamer_level INTEGER NOT NULL DEFAULT 1,
        coins INTEGER NOT NULL DEFAULT 500,
        gacha_tickets INTEGER NOT NULL DEFAULT 3,
        reincarnations INTEGER NOT NULL DEFAULT 0,
        total_wins INTEGER NOT NULL DEFAULT 0,
        total_losses INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS save_slots (
        user_id TEXT NOT NULL,
        slot_no INTEGER NOT NULL,
        save_version INTEGER NOT NULL DEFAULT 1,
        save_status TEXT NOT NULL DEFAULT 'normal',
        slot_name TEXT,
        current_character_id TEXT,
        current_character_name TEXT,
        current_stage TEXT,
        visual_name TEXT,
        state_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        thread_id TEXT DEFAULT '',
        status_message_id TEXT DEFAULT '',
        PRIMARY KEY (user_id, slot_no)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS dex_entries (
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        character_name TEXT NOT NULL,
        unlocked_at TEXT NOT NULL,
        PRIMARY KEY (user_id, character_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS unlocked_visuals (
        user_id TEXT NOT NULL,
        visual_name TEXT NOT NULL,
        unlocked_at TEXT NOT NULL,
        PRIMARY KEY (user_id, visual_name)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tag_bonds (
        user_id TEXT NOT NULL,
        partner_id TEXT NOT NULL,
        bond INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, partner_id)
    )
    """)
    cur.execute("PRAGMA table_info(save_slots)")
    save_slot_cols = {row["name"] for row in cur.fetchall()}
    if "thread_id" not in save_slot_cols:
        cur.execute("ALTER TABLE save_slots ADD COLUMN thread_id TEXT DEFAULT ''")
    if "status_message_id" not in save_slot_cols:
        cur.execute("ALTER TABLE save_slots ADD COLUMN status_message_id TEXT DEFAULT ''")
    conn.commit()
    conn.close()

def ensure_profile(user_id: str):
    conn = db()
    cur = conn.cursor()
    now = dt_to_str(now_jst())
    cur.execute("SELECT user_id FROM profiles WHERE user_id=?", (user_id,))
    if not cur.fetchone():
        cur.execute("""
        INSERT INTO profiles (user_id, created_at, updated_at)
        VALUES (?, ?, ?)
        """, (user_id, now, now))
        for slot_no in range(1, MAX_SLOTS + 1):
            cur.execute("""
            INSERT OR IGNORE INTO save_slots (
                user_id, slot_no, save_version, save_status, slot_name,
                current_character_id, current_character_name, current_stage,
                visual_name, state_json, created_at, updated_at, thread_id, status_message_id
            ) VALUES (?, ?, ?, 'empty', ?, '', '', '', '', '', ?, ?, '', '')
            """, (user_id, slot_no, SAVE_VERSION, f"セーブ{slot_no}", now, now))
    conn.commit()
    conn.close()

def get_profile(user_id: str) -> sqlite3.Row:
    ensure_profile(user_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def update_profile(user_id: str, **kwargs):
    if not kwargs:
        return
    ensure_profile(user_id)
    allowed = {"tamer_level", "coins", "gacha_tickets", "reincarnations", "total_wins", "total_losses"}
    keys = [k for k in kwargs.keys() if k in allowed]
    if not keys:
        return
    conn = db()
    cur = conn.cursor()
    parts = [f"{k}=?" for k in keys]
    vals = [kwargs[k] for k in keys]
    parts.append("updated_at=?")
    vals.append(dt_to_str(now_jst()))
    vals.append(user_id)
    cur.execute(f"UPDATE profiles SET {', '.join(parts)} WHERE user_id=?", vals)
    conn.commit()
    conn.close()

def list_slots(user_id: str) -> List[sqlite3.Row]:
    ensure_profile(user_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM save_slots WHERE user_id=? ORDER BY slot_no", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_slot(user_id: str, slot_no: int) -> sqlite3.Row:
    ensure_profile(user_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM save_slots WHERE user_id=? AND slot_no=?", (user_id, slot_no))
    row = cur.fetchone()
    conn.close()
    return row

def save_slot(user_id: str, slot_no: int, state: dict, status: str = "normal", slot_name: Optional[str] = None):
    ensure_profile(user_id)
    conn = db()
    cur = conn.cursor()
    now = dt_to_str(now_jst())
    if slot_name is None:
        old = get_slot(user_id, slot_no)
        slot_name = old["slot_name"] if old else f"セーブ{slot_no}"
    cur.execute("""
    UPDATE save_slots
    SET save_version=?, save_status=?, slot_name=?, current_character_id=?, current_character_name=?,
        current_stage=?, visual_name=?, state_json=?, updated_at=?
    WHERE user_id=? AND slot_no=?
    """, (
        SAVE_VERSION,
        status,
        slot_name,
        state.get("character_id", ""),
        state.get("character_name", ""),
        state.get("stage", ""),
        state.get("visual_name", ""),
        json.dumps(state, ensure_ascii=False),
        now,
        user_id,
        slot_no,
    ))
    conn.commit()
    conn.close()

def set_slot_thread_info(user_id: str, slot_no: int, thread_id: Optional[int], status_message_id: Optional[int]):
    ensure_profile(user_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE save_slots
    SET thread_id=?, status_message_id=?, updated_at=?
    WHERE user_id=? AND slot_no=?
    """, (
        str(thread_id) if thread_id else "",
        str(status_message_id) if status_message_id else "",
        dt_to_str(now_jst()),
        user_id,
        slot_no,
    ))
    conn.commit()
    conn.close()

def clear_slot(user_id: str, slot_no: int):
    ensure_profile(user_id)
    conn = db()
    cur = conn.cursor()
    now = dt_to_str(now_jst())
    cur.execute("""
    UPDATE save_slots
    SET save_version=?, save_status='empty', current_character_id='', current_character_name='',
        current_stage='', visual_name='', state_json='', updated_at=?, thread_id='', status_message_id=''
    WHERE user_id=? AND slot_no=?
    """, (SAVE_VERSION, now, user_id, slot_no))
    conn.commit()
    conn.close()

def unlock_dex(user_id: str, character_id: str, character_name: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT OR IGNORE INTO dex_entries (user_id, character_id, character_name, unlocked_at)
    VALUES (?, ?, ?, ?)
    """, (user_id, character_id, character_name, dt_to_str(now_jst())))
    conn.commit()
    conn.close()

def unlock_visual(user_id: str, visual_name: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT OR IGNORE INTO unlocked_visuals (user_id, visual_name, unlocked_at)
    VALUES (?, ?, ?)
    """, (user_id, visual_name, dt_to_str(now_jst())))
    conn.commit()
    conn.close()

def list_dex(user_id: str) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM dex_entries WHERE user_id=? ORDER BY unlocked_at", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def list_visuals(user_id: str) -> List[str]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT visual_name FROM unlocked_visuals WHERE user_id=? ORDER BY visual_name", (user_id,))
    rows = [r["visual_name"] for r in cur.fetchall()]
    conn.close()
    return rows

# =========================
# マスターデータ
# =========================
characters_data: Dict[str, dict] = {}
evolution_data: List[dict] = []
image_map: Dict[str, str] = {}

def parse_master_blocks(text: str, marker: str) -> List[dict]:
    chunks = [x.strip() for x in text.split(marker) if x.strip()]
    rows = []
    for chunk in chunks:
        data = {}
        for line in chunk.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            data[k.strip()] = v.strip()
        if data:
            rows.append(data)
    return rows

async def read_latest_attachment_text(channel: discord.TextChannel, filename: str):
    latest = None
    async for message in channel.history(limit=100):
        for att in message.attachments:
            if att.filename == filename:
                latest = att
                break
        if latest:
            break
    if not latest:
        return None, f"{filename} が見つからない"
    raw = await latest.read()
    for enc in ("utf-8", "utf-8-sig"):
        try:
            return raw.decode(enc), None
        except Exception:
            pass
    return None, f"{filename} の文字コードが読めない"

async def load_characters_master(channel: discord.TextChannel):
    global characters_data
    text, err = await read_latest_attachment_text(channel, "characters_master.txt")
    if err:
        characters_data = {}
        return False, err
    rows = parse_master_blocks(text, "===CHARACTER===")
    data = {}
    for row in rows:
        cid = row.get("character_id")
        if cid:
            data[cid] = row
    characters_data = data
    return True, f"{len(characters_data)}件"

async def load_evolution_master(channel: discord.TextChannel):
    global evolution_data
    text, err = await read_latest_attachment_text(channel, "evolution_master.txt")
    if err:
        evolution_data = []
        return False, err
    evolution_data = parse_master_blocks(text, "===EVOLUTION===")
    return True, f"{len(evolution_data)}件"


def _extract_declared_name_from_message(message: discord.Message) -> str:
    content = (message.content or "").strip()
    if not content:
        return ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        low = line.lower()
        if low.startswith("name:") or low.startswith("name："):
            parts = re.split(r"[:：]", line, maxsplit=1)
            if len(parts) == 2:
                return parts[1].strip()
    return ""

def _register_image_alias(name: str, url: str):
    name = (name or "").strip()
    if not name:
        return
    image_map[name] = url

    # ありがちな表記ゆれも少し吸う
    compact = name.replace(" ", "").replace("　", "")
    image_map[compact] = url

    # GIFやスマホ投稿で name: がある場合に、基本名も吸う
    if compact.startswith("name:"):
        image_map[compact.split(":", 1)[1].strip()] = url

def _state_aliases(base_name: str) -> List[str]:
    aliases = []
    compact = (base_name or "").replace(" ", "").replace("　", "")
    if not compact:
        return aliases
    # 幼年期の略称画像を、通常状態の別名としても拾えるようにする
    if compact in ("MJ", "SS", "ポン"):
        aliases.append(f"{compact}通常")
    return aliases

async def load_images_from_channel(channel: discord.TextChannel):
    count = 0
    async for message in channel.history(limit=1000):
        declared_name = _extract_declared_name_from_message(message)
        for att in message.attachments:
            base = att.filename.rsplit(".", 1)[0].strip()
            _register_image_alias(base, att.url)
            for alias in _state_aliases(base):
                _register_image_alias(alias, att.url)
            if declared_name:
                _register_image_alias(declared_name, att.url)
                for alias in _state_aliases(declared_name):
                    _register_image_alias(alias, att.url)
            count += 1
    return count

async def load_images(image_channel: Optional[discord.TextChannel], character_channel: Optional[discord.TextChannel] = None):
    global image_map
    image_map = {}
    count = 0

    if image_channel:
        count += await load_images_from_channel(image_channel)

    # たまご・手紙・キャラ画像がキャラクター管理チャンネル側にあるケースも吸う
    if character_channel and (not image_channel or character_channel.id != image_channel.id):
        count += await load_images_from_channel(character_channel)

    return True, f"{count}件"


def _uploaded_character_bases() -> List[str]:
    bases = set()
    for key in image_map.keys():
        if key in ("たまご", "手紙"):
            continue
        for state in REQUIRED_STATES:
            if key.endswith(state):
                bases.add(key[: -len(state)])
                break
    return sorted(bases)

def expected_image_names() -> List[str]:
    # いま実際に画像があるキャラだけをチェック対象にする
    # これで MJ / SS 画像をまだ入れてなくても不足扱いにしない
    names = {"たまご", "手紙"}
    for base in _uploaded_character_bases():
        for state in REQUIRED_STATES:
            names.add(f"{base}{state}")
    return sorted(names)

def check_missing_images() -> List[str]:
    return [name for name in expected_image_names() if name not in image_map]

# =========================
# ログ
# =========================
async def send_log(text: str):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        await channel.send(f"```{text}```")
    else:
        print(text)

# =========================
# 育成
# =========================
def to_int(row: dict, key: str, default: int = 0) -> int:
    try:
        return int(row.get(key, default))
    except Exception:
        return default

def make_state(character_id: str) -> dict:
    row = characters_data[character_id]
    t = now_jst()
    return {
        "character_id": character_id,
        "character_name": row.get("name", character_id),
        "stage": row.get("stage", ""),
        "visual_name": row.get("name", character_id),
        "ui_state": "通常",
        "is_sleeping": False,
        "sleep_quality": 0,
        "care_miss": 0,
        "age_minutes": 0,
        "life": 3,
        "mental_endurance": to_int(row, "base_mental_endurance", 3),
        "condition": to_int(row, "base_condition", 5),
        "professionalism": to_int(row, "base_professionalism", 1),
        "stress": to_int(row, "base_stress", 0),
        "influence": to_int(row, "base_influence", 0),
        "training_count": to_int(row, "base_training_count", 0),
        "stamina": to_int(row, "base_stamina", 10),
        "expression": to_int(row, "base_expression", 10),
        "performance": to_int(row, "base_performance", 10),
        "stability": to_int(row, "base_stability", 10),
        "response": to_int(row, "base_response", 10),
        "intelligence": to_int(row, "base_intelligence", 10),
        "wins": 0,
        "losses": 0,
        "last_update": dt_to_str(t),
        "created_at": dt_to_str(t),
        "last_action_text": "誕生した",
        "last_reincarnation_points": 0,
    }

def load_state(slot_row: sqlite3.Row) -> Optional[dict]:
    raw = slot_row["state_json"]
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def get_sleep_window(stage: str) -> Tuple[int, int]:
    # デジモン寄りの固定感を優先
    if stage in ("mj", "ss"):
        return (21, 8)
    if stage in ("pon", "adult"):
        return (23, 8)
    return (22, 8)

def is_sleep_time(stage: str, now: datetime) -> bool:
    start, end = get_sleep_window(stage)
    if start < end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end

def update_ui_state(state: dict):
    if state.get("life", 1) <= 0:
        state["ui_state"] = "体調不良"
        return

    if state.get("is_sleeping", False):
        state["ui_state"] = "ねむい"
        return

    if is_sleep_time(state["stage"], now_jst()):
        state["ui_state"] = "ねむい"
        return

    if state.get("stress", 0) >= 5 or state.get("condition", 5) <= 1:
        state["ui_state"] = "体調不良"
        return

    if state.get("ui_state") in ("ごはん", "よろこび"):
        return

    state["ui_state"] = "通常"


def state_image_name(state: dict) -> str:
    if is_letter_state(state):
        if "手紙" in image_map:
            return "手紙"
    if state.get("stage") == "egg":
        if "たまご" in image_map:
            return "たまご"

    visual_name = (state.get("visual_name") or "").strip()
    character_name = (state.get("character_name") or "").strip()
    current = state.get("ui_state", "通常")

    candidates = [
        f"{visual_name}{current}" if visual_name else "",
        f"{character_name}{current}" if character_name else "",
        visual_name if visual_name else "",
        character_name if character_name else "",
        f"{visual_name}通常" if visual_name else "",
        f"{character_name}通常" if character_name else "",
    ]

    if state.get("stage") in ("mj", "ss", "pon"):
        candidates.append("たまご")

    for name in candidates:
        if name and name in image_map:
            return name

    # 部分一致も最後に試す
    for key in image_map.keys():
        if visual_name and visual_name in key and current in key:
            return key
        if character_name and character_name in key and current in key:
            return key

    return "たまご" if "たまご" in image_map else (candidates[0] if candidates and candidates[0] else "")

def apply_time_passage(state: dict) -> dict:
    now = now_jst()
    last = parse_dt(state["last_update"])
    elapsed = int((now - last).total_seconds() // 60)
    if elapsed <= 0:
        update_ui_state(state)
        return state

    for _ in range(elapsed):
        state["age_minutes"] += 1

        if state["stage"] != "egg":
            if not state.get("is_sleeping"):
                if state["age_minutes"] % 20 == 0:
                    state["condition"] = max(0, state["condition"] - 1)
                if state["age_minutes"] % 30 == 0:
                    state["stress"] = min(9, state["stress"] + 1)
                if state["age_minutes"] % 45 == 0:
                    state["influence"] = max(0, state["influence"] - 1)
                if is_sleep_time(state["stage"], now):
                    if state["age_minutes"] % 30 == 0:
                        state["care_miss"] += 1
                        state["stress"] = min(9, state["stress"] + 1)
            else:
                if state["age_minutes"] % 30 == 0:
                    state["sleep_quality"] += 1
                    state["stress"] = max(0, state["stress"] - 1)
                    state["condition"] = min(5, state["condition"] + 1)

        if state["condition"] <= 0 and state["age_minutes"] % 30 == 0:
            state["care_miss"] += 1
            state["life"] = max(0, state["life"] - 1)

        if state["stage"] == "adult" and state["age_minutes"] >= 720:
            # 寿命の簡易土台
            state["life"] = 0

    state["last_update"] = dt_to_str(now)
    update_ui_state(state)
    return state

def meets_condition(value: int, min_v: int, max_v: int) -> bool:
    return min_v <= value <= max_v

def get_evolution_candidates(from_id: str) -> List[dict]:
    rows = [r for r in evolution_data if r.get("from") == from_id]
    rows.sort(key=lambda x: int(x.get("priority", "0")), reverse=True)
    return rows

def pick_evolution(state: dict) -> Optional[str]:
    candidates = get_evolution_candidates(state["character_id"])
    if not candidates:
        return None

    created = parse_dt(state["created_at"])
    passed = int((now_jst() - created).total_seconds())
    for evo in candidates:
        if passed < int(evo.get("time_required", "0")):
            continue
        checks = [
            ("influence", "condition_influence_min", "condition_influence_max"),
            ("professionalism", "condition_professionalism_min", "condition_professionalism_max"),
            ("stress", "condition_stress_min", "condition_stress_max"),
            ("training_count", "condition_training_min", "condition_training_max"),
            ("care_miss", "condition_miss_min", "condition_miss_max"),
            ("sleep_quality", "condition_sleep_quality_min", "condition_sleep_quality_max"),
        ]
        ok = True
        for state_key, min_key, max_key in checks:
            value = int(state.get(state_key, 0))
            min_v = int(evo.get(min_key, "0"))
            max_v = int(evo.get(max_key, "999"))
            if not meets_condition(value, min_v, max_v):
                ok = False
                break
        if ok:
            return evo.get("to")
    return None

def perform_evolution(state: dict, next_id: str) -> dict:
    row = characters_data[next_id]
    keep = {
        "influence": state["influence"],
        "professionalism": state["professionalism"],
        "stress": state["stress"],
        "training_count": state["training_count"],
        "care_miss": state["care_miss"],
        "sleep_quality": state["sleep_quality"],
        "wins": state["wins"],
        "losses": state["losses"],
        "life": state["life"],
    }
    new_state = make_state(next_id)
    new_state.update(keep)
    new_state["created_at"] = dt_to_str(now_jst())
    new_state["last_update"] = dt_to_str(now_jst())
    new_state["last_action_text"] = f"{row.get('name', next_id)}に進化した"
    update_ui_state(new_state)
    return new_state

def check_and_apply_evolution(state: dict) -> Tuple[dict, Optional[str]]:
    nxt = pick_evolution(state)
    if not nxt or nxt == "adult_split":
        return state, None
    if nxt in characters_data:
        evolved = perform_evolution(state, nxt)
        return evolved, nxt
    return state, None

def calc_reincarnation_points(state: dict) -> int:
    points = 0
    stage = state.get("stage", "")
    if stage == "adult":
        points += 5
    elif stage == "pon":
        points += 3
    elif stage == "ss":
        points += 2
    elif stage == "mj":
        points += 1
    points += min(5, state.get("wins", 0))
    points += max(0, 3 - min(3, state.get("care_miss", 0)))
    return points

def apply_reincarnation(user_id: str, slot_no: int, state: dict):
    points = calc_reincarnation_points(state)
    profile = get_profile(user_id)
    update_profile(
        user_id,
        reincarnations=profile["reincarnations"] + 1,
        coins=profile["coins"] + points * 50,
        gacha_tickets=profile["gacha_tickets"] + 1,
    )
    unlock_dex(user_id, state["character_id"], state["character_name"])
    unlock_visual(user_id, state["character_name"])
    egg = make_state("egg")
    egg["last_reincarnation_points"] = points
    egg["last_action_text"] = f"{state['character_name']}は手紙を残して旅立った"
    save_slot(user_id, slot_no, egg, status="normal")

def validate_state(state: dict) -> Tuple[str, List[str], dict]:
    problems = []
    if not isinstance(state, dict):
        return "broken", ["state_jsonが壊れてる"], {}
    required = [
        "character_id", "character_name", "stage", "visual_name", "last_update",
        "created_at", "life", "condition", "professionalism", "stress",
        "influence", "training_count", "care_miss", "sleep_quality",
        "stamina", "expression", "performance", "stability", "response", "intelligence"
    ]
    for key in required:
        if key not in state:
            problems.append(f"{key} がない")
    cid = state.get("character_id")
    if cid and cid not in characters_data:
        problems.append(f"{cid} が characters_master にない")

    repaired = dict(state)
    if "visual_name" not in repaired and repaired.get("character_name"):
        repaired["visual_name"] = repaired["character_name"]
    if "last_update" not in repaired:
        repaired["last_update"] = dt_to_str(now_jst())
    if "created_at" not in repaired:
        repaired["created_at"] = dt_to_str(now_jst())

    if not problems:
        return "normal", [], repaired

    light_only = all("がない" in p for p in problems)
    if light_only and cid in characters_data:
        for k, v in make_state(cid).items():
            repaired.setdefault(k, v)
        return "migrated", problems, repaired

    return "broken", problems, repaired

# =========================
# 表示
# =========================

def _clamp(value: int, min_v: int, max_v: int) -> int:
    return max(min_v, min(value, max_v))


def care_bar(value: int, max_value: int = 5, full: str = "■", empty: str = "□") -> str:
    value = _clamp(int(value), 0, max_value)
    return full * value + empty * (max_value - value)


def stat_bar(value: int, scale: int = 5, cap: int = 999) -> str:
    if value <= 0:
        filled = 0
    else:
        filled = max(1, round((_clamp(int(value), 0, cap) / cap) * scale))
    return care_bar(filled, scale, "▰", "▱")


def life_hearts(value: int) -> str:
    value = _clamp(int(value), 0, 3)
    return "♥" * value + "♡" * (3 - value)


def sleep_status_text(state: dict) -> str:
    if state.get("is_sleeping", False):
        return "睡眠中"
    if is_sleep_time(state["stage"], now_jst()):
        return "ねむい"
    return "起きてる"


def is_egg_state(state: dict) -> bool:
    return state.get("stage") == "egg"

def is_letter_state(state: dict) -> bool:
    return state.get("life", 1) <= 0 and state.get("stage") != "egg"

def is_non_care_state(state: dict) -> bool:
    return is_egg_state(state) or is_letter_state(state)


def build_status_embed(user_id: str, slot_no: int, state: dict) -> discord.Embed:
    profile = get_profile(user_id)
    image_name = state_image_name(state)
    image_url = image_map.get(image_name, "")

    title_name = "手紙" if is_letter_state(state) else state["character_name"]
    stage_text = "letter" if is_letter_state(state) else state["stage"]
    state_text = "おわかれ" if is_letter_state(state) else state["ui_state"]

    embed = discord.Embed(
        title=f"【セーブ{slot_no}】{title_name}",
        description=(
            f"段階: **{stage_text}**\n"
            f"表示: **{state['visual_name']}**\n"
            f"状態: **{state_text}**\n"
            f"最終更新(JST): {fmt_dt(parse_dt(state['last_update']))}"
        ),
        color=discord.Color.magenta(),
    )

    if is_egg_state(state):
        care_value = (
            "🥚 たまごの状態\n"
            "まだごはん・あそぶ・トレーニングはできない。\n"
            "時間がたつと孵化や進化に進む。"
        )
    elif is_letter_state(state):
        care_value = (
            "✉️ おわかれ状態\n"
            "この状態ではお世話できない。\n"
            "下の『卵にもどる』で次の育成を始める。"
        )
    else:
        care_value = (
            f"❤️ ライフ {life_hearts(state['life'])}\n"
            f"🍖 コンディション {care_bar(state['condition'], 5)}\n"
            f"🎤 ノリ {care_bar(_clamp(state['influence'], 0, 5), 5)}\n"
            f"🧠 しつけ {care_bar(_clamp(state['professionalism'], 0, 5), 5)}\n"
            f"💢 ストレス {care_bar(_clamp(state['stress'], 0, 5), 5)}\n"
            f"😴 ねむけ {sleep_status_text(state)}\n"
            f"⚠️ お世話ミス {state['care_miss']}回"
        )

    embed.add_field(name="お世話", value=care_value, inline=False)

    embed.add_field(
        name="育成メモ",
        value=(
            f"✨ 努力回数 {state['training_count']}回\n"
            f"🛌 睡眠品質 {state['sleep_quality']}\n"
            f"📝 前回: {state.get('last_action_text', '-')}"
        ),
        inline=False,
    )

    embed.add_field(
        name="バトル能力",
        value=(
            f"体力 {state['stamina']} {stat_bar(state['stamina'])}\n"
            f"表現 {state['expression']} {stat_bar(state['expression'])}\n"
            f"歌唱 {state['performance']} {stat_bar(state['performance'])}\n"
            f"安定 {state['stability']} {stat_bar(state['stability'])}\n"
            f"反応 {state['response']} {stat_bar(state['response'])}\n"
            f"知性 {state['intelligence']} {stat_bar(state['intelligence'])}"
        ),
        inline=False,
    )

    embed.set_footer(
        text=f"コイン: {profile['coins']} / ガチャ券: {profile['gacha_tickets']} / 転生回数: {profile['reincarnations']}"
    )

    if image_url:
        embed.set_image(url=image_url)
    else:
        embed.add_field(name="画像", value=f"{image_name}（未読込）", inline=False)

    return embed


def get_slot_summary(slot_row: sqlite3.Row) -> str:
    status = slot_row["save_status"]
    if status == "empty":
        return f"セーブ{slot_row['slot_no']} / 空き"
    name = slot_row["current_character_name"] or "不明"
    stage = slot_row["current_stage"] or "不明"
    updated = slot_row["updated_at"]
    return f"セーブ{slot_row['slot_no']} / {name} / {stage} / {updated[:16]} / {status}"

def build_state_text(user_id: str, slot_no: int, state: dict) -> str:
    profile = get_profile(user_id)
    return (
        f"【セーブ{slot_no}】{state['character_name']}\n"
        f"段階: {state['stage']}\n"
        f"表示: {state['visual_name']}\n"
        f"状態: {state['ui_state']}\n"
        f"最終更新(JST): {fmt_dt(parse_dt(state['last_update']))}\n\n"
        f"--- 育成 ---\n"
        f"メンタル耐久: {state['mental_endurance']}\n"
        f"コンディション: {state['condition']}\n"
        f"プロ意識: {state['professionalism']}\n"
        f"ストレス: {state['stress']}\n"
        f"影響力: {state['influence']}\n"
        f"努力回数: {state['training_count']}\n"
        f"お世話ミス: {state['care_miss']}\n"
        f"睡眠品質: {state['sleep_quality']}\n"
        f"ライフ: {state['life']}\n\n"
        f"--- バトル ---\n"
        f"スタミナ: {state['stamina']}\n"
        f"表現力: {state['expression']}\n"
        f"パフォーマンス: {state['performance']}\n"
        f"安定感: {state['stability']}\n"
        f"対応力: {state['response']}\n"
        f"理解力: {state['intelligence']}\n\n"
        f"前回: {state.get('last_action_text', '-')}\n"
        f"コイン: {profile['coins']} / ガチャ券: {profile['gacha_tickets']}\n"
        f"転生回数: {profile['reincarnations']}"
    )

def build_load_log_text(c_ok, c_msg, e_ok, e_msg, i_ok, i_msg) -> str:
    text = "【読み込みチェック】\n\n"
    text += f"実行時刻（JST）: {fmt_dt(now_jst())}\n\n"
    text += f"characters_master.txt → {'OK' if c_ok else 'NG'}（{c_msg}）\n"
    text += f"evolution_master.txt → {'OK' if e_ok else 'NG'}（{e_msg}）\n"
    text += f"画像読み込み → {'OK' if i_ok else 'NG'}（{i_msg}）\n\n"
    sample_keys = sorted(image_map.keys())[:40]
    if sample_keys:
        text += "読めた画像名（一部）:\n"
        for k in sample_keys:
            text += f"・{k}\n"
        text += "\n"
    if c_ok and i_ok:
        missing = check_missing_images()
        if missing:
            text += "不足画像：\n"
            for item in missing:
                text += f"・{item}\n"
            text += "\n読み込み未完了"
        elif c_ok and e_ok and i_ok:
            text += "すべての読み込み完了"
        else:
            text += "読み込み未完了"
    else:
        text += "読み込み未完了"
    return text

# =========================
# 読み込み
# =========================
async def full_reload():
    char_channel = bot.get_channel(CHARACTER_CHANNEL_ID)
    image_channel = bot.get_channel(IMAGE_CHANNEL_ID)

    if not char_channel:
        await send_log("【読み込みチェック】\n\nCHARACTER_CHANNEL_ID のチャンネルが見つからない。")
        return

    c_ok, c_msg = await load_characters_master(char_channel)
    e_ok, e_msg = await load_evolution_master(char_channel)
    i_ok, i_msg = await load_images(image_channel, char_channel)

    await send_log(build_load_log_text(c_ok, c_msg, e_ok, e_msg, i_ok, i_msg))

# =========================
# パネル / スレッド / メイン表示
# =========================

async def ensure_panel():
    channel = bot.get_channel(PANEL_CHANNEL_ID)
    if not channel:
        return

    # persistent view を使わず、起動時に古いパネルを消して新しいパネルを置き直す
    # これで custom_id 問題を避けつつ、毎回 setup_panel を打たなくてよくする
    async for msg in channel.history(limit=50):
        if msg.author != bot.user:
            continue
        if msg.content in ("MJSSモン操作パネル", "管理パネル", "パネル設置完了。"):
            try:
                await msg.delete()
            except Exception:
                pass

    await channel.send("MJSSモン操作パネル", view=MainView())
    await channel.send("管理パネル", view=UtilityView())

async def get_or_create_slot_thread(panel_channel: discord.TextChannel, user: discord.User, slot_no: int) -> discord.Thread:
    slot = get_slot(str(user.id), slot_no)
    thread_id = slot["thread_id"] or ""
    if thread_id:
        existing = bot.get_channel(int(thread_id))
        if existing and isinstance(existing, discord.Thread):
            return existing

    thread = await panel_channel.create_thread(
        name=f"{user.display_name}_セーブ{slot_no}",
        type=discord.ChannelType.public_thread,
        auto_archive_duration=10080
    )
    return thread

async def get_or_create_status_message(thread: discord.Thread, user_id: str, slot_no: int) -> discord.Message:
    slot = get_slot(user_id, slot_no)
    message_id = slot["status_message_id"] or ""
    if message_id:
        try:
            return await thread.fetch_message(int(message_id))
        except Exception:
            pass

    msg = await thread.send("初期化中...")
    set_slot_thread_info(user_id, slot_no, thread.id, msg.id)
    return msg

async def refresh_status_message(user_id: str, slot_no: int):
    slot = get_slot(user_id, slot_no)
    thread_id = slot["thread_id"] or ""
    if not thread_id:
        return

    thread = bot.get_channel(int(thread_id))
    if not thread or not isinstance(thread, discord.Thread):
        return

    state = load_state(slot)
    if not state:
        return

    state = apply_time_passage(state)

    if not is_letter_state(state):
        state, evolved = check_and_apply_evolution(state)
        if evolved:
            unlock_dex(user_id, state["character_id"], state["character_name"])
            unlock_visual(user_id, state["character_name"])

    save_slot(user_id, slot_no, state)

    msg = await get_or_create_status_message(thread, user_id, slot_no)
    embed = build_status_embed(user_id, slot_no, state)
    await msg.edit(content=None, embed=embed, view=CareView(user_id, slot_no))

# =========================
# UI
# =========================
class SlotSelect(discord.ui.Select):
    def __init__(self, user_id: str, mode: str):
        self.user_id = user_id
        self.mode = mode
        slots = list_slots(user_id)
        options = []
        for row in slots:
            label = f"セーブ{row['slot_no']}"
            desc = get_slot_summary(row)[:100]
            options.append(discord.SelectOption(label=label, value=str(row["slot_no"]), description=desc))
        super().__init__(placeholder="セーブを選びなよ", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        slot_no = int(self.values[0])
        if self.mode == "start":
            modal = ConfirmStartView(self.user_id, slot_no)
            await interaction.response.send_message(f"セーブ{slot_no}ではじめるの？ 既存データがあったら上書きされるよ。", view=modal, ephemeral=True)
        elif self.mode == "continue":
            slot = get_slot(self.user_id, slot_no)
            if slot["save_status"] == "empty":
                await interaction.response.send_message("そこ空きなんだけど？ 先にはじめなよ。", ephemeral=True)
                return
            state = load_state(slot)
            if not state:
                await interaction.response.send_message("そのセーブ壊れてるっぽい。削除か修復が必要。", ephemeral=True)
                return
            status, problems, repaired = validate_state(state)
            if status == "broken":
                save_slot(self.user_id, slot_no, state, status="broken")
                await interaction.response.send_message(
                    f"要修復セーブなんだけど？\n" + "\n".join(f"・{p}" for p in problems),
                    ephemeral=True,
                )
                return
            if status == "migrated":
                save_slot(self.user_id, slot_no, repaired, status="migrated")
                await send_log(
                    f"【セーブ自動補正】\nユーザー: {self.user_id}\nセーブ: {slot_no}\n"
                    + "\n".join(f"・{p}" for p in problems)
                )
                state = repaired

            panel_channel = interaction.channel
            if not isinstance(panel_channel, discord.TextChannel):
                await interaction.response.send_message("パネルチャンネルから選びなよ。", ephemeral=True)
                return
            thread = await get_or_create_slot_thread(panel_channel, interaction.user, slot_no)
            msg = await get_or_create_status_message(thread, self.user_id, slot_no)
            set_slot_thread_info(self.user_id, slot_no, thread.id, msg.id)
            save_slot(self.user_id, slot_no, state)
            await refresh_status_message(self.user_id, slot_no)
            await interaction.response.send_message(
                f"{thread.mention} で続きから遊びなよ。",
                ephemeral=True,
            )
        elif self.mode == "delete":
            await interaction.response.send_message(
                f"セーブ{slot_no}を消したいの？ 本当に？",
                view=ConfirmDeleteView(self.user_id, slot_no),
                ephemeral=True,
            )
        elif self.mode == "check":
            slot = get_slot(self.user_id, slot_no)
            if slot["save_status"] == "empty":
                await interaction.response.send_message("空きセーブだよ。", ephemeral=True)
                return
            state = load_state(slot)
            status, problems, repaired = validate_state(state)
            txt = f"セーブ{slot_no} / 判定: {status}\n"
            if problems:
                txt += "\n".join(f"・{p}" for p in problems)
            else:
                txt += "問題なし"
            await interaction.response.send_message(txt, ephemeral=True)

class SlotPickerView(discord.ui.View):
    def __init__(self, user_id: str, mode: str):
        super().__init__(timeout=180)
        self.add_item(SlotSelect(user_id, mode))

class ConfirmStartView(discord.ui.View):
    def __init__(self, user_id: str, slot_no: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.slot_no = slot_no

    @discord.ui.button(label="はい", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = make_state("egg")
        state["last_action_text"] = "たまごを受け取った"
        save_slot(self.user_id, self.slot_no, state, status="normal")

        panel_channel = interaction.channel
        if not isinstance(panel_channel, discord.TextChannel):
            await interaction.response.send_message("パネルチャンネルからやって。", ephemeral=True)
            return
        thread = await get_or_create_slot_thread(panel_channel, interaction.user, self.slot_no)
        msg = await get_or_create_status_message(thread, self.user_id, self.slot_no)
        set_slot_thread_info(self.user_id, self.slot_no, thread.id, msg.id)
        await refresh_status_message(self.user_id, self.slot_no)

        await interaction.response.send_message(
            f"{thread.mention} を作ったよ。そこで育てなよ。",
            ephemeral=True,
        )

    @discord.ui.button(label="いいえ", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("びびったの？ また今度ね。", ephemeral=True)

class ConfirmDeleteView(discord.ui.View):
    def __init__(self, user_id: str, slot_no: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.slot_no = slot_no

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        clear_slot(self.user_id, self.slot_no)
        await interaction.response.send_message(f"セーブ{self.slot_no}を消したよ。", ephemeral=True)

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("消さないならそれでいいでしょ。", ephemeral=True)

class SkinSelect(discord.ui.Select):
    def __init__(self, user_id: str, slot_no: int):
        visuals = list_visuals(user_id)
        if not visuals:
            visuals = ["未解放"]
        options = [discord.SelectOption(label=v, value=v) for v in visuals[:25]]
        super().__init__(placeholder="見た目を選びなよ", min_values=1, max_values=1, options=options)
        self.user_id = user_id
        self.slot_no = slot_no

    async def callback(self, interaction: discord.Interaction):
        visual = self.values[0]
        if visual == "未解放":
            await interaction.response.send_message("図鑑増やしてから出直しなよ。", ephemeral=True)
            return
        slot = get_slot(self.user_id, self.slot_no)
        state = load_state(slot)
        state["visual_name"] = visual
        state["last_action_text"] = f"{visual}の見た目に変えた"
        update_ui_state(state)
        save_slot(self.user_id, self.slot_no, state)
        await refresh_status_message(self.user_id, self.slot_no)
        await interaction.response.send_message("見た目変えたよ。満足？", ephemeral=True)

class SkinView(discord.ui.View):
    def __init__(self, user_id: str, slot_no: int):
        super().__init__(timeout=180)
        self.add_item(SkinSelect(user_id, slot_no))

class CareView(discord.ui.View):
    def __init__(self, user_id: str, slot_no: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.slot_no = slot_no

    def _load(self):
        slot = get_slot(self.user_id, self.slot_no)
        state = load_state(slot)
        if not state:
            return None
        state = apply_time_passage(state)
        state, evolved = check_and_apply_evolution(state)
        if evolved:
            unlock_dex(self.user_id, state["character_id"], state["character_name"])
            unlock_visual(self.user_id, state["character_name"])
        save_slot(self.user_id, self.slot_no, state)
        return state

    async def _ensure_thread(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("専用スレッドで押して。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="ごはん", style=discord.ButtonStyle.primary)
    async def food(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_thread(interaction):
            return
        state = self._load()
        if is_non_care_state(state):
            await interaction.response.send_message("たまごと手紙にはごはんできないよ。", ephemeral=True)
            return
        state["condition"] = min(5, state["condition"] + 2)
        state["stress"] = max(0, state["stress"] - 1)
        state["ui_state"] = "ごはん"
        state["last_action_text"] = "ごはんをあげた"
        save_slot(self.user_id, self.slot_no, state)
        await refresh_status_message(self.user_id, self.slot_no)
        await interaction.response.defer()

    @discord.ui.button(label="あそぶ", style=discord.ButtonStyle.primary)
    async def play(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_thread(interaction):
            return
        state = self._load()
        if is_non_care_state(state):
            await interaction.response.send_message("たまごと手紙ではあそべないよ。", ephemeral=True)
            return
        state["stress"] = max(0, state["stress"] - 1)
        state["influence"] += 2
        state["ui_state"] = "よろこび"
        state["last_action_text"] = "あそんだ"
        save_slot(self.user_id, self.slot_no, state)
        await refresh_status_message(self.user_id, self.slot_no)
        await interaction.response.defer()

    @discord.ui.button(label="トレーニング", style=discord.ButtonStyle.primary)
    async def train(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_thread(interaction):
            return
        state = self._load()
        if is_non_care_state(state):
            await interaction.response.send_message("たまごと手紙はトレーニングできないよ。", ephemeral=True)
            return
        state["training_count"] += 1
        state["influence"] += 3
        state["stress"] = min(9, state["stress"] + 1)
        state["condition"] = max(0, state["condition"] - 1)
        state["performance"] += 2
        state["response"] += 1
        state["last_action_text"] = "トレーニングした"
        save_slot(self.user_id, self.slot_no, state)
        await refresh_status_message(self.user_id, self.slot_no)
        await interaction.response.defer()

    @discord.ui.button(label="休ませる", style=discord.ButtonStyle.secondary)
    async def sleep(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_thread(interaction):
            return
        state = self._load()
        if is_non_care_state(state):
            await interaction.response.send_message("たまごと手紙は休ませる操作はいらないよ。", ephemeral=True)
            return
        state["is_sleeping"] = not state.get("is_sleeping", False)
        state["last_action_text"] = "休ませた" if state["is_sleeping"] else "起こした"
        update_ui_state(state)
        save_slot(self.user_id, self.slot_no, state)
        await refresh_status_message(self.user_id, self.slot_no)
        await interaction.response.defer()

    @discord.ui.button(label="様子を見る", style=discord.ButtonStyle.secondary)
    async def look(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_thread(interaction):
            return
        await refresh_status_message(self.user_id, self.slot_no)
        await interaction.response.defer()

    @discord.ui.button(label="卵にもどる", style=discord.ButtonStyle.danger)
    async def rebirth(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_thread(interaction):
            return
        slot = get_slot(self.user_id, self.slot_no)
        state = load_state(slot)
        state = apply_time_passage(state)
        if not is_letter_state(state):
            await interaction.response.send_message("まだお別れしてないから戻れないよ。", ephemeral=True)
            return
        apply_reincarnation(self.user_id, self.slot_no, state)
        await refresh_status_message(self.user_id, self.slot_no)
        await interaction.response.send_message("卵にもどったよ。次の育成を始めよう。", ephemeral=True)

    @discord.ui.button(label="その他", style=discord.ButtonStyle.secondary)
    async def menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_thread(interaction):
            return
        await interaction.response.send_message(
            "どれ見るの？",
            view=SubMenuView(self.user_id, self.slot_no),
            ephemeral=True,
        )

class SubMenuSelect(discord.ui.Select):
    def __init__(self, user_id: str, slot_no: int):
        opts = [
            discord.SelectOption(label="進化情報", value="evo"),
            discord.SelectOption(label="転生情報", value="reinc"),
            discord.SelectOption(label="ガチャ", value="gacha"),
            discord.SelectOption(label="タッグ進化", value="tag"),
            discord.SelectOption(label="図鑑", value="dex"),
            discord.SelectOption(label="見た目変更", value="skin"),
            discord.SelectOption(label="PvP", value="pvp"),
        ]
        super().__init__(placeholder="選びなよ", min_values=1, max_values=1, options=opts)
        self.user_id = user_id
        self.slot_no = slot_no

    async def callback(self, interaction: discord.Interaction):
        slot = get_slot(self.user_id, self.slot_no)
        state = load_state(slot)
        state = apply_time_passage(state)
        save_slot(self.user_id, self.slot_no, state)

        if self.values[0] == "evo":
            cands = get_evolution_candidates(state["character_id"])
            if not cands:
                txt = "もう次がないか、まだ設定してないよ。"
            else:
                lines = [f"現在: {state['character_name']} ({state['stage']})", ""]
                for c in cands:
                    target = c.get("to", "")
                    if target == "adult_split":
                        continue
                    if target in characters_data:
                        lines.append(f"→ {characters_data[target].get('name', target)} / 優先度 {c.get('priority', '0')}")
                txt = "\n".join(lines)
            await interaction.response.send_message(txt, ephemeral=True)

        elif self.values[0] == "reinc":
            points = calc_reincarnation_points(state)
            txt = (
                f"今転生した時の予想ポイント: {points}\n"
                f"ライフ: {state['life']}\n"
                f"段階: {state['stage']}\n"
                f"大人になってからの転生の方がうまいよ。"
            )
            await interaction.response.send_message(txt, view=ReincView(self.user_id, self.slot_no), ephemeral=True)

        elif self.values[0] == "gacha":
            await interaction.response.send_message("ガチャ行くの？", view=GachaView(self.user_id, self.slot_no), ephemeral=True)

        elif self.values[0] == "tag":
            await interaction.response.send_message(
                "タッグ進化画面だよ。\n"
                "今は絆値の土台だけ入ってる。\n"
                "将来は相手選択と条件達成で特別フォームを解放する。",
                view=TagView(self.user_id, self.slot_no),
                ephemeral=True,
            )

        elif self.values[0] == "dex":
            rows = list_dex(self.user_id)
            if not rows:
                txt = "図鑑スカスカなんだけど？ まず進化しなよ。"
            else:
                txt = "【図鑑】\n" + "\n".join(f"・{r['character_name']}" for r in rows[:50])
            await interaction.response.send_message(txt, ephemeral=True)

        elif self.values[0] == "skin":
            await interaction.response.send_message("見た目いじりたいの？", view=SkinView(self.user_id, self.slot_no), ephemeral=True)

        elif self.values[0] == "pvp":
            await interaction.response.send_message("疑似PvPするの？", view=PvpView(self.user_id, self.slot_no), ephemeral=True)

class SubMenuView(discord.ui.View):
    def __init__(self, user_id: str, slot_no: int):
        super().__init__(timeout=180)
        self.add_item(SubMenuSelect(user_id, slot_no))

class ReincView(discord.ui.View):
    def __init__(self, user_id: str, slot_no: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.slot_no = slot_no

    @discord.ui.button(label="卵にもどる", style=discord.ButtonStyle.danger)
    async def reinc(self, interaction: discord.Interaction, button: discord.ui.Button):
        slot = get_slot(self.user_id, self.slot_no)
        state = load_state(slot)
        apply_reincarnation(self.user_id, self.slot_no, state)
        await refresh_status_message(self.user_id, self.slot_no)
        await interaction.response.send_message("手紙を残して卵にもどったよ。", ephemeral=True)

class GachaView(discord.ui.View):
    def __init__(self, user_id: str, slot_no: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.slot_no = slot_no

    @discord.ui.button(label="ガチャを引く", style=discord.ButtonStyle.success)
    async def roll(self, interaction: discord.Interaction, button: discord.ui.Button):
        profile = get_profile(self.user_id)
        if profile["gacha_tickets"] <= 0 and profile["coins"] < 100:
            await interaction.response.send_message("ガチャ券もコインもないんだけど？", ephemeral=True)
            return

        slot = get_slot(self.user_id, self.slot_no)
        state = load_state(slot)
        state = apply_time_passage(state)

        reward_cycle = ["stamina", "expression", "performance", "stability", "response", "intelligence"]
        idx = (state["training_count"] + profile["reincarnations"]) % len(reward_cycle)
        stat = reward_cycle[idx]
        state[stat] += 5
        state["last_action_text"] = f"ガチャで {stat} +5"
        save_slot(self.user_id, self.slot_no, state)

        if profile["gacha_tickets"] > 0:
            update_profile(self.user_id, gacha_tickets=profile["gacha_tickets"] - 1)
        else:
            update_profile(self.user_id, coins=profile["coins"] - 100)

        await refresh_status_message(self.user_id, self.slot_no)
        await interaction.response.send_message(f"ガチャ結果: {stat} +5", ephemeral=True)

class TagView(discord.ui.View):
    def __init__(self, user_id: str, slot_no: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.slot_no = slot_no

    @discord.ui.button(label="絆を深める", style=discord.ButtonStyle.primary)
    async def bond(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO tag_bonds (user_id, partner_id, bond)
        VALUES (?, 'default_partner', 1)
        ON CONFLICT(user_id, partner_id)
        DO UPDATE SET bond = bond + 1
        """, (self.user_id,))
        conn.commit()
        cur.execute("SELECT bond FROM tag_bonds WHERE user_id=? AND partner_id='default_partner'", (self.user_id,))
        bond = cur.fetchone()["bond"]
        conn.close()
        await interaction.response.send_message(f"絆 +1。今は {bond}。タッグ進化本体はこの土台の上に乗せる。", ephemeral=True)

class PvpView(discord.ui.View):
    def __init__(self, user_id: str, slot_no: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.slot_no = slot_no

    @discord.ui.button(label="疑似PvP", style=discord.ButtonStyle.primary)
    async def pvp(self, interaction: discord.Interaction, button: discord.ui.Button):
        my_slot = get_slot(self.user_id, self.slot_no)
        my_state = load_state(my_slot)
        my_power = my_state["stamina"] + my_state["performance"] + my_state["stability"] + my_state["response"] + my_state["intelligence"]

        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT * FROM save_slots
        WHERE save_status IN ('normal', 'migrated')
          AND state_json != ''
          AND user_id != ?
        ORDER BY updated_at DESC LIMIT 1
        """, (self.user_id,))
        enemy_slot = cur.fetchone()
        conn.close()

        if not enemy_slot:
            enemy_power = 180
            enemy_name = "練習用ダミー"
        else:
            enemy_state = json.loads(enemy_slot["state_json"])
            enemy_power = enemy_state["stamina"] + enemy_state["performance"] + enemy_state["stability"] + enemy_state["response"] + enemy_state["intelligence"]
            enemy_name = enemy_state["character_name"]

        if my_power >= enemy_power:
            my_state["wins"] += 1
            update_profile(self.user_id, total_wins=get_profile(self.user_id)["total_wins"] + 1, coins=get_profile(self.user_id)["coins"] + 50)
            result = f"勝ち。{enemy_name}を倒したよ。"
        else:
            my_state["losses"] += 1
            my_state["life"] = max(0, my_state["life"] - 1)
            update_profile(self.user_id, total_losses=get_profile(self.user_id)["total_losses"] + 1)
            result = f"負け。{enemy_name}にやられた。ライフ -1"

        my_state["last_action_text"] = result
        save_slot(self.user_id, self.slot_no, my_state)
        await refresh_status_message(self.user_id, self.slot_no)
        await interaction.response.send_message(result, ephemeral=True)

class MainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="はじめる", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        ensure_profile(str(interaction.user.id))
        await interaction.response.send_message("どのセーブではじめるの？", view=SlotPickerView(str(interaction.user.id), "start"), ephemeral=True)

    @discord.ui.button(label="つづきから", style=discord.ButtonStyle.secondary)
    async def cont(self, interaction: discord.Interaction, button: discord.ui.Button):
        ensure_profile(str(interaction.user.id))
        await interaction.response.send_message("続きを選びなよ。", view=SlotPickerView(str(interaction.user.id), "continue"), ephemeral=True)

    @discord.ui.button(label="セーブ削除", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        ensure_profile(str(interaction.user.id))
        await interaction.response.send_message("消すセーブを選びなよ。", view=SlotPickerView(str(interaction.user.id), "delete"), ephemeral=True)

    @discord.ui.button(label="セーブチェック", style=discord.ButtonStyle.secondary)
    async def check(self, interaction: discord.Interaction, button: discord.ui.Button):
        ensure_profile(str(interaction.user.id))
        await interaction.response.send_message("チェックするセーブを選びなよ。", view=SlotPickerView(str(interaction.user.id), "check"), ephemeral=True)

class UtilityView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="読み込みチェック", style=discord.ButtonStyle.success)
    async def reload(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("読み込みチェック流すよ。", ephemeral=True)
        await full_reload()

    @discord.ui.button(label="あそびかた", style=discord.ButtonStyle.secondary)
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "プレイヤーはボタンだけで遊べるようにしてる。\n"
            "最初は『はじめる』、次からは『つづきから』。\n"
            "画像・master・ログの確認は管理ログを見るの。",
            ephemeral=True,
        )

# =========================
# コマンド
# =========================
@bot.command()
async def setup_panel(ctx):
    if PANEL_CHANNEL_ID and ctx.channel.id != PANEL_CHANNEL_ID:
        await ctx.send("パネル用チャンネルでやって。")
        return
    await ensure_panel()
    await ctx.send("パネル確認したよ。")
    await full_reload()

@bot.command()
async def reload_master(ctx):
    await ctx.send("再読み込みするよ。")
    await full_reload()

# =========================
# 起動
# =========================

@bot.event
async def on_ready():
    init_db()
    await full_reload()
    await ensure_panel()
    print(f"Logged in as {bot.user} / JST: {fmt_dt(now_jst())}")

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN が入ってない")
    init_db()
    bot.run(TOKEN)
