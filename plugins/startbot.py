import asyncio
import uuid
from datetime import datetime, timedelta
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, CallbackQuery
import sqlite3
import random
import os
import logging
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, QueryIdInvalid, MessageNotModified
import sys

sys.stdout.reconfigure(encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('telegram_api.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

bot_id = None
last_access_check = None
api_request_counter = 0
failed_request_counter = 0
processed_queries = set()
user_cache = {}
channel_members_cache = {}
channel_members_lock = asyncio.Lock()
answer_queue = asyncio.Queue()

TOPIC_TO_TABLE = {
    "topic_economics": "questions_calan",
    "topic_development": "questions_development",
    "topic_macroeconomic": "questions_english",
    "topic_international_trade": "questions_international_trade",
    "topic_microeconomics": "questions_microeconomics",
    "topic_econthought_history": "questions_EconThoughtHistory",
}

TOPIC_TO_PERSIAN = {
    "topic_economics": "اقتصاد کلان",
    "topic_development": "توسعه",
    "topic_macroeconomic": "زبان تخصصی",
    "topic_international_trade": "تجارت بین‌الملل",
    "topic_microeconomics": "اقتصاد خرد",
    "topic_econthought_history": "تاریخ عقاید اقتصادی",
}

original_methods = {
    'send_message': Client.send_message,
    'edit_message_text': Client.edit_message_text,
    'answer_callback_query': Client.answer_callback_query,
    'answer_inline_query': Client.answer_inline_query,
    'get_chat': Client.get_chat,
    'get_chat_member': Client.get_chat_member,
    'get_users': Client.get_users,
    'get_me': Client.get_me
}

def wrap_method(method_name, original_method):
    async def wrapped(self, *args, **kwargs):
        global api_request_counter, failed_request_counter
        api_request_counter += 1
        chat_id = args[0] if args and method_name == "get_chat" else "unknown"
        logger.info(f"API Request #{api_request_counter}: Method={method_name}, ChatID={chat_id}, Args={args}, Kwargs={kwargs}")
        try:
            result = await original_method(self, *args, **kwargs)
            logger.debug(f"API Request #{api_request_counter} Successful")
            return result
        except Exception as e:
            failed_request_counter += 1
            logger.error(f"API Request #{api_request_counter} Failed: Error={str(e)}")
            raise e
        finally:
            logger.info(f"Total API Requests: {api_request_counter}, Failed: {failed_request_counter}")

    return wrapped

for method_name, original_method in original_methods.items():
    setattr(Client, method_name, wrap_method(method_name, original_method))

class Game:
    def __init__(self, owner_id):
        self.game_id = str(uuid.uuid4())
        self.owner_id = owner_id
        self.selections = {"number": None, "time": [], "topics": []}
        self.players = []
        self.choices = {}
        self.created_at = datetime.now()
        self.last_updated = datetime.now()
        self.current_question = 0
        self.questions = []
        self.scores = {}
        self.used_questions = set()  # هر بازی مجموعه سوالات استفاده‌شده خودش را دارد

    def update_timestamp(self):
        self.last_updated = datetime.now()

    def is_expired(self, timeout_minutes=30):
        return datetime.now() - self.last_updated > timedelta(minutes=timeout_minutes)

    def get_settings_summary(self):
        number = self.selections["number"][4:] if self.selections["number"] else "انتخاب نشده"
        time = self.selections["time"][0][4:] + " ثانیه" if self.selections["time"] else "انتخاب نشده"
        topics = ", ".join([TOPIC_TO_PERSIAN.get(t, t[6:].capitalize()) for t in self.selections["topics"]]) or "انتخاب نشده"
        return f"📋 تنظیمات:\nسوالات: {number}\nزمان: {time}\nموضوعات: {topics}"

    def get_total_questions(self):
        return int(self.selections["number"][4:]) if self.selections["number"] else 0

    def get_random_questions(self, table_name, num_questions):
        db_path = "plugins/questions.db"
        try:
            if not os.path.exists(db_path):
                raise FileNotFoundError(f"فایل پایگاه داده {db_path} پیدا نشد!")
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(f"SELECT question, option1, option2, correct_answer FROM {table_name}")
                questions = cursor.fetchall()
            available_questions = [q for q in questions if str(q) not in self.used_questions]
            if len(available_questions) < num_questions:
                self.used_questions.clear()
                available_questions = questions
            if len(available_questions) < num_questions:
                raise ValueError(f"تعداد سوالات کافی در جدول {table_name} وجود ندارد!")
            selected_questions = random.sample(available_questions, num_questions)
            processed_questions = []
            for question, option1, option2, correct_answer in selected_questions:
                question = question.replace("\\n", "\n")
                option1 = option1.replace("\\n", "\n")
                option2 = option2.replace("\\n", "\n")
                correct_answer = correct_answer.replace("\\n", "\n")
                processed_questions.append((question, option1, option2, correct_answer))
                self.used_questions.add(str((question, option1, option2, correct_answer)))
            return processed_questions
        except Exception as e:
            logger.error(f"Error getting random questions: {str(e)}")
            return []

games = {}

def init_leaderboard_db():
    db_path = "plugins/questions.db"
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS leaderboard (
                    user_id INTEGER,
                    username TEXT,
                    correct_answers INTEGER,
                    game_date TEXT,
                    PRIMARY KEY (user_id, game_date)
                )
            """)
            conn.commit()
    except Exception as e:
        logger.error(f"Error initializing leaderboard DB: {str(e)}")

def save_player_score(user_id, username, correct_answers):
    db_path = "plugins/questions.db"
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            game_date = datetime.now().strftime("%Y-%m-%d")
            cursor.execute("SELECT correct_answers FROM leaderboard WHERE user_id = ? AND game_date = ?",
                           (user_id, game_date))
            result = cursor.fetchone()
            if result:
                existing_correct_answers = result[0]
                total_correct_answers = existing_correct_answers + correct_answers
                cursor.execute(
                    "UPDATE leaderboard SET correct_answers = ?, username = ? WHERE user_id = ? AND game_date = ?",
                    (total_correct_answers, username, user_id, game_date))
            else:
                cursor.execute(
                    "INSERT INTO leaderboard (user_id, username, correct_answers, game_date) VALUES (?, ?, ?, ?)",
                    (user_id, username, correct_answers, game_date))
            conn.commit()
    except Exception as e:
        logger.error(f"Error saving player score: {str(e)}")

def get_leaderboard():
    db_path = "plugins/questions.db"
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT user_id, SUM(correct_answers) as total_correct
                FROM leaderboard
                GROUP BY user_id
                ORDER BY total_correct DESC
                LIMIT 50
                """
            )
            results = cursor.fetchall()
            leaderboard = []
            for user_id, total_correct in results:
                cursor.execute(
                    """
                    SELECT username
                    FROM leaderboard
                    WHERE user_id = ?
                    ORDER BY game_date DESC
                    LIMIT 1
                    """,
                    (user_id,)
                )
                username = cursor.fetchone()[0]
                leaderboard.append((username, total_correct))
            return leaderboard
    except Exception as e:
        logger.error(f"Error fetching leaderboard: {str(e)}")
        return []

def check_member_in_cache(user_id):
    return channel_members_cache.get(user_id, {}).get("status")

async def sync_channel_members(client):
    try:
        async with channel_members_lock:
            channel_members_cache.clear()
            async for member in client.get_chat_members("@chalesh_yarr"):
                try:
                    user_id = member.user.id
                    status = member.status.value
                    channel_members_cache[user_id] = {
                        "status": status,
                        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                except Exception as e:
                    logger.error(f"Error syncing member {user_id}: {str(e)}")
    except Exception as e:
        logger.error(f"Error syncing channel members: {str(e)}")

async def update_channel_members_periodically(client):
    while True:
        try:
            await sync_channel_members(client)
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error in update_channel_members_periodically: {str(e)}")
            await asyncio.sleep(5)

async def process_queued_actions(client: Client):
    while True:
        try:
            actions = []
            while not answer_queue.empty():
                action = await answer_queue.get()
                actions.append(action)

            if actions:
                for action in actions:
                    callback_query_id = action['callback_query_id']
                    action_type = action.get('type', 'answer')
                    text = action['text']
                    show_alert = action.get('show_alert', False)

                    if action_type == 'answer':
                        game_id = action['game_id']
                        user_id = action['user_id']
                        pure_data = action['pure_data']
                        game = games.get(game_id)
                        if not game:
                            logger.warning(f"Game {game_id} not found for answer processing")
                            continue
                        current_question = game.current_question
                        if current_question not in game.choices:
                            game.choices[current_question] = {}
                        if user_id in game.choices[current_question]:
                            text = "⚠️ شما قبلاً برای این سوال گزینه‌ای انتخاب کرده‌اید"
                            show_alert = False
                        else:
                            game.choices[current_question][user_id] = pure_data
                            question_text, _, _, correct_answer = game.questions[current_question - 1]
                            is_correct = pure_data[-1] == correct_answer[-1]
                            if is_correct:
                                game.scores[user_id] = game.scores.get(user_id, 0) + 1
                            text = "✅ پاسخ درست!" if is_correct else "❌ پاسخ نادرست!"
                            show_alert = False
                            logger.info(f"Processed answer for user {user_id} in game {game_id}: {pure_data}, Correct: {is_correct}")

                    try:
                        await client.answer_callback_query(
                            callback_query_id=callback_query_id,
                            text=text,
                            show_alert=show_alert
                        )
                        logger.info(f"Processed action: {text} for callback {callback_query_id}")
                    except QueryIdInvalid:
                        logger.warning(f"Invalid callback query ID: {callback_query_id}")
                    except Exception as e:
                        logger.error(f"Failed to process action: {e}")

                logger.info(f"Processed {len(actions)} queued actions")

            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Error in process_queued_actions: {e}")
            await asyncio.sleep(3)

async def start_background_tasks(client):
    global bot_id
    try:
        bot_id = (await client.get_me()).id
        asyncio.create_task(cleanup_expired_games())
        asyncio.create_task(announce_leaderboard(client))
        asyncio.create_task(update_channel_members_periodically(client))
        asyncio.create_task(cleanup_processed_queries())
        asyncio.create_task(log_request_summary())
        asyncio.create_task(process_queued_actions(client))
    except Exception as e:
        logger.error(f"Error in starting background tasks: {str(e)}")

async def cleanup_processed_queries():
    while True:
        processed_queries.clear()
        await asyncio.sleep(3600)

async def log_request_summary():
    while True:
        logger.info(f"Summary: Total API Requests={api_request_counter}, Failed Requests={failed_request_counter}")
        await asyncio.sleep(300)

async def announce_leaderboard(client):
    while True:
        try:
            leaderboard = get_leaderboard()
            if not leaderboard:
                message = "🌟 **هنوز هیچ بازیکنی در رتبه‌بندی ثبت نشده است!** 🌟\n" \
                          "🎮 بیا و در چالش یار شرکت کن تا نامت اینجا بدرخشه! ✨"
            else:
                message = "🌟 **جدول نفرات برتر چالش یار** 🌟\n\n"
                for idx, (username, total_correct) in enumerate(leaderboard, 1):
                    if idx == 1:
                        medal = "🥇"
                    elif idx == 2:
                        medal = "🥈"
                    elif idx == 3:
                        medal = "🥉"
                    else:
                        medal = f"{idx}."
                    message += f"{medal} **{username}** - {total_correct} پاسخ درست 🎉\n"
                message += "\n🏆 به جمع برترین‌ها بپیوندید! 🚀"
            await client.send_message(chat_id="@chalesh_yarr", text=message, disable_web_page_preview=True)
            await asyncio.sleep(300)
        except Exception as e:
            logger.error(f"Error in announce_leaderboard: {str(e)}")
            await asyncio.sleep(300)

async def cleanup_expired_games():
    while True:
        try:
            expired_games = [game_id for game_id, game in games.items() if game.is_expired()]
            for game_id in expired_games:
                del games[game_id]
                logger.info(f"Cleaned up expired game: {game_id}")
            await asyncio.sleep(300)
        except Exception as e:
            logger.error(f"Error in cleanup_expired_games: {str(e)}")
            await asyncio.sleep(300)

def test_db_connection():
    db_path = "plugins/questions.db"
    if not os.path.exists(db_path):
        logger.error(f"Database file {db_path} does not exist")
        return False
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            return True
    except Exception as e:
        logger.error(f"Error testing DB connection: {str(e)}")
        return False

def get_combined_questions(game, topics, total_questions):
    try:
        if not topics:
            raise ValueError("هیچ موضوعی انتخاب نشده است!")
        num_topics = len(topics)
        questions_per_topic = max(1, total_questions // num_topics)
        remaining_questions = total_questions - (questions_per_topic * num_topics)
        all_questions = []
        for topic in topics:
            table_name = TOPIC_TO_TABLE.get(topic)
            if not table_name:
                continue
            num_questions = questions_per_topic + (1 if remaining_questions > 0 else 0)
            if remaining_questions > 0:
                remaining_questions -= 1
            questions = game.get_random_questions(table_name, min(num_questions, total_questions - len(all_questions)))
            all_questions.extend(questions)
        random.shuffle(all_questions)
        return all_questions[:total_questions]
    except Exception as e:
        logger.error(f"Error getting combined questions: {str(e)}")
        return []

def create_options_keyboard(game_id, option1, option2):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔵 {option1}", callback_data=f"{game_id}|option_1")],
        [InlineKeyboardButton(f"🟢 {option2}", callback_data=f"{game_id}|option_2")]
    ])

if not test_db_connection():
    logger.critical("Failed to connect to database. Exiting.")
    exit(1)

init_leaderboard_db()

@Client.on_inline_query()
async def inline_main_menu(client: Client, inline_query):
    user_id = inline_query.from_user.id
    game = Game(user_id)
    games[game.game_id] = game
    logger.info(f"New game created: {game.game_id} by user {user_id}")
    try:
        header = "به ربات سوالات خوش آمدید!                                              "
        settings = game.get_settings_summary()
        footer = "📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید.    "
        message = f"{header}\n{settings}\n\n{footer}"
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    title="🎮 تنظیمات بازی",
                    description="تعداد سوال، زمان و دعوت از دوستان",
                    input_message_content=InputTextMessageContent(message),
                    reply_markup=my_start_def_glassButton(game.game_id)
                )
            ],
            cache_time=1
        )
    except Exception as e:
        logger.error(f"Error in inline_main_menu: {str(e)}")
        await inline_query.answer(
            results=[],
            cache_time=1,
            switch_pm_text="⚠️ خطا در پردازش درخواست!",
            switch_pm_parameter="error"
        )

def my_start_def_glassButton(game_id):
    game = games.get(game_id)
    if not game:
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ بازی منقضی شده", callback_data="expired")]])
    selections = game.selections
    number = selections["number"]
    times = selections["time"]
    topics = selections["topics"]

    def cb(data): return f"{game_id}|{data}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("تعداد سوالات", callback_data=cb("numberofQ"))],
        [InlineKeyboardButton(f"{n[4:]} {'✅' if number == n else ''}", callback_data=cb(n)) for n in
         ["numb6", "numb8", "numb10", "numb12", "numb15", "numb18", "numb20"]],
        [InlineKeyboardButton("⏱️ زمان پاسخ", callback_data=cb("timeForQ"))],
        [InlineKeyboardButton(f"{t[4:]} {'✅' if t in times else ''}", callback_data=cb(t)) for t in
         ["time10", "time15", "time20"]],
        [InlineKeyboardButton("📚 انتخاب موضوع", callback_data=cb("selectTopic"))],
        [
            InlineKeyboardButton(f"اقتصاد کلان {'✅' if 'topic_economics' in topics else ''}", callback_data=cb("topic_economics")),
            InlineKeyboardButton(f"توسعه {'✅' if 'topic_development' in topics else ''}", callback_data=cb("topic_development")),
            InlineKeyboardButton(f"زبان تخصصی {'✅' if 'topic_macroeconomic' in topics else ''}", callback_data=cb("topic_macroeconomic"))
        ],
        [
            InlineKeyboardButton(f"تجارت بین‌الملل {'✅' if 'topic_international_trade' in topics else ''}", callback_data=cb("topic_international_trade")),
            InlineKeyboardButton(f"اقتصاد خرد {'✅' if 'topic_microeconomics' in topics else ''}", callback_data=cb("topic_microeconomics")),
            InlineKeyboardButton(f"تاریخ عقاید {'✅' if 'topic_econthought_history' in topics else ''}", callback_data=cb("topic_econthought_history"))
        ],
        [InlineKeyboardButton("🤝 دعوت از دوستان", switch_inline_query=f"start_quiz_{game_id}")],
        [InlineKeyboardButton("🎮 ساخت بازی", callback_data=cb("start_exam"))],
        [InlineKeyboardButton("🗑️ لغو بازی", callback_data=cb("cancel_game"))]
    ])

@Client.on_callback_query()
async def handle_callback_query(client: Client, callback_query: CallbackQuery):
    if callback_query.id in processed_queries:
        return
    processed_queries.add(callback_query.id)
    from_user = callback_query.from_user
    from_user_id = from_user.id
    data = callback_query.data
    logger.info(f"Callback query from user {from_user_id}: {data}")
    if data == "expired":
        try:
            await callback_query.answer("❌ این بازی منقضی شده است.", show_alert=True)
        except QueryIdInvalid:
            pass
        return
    try:
        game_id, pure_data = data.split("|", 1)
        game = games.get(game_id)
        if not game:
            try:
                await callback_query.answer("❌ بازی نامعتبر یا منقضی شده است", show_alert=True)
            except QueryIdInvalid:
                pass
            return
        owner_id = game.owner_id
    except ValueError:
        try:
            await callback_query.answer("❌ دکمه نامعتبر", show_alert=True)
        except QueryIdInvalid:
            pass
        return
    game.update_timestamp()
    selections = game.selections
    needs_update = False
    if from_user_id != owner_id and pure_data not in ["ready_now", "option_1", "option_2"]:
        try:
            await callback_query.answer("⛔ فقط سازنده بازی می‌تواند تنظیمات را تغییر دهد", show_alert=True)
        except QueryIdInvalid:
            pass
        return
    try:
        if pure_data.startswith("numb"):
            selections["number"] = pure_data
            needs_update = True
            await answer_queue.put({
                'callback_query_id': callback_query.id,
                'type': 'response',
                'text': "✅ انتخاب تغییر کرد",
                'show_alert': False
            })
        elif pure_data.startswith("time"):
            selections["time"] = [pure_data]
            needs_update = True
            await answer_queue.put({
                'callback_query_id': callback_query.id,
                'type': 'response',
                'text': "✅ انتخاب تغییر کرد",
                'show_alert': False
            })
        elif pure_data.startswith("topic_"):
            if pure_data in selections["topics"]:
                selections["topics"].remove(pure_data)
            else:
                selections["topics"].append(pure_data)
            needs_update = True
            await answer_queue.put({
                'callback_query_id': callback_query.id,
                'type': 'response',
                'text': "✅ انتخاب تغییر کرد",
                'show_alert': False
            })
        elif pure_data == "start_exam":
            if not selections["number"] or not selections["time"] or not selections["topics"]:
                await callback_query.answer("لطفاً همه فیلدها را انتخاب کنید ❗", show_alert=True)
                return
            await callback_query.edit_message_text(
                f"🎯 لطفاً یکی از گزینه‌ها را انتخاب کنید:                                              \n\n{game.get_settings_summary()}\n\n{await get_players_list(client, game_id)}\n\n📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید.    ",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ حاضر", callback_data=f"{game_id}|ready_now")],
                    [InlineKeyboardButton("🚀 شروع", callback_data=f"{game_id}|start_now")],
                    [InlineKeyboardButton("🔙 برگشت به منو", callback_data=f"{game_id}|back_to_menu")],
                    [InlineKeyboardButton("🗑️ لغو بازی", callback_data=f"{game_id}|cancel_game")]
                ])
            )
            await answer_queue.put({
                'callback_query_id': callback_query.id,
                'type': 'response',
                'text': "✅ بازی ساخته شد",
                'show_alert': False
            })
            return
        elif pure_data == "ready_now":
            status_from_cache = check_member_in_cache(from_user_id)
            if status_from_cache and status_from_cache in ["member", "administrator", "owner", "restricted"]:
                if from_user_id in game.players:
                    await callback_query.answer("✅ شما قبلاً به بازی پیوسته‌اید", show_alert=True)
                    return
                game.players.append(from_user_id)
                if from_user_id not in user_cache:
                    user_cache[from_user_id] = from_user
                await callback_query.edit_message_text(
                    f"🎯 لطفاً یکی از گزینه‌ها را انتخاب کنید:                                              \n\n{game.get_settings_summary()}\n\n{await get_players_list(client, game_id)}\n\n📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید.    ",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ حاضر", callback_data=f"{game_id}|ready_now")],
                        [InlineKeyboardButton("🚀 شروع", callback_data=f"{game_id}|start_now")],
                        [InlineKeyboardButton("🔙 برگشت به منو", callback_data=f"{game_id}|back_to_menu")],
                        [InlineKeyboardButton("🗑️ لغو بازی", callback_data=f"{game_id}|cancel_game")]
                    ])
                )
                await answer_queue.put({
                    'callback_query_id': callback_query.id,
                    'type': 'response',
                    'text': "✅ شما به لیست بازیکنان اضافه شدید",
                    'show_alert': False
                })
                return
            await callback_query.answer("⛔ شما حاضر نیستید! لطفاً ابتدا عضو کانال چالش-یار شوید! 👉 @chalesh_yarr",
                                        show_alert=True)
            return
        elif pure_data in ["option_1", "option_2"]:
            if from_user_id not in game.players:
                await callback_query.answer("⛔ شما در این بازی حضور ندارید!", show_alert=True)
                return
            await answer_queue.put({
                'callback_query_id': callback_query.id,
                'type': 'answer',
                'game_id': game_id,
                'user_id': from_user_id,
                'pure_data': pure_data,
                'text': ""
            })
            return
        elif pure_data == "start_now":
            if from_user_id != owner_id:
                await callback_query.answer("⛔ فقط سازنده می‌تواند بازی را شروع کند", show_alert=True)
                return
            if len(game.players) < 2:
                await callback_query.edit_message_text(
                    f"⏳ در انتظار ورود بازیکن (حداقل 2 بازیکن مورد نیاز است):                                              \n\n{game.get_settings_summary()}\n\n{await get_players_list(client, game_id)}\n\n📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید.    ",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ حاضر", callback_data=f"{game_id}|ready_now")],
                        [InlineKeyboardButton("🚀 شروع", callback_data=f"{game_id}|start_now")],
                        [InlineKeyboardButton("🔙 برگشت به منو", callback_data=f"{game_id}|back_to_menu")],
                        [InlineKeyboardButton("🗑️ لغو بازی", callback_data=f"{game_id}|cancel_game")]
                    ])
                )
                await callback_query.answer("⛔ حداقل 2 بازیکن برای شروع لازم است", show_alert=True)
                return
            time_str = selections["time"][0]
            seconds = int(time_str.replace("time", ""))
            total_questions = game.get_total_questions()
            game.questions = get_combined_questions(game, selections["topics"], total_questions)
            if not game.questions:
                await callback_query.answer("⚠️ خطا در بارگذاری سوالات! لطفاً موضوعات دیگر را انتخاب کنید.",
                                            show_alert=True)
                return
            for player_id in game.players:
                game.scores[player_id] = 0
            for question_idx in range(total_questions):
                game.current_question = question_idx + 1
                game.choices[game.current_question] = {}
                question_text, option1, option2, _ = game.questions[question_idx]
                try:
                    keyboard = create_options_keyboard(game_id, option1, option2)
                    await callback_query.edit_message_text(
                        f"❓ سوال {game.current_question} از {total_questions}\n⏳ {seconds} ثانیه وقت دارید:\n\n{question_text}\n\n{game.get_settings_summary()}",
                        reply_markup=keyboard
                    )
                except MessageNotModified:
                    pass
                except Exception as e:
                    logger.error(f"Error displaying question: {str(e)}")
                    break
                await asyncio.sleep(seconds)
            result_lines = ["📊 نتایج بازی:"]
            sorted_players = sorted(game.players, key=lambda pid: game.scores.get(pid, 0), reverse=True)
            for rank, player_id in enumerate(sorted_players, 1):
                player_name = user_cache.get(player_id, await client.get_users(player_id)).first_name
                result_lines.append(f"{rank}. {player_name}")
                status_row = []
                for question_idx in range(total_questions):
                    question_num = question_idx + 1
                    choice = game.choices.get(question_num, {}).get(player_id, None)
                    if choice:
                        _, _, _, correct_answer = game.questions[question_idx]
                        is_correct = choice[-1] == correct_answer[-1]
                        status_row.append("✅" if is_correct else "❌")
                    else:
                        status_row.append("☐")
                status_line = " ".join(status_row)
                result_lines.append(status_line)
                correct_count = status_row.count("✅")
                wrong_count = status_row.count("❌")
                unanswered_count = status_row.count("☐")
                result_lines.append(f"✅ {correct_count} | ❌ {wrong_count} | ☐ {unanswered_count}")
                save_player_score(player_id, player_name, correct_count)
            try:
                if callback_query.message:
                    await client.send_message(chat_id=callback_query.message.chat.id, text="\n".join(result_lines),
                                              disable_web_page_preview=True)
                elif callback_query.inline_message_id:
                    await callback_query.edit_message_text(text="\n".join(result_lines), disable_web_page_preview=True)
                del games[game_id]
            except MessageNotModified:
                pass
            except Exception as e:
                logger.error(f"Error displaying results: {str(e)}")
                try:
                    await callback_query.answer("⚠️ خطایی در نمایش نتایج رخ داد", show_alert=True)
                except QueryIdInvalid:
                    pass
            return
        elif pure_data == "back_to_menu":
            await callback_query.edit_message_text(
                f"🎮 لطفاً تعداد سوال، زمان و موضوع را انتخاب کنید:                                              \n\n{game.get_settings_summary()}",
                reply_markup=my_start_def_glassButton(game_id)
            )
            await answer_queue.put({
                'callback_query_id': callback_query.id,
                'type': 'response',
                'text': "🔙 برگشت به منو",
                'show_alert': False
            })
            return
        elif pure_data == "cancel_game":
            await callback_query.edit_message_text("🗑️ بازی لغو شد.")
            del games[game_id]
            await answer_queue.put({
                'callback_query_id': callback_query.id,
                'type': 'response',
                'text': "✅ بازی لغو شد",
                'show_alert': False
            })
            return
        if needs_update:
            try:
                await callback_query.edit_message_text(
                    f"🎮 لطفاً تعداد سوال، زمان و موضوع را انتخاب کنید:                                              \n\n{game.get_settings_summary()}",
                    reply_markup=my_start_def_glassButton(game_id)
                )
            except MessageNotModified:
                pass
            except Exception as e:
                logger.error(f"Error updating message: {str(e)}")
        else:
            await answer_queue.put({
                'callback_query_id': callback_query.id,
                'type': 'response',
                'text': "⚠️ این گزینه از قبل انتخاب شده",
                'show_alert': False
            })
    except Exception as e:
        logger.error(f"Error in handle_callback_query: {str(e)}")
        try:
            await callback_query.answer("⚠️ خطایی رخ داد!", show_alert=True)
        except QueryIdInvalid:
            pass

async def get_players_list(client, game_id):
    game = games.get(game_id, Game(0))
    if not game.players:
        return "⏳ هنوز بازیکنی اضافه نشده!"
    missing_users = [user_id for user_id in game.players if user_id not in user_cache]
    if missing_users:
        try:
            users = await client.get_users(missing_users)
            for user in users:
                user_cache[user.id] = user
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Error fetching users: {str(e)}")
    players_list = [
        f"👤 {user_cache[user_id].first_name}" if user_id in user_cache else f"👤 کاربر ناشناس (ID: {user_id})" for
        user_id in game.players]
    return "👥 بازیکنان حاضر:\n" + "\n".join(players_list)

# import asyncio
# import uuid
# from datetime import datetime, timedelta
# from pyrogram import Client, filters
# from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, CallbackQuery
# import sqlite3
# import random
# import os
# import logging
# from pyrogram.enums import ChatMemberStatus
# from pyrogram.errors import FloodWait, QueryIdInvalid, MessageNotModified
# import sys
#
# sys.stdout.reconfigure(encoding='utf-8')
#
# logging.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)s - %(levelname)s - %(message)s',
#     handlers=[
#         logging.FileHandler('telegram_api.log', encoding='utf-8'),
#         logging.StreamHandler()
#     ]
# )
# logger = logging.getLogger(__name__)
#
# bot_id = None
# last_access_check = None
# api_request_counter = 0
# failed_request_counter = 0
# processed_queries = set()
# used_questions = set()
# user_cache = {}
# channel_members_cache = {}
# channel_members_lock = asyncio.Lock()
# answer_queue = asyncio.Queue()
#
# TOPIC_TO_TABLE = {
#     "topic_economics": "questions_calan",
#     "topic_development": "questions_development",
#     "topic_macroeconomic": "questions_english",
#     "topic_international_trade": "questions_international_trade",
#     "topic_microeconomics": "questions_microeconomics",
#     "topic_econthought_history": "questions_EconThoughtHistory",
# }
#
# TOPIC_TO_PERSIAN = {
#     "topic_economics": "اقتصاد کلان",
#     "topic_development": "توسعه",
#     "topic_macroeconomic": "زبان تخصصی",
#     "topic_international_trade": "تجارت بین‌الملل",
#     "topic_microeconomics": "اقتصاد خرد",
#     "topic_econthought_history": "تاریخ عقاید اقتصادی",
# }
#
# original_methods = {
#     'send_message': Client.send_message,
#     'edit_message_text': Client.edit_message_text,
#     'answer_callback_query': Client.answer_callback_query,
#     'answer_inline_query': Client.answer_inline_query,
#     'get_chat': Client.get_chat,
#     'get_chat_member': Client.get_chat_member,
#     'get_users': Client.get_users,
#     'get_me': Client.get_me
# }
#
# def wrap_method(method_name, original_method):
#     async def wrapped(self, *args, **kwargs):
#         global api_request_counter, failed_request_counter
#         api_request_counter += 1
#         chat_id = args[0] if args and method_name == "get_chat" else "unknown"
#         logger.info(f"API Request #{api_request_counter}: Method={method_name}, ChatID={chat_id}, Args={args}, Kwargs={kwargs}")
#         try:
#             result = await original_method(self, *args, **kwargs)
#             logger.debug(f"API Request #{api_request_counter} Successful")
#             return result
#         except Exception as e:
#             failed_request_counter += 1
#             logger.error(f"API Request #{api_request_counter} Failed: Error={str(e)}")
#             raise e
#         finally:
#             logger.info(f"Total API Requests: {api_request_counter}, Failed: {failed_request_counter}")
#
#     return wrapped
#
# for method_name, original_method in original_methods.items():
#     setattr(Client, method_name, wrap_method(method_name, original_method))
#
# class Game:
#     def __init__(self, owner_id):
#         self.game_id = str(uuid.uuid4())
#         self.owner_id = owner_id
#         self.selections = {"number": None, "time": [], "topics": []}
#         self.players = []
#         self.choices = {}
#         self.created_at = datetime.now()
#         self.last_updated = datetime.now()
#         self.current_question = 0
#         self.questions = []
#         self.scores = {}
#
#     def update_timestamp(self):
#         self.last_updated = datetime.now()
#
#     def is_expired(self, timeout_minutes=30):
#         return datetime.now() - self.last_updated > timedelta(minutes=timeout_minutes)
#
#     def get_settings_summary(self):
#         number = self.selections["number"][4:] if self.selections["number"] else "انتخاب نشده"
#         time = self.selections["time"][0][4:] + " ثانیه" if self.selections["time"] else "انتخاب نشده"
#         topics = ", ".join([TOPIC_TO_PERSIAN.get(t, t[6:].capitalize()) for t in self.selections["topics"]]) or "انتخاب نشده"
#         return f"📋 تنظیمات:\nسوالات: {number}\nزمان: {time}\nموضوعات: {topics}"
#
#     def get_total_questions(self):
#         return int(self.selections["number"][4:]) if self.selections["number"] else 0
#
# games = {}
#
# def init_leaderboard_db():
#     db_path = "plugins/questions.db"
#     try:
#         with sqlite3.connect(db_path) as conn:
#             cursor = conn.cursor()
#             cursor.execute("""
#                 CREATE TABLE IF NOT EXISTS leaderboard (
#                     user_id INTEGER,
#                     username TEXT,
#                     correct_answers INTEGER,
#                     game_date TEXT,
#                     PRIMARY KEY (user_id, game_date)
#                 )
#             """)
#             conn.commit()
#     except Exception as e:
#         logger.error(f"Error initializing leaderboard DB: {str(e)}")
#
# def save_player_score(user_id, username, correct_answers):
#     db_path = "plugins/questions.db"
#     try:
#         with sqlite3.connect(db_path) as conn:
#             cursor = conn.cursor()
#             game_date = datetime.now().strftime("%Y-%m-%d")
#             cursor.execute("SELECT correct_answers FROM leaderboard WHERE user_id = ? AND game_date = ?",
#                            (user_id, game_date))
#             result = cursor.fetchone()
#             if result:
#                 existing_correct_answers = result[0]
#                 total_correct_answers = existing_correct_answers + correct_answers
#                 cursor.execute(
#                     "UPDATE leaderboard SET correct_answers = ?, username = ? WHERE user_id = ? AND game_date = ?",
#                     (total_correct_answers, username, user_id, game_date))
#             else:
#                 cursor.execute(
#                     "INSERT INTO leaderboard (user_id, username, correct_answers, game_date) VALUES (?, ?, ?, ?)",
#                     (user_id, username, correct_answers, game_date))
#             conn.commit()
#     except Exception as e:
#         logger.error(f"Error saving player score: {str(e)}")
#
# def get_leaderboard():
#     db_path = "plugins/questions.db"
#     try:
#         with sqlite3.connect(db_path) as conn:
#             cursor = conn.cursor()
#             cursor.execute(
#                 """
#                 SELECT user_id, SUM(correct_answers) as total_correct
#                 FROM leaderboard
#                 GROUP BY user_id
#                 ORDER BY total_correct DESC
#                 LIMIT 50
#                 """
#             )
#             results = cursor.fetchall()
#             leaderboard = []
#             for user_id, total_correct in results:
#                 cursor.execute(
#                     """
#                     SELECT username
#                     FROM leaderboard
#                     WHERE user_id = ?
#                     ORDER BY game_date DESC
#                     LIMIT 1
#                     """,
#                     (user_id,)
#                 )
#                 username = cursor.fetchone()[0]
#                 leaderboard.append((username, total_correct))
#             return leaderboard
#     except Exception as e:
#         logger.error(f"Error fetching leaderboard: {str(e)}")
#         return []
#
# def check_member_in_cache(user_id):
#     return channel_members_cache.get(user_id, {}).get("status")
#
# async def sync_channel_members(client):
#     try:
#         async with channel_members_lock:
#             channel_members_cache.clear()
#             async for member in client.get_chat_members("@chalesh_yarr"):
#                 try:
#                     user_id = member.user.id
#                     status = member.status.value
#                     channel_members_cache[user_id] = {
#                         "status": status,
#                         "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#                     }
#                 except Exception as e:
#                     logger.error(f"Error syncing member {user_id}: {str(e)}")
#     except Exception as e:
#         logger.error(f"Error syncing channel members: {str(e)}")
#
# async def update_channel_members_periodically(client):
#     while True:
#         try:
#             await sync_channel_members(client)
#             await asyncio.sleep(5)
#         except Exception as e:
#             logger.error(f"Error in update_channel_members_periodically: {str(e)}")
#             await asyncio.sleep(5)
#
# async def process_queued_actions(client: Client):
#     while True:
#         try:
#             actions = []
#             while not answer_queue.empty():
#                 action = await answer_queue.get()
#                 actions.append(action)
#
#             if actions:
#                 for action in actions:
#                     callback_query_id = action['callback_query_id']
#                     action_type = action.get('type', 'answer')
#                     text = action['text']
#                     show_alert = action.get('show_alert', False)
#
#                     if action_type == 'answer':
#                         game_id = action['game_id']
#                         user_id = action['user_id']
#                         pure_data = action['pure_data']
#                         game = games.get(game_id)
#                         if not game:
#                             logger.warning(f"Game {game_id} not found for answer processing")
#                             continue
#                         current_question = game.current_question
#                         if current_question not in game.choices:
#                             game.choices[current_question] = {}
#                         if user_id in game.choices[current_question]:
#                             text = "⚠️ شما قبلاً برای این سوال گزینه‌ای انتخاب کرده‌اید"
#                             show_alert = False
#                         else:
#                             game.choices[current_question][user_id] = pure_data
#                             question_text, _, _, correct_answer = game.questions[current_question - 1]
#                             is_correct = pure_data[-1] == correct_answer[-1]
#                             if is_correct:
#                                 game.scores[user_id] = game.scores.get(user_id, 0) + 1
#                             text = "✅ پاسخ درست!" if is_correct else "❌ پاسخ نادرست!"
#                             show_alert = False
#                             logger.info(f"Processed answer for user {user_id} in game {game_id}: {pure_data}, Correct: {is_correct}")
#
#                     try:
#                         await client.answer_callback_query(
#                             callback_query_id=callback_query_id,
#                             text=text,
#                             show_alert=show_alert
#                         )
#                         logger.info(f"Processed action: {text} for callback {callback_query_id}")
#                     except QueryIdInvalid:
#                         logger.warning(f"Invalid callback query ID: {callback_query_id}")
#                     except Exception as e:
#                         logger.error(f"Failed to process action: {e}")
#
#                 logger.info(f"Processed {len(actions)} queued actions")
#
#             await asyncio.sleep(3)
#         except Exception as e:
#             logger.error(f"Error in process_queued_actions: {e}")
#             await asyncio.sleep(3)
#
# async def start_background_tasks(client):
#     global bot_id
#     try:
#         bot_id = (await client.get_me()).id
#         asyncio.create_task(cleanup_expired_games())
#         asyncio.create_task(announce_leaderboard(client))
#         asyncio.create_task(update_channel_members_periodically(client))
#         asyncio.create_task(cleanup_processed_queries())
#         asyncio.create_task(log_request_summary())
#         asyncio.create_task(process_queued_actions(client))
#     except Exception as e:
#         logger.error(f"Error in starting background tasks: {str(e)}")
#
# async def cleanup_processed_queries():
#     while True:
#         processed_queries.clear()
#         await asyncio.sleep(3600)
#
# async def log_request_summary():
#     while True:
#         logger.info(f"Summary: Total API Requests={api_request_counter}, Failed Requests={failed_request_counter}")
#         await asyncio.sleep(21600)# تایم بروز شدن لیست کانال
#
# async def announce_leaderboard(client):
#     while True:
#         try:
#             leaderboard = get_leaderboard()
#             if not leaderboard:
#                 message = "🌟 **هنوز هیچ بازیکنی در رتبه‌بندی ثبت نشده است!** 🌟\n" \
#                           "🎮 بیا و در چالش یار شرکت کن تا نامت اینجا بدرخشه! ✨"
#             else:
#                 message = "🌟 **جدول نفرات برتر چالش یار** 🌟\n\n"
#                 for idx, (username, total_correct) in enumerate(leaderboard, 1):
#                     if idx == 1:
#                         medal = "🥇"
#                     elif idx == 2:
#                         medal = "🥈"
#                     elif idx == 3:
#                         medal = "🥉"
#                     else:
#                         medal = f"{idx}."
#                     message += f"{medal} **{username}** - {total_correct} پاسخ درست 🎉\n"
#                 message += "\n🏆 به جمع برترین‌ها بپیوندید! 🚀"
#             await client.send_message(chat_id="@chalesh_yarr", text=message, disable_web_page_preview=True)
#             await asyncio.sleep(300)
#         except Exception as e:
#             logger.error(f"Error in announce_leaderboard: {str(e)}")
#             await asyncio.sleep(300)
#
# async def cleanup_expired_games():
#     while True:
#         try:
#             expired_games = [game_id for game_id, game in games.items() if game.is_expired()]
#             for game_id in expired_games:
#                 del games[game_id]
#                 logger.info(f"Cleaned up expired game: {game_id}")
#             await asyncio.sleep(300)
#         except Exception as e:
#             logger.error(f"Error in cleanup_expired_games: {str(e)}")
#             await asyncio.sleep(300)
#
# def test_db_connection():
#     db_path = "plugins/questions.db"
#     if not os.path.exists(db_path):
#         logger.error(f"Database file {db_path} does not exist")
#         return False
#     try:
#         with sqlite3.connect(db_path) as conn:
#             cursor = conn.cursor()
#             cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
#             return True
#     except Exception as e:
#         logger.error(f"Error testing DB connection: {str(e)}")
#         return False
#
# def get_random_questions(table_name, num_questions):
#     db_path = "plugins/questions.db"
#     try:
#         if not os.path.exists(db_path):
#             raise FileNotFoundError(f"فایل پایگاه داده {db_path} پیدا نشد!")
#         with sqlite3.connect(db_path) as conn:
#             cursor = conn.cursor()
#             cursor.execute(f"SELECT question, option1, option2, correct_answer FROM {table_name}")
#             questions = cursor.fetchall()
#         available_questions = [q for q in questions if str(q) not in used_questions]
#         if len(available_questions) < num_questions:
#             used_questions.clear()
#             available_questions = questions
#         if len(available_questions) < num_questions:
#             raise ValueError(f"تعداد سوالات کافی در جدول {table_name} وجود ندارد!")
#         selected_questions = random.sample(available_questions, num_questions)
#         processed_questions = []
#         for question, option1, option2, correct_answer in selected_questions:
#             question = question.replace("\\n", "\n")
#             option1 = option1.replace("\\n", "\n")
#             option2 = option2.replace("\\n", "\n")
#             correct_answer = correct_answer.replace("\\n", "\n")
#             processed_questions.append((question, option1, option2, correct_answer))
#             used_questions.add(str((question, option1, option2, correct_answer)))
#         return processed_questions
#     except Exception as e:
#         logger.error(f"Error getting random questions: {str(e)}")
#         return []
#
# def get_combined_questions(topics, total_questions):
#     try:
#         if not topics:
#             raise ValueError("هیچ موضوعی انتخاب نشده است!")
#         num_topics = len(topics)
#         questions_per_topic = max(1, total_questions // num_topics)
#         remaining_questions = total_questions - (questions_per_topic * num_topics)
#         all_questions = []
#         for topic in topics:
#             table_name = TOPIC_TO_TABLE.get(topic)
#             if not table_name:
#                 continue
#             num_questions = questions_per_topic + (1 if remaining_questions > 0 else 0)
#             if remaining_questions > 0:
#                 remaining_questions -= 1
#             questions = get_random_questions(table_name, min(num_questions, total_questions - len(all_questions)))
#             all_questions.extend(questions)
#         random.shuffle(all_questions)
#         return all_questions[:total_questions]
#     except Exception as e:
#         logger.error(f"Error getting combined questions: {str(e)}")
#         return []
#
# def create_options_keyboard(game_id, option1, option2):
#     return InlineKeyboardMarkup([
#         [InlineKeyboardButton(f"🔵 {option1}", callback_data=f"{game_id}|option_1")],
#         [InlineKeyboardButton(f"🟢 {option2}", callback_data=f"{game_id}|option_2")]
#     ])
#
# if not test_db_connection():
#     logger.critical("Failed to connect to database. Exiting.")
#     exit(1)
#
# init_leaderboard_db()
#
# @Client.on_inline_query()
# async def inline_main_menu(client: Client, inline_query):
#     user_id = inline_query.from_user.id
#     game = Game(user_id)
#     games[game.game_id] = game
#     logger.info(f"New game created: {game.game_id} by user {user_id}")
#     try:
#         # تنظیم متن با فاصله‌های خالی برای تطبیق با عرض دکمه‌ها
#         header = "به ربات سوالات خوش آمدید!                                              "
#         settings = game.get_settings_summary()
#         footer = "📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید.    "
#         message = f"{header}\n{settings}\n\n{footer}"
#         await inline_query.answer(
#             results=[
#                 InlineQueryResultArticle(
#                     title="🎮 تنظیمات بازی",
#                     description="تعداد سوال، زمان و دعوت از دوستان",
#                     input_message_content=InputTextMessageContent(message),
#                     reply_markup=my_start_def_glassButton(game.game_id)
#                 )
#             ],
#             cache_time=1
#         )
#     except Exception as e:
#         logger.error(f"Error in inline_main_menu: {str(e)}")
#         await inline_query.answer(
#             results=[],
#             cache_time=1,
#             switch_pm_text="⚠️ خطا در پردازش درخواست!",
#             switch_pm_parameter="error"
#         )
#
# def my_start_def_glassButton(game_id):
#     game = games.get(game_id)
#     if not game:
#         return InlineKeyboardMarkup([[InlineKeyboardButton("❌ بازی منقضی شده", callback_data="expired")]])
#     selections = game.selections
#     number = selections["number"]
#     times = selections["time"]
#     topics = selections["topics"]
#
#     def cb(data): return f"{game_id}|{data}"
#
#     return InlineKeyboardMarkup([
#         [InlineKeyboardButton("تعداد سوالات", callback_data=cb("numberofQ"))],
#         [InlineKeyboardButton(f"{n[4:]} {'✅' if number == n else ''}", callback_data=cb(n)) for n in
#          ["numb6", "numb8", "numb10", "numb12", "numb15", "numb18", "numb20"]],
#         [InlineKeyboardButton("⏱️ زمان پاسخ", callback_data=cb("timeForQ"))],
#         [InlineKeyboardButton(f"{t[4:]} {'✅' if t in times else ''}", callback_data=cb(t)) for t in
#          ["time10", "time15", "time20"]],
#         [InlineKeyboardButton("📚 انتخاب موضوع", callback_data=cb("selectTopic"))],
#         [
#             InlineKeyboardButton(f"اقتصاد کلان {'✅' if 'topic_economics' in topics else ''}", callback_data=cb("topic_economics")),
#             InlineKeyboardButton(f"توسعه {'✅' if 'topic_development' in topics else ''}", callback_data=cb("topic_development")),
#             InlineKeyboardButton(f"زبان تخصصی {'✅' if 'topic_macroeconomic' in topics else ''}", callback_data=cb("topic_macroeconomic"))
#         ],
#         [
#             InlineKeyboardButton(f"تجارت بین‌الملل {'✅' if 'topic_international_trade' in topics else ''}", callback_data=cb("topic_international_trade")),
#             InlineKeyboardButton(f"اقتصاد خرد {'✅' if 'topic_microeconomics' in topics else ''}", callback_data=cb("topic_microeconomics")),
#             InlineKeyboardButton(f"تاریخ عقاید {'✅' if 'topic_econthought_history' in topics else ''}", callback_data=cb("topic_econthought_history"))
#         ],
#         [InlineKeyboardButton("🤝 دعوت از دوستان", switch_inline_query=f"start_quiz_{game_id}")],
#         [InlineKeyboardButton("🎮 ساخت بازی", callback_data=cb("start_exam"))],
#         [InlineKeyboardButton("🗑️ لغو بازی", callback_data=cb("cancel_game"))]
#     ])
#
# @Client.on_callback_query()
# async def handle_callback_query(client: Client, callback_query: CallbackQuery):
#     if callback_query.id in processed_queries:
#         return
#     processed_queries.add(callback_query.id)
#     from_user = callback_query.from_user
#     from_user_id = from_user.id
#     data = callback_query.data
#     logger.info(f"Callback query from user {from_user_id}: {data}")
#     if data == "expired":
#         try:
#             await callback_query.answer("❌ این بازی منقضی شده است.", show_alert=True)
#         except QueryIdInvalid:
#             pass
#         return
#     try:
#         game_id, pure_data = data.split("|", 1)
#         game = games.get(game_id)
#         if not game:
#             try:
#                 await callback_query.answer("❌ بازی نامعتبر یا منقضی شده است", show_alert=True)
#             except QueryIdInvalid:
#                 pass
#             return
#         owner_id = game.owner_id
#     except ValueError:
#         try:
#             await callback_query.answer("❌ دکمه نامعتبر", show_alert=True)
#         except QueryIdInvalid:
#             pass
#         return
#     game.update_timestamp()
#     selections = game.selections
#     needs_update = False
#     if from_user_id != owner_id and pure_data not in ["ready_now", "option_1", "option_2"]:
#         try:
#             await callback_query.answer("⛔ فقط سازنده بازی می‌تواند تنظیمات را تغییر دهد", show_alert=True)
#         except QueryIdInvalid:
#             pass
#         return
#     try:
#         if pure_data.startswith("numb"):
#             selections["number"] = pure_data
#             needs_update = True
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "✅ انتخاب تغییر کرد",
#                 'show_alert': False
#             })
#         elif pure_data.startswith("time"):
#             selections["time"] = [pure_data]
#             needs_update = True
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "✅ انتخاب تغییر کرد",
#                 'show_alert': False
#             })
#         elif pure_data.startswith("topic_"):
#             if pure_data in selections["topics"]:
#                 selections["topics"].remove(pure_data)
#             else:
#                 selections["topics"].append(pure_data)
#             needs_update = True
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "✅ انتخاب تغییر کرد",
#                 'show_alert': False
#             })
#         elif pure_data == "start_exam":
#             if not selections["number"] or not selections["time"] or not selections["topics"]:
#                 await callback_query.answer("لطفاً همه فیلدها را انتخاب کنید ❗", show_alert=True)
#                 return
#             await callback_query.edit_message_text(
#                 f"🎯 لطفاً یکی از گزینه‌ها را انتخاب کنید:                                              \n\n{game.get_settings_summary()}\n\n{await get_players_list(client, game_id)}\n\n📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید.    ",
#                 reply_markup=InlineKeyboardMarkup([
#                     [InlineKeyboardButton("✅ حاضر", callback_data=f"{game_id}|ready_now")],
#                     [InlineKeyboardButton("🚀 شروع", callback_data=f"{game_id}|start_now")],
#                     [InlineKeyboardButton("🔙 برگشت به منو", callback_data=f"{game_id}|back_to_menu")],
#                     [InlineKeyboardButton("🗑️ لغو بازی", callback_data=f"{game_id}|cancel_game")]
#                 ])
#             )
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "✅ بازی ساخته شد",
#                 'show_alert': False
#             })
#             return
#         elif pure_data == "ready_now":
#             status_from_cache = check_member_in_cache(from_user_id)
#             if status_from_cache and status_from_cache in ["member", "administrator", "owner", "restricted"]:
#                 if from_user_id in game.players:
#                     await callback_query.answer("✅ شما قبلاً به بازی پیوسته‌اید", show_alert=True)
#                     return
#                 game.players.append(from_user_id)
#                 if from_user_id not in user_cache:
#                     user_cache[from_user_id] = from_user
#                 await callback_query.edit_message_text(
#                     f"🎯 لطفاً یکی از گزینه‌ها را انتخاب کنید:                                              \n\n{game.get_settings_summary()}\n\n{await get_players_list(client, game_id)}\n\n📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید.    ",
#                     reply_markup=InlineKeyboardMarkup([
#                         [InlineKeyboardButton("✅ حاضر", callback_data=f"{game_id}|ready_now")],
#                         [InlineKeyboardButton("🚀 شروع", callback_data=f"{game_id}|start_now")],
#                         [InlineKeyboardButton("🔙 برگشت به منو", callback_data=f"{game_id}|back_to_menu")],
#                         [InlineKeyboardButton("🗑️ لغو بازی", callback_data=f"{game_id}|cancel_game")]
#                     ])
#                 )
#                 await answer_queue.put({
#                     'callback_query_id': callback_query.id,
#                     'type': 'response',
#                     'text': "✅ شما به لیست بازیکنان اضافه شدید",
#                     'show_alert': False
#                 })
#                 return
#             await callback_query.answer("⛔ شما حاضر نیستید! لطفاً ابتدا عضو کانال چالش-یار شوید! 👉 @chalesh_yarr",
#                                         show_alert=True)
#             return
#         elif pure_data in ["option_1", "option_2"]:
#             if from_user_id not in game.players:
#                 await callback_query.answer("⛔ شما در این بازی حضور ندارید!", show_alert=True)
#                 return
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'answer',
#                 'game_id': game_id,
#                 'user_id': from_user_id,
#                 'pure_data': pure_data,
#                 'text': ""
#             })
#             return
#         elif pure_data == "start_now":
#             if from_user_id != owner_id:
#                 await callback_query.answer("⛔ فقط سازنده می‌تواند بازی را شروع کند", show_alert=True)
#                 return
#             if len(game.players) < 2:
#                 await callback_query.edit_message_text(
#                     f"⏳ در انتظار ورود بازیکن (حداقل 2 بازیکن مورد نیاز است):                                              \n\n{game.get_settings_summary()}\n\n{await get_players_list(client, game_id)}\n\n📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید.    ",
#                     reply_markup=InlineKeyboardMarkup([
#                         [InlineKeyboardButton("✅ حاضر", callback_data=f"{game_id}|ready_now")],
#                         [InlineKeyboardButton("🚀 شروع", callback_data=f"{game_id}|start_now")],
#                         [InlineKeyboardButton("🔙 برگشت به منو", callback_data=f"{game_id}|back_to_menu")],
#                         [InlineKeyboardButton("🗑️ لغو بازی", callback_data=f"{game_id}|cancel_game")]
#                     ])
#                 )
#                 await callback_query.answer("⛔ حداقل 2 بازیکن برای شروع لازم است", show_alert=True)
#                 return
#             time_str = selections["time"][0]
#             seconds = int(time_str.replace("time", ""))
#             total_questions = game.get_total_questions()
#             game.questions = get_combined_questions(selections["topics"], total_questions)
#             if not game.questions:
#                 await callback_query.answer("⚠️ خطا در بارگذاری سوالات! لطفاً موضوعات دیگر را انتخاب کنید.",
#                                             show_alert=True)
#                 return
#             for player_id in game.players:
#                 game.scores[player_id] = 0
#             for question_idx in range(total_questions):
#                 game.current_question = question_idx + 1
#                 game.choices[game.current_question] = {}
#                 question_text, option1, option2, _ = game.questions[question_idx]
#                 try:
#                     keyboard = create_options_keyboard(game_id, option1, option2)
#                     await callback_query.edit_message_text(
#                         f"❓ سوال {game.current_question} از {total_questions}\n⏳ {seconds} ثانیه وقت دارید:\n\n{question_text}\n\n{game.get_settings_summary()}",
#                         reply_markup=keyboard
#                     )
#                 except MessageNotModified:
#                     pass
#                 except Exception as e:
#                     logger.error(f"Error displaying question: {str(e)}")
#                     break
#                 await asyncio.sleep(seconds)
#             result_lines = ["📊 نتایج بازی:"]
#             sorted_players = sorted(game.players, key=lambda pid: game.scores.get(pid, 0), reverse=True)
#             for rank, player_id in enumerate(sorted_players, 1):
#                 player_name = user_cache.get(player_id, await client.get_users(player_id)).first_name
#                 result_lines.append(f"{rank}. {player_name}")
#                 status_row = []
#                 for question_idx in range(total_questions):
#                     question_num = question_idx + 1
#                     choice = game.choices.get(question_num, {}).get(player_id, None)
#                     if choice:
#                         _, _, _, correct_answer = game.questions[question_idx]
#                         is_correct = choice[-1] == correct_answer[-1]
#                         status_row.append("✅" if is_correct else "❌")
#                     else:
#                         status_row.append("☐")
#                 status_line = " ".join(status_row)
#                 result_lines.append(status_line)
#                 correct_count = status_row.count("✅")
#                 wrong_count = status_row.count("❌")
#                 unanswered_count = status_row.count("☐")
#                 result_lines.append(f"✅ {correct_count} | ❌ {wrong_count} | ☐ {unanswered_count}")
#                 save_player_score(player_id, player_name, correct_count)
#             try:
#                 if callback_query.message:
#                     await client.send_message(chat_id=callback_query.message.chat.id, text="\n".join(result_lines),
#                                               disable_web_page_preview=True)
#                 elif callback_query.inline_message_id:
#                     await callback_query.edit_message_text(text="\n".join(result_lines), disable_web_page_preview=True)
#                 del games[game_id]
#             except MessageNotModified:
#                 pass
#             except Exception as e:
#                 logger.error(f"Error displaying results: {str(e)}")
#                 try:
#                     await callback_query.answer("⚠️ خطایی در نمایش نتایج رخ داد", show_alert=True)
#                 except QueryIdInvalid:
#                     pass
#             return
#         elif pure_data == "back_to_menu":
#             await callback_query.edit_message_text(
#                 f"🎮 لطفاً تعداد سوال، زمان و موضوع را انتخاب کنید:                                              \n\n{game.get_settings_summary()}",
#                 reply_markup=my_start_def_glassButton(game_id)
#             )
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "🔙 برگشت به منو",
#                 'show_alert': False
#             })
#             return
#         elif pure_data == "cancel_game":
#             await callback_query.edit_message_text("🗑️ بازی لغو شد.")
#             del games[game_id]
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "✅ بازی لغو شد",
#                 'show_alert': False
#             })
#             return
#         if needs_update:
#             try:
#                 await callback_query.edit_message_text(
#                     f"🎮 لطفاً تعداد سوال، زمان و موضوع را انتخاب کنید:                                              \n\n{game.get_settings_summary()}",
#                     reply_markup=my_start_def_glassButton(game_id)
#                 )
#             except MessageNotModified:
#                 pass
#             except Exception as e:
#                 logger.error(f"Error updating message: {str(e)}")
#         else:
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "⚠️ این گزینه از قبل انتخاب شده",
#                 'show_alert': False
#             })
#     except Exception as e:
#         logger.error(f"Error in handle_callback_query: {str(e)}")
#         try:
#             await callback_query.answer("⚠️ خطایی رخ داد!", show_alert=True)
#         except QueryIdInvalid:
#             pass
#
# async def get_players_list(client, game_id):
#     game = games.get(game_id, Game(0))
#     if not game.players:
#         return "⏳ هنوز بازیکنی اضافه نشده!"
#     missing_users = [user_id for user_id in game.players if user_id not in user_cache]
#     if missing_users:
#         try:
#             users = await client.get_users(missing_users)
#             for user in users:
#                 user_cache[user.id] = user
#             await asyncio.sleep(0.1)
#         except Exception as e:
#             logger.error(f"Error fetching users: {str(e)}")
#     players_list = [
#         f"👤 {user_cache[user_id].first_name}" if user_id in user_cache else f"👤 کاربر ناشناس (ID: {user_id})" for
#         user_id in game.players]
#     return "👥 بازیکنان حاضر:\n" + "\n".join(players_list)



# import asyncio
# import uuid
# from datetime import datetime, timedelta
# from pyrogram import Client, filters
# from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, CallbackQuery
# import sqlite3
# import random
# import os
# import logging
# # from pyrogram.enums import ChatMemberStatus
# from pyrogram.errors import FloodWait, QueryIdInvalid, MessageNotModified
# import sys
#
# sys.stdout.reconfigure(encoding='utf-8')
#
# logging.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)s - %(levelname)s - %(message)s',
#     handlers=[
#         logging.FileHandler('telegram_api.log', encoding='utf-8'),
#         logging.StreamHandler()
#     ]
# )
# logger = logging.getLogger(__name__)
#
# bot_id = None
# last_access_check = None
# api_request_counter = 0
# failed_request_counter = 0
# processed_queries = set()
# used_questions = set()
# user_cache = {}
# channel_members_cache = {}
# channel_members_lock = asyncio.Lock()
# answer_queue = asyncio.Queue()
#
# TOPIC_TO_TABLE = {
#     "topic_economics": "questions_calan",
#     "topic_development": "questions_development",
#     "topic_macroeconomic": "questions_english",
#     "topic_international_trade": "questions_international_trade",
#     "topic_microeconomics": "questions_microeconomics",
#     "topic_econthought_history": "questions_EconThoughtHistory",
# }
#
# TOPIC_TO_PERSIAN = {
#     "topic_economics": "اقتصاد کلان",
#     "topic_development": "توسعه",
#     "topic_macroeconomic": "زبان تخصصی",
#     "topic_international_trade": "تجارت بین‌الملل",
#     "topic_microeconomics": "اقتصاد خرد",
#     "topic_econthought_history": "تاریخ عقاید اقتصادی",
# }
#
# original_methods = {
#     'send_message': Client.send_message,
#     'edit_message_text': Client.edit_message_text,
#     'answer_callback_query': Client.answer_callback_query,
#     'answer_inline_query': Client.answer_inline_query,
#     'get_chat': Client.get_chat,
#     'get_chat_member': Client.get_chat_member,
#     'get_users': Client.get_users,
#     'get_me': Client.get_me
# }
#
# def wrap_method(method_name, original_method):
#     async def wrapped(self, *args, **kwargs):
#         global api_request_counter, failed_request_counter
#         api_request_counter += 1
#         chat_id = args[0] if args and method_name == "get_chat" else "unknown"
#         logger.info(f"API Request #{api_request_counter}: Method={method_name}, ChatID={chat_id}, Args={args}, Kwargs={kwargs}")
#         try:
#             result = await original_method(self, *args, **kwargs)
#             logger.debug(f"API Request #{api_request_counter} Successful")
#             return result
#         except Exception as e:
#             failed_request_counter += 1
#             logger.error(f"API Request #{api_request_counter} Failed: Error={str(e)}")
#             raise e
#         finally:
#             logger.info(f"Total API Requests: {api_request_counter}, Failed: {failed_request_counter}")
#
#     return wrapped
#
# for method_name, original_method in original_methods.items():
#     setattr(Client, method_name, wrap_method(method_name, original_method))
#
# class Game:
#     def __init__(self, owner_id):
#         self.game_id = str(uuid.uuid4())
#         self.owner_id = owner_id
#         self.selections = {"number": None, "time": [], "topics": []}
#         self.players = []
#         self.choices = {}
#         self.created_at = datetime.now()
#         self.last_updated = datetime.now()
#         self.current_question = 0
#         self.questions = []
#         self.scores = {}
#
#     def update_timestamp(self):
#         self.last_updated = datetime.now()
#
#     def is_expired(self, timeout_minutes=30):
#         return datetime.now() - self.last_updated > timedelta(minutes=timeout_minutes)
#
#     def get_settings_summary(self):
#         number = self.selections["number"][4:] if self.selections["number"] else "انتخاب نشده"
#         time = self.selections["time"][0][4:] + " ثانیه" if self.selections["time"] else "انتخاب نشده"
#         # تبدیل موضوعات انتخاب‌شده به نام‌های فارسی
#         topics = ", ".join([TOPIC_TO_PERSIAN.get(t, t[6:].capitalize()) for t in self.selections["topics"]]) or "انتخاب نشده"
#         return f"📋 تنظیمات:\nسوالات: {number}\nزمان: {time}\nموضوعات: {topics}"
#
#     def get_total_questions(self):
#         return int(self.selections["number"][4:]) if self.selections["number"] else 0
#
# games = {}
#
# def init_leaderboard_db():
#     db_path = "plugins/questions.db"
#     try:
#         with sqlite3.connect(db_path) as conn:
#             cursor = conn.cursor()
#             cursor.execute("""
#                 CREATE TABLE IF NOT EXISTS leaderboard (
#                     user_id INTEGER,
#                     username TEXT,
#                     correct_answers INTEGER,
#                     game_date TEXT,
#                     PRIMARY KEY (user_id, game_date)
#                 )
#             """)
#             conn.commit()
#     except Exception as e:
#         logger.error(f"Error initializing leaderboard DB: {str(e)}")
#
# def save_player_score(user_id, username, correct_answers):
#     db_path = "plugins/questions.db"
#     try:
#         with sqlite3.connect(db_path) as conn:
#             cursor = conn.cursor()
#             game_date = datetime.now().strftime("%Y-%m-%d")
#             cursor.execute("SELECT correct_answers FROM leaderboard WHERE user_id = ? AND game_date = ?",
#                            (user_id, game_date))
#             result = cursor.fetchone()
#             if result:
#                 existing_correct_answers = result[0]
#                 total_correct_answers = existing_correct_answers + correct_answers
#                 cursor.execute(
#                     "UPDATE leaderboard SET correct_answers = ?, username = ? WHERE user_id = ? AND game_date = ?",
#                     (total_correct_answers, username, user_id, game_date))
#             else:
#                 cursor.execute(
#                     "INSERT INTO leaderboard (user_id, username, correct_answers, game_date) VALUES (?, ?, ?, ?)",
#                     (user_id, username, correct_answers, game_date))
#             conn.commit()
#     except Exception as e:
#         logger.error(f"Error saving player score: {str(e)}")
#
# def get_leaderboard():
#     db_path = "plugins/questions.db"
#     try:
#         with sqlite3.connect(db_path) as conn:
#             cursor = conn.cursor()
#             # ابتدا مجموع امتیازات را بر اساس user_id محاسبه می‌کنیم
#             cursor.execute(
#                 """
#                 SELECT user_id, SUM(correct_answers) as total_correct
#                 FROM leaderboard
#                 GROUP BY user_id
#                 ORDER BY total_correct DESC
#                 LIMIT 50
#                 """
#             )
#             results = cursor.fetchall()
#             leaderboard = []
#             for user_id, total_correct in results:
#                 # آخرین نام ثبت‌شده برای این user_id را می‌گیریم
#                 cursor.execute(
#                     """
#                     SELECT username
#                     FROM leaderboard
#                     WHERE user_id = ?
#                     ORDER BY game_date DESC
#                     LIMIT 1
#                     """,
#                     (user_id,)
#                 )
#                 username = cursor.fetchone()[0]
#                 leaderboard.append((username, total_correct))
#             return leaderboard
#     except Exception as e:
#         logger.error(f"Error fetching leaderboard: {str(e)}")
#         return []
#
# def check_member_in_cache(user_id):
#     return channel_members_cache.get(user_id, {}).get("status")
#
# async def sync_channel_members(client):
#     try:
#         async with channel_members_lock:
#             channel_members_cache.clear()
#             async for member in client.get_chat_members("@chalesh_yarr"):
#                 try:
#                     user_id = member.user.id
#                     status = member.status.value
#                     channel_members_cache[user_id] = {
#                         "status": status,
#                         "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#                     }
#                 except Exception as e:
#                     logger.error(f"Error syncing member {user_id}: {str(e)}")
#     except Exception as e:
#         logger.error(f"Error syncing channel members: {str(e)}")
#
# async def update_channel_members_periodically(client):
#     while True:
#         try:
#             await sync_channel_members(client)
#             await asyncio.sleep(5)
#         except Exception as e:
#             logger.error(f"Error in update_channel_members_periodically: {str(e)}")
#             await asyncio.sleep(5)
#
# async def process_queued_actions(client: Client):
#     while True:
#         try:
#             actions = []
#             while not answer_queue.empty():
#                 action = await answer_queue.get()
#                 actions.append(action)
#
#             if actions:
#                 for action in actions:
#                     callback_query_id = action['callback_query_id']
#                     action_type = action.get('type', 'answer')
#                     text = action['text']
#                     show_alert = action.get('show_alert', False)
#
#                     if action_type == 'answer':
#                         game_id = action['game_id']
#                         user_id = action['user_id']
#                         pure_data = action['pure_data']
#                         game = games.get(game_id)
#                         if not game:
#                             logger.warning(f"Game {game_id} not found for answer processing")
#                             continue
#                         current_question = game.current_question
#                         if current_question not in game.choices:
#                             game.choices[current_question] = {}
#                         if user_id in game.choices[current_question]:
#                             text = "⚠️ شما قبلاً برای این سوال گزینه‌ای انتخاب کرده‌اید"
#                             show_alert = False
#                         else:
#                             game.choices[current_question][user_id] = pure_data
#                             question_text, _, _, correct_answer = game.questions[current_question - 1]
#                             is_correct = pure_data[-1] == correct_answer[-1]
#                             if is_correct:
#                                 game.scores[user_id] = game.scores.get(user_id, 0) + 1
#                             text = "✅ پاسخ درست!" if is_correct else "❌ پاسخ نادرست!"
#                             show_alert = False
#                             logger.info(f"Processed answer for user {user_id} in game {game_id}: {pure_data}, Correct: {is_correct}")
#
#                     try:
#                         await client.answer_callback_query(
#                             callback_query_id=callback_query_id,
#                             text=text,
#                             show_alert=show_alert
#                         )
#                         logger.info(f"Processed action: {text} for callback {callback_query_id}")
#                     except QueryIdInvalid:
#                         logger.warning(f"Invalid callback query ID: {callback_query_id}")
#                     except Exception as e:
#                         logger.error(f"Failed to process action: {e}")
#
#                 logger.info(f"Processed {len(actions)} queued actions")
#
#             await asyncio.sleep(3)
#         except Exception as e:
#             logger.error(f"Error in process_queued_actions: {e}")
#             await asyncio.sleep(3)
#
# async def start_background_tasks(client):
#     global bot_id
#     try:
#         bot_id = (await client.get_me()).id
#         asyncio.create_task(cleanup_expired_games())
#         asyncio.create_task(announce_leaderboard(client))
#         asyncio.create_task(update_channel_members_periodically(client))
#         asyncio.create_task(cleanup_processed_queries())
#         asyncio.create_task(log_request_summary())
#         asyncio.create_task(process_queued_actions(client))
#     except Exception as e:
#         logger.error(f"Error in starting background tasks: {str(e)}")
#
# async def cleanup_processed_queries():
#     while True:
#         processed_queries.clear()
#         await asyncio.sleep(3600)
#
# async def log_request_summary():
#     while True:
#         logger.info(f"Summary: Total API Requests={api_request_counter}, Failed Requests={failed_request_counter}")
#         await asyncio.sleep(300)
#
# async def announce_leaderboard(client):
#     while True:
#         try:
#             leaderboard = get_leaderboard()
#             if not leaderboard:
#                 message = "🌟 **هنوز هیچ بازیکنی در رتبه‌بندی ثبت نشده است!** 🌟\n" \
#                           "🎮 بیا و در چالش یار شرکت کن تا نامت اینجا بدرخشه! ✨"
#             else:
#                 message = "🌟 **جدول نفرات برتر چالش یار** 🌟\n\n"
#                 for idx, (username, total_correct) in enumerate(leaderboard, 1):
#                     # برای نفرات اول تا سوم، مدال اضافه می‌کنیم
#                     if idx == 1:
#                         medal = "🥇"
#                     elif idx == 2:
#                         medal = "🥈"
#                     elif idx == 3:
#                         medal = "🥉"
#                     else:
#                         medal = f"{idx}."
#                     message += f"{medal} **{username}** - {total_correct} پاسخ درست 🎉\n"
#                 message += "\n🏆 به جمع برترین‌ها بپیوندید! 🚀"
#             await client.send_message(chat_id="@chalesh_yarr", text=message, disable_web_page_preview=True)
#             await asyncio.sleep(300)
#         except Exception as e:
#             logger.error(f"Error in announce_leaderboard: {str(e)}")
#             await asyncio.sleep(300)
#
# async def cleanup_expired_games():
#     while True:
#         try:
#             expired_games = [game_id for game_id, game in games.items() if game.is_expired()]
#             for game_id in expired_games:
#                 del games[game_id]
#                 logger.info(f"Cleaned up expired game: {game_id}")
#             await asyncio.sleep(300)
#         except Exception as e:
#             logger.error(f"Error in cleanup_expired_games: {str(e)}")
#             await asyncio.sleep(300)
#
# def test_db_connection():
#     db_path = "plugins/questions.db"
#     if not os.path.exists(db_path):
#         logger.error(f"Database file {db_path} does not exist")
#         return False
#     try:
#         with sqlite3.connect(db_path) as conn:
#             cursor = conn.cursor()
#             cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
#             return True
#     except Exception as e:
#         logger.error(f"Error testing DB connection: {str(e)}")
#         return False
#
# def get_random_questions(table_name, num_questions):
#     db_path = "plugins/questions.db"
#     try:
#         if not os.path.exists(db_path):
#             raise FileNotFoundError(f"فایل پایگاه داده {db_path} پیدا نشد!")
#         with sqlite3.connect(db_path) as conn:
#             cursor = conn.cursor()
#             cursor.execute(f"SELECT question, option1, option2, correct_answer FROM {table_name}")
#             questions = cursor.fetchall()
#         available_questions = [q for q in questions if str(q) not in used_questions]
#         if len(available_questions) < num_questions:
#             used_questions.clear()
#             available_questions = questions
#         if len(available_questions) < num_questions:
#             raise ValueError(f"تعداد سوالات کافی در جدول {table_name} وجود ندارد!")
#         selected_questions = random.sample(available_questions, num_questions)
#         # پردازش سوالات برای جایگزینی \n
#         processed_questions = []
#         for question, option1, option2, correct_answer in selected_questions:
#             # جایگزینی \n با خط جدید واقعی
#             question = question.replace("\\n", "\n")
#             option1 = option1.replace("\\n", "\n")
#             option2 = option2.replace("\\n", "\n")
#             correct_answer = correct_answer.replace("\\n", "\n")
#             processed_questions.append((question, option1, option2, correct_answer))
#             used_questions.add(str((question, option1, option2, correct_answer)))
#         return processed_questions
#     except Exception as e:
#         logger.error(f"Error getting random questions: {str(e)}")
#         return []
#
# def get_combined_questions(topics, total_questions):
#     try:
#         if not topics:
#             raise ValueError("هیچ موضوعی انتخاب نشده است!")
#         num_topics = len(topics)
#         questions_per_topic = max(1, total_questions // num_topics)
#         remaining_questions = total_questions - (questions_per_topic * num_topics)
#         all_questions = []
#         for topic in topics:
#             table_name = TOPIC_TO_TABLE.get(topic)
#             if not table_name:
#                 continue
#             num_questions = questions_per_topic + (1 if remaining_questions > 0 else 0)
#             if remaining_questions > 0:
#                 remaining_questions -= 1
#             questions = get_random_questions(table_name, min(num_questions, total_questions - len(all_questions)))
#             all_questions.extend(questions)
#         random.shuffle(all_questions)
#         return all_questions[:total_questions]
#     except Exception as e:
#         logger.error(f"Error getting combined questions: {str(e)}")
#         return []
#
# def create_options_keyboard(game_id, option1, option2):
#     return InlineKeyboardMarkup([
#         [InlineKeyboardButton(f"🔵 {option1}", callback_data=f"{game_id}|option_1")],
#         [InlineKeyboardButton(f"🟢 {option2}", callback_data=f"{game_id}|option_2")]
#     ])
#
# if not test_db_connection():
#     logger.critical("Failed to connect to database. Exiting.")
#     exit(1)
#
# init_leaderboard_db()
#
# @Client.on_inline_query()
# async def inline_main_menu(client: Client, inline_query):
#     user_id = inline_query.from_user.id
#     game = Game(user_id)
#     games[game.game_id] = game
#     logger.info(f"New game created: {game.game_id} by user {user_id}")
#     try:
#         await inline_query.answer(
#             results=[
#                 InlineQueryResultArticle(
#                     title="🎮 تنظیمات بازی",
#                     description="تعداد سوال، زمان و دعوت از دوستان",
#                     input_message_content=InputTextMessageContent(
#                         f"به ربات سوالات خوش آمدید!\n{game.get_settings_summary()}\n\n📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید."
#                     ),
#                     reply_markup=my_start_def_glassButton(game.game_id)
#                 )
#             ],
#             cache_time=1
#         )
#     except Exception as e:
#         logger.error(f"Error in inline_main_menu: {str(e)}")
#         await inline_query.answer(
#             results=[],
#             cache_time=1,
#             switch_pm_text="⚠️ خطا در پردازش درخواست!",
#             switch_pm_parameter="error"
#         )
#
# def my_start_def_glassButton(game_id):
#     game = games.get(game_id)
#     if not game:
#         return InlineKeyboardMarkup([[InlineKeyboardButton("❌ بازی منقضی شده", callback_data="expired")]])
#     selections = game.selections
#     number = selections["number"]
#     times = selections["time"]
#     topics = selections["topics"]
#
#     def cb(data): return f"{game_id}|{data}"
#
#     return InlineKeyboardMarkup([
#         [InlineKeyboardButton("تعداد سوالات", callback_data=cb("numberofQ"))],
#         [InlineKeyboardButton(f"{n[4:]} {'✅' if number == n else ''}", callback_data=cb(n)) for n in
#          ["numb6", "numb8", "numb10", "numb12", "numb15", "numb18", "numb20"]],
#         [InlineKeyboardButton("⏱️ زمان پاسخ", callback_data=cb("timeForQ"))],
#         [InlineKeyboardButton(f"{t[4:]} {'✅' if t in times else ''}", callback_data=cb(t)) for t in
#          ["time10", "time15", "time20"]],
#         [InlineKeyboardButton("📚 انتخاب موضوع", callback_data=cb("selectTopic"))],
#         [
#             InlineKeyboardButton(f"اقتصاد کلان {'✅' if 'topic_economics' in topics else ''}", callback_data=cb("topic_economics")),
#             InlineKeyboardButton(f"توسعه {'✅' if 'topic_development' in topics else ''}", callback_data=cb("topic_development")),
#             InlineKeyboardButton(f"زبان تخصصی {'✅' if 'topic_macroeconomic' in topics else ''}", callback_data=cb("topic_macroeconomic"))
#         ],
#         [
#             InlineKeyboardButton(f"تجارت بین‌الملل {'✅' if 'topic_international_trade' in topics else ''}", callback_data=cb("topic_international_trade")),
#             InlineKeyboardButton(f"اقتصاد خرد {'✅' if 'topic_microeconomics' in topics else ''}", callback_data=cb("topic_microeconomics")),
#             InlineKeyboardButton(f"تاریخ عقاید {'✅' if 'topic_econthought_history' in topics else ''}", callback_data=cb("topic_econthought_history"))
#         ],
#         [InlineKeyboardButton("🤝 دعوت از دوستان", switch_inline_query=f"start_quiz_{game_id}")],
#         [InlineKeyboardButton("🎮 ساخت بازی", callback_data=cb("start_exam"))],
#         [InlineKeyboardButton("🗑️ لغو بازی", callback_data=cb("cancel_game"))]
#     ])
#
# @Client.on_callback_query()
# async def handle_callback_query(client: Client, callback_query: CallbackQuery):
#     if callback_query.id in processed_queries:
#         return
#     processed_queries.add(callback_query.id)
#     from_user = callback_query.from_user
#     from_user_id = from_user.id
#     data = callback_query.data
#     logger.info(f"Callback query from user {from_user_id}: {data}")
#     if data == "expired":
#         try:
#             await callback_query.answer("❌ این بازی منقضی شده است.", show_alert=True)
#         except QueryIdInvalid:
#             pass
#         return
#     try:
#         game_id, pure_data = data.split("|", 1)
#         game = games.get(game_id)
#         if not game:
#             try:
#                 await callback_query.answer("❌ بازی نامعتبر یا منقضی شده است", show_alert=True)
#             except QueryIdInvalid:
#                 pass
#             return
#         owner_id = game.owner_id
#     except ValueError:
#         try:
#             await callback_query.answer("❌ دکمه نامعتبر", show_alert=True)
#         except QueryIdInvalid:
#             pass
#         return
#     game.update_timestamp()
#     selections = game.selections
#     needs_update = False
#     if from_user_id != owner_id and pure_data not in ["ready_now", "option_1", "option_2"]:
#         try:
#             await callback_query.answer("⛔ فقط سازنده بازی می‌تواند تنظیمات را تغییر دهد", show_alert=True)
#         except QueryIdInvalid:
#             pass
#         return
#     try:
#         if pure_data.startswith("numb"):
#             selections["number"] = pure_data
#             needs_update = True
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "✅ انتخاب تغییر کرد",
#                 'show_alert': False
#             })
#         elif pure_data.startswith("time"):
#             selections["time"] = [pure_data]
#             needs_update = True
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "✅ انتخاب تغییر کرد",
#                 'show_alert': False
#             })
#         elif pure_data.startswith("topic_"):
#             if pure_data in selections["topics"]:
#                 selections["topics"].remove(pure_data)
#             else:
#                 selections["topics"].append(pure_data)
#             needs_update = True
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "✅ انتخاب تغییر کرد",
#                 'show_alert': False
#             })
#         elif pure_data == "start_exam":
#             if not selections["number"] or not selections["time"] or not selections["topics"]:
#                 await callback_query.answer("لطفاً همه فیلدها را انتخاب کنید ❗", show_alert=True)
#                 return
#             await callback_query.edit_message_text(
#                 f"🎯 لطفاً یکی از گزینه‌ها را انتخاب کنید:\n\n{game.get_settings_summary()}\n\n{await get_players_list(client, game_id)}\n\n📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید.",
#                 reply_markup=InlineKeyboardMarkup([
#                     [InlineKeyboardButton("✅ حاضر", callback_data=f"{game_id}|ready_now")],
#                     [InlineKeyboardButton("🚀 شروع", callback_data=f"{game_id}|start_now")],
#                     [InlineKeyboardButton("🔙 برگشت به منو", callback_data=f"{game_id}|back_to_menu")],
#                     [InlineKeyboardButton("🗑️ لغو بازی", callback_data=f"{game_id}|cancel_game")]
#                 ])
#             )
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "✅ بازی ساخته شد",
#                 'show_alert': False
#             })
#             return
#         elif pure_data == "ready_now":
#             status_from_cache = check_member_in_cache(from_user_id)
#             if status_from_cache and status_from_cache in ["member", "administrator", "owner", "restricted"]:
#                 if from_user_id in game.players:
#                     await callback_query.answer("✅ شما قبلاً به بازی پیوسته‌اید", show_alert=True)
#                     return
#                 game.players.append(from_user_id)
#                 if from_user_id not in user_cache:
#                     user_cache[from_user_id] = from_user
#                 await callback_query.edit_message_text(
#                     f"🎯 لطفاً یکی از گزینه‌ها را انتخاب کنید:\n\n{game.get_settings_summary()}\n\n{await get_players_list(client, game_id)}\n\n📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید.",
#                     reply_markup=InlineKeyboardMarkup([
#                         [InlineKeyboardButton("✅ حاضر", callback_data=f"{game_id}|ready_now")],
#                         [InlineKeyboardButton("🚀 شروع", callback_data=f"{game_id}|start_now")],
#                         [InlineKeyboardButton("🔙 برگشت به منو", callback_data=f"{game_id}|back_to_menu")],
#                         [InlineKeyboardButton("🗑️ لغو بازی", callback_data=f"{game_id}|cancel_game")]
#                     ])
#                 )
#                 await answer_queue.put({
#                     'callback_query_id': callback_query.id,
#                     'type': 'response',
#                     'text': "✅ شما به لیست بازیکنان اضافه شدید",
#                     'show_alert': False
#                 })
#                 return
#             await callback_query.answer("⛔ شما حاضر نیستید! لطفاً ابتدا عضو کانال چالش-یار شوید! 👉 @chalesh_yarr",
#                                         show_alert=True)
#             return
#         elif pure_data in ["option_1", "option_2"]:
#             if from_user_id not in game.players:
#                 await callback_query.answer("⛔ شما در این بازی حضور ندارید!", show_alert=True)
#                 return
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'answer',
#                 'game_id': game_id,
#                 'user_id': from_user_id,
#                 'pure_data': pure_data,
#                 'text': ""
#             })
#             return
#         elif pure_data == "start_now":
#             if from_user_id != owner_id:
#                 await callback_query.answer("⛔ فقط سازنده می‌تواند بازی را شروع کند", show_alert=True)
#                 return
#             if len(game.players) < 2:
#                 await callback_query.edit_message_text(
#                     f"⏳ در انتظار ورود بازیکن (حداقل 2 بازیکن مورد نیاز است):\n\n{game.get_settings_summary()}\n\n{await get_players_list(client, game_id)}\n\n📢 برای شرکت در بازی باید عضو کانال چالش-یار (@chalesh_yarr) باشید.",
#                     reply_markup=InlineKeyboardMarkup([
#                         [InlineKeyboardButton("✅ حاضر", callback_data=f"{game_id}|ready_now")],
#                         [InlineKeyboardButton("🚀 شروع", callback_data=f"{game_id}|start_now")],
#                         [InlineKeyboardButton("🔙 برگشت به منو", callback_data=f"{game_id}|back_to_menu")],
#                         [InlineKeyboardButton("🗑️ لغو بازی", callback_data=f"{game_id}|cancel_game")]
#                     ])
#                 )
#                 await callback_query.answer("⛔ حداقل 2 بازیکن برای شروع لازم است", show_alert=True)
#                 return
#             time_str = selections["time"][0]
#             seconds = int(time_str.replace("time", ""))
#             total_questions = game.get_total_questions()
#             game.questions = get_combined_questions(selections["topics"], total_questions)
#             if not game.questions:
#                 await callback_query.answer("⚠️ خطا در بارگذاری سوالات! لطفاً موضوعات دیگر را انتخاب کنید.",
#                                             show_alert=True)
#                 return
#             for player_id in game.players:
#                 game.scores[player_id] = 0
#             for question_idx in range(total_questions):
#                 game.current_question = question_idx + 1
#                 game.choices[game.current_question] = {}
#                 question_text, option1, option2, _ = game.questions[question_idx]
#                 try:
#                     keyboard = create_options_keyboard(game_id, option1, option2)
#                     await callback_query.edit_message_text(
#                         f"❓ سوال {game.current_question} از {total_questions}\n⏳ {seconds} ثانیه وقت دارید:\n\n{question_text}\n\n{game.get_settings_summary()}",
#                         reply_markup=keyboard
#                     )
#                 except MessageNotModified:
#                     pass
#                 except Exception as e:
#                     logger.error(f"Error displaying question: {str(e)}")
#                     break
#                 await asyncio.sleep(seconds)
#             result_lines = ["📊 نتایج بازی:"]
#             sorted_players = sorted(game.players, key=lambda pid: game.scores.get(pid, 0), reverse=True)
#             for rank, player_id in enumerate(sorted_players, 1):
#                 player_name = user_cache.get(player_id, await client.get_users(player_id)).first_name
#                 result_lines.append(f"{rank}. {player_name}")
#                 status_row = []
#                 for question_idx in range(total_questions):
#                     question_num = question_idx + 1
#                     choice = game.choices.get(question_num, {}).get(player_id, None)
#                     if choice:
#                         _, _, _, correct_answer = game.questions[question_idx]
#                         is_correct = choice[-1] == correct_answer[-1]
#                         status_row.append("✅" if is_correct else "❌")
#                     else:
#                         status_row.append("☐")
#                 status_line = " ".join(status_row)
#                 result_lines.append(status_line)
#                 correct_count = status_row.count("✅")
#                 wrong_count = status_row.count("❌")
#                 unanswered_count = status_row.count("☐")
#                 result_lines.append(f"✅ {correct_count} | ❌ {wrong_count} | ☐ {unanswered_count}")
#                 save_player_score(player_id, player_name, correct_count)
#             try:
#                 if callback_query.message:
#                     await client.send_message(chat_id=callback_query.message.chat.id, text="\n".join(result_lines),
#                                               disable_web_page_preview=True)
#                 elif callback_query.inline_message_id:
#                     await callback_query.edit_message_text(text="\n".join(result_lines), disable_web_page_preview=True)
#                 del games[game_id]
#             except MessageNotModified:
#                 pass
#             except Exception as e:
#                 logger.error(f"Error displaying results: {str(e)}")
#                 try:
#                     await callback_query.answer("⚠️ خطایی در نمایش نتایج رخ داد", show_alert=True)
#                 except QueryIdInvalid:
#                     pass
#             return
#         elif pure_data == "back_to_menu":
#             await callback_query.edit_message_text(
#                 f"🎮 لطفاً تعداد سوال، زمان و موضوع را انتخاب کنید:\n\n{game.get_settings_summary()}",
#                 reply_markup=my_start_def_glassButton(game_id)
#             )
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "🔙 برگشت به منو",
#                 'show_alert': False
#             })
#             return
#         elif pure_data == "cancel_game":
#             await callback_query.edit_message_text("🗑️ بازی لغو شد.")
#             del games[game_id]
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "✅ بازی لغو شد",
#                 'show_alert': False
#             })
#             return
#         if needs_update:
#             try:
#                 await callback_query.edit_message_text(
#                     f"🎮 لطفاً تعداد سوال، زمان و موضوع را انتخاب کنید:\n\n{game.get_settings_summary()}",
#                     reply_markup=my_start_def_glassButton(game_id)
#                 )
#             except MessageNotModified:
#                 pass
#             except Exception as e:
#                 logger.error(f"Error updating message: {str(e)}")
#         else:
#             await answer_queue.put({
#                 'callback_query_id': callback_query.id,
#                 'type': 'response',
#                 'text': "⚠️ این گزینه از قبل انتخاب شده",
#                 'show_alert': False
#             })
#     except Exception as e:
#         logger.error(f"Error in handle_callback_query: {str(e)}")
#         try:
#             await callback_query.answer("⚠️ خطایی رخ داد!", show_alert=True)
#         except QueryIdInvalid:
#             pass
#
# async def get_players_list(client, game_id):
#     game = games.get(game_id, Game(0))
#     if not game.players:
#         return "⏳ هنوز بازیکنی اضافه نشده!"
#     missing_users = [user_id for user_id in game.players if user_id not in user_cache]
#     if missing_users:
#         try:
#             users = await client.get_users(missing_users)
#             for user in users:
#                 user_cache[user.id] = user
#             await asyncio.sleep(0.1)
#         except Exception as e:
#             logger.error(f"Error fetching users: {str(e)}")
#     players_list = [
#         f"👤 {user_cache[user_id].first_name}" if user_id in user_cache else f"👤 کاربر ناشناس (ID: {user_id})" for
#         user_id in game.players]
#     return "👥 بازیکنان حاضر:\n" + "\n".join(players_list)
