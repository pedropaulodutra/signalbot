import os
import time
import pandas as pd
import requests
from binance.client import Client
from colorama import init, Fore

init(autoreset=True)

# ==============================================================================
# --- CONFIGURAÇÕES DO BOT ---
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Parâmetros da Estratégia de Volume
TIMEFRAME = '15m'
SMA_PERIOD = 20
VOLUME_THRESHOLD = 7.0
MIN_HOURLY_VOLUME_USDT = 5000000
SCAN_INTERVAL_MINUTES = 15

# <-- MUDANÇA: Novas configurações para o alerta de cruzamento de EMA
ALERT_ON_EMA_PROXIMITY = True       # Mude para False se não quiser este tipo de alerta
EMA_FAST_PERIOD = 6                 # EMA Rápida
EMA_SLOW_PERIOD = 12                # EMA Lenta
PROXIMITY_THRESHOLD_PERCENT = 0.1   # 🚨 Alerta se as EMAs estiverem a menos de 0.1% de distância uma da outra

# ==============================================================================

def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(Fore.RED + "AVISO: Segredos do Telegram não configurados.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(Fore.GREEN + "Alerta enviado com sucesso para o Telegram!")
    except Exception as e:
        print(Fore.RED + f"Falha ao enviar alerta: {e}")

class VolumeScanner:
    def __init__(self):
        print("Iniciando o Vigia de Volume e Cruzamento...")
        self.client = Client()
        self.alerted_volume = set()
        # <-- MUDANÇA: Um novo set de memória para os alertas de cruzamento
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
                time.sleep(0.2)
            except Exception: pass
        print(f"Encontrados {len(target_symbols)} símbolos com volume relevante.")
        return target_symbols
    
    # <-- MUDANÇA: Nova função para checar o pré-cruzamento
    def check_imminent_ema_cross(self, symbol, df):
        """Verifica se as EMAs estão muito próximas e convergindo."""
        try:
            # Calcula as duas EMAs
            df[f'ema_fast'] = df['close'].ewm(span=EMA_FAST_PERIOD, adjust=False).mean()
            df[f'ema_slow'] = df['close'].ewm(span=EMA_SLOW_PERIOD, adjust=False).mean()

            # Pega os valores da última vela fechada e da anterior a ela
            ema_fast_current = df[f'ema_fast'].iloc[-2]
            ema_slow_current = df[f'ema_slow'].iloc[-2]
            ema_fast_previous = df[f'ema_fast'].iloc[-3]
            ema_slow_previous = df[f'ema_slow'].iloc[-3]
            
            # 1. CONDIÇÃO DE PROXIMIDADE
            distance_percent = (abs(ema_fast_current - ema_slow_current) / ema_slow_current) * 100
            are_close = distance_percent < PROXIMITY_THRESHOLD_PERCENT
            
            # 2. CONDIÇÃO DE CONVERGÊNCIA
            previous_distance = abs(ema_fast_previous - ema_slow_previous)
            current_distance = abs(ema_fast_current - ema_slow_current)
            are_converging = current_distance < previous_distance

            # GATILHO DO ALERTA
            if are_close and are_converging:
                if symbol not in self.alerted_proximity:
                    direction = "para CIMA 📈" if ema_fast_current > ema_slow_current else "para BAIXO 📉"
                    
                    print(Fore.MAGENTA + f"ALERTA DE PROXIMIDADE! {symbol} | Dist: {distance_percent:.4f}% | Direção: {direction}")
                    
                    message = f"⏳ *Alerta de Cruzamento Iminente* ⏳\n\n*Moeda:* `{symbol}`\n*Timeframe:* `{TIMEFRAME}`\n\nAs EMAs ({EMA_FAST_PERIOD} e {EMA_SLOW_PERIOD}) estão a apenas `{distance_percent:.4f}%` de distância e se aproximando para um possível cruzamento {direction}."
                    send_telegram_alert(message)
                    
                    self.alerted_proximity.add(symbol)

        except Exception as e:
            # print(f"Erro ao analisar proximidade de EMA para {symbol}: {e}")
            pass

    def analyze_symbol_for_volume(self, symbol, df):
        """Analisa o PICO DE VOLUME para um símbolo."""
        try:
            last_closed_volume = df['volume'].iloc[-2]
            average_volume = df['volume'].iloc[-(SMA_PERIOD + 2):-2].mean()

            if average_volume > 0 and last_closed_volume > (average_volume * VOLUME_THRESHOLD):
                if symbol not in self.alerted_volume:
                    aumento_x = last_closed_volume / average_volume
                    print(Fore.GREEN + f"ALERTA DE VOLUME! {symbol} | Aumento: {aumento_x:.1f}x")
                    message = f"🔊 *Pico de Volume Detectado* 🔊\n\n*Moeda:* `{symbol}`\n*Timeframe:* `{TIMEFRAME}`\n\nO volume foi `~{aumento_x:.1f}x` maior que a média."
                    send_telegram_alert(message)
                    self.alerted_volume.add(symbol)
        except Exception:
            pass

    def start(self):
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
                    # Pega os dados uma única vez para ambas as análises
                    klines = self.client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=max(SMA_PERIOD + 2, EMA_SLOW_PERIOD + 2))
                    if len(klines) < max(SMA_PERIOD + 2, EMA_SLOW_PERIOD + 2): continue

                    df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_asset_volume', 'number_of_trades', 'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
                    df['volume'] = pd.to_numeric(df['volume'])
                    df['close'] = pd.to_numeric(df['close'])
                    
                    # Roda as duas análises
                    self.analyze_symbol_for_volume(symbol, df)
                    if ALERT_ON_EMA_PROXIMITY:
                        self.check_imminent_ema_cross(symbol, df)

                    time.sleep(0.5)
                except Exception:
                    pass

            print(Fore.CYAN + f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Ciclo completo. Aguardando {SCAN_INTERVAL_MINUTES} minutos.")
            time.sleep(SCAN_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    scanner = VolumeScanner()
    scanner.start()