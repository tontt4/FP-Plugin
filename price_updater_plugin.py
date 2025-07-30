from __future__ import annotations
import json
import time
import requests
import atexit
import signal
import threading
from threading import Thread, Lock
from typing import TYPE_CHECKING, Optional, Union, Dict, Any
from datetime import datetime as dt
import os
import xml.etree.ElementTree as ET
import tempfile

from FunPayAPI.types import LotShortcut

if TYPE_CHECKING:
    from cardinal import Cardinal
from FunPayAPI.updater.events import *
from tg_bot import CBT
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B
import telebot
import logging
from locales.localizer import Localizer

localizer = Localizer()
_ = localizer.translate

NAME = "Steam Price Updater"
VERSION = "2.0.2"
DESCRIPTION = "Автоматическое обновление цен лотов на основе Steam API с выбором валют"
CREDITS = "@humblegodq"
UUID = "247153d9-f732-4f01-a11f-a3945b68b533"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.steam_price_updater")
LOGGER_PREFIX = "[STEAM PRICE UPDATER]"


class Config:
    CACHE_TTL = 3600
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
    "time": 21600,
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
    """Потокобезпечний менеджер кешу з TTL"""
    
    def __init__(self, max_size: int = Config.MAX_CACHE_SIZE, ttl: int = Config.CACHE_TTL):
        self.cache = {}
        self.max_size = max_size
        self.ttl = ttl
        self._lock = Lock()
    
    def get(self, key: str):
        """Повертає значення з кешу з перевіркою TTL"""
        with self._lock:
            if key in self.cache:
                entry = self.cache[key]
                if time.time() - entry["timestamp"] < self.ttl:
                    return entry["value"]
                else:
                    self._remove_key_safe(key)
            return None
    
    def set(self, key: str, value):
        """Встановлює значення в кеш"""
        with self._lock:
            if len(self.cache) >= self.max_size:
                self._cleanup_oldest()
            
            self.cache[key] = {
                "value": value,
                "timestamp": time.time()
            }
    
    def _remove_key_safe(self, key: str):
        """Безпечне видалення ключа"""
        try:
            del self.cache[key]
        except KeyError:
            pass
    
    def _cleanup_oldest(self):
        """Очищує найстаріший запис"""
        try:
            oldest_key = min(self.cache.keys(), 
                           key=lambda k: self.cache[k]["timestamp"])
            self._remove_key_safe(oldest_key)
        except (ValueError, KeyError):
            pass
    
    def clear_expired(self):
        """Очищує прострочені записи"""
        with self._lock:
            current_time = time.time()
            expired_keys = [k for k, v in self.cache.items() 
                           if current_time - v["timestamp"] >= self.ttl]
            for key in expired_keys:
                self._remove_key_safe(key)
    
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


class FileManager:
    """Менеджер для роботи з файлами"""
    
    @staticmethod
    def save_json_safely(data: Dict[str, Any], filename: str, fallback_locations: list = None) -> bool:
        """Безпечне збереження JSON файлу з резервними локаціями"""
        if fallback_locations is None:
            fallback_locations = [
                f"storage/plugins/{filename}",
                filename,
                f"/tmp/{filename}",
                f"./{filename.replace('.json', '_backup.json')}"
            ]
        
        json_data = json.dumps(data, indent=4, ensure_ascii=False)
        
        for location in fallback_locations:
            try:
                # Створюємо директорію якщо потрібно
                dir_path = os.path.dirname(location)
                if dir_path and not os.path.exists(dir_path):
                    os.makedirs(dir_path, exist_ok=True)
                
                # Записуємо файл
                with open(location, "w", encoding="utf-8") as f:
                    f.write(json_data)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except (OSError, AttributeError):
                        pass
                
                # Перевіряємо що файл записався
                if os.path.exists(location):
                    file_size = os.path.getsize(location)
                    logger.info(f"{LOGGER_PREFIX} Дані збережено в {location} (розмір: {file_size} байт)")
                    return True
                    
            except (PermissionError, OSError, IOError) as e:
                logger.warning(f"{LOGGER_PREFIX} Не вдалося зберегти в {location}: {e}")
                continue
        
        # Екстрене збереження у тимчасовий файл
        try:
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json', encoding='utf-8') as tmp_file:
                tmp_file.write(json_data)
                logger.warning(f"{LOGGER_PREFIX} Екстрене збереження в {tmp_file.name}")
                return True
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Критична помилка збереження: {e}")
            return False
    
    @staticmethod
    def load_json_safely(filename: str, fallback_locations: list = None) -> Dict[str, Any]:
        """Безпечне завантаження JSON файлу"""
        if fallback_locations is None:
            fallback_locations = [
                f"storage/plugins/{filename}",
                filename,
                f"/tmp/{filename}"
            ]
        
        for location in fallback_locations:
            if os.path.exists(location):
                try:
                    with open(location, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        logger.info(f"{LOGGER_PREFIX} Дані завантажено з {location}")
                        return data
                except (json.JSONDecodeError, OSError, IOError) as e:
                    logger.warning(f"{LOGGER_PREFIX} Помилка завантаження з {location}: {e}")
                    continue
        
        logger.warning(f"{LOGGER_PREFIX} Не вдалося завантажити {filename}, використовую порожні дані")
        return {}


class CurrencyManager:
    """Менеджер валютних курсів"""
    
    def __init__(self, cache_manager: ThreadSafeCacheManager):
        self.cache = cache_manager
        self.fallback_rates = {
            "UAH": 41.5,
            "RUB": 95.0, 
            "KZT": 450.0,
            "EUR": 0.85,
            "USD": 1.0
        }
    
    def get_currency_rate(self, currency: str = "USD") -> float:
        """Уніфікована функція для отримання курсу валют"""
        currency = currency.upper()
        
        # Перевіряємо кеш
        cache_key = f"{currency}_rate"
        cached_rate = self.cache.get(cache_key)
        if cached_rate and isinstance(cached_rate, dict):
            cache_age = time.time() - cached_rate.get("timestamp", 0)
            if cache_age < 900:  # 15 хвилин
                logger.debug(f"{LOGGER_PREFIX} Використовую кеш для USD/{currency}: {cached_rate.get('rate')}")
                return cached_rate.get("rate", self.fallback_rates.get(currency, 1.0))
        
        # Отримуємо свіжий курс
        rate = self._fetch_currency_rate(currency)
        if rate > 0:
            self.cache.set(cache_key, {
                "rate": rate,
                "timestamp": time.time(),
                "source": "api"
            })
            logger.info(f"{LOGGER_PREFIX} Отримано свіжий курс USD/{currency}: {rate}")
            return rate
        
        # Використовуємо fallback
        return self.fallback_rates.get(currency, 1.0)
    
    def _fetch_currency_rate(self, currency: str) -> float:
        """Отримує курс валюти з API"""
        try:
            # Основний API
            url = "https://api.exchangerate-api.com/v4/latest/USD"
            response = requests.get(url, timeout=Config.REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                data = response.json()
                rates = data.get("rates", {})
                if currency in rates:
                    return float(rates[currency])
            
            # Резервні API для конкретних валют
            return self._fetch_fallback_rate(currency)
            
        except Exception as e:
            logger.warning(f"{LOGGER_PREFIX} Помилка отримання курсу USD/{currency}: {e}")
            return 0.0
    
    def _fetch_fallback_rate(self, currency: str) -> float:
        """Резервні API для валют"""
        try:
            if currency == "RUB":
                url = "https://www.cbr-xml-daily.ru/daily_json.js"
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    usd_data = data.get("Valute", {}).get("USD", {})
                    if usd_data:
                        return float(usd_data["Value"])
            
            elif currency == "UAH":
                url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"
                response = requests.get(url, timeout=Config.REQUEST_TIMEOUT)
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list) and len(data) > 0:
                        return float(data[0]["rate"])
            
            elif currency == "KZT":
                url = f"https://www.nationalbank.kz/rss/get_rates.cfm?fdate={time.strftime('%d.%m.%Y')}"
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    root = ET.fromstring(response.content)
                    for item in root.findall(".//item"):
                        title = item.find("title")
                        if title is not None and "USD" in title.text:
                            quant = item.find("quant")
                            if quant is not None:
                                return float(quant.text)
        
        except Exception as e:
            logger.warning(f"{LOGGER_PREFIX} Помилка резервного API для {currency}: {e}")
        
        return 0.0


# Ініціалізуємо глобальні об'єкти
CACHE = ThreadSafeCacheManager()
currency_manager = CurrencyManager(CACHE)
file_manager = FileManager()

# Константи для callback кнопок
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
CBT_SWITCH_PRICE_TYPE = "SPU_switch_price_type"


def get_currency_rate(currency: str = "USD") -> float:
    """Обгортка для сумісності з існуючим кодом"""
    return currency_manager.get_currency_rate(currency)


def validate_steam_id(steam_id: str) -> tuple[bool, str, str]:
    """Валідує Steam ID"""
    if not steam_id or not isinstance(steam_id, str):
        return False, "", ""
    
    steam_id = steam_id.strip()
    
    if steam_id.lower().startswith("sub"):
        try:
            sub_id_num = steam_id[3:]
            if sub_id_num.isdigit() and len(sub_id_num) > 0:
                return True, "sub", sub_id_num
        except:
            pass
    elif steam_id.lower().startswith("app"):
        try:
            app_id_num = steam_id[3:]
            if app_id_num.isdigit() and len(app_id_num) > 0:
                return True, "app", app_id_num
        except:
            pass
    elif steam_id.isdigit() and len(steam_id) > 0:
        return True, "app", steam_id
    
    return False, "", ""


def get_steam_price(steam_id: str, currency_code: str = "UAH") -> Optional[float]:
    """Отримує ціну з Steam API"""
    is_valid, id_type, clean_id = validate_steam_id(steam_id)
    if not is_valid:
        logger.warning(f"{LOGGER_PREFIX} Неправильний формат Steam ID: {steam_id}")
        return None
    
    currency_map = {
        "UAH": "ua", "KZT": "kz", "RUB": "ru", "USD": "us", "EUR": "eu"
    }
    cc_code = currency_map.get(currency_code, "ua")
    
    # Перевіряємо кеш
    cache_key = f"steam_price_{steam_id}_{currency_code}"
    cached_price = CACHE.get(cache_key)
    if cached_price is not None:
        logger.debug(f"{LOGGER_PREFIX} Кешована ціна для Steam {steam_id} ({currency_code})")
        return cached_price
    
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
                price_data = item_data.get("data", {})
                
                if id_type == "sub":
                    price_overview = price_data.get("price")
                else:
                    price_overview = price_data.get("price_overview")
                
                if price_overview:
                    final_price = price_overview.get("final", 0)
                    if final_price > 0:
                        price_value = final_price / 100.0
                        CACHE.set(cache_key, price_value)
                        logger.debug(f"{LOGGER_PREFIX} Steam ціна для {steam_id}: {price_value} {currency_code}")
                        return price_value
                
                # Ціна 0 або відсутня
                CACHE.set(cache_key, 0.0)
                return 0.0
        
        return None
        
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} Помилка отримання Steam ціни для {steam_id} ({currency_code}): {e}")
        return None


def calculate_lot_price(steam_price: Union[float, int, str], steam_currency: str = "UAH") -> float:
    """Обчислює ціну лота з урахуванням валюти FunPay аккаунта"""
    try:
        steam_price = float(steam_price)
        if steam_price < 0:
            logger.warning(f"{LOGGER_PREFIX} Від'ємна ціна Steam: {steam_price}")
            return 0.0
    except (ValueError, TypeError) as e:
        logger.warning(f"{LOGGER_PREFIX} Помилка перетворення steam_price: {e}")
        return 0.0
    
    if steam_price <= 0.01:
        return SETTINGS["min_price"]
    
    try:
        account_currency = SETTINGS.get("currency", "USD")
        
        # Конвертуємо в валюту аккаунта
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
                # Через USD
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
        
        # Применяємо націнки
        price_with_currency_markup = base_price * (1 + SETTINGS["first_markup"] / 100)
        final_price = price_with_currency_markup * (1 + SETTINGS["second_markup"] / 100) + SETTINGS["fixed_markup"]
        
        # Обмеження
        final_price = min(final_price, SETTINGS["max_price"])
        final_price = max(final_price, SETTINGS["min_price"])
        
        return round(final_price, 2)
        
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Помилка розрахунку ціни: {e}")
        return 0.0


def change_price(cardinal: Cardinal, my_lot_id: str, new_price: float) -> bool:
    """Змінює ціну лота"""
    try:
        logger.debug(f"{LOGGER_PREFIX} Спроба змінити ціну лота {my_lot_id} на {new_price}")
        
        if my_lot_id not in LOTS:
            logger.warning(f"{LOGGER_PREFIX} Лот {my_lot_id} не знайдено в списку")
            return False
        
        # Отримуємо поля лота
        try:
            lot_fields = cardinal.account.get_lot_fields(int(my_lot_id))
            time.sleep(0.5)
        except Exception as api_error:
            logger.error(f"{LOGGER_PREFIX} Помилка API при отриманні лота {my_lot_id}: {api_error}")
            
            # Видаляємо недоступний лот
            if "не найден" in str(api_error).lower() or "not found" in str(api_error).lower():
                logger.warning(f"{LOGGER_PREFIX} Видаляю недоступний лот {my_lot_id}")
                if my_lot_id in LOTS:
                    del LOTS[my_lot_id]
                    file_manager.save_json_safely(LOTS, "steam_price_updater_lots.json")
            return False
        
        if lot_fields is None or not hasattr(lot_fields, 'price'):
            logger.error(f"{LOGGER_PREFIX} Не вдалося отримати поля лота {my_lot_id}")
            return False
        
        old_price = lot_fields.price
        if old_price is None:
            logger.error(f"{LOGGER_PREFIX} Поточна ціна лота {my_lot_id} дорівнює None")
            return False
        
        # Перевіряємо чи потрібно оновлювати ціну
        if abs(round(new_price, 2) - round(old_price, 2)) >= 0.01:
            lot_fields.price = new_price
            
            if hasattr(cardinal.account, 'save_lot'):
                cardinal.account.save_lot(lot_fields)
                logger.info(f"{LOGGER_PREFIX} Лот {my_lot_id} оновлено: {old_price:.2f} → {new_price:.2f}")
                
                # Оновлюємо інформацію про лот
                if my_lot_id in LOTS:
                    LOTS[my_lot_id]["last_price"] = new_price
                    LOTS[my_lot_id]["last_update"] = time.time()
                
                return True
            else:
                logger.error(f"{LOGGER_PREFIX} Метод save_lot недоступний")
                return False
        else:
            logger.debug(f"{LOGGER_PREFIX} Ціна лота {my_lot_id} не потребує оновлення")
            return True
            
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Помилка зміни ціни лота {my_lot_id}: {e}")
        return False


class PriceUpdateScheduler:
    """Планувальник оновлення цін"""
    
    def __init__(self, cardinal, interval: int = 300):
        self.cardinal = cardinal
        self.interval = interval
        self.running = False
        self.thread = None
        self.lot_last_check = {}
    
    def start(self):
        """Запускає планувальник"""
        if not self.running:
            self.running = True
            self.thread = Thread(target=self._process_loop, daemon=True)
            self.thread.start()
            logger.info(f"{LOGGER_PREFIX} Планувальник запущено")
    
    def stop(self):
        """Зупиняє планувальник"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info(f"{LOGGER_PREFIX} Планувальник зупинено")
    
    def _process_loop(self):
        """Основний цикл обробки лотів"""
        while self.running:
            try:
                current_time = time.time()
                processed_count = 0
                
                for lot_id, lot_data in LOTS.items():
                    if not self.running:
                        break
                    
                    if lot_id == "0" or not lot_data.get("on", False):
                        continue
                    
                    # Перевіряємо інтервал оновлення
                    global_interval = SETTINGS["time"]
                    last_check = self.lot_last_check.get(lot_id, 0)
                    if current_time - last_check < global_interval:
                        continue
                    
                    # Оновлюємо лот
                    if self._update_single_lot(lot_id, lot_data):
                        processed_count += 1
                        self.lot_last_check[lot_id] = current_time
                        time.sleep(Config.LOT_PROCESSING_DELAY)
                
                # Зберігаємо зміни якщо були оновлення
                if processed_count > 0:
                    file_manager.save_json_safely(LOTS, "steam_price_updater_lots.json")
                    logger.info(f"{LOGGER_PREFIX} Оброблено {processed_count} лотів")
                
            except Exception as e:
                logger.error(f"{LOGGER_PREFIX} Критична помилка в циклі: {e}")
            
            # Пауза між циклами
            time.sleep(self.interval)
    
    def _update_single_lot(self, lot_id: str, lot_data: dict) -> bool:
        """Оновлює один лот"""
        try:
            # Отримуємо Steam ID
            steam_id = lot_data.get("steam_id") or lot_data.get("steam_app_id")
            if not steam_id:
                logger.warning(f"{LOGGER_PREFIX} Відсутній Steam ID для лота {lot_id}")
                return False
            
            steam_currency = lot_data.get("steam_currency", "UAH")
            
            # Отримуємо ціну Steam
            steam_price = get_steam_price(str(steam_id), steam_currency)
            if steam_price is None or steam_price <= 0:
                logger.warning(f"{LOGGER_PREFIX} Не вдалося отримати ціну Steam для лота {lot_id}")
                return False
            
            # Обчислюємо нову ціну
            new_price = calculate_lot_price(steam_price, steam_currency)
            if new_price <= 0:
                logger.error(f"{LOGGER_PREFIX} Неправильна ціна для лота {lot_id}: {new_price}")
                return False
            
            # Застосовуємо обмеження лота
            lot_min = lot_data.get("min", SETTINGS["min_price"])
            lot_max = lot_data.get("max", SETTINGS["max_price"])
            new_price = max(lot_min, min(new_price, lot_max))
            
            # Зберігаємо ціну Steam
            LOTS[lot_id]["last_steam_price"] = steam_price
            
            # Оновлюємо ціну
            return change_price(self.cardinal, lot_id, new_price)
            
        except Exception as e:
            logger.warning(f"{LOGGER_PREFIX} Помилка оновлення лота {lot_id}: {e}")
            return False


# Глобальний планувальник
price_scheduler = None


def cleanup_resources():
    """Очищує басейни при виході"""
    global price_scheduler
    if price_scheduler:
        price_scheduler.stop()
    
    # Зберігаємо дані
    file_manager.save_json_safely(SETTINGS, "steam_price_updater.json")
    file_manager.save_json_safely(LOTS, "steam_price_updater_lots.json")
    
    logger.info(f"{LOGGER_PREFIX} Ресурси очищено")


def init(cardinal: Cardinal):
    """Ініціалізація плагіна"""
    global CARDINAL_INSTANCE, SETTINGS, LOTS
    CARDINAL_INSTANCE = cardinal
    
    # Реєструємо очищення ресурсів
    atexit.register(cleanup_resources)
    
    if not cardinal.telegram:
        logger.warning(f"{LOGGER_PREFIX} Telegram бот не включений. Плагін не буде працювати.")
        return
    
    # Завантажуємо налаштування
    SETTINGS.update(file_manager.load_json_safely("steam_price_updater.json"))
    LOTS.update(file_manager.load_json_safely("steam_price_updater_lots.json"))
    
    logger.info(f"{LOGGER_PREFIX} Ініціалізація завершена. Завантажено {len(LOTS)} лотів")


def post_start(cardinal):
    """Запуск оптимізованого планувальника оновлення цін"""
    global price_scheduler
    
    if price_scheduler is None:
        price_scheduler = PriceUpdateScheduler(cardinal, Config.CYCLE_PAUSE)
        price_scheduler.start()
        logger.info(f"{LOGGER_PREFIX} Планувальник запущено")
    else:
        logger.info(f"{LOGGER_PREFIX} Планувальник вже працює")

def validate_code_integrity():
    """Проверяет целостность кода"""
    required_functions = [
        'init', 'post_start', 'get_steam_price', 
        'calculate_lot_price', 'update_lot_price'
    ]
  
    for func_name in required_functions:
        if func_name not in globals():
            logger.error(f"{LOGGER_PREFIX} Отсутствует функция: {func_name}")
            return False
    return True

try:
    validate_code_integrity()
    logger.info(f"{LOGGER_PREFIX} Код успешно инициализирован")
except Exception as e:
    logger.error(f"{LOGGER_PREFIX} Критическая ошибка инициализации: {e}")
    raise

BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [post_start]
BIND_TO_DELETE = None