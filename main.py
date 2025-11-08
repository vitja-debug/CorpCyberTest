import os
import asyncio
import asyncpg
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Document,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiohttp import web
from openai import AsyncOpenAI
import tempfile
# -----------------------------------------------------------------------------
# Конфігурація: читаємо токен і параметри підключення до БД
# -----------------------------------------------------------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 10000))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not DATABASE_URL:
    DB_USER = os.getenv("POSTGRES_USER", "postgres")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
    DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
    DB_PORT = os.getenv("POSTGRES_PORT", "5432")
    DB_NAME = os.getenv("POSTGRES_DB", "postgres")
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
# Ініціалізація OpenAI клієнта
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
# -----------------------------------------------------------------------------
# FSM (Машина станів) для керування діалогами
# -----------------------------------------------------------------------------
class AdminFlow(StatesGroup):
    choose_role = State()
    waiting_org_name = State()
    waiting_admin_pwd_existing = State()
    waiting_admin_pwd_new = State()
    main_menu = State()
    materials_menu = State()
    awaiting_material_upload = State()
    tests_menu = State()
    awaiting_test_upload = State()
    ai_test_menu = State()
    awaiting_file_deletion = State()
    # НОВИЙ СТАН для керування згенерованим тестом
    awaiting_ai_test_action = State()
# -----------------------------------------------------------------------------
# Клавіатури для користувацького інтерфейсу
# -----------------------------------------------------------------------------
def kb_roles() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=" 👑  Я Адміністратор"), KeyboardButton(text=" 🎓  Я Користувач")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
def kb_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=" 📚  Навчальні матеріали")],
            [KeyboardButton(text=" 🧪  Тести")],
            [KeyboardButton(text=" 🚪  Вийти")],
        ],
        resize_keyboard=True,
    )
def kb_materials_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=" 📤  Завантажити матеріал"), KeyboardButton(text=" 👀  Переглянути матеріали")],
            [KeyboardButton(text=" 🗑  Видалити матеріал"), KeyboardButton(text=" 🏠  Головне меню")],
        ],
        resize_keyboard=True,
    )
def kb_tests_menu() -> ReplyKeyboardMarkup:
    """Оновлена клавіатура тестів з кращим відображенням кнопок"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=" 📥  Завантажити тест"), KeyboardButton(text=" 👁  Переглянути тести")],
            [KeyboardButton(text=" 🗑  Видалити тест"), KeyboardButton(text=" 🤖  Згенерувати тест ШІ")],
            [KeyboardButton(text=" 🏠  Головне меню")], # На окремому рядку
        ],
        resize_keyboard=True,
    )
def kb_ai_test_menu() -> ReplyKeyboardMarkup:
    """Меню вибору кількості питань"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Згенерувати 10 питань"), KeyboardButton(text="Згенерувати 20 питань")],
            [KeyboardButton(text="Згенерувати 30 питань"), KeyboardButton(text="Згенерувати 40 питань")],
            [KeyboardButton(text=" 🏠  Головне меню")],
        ],
        resize_keyboard=True,
    )
def kb_ai_test_actions() -> ReplyKeyboardMarkup:
    """НОВА КЛАВІАТУРА: Дії з згенерованим тестом"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Пройти тест (Admin)")],
            [KeyboardButton(text="📤 Направити Користувачам")],
            [KeyboardButton(text="🔄 Оновити тест")],
            [KeyboardButton(text="↩️ Повернутися в Меню тестів")],
        ],
        resize_keyboard=True,
    )
def kb_delete_confirmation(file_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=" ✅   Так ,  видалити ", callback_data=f"delete_{file_id}"),
                InlineKeyboardButton(text=" ❌   Скасувати ", callback_data="cancel_delete"),
            ]
        ]
    )
# -----------------------------------------------------------------------------
# Функції для роботи з базою даних
# -----------------------------------------------------------------------------
async def setup_database(pool: asyncpg.Pool):
    """Створює необхідні таблиці, якщо вони не існують."""
    async with pool.acquire() as con:
        await con.execute("""
            CREATE TABLE IF NOT EXISTS orgs (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                admin_password_hash TEXT NOT NULL
            );
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id SERIAL PRIMARY KEY,
                org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
                file_type TEXT NOT NULL CHECK (file_type IN ('material', 'test')),
                file_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
async def get_org(con: asyncpg.Connection, org_name: str) -> asyncpg.Record | None:
    return await con.fetchrow("SELECT * FROM orgs WHERE name = $1", org_name)
async def create_org(con: asyncpg.Connection, org_name: str, password: str) -> asyncpg.Record:
    return await con.fetchrow(
        "INSERT INTO orgs (name, admin_password_hash) VALUES ($1, $2) RETURNING *",
        org_name,
        password,
    )
async def check_password(org: asyncpg.Record, password: str) -> bool:
    return org["admin_password_hash"] == password
async def save_file_to_db(pool: asyncpg.Pool, org_id: int, file_type: str, file_id: str, filename: str):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO files (org_id, file_type, file_id, filename) VALUES ($1, $2, $3, $4)",
            org_id,
            file_type,
            file_id,
            filename,
        )
async def get_files_by_type(pool: asyncpg.Pool, org_id: int, file_type: str):
    async with pool.acquire() as con:
        return await con.fetch(
            "SELECT * FROM files WHERE org_id = $1 AND file_type = $2 ORDER BY uploaded_at DESC",
            org_id,
            file_type,
        )
async def count_files_by_type(pool: asyncpg.Pool, org_id: int, file_type: str) -> int:
    async with pool.acquire() as con:
        result = await con.fetchval(
            "SELECT COUNT(*) FROM files WHERE org_id = $1 AND file_type = $2",
            org_id,
            file_type,
        )
        return result or 0
async def delete_file_by_id(pool: asyncpg.Pool, file_id: int):
    async with pool.acquire() as con:
        await con.execute("DELETE FROM files WHERE id = $1", file_id)
async def get_file_by_id(pool: asyncpg.Pool, file_id: int):
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM files WHERE id = $1", file_id)
# -----------------------------------------------------------------------------
# Функції для роботи з OpenAI
# -----------------------------------------------------------------------------
async def download_file_content(bot: Bot, file_id: str) -> str:
    """Завантажує файл з Telegram і повертає його текстовий вміст"""
    try:
        file = await bot.get_file(file_id)
        file_path = file.file_path

        # Завантажуємо файл
        file_bytes = await bot.download_file(file_path)

        # Спробуємо прочитати як текст
        try:
            content = file_bytes.read().decode('utf-8')
        except:
            # Якщо не вдалося декодувати як UTF-8, спробуємо інші кодування
            file_bytes.seek(0)
            try:
                content = file_bytes.read().decode('cp1251')
            except:
                file_bytes.seek(0)
                content = file_bytes.read().decode('latin-1')

        return content
    except Exception as e:
        print(f"Помилка при завантаженні файлу: {e}")
        return ""
async def generate_test_questions(materials_content: str, num_questions: int) -> str:
    """Генерує тестові питання на основі матеріалів через OpenAI API"""
    if not openai_client:
        return " ❌  OpenAI API  не   налаштовано .  Додайте  OPENAI_API_KEY  у   змінні   середовища ."

    try:
        prompt = f"""На основі наступних навчальних матеріалів створи {num_questions} тестових питань з 4 варіантами відповідей (A, B, C, D).
Для кожного питання вкажи правильну відповідь.
Формат відповіді:
1. [Питання]
A) [варіант]
B) [варіант]
C) [варіант]
D) [варіант]
Правильна відповідь: [буква]
Навчальні матеріали:
{materials_content[:8000]}
Створи {num_questions} питань українською мовою:"""
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ти - експерт з створення тестових питань для навчання. Створюй якісні питання на основі наданих матеріалів."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=3000,
        )

        return response.choices[0].message.content
    except Exception as e:
        return f" ❌   Помилка   при   генерації   тесту : {str(e)}"
# -----------------------------------------------------------------------------
# Обробники повідомлень (хендлери)
# -----------------------------------------------------------------------------
router = Router()
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "Привіт! Будь ласка, оберіть свою роль:",
        reply_markup=kb_roles(),
    )
    await state.set_state(AdminFlow.choose_role)
@router.message(StateFilter(AdminFlow.choose_role), F.text == " 👑  Я Адміністратор")
async def choose_admin(msg: Message, state: FSMContext):
    await msg.answer("Введіть назву вашої організації:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AdminFlow.waiting_org_name)
@router.message(StateFilter(AdminFlow.choose_role), F.text == " 🎓  Я Користувач")
async def choose_user(msg: Message, state: FSMContext):
    await msg.answer("Цей режим поки що в розробці. Будь ласка, оберіть роль адміністратора.")
@router.message(StateFilter(AdminFlow.waiting_org_name))
async def got_org_name(msg: Message, state: FSMContext, pool: asyncpg.Pool):
    org_name = msg.text.strip()
    async with pool.acquire() as con:
        org = await get_org(con, org_name)
    if org:
        await state.update_data(org_id=org["id"], org_name=org_name)
        await msg.answer("Організацію знайдено. Введіть пароль адміністратора:")
        await state.set_state(AdminFlow.waiting_admin_pwd_existing)
    else:
        await state.update_data(org_name=org_name)
        await msg.answer("Це нова організація. Придумайте пароль адміністратора (мін. 4 символи):")
        await state.set_state(AdminFlow.waiting_admin_pwd_new)
@router.message(StateFilter(AdminFlow.waiting_admin_pwd_new))
async def got_new_password(msg: Message, state: FSMContext, pool: asyncpg.Pool):
    password = msg.text.strip()
    if len(password) < 4:
        await msg.answer("Пароль занадто короткий. Спробуйте ще раз (мін. 4 символи):")
        return
    data = await state.get_data()
    org_name = data["org_name"]
    async with pool.acquire() as con:
        org = await create_org(con, org_name, password)
    await state.update_data(org_id=org["id"])
    await msg.answer(f" ✅   Організацію  '{org_name}'  створено !  Вхід   виконано .", reply_markup=kb_main_menu())
    await state.set_state(AdminFlow.main_menu)
@router.message(StateFilter(AdminFlow.waiting_admin_pwd_existing))
async def got_existing_password(msg: Message, state: FSMContext, pool: asyncpg.Pool):
    password = msg.text.strip()
    data = await state.get_data()
    org_name = data["org_name"]
    async with pool.acquire() as con:
        org = await get_org(con, org_name)
    if org and await check_password(org, password):
        await msg.answer(f" ✅   Вхід   виконано !  Вітаємо   в   організації  '{org_name}'.", reply_markup=kb_main_menu())
        await state.set_state(AdminFlow.main_menu)
    else:
        await msg.answer(" ❌   Неправильний   пароль .  Спробуйте   ще   раз   або   почніть   з   початку  /start.")
# --- Головне меню ---
@router.message(StateFilter(AdminFlow.main_menu), F.text == " 📚  Навчальні матеріали")
async def show_materials_menu(msg: Message, state: FSMContext):
    await msg.answer("Меню навчальних матеріалів:", reply_markup=kb_materials_menu())
    await state.set_state(AdminFlow.materials_menu)
@router.message(StateFilter(AdminFlow.main_menu), F.text == " 🧪  Тести")
async def show_tests_menu(msg: Message, state: FSMContext):
    await msg.answer("Меню тестів:", reply_markup=kb_tests_menu())
    await state.set_state(AdminFlow.tests_menu)
@router.message(StateFilter(AdminFlow.main_menu), F.text == " 🚪  Вийти")
async def exit_admin_mode(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Ви вийшли з режиму адміністратора. Щоб почати знову, введіть /start", reply_markup=ReplyKeyboardRemove())
    await msg.answer("Оберіть свою роль:", reply_markup=kb_roles())
    await state.set_state(AdminFlow.choose_role)
# --- Меню матеріалів ---
@router.message(StateFilter(AdminFlow.materials_menu), F.text == " 📤  Завантажити матеріал")
async def request_material_upload(msg: Message, state: FSMContext, pool: asyncpg.Pool):
    data = await state.get_data()
    org_id = data.get("org_id")

    # Перевіряємо, чи вже є завантажений матеріал
    count = await count_files_by_type(pool, org_id, "material")
    if count >= 1:
        await msg.answer(" ⚠️ У вас вже завантажений матеріал. Спочатку видаліть існуючий, щоб завантажити новий.")
        return

    await msg.answer("Будь ласка, надішліть файл (документ, PDF, тощо) як вкладення.")
    await state.set_state(AdminFlow.awaiting_material_upload)
@router.message(StateFilter(AdminFlow.materials_menu), F.text == " 👀  Переглянути матеріали")
async def view_materials(msg: Message, state: FSMContext, pool: asyncpg.Pool, bot: Bot):
    data = await state.get_data()
    org_id = data.get("org_id")

    files = await get_files_by_type(pool, org_id, "material")

    if not files:
        await msg.answer(" 📭  Матеріали відсутні.")
        return

    await msg.answer(f" 📚  Знайдено матеріалів: {len(files)}")

    for file in files:
        try:
            await bot.send_document(
                chat_id=msg.chat.id,
                document=file["file_id"],
                caption=f" 📄  {file['filename']}\n 📅  Завантажено: {file['uploaded_at'].strftime('%d.%m.%Y %H:%M')}"
            )
        except Exception as e:
            await msg.answer(f" ❌   Помилка   при   відправці  файлу '{file['filename']}': {e}")
@router.message(StateFilter(AdminFlow.materials_menu), F.text == " 🗑  Видалити матеріал")
async def delete_material_request(msg: Message, state: FSMContext, pool: asyncpg.Pool):
    data = await state.get_data()
    org_id = data.get("org_id")

    files = await get_files_by_type(pool, org_id, "material")

    if not files:
        await msg.answer(" 📭  Матеріали відсутні.")
        return

    for file in files:
        await msg.answer(
            f" 📄  {file['filename']}\n 📅  Завантажено: {file['uploaded_at'].strftime('%d.%m.%Y %H:%M')}\n\nВидалити цей файл?",
            reply_markup=kb_delete_confirmation(file["id"])
        )
@router.message(StateFilter(AdminFlow.materials_menu), F.text == " 🏠  Головне меню")
async def back_to_main_1(msg: Message, state: FSMContext):
    await msg.answer("Головне меню:", reply_markup=kb_main_menu())
    await state.set_state(AdminFlow.main_menu)
# --- Меню тестів ---
@router.message(StateFilter(AdminFlow.tests_menu), F.text == " 📥  Завантажити тест")
async def request_test_upload(msg: Message, state: FSMContext, pool: asyncpg.Pool):
    data = await state.get_data()
    org_id = data.get("org_id")

    # Перевіряємо, чи вже є завантажений тест
    count = await count_files_by_type(pool, org_id, "test")
    if count >= 1:
        await msg.answer(" ⚠️ У вас вже завантажений тест. Спочатку видаліть існуючий, щоб завантажити новий.")
        return

    await msg.answer("Будь ласка, надішліть файл (документ, PDF, тощо) як вкладення.")
    await state.set_state(AdminFlow.awaiting_test_upload)
@router.message(StateFilter(AdminFlow.tests_menu), F.text == " 👁  Переглянути тести")
async def view_tests(msg: Message, state: FSMContext, pool: asyncpg.Pool, bot: Bot):
    data = await state.get_data()
    org_id = data.get("org_id")

    files = await get_files_by_type(pool, org_id, "test")

    if not files:
        await msg.answer(" 📭  Тести відсутні.")
        return

    await msg.answer(f" 🧪  Знайдено тестів: {len(files)}")

    for file in files:
        try:
            await bot.send_document(
                chat_id=msg.chat.id,
                document=file["file_id"],
                caption=f" 📄  {file['filename']}\n 📅  Завантажено: {file['uploaded_at'].strftime('%d.%m.%Y %H:%M')}"
            )
        except Exception as e:
            await msg.answer(f" ❌   Помилка   при   відправці   файлу  '{file['filename']}': {e}")
@router.message(StateFilter(AdminFlow.tests_menu), F.text == " 🗑  Видалити тест")
async def delete_test_request(msg: Message, state: FSMContext, pool: asyncpg.Pool):
    data = await state.get_data()
    org_id = data.get("org_id")

    files = await get_files_by_type(pool, org_id, "test")

    if not files:
        await msg.answer(" 📭  Тести відсутні.")
        return

    for file in files:
        await msg.answer(
            f" 📄  {file['filename']}\n 📅  Завантажено: {file['uploaded_at'].strftime('%d.%m.%Y %H:%M')}\n\nВидалити цей файл?",
            reply_markup=kb_delete_confirmation(file["id"])
        )
@router.message(StateFilter(AdminFlow.tests_menu), F.text == " 🤖  Згенерувати тест ШІ")
async def show_ai_test_menu(msg: Message, state: FSMContext):
    if not openai_client:
        await msg.answer(" ❌  OpenAI API  не   налаштовано .  Зверніться   до   адміністратора   системи .")
        return

    await msg.answer(" 🤖  Оберіть кількість питань для генерації:", reply_markup=kb_ai_test_menu())
    await state.set_state(AdminFlow.ai_test_menu)
@router.message(StateFilter(AdminFlow.tests_menu), F.text == " 🏠  Головне меню")
async def back_to_main_2(msg: Message, state: FSMContext):
    await msg.answer("Головне меню:", reply_markup=kb_main_menu())
    await state.set_state(AdminFlow.main_menu)
# --- Меню генерації тестів ШІ ---
@router.message(StateFilter(AdminFlow.ai_test_menu), F.text.startswith("Згенерувати"))
async def generate_ai_test(msg: Message, state: FSMContext, pool: asyncpg.Pool, bot: Bot):
    # Витягуємо кількість питань з тексту
    text = msg.text
    if "10" in text:
        num_questions = 10
    elif "20" in text:
        num_questions = 20
    elif "30" in text:
        num_questions = 30
    elif "40" in text:
        num_questions = 40
    else:
        await msg.answer(" ❌   Невідома   кількість   питань .")
        return

    data = await state.get_data()
    org_id = data.get("org_id")

    # Отримуємо матеріали
    materials = await get_files_by_type(pool, org_id, "material")

    if not materials:
        await msg.answer(" ❌   Спочатку   завантажте   навчальні   матеріали !")
        return

    await msg.answer(f" ⏳   Генерую  {num_questions} питань на основі ваших матеріалів... Це може зайняти до 30 секунд.")

    # Завантажуємо вміст матеріалів
    materials_content = ""
    for material in materials:
        content = await download_file_content(bot, material["file_id"])
        materials_content += content + "\n\n"

    if not materials_content.strip():
        await msg.answer(" ❌   Не   вдалося   прочитати   вміст   матеріалів .  Переконайтеся ,  що   файли   містять   текст .")
        return

    # Генеруємо тест
    test_content = await generate_test_questions(materials_content, num_questions)

    # Зберігаємо тест у файл і відправляємо
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
            f.write(test_content)
            temp_path = f.name

        await bot.send_document(
            chat_id=msg.chat.id,
            document=FSInputFile(temp_path, filename=f"Згенерований_тест_{num_questions}_питань.txt"),
            caption=f" ✅  Тест з {num_questions} питань успішно згенеровано!"
        )

        # Видаляємо тимчасовий файл
        os.unlink(temp_path)
        
        # НОВИЙ ФЛОУ: Перехід у меню дій з тестом
        await state.update_data(generated_test_content=test_content, num_questions=num_questions)
        await msg.answer(
            "✅ Тест успішно згенеровано! Оберіть наступну дію:",
            reply_markup=kb_ai_test_actions()
        )
        await state.set_state(AdminFlow.awaiting_ai_test_action)

    except Exception as e:
        await msg.answer(f" ❌   Помилка   при   збереженні   тесту : {e}")

@router.message(StateFilter(AdminFlow.ai_test_menu), F.text == " 🏠  Головне меню")
async def back_to_main_from_ai(msg: Message, state: FSMContext):
    await msg.answer("Головне меню:", reply_markup=kb_main_menu())
    await state.set_state(AdminFlow.main_menu)

# --- НОВІ ХЕНДЛЕРИ ДЛЯ ДІЙ З ЗГЕНЕРОВАНИМ ТЕСТОМ ---

@router.message(StateFilter(AdminFlow.awaiting_ai_test_action), F.text == "↩️ Повернутися в Меню тестів")
async def back_from_ai_actions(msg: Message, state: FSMContext):
    """Повертає до загального меню тестів і очищає дані згенерованого тесту."""
    await state.set_data(
        {k: v for k, v in (await state.get_data()).items() if k not in ['generated_test_content', 'num_questions']}
    ) # Очищаємо дані згенерованого тесту
    await msg.answer("Повертаюсь до меню тестів:", reply_markup=kb_tests_menu())
    await state.set_state(AdminFlow.tests_menu)

@router.message(StateFilter(AdminFlow.awaiting_ai_test_action), F.text == "🔄 Оновити тест")
async def regenerate_ai_test_request(msg: Message, state: FSMContext):
    """Повертає до меню вибору кількості питань для повторної генерації."""
    await msg.answer("🤖 Оберіть кількість питань для повторної генерації:", reply_markup=kb_ai_test_menu())
    await state.set_state(AdminFlow.ai_test_menu) # Повертаємось у стан генерації

@router.message(StateFilter(AdminFlow.awaiting_ai_test_action), F.text == "📤 Направити Користувачам")
async def send_test_to_users(msg: Message):
    """Placeholder: Тут має бути логіка збереження тесту в БД і відправки користувачам."""
    await msg.answer(" 🏗️ Функціонал відправки користувачам знаходиться в розробці. Спершу потрібно створити логіку проходження тестів.")

@router.message(StateFilter(AdminFlow.awaiting_ai_test_action), F.text == "▶️ Пройти тест (Admin)")
async def start_admin_test_preview(msg: Message):
    """Placeholder: Тут має бути логіка запуску тесту для адміністратора."""
    await msg.answer(" 🚧 Функціонал проходження тесту знаходиться в розробці. Щоб його реалізувати, потрібно створити логіку перетворення текстового файлу тесту на запитання бота.")

# --- Обробка завантаження файлів ---
async def handle_document_upload(msg: Message, state: FSMContext, pool: asyncpg.Pool, file_type: str):
    if not msg.document:
        await msg.answer("Будь ласка, надішліть саме документ (файл).")
        return
    data = await state.get_data()
    org_id = data.get("org_id")
    if not org_id:
        await msg.answer("Помилка: не вдалося визначити вашу організацію. Спробуйте /start.")
        return
    doc = msg.document
    try:
        await save_file_to_db(pool, org_id, file_type, doc.file_id, doc.file_name)
        await msg.answer(f" ✅   Файл  '{doc.file_name}'  успішно   збережено .")
    except Exception as e:
        await msg.answer(f" ❌   Сталася   помилка   при   збереженні   файлу : {e}")
    # Повернення до відповідного меню
    if file_type == "material":
        await state.set_state(AdminFlow.materials_menu)
        await msg.answer("Меню навчальних матеріалів:", reply_markup=kb_materials_menu())
    else:
        await state.set_state(AdminFlow.tests_menu)
        await msg.answer("Меню тестів:", reply_markup=kb_tests_menu())
@router.message(StateFilter(AdminFlow.awaiting_material_upload), F.document)
async def got_material_upload(msg: Message, state: FSMContext, pool: asyncpg.Pool):
    await handle_document_upload(msg, state, pool, "material")
@router.message(StateFilter(AdminFlow.awaiting_test_upload), F.document)
async def got_test_upload(msg: Message, state: FSMContext, pool: asyncpg.Pool):
    await handle_document_upload(msg, state, pool, "test")
@router.message(StateFilter(AdminFlow.awaiting_material_upload, AdminFlow.awaiting_test_upload))
async def incorrect_upload(msg: Message):
    await msg.answer("Очікується файл. Будь ласка, надішліть документ.")
# --- Обробка callback-запитів (для видалення файлів) ---
@router.callback_query(F.data.startswith("delete_"))
async def confirm_delete(callback: CallbackQuery, pool: asyncpg.Pool):
    file_id = int(callback.data.split("_")[1])

    try:
        file = await get_file_by_id(pool, file_id)
        if file:
            await delete_file_by_id(pool, file_id)
            await callback.message.edit_text(f" ✅   Файл  '{file['filename']}'  успішно   видалено !")
        else:
            await callback.message.edit_text(" ❌   Файл   не   знайдено .")
    except Exception as e:
        await callback.message.edit_text(f" ❌   Помилка   при   видаленні : {e}")

    await callback.answer()
@router.callback_query(F.data == "cancel_delete")
async def cancel_delete(callback: CallbackQuery):
    await callback.message.edit_text(" ❌   Видалення   скасовано .")
    await callback.answer()
# -----------------------------------------------------------------------------
# HTTP сервер для Render
# -----------------------------------------------------------------------------
async def health_check(request):
    return web.Response(text="Bot is running!")
async def start_http_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"HTTP сервер запущено на порту {PORT}")
# -----------------------------------------------------------------------------
# Головна функція запуску бота
# -----------------------------------------------------------------------------
async def main():
    if not BOT_TOKEN:
        print("Помилка: не знайдено токен бота. Задайте змінну середовища TELEGRAM_BOT_TOKEN.")
        return
    if not DATABASE_URL:
        print("Помилка: не знайдено адресу бази даних. Задайте змінну середовища DATABASE_URL.")
        return
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
    except Exception as e:
        print(f"Не вдалося підключитися до бази даних: {e}")
        return

    # <<< ЗМІНИ ТУТ >>>: Викликаємо створення таблиць після успішного підключення
    # Це гарантує, що таблиці будуть створені/перевірені при кожному запуску
    try:
        await setup_database(pool)
        print("✅ Перевірка та створення таблиць завершено.")
    except Exception as e:
        print(f"❌ Помилка при створенні таблиць: {e}")
        return
    # <<< КІНЕЦЬ ЗМІН >>>

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage(), pool=pool)
    dp.include_router(router)
    await start_http_server()
    print("Бот запускається...")
    await dp.start_polling(bot)
    await pool.close()
if __name__ == "__main__":
    asyncio.run(main())
