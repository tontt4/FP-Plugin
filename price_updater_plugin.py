from __future__ import annotations
import json
import time
import requests
import atexit
import threading
from threading import Thread, Lock
from typing import TYPE_CHECKING, Optional, Union
from datetime import datetime as dt
import os

from FunPayAPI.types import LotShortcut

if TYPE_CHECKING:
    from cardinal import Cardinal
from FunPayAPI.updater.events import *
from tg_bot import CBT
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B
import telebot
import logging
from locales.localizer import Localizer
import tg_bot.static_keyboards

localizer = Localizer()
_ = localizer.translate

NAME = "Steam Price Updater"
VERSION = "2.1.0"
DESCRIPTION = "Автоматическое обновление цен лотов на основе Steam API с выбором валют"
CREDITS = "@humblegodq"
UUID = "247153d9-f732-4f01-a11f-a3945b68b533"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.steam_price_updater")
LOGGER_PREFIX = "[STEAM PRICE UPDATER]"

class Config:
    CACHE_TTL = 21600  # 6 hours in seconds
    CURRENCY_UPDATE_INTERVAL = 21600  # 6 hours for currency updates
    CYCLE_PAUSE = 300
    LOT_PROCESSING_DELAY = 2
    LOTS_PER_PAGE = 8
    STEAM_REQUEST_DELAY = 10
    MAX_RETRIES = 3
    REQUEST_TIMEOUT = 15
    DEFAULT_STEAM_CURRENCY = "UAH"
    SUPPORTED_CURRENCIES = ["UAH", "KZT", "RUB", "USD", "EUR"]
    ACCOUNT_CURRENCIES = ["USD", "RUB", "EUR"]
    MAX_CACHE_SIZE = 1000

SETTINGS = {
    "currency": "USD",
    "account_currency": "USD",
    "time": 21600,  # 6 hours
    "first_markup": 3.0,
    "second_markup": 5.0,
    "fixed_markup": 0.5,
    "max_price": 5000.0,
    "min_price": 1.0,
    "round_to_integer": False,
    "steam_request_delay": Config.STEAM_REQUEST_DELAY,
    "request_timeout": Config.REQUEST_TIMEOUT
}

LOTS = {}
CARDINAL_INSTANCE = None
WIZARD_STATES = {}

class ThreadSafeCacheManager:
    def __init__(self, max_size: int = Config.MAX_CACHE_SIZE, ttl: int = Config.CACHE_TTL):
        self.cache = {}
        self.max_size = max_size
        self.ttl = ttl
        self._lock = Lock()
  
    def get(self, key: str):
        """Возвращает значение из кеша с проверкой TTL"""
        with self._lock:
            if key in self.cache:
                entry = self.cache[key]
                if time.time() - entry["timestamp"] < self.ttl:
                    return entry["value"]
                else:
                    try:
                        del self.cache[key]
                    except KeyError:
                        pass
            return None
  
    def set(self, key: str, value):
        """Устанавливает значение в кеш"""
        with self._lock:
            if len(self.cache) >= self.max_size:
                try:
                    oldest_key = min(self.cache.keys(), 
                                   key=lambda k: self.cache[k]["timestamp"])
                    del self.cache[oldest_key]
                except (ValueError, KeyError):
                    pass
          
            self.cache[key] = {
                "value": value,
                "timestamp": time.time()
            }
  
    def __contains__(self, key):
        return self.get(key) is not None
  
    def __getitem__(self, key):
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value
  
    def __setitem__(self, key, value):
        self.set(key, value)
  
    def __len__(self):
        with self._lock:
            return len(self.cache)
  
    def keys(self):
        with self._lock:
            return list(self.cache.keys())
  
    def __delitem__(self, key):
        with self._lock:
            if key in self.cache:
                del self.cache[key]
            else:
                raise KeyError(key)
  
    def clear_expired(self):
        """Очищает устаревшие записи"""
        with self._lock:
            current_time = time.time()
            expired_keys = [k for k, v in self.cache.items() 
                           if current_time - v["timestamp"] >= self.ttl]
            for key in expired_keys:
                try:
                    del self.cache[key]
                except KeyError:
                    pass

# Global cache instance
CACHE = ThreadSafeCacheManager(ttl=Config.CURRENCY_UPDATE_INTERVAL)

# Callback constants
CBT_CHANGE_CURRENCY = "SPU_change_curr"
CBT_TEXT_CHANGE_LOT = "SPU_ChangeLot"
CBT_TEXT_EDIT = "SPU_Edit"
CBT_TEXT_DELETE = "SPU_DELETE"
CBT_UPDATE_NOW = "SPU_UpdateNow"
CBT_STATS = "SPU_Stats"
CBT_SHOW_SETTINGS = "SPU_show_settings"
CBT_CHANGE_STEAM_CURRENCY = "SPU_change_steam_curr"
CBT_LOTS_MENU = "SPU_lots_menu"
CBT_EDIT_LOT = "SPU_edit_lot"
CBT_TOGGLE_LOT = "SPU_toggle_lot"
CBT_DELETE_LOT = "SPU_delete_lot"
CBT_REFRESH_RATES = "SPU_refresh_rates"

def get_currency_rate(currency: str = "USD") -> float:
    """
    Получает курс валют с кешированием на 6 часов
    """
    currency = currency.upper()
    cache_key = f"{currency}_rate"
    
    # Проверяем кеш (TTL = 6 часов)
    cached_rate = CACHE.get(cache_key)
    if cached_rate and isinstance(cached_rate, dict):
        cache_age = time.time() - cached_rate.get("timestamp", 0)
        if cache_age < Config.CURRENCY_UPDATE_INTERVAL:
            logger.debug(f"{LOGGER_PREFIX} Используем кеш для USD/{currency}: {cached_rate.get('rate')} (возраст: {int(cache_age/3600)} ч)")
            return cached_rate.get("rate", get_fallback_rate(currency))
    
    try:
        logger.debug(f"{LOGGER_PREFIX} Получаю курс USD/{currency} через API")
        url = "https://api.exchangerate-api.com/v4/latest/USD"
        response = requests.get(url, timeout=Config.REQUEST_TIMEOUT)
      
        if response.status_code == 200:
            data = response.json()
            rates = data.get("rates", {})
          
            if currency in rates:
                rate = float(rates[currency])
                CACHE.set(cache_key, {
                    "rate": rate,
                    "timestamp": time.time(),
                    "source": "exchangerate-api"
                })
                logger.info(f"{LOGGER_PREFIX} Получен курс USD/{currency}: {rate}")
                return rate
            else:
                logger.warning(f"{LOGGER_PREFIX} Валюта {currency} не найдена")
        
        # Fallback к резервным API
        return get_fallback_rate(currency)
      
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} Ошибка получения курса USD/{currency}: {e}")
        return get_fallback_rate(currency)

def get_fallback_rate(currency: str) -> float:
    """Возвращает последние известные курсы из кеша или резервные курсы"""
    cache_key = f"{currency}_rate"
    cached_rate = CACHE.get(cache_key)
  
    if cached_rate and isinstance(cached_rate, dict):
        rate = cached_rate.get("rate")
        if rate and rate > 0:
            cache_age = time.time() - cached_rate.get("timestamp", 0)
            logger.warning(f"{LOGGER_PREFIX} Используем кеш USD/{currency}: {rate} (возраст: {int(cache_age/3600)}ч)")
            return rate
  
    # Резервные курсы
    fallback_rates = {
        "UAH": 41.82,
        "RUB": 78.42,
        "KZT": 519.86, 
        "EUR": 0.85, 
        "USD": 1.0
    }
    rate = fallback_rates.get(currency, 1.0)
    logger.warning(f"{LOGGER_PREFIX} Используем резервный курс USD/{currency}: {rate}")
    return rate

def clear_currency_cache():
    """Очищает кеш курсов валют"""
    try:
        currencies = ["USD", "UAH", "RUB", "EUR", "KZT"]
        cleared_count = 0
      
        for currency in currencies:
            cache_key = f"{currency}_rate"
            if cache_key in CACHE.cache:
                del CACHE.cache[cache_key]
                cleared_count += 1
      
        logger.info(f"{LOGGER_PREFIX} Очищен кеш курсов валют: {cleared_count} записей")
        return cleared_count
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка очистки кеша валют: {e}")
        return 0

def validate_steam_id(steam_id: str) -> tuple[bool, str, str]:
    """Валидирует Steam ID"""
    if not steam_id or not str(steam_id).strip():
        return False, "", ""
  
    steam_id = str(steam_id).strip()
  
    if steam_id.startswith("sub_"):
        try:
            sub_id_num = steam_id[4:]
            if sub_id_num.isdigit() and len(sub_id_num) > 0:
                return True, "sub", sub_id_num
            else:
                return False, "", ""
        except:
            return False, "", ""
    else:
        if steam_id.isdigit() and len(steam_id) > 0:
            return True, "app", steam_id
        else:
            return False, "", ""

def get_steam_price(steam_id: str, currency_code: str = "UAH") -> Optional[float]:
    """Получает цену из Steam API"""
    is_valid, id_type, clean_id = validate_steam_id(steam_id)
    if not is_valid:
        logger.warning(f"{LOGGER_PREFIX} Неверный формат Steam ID: {steam_id}")
        return None
  
    currency_map = {
        "UAH": "ua",
        "KZT": "kz", 
        "RUB": "ru",
        "USD": "us"
    }
    cc_code = currency_map.get(currency_code, "ua")
  
    # Кеш на 1 час для Steam цен
    cache_key = f"steam_price_{steam_id}_{currency_code}"
    cached_data = CACHE.get(cache_key)
    if cached_data:
        cache_age = time.time() - cached_data.get("timestamp", 0)
        if cache_age < 3600:  # 1 час для Steam цен
            logger.debug(f"{LOGGER_PREFIX} Кешированная цена для Steam {steam_id}")
            return cached_data.get("price")
  
    try:
        time.sleep(SETTINGS["steam_request_delay"])
      
        if id_type == "sub":
            url = f"https://store.steampowered.com/api/packagedetails/?packageids={clean_id}&cc={cc_code}"
        else:
            url = f"https://store.steampowered.com/api/appdetails/?appids={clean_id}&cc={cc_code}&filters=price_overview"
            
        response = requests.get(url, timeout=SETTINGS["request_timeout"])
      
        if response.status_code == 200:
            data = response.json()
            item_data = data.get(str(clean_id))
          
            if item_data and item_data.get("success"):
                if id_type == "sub":
                    price_overview = item_data.get("data", {}).get("price")
                else:
                    price_overview = item_data.get("data", {}).get("price_overview")
              
                if price_overview:
                    final_price = price_overview.get("final", 0)
                  
                    if final_price > 0:
                        price_value = final_price / 100.0
                        CACHE.set(cache_key, {
                            "price": price_value,
                            "timestamp": time.time()
                        })
                        logger.debug(f"{LOGGER_PREFIX} Steam цена для {steam_id}: {price_value} {currency_code}")
                        return price_value
                    else:
                        CACHE.set(cache_key, {"price": 0.0, "timestamp": time.time()})
                        return 0.0
      
        return None
      
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} Ошибка получения Steam цены для {steam_id}: {e}")
        return None

def calculate_lot_price(steam_price: Union[float, int, str], steam_currency: str = "UAH") -> float:
    """Вычисляет цену лота с учетом валюты FunPay аккаунта"""
    try:
        steam_price = float(steam_price)
        if steam_price < 0:
            logger.warning(f"{LOGGER_PREFIX} Отрицательная цена Steam: {steam_price}")
            return 0.0
    except (ValueError, TypeError) as e:
        logger.warning(f"{LOGGER_PREFIX} Ошибка преобразования steam_price: {e}")
        return 0.0
  
    if steam_price <= 0.01:
        return SETTINGS["min_price"]
  
    try:
        account_currency = SETTINGS.get("currency", "USD")
      
        # Конвертация валют
        if steam_currency == account_currency:
            base_price = steam_price
        else:
            if account_currency == "USD":
                if steam_currency == "USD":
                    base_price = steam_price
                else:
                    currency_rate = get_currency_rate(steam_currency)
                    if currency_rate <= 0:
                        return 0.0
                    base_price = steam_price / currency_rate
            else:
                if steam_currency == "USD":
                    price_usd = steam_price
                else:
                    steam_rate = get_currency_rate(steam_currency)
                    if steam_rate <= 0:
                        return 0.0
                    price_usd = steam_price / steam_rate
              
                account_rate = get_currency_rate(account_currency)
                if account_rate <= 0:
                    return 0.0
                base_price = price_usd * account_rate
      
        # Применение наценок
        price_with_currency_markup = base_price * (1 + SETTINGS["first_markup"] / 100)
        final_price = price_with_currency_markup * (1 + SETTINGS["second_markup"] / 100) + SETTINGS["fixed_markup"]
      
        # Ограничения по цене
        final_price = min(final_price, SETTINGS["max_price"])
        final_price = max(final_price, SETTINGS["min_price"])
      
        return round(final_price, 2)
      
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка расчета цены: {e}")
        return 0.0

def cleanup_resources():
    """Очистка ресурсов при завершении"""
    try:
        logger.info(f"{LOGGER_PREFIX} Очистка ресурсов")
        CACHE.clear_expired()
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка очистки ресурсов: {e}")

def check_cardinal_health() -> bool:
    """Проверяет доступность Cardinal"""
    global CARDINAL_INSTANCE
    try:
        if not CARDINAL_INSTANCE:
            return False
        return hasattr(CARDINAL_INSTANCE, 'account') and CARDINAL_INSTANCE.account is not None
    except Exception:
        return False

def validate_lot_data(lot_data: dict) -> bool:
    """Валидирует данные лота"""
    required_fields = ["steam_id", "steam_currency", "min", "max"]
  
    for field in required_fields:
        if field not in lot_data:
            return False
  
    steam_id = lot_data.get("steam_id")
    if not steam_id or steam_id == "":
        return False
  
    min_price = lot_data.get("min")
    max_price = lot_data.get("max")
    if not isinstance(min_price, (int, float)) or not isinstance(max_price, (int, float)):
        return False
  
    if min_price <= 0 or max_price <= 0 or min_price > max_price:
        return False
  
    return True

def get_lot_name(lot_data) -> str:
    """Получает название лота из Steam API"""
    steam_id = lot_data.get("steam_id")
    if not steam_id:
        steam_app_id = lot_data.get("steam_app_id")
        if not steam_app_id:
            return "Неизвестная игра"
        steam_id = str(steam_app_id)
  
    if not steam_id:
        return "Неизвестная игра"
  
    cache_key = f"game_name_{steam_id}"
    cached_data = CACHE.get(cache_key)
    if cached_data:
        return cached_data["name"]
  
    try:
        is_sub_id = str(steam_id).startswith("sub_")
      
        if is_sub_id:
            sub_id = str(steam_id)[4:]
            url = f"https://store.steampowered.com/api/packagedetails"
            params = {"packageids": sub_id, "filters": "basic"}
        else:
            url = f"https://store.steampowered.com/api/appdetails"
            params = {"appids": steam_id, "filters": "basic"}
        
        response = requests.get(url, params=params, timeout=10)
      
        if response.status_code == 200:
            data = response.json()
            item_data = data.get(str(sub_id if is_sub_id else steam_id), {})
            if item_data.get("success") and "data" in item_data:
                name = item_data["data"].get("name", f"Steam {steam_id}")
                CACHE.set(cache_key, {"name": name, "timestamp": time.time()})
                return name
    except Exception as e:
        logger.debug(f"{LOGGER_PREFIX} Ошибка получения названия игры {steam_id}: {e}")
  
    return f"Steam {steam_id}"

def update_lot_price(lot_id: str, lot_data: dict, cardinal) -> bool:
    """Обновляет цену одного лота"""
    try:
        if not validate_lot_data(lot_data):
            logger.warning(f"{LOGGER_PREFIX} Невалидные данные лота {lot_id}")
            return False
      
        steam_id = lot_data.get("steam_id") or str(lot_data.get("steam_app_id", ""))
        if not steam_id or steam_id == "0":
            logger.warning(f"{LOGGER_PREFIX} Отсутствует Steam ID для лота {lot_id}")
            return False
      
        steam_currency = lot_data.get("steam_currency", Config.DEFAULT_STEAM_CURRENCY)
      
        # Получение цены Steam с повторными попытками
        steam_price = None
        for attempt in range(Config.MAX_RETRIES):
            steam_price = get_steam_price(steam_id, steam_currency)
            if steam_price and steam_price > 0:
                break
            if attempt < Config.MAX_RETRIES - 1:
                time.sleep(Config.LOT_PROCESSING_DELAY)
      
        if not steam_price or steam_price <= 0:
            logger.warning(f"{LOGGER_PREFIX} Не удалось получить цену Steam для лота {lot_id}")
            return False
      
        # Расчет новой цены
        new_price = calculate_lot_price(steam_price, steam_currency)
        if new_price <= 0:
            logger.error(f"{LOGGER_PREFIX} Неверная вычисленная цена для лота {lot_id}: {new_price}")
            return False
      
        # Применение ограничений лота
        lot_min = lot_data.get("min", SETTINGS["min_price"])
        lot_max = lot_data.get("max", SETTINGS["max_price"])
        new_price = max(lot_min, min(new_price, lot_max))
      
        # Изменение цены лота
        success = change_price(cardinal, lot_id, new_price)
        if success:
            LOTS[lot_id]["last_steam_price"] = steam_price
            LOTS[lot_id]["last_update"] = time.time()
            logger.info(f"{LOGGER_PREFIX} Лот {lot_id} обновлен: Steam {steam_price} {steam_currency} → ${new_price:.2f}")
      
        return success
      
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка обновления лота {lot_id}: {e}")
        return False

def change_price(cardinal: Cardinal, my_lot_id: str, new_price: float) -> bool:
    """Изменяет цену лота"""
    try:
        if my_lot_id not in LOTS:
            logger.warning(f"{LOGGER_PREFIX} Лот {my_lot_id} не найден")
            return False
      
        try:
            lot_fields = cardinal.account.get_lot_fields(int(my_lot_id))
            time.sleep(0.5)
        except Exception as api_error:
            logger.error(f"{LOGGER_PREFIX} Ошибка API при получении лота {my_lot_id}: {api_error}")
            if "не найден" in str(api_error).lower() or "not found" in str(api_error).lower():
                logger.warning(f"{LOGGER_PREFIX} Удаляю недоступный лот {my_lot_id}")
                if my_lot_id in LOTS:
                    del LOTS[my_lot_id]
                    save_lots()
            return False
      
        if lot_fields is None or not hasattr(lot_fields, 'price'):
            logger.error(f"{LOGGER_PREFIX} Лот {my_lot_id} недоступен или не имеет цены")
            return False
      
        old_price = lot_fields.price
        if old_price is None:
            logger.error(f"{LOGGER_PREFIX} Текущая цена лота {my_lot_id} равна None")
            return False
      
        # Проверка необходимости обновления
        if abs(round(new_price, 2) - round(old_price, 2)) >= 0.005:
            lot_fields.price = new_price
          
            if hasattr(cardinal.account, 'save_lot'):
                cardinal.account.save_lot(lot_fields)
                logger.info(f"{LOGGER_PREFIX} Лот {my_lot_id} обновлён: {old_price:.2f} → {new_price:.2f}")
              
                if my_lot_id in LOTS:
                    LOTS[my_lot_id]["last_price"] = new_price
                    LOTS[my_lot_id]["last_update"] = time.time()
              
                return True
            else:
                logger.error(f"{LOGGER_PREFIX} Метод save_lot недоступен")
                return False
        else:
            logger.info(f"{LOGGER_PREFIX} Лот {my_lot_id} остался на {old_price:.2f}")
            return True
          
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка изменения цены лота {my_lot_id}: {e}")
        return False

def save_lots():
    """Сохраняет лоты в файл"""
    try:
        os.makedirs("storage/plugins", exist_ok=True)
        
        json_data = json.dumps(LOTS, indent=4, ensure_ascii=False)
        
        save_attempts = [
            "storage/plugins/steam_price_updater_lots.json",
            "steam_price_updater_lots.json",
            "/tmp/steam_price_updater_lots.json"
        ]
        
        for attempt_file in save_attempts:
            try:
                if "/" in attempt_file:
                    dir_path = os.path.dirname(attempt_file)
                    if dir_path and not os.path.exists(dir_path):
                        os.makedirs(dir_path, exist_ok=True)
              
                with open(attempt_file, 'w', encoding='utf-8') as f:
                    f.write(json_data)
                    f.flush()
              
                logger.info(f"{LOGGER_PREFIX} Лоты сохранены в {attempt_file}")
                return
            except (PermissionError, OSError, IOError):
                continue
        
        logger.error(f"{LOGGER_PREFIX} Не удалось сохранить лоты")
          
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Критическая ошибка сохранения лотов: {e}")

def save_settings():
    """Сохраняет настройки"""
    try:
        os.makedirs("storage/plugins", exist_ok=True)
        with open("storage/plugins/steam_price_updater.json", "w", encoding="utf-8") as f:
            f.write(json.dumps(SETTINGS, indent=4, ensure_ascii=False))
        logger.info(f"{LOGGER_PREFIX} Настройки сохранены")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка сохранения настроек: {e}")

def load_settings():
    """Загружает настройки"""
    global SETTINGS
    if os.path.exists("storage/plugins/steam_price_updater.json"):
        try:
            with open("storage/plugins/steam_price_updater.json", "r", encoding="utf-8") as f:
                content = f.read()
                if content.strip():
                    loaded_settings = json.loads(content)
                    SETTINGS.update(loaded_settings)
        except Exception as e:
            logger.warning(f"{LOGGER_PREFIX} Ошибка загрузки настроек: {e}")

def load_lots():
    """Загружает лоты"""
    global LOTS
    load_attempts = [
        "storage/plugins/steam_price_updater_lots.json",
        "steam_price_updater_lots.json", 
        "/tmp/steam_price_updater_lots.json"
    ]
  
    for attempt_file in load_attempts:
        if os.path.exists(attempt_file):
            try:
                with open(attempt_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    if content.strip():
                        LOTS = json.loads(content)
                        
                        # Миграция старых данных
                        for lot_id, lot_data in LOTS.items():
                            if "steam_id" not in lot_data and "steam_app_id" in lot_data:
                                LOTS[lot_id]["steam_id"] = str(lot_data["steam_app_id"])
                            if "steam_currency" not in lot_data:
                                LOTS[lot_id]["steam_currency"] = "UAH"
                            if "min" not in lot_data:
                                LOTS[lot_id]["min"] = SETTINGS["min_price"]
                            if "max" not in lot_data:
                                LOTS[lot_id]["max"] = SETTINGS["max_price"]
                        
                        save_lots()
                        logger.info(f"{LOGGER_PREFIX} Загружено {len(LOTS)} лотов")
                        return
            except Exception as e:
                logger.warning(f"{LOGGER_PREFIX} Ошибка загрузки лотов из {attempt_file}: {e}")
    
    LOTS = {}

def init(cardinal: Cardinal):
    global CARDINAL_INSTANCE
    CARDINAL_INSTANCE = cardinal
    
    # Регистрируем очистку ресурсов
    atexit.register(cleanup_resources)
    
    # Загружаем настройки и лоты
    load_settings()
    load_lots()
    
    if not cardinal.telegram:
        logger.warning(f"{LOGGER_PREFIX} Telegram бот не включен в FunPayCardinal. Плагин Steam Price Updater не будет работать.")
        return

    tg = cardinal.telegram
    bot = tg.bot

    logger.info(f"{LOGGER_PREFIX} Инициализация Telegram хэндлеров...")

    # Simplified initialization - settings and lots already loaded




    def open_settings(call: telebot.types.CallbackQuery):
        """Главное меню плагина"""
        try:
            keyboard = K()
            
            # Основные кнопки
            keyboard.row(
                B("📦 Лоты", callback_data=f"{CBT_LOTS_MENU}:0"),
                B("🔄 Обновить сейчас", callback_data=f"{CBT_UPDATE_NOW}:")
            )
            keyboard.row(
                B("⚙️ Настройки", callback_data=f"{CBT_SHOW_SETTINGS}:"),
                B("📊 Статистика", callback_data=f"{CBT_STATS}:")
            )
            keyboard.row(
                B("❓ Помощь", url="https://t.me/humblegodq"),
                B("◀ Назад", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0")
            )
            
            # Статистика
            active_lots = len([l for l in LOTS.values() if l.get('on', False)])
            total_lots = len(LOTS)
            hours = SETTINGS['time'] // 3600
            
            text = f"🎮 <b>Steam Price Updater v{VERSION}</b>\n\n"
            text += f"📦 <b>Лоты:</b> {total_lots} всего, {active_lots} активных\n"
            text += f"⏱ <b>Интервал:</b> {hours} ч\n"
            text += f"💰 <b>Валюта:</b> {SETTINGS.get('currency', 'USD')}\n"
            text += f"📈 Наценка: {SETTINGS['first_markup']}% + {SETTINGS['second_markup']}% + ${SETTINGS['fixed_markup']}"
            
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в open_settings: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def show_settings(call: telebot.types.CallbackQuery):
        """Показывает настройки плагина"""
        try:
            text = f"⚙️ <b>Настройки Steam Price Updater</b>\n\n"
            text += f"💱 Валюта: {SETTINGS.get('currency', 'USD')}\n"
            text += f"⏱ Интервал: {SETTINGS['time'] // 3600} ч\n"
            text += f"📈 Наценка курса: {SETTINGS['first_markup']}%\n"
            text += f"📊 Маржа: {SETTINGS['second_markup']}%\n"
            text += f"💵 Фикс. наценка: ${SETTINGS['fixed_markup']}"
            
            keyboard = K()
            keyboard.row(
                B("💱 Валюта", callback_data=f"{CBT_CHANGE_CURRENCY}:switch"),
                B("⏱ Интервал", callback_data=f"{CBT_TEXT_EDIT}:settings:time")
            )
            keyboard.row(
                B("📈 Наценка курса", callback_data=f"{CBT_TEXT_EDIT}:settings:first_markup"),
                B("📊 Маржа", callback_data=f"{CBT_TEXT_EDIT}:settings:second_markup")
            )
            keyboard.row(
                B("💵 Фикс. наценка", callback_data=f"{CBT_TEXT_EDIT}:settings:fixed_markup"),
                B("🔄 Обновить курсы", callback_data=f"{CBT_REFRESH_RATES}:")
            )
            keyboard.add(B("◀ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в show_settings: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def switch_currency(call: telebot.types.CallbackQuery):
        """Переключает валюту FunPay аккаунта"""
        try:
            account_currencies = ["USD", "RUB", "EUR"]
            try:
                current_currency = SETTINGS.get("currency", "USD")
                current_index = account_currencies.index(current_currency)
                SETTINGS["currency"] = account_currencies[(current_index + 1) % len(account_currencies)]
            except ValueError:
                SETTINGS["currency"] = "USD"
            
            save_settings()
            
            currency_symbols = {"USD": "$", "RUB": "₽", "EUR": "€"}
            symbol = currency_symbols.get(SETTINGS["currency"], SETTINGS["currency"])
            bot.answer_callback_query(call.id, f"Валюта: {symbol} {SETTINGS['currency']}")
            show_settings(call)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в switch_currency: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def switch_steam_currency(call: telebot.types.CallbackQuery):
        """Переключает валюту Steam для лота"""
        try:
            if not call.data:
                return
            
            parts = call.data.split(":")
            if len(parts) < 2:
                return
            
            lot_id = parts[1]
            if lot_id not in LOTS:
                bot.answer_callback_query(call.id, "❌ Лот не найден")
                return
            
            currencies = ["UAH", "KZT", "RUB", "USD"]
            current_currency = LOTS[lot_id].get("steam_currency", "UAH")
            
            try:
                current_index = currencies.index(current_currency)
                LOTS[lot_id]["steam_currency"] = currencies[(current_index + 1) % len(currencies)]
            except ValueError:
                LOTS[lot_id]["steam_currency"] = "UAH"
            
            save_lots()
            bot.answer_callback_query(call.id, f"Валюта: {LOTS[lot_id]['steam_currency']}")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в switch_steam_currency: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    # Essential handlers only - simplified versions
    
    def update_now(call: telebot.types.CallbackQuery):
        """Запускает принудительное обновление"""
        try:
            if not check_cardinal_health():
                bot.answer_callback_query(call.id, "❌ Cardinal недоступен")
                return
            
            active_lots = [lot_id for lot_id, lot_data in LOTS.items() 
                          if lot_data.get("on", False) and lot_id != "0"]
            
            if not active_lots:
                bot.answer_callback_query(call.id, "Нет активных лотов")
                return
            
            bot.answer_callback_query(call.id, "Обновление запущено...")
            
            def update_thread():
                updated = 0
                failed = 0
                
                for lot_id in active_lots:
                    try:
                        lot_data = LOTS[lot_id]
                        if update_lot_price(lot_id, lot_data, CARDINAL_INSTANCE):
                            updated += 1
                        else:
                            failed += 1
                        time.sleep(Config.LOT_PROCESSING_DELAY)
                    except Exception as e:
                        logger.error(f"{LOGGER_PREFIX} Ошибка обновления лота {lot_id}: {e}")
                        failed += 1
                
                save_lots()
                result_text = f"Обновление завершено!\nОбновлено: {updated}\nОшибок: {failed}"
                bot.send_message(call.message.chat.id, result_text)
            
            Thread(target=update_thread, daemon=True).start()
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в update_now: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")
    
    def show_stats(call: telebot.types.CallbackQuery):
        """Показывает статистику"""
        try:
            active_lots = [lot for lot in LOTS.values() if lot.get("on", False)]
            lots_with_prices = len([l for l in LOTS.values() if l.get("last_price", 0) > 0])
            cache_hits = len(CACHE.cache)
            
            text = f"📊 Статистика Steam Price Updater\n\n"
            text += f"📦 Всего лотов: {len(LOTS)}\n"
            text += f"✅ Активных: {len(active_lots)}\n"
            text += f"💰 Лотов с ценами: {lots_with_prices}\n"
            text += f"🔄 Кеш: {cache_hits} записей"
            
            keyboard = K()
            keyboard.add(B("◀ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в show_stats: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")
    
    def refresh_currency_rates(call: telebot.types.CallbackQuery):
        """Принудительно обновляет курсы валют"""
        try:
            bot.answer_callback_query(call.id, "Обновляю курсы...")
            
            def refresh_thread():
                try:
                    clear_currency_cache()
                    uah_rate = get_currency_rate("UAH")
                    rub_rate = get_currency_rate("RUB") 
                    kzt_rate = get_currency_rate("KZT")
                    
                    result_text = f"💱 Курсы валют обновлены:\n"
                    result_text += f"🇺🇦 USD/UAH: {uah_rate:.2f}\n"
                    result_text += f"🇷🇺 USD/RUB: {rub_rate:.2f}\n"
                    result_text += f"🇰🇿 USD/KZT: {kzt_rate:.2f}"
                    
                    bot.send_message(call.message.chat.id, result_text)
                except Exception as e:
                    logger.error(f"{LOGGER_PREFIX} Ошибка обновления курсов: {e}")
                    bot.send_message(call.message.chat.id, "❌ Ошибка обновления курсов")
            
            Thread(target=refresh_thread, daemon=True).start()
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в refresh_currency_rates: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")
    
    def simple_lot_menu(call: telebot.types.CallbackQuery):
        """Простое меню лотов"""
        try:
            text = f"📦 <b>Управление лотами</b>\n\n"
            text += f"📊 Всего: {len(LOTS)} | Активных: {len([l for l in LOTS.values() if l.get('on', False)])}\n\n"
            
            if not LOTS:
                text += "Лоты не добавлены"
            else:
                text += "Для настройки лотов используйте FunPay Cardinal"
            
            keyboard = K()
            keyboard.add(B("◀ Главное меню", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в simple_lot_menu: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    # Register essential handlers
    tg.cbq_handler(open_settings, lambda c: c.data and c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    tg.cbq_handler(show_settings, lambda c: c.data and c.data.startswith(CBT_SHOW_SETTINGS))
    tg.cbq_handler(switch_currency, lambda c: c.data and c.data.startswith(CBT_CHANGE_CURRENCY))
    tg.cbq_handler(switch_steam_currency, lambda c: c.data and c.data.startswith(CBT_CHANGE_STEAM_CURRENCY))
    tg.cbq_handler(update_now, lambda c: c.data and c.data.startswith(CBT_UPDATE_NOW))
    tg.cbq_handler(show_stats, lambda c: c.data and c.data.startswith(CBT_STATS))
    tg.cbq_handler(simple_lot_menu, lambda c: c.data and c.data.startswith(CBT_LOTS_MENU))
    tg.cbq_handler(refresh_currency_rates, lambda c: c.data and c.data.startswith(CBT_REFRESH_RATES))

    logger.info(f"{LOGGER_PREFIX} Инициализация завершена")

def post_start(cardinal):
    """Запуск основного потока обработки лотов"""
    def process(cardinal):
        """Основной цикл обработки лотов"""
        global LOTS, SETTINGS, CARDINAL_INSTANCE
        lot_last_check = {}
        
        logger.info(f"{LOGGER_PREFIX} Запущен основной цикл обработки лотов")
        
        while True:
            try:
                current_time = time.time()
                any_lot_processed = False
                
                # Обрабатываем только активные лоты
                for lot_id, lot_data in LOTS.items():
                    if lot_id == "0" or not lot_data.get("on", False):
                        continue
                    
                    # Проверяем интервал обновления (6 часов)
                    global_interval = SETTINGS["time"]
                    last_check = lot_last_check.get(lot_id, 0)
                    if current_time - last_check < global_interval:
                        continue
                    
                    # Обновляем лот
                    lot_last_check[lot_id] = current_time
                    any_lot_processed = True
                    
                    logger.info(f"{LOGGER_PREFIX} Обрабатываю лот {lot_id}")
                    
                    try:
                        steam_id = lot_data.get("steam_id")
                        if not steam_id:
                            steam_app_id = lot_data.get("steam_app_id")
                            if steam_app_id:
                                steam_id = str(steam_app_id)
                        
                        steam_currency = lot_data.get("steam_currency", "UAH")
                        
                        if not steam_id:
                            logger.info(f"{LOGGER_PREFIX} Нет Steam ID для лота {lot_id}")
                            continue
                        
                        # Получаем цену Steam
                        steam_price = get_steam_price(steam_id, steam_currency)
                        
                        if steam_price is None or steam_price == 0:
                            logger.warning(f"{LOGGER_PREFIX} Нет цены Steam для лота {lot_id}")
                            continue
                        
                        # Вычисляем новую цену
                        new_price = calculate_lot_price(steam_price, steam_currency)
                        
                        if new_price <= 0:
                            logger.error(f"{LOGGER_PREFIX} Неверная цена для лота {lot_id}: {new_price}")
                            continue
                        
                        # Применяем ограничения лота
                        lot_min = lot_data.get("min", SETTINGS["min_price"])
                        lot_max = lot_data.get("max", SETTINGS["max_price"])
                        new_price = max(lot_min, min(new_price, lot_max))
                        
                        # Сохраняем цену Steam
                        LOTS[lot_id]["last_steam_price"] = steam_price
                        
                        # Изменяем цену лота
                        change_price(CARDINAL_INSTANCE, lot_id, new_price)
                        
                        time.sleep(2)
                    
                    except Exception as e:
                        logger.warning(f"{LOGGER_PREFIX} Ошибка с лотом {lot_id}: {e}")
                
                # Сохраняем изменения
                if any_lot_processed:
                    save_lots()
                    logger.info(f"{LOGGER_PREFIX} Цикл обработки завершен")
            
            except Exception as e:
                logger.error(f"{LOGGER_PREFIX} Критическая ошибка в процессе: {e}")
            
            # Пауза между циклами
            time.sleep(300)

    if not hasattr(cardinal, '_steam_updater_thread_running') or not cardinal._steam_updater_thread_running:
        logger.info(f"{LOGGER_PREFIX} Запускаю поток обработки лотов")
        thread = Thread(target=process, daemon=True, args=(cardinal,))
        thread.start()
        cardinal._steam_updater_thread_running = True
    else:
        logger.info(f"{LOGGER_PREFIX} Поток уже запущен")

# Removed unused functions - everything below this point was redundant

BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [post_start]
BIND_TO_DELETE = None
