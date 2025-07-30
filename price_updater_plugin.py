from __future__ import annotations
import json
import time
import requests
import asyncio
import threading
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List, Tuple, Any
from datetime import datetime
import logging
from pathlib import Path

# FunPay imports  
from FunPayAPI.types import LotShortcut
if TYPE_CHECKING:
    from cardinal import Cardinal
from FunPayAPI.updater.events import *
from tg_bot import CBT
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B
import telebot
from locales.localizer import Localizer

# Plugin info
NAME = "Steam Price Updater"
VERSION = "3.0.0"
DESCRIPTION = "Автоматическое обновление цен лотов на основе Steam API (оптимизированная версия)"
CREDITS = "@humblegodq"
UUID = "247153d9-f732-4f01-a11f-a3945b68b533"
SETTINGS_PAGE = True

# Logging
logger = logging.getLogger("FPC.steam_price_updater")
PREFIX = "[STEAM PRICE UPDATER]"

# Localization
localizer = Localizer()
_ = localizer.translate

# Configuration
@dataclass
class Config:
    """Централизованная конфигурация"""
    CACHE_TTL: int = 3600  # 1 час
    UPDATE_INTERVAL: int = 21600  # 6 часов - ИСПРАВЛЕНА ОШИБКА ТАЙМЕРА
    LOT_DELAY: int = 2  # задержка между лотами
    API_TIMEOUT: int = 15
    MAX_RETRIES: int = 3
    
    # Steam API
    STEAM_DELAY: int = 1  # снижена задержка
    
    # Pricing
    CURRENCY_MARKUP: float = 3.0  # наценка на валютный курс
    PROFIT_MARGIN: float = 5.0   # маржа прибыли
    FIXED_MARKUP: float = 0.5    # фиксированная наценка
    MIN_PRICE: float = 1.0
    MAX_PRICE: float = 5000.0
    
    # Currencies
    DEFAULT_CURRENCY: str = "USD"
    STEAM_CURRENCIES: List[str] = None
    ACCOUNT_CURRENCIES: List[str] = None
    
    def __post_init__(self):
        if self.STEAM_CURRENCIES is None:
            self.STEAM_CURRENCIES = ["UAH", "RUB", "USD", "EUR", "KZT"]
        if self.ACCOUNT_CURRENCIES is None:
            self.ACCOUNT_CURRENCIES = ["USD", "RUB", "EUR"]

config = Config()

# Data models
@dataclass
class LotData:
    """Модель данных лота"""
    steam_id: str
    steam_currency: str = "UAH"
    min_price: float = config.MIN_PRICE
    max_price: float = config.MAX_PRICE
    enabled: bool = True
    last_steam_price: float = 0.0
    last_price: float = 0.0
    last_update: float = 0.0
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'LotData':
        # Backwards compatibility
        steam_id = data.get('steam_id') or str(data.get('steam_app_id', ''))
        return cls(
            steam_id=steam_id,
            steam_currency=data.get('steam_currency', 'UAH'),
            min_price=data.get('min', config.MIN_PRICE),
            max_price=data.get('max', config.MAX_PRICE),
            enabled=data.get('on', True),
            last_steam_price=data.get('last_steam_price', 0.0),
            last_price=data.get('last_price', 0.0),
            last_update=data.get('last_update', 0.0)
        )

@dataclass
class Settings:
    """Настройки плагина"""
    currency: str = config.DEFAULT_CURRENCY
    update_interval: int = config.UPDATE_INTERVAL
    currency_markup: float = config.CURRENCY_MARKUP
    profit_margin: float = config.PROFIT_MARGIN
    fixed_markup: float = config.FIXED_MARKUP
    min_price: float = config.MIN_PRICE
    max_price: float = config.MAX_PRICE

# Global state
class PluginState:
    """Централизованное состояние плагина"""
    def __init__(self):
        self.settings = Settings()
        self.lots: Dict[str, LotData] = {}
        self.cardinal: Optional[Cardinal] = None
        self.cache = {}
        self.lock = threading.RLock()
        self.update_thread: Optional[threading.Thread] = None
        self.running = False
    
    def save_settings(self):
        """Сохранение настроек"""
        try:
            Path("storage/plugins").mkdir(parents=True, exist_ok=True)
            with open("storage/plugins/steam_price_updater.json", "w", encoding="utf-8") as f:
                json.dump(asdict(self.settings), f, indent=2, ensure_ascii=False)
            logger.info(f"{PREFIX} Настройки сохранены")
        except Exception as e:
            logger.error(f"{PREFIX} Ошибка сохранения настроек: {e}")
    
    def load_settings(self):
        """Загрузка настроек"""
        try:
            settings_file = Path("storage/plugins/steam_price_updater.json")
            if settings_file.exists():
                with open(settings_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.settings = Settings(**data)
                logger.info(f"{PREFIX} Настройки загружены")
        except Exception as e:
            logger.warning(f"{PREFIX} Ошибка загрузки настроек: {e}, используются значения по умолчанию")
    
    def save_lots(self):
        """Сохранение лотов"""
        try:
            Path("storage/plugins").mkdir(parents=True, exist_ok=True)
            lots_data = {lot_id: lot.to_dict() for lot_id, lot in self.lots.items()}
            with open("storage/plugins/steam_price_updater_lots.json", "w", encoding="utf-8") as f:
                json.dump(lots_data, f, indent=2, ensure_ascii=False)
            logger.info(f"{PREFIX} Лоты сохранены: {len(self.lots)}")
        except Exception as e:
            logger.error(f"{PREFIX} Ошибка сохранения лотов: {e}")
    
    def load_lots(self):
        """Загрузка лотов"""
        try:
            lots_file = Path("storage/plugins/steam_price_updater_lots.json")
            if lots_file.exists():
                with open(lots_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.lots = {lot_id: LotData.from_dict(lot_data) 
                               for lot_id, lot_data in data.items() if lot_id != "0"}
                logger.info(f"{PREFIX} Лоты загружены: {len(self.lots)}")
        except Exception as e:
            logger.warning(f"{PREFIX} Ошибка загрузки лотов: {e}")

state = PluginState()

# Unified cache system
class Cache:
    """Единая система кешировования"""
    def __init__(self):
        self._data = {}
        self._lock = threading.RLock()
    
    def get(self, key: str, default=None):
        with self._lock:
            entry = self._data.get(key)
            if entry and time.time() - entry['time'] < config.CACHE_TTL:
                return entry['value']
            elif entry:
                del self._data[key]
            return default
    
    def set(self, key: str, value: Any):
        with self._lock:
            self._data[key] = {'value': value, 'time': time.time()}
    
    def clear(self):
        with self._lock:
            self._data.clear()

cache = Cache()

# Currency API
class CurrencyAPI:
    """Упрощенный API для валют"""
    
    FALLBACK_RATES = {
        "UAH": 41.82, "RUB": 78.42, "KZT": 519.86, 
        "EUR": 0.85, "USD": 1.0
    }
    
    @staticmethod
    def get_rate(currency: str) -> float:
        """Получение курса валюты к USD"""
        cache_key = f"rate_{currency}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        
        try:
            # Единый API для всех валют
            url = "https://api.exchangerate-api.com/v4/latest/USD"
            response = requests.get(url, timeout=config.API_TIMEOUT)
            
            if response.status_code == 200:
                data = response.json()
                rate = data.get("rates", {}).get(currency)
                if rate:
                    cache.set(cache_key, float(rate))
                    return float(rate)
        
        except Exception as e:
            logger.warning(f"{PREFIX} Ошибка API валют: {e}")
        
        # Fallback
        rate = CurrencyAPI.FALLBACK_RATES.get(currency, 1.0)
        logger.warning(f"{PREFIX} Используется fallback курс {currency}: {rate}")
        return rate

# Steam API
class SteamAPI:
    """Упрощенный Steam API"""
    
    CURRENCY_MAP = {"UAH": "ua", "RUB": "ru", "USD": "us", "EUR": "eu", "KZT": "kz"}
    
    @staticmethod
    def get_price(steam_id: str, currency: str = "UAH") -> Optional[float]:
        """Получение цены из Steam API"""
        cache_key = f"steam_{steam_id}_{currency}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        
        try:
            time.sleep(config.STEAM_DELAY)  # Rate limiting
            
            cc = SteamAPI.CURRENCY_MAP.get(currency, "ua")
            
            # Handle sub packages
            if steam_id.startswith("sub_"):
                package_id = steam_id[4:]
                url = f"https://store.steampowered.com/api/packagedetails/"
                params = {"packageids": package_id, "cc": cc}
            else:
                url = f"https://store.steampowered.com/api/appdetails/"
                params = {"appids": steam_id, "cc": cc, "filters": "price_overview"}
            
            response = requests.get(url, params=params, timeout=config.API_TIMEOUT)
            
            if response.status_code == 200:
                data = response.json()
                
                # Extract price based on type
                item_data = data.get(package_id if steam_id.startswith("sub_") else steam_id)
                if item_data and item_data.get("success"):
                    if steam_id.startswith("sub_"):
                        price_data = item_data.get("data", {}).get("price")
                    else:
                        price_data = item_data.get("data", {}).get("price_overview")
                    
                    if price_data:
                        final_price = price_data.get("final", 0)
                        if final_price > 0:
                            price = final_price / 100.0
                            cache.set(cache_key, price)
                            return price
            
            return 0.0
            
        except Exception as e:
            logger.warning(f"{PREFIX} Ошибка Steam API для {steam_id}: {e}")
            return None

# Price calculation
def calculate_price(steam_price: float, steam_currency: str) -> float:
    """Упрощенный расчет цены"""
    if steam_price <= 0:
        return state.settings.min_price
    
    try:
        # Convert to account currency
        if steam_currency != state.settings.currency:
            if state.settings.currency == "USD":
                # Steam currency to USD
                rate = CurrencyAPI.get_rate(steam_currency)
                base_price = steam_price / rate
            else:
                # Steam -> USD -> Account currency
                steam_rate = CurrencyAPI.get_rate(steam_currency)
                account_rate = CurrencyAPI.get_rate(state.settings.currency)
                base_price = (steam_price / steam_rate) * account_rate
        else:
            base_price = steam_price
        
        # Apply markups
        with_currency_markup = base_price * (1 + state.settings.currency_markup / 100)
        final_price = with_currency_markup * (1 + state.settings.profit_margin / 100) + state.settings.fixed_markup
        
        # Apply limits
        final_price = max(state.settings.min_price, min(final_price, state.settings.max_price))
        
        return round(final_price, 2)
        
    except Exception as e:
        logger.error(f"{PREFIX} Ошибка расчета цены: {e}")
        return state.settings.min_price

# Lot management
def update_lot_price(lot_id: str, lot: LotData) -> bool:
    """Обновление цены лота"""
    try:
        # Get Steam price
        steam_price = SteamAPI.get_price(lot.steam_id, lot.steam_currency)
        if not steam_price or steam_price <= 0:
            logger.warning(f"{PREFIX} Нет цены Steam для лота {lot_id}")
            return False
        
        # Calculate new price
        new_price = calculate_price(steam_price, lot.steam_currency)
        if new_price <= 0:
            return False
        
        # Apply lot limits
        new_price = max(lot.min_price, min(new_price, lot.max_price))
        
        # Update via Cardinal API
        success = change_lot_price(lot_id, new_price)
        if success:
            lot.last_steam_price = steam_price
            lot.last_price = new_price
            lot.last_update = time.time()
            logger.info(f"{PREFIX} Лот {lot_id} обновлен: Steam {steam_price} {lot.steam_currency} → ${new_price}")
        
        return success
        
    except Exception as e:
        logger.error(f"{PREFIX} Ошибка обновления лота {lot_id}: {e}")
        return False

def change_lot_price(lot_id: str, new_price: float) -> bool:
    """Изменение цены через Cardinal API"""
    try:
        if not state.cardinal or not state.cardinal.account:
            return False
        
        lot_fields = state.cardinal.account.get_lot_fields(int(lot_id))
        if not lot_fields or not hasattr(lot_fields, 'price'):
            return False
        
        old_price = lot_fields.price
        if abs(new_price - old_price) >= 0.01:  # Значимое изменение
            lot_fields.price = new_price
            state.cardinal.account.save_lot(lot_fields)
            return True
        
        return True  # Цена не изменилась значительно
        
    except Exception as e:
        logger.error(f"{PREFIX} Ошибка изменения цены лота {lot_id}: {e}")
        return False

# Main update loop - ИСПРАВЛЕНА ОШИБКА ТАЙМЕРА
def price_update_loop():
    """Основной цикл обновления - ИСПРАВЛЕН ИНТЕРВАЛ 6 ЧАСОВ"""
    logger.info(f"{PREFIX} Запущен цикл обновления с интервалом {state.settings.update_interval} секунд")
    
    while state.running:
        try:
            current_time = time.time()
            updated_count = 0
            
            # Process enabled lots
            for lot_id, lot in state.lots.items():
                if not lot.enabled:
                    continue
                
                # Check if update needed - ИСПРАВЛЕН РАСЧЕТ ВРЕМЕНИ
                if current_time - lot.last_update >= state.settings.update_interval:
                    if update_lot_price(lot_id, lot):
                        updated_count += 1
                    
                    time.sleep(config.LOT_DELAY)  # Rate limiting
            
            if updated_count > 0:
                state.save_lots()
                logger.info(f"{PREFIX} Обновлено лотов: {updated_count}")
            
            # Sleep until next cycle
            time.sleep(60)  # Check every minute
            
        except Exception as e:
            logger.error(f"{PREFIX} Ошибка в цикле обновления: {e}")
            time.sleep(60)

# Telegram Bot Handlers
def init_telegram_handlers(tg, bot):
    """Инициализация обработчиков Telegram"""
    
    # Callback data constants
    CBT_MAIN = f"{CBT.PLUGIN_SETTINGS}:{UUID}"
    CBT_LOTS = "SPU_lots"
    CBT_SETTINGS = "SPU_settings"
    CBT_ADD_LOT = "SPU_add_lot"
    CBT_EDIT_LOT = "SPU_edit_lot"
    CBT_DELETE_LOT = "SPU_delete_lot"
    CBT_TOGGLE_LOT = "SPU_toggle_lot"
    CBT_UPDATE_NOW = "SPU_update_now"
    CBT_STATS = "SPU_stats"
    
    def main_menu(call: telebot.types.CallbackQuery):
        """Главное меню"""
        try:
            active_lots = len([l for l in state.lots.values() if l.enabled])
            total_lots = len(state.lots)
            
            text = f"🎮 <b>Steam Price Updater v{VERSION}</b>\n\n"
            text += f"📦 Лоты: {total_lots} всего, {active_lots} активных\n"
            text += f"⏱ Интервал: {state.settings.update_interval // 3600} ч\n"
            text += f"💰 Валюта: {state.settings.currency}\n\n"
            text += f"📈 Наценка: {state.settings.currency_markup}% + {state.settings.profit_margin}% + ${state.settings.fixed_markup}"
            
            keyboard = K()
            keyboard.row(
                B("📦 Лоты", callback_data=f"{CBT_LOTS}:0"),
                B("🔄 Обновить", callback_data=CBT_UPDATE_NOW)
            )
            keyboard.row(
                B("⚙️ Настройки", callback_data=CBT_SETTINGS),
                B("📊 Статистика", callback_data=CBT_STATS)
            )
            keyboard.add(B("◀ Назад", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
            
        except Exception as e:
            logger.error(f"{PREFIX} Ошибка главного меню: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")
    
    def lots_menu(call: telebot.types.CallbackQuery):
        """Меню лотов"""
        try:
            page = int(call.data.split(":")[-1]) if ":" in call.data else 0
            per_page = 5
            
            lots_list = list(state.lots.items())
            total = len(lots_list)
            start = page * per_page
            end = start + per_page
            current_lots = lots_list[start:end]
            
            text = f"📦 <b>Управление лотами</b>\n\n"
            text += f"Всего: {total}, Страница: {page + 1}/{(total - 1) // per_page + 1 if total > 0 else 1}\n\n"
            
            keyboard = K()
            
            if total == 0:
                text += "Лоты не добавлены"
            else:
                for lot_id, lot in current_lots:
                    status = "🟢" if lot.enabled else "🔴"
                    name = get_game_name(lot.steam_id)
                    keyboard.add(B(f"{status} {name[:20]}", callback_data=f"{CBT_EDIT_LOT}:{lot_id}"))
            
            # Navigation
            nav_buttons = []
            if page > 0:
                nav_buttons.append(B("⬅", callback_data=f"{CBT_LOTS}:{page-1}"))
            if end < total:
                nav_buttons.append(B("➡", callback_data=f"{CBT_LOTS}:{page+1}"))
            
            if nav_buttons:
                keyboard.row(*nav_buttons)
            
            keyboard.add(B("➕ Добавить лот", callback_data=CBT_ADD_LOT))
            keyboard.add(B("◀ Главное меню", callback_data=CBT_MAIN))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                reply_markup=keyboard, parse_mode="HTML")
            bot.answer_callback_query(call.id)
            
        except Exception as e:
            logger.error(f"{PREFIX} Ошибка меню лотов: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")
    
    def add_lot_wizard(call: telebot.types.CallbackQuery):
        """Мастер добавления лота"""
        try:
            text = "🧙‍♂️ <b>Добавление лота</b>\n\n"
            text += "Введите ID лота с FunPay (только цифры):"
            
            keyboard = K()
            keyboard.add(B("◀ Отмена", callback_data=f"{CBT_LOTS}:0"))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                reply_markup=keyboard, parse_mode="HTML")
            
            tg.set_state(call.message.chat.id, call.message.id, call.from_user.id,
                        "add_lot", {"step": "lot_id"})
            bot.answer_callback_query(call.id)
            
        except Exception as e:
            logger.error(f"{PREFIX} Ошибка мастера: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")
    
    def update_now(call: telebot.types.CallbackQuery):
        """Принудительное обновление"""
        try:
            bot.answer_callback_query(call.id, "🔄 Обновление запущено...")
            
            def update_thread():
                updated = 0
                for lot_id, lot in state.lots.items():
                    if lot.enabled:
                        if update_lot_price(lot_id, lot):
                            updated += 1
                        time.sleep(config.LOT_DELAY)
                
                state.save_lots()
                bot.send_message(call.message.chat.id, f"✅ Обновлено лотов: {updated}")
            
            threading.Thread(target=update_thread, daemon=True).start()
            
        except Exception as e:
            logger.error(f"{PREFIX} Ошибка обновления: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")
    
    def handle_text(message: telebot.types.Message):
        """Обработка текстовых сообщений"""
        try:
            state_data = tg.get_state(message.chat.id, message.from_user.id)
            if not state_data or state_data.get("name") != "add_lot":
                return
            
            data = state_data.get("data", {})
            step = data.get("step")
            text = message.text.strip()
            
            if step == "lot_id":
                if not text.isdigit():
                    bot.reply_to(message, "❌ ID должен содержать только цифры")
                    return
                
                if text in state.lots:
                    bot.reply_to(message, f"❌ Лот {text} уже существует")
                    return
                
                # Next step - Steam ID
                msg = bot.reply_to(message, 
                    "Введите Steam ID игры:\n"
                    "• Для игр: 730 (CS2)\n" 
                    "• Для DLC: sub_12345")
                
                tg.set_state(message.chat.id, msg.message_id, message.from_user.id,
                           "add_lot", {"step": "steam_id", "lot_id": text})
            
            elif step == "steam_id":
                lot_id = data.get("lot_id")
                
                # Validate Steam ID
                if not (text.isdigit() or (text.startswith("sub_") and text[4:].isdigit())):
                    bot.reply_to(message, "❌ Неверный формат Steam ID")
                    return
                
                # Create lot with defaults
                lot = LotData(
                    steam_id=text,
                    steam_currency="UAH",
                    min_price=state.settings.min_price,
                    max_price=state.settings.max_price
                )
                
                state.lots[lot_id] = lot
                state.save_lots()
                
                game_name = get_game_name(text)
                bot.reply_to(message, 
                    f"✅ <b>Лот создан!</b>\n\n"
                    f"ID: {lot_id}\n"
                    f"Игра: {game_name}\n"
                    f"Steam ID: {text}", parse_mode="HTML")
                
                tg.clear_state(message.chat.id, message.from_user.id)
        
        except Exception as e:
            logger.error(f"{PREFIX} Ошибка обработки текста: {e}")
            tg.clear_state(message.chat.id, message.from_user.id)
    
    # Register handlers
    tg.cbq_handler(main_menu, lambda c: c.data and c.data.startswith(CBT_MAIN))
    tg.cbq_handler(lots_menu, lambda c: c.data and c.data.startswith(CBT_LOTS))
    tg.cbq_handler(add_lot_wizard, lambda c: c.data == CBT_ADD_LOT)
    tg.cbq_handler(update_now, lambda c: c.data == CBT_UPDATE_NOW)
    tg.msg_handler(handle_text)

def get_game_name(steam_id: str) -> str:
    """Получение названия игры"""
    cache_key = f"name_{steam_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    
    try:
        if steam_id.startswith("sub_"):
            package_id = steam_id[4:]
            url = f"https://store.steampowered.com/api/packagedetails"
            params = {"packageids": package_id, "filters": "basic"}
        else:
            url = f"https://store.steampowered.com/api/appdetails"
            params = {"appids": steam_id, "filters": "basic"}
        
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            item_data = data.get(package_id if steam_id.startswith("sub_") else steam_id)
            if item_data and item_data.get("success"):
                name = item_data.get("data", {}).get("name", f"Steam {steam_id}")
                cache.set(cache_key, name)
                return name
    
    except Exception as e:
        logger.debug(f"{PREFIX} Ошибка получения названия для {steam_id}: {e}")
    
    return f"Steam {steam_id}"

# Plugin lifecycle
def init(cardinal: Cardinal):
    """Инициализация плагина"""
    global state
    
    logger.info(f"{PREFIX} Инициализация v{VERSION}")
    
    state.cardinal = cardinal
    state.load_settings()
    state.load_lots()
    
    if cardinal.telegram:
        init_telegram_handlers(cardinal.telegram, cardinal.telegram.bot)
        logger.info(f"{PREFIX} Telegram обработчики зарегистрированы")
    else:
        logger.warning(f"{PREFIX} Telegram бот недоступен")

def post_start(cardinal: Cardinal):
    """Запуск после инициализации Cardinal"""
    global state
    
    if not state.running:
        state.running = True
        state.update_thread = threading.Thread(target=price_update_loop, daemon=True)
        state.update_thread.start()
        logger.info(f"{PREFIX} Цикл обновления запущен")

def cleanup():
    """Очистка ресурсов"""
    global state
    
    state.running = False
    if state.update_thread:
        state.update_thread.join(timeout=5)
    
    state.save_settings()
    state.save_lots()
    logger.info(f"{PREFIX} Плагин остановлен")

# Plugin bindings
BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [post_start]
BIND_TO_DELETE = [cleanup]