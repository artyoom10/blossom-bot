import os
import re
import html
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps

from flask import Flask, request, jsonify, make_response
from weasyprint import HTML

app = Flask(__name__)

# Переменные окружения
BOT_TOKEN = os.environ.get("BLOSSOM_BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _cors(resp):
    """Добавить CORS заголовки к ответу"""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Internal-Token"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS, GET"
    return resp


def require_internal_token(f):
    """Декоратор для проверки X-Internal-Token"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get("X-Internal-Token")
        if not token or token != INTERNAL_API_TOKEN:
            return _cors(jsonify({"error": "Unauthorized"})), 401
        return f(*args, **kwargs)
    return decorated_function


@app.get("/")
def health():
    return _cors(jsonify({"ok": True}))


@app.route("/admin/invoice/send", methods=["OPTIONS"])
def invoice_send_options():
    return _cors(make_response("", 204))


def _safe_filename(s: str) -> str:
    """Безопасное имя файла"""
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_\-]+", "", s)
    return (s[:60] or "invoice")


def send_pdf(chat_id: str, pdf_bytes: bytes, filename: str, caption: str):
    """Отправить PDF админу через Telegram Bot API"""
    files = {"document": (filename, pdf_bytes, "application/pdf")}
    data = {"chat_id": chat_id, "caption": caption}
    r = requests.post(f"{TG_API}/sendDocument", data=data, files=files, timeout=60)
    r.raise_for_status()


def build_invoice_html(
    order_id: str,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
    items: list,  # [{"name": "...", "quantity": 5, "price": 100.00}, ...]
    delivery_address: str,
    total_sum: float,
    generated_date: str,
) -> str:
    """Формирует красивую HTML накладную"""
    
    def esc(v):
        return html.escape("" if v is None else str(v))

    # Формируем строки таблицы товаров
    item_rows = []
    for idx, item in enumerate(items or [], start=1):
        name = esc(item.get("name", ""))
        qty = esc(item.get("quantity", ""))
        price = item.get("price", 0)
        try:
            price_float = float(price)
        except:
            price_float = 0
        amount = price_float * (float(qty) if qty else 0)
        
        item_rows.append(f"""
          <tr>
            <td class="num">{idx}</td>
            <td>{name}</td>
            <td class="num">{qty}</td>
            <td class="num">{price_float:.2f}</td>
            <td class="num">{amount:.2f}</td>
          </tr>
        """)

    items_tbody = "\n".join(item_rows) if item_rows else """
      <tr><td colspan="5" class="muted">Товары не указаны</td></tr>
    """

    return f"""
    <html>
      <head>
        <meta charset="utf-8">
        <style>
          @page {{ size: A4; margin: 16mm; }}
          body {{ 
            font-family: DejaVu Sans, Arial, sans-serif; 
            color: #1a1a1a;
            margin: 0;
            padding: 0;
          }}
          
          .header {{ 
            border-bottom: 3px solid #2c3e50; 
            padding-bottom: 14px; 
            margin-bottom: 18px; 
          }}
          
          .header h1 {{ 
            margin: 0; 
            font-size: 24px; 
            font-weight: 800;
            letter-spacing: -0.5px;
            color: #2c3e50;
          }}
          
          .header .order-id {{
            font-size: 12px;
            color: #666;
            margin-top: 4px;
          }}
          
          .meta-info {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-bottom: 18px;
            font-size: 11px;
          }}
          
          .meta-section {{
            border: 1px solid #e0e0e0;
            border-radius: 8px;
            padding: 10px 12px;
            background: #f9f9f9;
          }}
          
          .meta-section .label {{
            font-weight: 700;
            color: #555;
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
          }}
          
          .meta-section .value {{
            color: #1a1a1a;
            line-height: 1.4;
            word-break: break-word;
          }}
          
          .items-section {{
            margin-bottom: 18px;
          }}
          
          .section-title {{
            font-weight: 800;
            font-size: 13px;
            color: #2c3e50;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
          }}
          
          table.items {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 12px;
          }}
          
          table.items thead {{
            background: #f0f0f0;
          }}
          
          table.items thead th {{
            text-align: left;
            font-size: 11px;
            font-weight: 700;
            color: #333;
            border-bottom: 2px solid #d0d0d0;
            padding: 10px 8px;
          }}
          
          table.items tbody td {{
            border-bottom: 1px solid #e8e8e8;
            padding: 9px 8px;
            font-size: 11px;
          }}
          
          table.items tbody tr:nth-child(2n) td {{
            background: #fafafa;
          }}
          
          table.items tbody tr:hover td {{
            background: #f5f5f5;
          }}
          
          .num {{
            text-align: right;
            width: 60px;
          }}
          
          .muted {{
            color: #888;
            font-style: italic;
          }}
          
          .totals {{
            text-align: right;
            margin-top: 14px;
            padding-top: 12px;
            border-top: 2px solid #d0d0d0;
          }}
          
          .total-row {{
            display: flex;
            justify-content: flex-end;
            gap: 20px;
            margin-bottom: 8px;
            font-size: 13px;
          }}
          
          .total-label {{
            font-weight: 600;
            min-width: 140px;
          }}
          
          .total-amount {{
            text-align: right;
            min-width: 100px;
            font-weight: 800;
            font-size: 16px;
            color: #2c3e50;
          }}
          
          .footer {{
            margin-top: 20px;
            padding-top: 12px;
            border-top: 1px solid #ddd;
            font-size: 10px;
            color: #666;
          }}
          
          .footer-text {{
            margin: 4px 0;
          }}
        </style>
      </head>
      <body>
        <!-- Header -->
        <div class="header">
          <h1>Накладная (Invoice)</h1>
          <div class="order-id">Заказ: {esc(order_id)}</div>
        </div>

        <!-- Информация о заказе -->
        <div class="meta-info">
          <div class="meta-section">
            <div class="label">Клиент</div>
            <div class="value">
              <strong>{esc(customer_name)}</strong><br>
              {esc(customer_email)}<br>
              {esc(customer_phone)}
            </div>
          </div>
          
          <div class="meta-section">
            <div class="label">Адрес доставки</div>
            <div class="value">{esc(delivery_address)}</div>
          </div>
        </div>

        <!-- Таблица товаров -->
        <div class="items-section">
          <div class="section-title">Товары</div>
          <table class="items">
            <thead>
              <tr>
                <th class="num">№</th>
                <th>Наименование</th>
                <th class="num">Кол-во</th>
                <th class="num">Цена</th>
                <th class="num">Сумма</th>
              </tr>
            </thead>
            <tbody>
              {items_tbody}
            </tbody>
          </table>
        </div>

        <!-- Итоги -->
        <div class="totals">
          <div class="total-row">
            <div class="total-label">Итого к оплате:</div>
            <div class="total-amount">{total_sum:.2f} ₽</div>
          </div>
        </div>

        <!-- Footer -->
        <div class="footer">
          <div class="footer-text">Дата: {esc(generated_date)}</div>
          <div class="footer-text">BlossomffBot • Автоматически сформировано системой</div>
        </div>
      </body>
    </html>
    """


@app.post("/admin/invoice/send")
@require_internal_token
def send_invoice():
    """
    Endpoint для отправки накладной админу в Telegram.
    
    Ожидает JSON:
    {
      "order_id": "ORD-12345",
      "customer_name": "Иван Петров",
      "customer_email": "ivan@example.com",
      "customer_phone": "+7 (900) 000-00-00",
      "items": [
        {"name": "Товар 1", "quantity": 5, "price": 100.00},
        {"name": "Товар 2", "quantity": 2, "price": 250.50}
      ],
      "delivery_address": "Москва, ул. Примерная, дом 1, кв. 10",
      "total_sum": 1001.00
    }
    """
    payload = request.get_json(silent=True) or {}

    # Дата в Helsinki (или другой TZ, если нужна)
    generated_date = datetime.now(ZoneInfo("Europe/Helsinki")).strftime("%Y-%m-%d %H:%M")

    # Извлекаем данные из payload
    order_id = str(payload.get("order_id") or "UNKNOWN")
    customer_name = str(payload.get("customer_name") or "Не указано")
    customer_email = str(payload.get("customer_email") or "—")
    customer_phone = str(payload.get("customer_phone") or "—")
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    delivery_address = str(payload.get("delivery_address") or "Не указано")
    
    try:
        total_sum = float(payload.get("total_sum", 0))
    except:
        total_sum = 0.0

    # Формируем HTML накладной
    html_doc = build_invoice_html(
        order_id=order_id,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        items=items,
        delivery_address=delivery_address,
        total_sum=total_sum,
        generated_date=generated_date,
    )

    # Конвертируем в PDF
    pdf_bytes = HTML(string=html_doc).write_pdf()

    # Отправляем в Telegram
    filename = f"{_safe_filename(order_id)}_{generated_date.split()[0]}.pdf"
    caption = f"📋 Накладная {order_id}\nКлиент: {customer_name}\nСумма: {total_sum:.2f} ₽"
    
    try:
        send_pdf(ADMIN_CHAT_ID, pdf_bytes, filename=filename, caption=caption)
        return _cors(jsonify({"ok": True, "message": "Invoice sent", "order_id": order_id}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
