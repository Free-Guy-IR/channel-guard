# Channel Guard

ربات مانیتورینگ کانال تلگرام + پنل آنالیتیکس زنده (پورت ۹۰۰). رویدادهای جوین/لفت کانال را رصد می‌کند، برای هر رویداد اطلاعات خرید کاربر را از API ربات فروش می‌گیرد، به ادمین گزارش می‌دهد و همه‌چیز را در یک داشبورد زنده نشان می‌دهد.

پروژه‌ها و به‌روزرسانی‌ها: [@Freeguy_IR](https://t.me/Freeguy_IR)

## نصب سریع (یک دستور)

روی یه سرور تازه‌ی Ubuntu/Debian، به‌عنوان روت:

```bash
git clone https://github.com/Free-Guy-IR/channel-guard.git
cd channel-guard
sudo bash install.sh
```

اسکریپت پکیج‌های سیستمی، MySQL (اختیاری)، مخزن پایتون، و سرویس systemd رو خودش نصب/راه‌اندازی می‌کنه و تو همون اجرا چندتا سوال (توکن بات، چت‌آیدی ادمین، آیدی کانال، آدرس عمومی و...) می‌پرسه و `config.json` رو خودش می‌سازه.

## نصب دستی

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # مقادیر واقعی رو داخلش پر کن
```

`config.json` به یه دیتابیس MySQL نیاز داره (بخش `mysql` تو کانفیگ):

```bash
sudo mysql -e "CREATE DATABASE channel_guard_main;"
sudo mysql -e "CREATE USER 'channelguard_db'@'localhost' IDENTIFIED BY 'یه-پسورد-قوی';"
sudo mysql -e "GRANT ALL PRIVILEGES ON channel_guard_main.* TO 'channelguard_db'@'localhost';"
```


```bash
python -m app.main
```

پنل روی `http://<server-ip>:900/<admin_path>/` بالا می‌آید (لاگین با `panel_password` داخل `config.json`).

### اجرا به‌صورت سرویس (systemd) - بدون اسکریپت نصب

```bash
sudo useradd --system --no-create-home channelguard
sudo mkdir -p /opt/channel-guard
sudo cp -r . /opt/channel-guard
cd /opt/channel-guard
sudo python3 -m venv .venv
sudo ./.venv/bin/pip install -r requirements.txt
sudo chown -R channelguard:channelguard /opt/channel-guard
sudo cp channel-guard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now channel-guard
sudo journalctl -u channel-guard -f
```

اگر فایروال (`ufw`) فعال است:

```bash
sudo ufw allow 900/tcp
```

## فیچر «فروش» (اختیاری)

بخش «آنالیز فروش»/«کاربران فروش» به یه API خارجی (ربات فروش خودت) وصل می‌شه - اگه نداری، `sales_api_base_url`/`sales_api_token` رو خالی بذار، بقیه‌ی پنل (رصد جوین/لفت، نظرسنجی‌ها، نودها) بدون مشکل کار می‌کنه.

## محدودیت شناخته‌شده

Telegram Bot API راهی برای لیست‌کردن اعضای فعلی یک کانال ندارد؛ بنابراین ردیابی جوین/لفت فقط از لحظه‌ای که این سرویس اجرا می‌شود شروع می‌شود، نه از قبل.

## نکات امنیتی

- `config.json` هرگز نباید commit شود (در `.gitignore` هست) - شامل توکن بات، پسورد پنل، و پسورد دیتابیسه.
- `admin_path` رو یه رشته‌ی تصادفی و طولانی نگه دار - تنها لایه‌ی محافظتی قبل از صفحه‌ی لاگینه.
