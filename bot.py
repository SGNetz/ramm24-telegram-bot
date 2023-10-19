import asyncio
import os
import sqlite3
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Text
from aiogram.dispatcher.filters.state import State, StatesGroup
from selenium.common.exceptions import NoSuchElementException
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.contrib.fsm_storage.memory import MemoryStorage

# Global variable for the date-URL dictionary
date_url_dict = {
    "May 11, 2024: Prague": "URL_Placeholder",
    "May 15, 2024: Dresden": "URL_Placeholder",
    # Add more date URLs here
}

# Import configuration from config.py
from config import BOT_TOKEN, PASSWORD

# Check if BOT_TOKEN is available
if BOT_TOKEN is None:
    print("The BOT_TOKEN was not found. Make sure you have set the 'BOT_TOKEN' environment variable.")
    exit(1)

# SQLite database connection
conn = sqlite3.connect('tickets.db')
cursor = conn.cursor()

# Create the table if it does not exist
cursor.execute('''
    CREATE TABLE IF NOT EXISTS tickets (
        offer_id INTEGER PRIMARY KEY,
        date TEXT,
        seat_description TEXT,
        purchase_type TEXT,
        price TEXT,
        link TEXT
    )
''')
conn.commit()

class PasswordState(StatesGroup):
    ENTER_PASSWORD = State()

class TicketSelectionState(StatesGroup):
    DATE_SELECTION = State()
    TICKET_MONITORING = State()

class UserTicketMonitor:
    def __init__(self):
        self.user_stop_flags = {}

    def should_stop(self, user_id):
        return self.user_stop_flags.get(user_id, False)

    def stop(self, user_id):
        self.user_stop_flags[user_id] = True

    def reset(self, user_id):
        self.user_stop_flags[user_id] = False

user_ticket_monitor = UserTicketMonitor()

async def accept_cookies(driver):
    await asyncio.sleep(2)
    try:
        accept_button = driver.find_element('css selector', '#accept-cookies')
        if accept_button.is_displayed():
            accept_button.click()
            await asyncio.sleep(1)
    except NoSuchElementException:
        pass

def get_url_for_date(selected_date):
    return date_url_dict.get(selected_date, None)

async def get_event_data(bot, driver, url, selected_date, chat_id):
    await accept_cookies(driver)
    driver.get(url)
    await asyncio.sleep(4)
    html = driver.page_source
    soup = BeautifulSoup(html, 'html.parser')
    event_data = []
    event_entries = soup.select('.EventEntry')
    for event in event_entries:
        offer_id = event.get('data-offer-id')
        seat_description = event.select_one('.OfferEntry-SeatDescription')
        purchase_type = event.select_one('.OfferEntry-PurchaseTypeAndPrice')
        link_element = event.select_one('.EventEntryRow.EventEntry-Link.OfferEntry-Link a')
        if offer_id and seat_description and purchase_type and link_element:
            link = link_element.get('href')
            price = purchase_type.select_one('.CurrencyAndMoneyValueFormat .moneyValueFormat').text.strip()
            event_info = {
                'offer_id': offer_id,
                'seat_description': seat_description.text.strip(),
                'purchase_type': purchase_type.text.strip(),
                'price': price,
                'link': f"https://www.fansale.de{link}" if link else None
            }
            event_data.append(event_info)
            cursor.execute('''
                INSERT INTO tickets (offer_id, date, seat_description, purchase_type, price, link)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (offer_id, selected_date, event_info['seat_description'], event_info['purchase_type'], event_info['price'], event_info['link']))
            conn.commit()
            await send_telegram_message(bot, chat_id, event_info)
    return event_data

async def send_telegram_message(bot, chat_id, event):
    inline_keyboard = types.InlineKeyboardMarkup()
    inline_keyboard.add(types.InlineKeyboardButton(text='🎫 Get Ticket', url=event['link']))
    message = f"🎟️ *New ticket available!*\n\n"
    message += f"ℹ️ *Offer ID*: {event['offer_id']}\n"
    message += f"🪑 *Seat Description*: {event['seat_description']}\n"
    message += f"💲 *Price*: {event['price']}\n"
    message += f"💰 *Purchase Type*: {event['purchase_type']}\n"
    message += f"[🎫 Get Ticket]({event['link']})"
    await bot.send_message(chat_id=chat_id, text=message, reply_markup=inline_keyboard, parse_mode='Markdown')

async def start_password_entry(message: types.Message):
    await PasswordState.ENTER_PASSWORD.set()
    await message.reply("🔐 Please enter the password:")

async def process_password(message: types.Message, state: FSMContext):
    password = message.text
    correct_password = PASSWORD
    if password == correct_password:
        await state.finish()
        await start_ticket_selection(message)
    else:
        await message.reply("❌ Incorrect password! Please try again:")

async def start_ticket_selection(message: types.Message):
    await message.reply("🎟️ Welcome to ticket selection! 🎉 Please select a date.")
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for date in date_url_dict.keys():
        keyboard.add(types.KeyboardButton(date))
    await TicketSelectionState.DATE_SELECTION.set()
    await message.answer("Please select a date:", reply_markup=keyboard)

async def select_date(message: types.Message, state: FSMContext):
    selected_date = message.text
    if selected_date in date_url_dict:
        await state.update_data(selected_date=selected_date)
        chat_id = message.chat.id
        await message.reply(f"📅 You have selected {selected_date}. I will search for tickets. Please be patient.")
        stop_keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
        stop_keyboard.add(types.KeyboardButton("Stop"))
        await TicketSelectionState.TICKET_MONITORING.set()
        await message.answer("Press Stop to end the search.", reply_markup=stop_keyboard)
        url = get_url_for_date(selected_date)
        options = Options()
        options.headless = True
        driver = webdriver.Firefox(options=options)
        bot = Bot(token=BOT_TOKEN)
        try:
            while True:
                print(f"Checking tickets for {selected_date}...")
                if user_ticket_monitor.should_stop(message.from_user.id):
                    print(f"Search for tickets for {selected_date} stopped.")
                    break
                event_data = await get_event_data(bot, driver, url, selected_date, chat_id)
                if event_data:
                    for event in event_data:
                        await send_telegram_message(bot, chat_id, event)
                        print(f"New ticket found and sent for {selected_date}!")
                await asyncio.sleep(60)
        except Exception as e:
            print(f"Error retrieving data: {str(e)}")
        finally:
            user_ticket_monitor.reset(message.from_user.id)
            driver.quit()
        await state.finish()
    else:
        await message.reply("❌ Invalid selection. Please choose a date from the menu.")

async def stop_search(message: types.Message, state: FSMContext):
    if await state.get_state() == TicketSelectionState.TICKET_MONITORING.state:
        user_ticket_monitor.stop(message.from_user.id)
        await state.finish()
        await start_ticket_selection(message)
    else:
        await message.reply("⛔ The current search was not started.")

async def main():
    storage = MemoryStorage()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(bot, storage=storage, loop=asyncio.get_event_loop())
    dp.middleware.setup(LoggingMiddleware())
    dp.register_message_handler(start_password_entry, commands=['start'])
    dp.register_message_handler(process_password, state=PasswordState.ENTER_PASSWORD)
    dp.register_message_handler(select_date, state=TicketSelectionState.DATE_SELECTION)
    dp.register_message_handler(stop_search, state=TicketSelectionState.TICKET_MONITORING)
    await dp.start_polling()
    await dp.idle()

if __name__ == '__main__':
    asyncio.run(main())
