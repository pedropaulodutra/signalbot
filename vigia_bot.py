import os
import time
import pandas as pd
import requests
from binance.client import Client
from colorama import init, Fore
from flask import Flask
import threading

# Inicializa o colorama
init(autoreset=True)

# ==============================================================================
# --- CONFIGURAÇÕES DO BOT ---
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEFRAME = '15m'
SMA_PERIOD = 20
VOLUME_THRESHOLD = 7.0
MIN_HOURLY_VOLUME_USDT = 5000000
SCAN_INTERVAL_MINUTES = 15
ALERT_ON_EMA_PROXIMITY = True
EMA_FAST_PERIOD = 6
EMA_SLOW_PERIOD = 12
PROXIMITY_THRESHOLD_PERCENT = 0.1

# ==============================================================================
# --- LÓGICA DO TELEGRAM E DO BOT (Nenhuma mudança aqui) ---
# ==============================================================================

def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(Fore.RED + "AVISO: Segredos do Telegram não configurados.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(Fore.GREEN + f"Alerta enviado: {message.splitlines()[2]}") # Mostra a linha da moeda no log
    except Exception as e:
        print(Fore.RED + f"Falha ao enviar alerta: {e}")

class MarketScanner:
    def __init__(self):
        print("Iniciando o Vigia de Mercado...")
        self.client = Client()
        self.alerted_volume = set()
        self.alerted_proximity = set()
        self.all_symbols = self._get_all_perp_symbols()

    def _get_all_perp_symbols(self):
        print("Buscando lista completa de símbolos de futuros...")
        try:
            exchange_info = self.client.futures_exchange_info()
            return [s['symbol'] for s in exchange_info['symbols'] if s['quoteAsset'] == 'USDT' and s['contractType'] == 'PERPETUAL']
        except Exception as e:
            print(Fore.RED + f"Erro ao buscar lista de símbolos: {e}")
            return []

    def get_target_symbols_by_hourly_volume(self):
        print("Filtrando símbolos por volume da última hora...")
        target_symbols = []
        candles_per_hour = 60 // int(TIMEFRAME.replace('m', ''))
        for symbol in self.all_symbols:
            try:
                klines = self.client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=candles_per_hour)
                if len(klines) < candles_per_hour: continue
                hourly_volume = sum(float(k[7]) for k in klines)
                if hourly_volume > MIN_HOURLY_VOLUME_USDT:
                    target_symbols.append(symbol)
                time.sleep(0.1) # Pausa menor para acelerar o filtro
            except Exception: pass
        print(f"Encontrados {len(target_symbols)} símbolos com volume relevante.")
        return target_symbols

    def check_imminent_ema_cross(self, symbol, df):
        try:
            df['ema_fast'] = df['close'].ewm(span=EMA_FAST_PERIOD, adjust=False).mean()
            df['ema_slow'] = df['close'].ewm(span=EMA_SLOW_PERIOD, adjust=False).mean()

            ema_fast_current, ema_slow_current = df[['ema_fast', 'ema_slow']].iloc[-2]
            ema_fast_previous, ema_slow_previous = df[['ema_fast', 'ema_slow']].iloc[-3]
            
            distance_percent = (abs(ema_fast_current - ema_slow_current) / ema_slow_current) * 100
            are_close = distance_percent < PROXIMITY_THRESHOLD_PERCENT
            are_converging = abs(ema_fast_current - ema_slow_current) < abs(ema_fast_previous - ema_slow_previous)

            if are_close and are_converging and symbol not in self.alerted_proximity:
                direction = "para CIMA 📈" if ema_fast_current > ema_slow_current else "para BAIXO 📉"
                message = f"⏳ *Alerta de Cruzamento Iminente* ⏳\n\n*Moeda:* `{symbol}`\n*Timeframe:* `{TIMEFRAME}`\n\nAs EMAs ({EMA_FAST_PERIOD} e {EMA_SLOW_PERIOD}) estão a `{distance_percent:.4f}%` e se aproximando para um possível cruzamento {direction}."
                send_telegram_alert(message)
                self.alerted_proximity.add(symbol)
        except Exception: pass

    def analyze_symbol_for_volume(self, symbol, df):
        try:
            last_closed_volume = df['volume'].iloc[-2]
            average_volume = df['volume'].iloc[-(SMA_PERIOD + 2):-2].mean()

            if average_volume > 0 and last_closed_volume > (average_volume * VOLUME_THRESHOLD) and symbol not in self.alerted_volume:
                aumento_x = last_closed_volume / average_volume
                message = f"🔊 *Pico de Volume Detectado* 🔊\n\n*Moeda:* `{symbol}`\n*Timeframe:* `{TIMEFRAME}`\n\nO volume foi `~{aumento_x:.1f}x` maior que a média."
                send_telegram_alert(message)
                self.alerted_volume.add(symbol)
        except Exception: pass

    def start_scanner_loop(self):
        print(Fore.YELLOW + "\n--- VIGIA DE MERCADO (VOLUME E CRUZAMENTO) INICIADO ---")
        while True:
            print("\n" + Fore.CYAN + f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Iniciando novo ciclo de varredura...")
            self.alerted_volume.clear()
            self.alerted_proximity.clear()
            
            symbols_to_scan = self.get_target_symbols_by_hourly_volume()
            
            print(f"Iniciando análise de {len(symbols_to_scan)} símbolos filtrados...")
            for i, symbol in enumerate(symbols_to_scan):
                print(f"Analisando [{i+1}/{len(symbols_to_scan)}]: {symbol}...")
                try:
                    klines = self.client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=max(SMA_PERIOD + 2, EMA_SLOW_PERIOD + 2))
                    if not klines or len(klines) < max(SMA_PERIOD + 2, EMA_SLOW_PERIOD + 2): continue

                    df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_asset_volume', 'number_of_trades', 'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
                    df[['volume', 'close']] = df[['volume', 'close']].apply(pd.to_numeric)
                    
                    self.analyze_symbol_for_volume(symbol, df)
                    if ALERT_ON_EMA_PROXIMITY:
                        self.check_imminent_ema_cross(symbol, df)
                    time.sleep(0.5)
                except Exception: pass

            print(Fore.CYAN + f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Ciclo completo. Aguardando {SCAN_INTERVAL_MINUTES} minutos.")
            time.sleep(SCAN_INTERVAL_MINUTES * 60)

# ==============================================================================
# --- Parte 3: O "Disfarce" de Site (Flask) ---
# ==============================================================================

# Cria a aplicação web de fachada
app = Flask(__name__)

@app.route('/')
def home():
    """Esta é a página web que o Render vai verificar para saber se o serviço está 'saudável'."""
    return "O Bot Vigia de Mercado está ativo e rodando em segundo plano."

def run_bot():
    """Função que cria e inicia o nosso bot."""
    scanner = MarketScanner()
    scanner.start_scanner_loop()

if __name__ == "__main__":
    # Inicia o bot em uma 'thread' separada.
    # Isso significa que o bot rodará em segundo plano, de forma independente do site de fachada.
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Inicia o site de fachada, que manterá o serviço do Render ativo.
    # O Render precisa que um serviço web escute em uma porta, e é isso que esta linha faz.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))