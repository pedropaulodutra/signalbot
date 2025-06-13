import os
import time
import pandas as pd
import pandas_ta as ta
import requests
from binance.client import Client
from colorama import init, Fore
from flask import Flask
import threading

# Inicializa o colorama para o terminal
init(autoreset=True)

# ==============================================================================
# --- CONFIGURAÇÕES DO BOT AVANÇADO ---
# ==============================================================================
# As credenciais serão lidas do cofre de segredos do seu ambiente de hospedagem (Render, Codespaces, etc.)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- Configurações Gerais ---
TIMEFRAME = '15m'
SCAN_INTERVAL_MINUTES = 15
MIN_HOURLY_VOLUME_USDT = 5000000  # Ignora moedas com menos de 5M de volume na última hora

# --- LÓGICA 1: Alerta de Volume com Rompimento de Preço ---
ALERT_ON_BREAKOUT_VOLUME = True
VOLUME_SMA_PERIOD = 20
VOLUME_THRESHOLD = 7.0      # Volume 7x maior que a média
BREAKOUT_LOOKBACK_PERIOD = 50 # Período para checar as máximas/mínimas (50 velas)

# --- LÓGICA 2: Alerta de Cruzamento de EMA com Filtro de Tendência ---
ALERT_ON_TREND_FILTERED_CROSS = True
EMA_FAST_PERIOD = 6
EMA_SLOW_PERIOD = 12
TREND_FILTER_EMA_PERIOD = 50  # EMA longa para definir a tendência principal
PROXIMITY_THRESHOLD_PERCENT = 0.15 # Limiar para o alerta de "quase cruzamento"

# --- LÓGICA 3: Alerta de Squeeze das Bandas de Bollinger ---
ALERT_ON_BBAND_SQUEEZE = True
BBAND_LENGTH = 20
BBAND_STD_DEV = 2.0
SQUEEZE_LOOKBACK_PERIOD = 90 # Período para encontrar a "calmaria"

# ==============================================================================
# --- FUNÇÕES E CLASSES DO BOT ---
# ==============================================================================

def send_telegram_alert(message):
    """Envia uma mensagem de alerta formatada para o Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(Fore.RED + "AVISO: Segredos do Telegram não configurados.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message,
            "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=10)
        # Extrai a linha principal da mensagem para um log mais limpo
        log_message = message.splitlines()[2] if len(
            message.splitlines()) > 2 else message
        print(Fore.GREEN + f"Alerta enviado: {log_message}")
    except Exception as e:
        print(Fore.RED + f"Falha ao enviar alerta: {e}")


class MarketScanner:
    """Encapsula toda a lógica de escaneamento do mercado."""

    def __init__(self):
        print("Iniciando o Vigia de Mercado Avançado...")
        self.client = Client()
        self.alerted_symbols = {}  # Dicionário para gerenciar alertas por tipo e evitar spam
        self.all_symbols = self._get_all_perp_symbols()

    def _get_all_perp_symbols(self):
        """Pega a lista de todos os símbolos de futuros perpétuos USDT uma vez."""
        print("Buscando lista completa de símbolos de futuros...")
        try:
            exchange_info = self.client.futures_exchange_info()
            return [s['symbol'] for s in exchange_info['symbols'] if s['quoteAsset'] == 'USDT' and s['contractType'] == 'PERPETUAL']
        except Exception as e:
            print(Fore.RED + f"Erro ao buscar lista de símbolos: {e}")
            return []

    def get_target_symbols_by_hourly_volume(self):
        """Filtra a lista de símbolos, mantendo apenas aqueles com volume relevante na última hora."""
        print("Filtrando símbolos por volume da última hora...")
        target_symbols = []
        candles_per_hour = 60 // int(TIMEFRAME.replace('m', ''))
        for symbol in self.all_symbols:
            try:
                klines = self.client.futures_klines(
                    symbol=symbol, interval=TIMEFRAME, limit=candles_per_hour)
                if len(klines) < candles_per_hour:
                    continue
                # k[7] é o 'quote_asset_volume'
                hourly_volume = sum(float(k[7]) for k in klines)
                if hourly_volume > MIN_HOURLY_VOLUME_USDT:
                    target_symbols.append(symbol)
                time.sleep(0.1)
            except Exception:
                pass
        print(
            f"Encontrados {len(target_symbols)} símbolos com volume relevante para análise.")
        return target_symbols

    def analyze_and_alert(self, symbol):
        """Pega os dados, calcula todos os indicadores e chama as funções de verificação de alerta."""
        try:
            # Pega um histórico maior para garantir que todos os indicadores possam ser calculados
            limit = max(SQUEEZE_LOOKBACK_PERIOD,
                        BREAKOUT_LOOKBACK_PERIOD, TREND_FILTER_EMA_PERIOD) + 5
            klines = self.client.futures_klines(
                symbol=symbol, interval=TIMEFRAME, limit=limit)
            if not klines or len(klines) < limit:
                return

            # Converte para DataFrame do Pandas
            df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time',
                            'quote_asset_volume', 'number_of_trades', 'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
            df['open'] = pd.to_numeric(df['open'])
            df['high'] = pd.to_numeric(df['high'])
            df['low'] = pd.to_numeric(df['low'])
            df['close'] = pd.to_numeric(df['close'])
            df['volume'] = pd.to_numeric(df['volume'])

            # --- CÁLCULO DE TODOS OS INDICADORES DE UMA VEZ USANDO PANDAS-TA ---
            df.ta.ema(length=EMA_FAST_PERIOD, append=True)
            df.ta.ema(length=EMA_SLOW_PERIOD, append=True)
            df.ta.ema(length=TREND_FILTER_EMA_PERIOD, append=True)
            df.ta.sma(length=VOLUME_SMA_PERIOD, close='volume', append=True)
            df.ta.bbands(length=BBAND_LENGTH, std=BBAND_STD_DEV, append=True)

            # --- VERIFICAÇÃO DAS LÓGICAS DE ALERTA (se estiverem ativadas) ---
            if ALERT_ON_BREAKOUT_VOLUME:
                self.check_volume_breakout(symbol, df)
            if ALERT_ON_TREND_FILTERED_CROSS:
                self.check_trend_filtered_cross(symbol, df)
            if ALERT_ON_BBAND_SQUEEZE:
                self.check_bband_squeeze(symbol, df)

        except Exception:
            pass

    def check_volume_breakout(self, symbol, df):
        """Alerta sobre picos de volume que resultam em rompimentos de preço."""
        if symbol in self.alerted_symbols.get("volume_breakout", []):
            return
        last = df.iloc[-2]
        volume_avg = last[f'SMA_{VOLUME_SMA_PERIOD}']
        if volume_avg and last['volume'] > volume_avg * VOLUME_THRESHOLD:
            highest_high = df['high'].iloc[-(
                BREAKOUT_LOOKBACK_PERIOD+2):-2].max()
            lowest_low = df['low'].iloc[-(BREAKOUT_LOOKBACK_PERIOD+2):-2].min()
            breakout_type = "Rompimento de Alta 📈" if last[
                'close'] > highest_high else "Rompimento de Baixa 📉" if last['close'] < lowest_low else None
            if breakout_type:
                aumento_x = last['volume'] / volume_avg
                message = f"💥 *Volume com Rompimento!* 💥\n\n*Moeda:* `{symbol}`\n*Preço:* `${last['close']:.4f}`\n\n*Sinal:* {breakout_type}\n*Detalhe:* Volume `~{aumento_x:.1f}x` acima da média no rompimento da máxima/mínima das últimas {BREAKOUT_LOOKBACK_PERIOD} velas."
                send_telegram_alert(message)
                self.alerted_symbols.setdefault(
                    "volume_breakout", set()).add(symbol)

    def check_trend_filtered_cross(self, symbol, df):
        """Alerta sobre cruzamentos de EMA iminentes que estão a favor da tendência principal."""
        if symbol in self.alerted_symbols.get("ema_cross", []):
            return
        current, previous = df.iloc[-2], df.iloc[-3]
        ema_fast, ema_slow, ema_trend = current[f'EMA_{EMA_FAST_PERIOD}'], current[
            f'EMA_{EMA_SLOW_PERIOD}'], current[f'EMA_{TREND_FILTER_EMA_PERIOD}']
        distance_percent = (abs(ema_fast - ema_slow) / ema_slow) * 100
        are_close = distance_percent < PROXIMITY_THRESHOLD_PERCENT
        are_converging = abs(ema_fast - ema_slow) < abs(
            previous[f'EMA_{EMA_FAST_PERIOD}'] - previous[f'EMA_{EMA_SLOW_PERIOD}'])
        if are_close and are_converging:
            is_uptrend, is_downtrend = ema_slow > ema_trend, ema_slow < ema_trend
            direction = "Alta (a favor da tendência) 📈" if is_uptrend and ema_fast > ema_slow else "Baixa (a favor da tendência) 📉" if is_downtrend and ema_fast < ema_slow else None
            if direction:
                message = f"🧭 *Cruzamento Iminente com Tendência!* 🧭\n\n*Moeda:* `{symbol}`\n*Timeframe:* `{TIMEFRAME}`\n\n*Sinal:* As EMAs (`{EMA_FAST_PERIOD}`/`{EMA_SLOW_PERIOD}`) estão prestes a cruzar na direção de {direction}"
                send_telegram_alert(message)
                self.alerted_symbols.setdefault("ema_cross", set()).add(symbol)

    def check_bband_squeeze(self, symbol, df):
        """Alerta quando as Bandas de Bollinger estão extremamente 'apertadas'."""
        if symbol in self.alerted_symbols.get("bband_squeeze", []):
            return
        df['bband_width'] = (df[f'BBU_{BBAND_LENGTH}_{BBAND_STD_DEV}'] -
                            df[f'BBL_{BBAND_LENGTH}_{BBAND_STD_DEV}']) / df[f'BBM_{BBAND_LENGTH}_{BBAND_STD_DEV}']
        last_width = df['bband_width'].iloc[-2]
        min_width_in_period = df['bband_width'].iloc[-(
            SQUEEZE_LOOKBACK_PERIOD+2):-2].min()
        if last_width <= min_width_in_period:
            message = f"🗜️ *Alerta de Squeeze!* 🗜️\n\n*Moeda:* `{symbol}`\n*Timeframe:* `{TIMEFRAME}`\n\n*Sinal:* As Bandas de Bollinger estão no seu ponto mais estreito dos últimos {SQUEEZE_LOOKBACK_PERIOD} períodos. Uma explosão de volatilidade pode estar próxima."
            send_telegram_alert(message)
            self.alerted_symbols.setdefault("bband_squeeze", set()).add(symbol)

    def start_scanner_loop(self):
        """O loop principal que orquestra todo o trabalho do bot."""
        print(Fore.YELLOW + "\n--- VIGIA DE MERCADO AVANÇADO INICIADO ---")
        while True:
            print("\n" + Fore.CYAN +
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Iniciando novo ciclo de varredura...")
            self.alerted_symbols.clear()
            symbols_to_scan = self.get_target_symbols_by_hourly_volume()
            print(
                f"Iniciando análise de {len(symbols_to_scan)} símbolos filtrados...")
            for i, symbol in enumerate(symbols_to_scan):
                print(
                    f"Analisando [{i+1}/{len(symbols_to_scan)}]: {symbol}...")
                self.analyze_and_alert(symbol)
                time.sleep(0.5)
            print(
                Fore.CYAN + f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Ciclo completo. Aguardando {SCAN_INTERVAL_MINUTES} minutos.")
            time.sleep(SCAN_INTERVAL_MINUTES * 60)


# ==============================================================================
# --- SERVIDOR WEB DE FACHADA E INICIALIZAÇÃO ---
# ==============================================================================
app = Flask(__name__)


@app.route('/')
def home():
    """Página web mínima para manter o serviço do Render ativo."""
    return "O Bot Vigia de Mercado Avançado está ativo e rodando."


def run_bot():
    """Função para iniciar o scanner em uma thread separada."""
    scanner = MarketScanner()
    scanner.start_scanner_loop()


if __name__ == "__main__":
    # Inicia o bot em segundo plano
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    # Inicia o servidor web de fachada para o Render
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
