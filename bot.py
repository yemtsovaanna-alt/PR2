import asyncio
import logging
import os
import requests
from typing import Any, Awaitable, Callable, Optional
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject, Update
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Middleware для логирования всех сообщений
class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        if isinstance(event, Message):
            user = event.from_user
            logger.info(f"User {user.id} (@{user.username}): {event.text}")
        return await handler(event, data)


# Токены из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "YOUR_WEATHER_API_KEY")

# URL вашего сервиса на render.com (установите в переменных окружения)
# Например: https://your-bot-name.onrender.com
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

# Порт для веб-сервера (render.com предоставляет через переменную PORT)
PORT = int(os.getenv("PORT", 8000))

# Хранилище данных пользователей
users = {}

router = Router()


# Состояния для настройки профиля
class ProfileSetup(StatesGroup):
    weight = State()
    height = State()
    age = State()
    activity = State()
    city = State()


# Состояния для логирования еды
class FoodLogging(StatesGroup):
    waiting_for_grams = State()


# Калории за минуту для разных типов тренировок
WORKOUT_CALORIES = {
    "бег": 10,
    "ходьба": 5,
    "плавание": 8,
    "велосипед": 7,
    "силовая": 6,
    "йога": 3,
    "кардио": 8,
}


def get_food_info(product_name: str) -> Optional[dict]:
    """Получить информацию о продукте через OpenFoodFacts API"""
    try:
        url = f"https://world.openfoodfacts.org/cgi/search.pl?action=process&search_terms={product_name}&json=true"
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            products = data.get("products", [])
            if products:
                first_product = products[0]
                return {
                    "name": first_product.get("product_name", product_name),
                    "calories": first_product.get("nutriments", {}).get("energy-kcal_100g", 0),
                }
    except Exception as e:
        logger.error(f"Ошибка получения информации о продукте: {e}")
    return None


def get_weather(city: str) -> Optional[float]:
    """Получить текущую температуру в городе через OpenWeatherMap API"""
    try:
        url = f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_API_KEY}&units=metric"
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            return data["main"]["temp"]
    except Exception as e:
        logger.error(f"Ошибка получения погоды: {e}")
    return None


def calculate_water_goal(weight: float, activity_minutes: int, temperature: Optional[float]) -> int:
    """
    Рассчитать дневную норму воды (мл)
    Базовая норма = Вес × 30 мл/кг
    + 500 мл за каждые 30 минут активности
    + 500-1000 мл за жаркую погоду (> 25°C)
    """
    base = weight * 30
    activity_bonus = (activity_minutes // 30) * 500

    weather_bonus = 0
    if temperature is not None:
        if temperature > 30:
            weather_bonus = 1000
        elif temperature > 25:
            weather_bonus = 500

    return int(base + activity_bonus + weather_bonus)


def calculate_calorie_goal(weight: float, height: float, age: int, activity_minutes: int) -> int:
    """
    Рассчитать дневную норму калорий
    Базовая формула (Mifflin-St Jeor): 10 × Вес + 6.25 × Рост - 5 × Возраст
    + Бонус за активность (примерно 5-7 ккал за минуту тренировки)
    """
    base = 10 * weight + 6.25 * height - 5 * age
    activity_bonus = activity_minutes * 5  # ~5 ккал/мин средней активности
    return int(base + activity_bonus)


@router.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    await message.answer(
        "Привет! Я бот для отслеживания питания, воды и активности.\n\n"
        "Доступные команды:\n"
        "/set_profile - Настроить профиль\n"
        "/log_water <мл> - Записать выпитую воду\n"
        "/log_food <продукт> - Записать еду\n"
        "/log_workout <тип> <минуты> - Записать тренировку\n"
        "/check_progress - Показать прогресс"
    )


@router.message(Command("set_profile"))
async def cmd_set_profile(message: Message, state: FSMContext):
    """Начать настройку профиля"""
    await state.set_state(ProfileSetup.weight)
    await message.answer("Введите ваш вес (в кг):")


@router.message(ProfileSetup.weight)
async def process_weight(message: Message, state: FSMContext):
    """Обработка ввода веса"""
    try:
        weight = float(message.text.replace(",", "."))
        if weight <= 0 or weight > 500:
            raise ValueError("Некорректный вес")
        await state.update_data(weight=weight)
        await state.set_state(ProfileSetup.height)
        await message.answer("Введите ваш рост (в см):")
    except ValueError:
        await message.answer("Пожалуйста, введите корректный вес (число в кг):")


@router.message(ProfileSetup.height)
async def process_height(message: Message, state: FSMContext):
    """Обработка ввода роста"""
    try:
        height = float(message.text.replace(",", "."))
        if height <= 0 or height > 300:
            raise ValueError("Некорректный рост")
        await state.update_data(height=height)
        await state.set_state(ProfileSetup.age)
        await message.answer("Введите ваш возраст:")
    except ValueError:
        await message.answer("Пожалуйста, введите корректный рост (число в см):")


@router.message(ProfileSetup.age)
async def process_age(message: Message, state: FSMContext):
    """Обработка ввода возраста"""
    try:
        age = int(message.text)
        if age <= 0 or age > 150:
            raise ValueError("Некорректный возраст")
        await state.update_data(age=age)
        await state.set_state(ProfileSetup.activity)
        await message.answer("Сколько минут активности у вас в день?")
    except ValueError:
        await message.answer("Пожалуйста, введите корректный возраст (целое число):")


@router.message(ProfileSetup.activity)
async def process_activity(message: Message, state: FSMContext):
    """Обработка ввода активности"""
    try:
        activity = int(message.text)
        if activity < 0 or activity > 1440:
            raise ValueError("Некорректное время активности")
        await state.update_data(activity=activity)
        await state.set_state(ProfileSetup.city)
        await message.answer("В каком городе вы находитесь?")
    except ValueError:
        await message.answer("Пожалуйста, введите корректное количество минут:")


@router.message(ProfileSetup.city)
async def process_city(message: Message, state: FSMContext):
    """Обработка ввода города и завершение настройки профиля"""
    city = message.text.strip()
    data = await state.get_data()

    # Получаем температуру в городе
    temperature = get_weather(city)

    # Рассчитываем нормы
    water_goal = calculate_water_goal(data["weight"], data["activity"], temperature)
    calorie_goal = calculate_calorie_goal(data["weight"], data["height"], data["age"], data["activity"])

    # Сохраняем профиль пользователя
    user_id = message.from_user.id
    users[user_id] = {
        "weight": data["weight"],
        "height": data["height"],
        "age": data["age"],
        "activity": data["activity"],
        "city": city,
        "water_goal": water_goal,
        "calorie_goal": calorie_goal,
        "logged_water": 0,
        "logged_calories": 0,
        "burned_calories": 0,
    }

    await state.clear()

    # Формируем ответ
    weather_info = f"Температура в {city}: {temperature:.1f}°C" if temperature else f"Не удалось получить погоду для {city}"

    await message.answer(
        f"Профиль сохранён!\n\n"
        f"Вес: {data['weight']} кг\n"
        f"Рост: {data['height']} см\n"
        f"Возраст: {data['age']} лет\n"
        f"Активность: {data['activity']} мин/день\n"
        f"Город: {city}\n\n"
        f"{weather_info}\n\n"
        f"Ваши дневные нормы:\n"
        f"Вода: {water_goal} мл\n"
        f"Калории: {calorie_goal} ккал"
    )


@router.message(Command("log_water"))
async def cmd_log_water(message: Message):
    """Логирование выпитой воды"""
    user_id = message.from_user.id

    if user_id not in users:
        await message.answer("Сначала настройте профиль командой /set_profile")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /log_water <количество мл>\nПример: /log_water 250")
        return

    try:
        amount = int(args[1])
        if amount <= 0:
            raise ValueError("Количество должно быть положительным")

        users[user_id]["logged_water"] += amount
        logged = users[user_id]["logged_water"]
        goal = users[user_id]["water_goal"]
        remaining = max(0, goal - logged)

        await message.answer(
            f"Записано: {amount} мл воды.\n\n"
            f"Выпито за день: {logged} мл из {goal} мл.\n"
            f"Осталось: {remaining} мл."
        )
    except ValueError:
        await message.answer("Пожалуйста, введите корректное количество воды в мл.")


@router.message(Command("log_food"))
async def cmd_log_food(message: Message, state: FSMContext):
    """Логирование еды"""
    user_id = message.from_user.id

    if user_id not in users:
        await message.answer("Сначала настройте профиль командой /set_profile")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /log_food <название продукта>\nПример: /log_food банан")
        return

    product_name = args[1]
    food_info = get_food_info(product_name)

    if food_info is None or food_info["calories"] == 0:
        await message.answer(
            f"Не удалось найти информацию о продукте '{product_name}'.\n"
            "Попробуйте ввести название на английском или другой продукт."
        )
        return

    await state.update_data(
        food_name=food_info["name"],
        food_calories=food_info["calories"]
    )
    await state.set_state(FoodLogging.waiting_for_grams)

    await message.answer(
        f"{food_info['name']} — {food_info['calories']} ккал на 100 г.\n"
        "Сколько грамм вы съели?"
    )


@router.message(FoodLogging.waiting_for_grams)
async def process_food_grams(message: Message, state: FSMContext):
    """Обработка ввода граммов еды"""
    user_id = message.from_user.id

    try:
        grams = float(message.text.replace(",", "."))
        if grams <= 0:
            raise ValueError("Количество должно быть положительным")

        data = await state.get_data()
        calories = (data["food_calories"] / 100) * grams

        users[user_id]["logged_calories"] += calories

        await state.clear()

        await message.answer(
            f"Записано: {calories:.1f} ккал ({grams:.0f} г {data['food_name']})."
        )
    except ValueError:
        await message.answer("Пожалуйста, введите корректное количество в граммах:")


@router.message(Command("log_workout"))
async def cmd_log_workout(message: Message):
    """Логирование тренировки"""
    user_id = message.from_user.id

    if user_id not in users:
        await message.answer("Сначала настройте профиль командой /set_profile")
        return

    args = message.text.split()
    if len(args) < 3:
        workout_types = ", ".join(WORKOUT_CALORIES.keys())
        await message.answer(
            f"Использование: /log_workout <тип> <минуты>\n"
            f"Пример: /log_workout бег 30\n\n"
            f"Доступные типы тренировок: {workout_types}"
        )
        return

    workout_type = args[1].lower()
    try:
        minutes = int(args[2])
        if minutes <= 0:
            raise ValueError("Время должно быть положительным")
    except ValueError:
        await message.answer("Пожалуйста, введите корректное время тренировки в минутах.")
        return

    # Получаем калории за минуту (по умолчанию 5, если тип не найден)
    calories_per_min = WORKOUT_CALORIES.get(workout_type, 5)
    burned_calories = calories_per_min * minutes

    # Дополнительная вода: 200 мл за каждые 30 минут тренировки
    extra_water = (minutes // 30) * 200
    if minutes % 30 > 0:
        extra_water += 200  # Добавляем воду даже за неполные 30 минут

    users[user_id]["burned_calories"] += burned_calories
    users[user_id]["water_goal"] += extra_water

    workout_emoji = {
        "бег": "🏃",
        "ходьба": "🚶",
        "плавание": "🏊",
        "велосипед": "🚴",
        "силовая": "🏋️",
        "йога": "🧘",
        "кардио": "💪",
    }
    emoji = workout_emoji.get(workout_type, "🏋️")

    await message.answer(
        f"{emoji} {workout_type.capitalize()} {minutes} минут — {burned_calories} ккал сожжено.\n"
        f"Дополнительно: выпейте {extra_water} мл воды."
    )


@router.message(Command("check_progress"))
async def cmd_check_progress(message: Message):
    """Показать прогресс по воде и калориям"""
    user_id = message.from_user.id

    if user_id not in users:
        await message.answer("Сначала настройте профиль командой /set_profile")
        return

    user = users[user_id]

    water_logged = user["logged_water"]
    water_goal = user["water_goal"]
    water_remaining = max(0, water_goal - water_logged)

    calories_logged = user["logged_calories"]
    calorie_goal = user["calorie_goal"]
    burned = user["burned_calories"]
    calorie_balance = calories_logged - burned

    await message.answer(
        f"📊 Прогресс:\n\n"
        f"💧 Вода:\n"
        f"  • Выпито: {water_logged} мл из {water_goal} мл\n"
        f"  • Осталось: {water_remaining} мл\n\n"
        f"🍽 Калории:\n"
        f"  • Потреблено: {calories_logged:.0f} ккал из {calorie_goal} ккал\n"
        f"  • Сожжено: {burned} ккал\n"
        f"  • Баланс: {calorie_balance:.0f} ккал"
    )


async def on_startup(bot: Bot):
    """Действия при запуске бота - установка webhook"""
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook установлен: {WEBHOOK_URL}")


async def on_shutdown(bot: Bot):
    """Действия при остановке бота - удаление webhook"""
    await bot.delete_webhook()
    logger.info("Webhook удалён")


async def health_check(request):
    """Эндпоинт для проверки здоровья сервиса"""
    return web.Response(text="OK")


def main():
    """Запуск бота с webhook"""
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Добавляем middleware для логирования
    router.message.middleware(LoggingMiddleware())

    dp.include_router(router)

    # Регистрируем startup и shutdown хуки
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Создаём aiohttp приложение
    app = web.Application()

    # Добавляем health check эндпоинт
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    # Настраиваем webhook handler
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    # Настраиваем приложение
    setup_application(app, dp, bot=bot)

    logger.info(f"Запуск веб-сервера на порту {PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
