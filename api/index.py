import os
import json
import aiohttp
import asyncio
from fastapi import FastAPI, Request, Response, BackgroundTasks
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeAllChatAdministrators
from motor.motor_asyncio import AsyncIOMotorClient

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
MONGO_URL = os.getenv("MONGO_URL")

app = FastAPI()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ПОДКЛЮЧЕНИЕ К MONGODB ---
cluster = AsyncIOMotorClient(MONGO_URL)
db = cluster["quiz_bot_db"]
quizzes_collection = db["quizzes"]
answers_collection = db["answers"]

# --- ФУНКЦИЯ ЗАПРОСА К ИИ ---
async def generate_ai_quiz():
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json"
    }
    
    prompt = (
        "Ishchilar uchun texnika xavfsizligi, yong'in xavfsizligi yoki birinchi yordamga oid "
        "tasodifiy bitta qiziqarli test savolini o'zbek tilida yarat. "
        "Javobni FAQAT mana bu JSON formatda qaytar, boshqa hech qanday tekst, kirish so'zi yoki markdown belgilari yozma:\n"
        "{\n"
        '  "question": "Savol matni",\n'
        '  "options": ["1-javob", "2-javob", "3-javob", "4-javob"],\n'
        '  "correct_id": 0\n'
        "}\n"
        "Eslatma: correct_id 0 dan 3 gacha bo'lgan to'g'ri javob indeksi bo'lsin."
    )
    
    # Каскад бесплатных моделей из твоего списка
    models_to_try = [
        "deepseek/deepseek-v4-flash:free",
        "meta-llama/llama-3.3-70b-instruct:free"
    ]
    
    async with aiohttp.ClientSession() as session:
        for model in models_to_try:
            print(f"Пытаюсь сгенерировать через модель: {model}")
            data = {
                "model": model, 
                "messages": [{"role": "user", "content": prompt}]
            }
            
            try:
                # Ставим 4.5 секунды на модель, чтобы суммарно уложиться в 10 секунд бесплатного тарифа Vercel
                async with session.post(url, headers=headers, json=data, timeout=4.5) as response:
                    if response.status != 200:
                        print(f"Модель {model} выдала ошибку со статусом: {response.status}. Переключаюсь...")
                        continue
                        
                    result = await response.json()
                    if 'choices' not in result:
                        print(f"Модель {model} вернула некорректный ответ. Переключаюсь...")
                        continue
                        
                    content = result['choices'][0]['message']['content'].strip()
                    print(f"Успешно! Ответ от {model}: {content}")
                    
                    # Очищаем markdown-теги, если ИИ их добавил
                    if "```" in content:
                        content = content.split("```")[1]
                        if content.startswith("json"):
                            content = content[4:]
                    
                    return json.loads(content.strip())
                    
            except Exception as e:
                print(f"Ошибка или таймаут модели {model}: {e}. Пробую следующую...")
                continue
                
    print("Ни одна из нейросетей не ответила успешно за отведенное время.")
    return None


# --- ФОНОВАЯ ЗАДАЧА ---
async def async_quiz_task(chat_id: int, status_msg_id: int):
    quiz_data = await generate_ai_quiz()
    try:
        await bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
    except:
        pass

    if not quiz_data:
        await bot.send_message(chat_id=chat_id, text="❌ Xatolik yuz berdi. Qayta urinib ko'ring.")
        return

    poll_msg = await bot.send_poll(
        chat_id=chat_id,
        question=quiz_data["question"],
        options=quiz_data["options"],
        type='quiz',
        correct_option_id=int(quiz_data["correct_id"]),
        is_anonymous=False
    )
    
    await quizzes_collection.insert_one({
        "_id": poll_msg.poll.id,
        "chat_id": chat_id,
        "question": quiz_data["question"],
        "correct_id": int(quiz_data["correct_id"])
    })


# --- КОМАНДЫ БОТА ---
@dp.message(Command("generate_quiz"))
async def admin_start_quiz(message: types.Message, background_tasks: BackgroundTasks):
    if message.chat.type not in ['group', 'supergroup']:
        await message.answer("Bu buyruqni faqat guruhda ishlatish mumkin!")
        return

    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ['administrator', 'creator']:
        await message.answer("Sizda guruh administratori huquqlari yo'q!")
        return

    status_msg = await message.answer("🔄 *Sun'iy intellekt savol o'ylayapti, kuting...*")
    
    # Легальный запуск тяжелой задачи в фоне через FastAPI.
    # Мы мгновенно отвечаем Телеграму "200 OK", но Vercel дает процессу завершить генерацию.
    background_tasks.add_task(async_quiz_task, message.chat.id, status_msg.message_id)


@dp.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer):
    poll_id = poll_answer.poll_id
    quiz = await quizzes_collection.find_one({"_id": poll_id})
    if quiz:
        user_id = poll_answer.user.id
        user_name = poll_answer.user.full_name
        selected_id = poll_answer.option_ids[0]
        is_correct = selected_id == quiz["correct_id"]
        
        await answers_collection.update_one(
            {"poll_id": poll_id, "user_id": user_id},
            {"$set": {"user_name": user_name, "correct": is_correct}},
            upsert=True
        )


@dp.message(Command("stats"))
async def show_stats(message: types.Message):
    if message.chat.type != 'private':
        await message.answer("Statistikani faqat botning o'zida (Lichka) ko'rishingiz mumkin!")
        return
        
    text = "📊 *Xodimlar javoblari statistikasi:*\n\n"
    cursor = quizzes_collection.find()
    async for quiz in cursor:
        text += f"❓ *Savol:* {quiz['question']}\n"
        has_answers = False
        async for answer in answers_collection.find({"poll_id": quiz["_id"]}):
            has_answers = True
            status = "✅" if answer["correct"] else "❌"
            text += f" └ {status} {answer['user_name']}\n"
        if not has_answers:
            text += " └ _Hozircha javoblar yo'q_\n"
        text += "\n"
        
    if text == "📊 *Xodimlar javoblari statistikasi:*\n\n":
        await message.answer("Hozircha bazada hech qanday test yo'q.")
    else:
        await message.answer(text, parse_mode="Markdown")


# --- ВЕБХУК ДЛЯ VERCEL ---
@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    update_dict = await request.json()
    update = types.Update(**update_dict)
    
    # Передаем background_tasks внутрь диспетчера aiogram, 
    # чтобы хэндлер /generate_quiz мог его использовать
    await dp.feed_update(bot, update, background_tasks=background_tasks)
    return Response(status_code=200)


@app.get("/")
async def index():
    # Автоматическая настройка меню команд в Telegram при открытии главной страницы
    try:
        await bot.set_my_commands(
            [BotCommand(command="generate_quiz", description="AI test yaratish")],
            scope=BotCommandScopeAllChatAdministrators()
        )
    except Exception as e:
        print(f"Ошибка установки меню команд: {e}")
        
    return {"status": "Бот запущен на Vercel!"}
