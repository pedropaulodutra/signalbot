from flask import Flask
import threading
import time

# Importa o seu bot
from volume_scanner import VolumeScanner

app = Flask(__name__)

@app.route('/')
def home():
    # Esta página web simplesmente diz que o bot está rodando
    return "O Bot Vigia de Volume está ativo!"

def run_bot():
    # Função para rodar o scanner
    scanner = VolumeScanner()
    scanner.start()

if __name__ == "__main__":
    # Inicia o bot em uma thread separada
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()

    # Inicia o servidor web falso
    app.run(host='0.0.0.0', port=10000)