from flask import Flask, request, jsonify
import os
import datetime
import requests

app = Flask(__name__)

# ===== TELEGRAM CONFIG (Optional but recommended) =====
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg}, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

# ===== MAIN WEBHOOK =====
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        print(f"[{timestamp}] Signal received: {data}")
        
        action = data.get('action', 'UNKNOWN')
        symbol = data.get('symbol', 'UNKNOWN')
        qty = data.get('qty', 0)
        
        # 🔥 ADD YOUR BROKER ORDER CODE HERE
        # place_order(action, symbol, qty)
        
        # Telegram notification
        send_telegram(f"✅ {action} {symbol} Qty:{qty}\n🕐 {timestamp}")
        
        return jsonify({'status': 'success', 'received': data}), 200
        
    except Exception as e:
        error_msg = f"🚨 Webhook Error: {str(e)}"
        print(error_msg)
        send_telegram(error_msg)
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ===== HEALTH CHECK =====
@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'alive', 'time': str(datetime.datetime.now())}), 200

@app.route('/', methods=['GET'])
def home():
    return "Algo Webhook Server Running ✅", 200

# ===== START SERVER =====
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)