import ccxt
import numpy as np
import talib
import pandas as pd
import time
import configparser
import logging
import telegram
from datetime import datetime
from binance.client import Client
from configparser import ConfigParser
from requests.exceptions import ConnectionError, HTTPError

# Загрузка конфигурации
config = configparser.ConfigParser()
config.read('config.ini')

api_key = config['Binance']['api_key']
api_secret = config['Binance']['api_secret']
telegram_token = config['telegram']['token']
telegram_chat_id = config['telegram']['chat_id']

margin_mode = config['Binance']['margin_mode']
position_size_percent = float(config['Binance']['position_size_percent'])
leverage = int(config['Binance']['leverage'])
take_profit_percent = float(config['Binance']['take_profit_percent'])
stop_loss_percent = float(config['Binance']['stop_loss_percent'])

# Настройка Telegram бота
telegram_bot = telegram.Bot(token=telegram_token)

# Настройка логирования
logging.basicConfig(filename='bot.log', level=logging.INFO)

# Создание экземпляра клиента Binance Futures (ccxt)
exchange = ccxt.binance({
    'apiKey': api_key,
    'secret': api_secret,
})
exchange.options['defaultType'] = 'future'

# Создание экземпляра клиента Binance (binance)
binance_client = Client(api_key=api_key, api_secret=api_secret)
open_orders = {}

def read_config(file_path):
    config = ConfigParser()
    try:
        config.read(file_path)
        settings = {
            'api_key': config.get('Binance', 'api_key'),
            'api_secret': config.get('Binance', 'api_secret'),
            'margin_mode': config.get('Binance', 'margin_mode'),
            'position_size_percent': float(config.get('Binance', 'position_size_percent')),
            'leverage': int(config.get('Binance', 'leverage')),
            'take_profit_percent': float(config.get('Binance', 'take_profit_percent')),
            'stop_loss_percent': float(config.get('Binance', 'stop_loss_percent')),
        }
        return settings
    except Exception as e:
        logging.error(f"Error reading config file: {e}")
        exit()

def initialize_client(api_key, api_secret):
    try:
        return Client(api_key=api_key, api_secret=api_secret)
    except Exception as e:
        logging.error(f"Error initializing Binance client: {e}")
        exit()

def send_telegram_message(message):
    try:
        telegram_bot.send_message(chat_id=telegram_chat_id, text=message)
        logging.info(f"Telegram message sent: {message}")
    except Exception as e:
        logging.error(f"Error sending Telegram message: {e}")

def fetch_ohlcv(symbol, timeframe='15m'):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        logging.error(f"Error fetching OHLCV data for {symbol} on {timeframe} timeframe: {e}")
        return None

def calculate_indicators(df):
    try:
        df['upper_band'], df['middle_band'], df['lower_band'] = talib.BBANDS(df['close'], timeperiod=14, nbdevup=1, nbdevdn=1)
        df['ao'] = talib.ADOSC(df['high'], df['low'], df['close'], df['volume'])
        df['rsi'] = talib.RSI(df['close'], timeperiod=14)
        df['ema'] = talib.EMA(df['close'], timeperiod=4)
        return df
    except Exception as e:
        logging.error(f"Error calculating technical indicators: {e}")
        return None

def check_signals(df):
    try:
        latest = df.iloc[-1]
        previous = df.iloc[-2]

        buy_signal = (
            previous['ema'] <= previous['middle_band'] and latest['ema'] > latest['middle_band'] and
            previous['ao'] <= 0 and latest['ao'] > 0 and
            previous['rsi'] <= 50 and latest['rsi'] > 50
        )

        sell_signal = (
            previous['ema'] >= previous['middle_band'] and latest['ema'] < latest['middle_band'] and
            previous['ao'] >= 0 and latest['ao'] < 0 and
            previous['rsi'] >= 50 and latest['rsi'] < 50
        )

        if buy_signal:
            position_side = 'LONG'
            return "LONG", latest['close'], position_side
        elif sell_signal:
            position_side = 'SHORT'
            return "SHORT", latest['close'], position_side
        else:
            return None, None, None
    except Exception as e:
        logging.error(f"Error checking signals: {e}")
        return None, None, None

def get_symbol_info(client, trading_pair):
    try:
        trading_pair = trading_pair.replace(':USDT', '').replace('/', '')  # Clean symbol
        symbol_info = client.futures_exchange_info()
        for symbol in symbol_info['symbols']:
            if symbol['symbol'] == trading_pair:
                step_size = float(symbol['filters'][1]['stepSize'])
                tick_size = float(symbol['filters'][0]['tickSize'])
                min_notional = float(symbol['filters'][5]['notional'])
                return step_size, tick_size, min_notional
        logging.error(f"Symbol info not found for {trading_pair}.")
        return None, None, None
    except Exception as e:
        logging.error(f"Error fetching symbol info: {e}")
        return None, None, None

def set_margin_mode(client, trading_pair, margin_mode):
    try:
        trading_pair = trading_pair.replace(':USDT', '').replace('/', '')  # Clean symbol
        if margin_mode.lower() == 'isolated':
            client.futures_change_margin_type(symbol=trading_pair, marginType='ISOLATED')
        elif margin_mode.lower() == 'cross':
            client.futures_change_margin_type(symbol=trading_pair, marginType='CROSSED')
        else:
            logging.error(f"Invalid margin mode: {margin_mode}")
            exit()
    except Exception as e:
        if "No need to change margin type" in str(e):
            logging.info("Margin mode already set.")
        else:
            logging.error(f"Error changing margin mode: {e}")
            exit()

def get_account_balance(client):
    try:
        account_info = client.futures_account()
        balance = float(account_info['totalWalletBalance'])
        return balance
    except Exception as e:
        logging.error(f"Error fetching account balance: {e}")
        exit()

def calculate_position_size(balance, position_size_percent, leverage, current_price, step_size, min_notional):
    try:
        if step_size is None or min_notional is None:
            logging.error("Failed to get symbol info for position size calculation.")
            return None

        notional_value = balance * position_size_percent / 100 * leverage
        position_size = notional_value / current_price
        position_size = round(position_size - (position_size % step_size), 3)

        if notional_value < min_notional:
            position_size = min_notional / current_price
            position_size = round(position_size - (position_size % step_size), 3)
            logging.info(f"Position size adjusted to minimum notional value: {position_size}")

        return position_size
    except Exception as e:
        logging.error(f"Error calculating position size: {e}")
        return None

def calculate_prices(current_price, take_profit_percent, stop_loss_percent, position_side, tick_size):
    try:
        if position_side == 'LONG':
            take_profit_price = current_price * (1 + take_profit_percent / 100)
            stop_loss_price = current_price * (1 - stop_loss_percent / 100)
        elif position_side == 'SHORT':
            take_profit_price = current_price * (1 - take_profit_percent / 100)
            stop_loss_price = current_price * (1 + stop_loss_percent / 100)
        else:
            logging.error(f"Invalid position_side: {position_side}")
            exit()

        take_profit_price = round(take_profit_price - (take_profit_price % tick_size), 5)
        stop_loss_price = round(stop_loss_price - (stop_loss_price % tick_size), 5)

        return take_profit_price, stop_loss_price
    except Exception as e:
        logging.error(f"Error calculating prices: {e}")
        return None, None

def count_open_positions(client, position_side):
    try:
        account_info = client.futures_account()
        positions = account_info['positions']
        count = 0
        for pos in positions:
            if pos['positionSide'] == position_side and float(pos['positionAmt']) != 0:
                count += 1
        return count
    except Exception as e:
        logging.error(f"Error counting open positions: {e}")
        return None

def cancel_all_orders(client, trading_pair):
    try:
        trading_pair = trading_pair.replace(':USDT', '').replace('/', '')  # Clean symbol
        open_orders = client.futures_get_open_orders(symbol=trading_pair)
        for order in open_orders:
            client.futures_cancel_order(symbol=trading_pair, orderId=order['orderId'])
        logging.info(f"Cancelled all open orders for {trading_pair}.")
    except Exception as e:
        logging.error(f"Error cancelling orders for {trading_pair}: {e}")

def cleanup_orders(client):
    try:
        # Получаем список всех открытых ордеров
        open_orders = client.futures_get_open_orders()
        # Создаем словарь, чтобы отслеживать ордера по символам
        orders_by_symbol = {}
        for order in open_orders:
            symbol = order['symbol']
            if symbol not in orders_by_symbol:
                orders_by_symbol[symbol] = []
            orders_by_symbol[symbol].append(order)
        
        # Проверяем позиции по каждому символу
        for symbol, orders in orders_by_symbol.items():
            open_positions = client.futures_position_information(symbol=symbol)
            has_open_position = any(float(pos['positionAmt']) != 0 for pos in open_positions)

            # Удаляем ордера, если позиции нет
            if not has_open_position:
                for order in orders:
                    if order['type'] in ['TAKE_PROFIT_MARKET', 'STOP_MARKET']:
                        client.futures_cancel_order(symbol=symbol, orderId=order['orderId'])
                        logging.info(f"Removed {order['type']} order for {symbol} as no open position exists.")
    except Exception as e:
        logging.error(f"Error cleaning up orders: {e}")

def ensure_stop_loss_take_profit(client):
    try:
        # Получаем список всех открытых позиций
        open_positions = client.futures_position_information()
        for pos in open_positions:
            symbol = pos['symbol']
            position_amt = float(pos['positionAmt'])
            entry_price = float(pos['entryPrice'])
            if position_amt == 0:
                continue

            # Получаем текущие ордера по символу
            open_orders = client.futures_get_open_orders(symbol=symbol)
            has_take_profit = any(order['type'] == 'TAKE_PROFIT_MARKET' for order in open_orders)
            has_stop_loss = any(order['type'] == 'STOP_MARKET' for order in open_orders)

            # Вычисляем текущий ROI
            ticker = client.get_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            roi = ((current_price - entry_price) / entry_price) * 100 * leverage if pos['positionSide'] == 'LONG' else ((entry_price - current_price) / entry_price) * 100 *leverage
            print(symbol,roi)
            if roi >15:
                stop_loss_price = entry_price
                # Создаем стоп-лосс ордер
                client.futures_create_order(
                    symbol=symbol,
                    side='SELL' if pos['positionSide'] == 'LONG' else 'BUY',
                    type='STOP_MARKET',
                    quantity=abs(position_amt),
                    stopPrice=stop_loss_price,
                    positionSide=pos['positionSide']
                )
                message=(f"Updated STOP_LOSS order for {symbol} to breakeven at price {stop_loss_price}")
                send_telegram_message(message)

            if not has_take_profit or not has_stop_loss:
                step_size, tick_size, min_notional = get_symbol_info(client, symbol)
                balance = get_account_balance(client)
                position_size = calculate_position_size(balance, position_size_percent, leverage, current_price, step_size, min_notional)
                take_profit_price, stop_loss_price = calculate_prices(current_price, take_profit_percent, stop_loss_percent, pos['positionSide'], tick_size)

                if not has_take_profit:
                    client.futures_create_order(
                        symbol=symbol,
                        side='SELL' if pos['positionSide'] == 'LONG' else 'BUY',
                        type='TAKE_PROFIT_MARKET',
                        quantity=position_size,
                        stopPrice=take_profit_price,
                        
                        positionSide=pos['positionSide']
                    )
                    logging.info(f"Created TAKE_PROFIT order for {symbol} at {take_profit_price}")

                if not has_stop_loss:
                    client.futures_create_order(
                        symbol=symbol,
                        side='SELL' if pos['positionSide'] == 'LONG' else 'BUY',
                        type='STOP_MARKET',
                        quantity=position_size,
                        stopPrice=stop_loss_price,
                        
                        positionSide=pos['positionSide']
                    )
                    logging.info(f"Created STOP_LOSS order for {symbol} at {stop_loss_price}")

    except Exception as e:
        logging.error(f"Error ensuring stop loss and take profit orders: {e}")



def cancel_take_profit_stop_loss_orders(client, trading_pair):
    try:
        trading_pair = trading_pair.replace(':USDT', '').replace('/', '')  # Clean symbol
        open_orders = client.futures_get_open_orders(symbol=trading_pair)
        for order in open_orders:
            if order['type'] in ['TAKE_PROFIT_MARKET', 'STOP_MARKET']:
                client.futures_cancel_order(symbol=trading_pair, orderId=order['orderId'])
                logging.info(f"Cancelled {order['type']} order for {trading_pair}.")
    except Exception as e:
        logging.error(f"Error cancelling take profit and stop loss orders for {trading_pair}: {e}")

def create_orders(client, trading_pair, position_size, take_profit_price, stop_loss_price, position_side_setting, position_side):
    max_retries = 5
    trading_pair = trading_pair.replace(':USDT', '').replace('/', '')  # Clean symbol

    for attempt in range(max_retries):
        try:
            # Check for existing order
            if trading_pair in open_orders and (datetime.now() - open_orders[trading_pair]).total_seconds() < 43200:  # 12 hours = 43200 seconds
                logging.info(f"Order for pair {trading_pair} already exists. Skipping...")
                return

            # Check number of open positions
            if position_side == 'LONG':
                open_positions_count = count_open_positions(client, 'LONG')
            elif position_side == 'SHORT':
                open_positions_count = count_open_positions(client, 'SHORT')
            else:
                logging.error(f"Invalid position_side: {position_side}")
                return

            if open_positions_count is None or open_positions_count >= 5:
                logging.info(f"Exceeded number of open {position_side} positions. Skipping...")
                return

            # Create market order
            market_order = client.futures_create_order(
                symbol=trading_pair,
                side=Client.SIDE_BUY if position_side == 'LONG' else Client.SIDE_SELL,
                type=Client.ORDER_TYPE_MARKET,
                quantity=position_size,
                positionSide=position_side_setting
            )
            logging.info("Market order successfully created:")
            logging.info(market_order)

            # Record order open time
            open_orders[trading_pair] = datetime.now()

            # Send Telegram message and log account balance and order details
            balance = get_account_balance(client)
            message = (
                f"Opened {position_side} order for pair {trading_pair}\n"
                f"Position size: {position_size}\n"
                f"Take profit price: {take_profit_price}\n"
                f"Stop loss price: {stop_loss_price}\n"
                f"Current balance: {balance} USDT"
            )
            send_telegram_message(message)

            # Ensure take profit and stop loss prices are valid
            current_price = float(client.get_symbol_ticker(symbol=trading_pair)['price'])
            if (position_side == 'LONG' and (take_profit_price <= current_price or stop_loss_price >= current_price)) or \
               (position_side == 'SHORT' and (take_profit_price >= current_price or stop_loss_price <= current_price)):
                logging.error(f"Invalid take profit or stop loss price for {position_side} order: {trading_pair}")
                return

            # Cancel existing take profit and stop loss orders
            cancel_take_profit_stop_loss_orders(client, trading_pair)

            # Create take profit order
            for attempt_tp in range(max_retries):
                try:
                    tp_order = client.futures_create_order(
                        symbol=trading_pair,
                        side=Client.SIDE_SELL if position_side == 'LONG' else Client.SIDE_BUY,
                        type="TAKE_PROFIT_MARKET",
                        quantity=position_size,
                        stopPrice=take_profit_price,
                        positionSide=position_side_setting
                    )
                    message = "Take profit order created for pair " + trading_pair
                    send_telegram_message(message)
                    logging.info(tp_order)
                    break
                except (ConnectionError, HTTPError) as e:
                    logging.error(f"Error creating take profit order: {e}. Attempt {attempt_tp + 1} of {max_retries}")
                    time.sleep(5)
                    continue

            # Create stop loss order
            for attempt_sl in range(max_retries):
                try:
                    sl_order = client.futures_create_order(
                        symbol=trading_pair,
                        side=Client.SIDE_SELL if position_side == 'LONG' else Client.SIDE_BUY,
                        type="STOP_MARKET",
                        quantity=position_size,
                        stopPrice=stop_loss_price,
                        positionSide=position_side_setting
                    )
                    message = "Stop loss order created for pair " + trading_pair
                    send_telegram_message(message)
                    logging.info(sl_order)
                    break
                except (ConnectionError, HTTPError) as e:
                    logging.error(f"Error creating stop loss order: {e}. Attempt {attempt_sl + 1} of {max_retries}")
                    time.sleep(5)
                    continue

            return  # Exit loop if all orders are successfully created

        except (ConnectionError, HTTPError) as e:
            send_telegram_message(f"Error creating order: {e}. Attempt {attempt + 1} of {max_retries}")
            time.sleep(5)  # Pause before retrying

    logging.error(f"Failed to create orders after {max_retries} attempts.")


def main():
    # Send Telegram message when bot starts
    send_telegram_message("Bot started and ready for operation.")

    while True:
        try:
            # Clean up orders and ensure stop loss and take profit orders
            cleanup_orders(binance_client)
            ensure_stop_loss_take_profit(binance_client)

            markets = exchange.load_markets()
            usdt_pairs = [symbol for symbol in markets if symbol.endswith('USDT')]

            for symbol in usdt_pairs:
                try:
                    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    logging.info(f"Processing pair: {symbol} on 15m timeframe at {current_time}")
                    print(f"Processing pair: {symbol} on 15m timeframe at {current_time}")

                    df = fetch_ohlcv(symbol)
                    if df is None:
                        continue

                    df = calculate_indicators(df)
                    if df is None:
                        continue

                    signal, price, position_side = check_signals(df)

                    if signal:
                        symbol = symbol.replace('/', '')
                        message = f"🟦🟦🟦{symbol} {signal} at price {price}. Position side: {position_side}🟦🟦🟦"
                        logging.info(message)
                        print(message)

                        step_size, tick_size, min_notional = get_symbol_info(binance_client, symbol)
                        if step_size is None or min_notional is None:
                            continue

                        set_margin_mode(binance_client, symbol, margin_mode)

                        balance = get_account_balance(binance_client)
                        binance_client.futures_change_leverage(symbol=symbol, leverage=leverage)

                        ticker = binance_client.get_symbol_ticker(symbol=symbol)
                        current_price = float(ticker['price'])

                        position_size = calculate_position_size(balance, position_size_percent, leverage, current_price, step_size, min_notional)
                        if position_size is None:
                            continue

                        take_profit_price, stop_loss_price = calculate_prices(current_price, take_profit_percent, stop_loss_percent, position_side, tick_size)

                        position_mode = binance_client.futures_get_position_mode()
                        if position_side == 'LONG':
                            position_side_setting = 'BOTH' if not position_mode['dualSidePosition'] else 'LONG'
                        elif position_side == 'SHORT':
                            position_side_setting = 'BOTH' if not position_mode['dualSidePosition'] else 'SHORT'
                        else:
                            logging.error(f"Invalid position_side in configuration: {position_side}")
                            continue

                        create_orders(binance_client, symbol, position_size, take_profit_price, stop_loss_price, position_side_setting, position_side)

                except Exception as e:
                    logging.error(f"Error processing pair {symbol}: {e}")

        except Exception as e:
            logging.error(f"Error loading markets: {e}")

        time.sleep(30)  # Pause for 30 seconds before re-checking



if __name__ == "__main__":
    main()
