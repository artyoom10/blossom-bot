import os
import re
import io
import html
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps

from flask import Flask, request, jsonify, make_response
from weasyprint import HTML


app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BOT_TOKEN = os.environ.get("BLOSSOM_BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN")

SENDER_NAME = os.environ.get("SENDER_NAME", "—")
SENDER_PHONE = os.environ.get("SENDER_PHONE", "—")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Internal-Token"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS, GET"
    # полезно, если фронт/веб захочет прочитать имя файла
    resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
    return resp


def require_internal_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-Internal-Token")
        if not token or token != INTERNAL_API_TOKEN:
            return _cors(jsonify({"error": "Unauthorized"})), 401
        return f(*args, **kwargs)

    return decorated


@app.get("/")
def health():
    return _cors(jsonify({"ok": True}))


def _safe_filename(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_\-]+", "", s)
    return (s[:60] or "file")


def send_pdf(chat_id: str, pdf_bytes: bytes, filename: str, caption: str):
    if not BOT_TOKEN:
        raise RuntimeError("BLOSSOM_BOT_TOKEN is not set")
    files = {"document": (filename, pdf_bytes, "application/pdf")}
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    r = requests.post(f"{TG_API}/sendDocument", data=data, files=files, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")
    return r.json()


def build_invoice_html(
    salon_name: str,
    sender_name: str,
    sender_phone: str,
    logo_path: str,
    order_id: str,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
    items: list,
    delivery_address: str,
    total_sum: float,
    generation_dt_str: str,
    header_date_str: str,
) -> str:
    def esc(v):
        return html.escape("" if v is None else str(v))

    def num_cell(value: str, width_ch: int) -> str:
        v = esc(value)
        return f'<span class="numbox" style="width:{width_ch}ch">{v}</span>'

    rows = []
    for idx, item in enumerate(items or [], start=1):
        name = esc(item.get("name", ""))

        try:
            qty = float(item.get("quantity", 0))
        except Exception:
            qty = 0.0

        try:
            price = float(item.get("price", 0))
        except Exception:
            price = 0.0

        amount = price * qty

        rows.append(
            f"""
          <tr>
            <td class="td-idx">{num_cell(str(idx), 3)}</td>
            <td class="td-name">{name}</td>
            <td class="td-qty">{num_cell(f"{qty:g}", 6)}</td>
            <td class="td-price">{num_cell(f"{price:.2f}", 10)}</td>
            <td class="td-sum">{num_cell(f"{amount:.2f}", 10)}</td>
          </tr>
        """
        )

    tbody = "\n".join(rows) if rows else """
      <tr><td colspan="5" class="muted">Товары не указаны</td></tr>
    """

    logo_html = ""
    if logo_path:
        logo_html = f'<img class="logo" src="{esc(logo_path)}" alt="logo">'

    return f"""
    <html>
      <head>
        <meta charset="utf-8">
        <style>
          @page {{ size: A5; margin: 10mm 12mm 12mm 12mm; }}

          body {{
            font-family: DejaVu Sans, Arial, sans-serif;
            color: #1a1a1a;
          }}

          .header {{
            position: relative;
            border-bottom: 3px solid #2c3e50;
            padding: 3mm 0 2mm 0;
            margin-bottom: 12px;
            min-height: 18mm;
          }}

          .logo {{
            position: absolute;
            left: 0;
            top: -6mm;
            width: 24mm;
            height: auto;
          }}

          .header-date {{
            position: absolute;
            right: 0;
            top: 0;
            font-size: 11px;
            color: #666;
            white-space: nowrap;
          }}

          .header-center {{
            position: absolute;
            left: 50%;
            top: 0;
            transform: translateX(-50%);
            text-align: center;
            width: 120mm;
          }}

          .title {{
            margin: 0;
            font-size: 22px;
            font-weight: 800;
            letter-spacing: -0.4px;
            color: #2c3e50;
          }}

          .order-id {{
            margin-top: 5px;
            font-size: 12px;
            color: #666;
          }}

          .header-spacer {{
            height: 14mm;
          }}

          .sender {{
            margin: 10px 0 14px 0;
            border: 1px solid #d8e6f2;
            background: #eef6ff;
            border-radius: 10px;
            padding: 10px 12px;
          }}
          .sender .label {{
            font-size: 10px;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.6px;
            color: #2c3e50;
            margin-bottom: 4px;
          }}
          .sender .value {{
            font-size: 13px;
            font-weight: 900;
            color: #1a1a1a;
          }}
          .sender .phone {{
            font-weight: 800;
            color: #2c3e50;
          }}

          .meta-info {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 14px;
            margin-bottom: 14px;
            font-size: 11px;
          }}
          .meta-section {{
            border: 1px solid #e0e0e0;
            border-radius: 10px;
            padding: 10px 12px;
            background: #f9f9f9;
          }}
          .meta-section .label {{
            font-weight: 800;
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

          .section-title {{
            font-weight: 900;
            font-size: 12px;
            color: #2c3e50;
            margin: 12px 0 8px 0;
            text-transform: uppercase;
            letter-spacing: 0.5px;
          }}

          table.items {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
          }}
          col.cw-idx {{ width: 12mm; }}
          col.cw-qty {{ width: 24mm; }}
          col.cw-price {{ width: 32mm; }}
          col.cw-sum {{ width: 34mm; }}

          table.items thead th {{
            background: #f0f0f0;
            font-size: 11px;
            font-weight: 800;
            color: #333;
            border-bottom: 2px solid #d0d0d0;
            padding: 10px 10px;
          }}
          table.items tbody td {{
            border-bottom: 1px solid #e8e8e8;
            padding: 9px 10px;
            font-size: 11px;
            vertical-align: middle;
          }}
          table.items tbody tr:nth-child(2n) td {{
            background: #fafafa;
          }}

          th.th-num {{ text-align: right; }}
          td.td-idx, td.td-qty, td.td-price, td.td-sum {{ text-align: right; }}

          .numbox {{
            display: inline-block;
            text-align: right;
            white-space: nowrap;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
          }}

          .muted {{
            color: #888;
            font-style: italic;
            text-align: center;
          }}

          .totals {{
            margin-top: 10px;
            padding-top: 10px;
            border-top: 2px solid #d0d0d0;
          }}
          .total-row {{
            display: table;
            width: 100%;
          }}
          .total-label {{
            display: table-cell;
            text-align: right;
            font-weight: 700;
            font-size: 12px;
            color: #333;
            padding-right: 10px;
          }}
          .total-amount {{
            display: table-cell;
            width: 50mm;
            text-align: right;
            font-weight: 900;
            font-size: 16px;
            color: #2c3e50;
            white-space: nowrap;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
          }}

          .footer {{
            margin-top: 18px;
            padding-top: 10px;
            border-top: 1px solid #ddd;
            font-size: 10px;
            color: #666;
            line-height: 1.35;
          }}
        </style>
      </head>
      <body>

        <div class="header">
          {logo_html}
          <div class="header-date">{esc(header_date_str)}</div>
          <div class="header-center">
            <div class="title">Накладная для {esc(salon_name)}</div>
            <div class="order-id">Заказ №{esc(order_id)}</div>
          </div>
          <div class="header-spacer"></div>
        </div>

        <div class="sender">
          <div class="label">От кого</div>
          <div class="value">{esc(sender_name)} <span class="phone">({esc(sender_phone)})</span></div>
        </div>

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

        <div class="section-title">Товары</div>
        <table class="items">
          <colgroup>
            <col class="cw-idx">
            <col>
            <col class="cw-qty">
            <col class="cw-price">
            <col class="cw-sum">
          </colgroup>
          <thead>
            <tr>
              <th>№</th>
              <th>Наименование</th>
              <th class="th-num">Кол-во</th>
              <th class="th-num">Цена</th>
              <th class="th-num">Сумма</th>
            </tr>
          </thead>
          <tbody>
            {tbody}
          </tbody>
        </table>

        <div class="totals">
          <div class="total-row">
            <div class="total-label">Итого к оплате:</div>
            <div class="total-amount">{total_sum:.2f} ₽</div>
          </div>
        </div>

        <div class="footer">
          <div>Дата генерации (МСК): {esc(generation_dt_str)}</div>
          <div>@BlossomffBot • Автоматически сформировано системой</div>
        </div>

      </body>
    </html>
    """


def _extract_invoice_fields(payload: dict):
    salon_name = str(payload.get("salon_name") or "Салон")

    now_dt = datetime.now(ZoneInfo("Europe/Moscow"))
    generation_dt_str = now_dt.strftime("%d.%m.%Y %H:%M")

    header_date_str = (
        payload.get("invoice_date")
        or payload.get("date")
        or now_dt.strftime("%d.%m.%Y")
    )
    header_date_str = str(header_date_str)

    order_id = str(payload.get("order_id") or "UNKNOWN")

    customer_name = str(payload.get("customer_name") or "Не указано")
    customer_email = str(payload.get("customer_email") or "—")
    customer_phone = str(payload.get("customer_phone") or "—")

    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    delivery_address = str(payload.get("delivery_address") or "Не указано")

    try:
        total_sum = float(payload.get("total_sum", 0))
    except Exception:
        total_sum = 0.0

    logo_path = "blossom_logo.png" if os.path.exists(os.path.join(BASE_DIR, "blossom_logo.png")) else ""

    return {
        "salon_name": salon_name,
        "generation_dt_str": generation_dt_str,
        "header_date_str": header_date_str,
        "order_id": order_id,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "customer_phone": customer_phone,
        "items": items,
        "delivery_address": delivery_address,
        "total_sum": total_sum,
        "logo_path": logo_path,
    }


def _build_invoice_pdf(payload: dict):
    f = _extract_invoice_fields(payload)

    html_doc = build_invoice_html(
        salon_name=f["salon_name"],
        sender_name=SENDER_NAME,
        sender_phone=SENDER_PHONE,
        logo_path=f["logo_path"],
        order_id=f["order_id"],
        customer_name=f["customer_name"],
        customer_email=f["customer_email"],
        customer_phone=f["customer_phone"],
        items=f["items"],
        delivery_address=f["delivery_address"],
        total_sum=f["total_sum"],
        generation_dt_str=f["generation_dt_str"],
        header_date_str=f["header_date_str"],
    )

    pdf_bytes = HTML(string=html_doc, base_url=BASE_DIR).write_pdf()

    safe_salon = _safe_filename(f["salon_name"])
    safe_order = _safe_filename(f["order_id"])
    filename = f'{f["header_date_str"]}_{safe_salon}_{safe_order}.pdf'

    caption = (
        f"<b>🧾 Накладная заказа №</b><code>{html.escape(f['order_id'])}</code>\n"
        f"<b>📅 Дата:</b> <code>{html.escape(f['header_date_str'])}</code>\n"
        f"<b>👤 Клиент:</b> <code>{html.escape(f['salon_name'])}</code>\n"
        f"<b>💸 Общая сумма:</b> <code>{f['total_sum']:.2f} ₽</code>"
    )

    return pdf_bytes, filename, caption, f["order_id"]


# ----------- OPTIONS (CORS preflight) -----------

@app.route("/admin/invoice/pdf", methods=["OPTIONS"])
def invoice_pdf_options():
    return _cors(make_response("", 204))


@app.route("/admin/invoice/send", methods=["OPTIONS"])
def invoice_send_options():
    return _cors(make_response("", 204))


# ----------- PDF generation (preview) -----------

@app.post("/admin/invoice/pdf")
@require_internal_token
def invoice_pdf():
    payload = request.get_json(silent=True) or {}

    try:
        pdf_bytes, filename, _caption, order_id = _build_invoice_pdf(payload)

        resp = make_response(pdf_bytes)
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f'inline; filename="{filename}"'
        resp.headers["Content-Length"] = str(len(pdf_bytes))
        resp.headers["X-Order-Id"] = order_id  # опционально, удобно для логов
        return _cors(resp)
    except Exception as e:
        return _cors(jsonify({"ok": False, "error": str(e)})), 500


# ----------- Send to Telegram -----------

@app.post("/admin/invoice/send")
@require_internal_token
def send_invoice():
    payload = request.get_json(silent=True) or {}

    if not ADMIN_CHAT_ID:
        return _cors(jsonify({"ok": False, "error": "ADMIN_CHAT_ID is not set"})), 500

    try:
        pdf_bytes, filename, caption, order_id = _build_invoice_pdf(payload)
        tg_resp = send_pdf(ADMIN_CHAT_ID, pdf_bytes, filename=filename, caption=caption)
        return _cors(jsonify({"ok": True, "order_id": order_id, "telegram": tg_resp}))
    except Exception as e:
        return _cors(jsonify({"ok": False, "error": str(e)})), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
