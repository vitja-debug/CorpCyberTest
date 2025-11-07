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
)
from aiohttp import web

# -----------------------------------------------------------------------------
# Конфігурація: читаємо токен і параметри підключення до БД
# -----------------------------------------------------------------------------

# На Render ці змінні середовища будуть доступні автоматично.
# Для локального тестування їх треба задати вручну або через .env файл.
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 10000))  # Render передає PORT через змінну середовища

if not DATABASE_URL:
    # Запасний варіант, якщо DATABASE_URL не задано,
    # збираємо його з окремих змінних PG*.
    # Це корисно для локального запуску через Docker Compose.
    DB_USER = os.getenv("POSTGRES_USER", "postgres")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
    DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
    DB_PORT = os.getenv("POSTGRES_PORT", "5432")
    DB_NAME = os.getenv("POSTGRES_DB", "postgres")
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# -----------------------------------------------------------------------------
# FSM (Машина станів) для керування діалогами
# -----------------------------------------------------------------------------
class AdminFlow(StatesGroup):
    choose_role = State()
    # Реєстрація/вхід адміна
    waiting_org_name = State()
    waiting_admin_pwd_existing = State()
    waiting_admin_pwd_new = State()
    # Головне меню
    main_menu = State()
    # Меню матеріалів
    materials_menu = State()
    awaiting_material_upload = State()
    # Меню тестів
    tests_menu = State()
    awaiting_test_upload = State()

# -----------------------------------------------------------------------------
# Клавіатури для користувацького інтерфейсу
# -----------------------------------------------------------------------------
def kb_roles() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="👑 Я Адміністратор"), KeyboardButton(text="🎓 Я Користувач")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def kb_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📚 Навчальні матеріали")],
            [KeyboardButton(text="🧪 Тести")],
            [KeyboardButton(text="🚪 Вийти")],
        ],
        resize_keyboard=True,
    )

def kb_materials_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📤 Завантажити матеріал")],
            [KeyboardButton(text="👀 Переглянути матеріали")],
            [KeyboardButton(text="🗑 Видалити матеріал")],
            [KeyboardButton(text="🏠 Головне меню")],
        ],
        resize_keyboard=True,
    )

def kb_tests_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📥 Завантажити тест")],
            [KeyboardButton(text="👁 Переглянути тести")],
            [KeyboardButton(text="🗑 Видалити тест")],
            [KeyboardButton(text="🏠 Головне меню")],
        ],
        resize_keyboard=True,
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
    # У реальному проєкті пароль треба хешувати!
    # Наприклад, за допомогою `passlib`. Зараз для простоти зберігаємо як є.
    return await con.fetchrow(
        "INSERT INTO orgs (name, admin_password_hash) VALUES ($1, $2) RETURNING *",
        org_name,
        password,
    )

async def check_password(org: asyncpg.Record, password: str) -> bool:
    # Тут має бути перевірка хешу пароля
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

@router.message(StateFilter(AdminFlow.choose_role), F.text == "👑 Я Адміністратор")
async def choose_admin(msg: Message, state: FSMContext):
    await msg.answer("Введіть назву вашої організації:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AdminFlow.waiting_org_name)

@router.message(StateFilter(AdminFlow.choose_role), F.text == "🎓 Я Користувач")
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
    await msg.answer(f"✅ Організацію '{org_name}' створено! Вхід виконано.", reply_markup=kb_main_menu())
    await state.set_state(AdminFlow.main_menu)

@router.message(StateFilter(AdminFlow.waiting_admin_pwd_existing))
async def got_existing_password(msg: Message, state: FSMContext, pool: asyncpg.Pool):
    password = msg.text.strip()
    data = await state.get_data()
    org_name = data["org_name"]

    async with pool.acquire() as con:
        org = await get_org(con, org_name)

    if org and await check_password(org, password):
        await msg.answer(f"✅ Вхід виконано! Вітаємо в організації '{org_name}'.", reply_markup=kb_main_menu())
        await state.set_state(AdminFlow.main_menu)
    else:
        await msg.answer("❌ Неправильний пароль. Спробуйте ще раз або почніть з початку /start.")

# --- Головне меню ---
@router.message(StateFilter(AdminFlow.main_menu), F.text == "📚 Навчальні матеріали")
async def show_materials_menu(msg: Message, state: FSMContext):
    await msg.answer("Меню навчальних матеріалів:", reply_markup=kb_materials_menu())
    await state.set_state(AdminFlow.materials_menu)

@router.message(StateFilter(AdminFlow.main_menu), F.text == "🧪 Тести")
async def show_tests_menu(msg: Message, state: FSMContext):
    await msg.answer("Меню тестів:", reply_markup=kb_tests_menu())
    await state.set_state(AdminFlow.tests_menu)

@router.message(StateFilter(AdminFlow.main_menu), F.text == "🚪 Вийти")
async def exit_admin_mode(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Ви вийшли з режиму адміністратора. Щоб почати знову, введіть /start", reply_markup=ReplyKeyboardRemove())
    await msg.answer("Оберіть свою роль:", reply_markup=kb_roles())
    await state.set_state(AdminFlow.choose_role)

# --- Меню матеріалів ---
@router.message(StateFilter(AdminFlow.materials_menu), F.text == "📤 Завантажити матеріал")
async def request_material_upload(msg: Message, state: FSMContext):
    await msg.answer("Будь ласка, надішліть файл (документ, PDF, тощо) як вкладення.")
    await state.set_state(AdminFlow.awaiting_material_upload)

@router.message(StateFilter(AdminFlow.materials_menu), F.text == "🏠 Головне меню")
async def back_to_main_1(msg: Message, state: FSMContext):
    await msg.answer("Головне меню:", reply_markup=kb_main_menu())
    await state.set_state(AdminFlow.main_menu)

# --- Меню тестів ---
@router.message(StateFilter(AdminFlow.tests_menu), F.text == "📥 Завантажити тест")
async def request_test_upload(msg: Message, state: FSMContext):
    await msg.answer("Будь ласка, надішліть файл (документ, PDF, тощо) як вкладення.")
    await state.set_state(AdminFlow.awaiting_test_upload)

@router.message(StateFilter(AdminFlow.tests_menu), F.text == "🏠 Головне меню")
async def back_to_main_2(msg: Message, state: FSMContext):
    await msg.answer("Головне меню:", reply_markup=kb_main_menu())
    await state.set_state(AdminFlow.main_menu)

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
        await msg.answer(f"✅ Файл '{doc.file_name}' успішно збережено.")
    except Exception as e:
        await msg.answer(f"❌ Сталася помилка при збереженні файлу: {e}")

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

# Обробники для інших типів контенту в станах очікування файлу
@router.message(StateFilter(AdminFlow.awaiting_material_upload, AdminFlow.awaiting_test_upload))
async def incorrect_upload(msg: Message):
    await msg.answer("Очікується файл. Будь ласка, надішліть документ.")

# -----------------------------------------------------------------------------
# HTTP сервер для Render (щоб сервіс не падав через відсутність відкритого порту)
# -----------------------------------------------------------------------------
async def health_check(request):
    """Простий health check endpoint для Render"""
    return web.Response(text="Bot is running!")

async def start_http_server():
    """Запускає простий HTTP сервер на порту, який очікує Render"""
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

    # Створюємо пул підключень до БД
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
    except Exception as e:
        print(f"Не вдалося підключитися до бази даних: {e}")
        return
    
    # Створюємо таблиці, якщо їх немає
    await setup_database(pool)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage(), pool=pool) # Передаємо пул у диспетчер
    dp.include_router(router)

    # Запускаємо HTTP сервер для Render
    await start_http_server()

    print("Бот запускається...")
    await dp.start_polling(bot)

    # Закриваємо пул при зупинці
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
