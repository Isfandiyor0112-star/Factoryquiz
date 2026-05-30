import os
import json
import aiohttp
import asyncio
from fastapi import FastAPI, Request, Response
from aiogram import Bot, Dispatcher, types, Router
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
router = Router()

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
    
    system_prompt = (
        "Sen 'ProfPack' qog'ozni qayta ishlash zavodining xavfsizlik bo'yicha nazoratchisisan. "
        "Sening vazifang zavod xodimlari uchun birinchi yordam, yong'in xavfsizligi va ishlab chiqarish "
        "texnika xavfsizligi bo'yicha real va professional test savollarini o'zbek tilida tuzish."
    )
    
    user_prompt = (
        "ProfPack qog'ozni qayta ishlash zavodi ishchilari uchun real hayotiy test yarating. "
        "DIQQAT: IT, dasturlash yoki kompyuterlarga oid SAVOLLAR TUZISH MUTLAQO TAQIQLANADI! "
        "Savol faqat va faqat sexdagi real narsalar haqida bo'lsin: qog'oz pressi, stanoklar, "
        "yuk ko'targichlar (karr), yong'in o'chirish moslamasi (ognetushitel) yoki jarohatlanganda birinchi yordam. "
        "To'g'ri javob mantiqan to'g'ri va qisqa bo'lsin va uni HAR DOIM 1-chi variantga qo'ying (correct_id: 0). "
        "Javobni FAQAT shu formatda qaytaring:\n"
        '{"question": "Savol matni", "options": ["To\'g\'ri javob matni", "Noto\'g\'ri variant 1", "Noto\'g\'ri variant 2", "Noto\'g\'ri variant 3"], "correct_id": 0}'
    )


       # Максимально надежный и расширенный список моделей
    models_to_try = [
        "meta-llama/llama-3-8b-instruct:free",        # Ультрабыстрая, стабильная, отличный JSON
        "google/gemma-4-26b-a4b-it:free",              # Наш фаворит, если свободен
        "nvidia/nemotron-nano-12b-v2-vl:free",         # Тот самый щит, который уже спасал
        "mistralai/mistral-7b-instruct:free",          # Очень стабильный европеец
        "openai/gpt-oss-120b:free",                    # Умная, но тяжелая
        "openai/gpt-oss-20b:free"                      # Быстрее, чем старший брат
    ]
    
    async with aiohttp.ClientSession() as session:
        for model in models_to_try:
            data = {
                "model": model, 
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.4, # Чуть подняли для уверенности
                "max_tokens": 400
            }
            
            try:
                print(f"[AI] Запрос к модели: {model}")
                # Увеличиваем таймаут до 4.5 секунд на одну модель
                async with session.post(url, headers=headers, json=data, timeout=4.5) as response:
                    
                    if response.status == 200:
                        print(f"[AI] {model} -> ✅ Статус 200 OK")
                        res_json = await response.json()
                        
                        choices = res_json.get('choices', [])
                        if not choices:
                            continue
                            
                        raw_content = choices[0].get('message', {}).get('content')
                        if not raw_content:
                            continue
                            
                        content = raw_content.strip()
                        start = content.find('{')
                        end = content.rfind('}') + 1
                        
                        if start != -1 and end != 0:
                            quiz_json = json.loads(content[start:end])
                            opts = quiz_json.get("options", [])
                            if len(opts) == 4 and len(str(opts[0])) > 2:
                                print(f"[AI] {model} -> Успешный JSON!")
                                return quiz_json
                    
                    elif response.status == 429:
                        print(f"[AI] {model} -> ❌ Ошибка 429 (Лимиты)")
                    else:
                        print(f"[AI] {model} -> 🚫 Ошибка {response.status}")
                        
            except asyncio.TimeoutError:
                print(f"[AI] {model} -> ⏳ Превышен таймаут генерации")
            except Exception as e:
                print(f"[AI] {model} -> Ошибка: {e}")
                continue

                
    print("[AI] 🛑 Ни одна модель из списка не смогла сгенерировать опрос.")
    return None



# --- ХЭНДЛЕРЫ КОМАНД ---

@router.message(Command("generate_quiz"))
async def admin_start_quiz(message: types.Message):
    print(f"[ХЭНДЛЕР] Команда вызвана пользователем {message.from_user.id} в чате {message.chat.id}")
    
    if message.chat.type not in ['group', 'supergroup']:
        await message.answer("Bu buyruqni faqat guruhda ishlatish mumkin!")
        return

    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in ['administrator', 'creator']:
            await message.answer("Sizda guruh administratori huquqlari yo'q!")
            return
    except Exception as e:
        print(f"[ХЭНДЛЕР] Ошибка проверки прав: {e}")
        return

    status_msg = await message.answer("🔄 *Sun'iy intellekt savol o'ylayapti, kuting...*")
    
    quiz_data = await generate_ai_quiz()
    
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
    except:
        pass

    if not quiz_data:
        await message.answer("❌ Xatolik yuz berdi. Qayta urinib ko'ring.")
        return

    try:
        poll_msg = await bot.send_poll(
            chat_id=message.chat.id,
            question=quiz_data["question"],
            options=quiz_data["options"],
            type='quiz',
            correct_option_id=int(quiz_data["correct_id"]),
            is_anonymous=False
        )
        
        await quizzes_collection.insert_one({
            "_id": poll_msg.poll.id,
            "chat_id": message.chat.id,
            "question": quiz_data["question"],
            "correct_id": int(quiz_data["correct_id"])
        })
        print("[ХЭНДЛЕР] Опрос успешно отправлен!")
    except Exception as e:
        print(f"[ХЭНДЛЕР] Ошибка отправки пула: {e}")


@router.poll_answer()
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


@router.message(Command("stats"))
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

# Регистрируем роутер в диспетчере
dp.include_router(router)

# --- ВЕБХУК ДЛЯ VERCEL ---
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        update_dict = await request.json()
        print(f"[ВЕБХУК] Входящий апдейт от ТГ: {json.dumps(update_dict)}")
        
        update = types.Update(**update_dict)
        await dp.feed_update(bot, update)
        
    except Exception as e:
        print(f"[ВЕБХУК ОШИБКА] Ошибка парсинга или обработки: {e}")
        
    return Response(status_code=200)


@app.get("/")
async def index():
    try:
        await bot.set_my_commands(
            [BotCommand(command="generate_quiz", description="AI test yaratish")],
            scope=BotCommandScopeAllChatAdministrators()
        )
    except Exception as e:
        print(f"Ошибка меню команд: {e}")
    return {"status": "Бот запущен на Vercel!"}
