# Deploy Telegram Bot Lich Cup Dien

## 1. Chuan bi truoc khi push

Khong push cac file runtime/secret:

- `.env`
- `.venv/`
- `subscriptions.sqlite3`
- `__pycache__/`

Neu token Telegram tung bi lo trong chat, log, hoac commit, hay rotate token trong BotFather truoc khi chay production.

## 2. Push len Git

Neu thu muc chua phai Git repo:

```bash
git init
git branch -M main
git add .
git commit -m "Initial bot deployment setup"
git remote add origin <your-git-repo-url>
git push -u origin main
```

Neu da co repo:

```bash
git add .
git commit -m "Prepare VPS deployment"
git push
```

Kiem tra truoc khi commit:

```bash
git status --short
git check-ignore -v .env subscriptions.sqlite3 .venv
```

## 3. Cai dat tren VPS Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
cd /opt
sudo git clone <your-git-repo-url> telegram-bot-lichcupdien
sudo chown -R $USER:$USER /opt/telegram-bot-lichcupdien
cd /opt/telegram-bot-lichcupdien
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
nano .env
```

Trong `.env`, dien:

```bash
TELEGRAM_BOT_TOKEN=...
ADMIN_TELEGRAM_ID=...
SUBSCRIPTIONS_DB=/opt/telegram-bot-lichcupdien/subscriptions.sqlite3
AREA_ALIASES_FILE=/opt/telegram-bot-lichcupdien/area_aliases.json
DAILY_NOTIFY_TIME=07:00
```

Chay thu:

```bash
. .venv/bin/activate
python bot.py
```

Dung bang `Ctrl+C` sau khi thay bot da polling thanh cong.

## 4. Chay bang systemd

Tao service:

```bash
sudo nano /etc/systemd/system/telegram-bot-lichcupdien.service
```

Noi dung:

```ini
[Unit]
Description=Telegram Bot Lich Cup Dien
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/telegram-bot-lichcupdien
EnvironmentFile=/opt/telegram-bot-lichcupdien/.env
ExecStart=/opt/telegram-bot-lichcupdien/.venv/bin/python /opt/telegram-bot-lichcupdien/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Bat service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot-lichcupdien
sudo systemctl start telegram-bot-lichcupdien
sudo systemctl status telegram-bot-lichcupdien
```

Xem log:

```bash
journalctl -u telegram-bot-lichcupdien -f
```

Cap nhat code tren VPS:

```bash
cd /opt/telegram-bot-lichcupdien
git pull
. .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart telegram-bot-lichcupdien
```

## 5. Backup SQLite

`subscriptions.sqlite3` chua dang ky thong bao va gop y. Backup dinh ky:

```bash
mkdir -p ~/bot-backups
cp /opt/telegram-bot-lichcupdien/subscriptions.sqlite3 ~/bot-backups/subscriptions-$(date +%F).sqlite3
```
