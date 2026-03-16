import requests
import json
import sys

CONFIG_PATH = "/Users/aly4x/.telegram_bot/config.json"

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

def send_message(text):
    config = load_config()
    token = config["bot_token"]
    chat_id = config["chat_id"]

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Telegram limit is 4096 chars, split if needed
    chunks = [text[i:i+4096] for i in range(0, len(text), 4096)]

    for chunk in chunks:
        response = requests.post(url, json={
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML"
        })
        if not response.json().get("ok"):
            print(f"Error: {response.json()}")
            return False

    print(f"Sent successfully ({len(chunks)} message(s))")
    return True

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 send_post.py 'your post text'")
        sys.exit(1)

    text = sys.argv[1]
    send_message(text)
