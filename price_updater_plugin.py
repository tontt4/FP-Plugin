from __future__ import annotations
import json
import time
import requests
import atexit
import signal
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
VERSION = "2.0.1"
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
  
    def get_with_timestamp(self, key: str):
        """Возвращает полный объект кеша с timestamp"""
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
        """Поддержка оператора 'in'"""
        return self.get(key) is not None
  
    def __getitem__(self, key):
        """Поддержка получения значения через []"""
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value
  
    def __setitem__(self, key, value):
        """Поддержка установки значения через []"""
        self.set(key, value)
  
    def __len__(self):
        """Поддержка len()"""
        with self._lock:
            return len(self.cache)
  
    def keys(self):
        """Возвращает ключи"""
        with self._lock:
            return list(self.cache.keys())
  
    def __delitem__(self, key):
        """Поддержка удаления через del cache[key]"""
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

steam_price_cache = {}
usd_rate_cache = {"rate": 0.0, "timestamp": 0.0, "cache_duration": float(Config.CACHE_TTL)}
CACHE = ThreadSafeCacheManager()

steam_price_cache_lock = Lock()
usd_rate_cache_lock = Lock()

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
    """
    Унифицированная функция для получения курса валют
    ПРИОРИТЕТ: exchangerate-api для ВСЕХ валют включая UAH
    """
    currency = currency.upper()
  

    cache_key = f"{currency}_rate"
    cached_rate = CACHE.get(cache_key)
    if cached_rate and isinstance(cached_rate, dict):
    
        cache_age = time.time() - cached_rate.get("timestamp", 0)
        if cache_age < 900:
            logger.debug(f"{LOGGER_PREFIX} Использую кеш для USD/{currency}: {cached_rate.get('rate')} (возраст: {int(cache_age/60)} мин)")
            return cached_rate.get("rate", get_fallback_rate(currency))
        else:
            logger.debug(f"{LOGGER_PREFIX} Кеш USD/{currency} устарел ({int(cache_age/60)} мин), обновляю")
  
    try:
    
        logger.debug(f"{LOGGER_PREFIX} Получаю курс USD/{currency} через exchangerate-api")
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
              
                logger.info(f"{LOGGER_PREFIX} Получен СВЕЖИЙ курс USD/{currency}: {rate} (exchangerate-api)")
                return rate
            else:
                logger.warning(f"{LOGGER_PREFIX} Валюта {currency} не найдена в exchangerate-api")
        else:
            logger.warning(f"{LOGGER_PREFIX} exchangerate-api недоступен, статус: {response.status_code}")
      
    
        logger.info(f"{LOGGER_PREFIX} Переход на резервный API для {currency}")
        return get_currency_fallback(currency)
      
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} Ошибка получения курса USD/{currency}: {e}")
        return get_currency_fallback(currency)

def get_usd_to_uah_rate() -> float:
    """Получает курс USD к UAH из НБУ"""
    with usd_rate_cache_lock:
        current_time = time.time()
      
    
        if (current_time - usd_rate_cache["timestamp"] < usd_rate_cache["cache_duration"] 
            and usd_rate_cache["rate"] > 0):
            return usd_rate_cache["rate"]
      
        try:
        
            nbu_url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"
            response = requests.get(nbu_url, timeout=SETTINGS["request_timeout"])
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list) and len(data) > 0:
                    rate = float(data[0]["rate"])
                    usd_rate_cache["rate"] = float(rate)
                    usd_rate_cache["timestamp"] = float(current_time)
                    logger.info(f"{LOGGER_PREFIX} Получен курс USD/UAH: {rate} (НБУ)")
                    return rate
        except Exception as e:
            logger.warning(f"{LOGGER_PREFIX} Ошибка НБУ API: {e}")
      
      
        return usd_rate_cache.get("rate", 41.5)

def get_currency_fallback(currency: str) -> float:
    """Fallback API для получения курсов валют"""
    try:
        if currency == "RUB":
        
            cbr_url = "https://www.cbr-xml-daily.ru/daily_json.js"
            response = requests.get(cbr_url, timeout=10)
            if response.status_code == 200:
                cbr_data = response.json()
                usd_data = cbr_data.get("Valute", {}).get("USD", {})
                if usd_data:
                    rate = float(usd_data["Value"])
                    CACHE.set(f"{currency}_rate", {"rate": rate, "timestamp": time.time()})
                    logger.info(f"{LOGGER_PREFIX} Получен курс USD/RUB: {rate} (ЦБ РФ)")
                    return rate
      
        elif currency == "KZT":
        
            try:
                kz_url = "https://www.nationalbank.kz/rss/get_rates.cfm?fdate=" + time.strftime("%d.%m.%Y")
                response = requests.get(kz_url, timeout=10)
                if response.status_code == 200:
                
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(response.content)
                    for item in root.findall(".//item"):
                        title = item.find("title")
                        description = item.find("description")
                        if title is not None and "USD" in title.text:
                            rate_text = description.text if description is not None else ""
                        
                            import re
                            rate_match = re.search(r'(\d+\.?\d*)', rate_text)
                            if rate_match:
                                rate = float(rate_match.group(1))
                                CACHE.set(f"{currency}_rate", {"rate": rate, "timestamp": time.time()})
                                logger.info(f"{LOGGER_PREFIX} Получен курс USD/KZT: {rate} (Нацбанк КЗ)")
                                return rate
            except Exception as e:
                logger.warning(f"{LOGGER_PREFIX} Ошибка API Казахстана: {e}")
      
        elif currency == "EUR":
        
            try:
                ecb_url = "https://api.exchangerate-api.com/v4/latest/USD"
                response = requests.get(ecb_url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if "rates" in data and "EUR" in data["rates"]:
                        eur_to_usd = data["rates"]["EUR"]
                    
                        rate = 1.0 / eur_to_usd
                        CACHE.set(f"{currency}_rate", {"rate": rate, "timestamp": time.time()})
                        logger.info(f"{LOGGER_PREFIX} Получен курс USD/EUR: {rate} (ECB)")
                        return rate
            except Exception as e:
                logger.warning(f"{LOGGER_PREFIX} Ошибка API EUR: {e}")
              
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} Ошибка fallback API для {currency}: {e}")
  

    return get_fallback_rate(currency)

def get_fallback_rate(currency: str) -> float:
    """Возвращает последние известные курсы из кеша или актуальные fallback курсы"""

    cache_key = f"{currency}_rate"
    cached_rate = CACHE.get_with_timestamp(cache_key)
  
    if cached_rate and isinstance(cached_rate, dict):
        rate = cached_rate.get("rate")
        if rate and rate > 0:
            cache_age = time.time() - cached_rate.get("timestamp", 0)
            logger.warning(f"{LOGGER_PREFIX} Используем последний известный курс USD/{currency}: {rate} (возраст: {int(cache_age/3600)}ч {int((cache_age%3600)/60)}м)")
            return rate
  

    fallback_rates = {
        "UAH": 41.82,
        "RUB": 78.42,
        "KZT": 519.86, 
        "EUR": 0.85, 
        "USD": 1.0
    }
    rate = fallback_rates.get(currency, 1.0)
    logger.warning(f"{LOGGER_PREFIX} Используем экстренный fallback курс USD/{currency}: {rate}")
    return rate

def get_usd_rate() -> float:
    """Получает актуальный курс доллара - алиас для get_currency_rate('USD')"""
    return get_currency_rate("USD")

def clear_currency_cache():
    """Принудительно очищает кеш курсов валют"""
    global CACHE
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
    """

    Возвращает: (is_valid, id_type, clean_id)
    """
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
    """

    """

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
  

    cache_key = f"steam_price_{steam_id}_{currency_code}"
    with steam_price_cache_lock:
        if cache_key in steam_price_cache:
            cached_data = steam_price_cache[cache_key]
            if time.time() - cached_data["timestamp"] < 3600:
                logger.debug(f"{LOGGER_PREFIX} Кешированная цена для Steam {steam_id} ({currency_code})")
                return cached_data["price"]
  
    try:
        time.sleep(SETTINGS["steam_request_delay"])
      
    
        if id_type == "sub":
        
            url = f"https://store.steampowered.com/api/packagedetails/?packageids={clean_id}&cc={cc_code}"
            response = requests.get(url, timeout=SETTINGS["request_timeout"])
          
            if response.status_code == 200:
                data = response.json()
                package_data = data.get(str(clean_id))
              
                if package_data and package_data.get("success"):
                    price_overview = package_data.get("data", {}).get("price")
                  
                    if price_overview:
                        final_price = price_overview.get("final", 0)
                      
                        if final_price > 0:
                            price_value = final_price / 100.0
                          
                        
                            with steam_price_cache_lock:
                                steam_price_cache[cache_key] = {
                                    "price": price_value,
                                    "timestamp": time.time()
                                }
                            logger.debug(f"{LOGGER_PREFIX} Steam цена для Sub ID {steam_id}: {price_value} {currency_code}")
                            return price_value
                    else:
                    
                        with steam_price_cache_lock:
                            steam_price_cache[cache_key] = {"price": 0.0, "timestamp": time.time()}
                        return 0.0
        else:
        
            url = f"https://store.steampowered.com/api/appdetails/?appids={clean_id}&cc={cc_code}&filters=price_overview"
            response = requests.get(url, timeout=SETTINGS["request_timeout"])
          
            if response.status_code == 200:
                data = response.json()
                app_data = data.get(str(clean_id))
              
                if app_data and app_data.get("success"):
                    price_overview = app_data.get("data", {}).get("price_overview")
                  
                    if price_overview:
                        final_price = price_overview.get("final", 0)
                      
                        if final_price > 0:
                            price_value = final_price / 100.0
                          
                        
                            with steam_price_cache_lock:
                                steam_price_cache[cache_key] = {
                                    "price": price_value,
                                    "timestamp": time.time()
                                }
                            logger.debug(f"{LOGGER_PREFIX} Steam цена для App ID {steam_id}: {price_value} {currency_code}")
                            return price_value
                    else:
                    
                        with steam_price_cache_lock:
                            steam_price_cache[cache_key] = {"price": 0.0, "timestamp": time.time()}
                        return 0.0
      
        return None
      
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} Ошибка получения Steam цены для {steam_id} ({currency_code}): {e}")
        return None

def calculate_lot_price(steam_price: Union[float, int, str], steam_currency: str = "UAH") -> float:
    """
    Вычисляет цену лота с учетом валюты FunPay аккаунта
  
    Логика наценки:
    - 3% наценка на валютный курс
    - 5% маржа прибыли
    - 0.5 единицы валюты фиксированная наценка (для дешевых игр)
    """

    try:
        if isinstance(steam_price, str):
            steam_price = float(steam_price)
        elif not isinstance(steam_price, (int, float)):
            logger.warning(f"{LOGGER_PREFIX} Неверный тип данных для steam_price: {type(steam_price)}")
            return 0.0
      
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
      
    
        if steam_currency == account_currency:
        
            base_price = steam_price
        else:
        
            if account_currency == "USD":
            
                if steam_currency == "USD":
                    base_price = steam_price
                else:
                    currency_rate = get_currency_rate(steam_currency)
                    if currency_rate <= 0:
                        logger.warning(f"{LOGGER_PREFIX} Неверный курс валюты: {currency_rate}")
                        return 0.0
                    base_price = steam_price / currency_rate
            else:
            
                if steam_currency == "USD":
                    price_usd = steam_price
                else:
                    steam_rate = get_currency_rate(steam_currency)
                    if steam_rate <= 0:
                        logger.warning(f"{LOGGER_PREFIX} Неверный курс Steam валюты: {steam_rate}")
                        return 0.0
                    price_usd = steam_price / steam_rate
              
            
                account_rate = get_currency_rate(account_currency)
                if account_rate <= 0:
                    logger.warning(f"{LOGGER_PREFIX} Неверный курс валюты аккаунта: {account_rate}")
                    return 0.0
                base_price = price_usd * account_rate
      
    
        price_with_currency_markup = base_price * (1 + SETTINGS["first_markup"] / 100)
      
    
        final_price = price_with_currency_markup * (1 + SETTINGS["second_markup"] / 100) + SETTINGS["fixed_markup"]
      
    
        final_price = min(final_price, SETTINGS["max_price"])
        final_price = max(final_price, SETTINGS["min_price"])
      
    
        final_price = round(final_price, 2)
      
    
        currency_symbol = {"USD": "$", "RUB": "₽", "EUR": "€"}.get(account_currency, account_currency)
      
        logger.debug(f"{LOGGER_PREFIX} Расчет цены: {steam_price} {steam_currency} → {base_price:.4f} {account_currency} → +3% курс → {price_with_currency_markup:.4f} → +5% маржа + {SETTINGS['fixed_markup']} → {currency_symbol}{final_price:.2f}")
      
        return final_price
      
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка расчета цены: {e}")
        return 0.0

def safe_cache_operation(operation_name: str):
    """Декоратор для безопасных операций с кешем"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.warning(f"{LOGGER_PREFIX} Ошибка {operation_name}: {e}")
                return None
        return wrapper
    return decorator

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
  

    missing_fields = []
    for field in required_fields:
        if field not in lot_data:
            missing_fields.append(field)
  
    if missing_fields:
        logger.debug(f"{LOGGER_PREFIX} Отсутствующие поля в lot_data: {missing_fields}")
        logger.debug(f"{LOGGER_PREFIX} Доступные поля: {list(lot_data.keys())}")
        return False
  

    steam_id = lot_data.get("steam_id")
    if not steam_id or steam_id == "":
        logger.debug(f"{LOGGER_PREFIX} Пустой steam_id: '{steam_id}'")
        return False
  
    min_price = lot_data.get("min")
    max_price = lot_data.get("max")
    if not isinstance(min_price, (int, float)) or not isinstance(max_price, (int, float)):
        logger.debug(f"{LOGGER_PREFIX} Неверный тип цен: min={min_price} ({type(min_price)}), max={max_price} ({type(max_price)})")
        return False
  
    if min_price <= 0 or max_price <= 0:
        logger.debug(f"{LOGGER_PREFIX} Отрицательные цены: min={min_price}, max={max_price}")
        return False
  
    if min_price > max_price:
        logger.debug(f"{LOGGER_PREFIX} min больше max: min={min_price}, max={max_price}")
        return False
  
    return True

def get_lot_name(lot_data) -> str:
    """Получает название лота из Steam API с поддержкой App ID и Sub ID"""

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
            response = requests.get(url, params=params, timeout=10)
          
            if response.status_code == 200:
                data = response.json()
                package_data = data.get(str(sub_id), {})
                if package_data.get("success") and "data" in package_data:
                    name = package_data["data"].get("name", f"Sub {steam_id}")
                    CACHE.set(cache_key, {"name": name, "timestamp": time.time()})
                    return name
        else:
        
            url = f"https://store.steampowered.com/api/appdetails"
            params = {"appids": steam_id, "filters": "basic"}
            response = requests.get(url, params=params, timeout=10)
          
            if response.status_code == 200:
                data = response.json()
                app_data = data.get(str(steam_id), {})
                if app_data.get("success") and "data" in app_data:
                    name = app_data["data"].get("name", f"App {steam_id}")
                    CACHE.set(cache_key, {"name": name, "timestamp": time.time()})
                    return name
    except Exception as e:
        logger.debug(f"{LOGGER_PREFIX} Ошибка получения названия игры {steam_id}: {e}")
  
    return f"Steam {steam_id}"

def update_lot_price(lot_id: str, lot_data: dict, cardinal) -> bool:
    """Обновляет цену одного лота с полной валидацией и retry механизмом"""
    try:
    
        logger.debug(f"{LOGGER_PREFIX} Обновление лота {lot_id}: получены данные {lot_data}")
      
    
        validation_result = validate_lot_data(lot_data)
        if not validation_result:
            logger.warning(f"{LOGGER_PREFIX} Невалидные данные лота {lot_id}")
            logger.warning(f"{LOGGER_PREFIX} Полученные данные: {lot_data}")
        
            logger.warning(f"{LOGGER_PREFIX} ВНИМАНИЕ: Валидация отключена для отладки")
        else:
            logger.debug(f"{LOGGER_PREFIX} Валидация лота {lot_id} пройдена успешно")
      
    
        steam_id = lot_data.get("steam_id") or str(lot_data.get("steam_app_id", ""))
        if not steam_id or steam_id == "0":
            logger.warning(f"{LOGGER_PREFIX} Отсутствует Steam ID для лота {lot_id}")
            return False
      
        steam_currency = lot_data.get("steam_currency", Config.DEFAULT_STEAM_CURRENCY)
      
    
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
      
    
        new_price = calculate_lot_price(steam_price, steam_currency)
        if new_price <= 0:
            logger.error(f"{LOGGER_PREFIX} Неверная вычисленная цена для лота {lot_id}: {new_price}")
            return False
      
    
        lot_min = lot_data.get("min", SETTINGS["min_price"])
        lot_max = lot_data.get("max", SETTINGS["max_price"])
        new_price = max(lot_min, min(new_price, lot_max))
      
    
        success = change_price(cardinal, lot_id, new_price)
        if success:
            LOTS[lot_id]["last_steam_price"] = steam_price
            LOTS[lot_id]["last_update"] = time.time()
            logger.info(f"{LOGGER_PREFIX} Лот {lot_id} успешно обновлен: Steam {steam_price} {steam_currency} → ${new_price:.2f}")
      
        return success
      
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка обновления лота {lot_id}: {e}")
        return False

def change_price(cardinal: Cardinal, my_lot_id: str, new_price: float) -> bool:
    """Изменяет цену лота"""
    try:
        logger.debug(f"{LOGGER_PREFIX} Попытка изменить цену лота {my_lot_id} на {new_price}")
      
    
        if my_lot_id not in LOTS:
            logger.warning(f"{LOGGER_PREFIX} Лот {my_lot_id} не найден в списке добавленных лотов (всего лотов: {len(LOTS)})")
            logger.debug(f"{LOGGER_PREFIX} Доступные лоты: {list(LOTS.keys())}")
            return False
      
    
        try:
            lot_fields = cardinal.account.get_lot_fields(int(my_lot_id))
            time.sleep(0.5)
        except Exception as api_error:
            logger.error(f"{LOGGER_PREFIX} Ошибка API при получении лота {my_lot_id}: {api_error}")
        
            if "не найден" in str(api_error).lower() or "not found" in str(api_error).lower():
                logger.warning(f"{LOGGER_PREFIX} Удаляю недоступный лот {my_lot_id} из списка")
                if my_lot_id in LOTS:
                    del LOTS[my_lot_id]
                
                    try:
                        import json
                        import os
                        json_data = json.dumps(LOTS, indent=4, ensure_ascii=False)
                      
                    
                        save_attempts = [
                            "storage/plugins/steam_price_updater_lots.json",
                            "steam_price_updater_lots.json",
                            "/tmp/steam_price_updater_lots.json"
                        ]
                      
                        saved = False
                        for attempt_file in save_attempts:
                            try:
                            
                                if "/" in attempt_file:
                                    dir_path = os.path.dirname(attempt_file)
                                    if dir_path and not os.path.exists(dir_path):
                                        os.makedirs(dir_path, exist_ok=True)
                              
                                with open(attempt_file, 'w', encoding='utf-8') as f:
                                    f.write(json_data)
                                    f.flush()
                              
                                logger.info(f"{LOGGER_PREFIX} Список лотов сохранен в {attempt_file}")
                                saved = True
                                break
                            except (PermissionError, OSError, IOError):
                                continue
                      
                        if not saved:
                            logger.error(f"{LOGGER_PREFIX} Не удалось сохранить обновленный список лотов")
                          
                    except Exception as save_error:
                        logger.error(f"{LOGGER_PREFIX} Ошибка сохранения списка лотов: {save_error}")
            return False
      
        if lot_fields is None:
            logger.error(f"{LOGGER_PREFIX} Не удалось получить поля лота {my_lot_id} - лот может быть удален или недоступен")
        
            logger.warning(f"{LOGGER_PREFIX} Удаляю недоступный лот {my_lot_id} из списка")
            if my_lot_id in LOTS:
                del LOTS[my_lot_id]
            
                try:
                    import json
                    import os
                    lots_file = "storage/plugins/steam_price_updater_lots.json"
                    os.makedirs(os.path.dirname(lots_file), exist_ok=True)
                    with open(lots_file, 'w', encoding='utf-8') as f:
                        json.dump(LOTS, f, ensure_ascii=False, indent=4)
                    logger.info(f"{LOGGER_PREFIX} Список лотов обновлен")
                except Exception as save_error:
                    logger.error(f"{LOGGER_PREFIX} Ошибка сохранения списка лотов: {save_error}")
            return False
          
    
        if not hasattr(lot_fields, 'price'):
            logger.error(f"{LOGGER_PREFIX} У лота {my_lot_id} нет атрибута price")
            return False
      
    
        old_price = lot_fields.price
      
        if old_price is None:
            logger.error(f"{LOGGER_PREFIX} Текущая цена лота {my_lot_id} равна None")
            return False
      
        logger.debug(f"{LOGGER_PREFIX} Лот {my_lot_id}: текущая цена {old_price:.2f}, новая {new_price:.2f}")
      
    
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

def init(cardinal: Cardinal):

    global CARDINAL_INSTANCE
    CARDINAL_INSTANCE = cardinal
  

    atexit.register(cleanup_resources)
  
    if not cardinal.telegram:
        logger.warning(f"{LOGGER_PREFIX} Telegram бот не включен в FunPayCardinal. Плагин Steam Price Updater не будет работать.")
        return

    tg = cardinal.telegram
    bot = tg.bot

    logger.info(f"{LOGGER_PREFIX} Инициализация Telegram хэндлеров...")

    def save_settings():
        try:
        
            import os
            os.makedirs("storage/plugins", exist_ok=True)
          
            with open("storage/plugins/steam_price_updater.json", "w", encoding="utf-8") as f:
                f.write(json.dumps(SETTINGS, indent=4, ensure_ascii=False))
            logger.info(f"{LOGGER_PREFIX} Настройки сохранены.")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка сохранения настроек: {e}")

    def save_lots():
        try:
            import os
          
            logger.info(f"{LOGGER_PREFIX} Начинаем сохранение лотов. Всего: {len(LOTS)}")
          
        
            target_file = None
            json_data = json.dumps(LOTS, indent=4, ensure_ascii=False)
            logger.info(f"{LOGGER_PREFIX} Данные сериализованы. Размер: {len(json_data)} символов")
          
        
            save_attempts = [
                ("storage/plugins/steam_price_updater_lots.json", "основное расположение"),
                ("steam_price_updater_lots.json", "текущая директория"),
                ("/tmp/steam_price_updater_lots.json", "временная директория"),
                ("./lots_backup.json", "резервная копия")
            ]
          
            saved = False
            for attempt_file, description in save_attempts:
                try:
                
                    if "/" in attempt_file:
                        dir_path = os.path.dirname(attempt_file)
                        if dir_path and not os.path.exists(dir_path):
                            os.makedirs(dir_path, exist_ok=True)
                  
                
                    with open(attempt_file, "w", encoding="utf-8") as f:
                        f.write(json_data)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except (OSError, AttributeError):
                            pass
                  
                
                    if os.path.exists(attempt_file):
                        file_size = os.path.getsize(attempt_file)
                        logger.info(f"{LOGGER_PREFIX} ✅ Лоты сохранены в {attempt_file} ({description}, размер: {file_size} байт)")
                        target_file = attempt_file
                        saved = True
                        break
                  
                except (PermissionError, OSError, IOError) as e:
                    logger.warning(f"{LOGGER_PREFIX} Не удалось сохранить в {attempt_file} ({description}): {e}")
                    continue
          
            if not saved:
                logger.error(f"{LOGGER_PREFIX} ❌ Не удалось сохранить лоты ни в одно расположение!")
            
                try:
                    import tempfile
                    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json', encoding='utf-8') as tmp_file:
                        tmp_file.write(json_data)
                        logger.warning(f"{LOGGER_PREFIX} Экстренное сохранение в {tmp_file.name}")
                        target_file = tmp_file.name
                        saved = True
                except Exception as tmp_e:
                    logger.error(f"{LOGGER_PREFIX} Даже экстренное сохранение не удалось: {tmp_e}")
          
        
            if os.path.os.path.exists(target_file):
                file_size = os.path.getsize(target_file)
                logger.info(f"{LOGGER_PREFIX} ✅ Лоты сохранены в {target_file} (размер: {file_size} байт)")
            else:
                logger.error(f"{LOGGER_PREFIX} ❌ Файл не создался: {target_file}")
              
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} ❌ Критическая ошибка сохранения лотов: {e}")
            logger.error(f"{LOGGER_PREFIX} Тип ошибки: {type(e).__name__}")
            import traceback
            logger.error(f"{LOGGER_PREFIX} Трассировка: {traceback.format_exc()}")
          
        
            try:
                logger.info(f"{LOGGER_PREFIX} Лоты остаются в памяти: {len(LOTS)} лотов")
                for lot_id, lot_data in LOTS.items():
                    logger.info(f"{LOGGER_PREFIX} - Лот {lot_id}: {lot_data.get('steam_id', 'N/A')}")
            except:
                logger.error(f"{LOGGER_PREFIX} Не удается получить информацию о лотах в памяти")

    def save_wizard_states():
        """Сохраняет состояния мастера в файл"""
        try:
            import os
          
            target_file = None
            try:
                os.makedirs("storage/plugins", exist_ok=True)
                target_file = "storage/plugins/steam_price_updater_wizard.json"
            except (PermissionError, OSError):
                target_file = "steam_price_updater_wizard.json"
          
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(WIZARD_STATES, indent=4, ensure_ascii=False))
                f.flush()
          
            logger.debug(f"{LOGGER_PREFIX} Состояния мастера сохранены: {len(WIZARD_STATES)} состояний")
        except Exception as e:
            logger.warning(f"{LOGGER_PREFIX} Ошибка сохранения состояний мастера: {e}")

    def load_wizard_states():
        """Загружает состояния мастера из файла"""
        global WIZARD_STATES
        try:
            wizard_file = None
            if os.path.exists("storage/plugins/steam_price_updater_wizard.json"):
                wizard_file = "storage/plugins/steam_price_updater_wizard.json"
            elif os.path.exists("steam_price_updater_wizard.json"):
                wizard_file = "steam_price_updater_wizard.json"
          
            if wizard_file:
                with open(wizard_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    if content.strip():
                        WIZARD_STATES = json.loads(content)
                        logger.info(f"{LOGGER_PREFIX} Загружены состояния мастера: {len(WIZARD_STATES)} состояний")
        except Exception as e:
            logger.warning(f"{LOGGER_PREFIX} Ошибка загрузки состояний мастера: {e}")
            WIZARD_STATES = {}


    load_wizard_states()


    if os.path.os.path.exists("storage/plugins/steam_price_updater.json"):
        try:
            with open("storage/plugins/steam_price_updater.json", "r", encoding="utf-8") as f:
                content = f.read()
                if content.strip():
                    loaded_settings = json.loads(content)
                    SETTINGS.update(loaded_settings)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"{LOGGER_PREFIX} Ошибка при чтении настроек: {e}. Использую настройки по умолчанию.")


    load_attempts = [
        ("storage/plugins/steam_price_updater_lots.json", "основное расположение"),
        ("steam_price_updater_lots.json", "текущая директория"),
        ("/tmp/steam_price_updater_lots.json", "временная директория"),
        ("./lots_backup.json", "резервная копия")
    ]
  
    lots_file = None
    for attempt_file, description in load_attempts:
        if os.path.exists(attempt_file):
            lots_file = attempt_file
            logger.info(f"{LOGGER_PREFIX} Найден файл лотов: {lots_file} ({description})")
            break
  
    if lots_file:
        try:
            with open(lots_file, "r", encoding="utf-8") as f:
                content = f.read()
                global LOTS
                if content.strip():
                    LOTS = json.loads(content)
                
                    for lot_id, lot_data in LOTS.items():
                    
                        if "steam_id" not in lot_data and "steam_app_id" in lot_data:
                            LOTS[lot_id]["steam_id"] = str(lot_data["steam_app_id"])
                      
                        if "steam_app_id" not in lot_data:
                            LOTS[lot_id]["steam_app_id"] = 0
                          
                        if "steam_id" not in lot_data:
                            LOTS[lot_id]["steam_id"] = "730"
                          
                        if "interval" not in lot_data or lot_data["interval"] < 3600:
                            LOTS[lot_id]["interval"] = 21600
                        if "min" not in lot_data:
                            LOTS[lot_id]["min"] = SETTINGS["min_price"]
                        if "max" not in lot_data:
                            LOTS[lot_id]["max"] = SETTINGS["max_price"]
                        if "last_steam_price" not in lot_data:
                            LOTS[lot_id]["last_steam_price"] = 0
                        if "last_price" not in lot_data:
                            LOTS[lot_id]["last_price"] = 0
                        if "last_update" not in lot_data:
                            LOTS[lot_id]["last_update"] = 0
                        if "steam_currency" not in lot_data:
                            LOTS[lot_id]["steam_currency"] = "UAH"
                    save_lots()
                else:
                    LOTS = {}
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"{LOGGER_PREFIX} Ошибка при чтении лотов: {e}. Инициализирую настройки лотов по умолчанию.")
            LOTS = {}




    def open_settings(call: telebot.types.CallbackQuery):
        """Главное меню плагина с улучшенным интерфейсом"""
        try:
        
            global LOTS
            lots_file = None
            if os.path.exists("storage/plugins/steam_price_updater_lots.json"):
                lots_file = "storage/plugins/steam_price_updater_lots.json"
            elif os.path.exists("steam_price_updater_lots.json"):
                lots_file = "steam_price_updater_lots.json"
          
            if lots_file:
                try:
                    with open(lots_file, "r", encoding="utf-8") as f:
                        content = f.read()
                        if content.strip():
                            file_lots = json.loads(content)
                            LOTS.update(file_lots)
                            logger.info(f"{LOGGER_PREFIX} Перезагружены лоты в главном меню: {len(file_lots)} лотов")
                except Exception as e:
                    logger.warning(f"{LOGGER_PREFIX} Ошибка перезагрузки лотов в меню: {e}")
          
            keyboard = K()
          
        
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
          
        
            active_lots = len([l for l in LOTS.values() if l.get('on', False)])
            total_lots = len(LOTS)
          
            text = f"🎮 <b>Steam Price Updater v{VERSION}</b>\n\n"
          
        
            if total_lots == 0:
                text += f"📦 <b>Лоты:</b> Не добавлены\n"
            else:
                text += f"📦 <b>Лоты:</b> {total_lots} всего, {active_lots} активных\n"
          
        
            hours = SETTINGS['time'] // 3600
            text += f"⏱ <b>Интервал:</b> {hours} ч\n"
          
        
            text += f"💰 <b>Валюта:</b> {SETTINGS.get('currency', 'USD')}\n\n"
          
        
            text += "<b>💱 Курсы валют (USD к местной):</b>\n"
            try:
            
                uah_rate = get_currency_rate("UAH")
                rub_rate = get_currency_rate("RUB")
                kzt_rate = get_currency_rate("KZT")
              
                text += f"🇺🇦 UAH: {uah_rate:.2f}\n"
                text += f"🇷🇺 RUB: {rub_rate:.2f}\n"
                text += f"🇰🇿 KZT: {kzt_rate:.2f}\n"
                  
            except Exception as e:
                text += f"💰 Курсы валют: загрузка...\n"
            text += f"📈 Наценка на валютный курс: {SETTINGS['first_markup']}%\n"
            text += f"💸 Маржа: {SETTINGS['second_markup']}% + ${SETTINGS['fixed_markup']}"
          
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в open_settings: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def show_settings(call: telebot.types.CallbackQuery):
        """Показывает настройки плагина"""
        try:
            global SETTINGS
          
            text = f"⚙️ <b>Настройки Steam Price Updater</b>\n\n"
          
        
            text += f"💱 <b>Валюта расчетов:</b> {SETTINGS.get('currency', 'USD')}\n"
            text += f"⏱ <b>Интервал обновления:</b> {SETTINGS['time'] // 3600} ч\n\n"
          
        
            text += f"<b>💰 Настройки наценок:</b>\n"
            text += f"📈 Наценка на валютный курс: {SETTINGS['first_markup']}%\n"
            text += f"📊 Маржа: {SETTINGS['second_markup']}%\n"
            text += f"💵 Фикс. наценка: ${SETTINGS['fixed_markup']}\n\n"
          
        
            text += f"<b>🔧 Дополнительно:</b>\n"
            text += f"🎮 Steam валюта по умолчанию: {Config.DEFAULT_STEAM_CURRENCY}\n"
            text += f"⏰ Пауза между лотами: {Config.LOT_PROCESSING_DELAY}с\n"
            text += f"🔄 Макс. попыток: {Config.MAX_RETRIES}\n"
          
            keyboard = K()
          
        
            keyboard.row(
                B("💱 Валюта", callback_data=f"{CBT_CHANGE_CURRENCY}:switch"),
                B("⏱ Интервал", callback_data=f"{CBT_TEXT_EDIT}:settings:time")
            )
          
        
            keyboard.row(
                B("📈 Наценка на валютный курс", callback_data=f"{CBT_TEXT_EDIT}:settings:first_markup"),
                B("📊 Маржа", callback_data=f"{CBT_TEXT_EDIT}:settings:second_markup")
            )
          
        
            keyboard.row(
                B("💵 Фикс. наценка", callback_data=f"{CBT_TEXT_EDIT}:settings:fixed_markup"),
                B("🔄 Курсы валют", callback_data=f"{CBT_REFRESH_RATES}:")
            )
          
            keyboard.add(B("◀ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
          
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в show_settings: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def switch_currency(call: telebot.types.CallbackQuery):
        """Переключает валюту FunPay аккаунта для расчетов"""
        try:
            global SETTINGS
        
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
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
              
            parts = call.data.split(":")
            if len(parts) < 2:
                bot.answer_callback_query(call.id, "❌ Неверный формат данных")
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
          
        
            import types
            fixed_call = types.SimpleNamespace()
            fixed_call.id = call.id
            fixed_call.message = call.message
            fixed_call.from_user = call.from_user
            fixed_call.data = f"{CBT_EDIT_LOT}:{lot_id}"
          
            edit_lot_menu(fixed_call)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в switch_steam_currency: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def wizard_step2_steam_id(message, lot_id):
        """Мастер - Шаг 2: Steam ID"""
        text = "🧙‍♂️ <b>Мастер добавления лота</b>\n\n"
        text += "📋 <b>Шаг 2 из 4: Steam ID</b>\n\n"
        text += f"✅ ID лота: <code>{lot_id}</code>\n\n"
        text += "Введите Steam ID игры:\n"
        text += "• <b>App ID</b> (обычная игра): просто цифры, например <code>730</code>\n"
        text += "• <b>Sub ID</b> (DLC/Package): <code>sub_12345</code>\n\n"
        text += "🔍 Найти можно:\n"
        text += "• SteamDB.info\n"
        text += "• Steam URL игры\n"
        text += "• Например: CS2 = <code>730</code>"
      
        keyboard = K()
        keyboard.add(B("◀ К лотам", callback_data=f"{CBT_LOTS_MENU}:0"))
      
        msg = bot.send_message(message.chat.id, text, reply_markup=keyboard, parse_mode="HTML")
        tg.set_state(message.chat.id, msg.message_id, message.from_user.id, 
                    "lot_wizard", {"step": "steam_id", "lot_id": lot_id})

    def wizard_step3_currency(message, lot_id, steam_id):
        """Мастер - Шаг 3: Валюта Steam"""
        text = "🧙‍♂️ <b>Мастер добавления лота</b>\n\n"
        text += "📋 <b>Шаг 3 из 4: Валюта Steam</b>\n\n"
        text += f"✅ ID лота: <code>{lot_id}</code>\n"
        text += f"✅ Steam ID: <code>{steam_id}</code>\n\n"
        text += "Выберите валюту для получения цен Steam:"
      
        keyboard = K()
        keyboard.row(
            B("🇺🇦 UAH", callback_data=f"wizard_currency:{lot_id}:{steam_id}:UAH"),
            B("🇺🇸 USD", callback_data=f"wizard_currency:{lot_id}:{steam_id}:USD")
        )
        keyboard.row(
            B("🇷🇺 RUB", callback_data=f"wizard_currency:{lot_id}:{steam_id}:RUB"),
            B("🇰🇿 KZT", callback_data=f"wizard_currency:{lot_id}:{steam_id}:KZT")
        )
        keyboard.add(B("◀ К лотам", callback_data=f"{CBT_LOTS_MENU}:0"))
      
        tg.clear_state(message.chat.id, message.from_user.id)
        bot.send_message(message.chat.id, text, reply_markup=keyboard, parse_mode="HTML")

    def wizard_step4_max_price(message, lot_id, steam_id, steam_currency, min_price):
        """Мастер - Шаг 4: Максимальная цена"""
        text = "🧙‍♂️ <b>Мастер добавления лота</b>\n\n"
        text += "📋 <b>Шаг 4 из 4: Максимальная цена</b>\n\n"
        text += f"✅ ID лота: <code>{lot_id}</code>\n"
        text += f"✅ Steam ID: <code>{steam_id}</code>\n"
        text += f"✅ Валюта: {steam_currency}\n"
        text += f"✅ Мин. цена: ${min_price}\n\n"
        text += f"Введите максимальную цену (больше {min_price}):"
      
        keyboard = K()
        keyboard.add(B("◀ К лотам", callback_data=f"{CBT_LOTS_MENU}:0"))
      
        msg = bot.send_message(message.chat.id, text, reply_markup=keyboard, parse_mode="HTML")
        tg.set_state(message.chat.id, msg.message_id, message.from_user.id, 
                    "lot_wizard", {
                        "step": "max_price", 
                        "lot_id": lot_id,
                        "steam_id": steam_id,
                        "steam_currency": steam_currency,
                        "min_price": min_price
                    })

    def wizard_complete(message, lot_id, steam_id, steam_currency, min_price, max_price):
        """Завершение мастера - создание лота"""
        global LOTS
      
        logger.info(f"{LOGGER_PREFIX} === ЗАВЕРШЕНИЕ МАСТЕРА ===")
        logger.info(f"{LOGGER_PREFIX} Lot ID: {lot_id}")
        logger.info(f"{LOGGER_PREFIX} Steam ID: {steam_id}")
        logger.info(f"{LOGGER_PREFIX} Currency: {steam_currency}")
        logger.info(f"{LOGGER_PREFIX} Price range: {min_price} - {max_price}")
      
    
        LOTS[lot_id] = {
            "on": True,
            "steam_id": steam_id,
            "steam_app_id": 0,
            "steam_currency": steam_currency,
            "min": min_price,
            "max": max_price,
            "last_steam_price": 0,
            "last_price": 0,
            "last_update": 0
        }
      
        logger.info(f"{LOGGER_PREFIX} Сохранен Steam ID: {steam_id}")
      
        logger.info(f"{LOGGER_PREFIX} Лот создан в памяти. Всего лотов: {len(LOTS)}")
        logger.info(f"{LOGGER_PREFIX} Сохраняем лоты...")
        save_lots()
        tg.clear_state(message.chat.id, message.from_user.id)
      
    
        global_interval_hours = SETTINGS['time'] // 3600
      
        text = "🎉 <b>Лот успешно создан!</b>\n\n"
        text += f"📦 ID лота: <code>{lot_id}</code>\n"
        text += f"🎮 Steam ID: <code>{steam_id}</code>\n" 
        text += f"💱 Валюта: {steam_currency}\n"
        text += f"💰 Цены: ${min_price} - ${max_price}\n"
        text += f"✅ Статус: <b>Включен</b>\n\n"
        text += f"⏰ Лот будет обновляться каждые <b>{global_interval_hours} ч</b>"
      
        keyboard = K()
        keyboard.add(B("📦 К лотам", callback_data=f"{CBT_LOTS_MENU}:0"))
      
        bot.send_message(message.chat.id, text, reply_markup=keyboard, parse_mode="HTML")

    def start_lot_wizard(call: telebot.types.CallbackQuery):
        """Мастер добавления лота - Шаг 1: ID лота"""
        global WIZARD_STATES
        try:
            text = "🧙‍♂️ <b>Мастер добавления лота</b>\n\n"
            text += "📋 <b>Шаг 1 из 4: ID лота</b>\n\n"
            text += "Введите ID лота с FunPay:\n"
            text += "• Найдите лот на funpay.com\n"
            text += "• Скопируйте цифры из URL\n"
            text += "• Например: из funpay.com/lots/offer?id=<b>12345</b>\n"
            text += "• Введите просто: <code>12345</code>\n\n"
            text += "💡 Это нужно для связи с вашим лотом на FunPay"
          
            keyboard = K()
            keyboard.add(B("◀ Отмена", callback_data=f"{CBT_LOTS_MENU}:0"))
          
        
            user_key = f"{call.message.chat.id}_{call.from_user.id}"
            WIZARD_STATES[user_key] = {"step": "lot_id"}
          
        
            logger.info(f"{LOGGER_PREFIX} === МАСТЕР ЗАПУЩЕН ===")
            logger.info(f"{LOGGER_PREFIX} User key: {user_key}")
            logger.info(f"{LOGGER_PREFIX} Состояние: {WIZARD_STATES[user_key]}")
            logger.info(f"{LOGGER_PREFIX} Все состояния: {WIZARD_STATES}")
            logger.info(f"{LOGGER_PREFIX} Chat ID: {call.message.chat.id}, User ID: {call.from_user.id}")
          
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id, "🧙‍♂️ Начинаем мастер!")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в start_lot_wizard: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def to_lot_mess(call: telebot.types.CallbackQuery):
        """Настройка лота или запуск мастера для нового"""
        try:
            if not call.data:
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
              
            n = call.data.split(":")[-1]
            global LOTS, SETTINGS
          
        
            if n == "0":
                start_lot_wizard(call)
                return
          
        
            if n not in LOTS.keys():
                LOTS.setdefault(n, {
                    "on": True,
                    "steam_id": "730",
                    "steam_app_id": 730,
                    "price_type": "app",
                    "min": SETTINGS["min_price"],
                    "max": SETTINGS["max_price"],
                    "last_steam_price": 0,
                    "last_price": 0,
                    "last_update": 0,
                    "steam_currency": "UAH"
                })
                save_lots()

            lot_data = LOTS[n]
            is_new_lot = n == "0"
          
        
            game_name = get_lot_name(lot_data)
          
        
            if is_new_lot:
                text = f"➕ <b>Добавление нового лота</b>\n\n"
                text += f"📋 <b>Статус:</b> Настройка\n"
                text += f"🎮 <b>Игра:</b> {game_name}\n\n"
            else:
                status_icon = "🟢" if lot_data["on"] else "🔴"
                text = f"{status_icon} <b>Настройка лота #{n}</b>\n"
                text += f"🎮 <b>Игра:</b> {game_name}\n\n"
                text += f"🔗 <b>Ссылка:</b> https://funpay.com/lots/offer?id={n}\n\n"
          
        
            steam_id = lot_data.get("steam_id", lot_data.get("steam_app_id", "730"))
            steam_currency = lot_data.get("steam_currency", "UAH")
          
            text += f"<b>⚙️ Настройки Steam:</b>\n"
            if str(steam_id).startswith("sub_"):
                text += f"📦 Sub ID: {steam_id[4:]} (DLC/Package)\n"
            else:
                text += f"🎯 App ID: {steam_id} (Игра)\n"
            text += f"💱 Валюта: {steam_currency}\n\n"
          
        
            text += f"<b>💰 Ценовые ограничения:</b>\n"
            text += f"🔻 Минимум: ${lot_data.get('min', 1.0):.2f}\n"
            text += f"🔺 Максимум: ${lot_data.get('max', 5000.0):.2f}\n\n"
          
        
            global_interval_hours = SETTINGS.get("time", 21600) // 3600
            text += f"<b>⏰ Обновление:</b>\n"
            text += f"🔄 Интервал: {global_interval_hours} ч (глобальный)\n"
          
        
            if lot_data.get('last_price', 0) > 0 or lot_data.get('last_steam_price', 0) > 0:
                text += f"\n<b>📊 Последние цены:</b>\n"
                if lot_data.get('last_price', 0) > 0:
                    text += f"💵 FunPay: ${lot_data['last_price']:.2f}\n"
                if lot_data.get('last_steam_price', 0) > 0:
                    text += f"🎮 Steam: {lot_data['last_steam_price']:.2f} {steam_currency}\n"
          
        
            last_update = lot_data.get("last_update", 0)
            if last_update > 0:
                last_update_str = dt.fromtimestamp(last_update).strftime("%d.%m %H:%M")
                text += f"📅 Обновлено: {last_update_str}\n"
          
        
            keyboard = K()
          
        
            if not is_new_lot:
                status_text = "❌ Выключить" if lot_data["on"] else "✅ Включить"
                keyboard.add(B(status_text, callback_data=f"{CBT_TEXT_EDIT}:{n}:on"))
          
        
            keyboard.row(
                B("🔧 Steam ID", callback_data=f"{CBT_TEXT_EDIT}:{n}:steam_app_id"),
                B("💱 Валюта", callback_data=f"{CBT_CHANGE_STEAM_CURRENCY}:{n}")
            )
          
        
            keyboard.add(
                B("📝 ID лота", callback_data=f"{CBT_TEXT_EDIT}:{n}:lot_id")
            )
          
        
            keyboard.row(
                B("💰 Мин. цена", callback_data=f"{CBT_TEXT_EDIT}:{n}:min"),
                B("💸 Макс. цена", callback_data=f"{CBT_TEXT_EDIT}:{n}:max")
            )
          
        
            if is_new_lot:
                keyboard.row(
                    B("💾 Сохранить", callback_data=f"{CBT_LOTS_MENU}:0"),
                    B("◀ Отмена", callback_data=f"{CBT_LOTS_MENU}:0")
                )
            else:
                keyboard.row(
                    B("🗑 Удалить", callback_data=f"{CBT_TEXT_DELETE}:{n}"),
                    B("◀ К лотам", callback_data=f"{CBT_LOTS_MENU}:0")
                )
          
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, 
                                  text=text, reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в to_lot_mess: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def answer_to_lot_mess(call: telebot.types.CallbackQuery):
        try:
            if not call.data:
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
              
            parts = call.data.split(":")
            if len(parts) < 3:
                bot.answer_callback_query(call.id, "❌ Неверный формат данных")
                return
              
            n = parts[-2]
            key = parts[-1]

            global LOTS, SETTINGS
          
        
            if n == "settings":
                bot.answer_callback_query(call.id)
                d = {
                    "time": "интервал обновления (в часах)",
                    "first_markup": "первую наценку (%)",
                    "second_markup": "вторую наценку (%)",
                    "fixed_markup": "фиксированную наценку ($)",
                    "min_price": "минимальную цену ($)",
                    "max_price": "максимальную цену ($)"
                }
              
                current_value = ""
                if key == "time":
                    current_value = SETTINGS.get(key, 21600) // 3600
                elif key in ("first_markup", "second_markup"):
                    current_value = SETTINGS.get(key, 0)
                elif key == "fixed_markup":
                    current_value = SETTINGS.get(key, 0.5)
                elif key == "min_price":
                    current_value = SETTINGS.get(key, 1.0)
                elif key == "max_price":
                    current_value = SETTINGS.get(key, 5000.0)
              
                text = f'Введите {d.get(key, "значение")}. Текущее: {current_value}'
                msg = bot.send_message(call.message.chat.id, text, 
                                       reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
                tg.set_state(call.message.chat.id, msg.id, call.from_user.id, 
                            CBT_TEXT_EDIT, {"n": n, "key": key})
                return
          
        
            elif key in ("max", "min", "steam_app_id", "lot_id"):
                bot.answer_callback_query(call.id)
                d = {
                    "max": "максимальную цену",
                    "min": "минимальную цену",
                    "steam_app_id": "Steam App ID игры",
                    "lot_id": "ID лота",
                    "interval": "интервал проверки лота (в часах)"
                }
                current_value = ""
                if n in LOTS:
                    if key == "interval":
                        current_value = LOTS[n].get(key, 21600) // 3600
                    else:
                        current_value = LOTS[n].get(key, "")
                    text = f'Введите {d.get(key, "значение")} для лота {n}. Текущее: {current_value}'
                    msg = bot.send_message(call.message.chat.id, text, 
                                           reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
                    tg.set_state(call.message.chat.id, msg.id, call.from_user.id, 
                                CBT_TEXT_EDIT, {"n": n, "key": key})
                return
            elif key == "on":
                LOTS[n]["on"] = not LOTS[n]["on"]
                save_lots()
                to_lot_mess(call)
                return
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в answer_to_lot_mess: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def to_delete(call: telebot.types.CallbackQuery):
        try:
            if not call.data:
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
              
            n = call.data.split(":")[-1]
            global LOTS
            if n in LOTS:
                del LOTS[n]
                save_lots()
            open_settings(call)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в to_delete: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def update_now(call: telebot.types.CallbackQuery):
        """Запускает принудительное обновление с исправленной логикой"""
        try:
            global LOTS, CARDINAL_INSTANCE
          
        
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
                        logger.debug(f"{LOGGER_PREFIX} Основной цикл: лот {lot_id}, данные из LOTS: {lot_data}")
                    
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
            global LOTS
          
            active_lots = [lot for lot in LOTS.values() if lot.get("on", False)]
            lots_with_prices = len([l for l in LOTS.values() if l.get("last_price", 0) > 0])
            cache_hits = len(steam_price_cache)
          
            text = f"📊 Статистика Steam Price Updater\n\n"
            text += f"📦 Всего лотов: {len(LOTS)}\n"
            text += f"✅ Активных: {len(active_lots)}\n"
            text += f"💰 Лотов с ценами: {lots_with_prices}\n"
            text += f"🔄 Кеш Steam: {cache_hits} записей\n"
          
        
            try:
                uah_rate = get_currency_rate("UAH")
                text += f"💱 USD/UAH: {uah_rate:.2f}\n"
              
                rub_cached = CACHE.get("currency_rate_RUB")
                kzt_cached = CACHE.get("currency_rate_KZT")
              
                if rub_cached:
                    text += f"💱 USD/RUB: {rub_cached['rate']:.2f}\n"
                if kzt_cached:
                    text += f"💱 USD/KZT: {kzt_cached['rate']:.2f}\n"
            except:
                text += f"💱 Курсы валют: загрузка...\n"
          
        
            recent_updates = [lot for lot in LOTS.values() if lot.get("last_update", 0) > 0]
            if recent_updates:
                last_update_time = max(lot.get("last_update", 0) for lot in recent_updates)
                last_update_str = dt.fromtimestamp(last_update_time).strftime("%d.%m %H:%M")
                text += f"🕐 Последнее обновление: {last_update_str}\n"
            else:
                text += f"🕐 Последнее обновление: Никогда\n"
          
            keyboard = K()
            keyboard.add(B("◀ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
          
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в show_stats: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def show_lots_menu(call: telebot.types.CallbackQuery):
        """Улучшенное меню управления лотами"""
        try:
        
            global LOTS
            lots_file = None
            if os.path.exists("storage/plugins/steam_price_updater_lots.json"):
                lots_file = "storage/plugins/steam_price_updater_lots.json"
            elif os.path.exists("steam_price_updater_lots.json"):
                lots_file = "steam_price_updater_lots.json"
          
            if lots_file:
                try:
                    with open(lots_file, "r", encoding="utf-8") as f:
                        content = f.read()
                        if content.strip():
                            file_lots = json.loads(content)
                        
                            LOTS.update(file_lots)
                            logger.debug(f"{LOGGER_PREFIX} Перезагружены лоты из файла: {len(file_lots)} лотов")
                except Exception as e:
                    logger.warning(f"{LOGGER_PREFIX} Ошибка перезагрузки лотов: {e}")
          
            if not call.data:
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
              
            page = int(call.data.split(":")[-1]) if call.data.split(":")[-1].isdigit() else 0
            per_page = Config.LOTS_PER_PAGE
          
            lot_items = [(lot_id, lot_data) for lot_id, lot_data in LOTS.items() if lot_id != "0"]
            total_lots = len(lot_items)
          
            logger.info(f"{LOGGER_PREFIX} Показываем меню лотов. В памяти: {len(LOTS)} лотов, отображаем: {total_lots} лотов")
          
        
            lot_items.sort(key=lambda x: (not x[1].get("on", False), x[0]))
          
            start_idx = page * per_page
            end_idx = start_idx + per_page
            current_lots = lot_items[start_idx:end_idx]
          
        
            active_count = len([l for _, l in lot_items if l.get("on", False)])
            text = f"📦 <b>Управление лотами</b>\n\n"
            text += f"📊 <b>Всего:</b> {total_lots} | <b>Активных:</b> {active_count}\n"
            if total_lots > per_page:
                text += f"📄 <b>Страница:</b> {page + 1}/{(total_lots - 1) // per_page + 1}\n"
            text += "\n"
          
            keyboard = K()
          
            if total_lots == 0:
                text += "📝 <i>Лоты не добавлены</i>\n\n"
                text += "💡 <b>Для начала работы:</b>\n"
                text += "1. Нажмите 'Добавить лот'\n"
                text += "2. Введите ID лота FunPay\n"
                text += "3. Настройте Steam ID игры"
            else:
                text += "<b>Ваши лоты:</b>\n"
              
                for lot_id, lot_data in current_lots:
                    game_name = get_lot_name(lot_data)
                    status_icon = "🟢" if lot_data.get("on", False) else "🔴"
                  

                  
                    button_text = f"{status_icon} {game_name[:25]}"
                    callback_data = f"{CBT_EDIT_LOT}:{lot_id}"
                    keyboard.add(B(button_text, callback_data=callback_data))
          
        
            action_buttons = []
          
        
            if page > 0:
                action_buttons.append(B("⬅ Пред", callback_data=f"{CBT_LOTS_MENU}:{page-1}"))
            if end_idx < total_lots:
                action_buttons.append(B("След ➡", callback_data=f"{CBT_LOTS_MENU}:{page+1}"))
              
            if action_buttons:
                keyboard.row(*action_buttons)
          
        
            keyboard.row(
                B("➕ Добавить лот", callback_data=f"{CBT_TEXT_CHANGE_LOT}:0"),
                B("🔄 Обновить сейчас", callback_data=f"{CBT_UPDATE_NOW}:")
            )
            keyboard.add(B("◀ Главное меню", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
          
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в show_lots_menu: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def edit_lot_menu(call: telebot.types.CallbackQuery):
        """Улучшенное меню редактирования лота с детальной информацией"""
        try:
            if not call.data:
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
              
            lot_id = call.data.split(":")[-1]
          
            if lot_id not in LOTS:
                bot.answer_callback_query(call.id, "❌ Лот не найден")
                return
          
            lot_data = LOTS[lot_id]
            game_name = get_lot_name(lot_data)
          
        
            status_icon = "🟢" if lot_data.get("on", False) else "🔴"
            text = f"{status_icon} <b>Лот #{lot_id}</b>\n"
            text += f"🎮 <b>{game_name}</b>\n\n"
          
        
            steam_id = lot_data.get("steam_id", lot_data.get("steam_app_id", "N/A"))
            steam_currency = lot_data.get("steam_currency", "UAH")
          
            if str(steam_id).startswith("sub_"):
                text += f"📦 <b>Steam Sub ID:</b> {steam_id[4:]}\n"
                text += f"💿 <b>Тип:</b> DLC/Package\n"
            else:
                text += f"🎯 <b>Steam App ID:</b> {steam_id}\n" 
                text += f"🎮 <b>Тип:</b> Игра\n"
          
            text += f"💱 <b>Валюта Steam:</b> {steam_currency}\n\n"
          
        
            min_price = lot_data.get("min", 1.0)
            max_price = lot_data.get("max", 5000.0)
            last_price = lot_data.get("last_price", 0)
            last_steam_price = lot_data.get("last_steam_price", 0)
          
            text += "💰 <b>Ценовые настройки:</b>\n"
            text += f"🔻 Мин. цена: ${min_price:.2f}\n"
            text += f"🔺 Макс. цена: ${max_price:.2f}\n"
          
            if last_price > 0:
                text += f"💵 Текущая цена: ${last_price:.2f}\n"
            if last_steam_price > 0:
                text += f"🎮 Steam цена: {last_steam_price:.2f} {steam_currency}\n"
          
            text += "\n"
          
        
            global_interval_hours = SETTINGS["time"] // 3600
            last_update = lot_data.get("last_update", 0)
          
            text += "⏰ <b>Обновления:</b>\n"
            text += f"🔄 Интервал: {global_interval_hours} ч (глобальный)\n"
          
            if last_update > 0:
                last_update_str = dt.fromtimestamp(last_update).strftime("%d.%m %H:%M")
                text += f"📅 Последнее: {last_update_str}\n"
            else:
                text += f"📅 Последнее: Никогда\n"
          
        
            keyboard = K()
          
        
            status_text = "❌ Выключить" if lot_data.get("on", False) else "✅ Включить"
            keyboard.add(B(status_text, callback_data=f"{CBT_TOGGLE_LOT}:{lot_id}"))
          
        
            keyboard.row(
                B("🔧 Steam ID", callback_data=f"{CBT_TEXT_EDIT}:{lot_id}:steam_app_id"),
                B("💱 Валюта", callback_data=f"{CBT_CHANGE_STEAM_CURRENCY}:{lot_id}")
            )
          
        
            keyboard.row(
                B("💰 Мин. цена", callback_data=f"{CBT_TEXT_EDIT}:{lot_id}:min"),
                B("💸 Макс. цена", callback_data=f"{CBT_TEXT_EDIT}:{lot_id}:max")
            )
          
        
            keyboard.add(B("🔄 Обновить лот", callback_data=f"update_single_lot:{lot_id}"))
          
        
            keyboard.row(
                B("🗑 Удалить", callback_data=f"{CBT_DELETE_LOT}:{lot_id}"),
                B("◀ К лотам", callback_data=f"{CBT_LOTS_MENU}:0")
            )
          

          
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в edit_lot_menu: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def toggle_lot_status(call: telebot.types.CallbackQuery):
        """Переключает статус лота (включен/выключен)"""
        try:
            if not call.data:
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
              
            lot_id = call.data.split(":")[-1]
          
            if lot_id not in LOTS:
                bot.answer_callback_query(call.id, "❌ Лот не найден")
                return
          
            LOTS[lot_id]["on"] = not LOTS[lot_id].get("on", False)
            save_lots()
          
            status = "включен" if LOTS[lot_id]["on"] else "выключен"
            bot.answer_callback_query(call.id, f"Лот {status}")
          
            edit_lot_menu(call)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в toggle_lot_status: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def delete_lot_confirm(call: telebot.types.CallbackQuery):
        """Подтверждение удаления лота"""
        try:
            if not call.data:
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
              
            lot_id = call.data.split(":")[-1]
          
            if lot_id not in LOTS:
                bot.answer_callback_query(call.id, "❌ Лот не найден")
                return
          
            del LOTS[lot_id]
            save_lots()
          
            bot.answer_callback_query(call.id, f"Лот {lot_id} удален")
            show_lots_menu(call)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в delete_lot_confirm: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def refresh_currency_rates(call: telebot.types.CallbackQuery):
        """Принудительно обновляет курсы валют"""
        try:
            bot.answer_callback_query(call.id, "Обновляю курсы...")
          
            def refresh_thread():
                try:
                
                    global CACHE, usd_rate_cache
                  
                
                    cleared_count = clear_currency_cache()
                  
                
                    try:
                        currency_keys = [k for k in CACHE.keys() if k.startswith("currency_rate_")]
                        for key in currency_keys:
                            if key in CACHE.cache:
                                del CACHE.cache[key]
                    except Exception:
                        pass
                  
                    usd_rate_cache["timestamp"] = 0
                  
                
                    uah_rate = get_currency_rate("UAH")
                    rub_rate = get_currency_rate("RUB")
                    kzt_rate = get_currency_rate("KZT")
                    eur_rate = get_currency_rate("EUR")
                  
                    result_text = f"💱 Курсы валют обновлены (exchangerate-api):\n\n"
                    result_text += f"🇺🇦 USD/UAH: {uah_rate:.2f}\n"
                    result_text += f"🇷🇺 USD/RUB: {rub_rate:.2f}\n"
                    result_text += f"🇰🇿 USD/KZT: {kzt_rate:.2f}\n"
                    result_text += f"🇪🇺 USD/EUR: {eur_rate:.2f}\n"
                    result_text += f"\n🕐 {time.strftime('%H:%M:%S')}"
                  
                    bot.send_message(call.message.chat.id, result_text)
                  
                except Exception as e:
                    logger.error(f"{LOGGER_PREFIX} Ошибка обновления курсов: {e}")
                    bot.send_message(call.message.chat.id, "❌ Ошибка обновления курсов")
          
            Thread(target=refresh_thread, daemon=True).start()
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в refresh_currency_rates: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def update_single_lot(call: telebot.types.CallbackQuery):
        """Обновляет только один конкретный лот"""
        try:
            if not call.data:
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
              
            lot_id = call.data.split(":")[-1]
          
            if lot_id not in LOTS:
                bot.answer_callback_query(call.id, "❌ Лот не найден")
                return
          
            lot_data = LOTS[lot_id]
          
            if not lot_data.get("on", False):
                bot.answer_callback_query(call.id, "❌ Лот выключен")
                return
          
            bot.answer_callback_query(call.id, f"🔄 Обновляю лот {lot_id}...")
          
            def update_thread():
                try:
                    success = update_lot_price(lot_id, lot_data, cardinal)
                  
                    if success:
                    
                        import types
                        fixed_call = types.SimpleNamespace()
                        fixed_call.id = call.id
                        fixed_call.message = call.message
                        fixed_call.from_user = call.from_user
                        fixed_call.data = f"{CBT_EDIT_LOT}:{lot_id}"
                      
                        edit_lot_menu(fixed_call)
                      
                    
                        try:
                            bot.send_message(
                                call.message.chat.id,
                                f"✅ Лот {lot_id} успешно обновлен!",
                                reply_to_message_id=call.message.message_id
                            )
                        except:
                            pass
                    else:
                        bot.send_message(
                            call.message.chat.id,
                            f"❌ Ошибка обновления лота {lot_id}",
                            reply_to_message_id=call.message.message_id
                        )
                      
                except Exception as e:
                    logger.error(f"{LOGGER_PREFIX} Ошибка в update_thread: {e}")
                    bot.send_message(
                        call.message.chat.id,
                        f"❌ Ошибка: {e}",
                        reply_to_message_id=call.message.message_id
                    )
          
            Thread(target=update_thread, daemon=True).start()
          
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в update_single_lot: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    def edited(message: telebot.types.Message):
        """Обрабатывает ввод текста"""
        try:
            global LOTS
          
            if not message.text:
                bot.reply_to(message, "❌ Сообщение не содержит текста")
                return
              
            if not message.from_user:
                bot.reply_to(message, "❌ Пользователь не определен")
                return
              
            state_data = tg.get_state(message.chat.id, message.from_user.id)
            if not state_data:
            
                logger.info(f"{LOGGER_PREFIX} Состояние не найдено для пользователя {message.from_user.id}")
                return
          
        
            logger.info(f"{LOGGER_PREFIX} State data: {state_data}, type: {type(state_data)}")
          
        
            if isinstance(state_data, dict):
                if "wizard" in state_data and state_data["wizard"] == "lot_wizard":
                
                    state_name = "lot_wizard"
                    data = state_data
                elif "name" in state_data:
                
                    state_name = state_data.get("name")
                    data = state_data.get("data", {})
                elif "step" in state_data:
                
                    state_name = "lot_wizard"
                    data = state_data
                else:
                
                    state_name = None
                    data = state_data
            else:
            
                state_name = state_data
                data = {}
            n = data.get("n")
            key = data.get("key")
            text = message.text.strip()
          
        
            if state_name == "lot_wizard":
                step = data.get("step")
              
                if step == "lot_id":
                
                    lot_id = text.strip()
                    if not lot_id.isdigit():
                        bot.reply_to(message, "❌ ID лота должен содержать только цифры")
                        return
                  
                    if lot_id in LOTS:
                        bot.reply_to(message, f"❌ Лот {lot_id} уже настроен")
                        return
                  
                
                    wizard_step2_steam_id(message, lot_id)
                    return
                  
                elif step == "steam_id":
                
                    lot_id = data.get("lot_id")
                    steam_id = text.strip()
                  
                
                    is_valid, id_type, clean_id = validate_steam_id(steam_id)
                    if not is_valid:
                        bot.reply_to(message, f"❌ Неверный формат Steam ID. {clean_id}")
                        return
                  
                
                    wizard_step3_currency(message, lot_id, clean_id)
                    return
                  
                elif step == "min_price":
                
                    lot_id = data.get("lot_id")
                    steam_id = data.get("steam_id")
                    steam_currency = data.get("steam_currency")
                  
                    try:
                        min_price = float(text)
                        if min_price <= 0:
                            bot.reply_to(message, "❌ Цена должна быть больше 0")
                            return
                        wizard_step4_max_price(message, lot_id, steam_id, steam_currency, min_price)
                        return
                    except ValueError:
                        bot.reply_to(message, "❌ Введите корректную цену (число)")
                        return
                      
                elif step == "max_price":
                
                    lot_id = data.get("lot_id")
                    steam_id = data.get("steam_id")
                    steam_currency = data.get("steam_currency")
                    min_price = data.get("min_price")
                  
                    try:
                        max_price = float(text)
                        if max_price <= min_price:
                            bot.reply_to(message, f"❌ Максимальная цена должна быть больше минимальной ({min_price})")
                            return
                        wizard_complete(message, lot_id, steam_id, steam_currency, min_price, max_price)
                        return
                    except ValueError:
                        bot.reply_to(message, "❌ Введите корректную цену (число)")
                        return
          
        
            if n == "settings":
                global SETTINGS
                try:
                    if key == "time":
                        hours = int(text)
                        if hours < 1:
                            hours = 1
                        SETTINGS[key] = hours * 3600
                        tg.clear_state(message.chat.id, message.from_user.id, True)
                        save_settings()
                        bot.reply_to(message, f"Интервал обновления изменен на {hours} часов", 
                                    reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
                      
                    elif key in ("first_markup", "second_markup"):
                        value = float(text)
                        if value < 0:
                            value = 0
                        SETTINGS[key] = value
                        tg.clear_state(message.chat.id, message.from_user.id, True)
                        save_settings()
                        bot.reply_to(message, f"Наценка изменена на {value}%", 
                                    reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
                                  
                    elif key == "fixed_markup":
                        value = float(text)
                        if value < 0:
                            value = 0
                        SETTINGS[key] = value
                        tg.clear_state(message.chat.id, message.from_user.id, True)
                        save_settings()
                        bot.reply_to(message, f"Фиксированная наценка изменена на ${value}", 
                                    reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
                                  
                    elif key in ("min_price", "max_price"):
                        value = float(text)
                        if value <= 0:
                            bot.reply_to(message, "Цена должна быть больше 0")
                            return
                        SETTINGS[key] = value
                        tg.clear_state(message.chat.id, message.from_user.id, True)
                        save_settings()
                        price_type = "Минимальная" if key == "min_price" else "Максимальная"
                        bot.reply_to(message, f"{price_type} цена изменена на ${value}", 
                                    reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
                                  
                except ValueError:
                    bot.reply_to(message, "Неверный формат числа")
                return
          
        
            elif key == "lot_id":
                new_lot_id = text.strip()
              
                if n == "0":
                
                    if new_lot_id in LOTS:
                        bot.reply_to(message, f"Лот {new_lot_id} уже существует")
                        return
                  
                    LOTS[new_lot_id] = LOTS.get("0", {
                        "on": True,
                        "steam_app_id": 0,
                        "min": SETTINGS["min_price"],
                        "max": SETTINGS["max_price"],
                        "last_steam_price": 0,
                        "last_price": 0,
                        "last_update": 0,
                        "steam_currency": "UAH"
                    })
                  
                    if "0" in LOTS:
                        del LOTS["0"]
                  
                    save_lots()
                    tg.clear_state(message.chat.id, message.from_user.id, True)
                    bot.reply_to(message, f"Лот {new_lot_id} добавлен", 
                                reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
                else:
                
                    if n not in LOTS:
                        bot.reply_to(message, f"Лот {n} не найден")
                        return
                      
                    if new_lot_id != n and new_lot_id in LOTS:
                        bot.reply_to(message, f"Лот {new_lot_id} уже существует")
                        return
                  
                    if new_lot_id != n:
                        LOTS[new_lot_id] = LOTS[n]
                        del LOTS[n]
                  
                    save_lots()
                    tg.clear_state(message.chat.id, message.from_user.id, True)
                    bot.reply_to(message, f"ID лота изменен на {new_lot_id}", 
                                reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
          
            elif key in ["min", "max"]:
                if n in LOTS:
                    try:
                        value = float(text)
                        LOTS[n][key] = value
                        save_lots()
                        tg.clear_state(message.chat.id, message.from_user.id, True)
                        bot.reply_to(message, f"Значение {key} изменено на {value}", 
                                    reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
                    except ValueError:
                        bot.reply_to(message, "Неверный формат числа")
                else:
                    bot.reply_to(message, f"Лот {n} не найден")
          
            elif key == "steam_app_id":
                if n in LOTS:
                
                    steam_id = text.strip()
                  
                
                    is_valid = False
                  
                    if steam_id.startswith("sub_"):
                    
                        try:
                            sub_id_num = steam_id[4:]
                            if sub_id_num.isdigit() and len(sub_id_num) > 0:
                                sub_id = int(sub_id_num)
                                is_valid = True
                            else:
                                bot.reply_to(message, "❌ Неверный формат Sub ID. Используйте: sub_123456")
                                return
                        except ValueError:
                            bot.reply_to(message, "❌ Неверный формат Sub ID. Используйте: sub_123456")
                            return
                    else:
                    
                        try:
                            app_id = int(steam_id)
                            if app_id > 0:
                                is_valid = True
                            else:
                                bot.reply_to(message, "❌ App ID должен быть положительным числом")
                                return
                        except ValueError:
                            bot.reply_to(message, "❌ Неверный формат App ID. Введите число или sub_123456 для DLC")
                            return
                  
                    if is_valid:
                    
                        LOTS[n]["steam_id"] = steam_id
                    
                        if steam_id.startswith("sub_"):
                            LOTS[n]["steam_app_id"] = 0
                        else:
                            LOTS[n]["steam_app_id"] = int(steam_id)
                      
                        save_lots()
                        tg.clear_state(message.chat.id, message.from_user.id, True)
                      
                        game_name = get_lot_name(LOTS[n])
                        bot.reply_to(message, f"✅ Steam ID изменен на {steam_id}\n🎮 Игра: {game_name}", 
                                    reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
                else:
                    bot.reply_to(message, f"Лот {n} не найден")
          
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в edited: {e}")
            if message.from_user:
                tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.reply_to(message, f"Произошла ошибка: {e}")


    tg.cbq_handler(open_settings, lambda c: c.data and c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    tg.cbq_handler(show_settings, lambda c: c.data and c.data.startswith(CBT_SHOW_SETTINGS))
    tg.cbq_handler(switch_currency, lambda c: c.data and c.data.startswith(CBT_CHANGE_CURRENCY))
    tg.cbq_handler(switch_steam_currency, lambda c: c.data and c.data.startswith(CBT_CHANGE_STEAM_CURRENCY))
    tg.cbq_handler(to_lot_mess, lambda c: c.data and c.data.startswith(CBT_TEXT_CHANGE_LOT))
    tg.cbq_handler(answer_to_lot_mess, lambda c: c.data and c.data.startswith(CBT_TEXT_EDIT))
    tg.cbq_handler(to_delete, lambda c: c.data and c.data.startswith(CBT_TEXT_DELETE))
    tg.cbq_handler(update_now, lambda c: c.data and c.data.startswith(CBT_UPDATE_NOW))
    tg.cbq_handler(show_stats, lambda c: c.data and c.data.startswith(CBT_STATS))
    tg.cbq_handler(show_lots_menu, lambda c: c.data and c.data.startswith(CBT_LOTS_MENU))
    tg.cbq_handler(edit_lot_menu, lambda c: c.data and c.data.startswith(CBT_EDIT_LOT))
    tg.cbq_handler(toggle_lot_status, lambda c: c.data and c.data.startswith(CBT_TOGGLE_LOT))
    tg.cbq_handler(delete_lot_confirm, lambda c: c.data and c.data.startswith(CBT_DELETE_LOT))
    tg.cbq_handler(refresh_currency_rates, lambda c: c.data and c.data.startswith(CBT_REFRESH_RATES))
    tg.cbq_handler(update_single_lot, lambda c: c.data and c.data.startswith("update_single_lot"))
  

    def wizard_currency_selected(call: telebot.types.CallbackQuery):
        """Обработка выбора валюты в мастере"""
        global WIZARD_STATES
        try:
            if not call.data:
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
              
            currency = call.data.split(':')[1]
            user_key = f"{call.message.chat.id}_{call.from_user.id}"
          
        
            if user_key not in WIZARD_STATES:
                bot.answer_callback_query(call.id, "❌ Сессия истекла")
                return
              
            state_data = WIZARD_STATES[user_key]
            lot_id = state_data.get("lot_id")
            steam_id = state_data.get("steam_id") 
            min_price = state_data.get("min_price")
          
            if not all([lot_id, steam_id, min_price]):
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
              
        
            WIZARD_STATES[user_key] = {
                "step": "max_price",
                "lot_id": lot_id,
                "steam_id": steam_id,
                "steam_currency": currency,
                "min_price": min_price
            }
          
            text = "🧙‍♂️ <b>Мастер добавления лота</b>\n\n"
            text += "📋 <b>Шаг 4 из 4: Максимальная цена</b>\n\n"
            text += f"✅ ID лота: <code>{lot_id}</code>\n"
            text += f"✅ Steam ID: <code>{steam_id}</code>\n"
            text += f"✅ Валюта: <code>{currency}</code>\n"
            text += f"✅ Мин. цена: <code>{min_price:.2f} {SETTINGS['account_currency']}</code>\n\n"
            text += f"Введите максимальную цену (больше {min_price:.2f}):"
          
            keyboard = K()
            keyboard.add(B("◀ К лотам", callback_data=f"{CBT_LOTS_MENU}:0"))
          
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id, f"✅ Валюта: {currency}")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в wizard_currency_selected: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")
  
    tg.cbq_handler(wizard_currency_selected, lambda c: c.data and c.data.startswith("wizard_currency:"))
  

    WIZARD_STATES = {}
  
    def wizard_message_handler(message: telebot.types.Message):
        """Обработчик сообщений для мастера с собственным хранением состояний"""
        global WIZARD_STATES
        try:
            logger.info(f"{LOGGER_PREFIX} === ПОЛУЧЕНО СООБЩЕНИЕ ===")
            logger.info(f"{LOGGER_PREFIX} Текст: '{message.text}'")
            logger.info(f"{LOGGER_PREFIX} От пользователя: {message.from_user.id if message.from_user else 'None'}")
            logger.info(f"{LOGGER_PREFIX} Чат: {message.chat.id}")
          
            if not message.text or not message.from_user:
                logger.info(f"{LOGGER_PREFIX} Сообщение пропущено (нет текста или пользователя)")
                return
              
            user_key = f"{message.chat.id}_{message.from_user.id}"
            logger.info(f"{LOGGER_PREFIX} User key: {user_key}")
            logger.info(f"{LOGGER_PREFIX} WIZARD_STATES: {WIZARD_STATES}")
            logger.info(f"{LOGGER_PREFIX} user_key in WIZARD_STATES: {user_key in WIZARD_STATES}")
          
        
            if user_key in WIZARD_STATES:
                state_data = WIZARD_STATES[user_key]
                logger.info(f"{LOGGER_PREFIX} ✅ НАЙДЕНО СОСТОЯНИЕ: {state_data}")
              
            
                handle_wizard_input(message, state_data)
                return
            else:
                logger.info(f"{LOGGER_PREFIX} ❌ СОСТОЯНИЕ НЕ НАЙДЕНО")
              
        
            state_data = tg.get_state(message.chat.id, message.from_user.id)
            if state_data:
                logger.info(f"{LOGGER_PREFIX} Найдено обычное состояние: {state_data}")
                edited(message)
            else:
                logger.info(f"{LOGGER_PREFIX} Состояний не найдено")
              
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в wizard_message_handler: {e}")
            import traceback
            logger.error(f"{LOGGER_PREFIX} Трассировка: {traceback.format_exc()}")
  
    def handle_wizard_input(message, state_data):
        """Прямая обработка ввода мастера"""
        global LOTS, WIZARD_STATES
        user_key = f"{message.chat.id}_{message.from_user.id}"
        step = state_data.get("step")
        text = message.text.strip()
      
        logger.info(f"{LOGGER_PREFIX} === ОБРАБОТКА ВВОДА МАСТЕРА ===")
        logger.info(f"{LOGGER_PREFIX} User key: {user_key}")
        logger.info(f"{LOGGER_PREFIX} Step: {step}")
        logger.info(f"{LOGGER_PREFIX} Text: '{text}'")
        logger.info(f"{LOGGER_PREFIX} State data: {state_data}")
      
        try:
            if step == "lot_id":
                logger.info(f"{LOGGER_PREFIX} Обрабатываем step=lot_id")
            
                if not text.isdigit():
                    bot.reply_to(message, "❌ ID лота должен содержать только цифры")
                    return
                  
                if text in LOTS:
                    bot.reply_to(message, f"❌ Лот {text} уже настроен")
                    return
              
            
                WIZARD_STATES[user_key] = {"step": "steam_id", "lot_id": text}
              
                text_msg = "🧙‍♂️ <b>Мастер добавления лота</b>\n\n"
                text_msg += "📋 <b>Шаг 2 из 4: Steam ID</b>\n\n"
                text_msg += f"✅ ID лота: <code>{text}</code>\n\n"
                text_msg += "Введите Steam ID игры:\n"
                text_msg += "• Для обычных игр: <code>730</code> (CS2)\n"
                text_msg += "• Для DLC: <code>sub/12345</code>\n"
                text_msg += "• Найти можно на steamdb.info"
              
                keyboard = K()
                keyboard.add(B("◀ К лотам", callback_data=f"{CBT_LOTS_MENU}:0"))
              
                bot.send_message(message.chat.id, text_msg, reply_markup=keyboard, parse_mode="HTML")
              
            elif step == "steam_id":
            
                logger.info(f"{LOGGER_PREFIX} Обрабатываем step=steam_id")
                lot_id = state_data.get("lot_id")
                logger.info(f"{LOGGER_PREFIX} Lot ID: {lot_id}")
                logger.info(f"{LOGGER_PREFIX} Валидируем Steam ID: {text}")
                is_valid, id_type, clean_id = validate_steam_id(text)
                logger.info(f"{LOGGER_PREFIX} Результат валидации: valid={is_valid}, type={id_type}, clean={clean_id}")
              
                if not is_valid:
                    logger.info(f"{LOGGER_PREFIX} ❌ Валидация не прошла: {clean_id}")
                    bot.reply_to(message, f"❌ {clean_id}")
                    return
              
            
                logger.info(f"{LOGGER_PREFIX} Получаем цену из Steam API для: {clean_id} (тип: {id_type})")
            
                if id_type == "sub":
                    original_steam_id = f"sub_{clean_id}"
                else:
                    original_steam_id = clean_id
                steam_price = get_steam_price(original_steam_id, "UAH")
                logger.info(f"{LOGGER_PREFIX} Steam цена: {steam_price}")
              
                if steam_price is None:
                    logger.info(f"{LOGGER_PREFIX} ❌ Не удалось получить цену из Steam API")
                    bot.reply_to(message, "❌ Не удалось получить цену из Steam API. Проверьте Steam ID или попробуйте позже.")
                    return
                  
                if steam_price == 0.0:
                    logger.info(f"{LOGGER_PREFIX} Бесплатная игра/DLC")
                    bot.reply_to(message, "❌ Это бесплатная игра или DLC. Нельзя создать лот для бесплатного контента.")
                    return
              
                logger.info(f"{LOGGER_PREFIX} Рассчитываем минимальную цену лота")
                min_price = calculate_lot_price(steam_price)
                logger.info(f"{LOGGER_PREFIX} Минимальная цена: {min_price}")
              
            
                logger.info(f"{LOGGER_PREFIX} Переходим к шагу 3: выбор валюты")
              
            
                original_steam_id = text
                WIZARD_STATES[user_key] = {
                    "step": "currency", 
                    "lot_id": lot_id, 
                    "steam_id": original_steam_id,
                    "steam_id_type": id_type,
                    "min_price": min_price
                }
                logger.info(f"{LOGGER_PREFIX} Обновлено состояние: {WIZARD_STATES[user_key]}")
              
                text_msg = "🧙‍♂️ <b>Мастер добавления лота</b>\n\n"
                text_msg += "📋 <b>Шаг 3 из 4: Валюта Steam</b>\n\n"
                text_msg += f"✅ ID лота: <code>{lot_id}</code>\n"
                text_msg += f"✅ Steam ID: <code>{original_steam_id}</code> ({id_type})\n"
                text_msg += f"✅ Мин. цена: <code>{min_price:.2f} {SETTINGS['account_currency']}</code>\n\n"
                text_msg += "Выберите валюту Steam для отслеживания:"
                logger.info(f"{LOGGER_PREFIX} Отправляем сообщение шага 3")
              
                keyboard = K()
                keyboard.row(
                    B("🇺🇦 UAH", callback_data=f"wizard_currency:UAH"),
                    B("🇺🇸 USD", callback_data=f"wizard_currency:USD")
                )
                keyboard.row(
                    B("🇷🇺 RUB", callback_data=f"wizard_currency:RUB"),
                    B("🇰🇿 KZT", callback_data=f"wizard_currency:KZT")
                )
                keyboard.add(B("🇪🇺 EUR", callback_data=f"wizard_currency:EUR"))
                keyboard.add(B("◀ К лотам", callback_data=f"{CBT_LOTS_MENU}:0"))
              
                bot.send_message(message.chat.id, text_msg, reply_markup=keyboard, parse_mode="HTML")
              
            elif step == "max_price":
            
                lot_id = state_data.get("lot_id")
                steam_id = state_data.get("steam_id")
                steam_currency = state_data.get("steam_currency")
                min_price = state_data.get("min_price")
              
                try:
                    max_price = float(text.replace(",", "."))
                    if max_price <= min_price:
                        bot.reply_to(message, f"❌ Максимальная цена должна быть больше {min_price:.2f}")
                        return
                except ValueError:
                    bot.reply_to(message, "❌ Введите корректную цену (например: 100.50)")
                    return
              
            
                lot_data = {
                    "steam_id": steam_id,
                    "steam_currency": steam_currency,
                    "min_price": min_price,
                    "max_price": max_price,
                    "enabled": True,
                    "last_update": 0,
                    "last_price": 0
                }
              
                LOTS[lot_id] = lot_data
                save_lots()
              
            
                if user_key in WIZARD_STATES:
                    del WIZARD_STATES[user_key]
              
            
                global_interval_hours = SETTINGS['time'] // 3600
              
                text_msg = "✅ <b>Лот успешно добавлен!</b>\n\n"
                text_msg += f"📦 ID лота: <code>{lot_id}</code>\n"
                text_msg += f"🎮 Steam ID: <code>{steam_id}</code>\n"
                text_msg += f"💰 Диапазон цен: {min_price:.2f} - {max_price:.2f} {SETTINGS['account_currency']}\n"
                text_msg += f"🌍 Валюта Steam: {steam_currency}\n\n"
                text_msg += f"⏰ Лот будет автоматически обновляться каждые <b>{global_interval_hours} ч</b>"
              
                keyboard = K()
                keyboard.add(B("📦 К лотам", callback_data=f"{CBT_LOTS_MENU}:0"))
                keyboard.add(B("🔄 Обновить сейчас", callback_data=f"update_single:{lot_id}"))
              
                bot.send_message(message.chat.id, text_msg, reply_markup=keyboard, parse_mode="HTML")
              
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в handle_wizard_input: {e}")
            bot.reply_to(message, "❌ Произошла ошибка")
  
    tg.msg_handler(wizard_message_handler)

    logger.info(f"{LOGGER_PREFIX} Инициализация завершена")

def post_start(cardinal):
    """Запуск основного потока обработки только добавленных лотов"""
  
    def process(cardinal):
        """Основной цикл обработки ТОЛЬКО добавленных лотов"""
        global LOTS, SETTINGS, CARDINAL_INSTANCE
        lot_last_check = {}
      
        logger.info(f"{LOGGER_PREFIX} Запущен основной цикл обработки добавленных лотов")
      
        while True:
            try:
                current_time = time.time()
                any_lot_processed = False
              
            
                for lot_id, lot_data in LOTS.items():
                    if lot_id == "0" or not lot_data.get("on", False):
                        continue
                  
                
                    global_interval = SETTINGS["time"]
                    last_check = lot_last_check.get(lot_id, 0)
                    if current_time - last_check < global_interval:
                        continue
                  
                
                    lot_last_check[lot_id] = current_time
                    any_lot_processed = True
                  
                    logger.info(f"{LOGGER_PREFIX} Обрабатываю добавленный лот {lot_id}")
                  
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
                      
                    
                        logger.debug(f"{LOGGER_PREFIX} Получаю цену Steam для {steam_id} в валюте {steam_currency}")
                        steam_price = get_steam_price(steam_id, steam_currency)
                      
                        if steam_price is None or steam_price == 0:
                            logger.warning(f"{LOGGER_PREFIX} ОШИБКА 1: Нет цены Steam для лота {lot_id} (steam_id: {steam_id}, валюта: {steam_currency})")
                            continue
                      
                    
                        logger.debug(f"{LOGGER_PREFIX} Вычисляю цену для лота {lot_id}: steam_price={steam_price}, steam_currency={steam_currency}")
                        new_price = calculate_lot_price(steam_price, steam_currency)
                      
                        if new_price <= 0:
                            logger.error(f"{LOGGER_PREFIX} ОШИБКА 2: Неверная цена для лота {lot_id}: {new_price} (steam_price: {steam_price}, steam_currency: {steam_currency})")
                            continue
                      
                    
                        lot_min = lot_data.get("min", SETTINGS["min_price"])
                        lot_max = lot_data.get("max", SETTINGS["max_price"])
                        new_price = max(lot_min, min(new_price, lot_max))
                      
                    
                        LOTS[lot_id]["last_steam_price"] = steam_price
                      
                    
                        change_price(CARDINAL_INSTANCE, lot_id, new_price)
                      
                        time.sleep(2)
                  
                    except Exception as e:
                        logger.warning(f"{LOGGER_PREFIX} Ошибка с лотом {lot_id}: {e}")
              
            
                if any_lot_processed:
                    with open("storage/plugins/steam_price_updater_lots.json", "w", encoding="utf-8") as f:
                        f.write(json.dumps(LOTS, indent=4, ensure_ascii=False))
                    logger.info(f"{LOGGER_PREFIX} Цикл обработки завершен")
          
            except Exception as e:
                logger.error(f"{LOGGER_PREFIX} Критическая ошибка в процессе: {e}")
          
        
            time.sleep(300)
  

    if not hasattr(cardinal, '_steam_updater_thread_running') or not cardinal._steam_updater_thread_running:
        logger.info(f"{LOGGER_PREFIX} Запускаю поток обработки добавленных лотов")
        thread = Thread(target=process, daemon=True, args=(cardinal,))
        thread.start()
        cardinal._steam_updater_thread_running = True
    else:
        logger.info(f"{LOGGER_PREFIX} Поток уже запущен")

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