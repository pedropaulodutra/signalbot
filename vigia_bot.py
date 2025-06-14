import os
import time
import pandas as pd
import pandas_ta as ta
import requests
from binance.client import Client
from colorama import init, Fore
from flask import Flask
import threading

# Inicializa o colorama
init(autoreset=True)

# ==============================================================================
# --- CONFIGURAÇÕES DO BOT DE CONFLUÊNCIA ---
# ==============================================================================
# Credenciais lidas do ambiente de hospedagem (Render, etc.)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Configurações Gerais
TIMEFRAME = '15m'
SCAN_INTERVAL_MINUTES = 15
MIN_HOURLY_VOLUME_USDT = 500

# Parâmetros para o Alerta de Confluência
RSI_PERIOD = 14
RSI_NEUTRAL_UPPER = 55
RSI_NEUTRAL_LOWER = 45
RSI_CONSOLIDATION_PERIOD = 5 # Nº de velas que o RSI precisa estar na zona neutra antes do rompimento
VOLUME_SHORT_SMA = 5
VOLUME_LONG_SMA = 20
BREAKOUT_LOOKBACK_PERIOD = 50 # Período para checar as máximas/mínimas do preço

# ==============================================================================
# --- FUNÇÕES E CLASSES DO BOT ---
# ==============================================================================

def send_telegram_alert(message):
    """Envia uma mensagem de alerta formatada para o Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(Fore.RED + "AVISO: Segredos do Telegram não configurados.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=10)
        log_message = message.splitlines()[2] if len(message.splitlines()) > 2 else message
        print(Fore.GREEN + f"Alerta enviado: {log_message}")
    except Exception as e:
        print(Fore.RED + f"Falha ao enviar alerta: {e}")

class MarketScanner:
    """Escaneia o mercado em busca de um único sinal de alta confluência."""
    def __init__(self):
        print("Iniciando o Vigia de Sinais de Confluência...")
        self.client = Client()
        self.alerted_symbols_in_cycle = set()
        self.all_symbols = self._get_all_perp_symbols()

    def _get_all_perp_symbols(self):
        """Pega a lista de todos os símbolos de futuros perpétuos USDT."""
        print("Buscando lista completa de símbolos de futuros...")
        try:
            exchange_info = self.client.futures_exchange_info()
            return [s['symbol'] for s in exchange_info['symbols'] if s['quoteAsset'] == 'USDT' and s['contractType'] == 'PERPETUAL']
        except Exception as e:
            print(Fore.RED + f"Erro ao buscar lista de símbolos: {e}")
            return []

    def get_target_symbols_by_hourly_volume(self):
        """Filtra os símbolos, mantendo apenas aqueles com volume relevante na última hora."""
        print("Filtrando símbolos por volume da última hora...")
        target_symbols = []
        candles_per_hour = 60 // int(TIMEFRAME.replace('m', ''))
        for symbol in self.all_symbols:
            try:
                klines = self.client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=candles_per_hour)
                if len(klines) < candles_per_hour: continue
                hourly_volume = sum(float(k[7]) for k in klines) # k[7] é o 'quote_asset_volume'
                if hourly_volume > MIN_HOURLY_VOLUME_USDT:
                    target_symbols.append(symbol)
                time.sleep(0.1)
            except Exception: pass
        print(f"Encontrados {len(target_symbols)} símbolos com volume relevante para análise.")
        return target_symbols
    
    def analyze_for_confluence_signal(self, symbol):
        """Analisa um símbolo para o sinal de confluência de 4 fatores."""
        if symbol in self.alerted_symbols_in_cycle: return

        try:
            # Pega um histórico maior para garantir que todos os indicadores possam ser calculados
            limit = 205 # Suficiente para EMA 200 e outros lookbacks
            klines = self.client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=limit)
            if not klines or len(klines) < limit: return

            df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'ct', 'qav', 'nt', 'tbbav', 'tbqav', 'ig'])
            df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].apply(pd.to_numeric)
            
            # --- CÁLCULO DOS INDICADORES NECESSÁRIOS ---
            df.ta.ema(length=6, append=True)
            df.ta.ema(length=12, append=True)
            df.ta.ema(length=200, append=True)
            df.ta.rsi(length=RSI_PERIOD, append=True)
            df.ta.sma(length=VOLUME_SHORT_SMA, close='volume', append=True)
            df.ta.sma(length=VOLUME_LONG_SMA, close='volume', append=True)
            
            # Pega os dados da última vela fechada
            current = df.iloc[-2]

            # --- CONDIÇÃO 1: Saída do RSI da Zona Neutra ---
            last_rsis = df[f'RSI_{RSI_PERIOD}'].iloc[-(RSI_CONSOLIDATION_PERIOD + 2):-2]
            was_neutral = all(RSI_NEUTRAL_LOWER < rsi < RSI_NEUTRAL_UPPER for rsi in last_rsis)
            bullish_rsi_breakout = was_neutral and current[f'RSI_{RSI_PERIOD}'] > RSI_NEUTRAL_UPPER
            bearish_rsi_breakout = was_neutral and current[f'RSI_{RSI_PERIOD}'] < RSI_NEUTRAL_LOWER
            if not (bullish_rsi_breakout or bearish_rsi_breakout): return

            # --- CONDIÇÃO 2: Alinhamento das EMAs (6, 12, 200) ---
            bullish_emas_aligned = current['EMA_6'] > current['EMA_12'] > current['EMA_200']
            bearish_emas_aligned = current['EMA_6'] < current['EMA_12'] < current['EMA_200']
            if not (bullish_emas_aligned or bearish_emas_aligned): return

            # --- CONDIÇÃO 3: Aumento do Volume Médio ---
            if not (current[f'SMA_{VOLUME_SHORT_SMA}_volume'] > current[f'SMA_{VOLUME_LONG_SMA}_volume']): return

            # --- CONDIÇÃO 4: Rompimento de Suporte/Resistência ---
            highest_high = df['high'].iloc[-(BREAKOUT_LOOKBACK_PERIOD + 2):-2].max()
            lowest_low = df['low'].iloc[-(BREAKOUT_LOOKBACK_PERIOD + 2):-2].min()
            price_breakout_up = current['close'] > highest_high
            price_breakout_down = current['close'] < lowest_low
            if not (price_breakout_up or price_breakout_down): return
            
            # --- CHECAGEM FINAL: Todos os sinais devem apontar para a mesma direção ---
            direction = None
            if bullish_rsi_breakout and bullish_emas_aligned and price_breakout_up:
                direction = "Alta 🟢"
            elif bearish_rsi_breakout and bearish_emas_aligned and price_breakout_down:
                direction = "Baixa 🔴"

            if direction:
                message = (
                    f"🔥 *Super-Sinal de Confluência!* 🔥\n\n"
                    f"*Moeda:* `{symbol}`\n"
                    f"*Direção:* *{direction}*\n\n"
                    f"*{'='*25}*\n"
                    f"✅ *RSI:* Saiu da zona neutra (45-55).\n"
                    f"✅ *EMAs:* 6, 12 e 200 alinhadas.\n"
                    f"✅ *Volume:* Média móvel curta acima da longa.\n"
                    f"✅ *Preço:* Rompeu a máxima/mínima recente."
                )
                send_telegram_alert(message)
                self.alerted_symbols_in_cycle.add(symbol)

        except Exception:
            pass

    def start_scanner_loop(self):
        """O loop principal que orquestra todo o trabalho do bot."""
        print(Fore.YELLOW + "\n--- VIGIA DE CONFLUÊNCIA INICIADO ---")
        while True:
            print("\n" + Fore.CYAN + f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Iniciando novo ciclo de varredura...")
            self.alerted_symbols_in_cycle.clear()
            symbols_to_scan = self.get_target_symbols_by_hourly_volume()
            
            print(f"Iniciando análise de {len(symbols_to_scan)} símbolos filtrados...")
            for i, symbol in enumerate(symbols_to_scan):
                print(f"Analisando [{i+1}/{len(symbols_to_scan)}]: {symbol}...")
                self.analyze_for_confluence_signal(symbol)
                time.sleep(0.5)

            print(Fore.CYAN + f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Ciclo completo. Aguardando {SCAN_INTERVAL_MINUTES} minutos.")
            time.sleep(SCAN_INTERVAL_MINUTES * 60)

# ==============================================================================
# --- SERVIDOR WEB DE FACHADA E INICIALIZAÇÃO ---
# ==============================================================================
app = Flask(__name__)
@app.route('/')
def home():
    """Página web mínima para manter o serviço do Render ativo."""
    return "O Bot Vigia de Sinais de Confluência está ativo."

def run_bot():
    """Função para iniciar o scanner em uma thread separada."""
    scanner = MarketScanner()
    scanner.start_scanner_loop()

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))