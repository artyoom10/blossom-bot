import hashlib
import hmac
import html
import json
import os
import re
from datetime import date as date_cls, datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)

BOT_TOKEN = os.getenv("BLOSSOM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")
FLOWERS_PUBLIC_BASE = os.getenv(
    "FLOWERS_PUBLIC_BASE",
    f"{SUPABASE_URL}/storage/v1/object/public/flowers" if SUPABASE_URL else "",
).rstrip("/")
REQUESTS_TABLE = os.getenv("REQUESTS_TABLE", "client_requests")

FLOWER_TYPES_SELECT_FULL = "id,name,stems_count,catalog_visible,price_rub,color,stem_length_cm"
FLOWER_TYPES_SELECT_BASIC = "id,name,stems_count,catalog_visible,price_rub"
REQUESTS_SELECT_FULL = (
    "id,status,salon_name,total_amount,total_stems,items,created_at,updated_at,manager_note,previous_items,"
    "delivery_date,previous_delivery_date"
)
REQUESTS_SELECT_BASIC = "id,status,salon_name,total_amount,total_stems,items,created_at,updated_at"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

app.config["JSON_AS_ASCII"] = False


def normalize_tg_user_id(value: Any) -> str:
    """
    Единый формат числового Telegram user id для сравнения с ADMIN_TELEGRAM_IDS.
    Убирает кавычки, BOM, пробелы; поддерживает int/float/str; вытаскивает длинную
    цепочку цифр из строк вида «id: 123456789».
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        if value == 0:
            return ""
        return str(value)
    if isinstance(value, float):
        try:
            i = int(value)
            if i == 0:
                return ""
            return str(i)
        except (ValueError, OverflowError):
            return ""
    s = str(value).strip().strip('"').strip("'")
    for ch in ("\ufeff", "\u200b", "\u200c", "\u200d"):
        s = s.replace(ch, "")
    s = re.sub(r"\s+", "", s)
    if not s:
        return ""
    if s.startswith("-") and s[1:].isdigit():
        return s
    if s.isdigit():
        return str(int(s))
    m = re.search(r"\d{8,}", s)
    if m:
        return m.group(0)
    return ""


def admin_telegram_ids() -> Set[str]:
    ids: Set[str] = set()
    raw = os.getenv("ADMIN_TELEGRAM_IDS", "") or ""
    for part in re.split(r"[\s,;|]+", raw):
        n = normalize_tg_user_id(part)
        if n:
            ids.add(n)
    one = os.getenv("ADMIN_TELEGRAM_ID", "") or ""
    n = normalize_tg_user_id(one)
    if n:
        ids.add(n)
    return ids


def admin_chat_id_normalized() -> str:
    """ADMIN_CHAT_ID из env (чат уведомлений); часто совпадает с user id личного чата с ботом."""
    return normalize_tg_user_id(os.getenv("ADMIN_CHAT_ID", "") or "")


def is_user_telegram_admin(tid_key: str) -> bool:
    if not tid_key:
        return False
    if tid_key in admin_telegram_ids():
        return True
    ac = admin_chat_id_normalized()
    return bool(ac) and tid_key == ac


def public_miniapp_base_url() -> str:
    """Явный URL из env (приоритет)."""
    for key in ("MINIAPP_BASE_URL", "RENDER_EXTERNAL_URL", "PUBLIC_BASE_URL"):
        v = os.getenv(key, "").strip().rstrip("/")
        if v:
            return v
    return ""


def miniapp_base_url_for_request() -> str:
    """
    URL сервиса для ссылок Web App. Сначала env, иначе Host из входящего HTTP-запроса
    (webhook Telegram к Render передаёт Host — можно не задавать MINIAPP_BASE_URL).
    """
    b = public_miniapp_base_url()
    if b:
        return b
    host = (request.headers.get("Host") or "").split(",")[0].strip()
    if not host or "localhost" in host.lower():
        return ""
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
    if proto not in ("http", "https"):
        proto = "https"
    return f"{proto}://{host}".rstrip("/")


# -------------------- helpers --------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_env() -> Optional[str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not set"
    return None


def supabase_request(method: str, path: str, *, params: Optional[Dict[str, Any]] = None, json_data: Any = None,
                     prefer_return: bool = False) -> requests.Response:
    headers = dict(HEADERS)
    if prefer_return:
        headers["Prefer"] = "return=representation"
    return requests.request(
        method=method,
        url=f"{SUPABASE_URL}/rest/v1/{path}",
        headers=headers,
        params=params,
        json=json_data,
        timeout=30,
    )


def supabase_get(path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
    return supabase_request("GET", path, params=params)


def supabase_post(path: str, json_data: Any, prefer_return: bool = True) -> requests.Response:
    return supabase_request("POST", path, json_data=json_data, prefer_return=prefer_return)


def supabase_patch(path: str, json_data: Any, params: Optional[Dict[str, Any]] = None,
                   prefer_return: bool = True) -> requests.Response:
    return supabase_request("PATCH", path, params=params, json_data=json_data, prefer_return=prefer_return)


def supabase_delete(path: str, params: Optional[Dict[str, Any]] = None,
                    prefer_return: bool = False) -> requests.Response:
    return supabase_request("DELETE", path, params=params, prefer_return=prefer_return)


def tg_api(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise RuntimeError("BLOSSOM_BOT_TOKEN is not set")
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=30)
    data = r.json()
    if not r.ok or not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def tg_send_message(chat_id: int | str, text: str, buttons: Optional[List[List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return tg_api("sendMessage", payload)


def tg_answer_callback(callback_query_id: str, text: str) -> None:
    try:
        tg_api(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": False,
            },
        )
    except Exception:
        pass


def tg_clear_buttons(chat_id: int | str, message_id: int) -> None:
    try:
        tg_api(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )
    except Exception:
        pass


def validate_webapp_init_data(init_data: str) -> Optional[Dict[str, Any]]:
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_received = parsed.pop("hash", None)
        if not hash_received:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, hash_received):
            return None
        user_raw = parsed.get("user")
        if not user_raw:
            return None
        return json.loads(user_raw)
    except Exception:
        return None


def require_admin_from_header() -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[Any, int]]]:
    init_data = request.headers.get("X-Telegram-Init-Data", "") or ""
    user = validate_webapp_init_data(init_data)
    if not user:
        return None, (jsonify({"ok": False, "error": "invalid_init_data"}), 401)
    uid = normalize_tg_user_id(user.get("id"))
    if not uid or not is_user_telegram_admin(uid):
        return None, (jsonify({"ok": False, "error": "forbidden"}), 403)
    return user, None


_MONTHS_RU = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def parse_iso_date_only(value: Any) -> Optional[date_cls]:
    if value is None:
        return None
    t = str(value).strip()[:10]
    if len(t) < 10 or t[4] != "-" or t[7] != "-":
        return None
    try:
        y, m, d = int(t[0:4]), int(t[5:7]), int(t[8:10])
        return date_cls(y, m, d)
    except ValueError:
        return None


def format_date_ru_long(value: Any) -> str:
    """Напр. 17 апреля 2026 года (пятница). Без HTML-экранирования — экранируйте при вставке в разметку."""
    d = parse_iso_date_only(value)
    if not d:
        return str(value).strip()[:64] if value else ""
    wd = _WEEKDAYS_RU[d.weekday()]
    return f"{d.day} {_MONTHS_RU[d.month]} {d.year} года ({wd})"


def add_items_to_reservation_map(acc: Dict[int, int], items: Any) -> None:
    if not isinstance(items, list):
        return
    for it in items:
        try:
            fid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        q = int(it.get("qty") or it.get("quantity") or 0)
        if q <= 0:
            continue
        acc[fid] = acc.get(fid, 0) + q


def aggregate_pending_reservations() -> Dict[int, int]:
    """Сумма количеств в заявках со статусом new и change_requested (ещё не списано со склада)."""
    out: Dict[int, int] = {}
    r = supabase_get(
        REQUESTS_TABLE,
        {
            "select": "items",
            "status": "in.(new,change_requested)",
            "limit": 1000,
        },
    )
    if not r.ok:
        r2 = supabase_get(REQUESTS_TABLE, {"select": "items,status", "limit": 1000})
        if not r2.ok:
            return out
        for row in r2.json() or []:
            if row.get("status") not in ("new", "change_requested"):
                continue
            add_items_to_reservation_map(out, row.get("items"))
        return out
    for row in r.json() or []:
        add_items_to_reservation_map(out, row.get("items"))
    return out


def deduct_stems_from_inventory_for_items(items: List[Dict[str, Any]]) -> None:
    """Списание стеблей при подтверждении заявки."""
    for it in items or []:
        try:
            fid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        qty = int(it.get("qty") or it.get("quantity") or 0)
        if qty <= 0:
            continue
        fr = supabase_get("flower_types", {"select": "stems_count", "id": f"eq.{fid}", "limit": 1})
        if not fr.ok:
            continue
        rows = fr.json() or []
        if not rows:
            continue
        cur = rows[0].get("stems_count")
        try:
            cur_i = int(cur) if cur is not None else 0
        except (TypeError, ValueError):
            cur_i = 0
        new_val = max(0, cur_i - qty)
        supabase_patch(
            "flower_types",
            {"stems_count": new_val},
            params={"id": f"eq.{fid}"},
            prefer_return=False,
        )


def parse_rub_price(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return round(float(match.group(0)), 2)
    except Exception:
        return 0.0


def flower_image(flower_type_id: Any) -> str:
    if not FLOWERS_PUBLIC_BASE:
        return ""
    return f"{FLOWERS_PUBLIC_BASE}/flower_{flower_type_id}.png"


def build_telegram_link(user_id: Any, username: Optional[str], full_name: str) -> str:
    safe_name = html.escape(full_name or "Клиент")
    if username:
        safe_username = html.escape(username.lstrip("@"))
        return f'<a href="https://t.me/{safe_username}">{safe_name}</a>'
    return f'<a href="tg://user?id={html.escape(str(user_id))}">{safe_name}</a>'


def load_salon_by_tg(telegram_id: int | str) -> Optional[Dict[str, Any]]:
    r = supabase_get(
        "salons",
        {
            "select": "id,name,tg_chat,address,phone,created_at",
            "tg_chat": f"eq.{telegram_id}",
            "limit": 1,
        },
    )
    if not r.ok:
        raise RuntimeError(r.text)
    rows = r.json() or []
    return rows[0] if rows else None


def load_flower_types() -> List[Dict[str, Any]]:
    r = supabase_get("flower_types", {"select": FLOWER_TYPES_SELECT_FULL, "order": "id.asc"})
    if r.ok:
        return r.json() or []
    r2 = supabase_get("flower_types", {"select": FLOWER_TYPES_SELECT_BASIC, "order": "id.asc"})
    if not r2.ok:
        raise RuntimeError(r2.text)
    return r2.json() or []


def flower_catalog_visible(flower: Dict[str, Any]) -> bool:
    if "catalog_visible" in flower and flower.get("catalog_visible") is False:
        return False
    return True


def flower_effective_price(flower: Dict[str, Any]) -> float:
    if flower.get("price_rub") is not None:
        try:
            return round(float(flower["price_rub"]), 2)
        except (TypeError, ValueError):
            pass
    return parse_rub_price(flower.get("color"))


def flower_effective_stock(flower: Dict[str, Any]) -> int:
    v = flower.get("stems_count")
    if v is None:
        return 0
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def load_request_by_id(request_id: int | str) -> Optional[Dict[str, Any]]:
    r = supabase_get(
        REQUESTS_TABLE,
        {"select": "*", "id": f"eq.{request_id}", "limit": 1},
    )
    if not r.ok:
        raise RuntimeError(r.text)
    rows = r.json() or []
    return rows[0] if rows else None


def request_status_label(status: str) -> str:
    """Краткая подпись для Telegram / логов."""
    return {
        "new": "Новая",
        "approved": "Подтверждена",
        "rejected": "Отмена админом",
        "rejected_by_client": "Отклонена клиентом",
        "change_requested": "Нужно изменить",
        "cancelled_by_client": "Отмена клиентом",
    }.get(status, status)


def build_items_summary(items: List[Dict[str, Any]]) -> str:
    lines = []
    for item in items:
        name = html.escape(str(item.get("name") or "Позиция"))
        qty = item.get("qty") or item.get("quantity") or 0
        price = parse_rub_price(item.get("price"))
        if price > 0:
            lines.append(f"• {name} — {qty} шт × {price:.0f} ₽")
        else:
            lines.append(f"• {name} — {qty} шт")
    return "\n".join(lines)


def totals_from_items(items: List[Dict[str, Any]]) -> Tuple[float, int]:
    total_amount = 0.0
    total_stems = 0
    for item in items:
        qty = int(item.get("qty") or item.get("quantity") or 0)
        price = parse_rub_price(item.get("price"))
        total_amount += qty * price
        total_stems += qty
    return round(total_amount, 2), total_stems


def build_items_summary_plain(items: List[Dict[str, Any]]) -> str:
    lines = []
    for item in items:
        name = str(item.get("name") or "Позиция")
        qty = item.get("qty") or item.get("quantity") or 0
        price = parse_rub_price(item.get("price"))
        if price > 0:
            lines.append(f"• {name} — {qty} шт × {price:.0f} ₽")
        else:
            lines.append(f"• {name} — {qty} шт")
    return "\n".join(lines)


def format_delivery_line(request_row: Dict[str, Any]) -> str:
    d = request_row.get("delivery_date")
    if not d:
        return ""
    s = format_date_ru_long(d)
    if not s:
        return ""
    return f"Желаемая дата поставки: <b>{html.escape(s)}</b>\n"


def build_client_order_details_message(request_row: Dict[str, Any], title: str) -> str:
    rid = request_row.get("id")
    items = request_row.get("items") or []
    total = float(request_row.get("total_amount") or 0)
    stems = int(request_row.get("total_stems") or 0)
    body = build_items_summary_plain(items)
    dline = format_delivery_line(request_row)
    return (
        f"{title}\n\n"
        f"Заявка <b>#{html.escape(str(rid))}</b>\n"
        f"{dline}"
        f"{html.escape(body)}\n\n"
        f"Стеблей: <b>{stems}</b>\n"
        f"Итого: <b>{total:.0f} ₽</b>"
    )


def build_revision_diff_message(request_row: Dict[str, Any]) -> str:
    rid = request_row.get("id")
    prev = request_row.get("previous_items") or []
    cur = request_row.get("items") or []
    note = str(request_row.get("manager_note") or "").strip()
    lines = [
        "✏️ <b>Менеджер внёс изменения в заявку</b>",
        "",
        f"Заявка <b>#{html.escape(str(rid))}</b>",
        "",
        "<b>Было (позиции):</b>",
        html.escape(build_items_summary_plain(prev) or "—"),
        "",
        "<b>Стало (позиции):</b>",
        html.escape(build_items_summary_plain(cur)),
        "",
        f"Новый итог: <b>{float(request_row.get('total_amount') or 0):.0f} ₽</b>, "
        f"стеблей: <b>{int(request_row.get('total_stems') or 0)}</b>",
    ]
    pd = request_row.get("previous_delivery_date")
    cd = request_row.get("delivery_date")
    pdd = parse_iso_date_only(pd)
    cdd = parse_iso_date_only(cd)
    if pdd and cdd and pdd != cdd:
        lines.extend(
            [
                "",
                "<b>Дата поставки:</b>",
                f"было — <b>{html.escape(format_date_ru_long(pd))}</b>",
                f"стало — <b>{html.escape(format_date_ru_long(cd))}</b>",
            ]
        )
    elif cdd and not pdd:
        lines.extend(["", f"<b>Дата поставки:</b> <b>{html.escape(format_date_ru_long(cd))}</b>"])
    if note:
        lines.extend(["", "<b>Комментарий менеджера:</b>", html.escape(note)])
    return "\n".join(lines)


def notify_client(request_row: Dict[str, Any], action: str) -> None:
    telegram_user_id = request_row.get("telegram_user_id")
    if not telegram_user_id:
        return
    if action == "approved":
        text = build_client_order_details_message(
            request_row,
            "✅ Заявка подтверждена.",
        )
        tg_send_message(telegram_user_id, text)
        return
    if action == "change_requested" and request_row.get("previous_items"):
        text = build_revision_diff_message(request_row)
        tg_send_message(telegram_user_id, text)
        return
    if action == "rejected":
        note = str(request_row.get("manager_note") or "").strip()
        msg = "❌ <b>Ваша заявка отменена менеджером.</b>"
        if note:
            msg += f"\n\n{html.escape(note)}"
        tg_send_message(telegram_user_id, msg)
        return
    if action == "rejected_by_client":
        tg_send_message(
            telegram_user_id,
            "❌ Вы отклонили заявку после правок менеджера.",
        )
        return
    texts = {
        "change_requested": "✏️ Менеджер предложил изменить заявку. Откройте приложение, чтобы увидеть детали.",
        "cancelled_by_client": "Заявка отменена вами.",
    }
    text = texts.get(action)
    if text:
        tg_send_message(telegram_user_id, text)


def salon_stats_for_telegram(telegram_id: int | str) -> Dict[str, Any]:
    r = supabase_get(
        REQUESTS_TABLE,
        {
            "select": "total_amount,total_stems",
            "telegram_user_id": f"eq.{telegram_id}",
            "status": "eq.approved",
        },
    )
    if not r.ok:
        return {"approved_rub": 0, "approved_stems": 0}
    rows = r.json() or []
    rub = sum(float(x.get("total_amount") or 0) for x in rows)
    stems = sum(int(x.get("total_stems") or 0) for x in rows)
    return {"approved_rub": round(rub, 2), "approved_stems": stems}


def build_products_from_flowers(
    flowers: List[Dict[str, Any]],
    *,
    for_admin: bool,
    reserved_map: Optional[Dict[int, int]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    reserved_map = reserved_map or {}
    categories: Dict[str, int] = {"Все": 0}
    products: List[Dict[str, Any]] = []
    for flower in flowers:
        if not for_admin and not flower_catalog_visible(flower):
            continue
        name = flower.get("name") or f"Цветок #{flower.get('id')}"
        category = name.split()[0] if name else "Каталог"
        fid = int(flower["id"])
        physical = flower_effective_stock(flower)
        reserved = int(reserved_map.get(fid, 0))
        if for_admin:
            available = physical
        else:
            available = max(0, physical - reserved)
        stock = available
        price = flower_effective_price(flower)
        item = {
            "id": flower["id"],
            "flower_type_id": flower["id"],
            "name": name,
            "price": price,
            "min": 1,
            "stock": stock,
            "stems_physical": physical,
            "reserved_pending": reserved,
            "stem": f"{flower.get('stem_length_cm')} см" if flower.get("stem_length_cm") else "—",
            "price_label": f"{price:.0f} ₽ / стебель" if price > 0 else "Цена по запросу",
            "status": "В наличии" if stock > 0 else "Нет в наличии",
            "available": stock > 0,
            "category": category,
            "image": flower_image(flower["id"]),
            "catalog_visible": flower.get("catalog_visible", True),
            "price_rub": flower.get("price_rub"),
            "stems_count": flower.get("stems_count"),
        }
        products.append(item)
        categories[category] = categories.get(category, 0) + 1
        categories["Все"] += 1

    category_list = [
        {"id": "all", "name": "Все", "count": categories.get("Все", len(products))}
    ] + [
        {"id": key, "name": key, "count": value}
        for key, value in categories.items()
        if key != "Все"
    ]
    return products, category_list


# -------------------- routes --------------------

@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp


@app.route("/api/<path:_>", methods=["OPTIONS"])
def options_api(_: str):
    return ("", 204)


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "blossom-miniapp", "time": now_iso()})


@app.get("/miniapp")
def miniapp():
    return send_from_directory("static", "index.html")


@app.get("/miniapp/<path:filename>")
def miniapp_file(filename: str):
    return send_from_directory("static", filename)


@app.post("/api/me")
def api_me():
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500

    payload = request.get_json(silent=True) or {}
    telegram_id = payload.get("telegram_id")
    if telegram_id is None or telegram_id == "":
        return jsonify({"ok": False, "error": "telegram_id is required"}), 400

    tid_norm = normalize_tg_user_id(telegram_id)
    is_admin = is_user_telegram_admin(tid_norm)

    try:
        salon = load_salon_by_tg(telegram_id)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if not salon:
        return jsonify({
            "ok": True,
            "linked": False,
            "salon": None,
            "message": "Ваш аккаунт не привязан к салону. Обратитесь к администратору.",
            "is_admin": is_admin,
            "stats": {"approved_rub": 0, "approved_stems": 0},
        })

    stats = salon_stats_for_telegram(telegram_id)
    return jsonify({
        "ok": True,
        "linked": True,
        "salon": salon,
        "is_admin": is_admin,
        "stats": stats,
    })


@app.get("/api/products")
def api_products():
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500

    try:
        flowers = load_flower_types()
        reserved = aggregate_pending_reservations()
        products, category_list = build_products_from_flowers(
            flowers, for_admin=False, reserved_map=reserved
        )
        return jsonify({"ok": True, "products": products, "categories": category_list})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/admin/catalog")
def api_admin_catalog():
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500
    _, err = require_admin_from_header()
    if err:
        return err[0], err[1]
    try:
        flowers = load_flower_types()
        reserved = aggregate_pending_reservations()
        products, _ = build_products_from_flowers(flowers, for_admin=True, reserved_map=reserved)
        return jsonify({"ok": True, "flowers": products})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/admin/catalog")
def api_admin_catalog_save():
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500
    _, err = require_admin_from_header()
    if err:
        return err[0], err[1]
    payload = request.get_json(silent=True) or {}
    rows = payload.get("flowers") or []
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "flowers array required"}), 400
    try:
        for row in rows:
            fid = row.get("id")
            if fid is None:
                continue
            patch: Dict[str, Any] = {}
            if "catalog_visible" in row:
                patch["catalog_visible"] = bool(row["catalog_visible"])
            if "price_rub" in row and row["price_rub"] is not None:
                patch["price_rub"] = round(float(row["price_rub"]), 2)
            if "stems_count" in row:
                v = row["stems_count"]
                if v is None or v == "":
                    patch["stems_count"] = None
                else:
                    patch["stems_count"] = int(v)
            if not patch:
                continue
            r = supabase_patch("flower_types", patch, params={"id": f"eq.{fid}"}, prefer_return=False)
            if not r.ok:
                return jsonify({"ok": False, "error": r.text}), 500
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/admin/request/<int:request_id>")
def api_admin_get_request(request_id: int):
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500
    _, err = require_admin_from_header()
    if err:
        return err[0], err[1]
    row = load_request_by_id(request_id)
    if not row:
        return jsonify({"ok": False, "error": "request_not_found"}), 404
    if row.get("status") not in {"new", "change_requested"}:
        return jsonify({"ok": False, "error": "request_not_editable"}), 400
    return jsonify({"ok": True, "request": row})


@app.post("/api/admin/request/<int:request_id>/revise")
def api_admin_revise_request(request_id: int):
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500
    _, err = require_admin_from_header()
    if err:
        return err[0], err[1]
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    comment = (payload.get("comment") or "").strip()
    delivery_new = (payload.get("delivery_date") or "").strip()
    if not items:
        return jsonify({"ok": False, "error": "items required"}), 400

    row = load_request_by_id(request_id)
    if not row:
        return jsonify({"ok": False, "error": "request_not_found"}), 404
    if row.get("status") not in {"new", "change_requested"}:
        return jsonify({"ok": False, "error": "request_not_editable"}), 400

    total_amount = 0.0
    total_stems = 0
    normalized: List[Dict[str, Any]] = []
    for item in items:
        qty = int(item.get("qty") or 0)
        price = parse_rub_price(item.get("price"))
        if qty <= 0:
            continue
        normalized.append({
            "id": item.get("id"),
            "name": item.get("name") or "Позиция",
            "qty": qty,
            "price": price,
        })
        total_amount += qty * price
        total_stems += qty

    if not normalized:
        return jsonify({"ok": False, "error": "no valid lines"}), 400

    previous_items = row.get("items") or []
    patch_body: Dict[str, Any] = {
        "previous_items": previous_items,
        "items": normalized,
        "total_amount": round(total_amount, 2),
        "total_stems": total_stems,
        "manager_note": comment if comment else None,
        "status": "change_requested",
        "updated_at": now_iso(),
    }
    if delivery_new:
        if len(delivery_new) < 10 or delivery_new[4] != "-" or delivery_new[7] != "-":
            return jsonify({"ok": False, "error": "invalid_delivery_date"}), 400
        dshort = delivery_new[:10]
        prev_d = row.get("delivery_date")
        if prev_d and str(prev_d)[:10] != dshort:
            patch_body["previous_delivery_date"] = prev_d
        patch_body["delivery_date"] = dshort

    upd = supabase_patch(
        REQUESTS_TABLE,
        patch_body,
        params={"id": f"eq.{request_id}"},
        prefer_return=True,
    )
    if not upd.ok:
        return jsonify({"ok": False, "error": upd.text}), 500
    updated = (upd.json() or [row])[0]

    admin_message_id = row.get("admin_message_id")
    if admin_message_id and ADMIN_CHAT_ID:
        tg_clear_buttons(ADMIN_CHAT_ID, admin_message_id)
    if ADMIN_CHAT_ID:
        note_line = f"Комментарий: {html.escape(comment)}" if comment else "Комментарий не указан."
        tg_send_message(
            ADMIN_CHAT_ID,
            f"✏️ Заявка #{request_id} изменена менеджером.\n{build_items_summary(normalized)}\n"
            f"Итого: <b>{total_amount:.0f} ₽</b>, стеблей: <b>{total_stems}</b>\n"
            f"{note_line}",
        )
    notify_client(updated, "change_requested")
    return jsonify({"ok": True, "request": updated})


@app.post("/api/admin/request/<int:request_id>/approve")
def api_admin_approve_request(request_id: int):
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500
    _, err = require_admin_from_header()
    if err:
        return err[0], err[1]
    row = load_request_by_id(request_id)
    if not row:
        return jsonify({"ok": False, "error": "request_not_found"}), 404
    if row.get("status") not in {"new", "change_requested"}:
        return jsonify({"ok": False, "error": "request_not_editable"}), 400

    items_for_inventory = row.get("items") or []
    upd = supabase_patch(
        REQUESTS_TABLE,
        {"status": "approved", "updated_at": now_iso()},
        params={"id": f"eq.{request_id}"},
        prefer_return=True,
    )
    if not upd.ok:
        return jsonify({"ok": False, "error": upd.text}), 500
    updated = (upd.json() or [row])[0]
    deduct_stems_from_inventory_for_items(items_for_inventory)

    admin_message_id = row.get("admin_message_id")
    if admin_message_id and ADMIN_CHAT_ID:
        tg_clear_buttons(ADMIN_CHAT_ID, admin_message_id)
    if ADMIN_CHAT_ID:
        tg_send_message(
            ADMIN_CHAT_ID,
            f"✅ Заявка <b>#{request_id}</b> подтверждена через мини-приложение.",
        )
    notify_client(updated, "approved")
    return jsonify({"ok": True, "request": updated})


ADMIN_DELETABLE_STATUSES = frozenset({"rejected", "cancelled_by_client", "rejected_by_client"})


@app.delete("/api/admin/request/<int:request_id>")
def api_admin_delete_request(request_id: int):
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500
    _, err = require_admin_from_header()
    if err:
        return err[0], err[1]
    row = load_request_by_id(request_id)
    if not row:
        return jsonify({"ok": False, "error": "request_not_found"}), 404
    if row.get("status") not in ADMIN_DELETABLE_STATUSES:
        return jsonify({"ok": False, "error": "request_not_deletable"}), 400

    res = supabase_delete(REQUESTS_TABLE, params={"id": f"eq.{request_id}"}, prefer_return=False)
    if not res.ok:
        return jsonify({"ok": False, "error": res.text}), 500
    return jsonify({"ok": True})


@app.post("/api/feedback")
def api_feedback():
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500
    payload = request.get_json(silent=True) or {}
    telegram_id = payload.get("telegram_id")
    text = (payload.get("message") or "").strip()
    if not telegram_id:
        return jsonify({"ok": False, "error": "telegram_id is required"}), 400
    if len(text) < 3:
        return jsonify({"ok": False, "error": "message too short"}), 400
    try:
        salon = load_salon_by_tg(telegram_id)
        if not salon:
            return jsonify({"ok": False, "error": "guest_forbidden"}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    if not ADMIN_CHAT_ID:
        return jsonify({"ok": False, "error": "admin not configured"}), 500
    name = html.escape(str(payload.get("telegram_name") or "Клиент"))
    salon_name = html.escape(str(salon.get("name") or "Салон"))
    tg_send_message(
        ADMIN_CHAT_ID,
        f"💬 <b>Обратная связь</b>\nСалон: <b>{salon_name}</b>\nОт: {name} (<code>{html.escape(str(telegram_id))}</code>)\n\n{html.escape(text)}",
    )
    return jsonify({"ok": True})


def load_all_requests_admin(*, limit: int = 300) -> List[Dict[str, Any]]:
    r = supabase_get(
        REQUESTS_TABLE,
        {
            "select": REQUESTS_SELECT_FULL,
            "order": "created_at.desc",
            "limit": limit,
        },
    )
    if r.ok:
        return r.json() or []
    r2 = supabase_get(
        REQUESTS_TABLE,
        {
            "select": REQUESTS_SELECT_BASIC,
            "order": "created_at.desc",
            "limit": limit,
        },
    )
    if not r2.ok:
        raise RuntimeError(r2.text)
    return r2.json() or []


def load_requests_for_user(telegram_id: int | str) -> List[Dict[str, Any]]:
    r = supabase_get(
        REQUESTS_TABLE,
        {
            "select": REQUESTS_SELECT_FULL,
            "telegram_user_id": f"eq.{telegram_id}",
            "order": "created_at.desc",
        },
    )
    if r.ok:
        return r.json() or []
    r2 = supabase_get(
        REQUESTS_TABLE,
        {
            "select": REQUESTS_SELECT_BASIC,
            "telegram_user_id": f"eq.{telegram_id}",
            "order": "created_at.desc",
        },
    )
    if not r2.ok:
        raise RuntimeError(r2.text)
    return r2.json() or []


@app.post("/api/my-requests")
def api_my_requests():
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500

    payload = request.get_json(silent=True) or {}
    telegram_id = payload.get("telegram_id")
    if not telegram_id:
        return jsonify({"ok": False, "error": "telegram_id is required"}), 400

    try:
        rows = load_requests_for_user(telegram_id)
        return jsonify({"ok": True, "requests": rows})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/order")
def api_order():
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500

    payload = request.get_json(silent=True) or {}
    telegram_id = payload.get("telegram_id")
    items = payload.get("items") or []
    telegram_name = (payload.get("telegram_name") or "Клиент").strip() or "Клиент"
    telegram_username = (payload.get("telegram_username") or "").strip().lstrip("@")

    if not telegram_id:
        return jsonify({"ok": False, "error": "telegram_id is required"}), 400
    if not items:
        return jsonify({"ok": False, "error": "items are required"}), 400

    try:
        salon = load_salon_by_tg(telegram_id)
        if not salon:
            return jsonify({"ok": False, "error": "salon_not_linked", "message": "Заказы доступны только привязанным салонам."}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    total_amount = 0.0
    total_stems = 0
    normalized_items: List[Dict[str, Any]] = []
    for item in items:
        qty = int(item.get("qty") or 0)
        price = parse_rub_price(item.get("price"))
        if qty <= 0:
            continue
        normalized_items.append({
            "id": item.get("id"),
            "name": item.get("name") or "Позиция",
            "qty": qty,
            "price": price,
        })
        total_amount += qty * price
        total_stems += qty

    if not normalized_items:
        return jsonify({"ok": False, "error": "cart is empty"}), 400

    delivery_raw = (payload.get("delivery_date") or "").strip()
    delivery_date: Optional[str] = None
    if delivery_raw:
        if len(delivery_raw) >= 10 and delivery_raw[4] == "-" and delivery_raw[7] == "-":
            delivery_date = delivery_raw[:10]
        else:
            return jsonify({"ok": False, "error": "invalid_delivery_date"}), 400

    try:
        reserved_map = aggregate_pending_reservations()
        flowers = load_flower_types()
        by_id = {int(f["id"]): f for f in flowers}
        for it in normalized_items:
            try:
                fi = int(it.get("id"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "invalid_item_id"}), 400
            frow = by_id.get(fi)
            if not frow:
                return jsonify({"ok": False, "error": "unknown_flower"}), 400
            phys = flower_effective_stock(frow)
            res = reserved_map.get(fi, 0)
            avail = max(0, phys - res)
            qty = int(it.get("qty") or 0)
            if qty > avail:
                return jsonify({"ok": False, "error": "insufficient_stock", "flower_id": fi, "available": avail}), 400

        salon_id = salon.get("id")
        salon_name = salon.get("name") or "Салон"

        insert_payload: Dict[str, Any] = {
            "telegram_user_id": telegram_id,
            "salon_id": salon_id,
            "salon_name": salon_name,
            "status": "new",
            "items": normalized_items,
            "total_amount": round(total_amount, 2),
            "total_stems": total_stems,
        }
        if delivery_date:
            insert_payload["delivery_date"] = delivery_date
        ins = supabase_post(REQUESTS_TABLE, insert_payload, prefer_return=True)
        if not ins.ok:
            return jsonify({"ok": False, "error": ins.text}), 500
        created = (ins.json() or [None])[0]
        if not created:
            return jsonify({"ok": False, "error": "request_not_created"}), 500

        manager_link = build_telegram_link(telegram_id, telegram_username, telegram_name)
        dd_line = (
            f"Дата поставки: <b>{html.escape(format_date_ru_long(delivery_date))}</b>\n" if delivery_date else ""
        )
        text = (
            f"🌸 <b>Новая заявка #{created['id']}</b>\n"
            f"Салон: <b>{html.escape(salon_name)}</b>\n"
            f"Менеджер: {manager_link}\n"
            f"{dd_line}"
            f"Стеблей: <b>{total_stems}</b>\n"
            f"Сумма: <b>{total_amount:.0f} ₽</b>\n\n"
            f"{build_items_summary(normalized_items)}"
        )
        buttons = [
            [
                {"text": "✅ Подтвердить", "callback_data": f"req:{created['id']}:approve"},
                {"text": "❌ Отмена", "callback_data": f"req:{created['id']}:reject"},
            ],
            [
                {"text": "✏️ Изменить заявку", "callback_data": f"req:{created['id']}:edit"},
            ],
        ]

        message = tg_send_message(ADMIN_CHAT_ID, text, buttons) if ADMIN_CHAT_ID else None
        if message and message.get("result", {}).get("message_id"):
            supabase_patch(
                REQUESTS_TABLE,
                {"admin_message_id": message["result"]["message_id"], "updated_at": now_iso()},
                params={"id": f"eq.{created['id']}"},
                prefer_return=False,
            )

        return jsonify({"ok": True, "request": created})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/request/cancel")
def api_cancel_request():
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500

    payload = request.get_json(silent=True) or {}
    telegram_id = payload.get("telegram_id")
    request_id = payload.get("request_id")
    if not telegram_id or not request_id:
        return jsonify({"ok": False, "error": "telegram_id and request_id are required"}), 400

    try:
        row = load_request_by_id(request_id)
        if not row:
            return jsonify({"ok": False, "error": "request_not_found"}), 404
        if str(row.get("telegram_user_id")) != str(telegram_id):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if row.get("status") not in {"new", "change_requested"}:
            return jsonify({"ok": False, "error": "request_cannot_be_cancelled"}), 400

        new_status = "rejected_by_client" if row.get("status") == "change_requested" else "cancelled_by_client"
        upd = supabase_patch(
            REQUESTS_TABLE,
            {"status": new_status, "updated_at": now_iso()},
            params={"id": f"eq.{request_id}"},
            prefer_return=True,
        )
        if not upd.ok:
            return jsonify({"ok": False, "error": upd.text}), 500
        updated = (upd.json() or [row])[0]

        admin_message_id = row.get("admin_message_id")
        if admin_message_id and ADMIN_CHAT_ID:
            tg_clear_buttons(ADMIN_CHAT_ID, admin_message_id)
        if ADMIN_CHAT_ID:
            if new_status == "rejected_by_client":
                adm_txt = f"ℹ️ Клиент <b>отклонил</b> заявку #{request_id} после правок.\nСалон: <b>{html.escape(str(row.get('salon_name') or '—'))}</b>"
            else:
                adm_txt = f"ℹ️ Клиент отменил заявку #{request_id}.\nСалон: <b>{html.escape(str(row.get('salon_name') or '—'))}</b>"
            tg_send_message(ADMIN_CHAT_ID, adm_txt)
        notify_client(updated, new_status)
        return jsonify({"ok": True, "request": updated})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/admin/request/<int:request_id>/reject")
def api_admin_reject_request(request_id: int):
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500
    _, err = require_admin_from_header()
    if err:
        return err[0], err[1]
    payload = request.get_json(silent=True) or {}
    comment = (payload.get("comment") or "").strip()
    if len(comment) < 2:
        return jsonify({"ok": False, "error": "comment required"}), 400

    row = load_request_by_id(request_id)
    if not row:
        return jsonify({"ok": False, "error": "request_not_found"}), 404
    if row.get("status") not in {"new", "change_requested"}:
        return jsonify({"ok": False, "error": "request_not_editable"}), 400

    upd = supabase_patch(
        REQUESTS_TABLE,
        {
            "status": "rejected",
            "manager_note": comment,
            "updated_at": now_iso(),
        },
        params={"id": f"eq.{request_id}"},
        prefer_return=True,
    )
    if not upd.ok:
        return jsonify({"ok": False, "error": upd.text}), 500
    updated = (upd.json() or [row])[0]

    admin_message_id = row.get("admin_message_id")
    if admin_message_id and ADMIN_CHAT_ID:
        tg_clear_buttons(ADMIN_CHAT_ID, admin_message_id)
    if ADMIN_CHAT_ID:
        tg_send_message(
            ADMIN_CHAT_ID,
            f"❌ Заявка #{request_id} отменена менеджером с комментарием.",
        )
    notify_client(updated, "rejected")
    return jsonify({"ok": True, "request": updated})


@app.get("/api/admin/requests")
def api_admin_requests_list():
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500
    _, err = require_admin_from_header()
    if err:
        return err[0], err[1]
    try:
        rows = load_all_requests_admin()
        return jsonify({"ok": True, "requests": rows})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/request/revision-response")
def api_revision_response():
    error = ensure_env()
    if error:
        return jsonify({"ok": False, "error": error}), 500

    payload = request.get_json(silent=True) or {}
    telegram_id = payload.get("telegram_id")
    request_id = payload.get("request_id")
    decision = (payload.get("decision") or "").strip().lower()
    if not telegram_id or not request_id:
        return jsonify({"ok": False, "error": "telegram_id and request_id are required"}), 400
    if decision not in {"accept", "reject"}:
        return jsonify({"ok": False, "error": "decision must be accept or reject"}), 400

    try:
        row = load_request_by_id(request_id)
        if not row:
            return jsonify({"ok": False, "error": "request_not_found"}), 404
        if str(row.get("telegram_user_id")) != str(telegram_id):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if row.get("status") != "change_requested":
            return jsonify({"ok": False, "error": "not_awaiting_client_response"}), 400

        if decision == "accept":
            patch_body: Dict[str, Any] = {
                "status": "approved",
                "previous_items": None,
                "manager_note": None,
                "updated_at": now_iso(),
            }
        else:
            prev = row.get("previous_items") or []
            if not prev:
                return jsonify({"ok": False, "error": "no_previous_items"}), 400
            total_amount, total_stems = totals_from_items(prev)
            patch_body = {
                "items": prev,
                "previous_items": None,
                "manager_note": None,
                "status": "new",
                "total_amount": total_amount,
                "total_stems": total_stems,
                "updated_at": now_iso(),
            }

        upd = supabase_patch(
            REQUESTS_TABLE,
            patch_body,
            params={"id": f"eq.{request_id}"},
            prefer_return=True,
        )
        if not upd.ok:
            return jsonify({"ok": False, "error": upd.text}), 500
        updated = (upd.json() or [row])[0]

        salon_label = html.escape(str(updated.get("salon_name") or "Салон"))
        if decision == "accept":
            deduct_stems_from_inventory_for_items(updated.get("items") or [])
            if ADMIN_CHAT_ID:
                tg_send_message(
                    ADMIN_CHAT_ID,
                    f"✅ Клиент <b>принял</b> правки по заявке #{request_id}.\nСалон: <b>{salon_label}</b>\n"
                    f"Итого: <b>{float(updated.get('total_amount') or 0):.0f} ₽</b>, "
                    f"стеблей: <b>{int(updated.get('total_stems') or 0)}</b>",
                )
            tg_send_message(
                telegram_id,
                "✅ <b>Вы приняли изменения.</b>\n"
                f"Заявка <b>#{html.escape(str(request_id))}</b> подтверждена с новым составом.",
            )
        else:
            if ADMIN_CHAT_ID:
                tg_send_message(
                    ADMIN_CHAT_ID,
                    f"↩️ Клиент <b>отклонил</b> правки по заявке #{request_id}. Восстановлен прежний состав.\n"
                    f"Салон: <b>{salon_label}</b>",
                )
            tg_send_message(
                telegram_id,
                "↩️ <b>Изменения отклонены.</b>\n"
                f"Заявка <b>#{html.escape(str(request_id))}</b> снова на рассмотрении в прежнем виде.",
            )

        return jsonify({"ok": True, "request": updated})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/webhook")
def webhook():
    payload = request.get_json(silent=True) or {}
    callback_query = payload.get("callback_query")
    if not callback_query:
        return jsonify({"ok": True})

    callback_id = callback_query.get("id")
    data = callback_query.get("data") or ""
    message = callback_query.get("message") or {}
    admin_message_id = message.get("message_id")

    if not data.startswith("req:"):
        if callback_id:
            tg_answer_callback(callback_id, "Неизвестное действие")
        return jsonify({"ok": True})

    try:
        _, raw_request_id, action = data.split(":", 2)
        request_row = load_request_by_id(raw_request_id)
        if not request_row:
            if callback_id:
                tg_answer_callback(callback_id, "Заявка не найдена")
            return jsonify({"ok": True})

        current_status = request_row.get("status")
        if current_status not in {"new", "change_requested"}:
            if callback_id:
                tg_answer_callback(callback_id, f"Уже обработано: {request_status_label(str(current_status))}")
            if admin_message_id and ADMIN_CHAT_ID:
                tg_clear_buttons(ADMIN_CHAT_ID, admin_message_id)
            return jsonify({"ok": True})

        if action == "edit":
            base = miniapp_base_url_for_request()
            if not base:
                if callback_id:
                    tg_answer_callback(
                        callback_id,
                        "Не удалось собрать ссылку на приложение. Укажите MINIAPP_BASE_URL "
                        "(https://ваш-сервис.onrender.com) в переменных окружения.",
                    )
                return jsonify({"ok": True})
            if callback_id:
                tg_answer_callback(callback_id, "Откройте редактор в мини-приложении")
            web_url = f"{base}/miniapp#adminReq={raw_request_id}"
            client_tg = request_row.get("telegram_user_id")
            if client_tg:
                tg_send_message(
                    client_tg,
                    "✏️ <b>Менеджер открыл заявку для правок.</b>\n"
                    f"Заявка <b>#{html.escape(str(raw_request_id))}</b> — скорее всего вам пришлют обновлённый состав. "
                    "Откройте мини-приложение: там можно будет <b>принять</b> или <b>отклонить</b> изменения.",
                )
            if ADMIN_CHAT_ID:
                tg_send_message(
                    ADMIN_CHAT_ID,
                    f"✏️ Редактор заявки <b>#{html.escape(str(raw_request_id))}</b>\n"
                    f"Измените состав, цены и оставьте комментарий клиенту.",
                    buttons=[[{"text": "Открыть редактор", "web_app": {"url": web_url}}]],
                )
            return jsonify({"ok": True})

        if action == "approve":
            new_status = "approved"
            answer_text = "Заявка подтверждена"
        elif action == "reject":
            new_status = "rejected"
            answer_text = "Заявка отменена"
        else:
            if callback_id:
                tg_answer_callback(callback_id, "Неизвестное действие")
            return jsonify({"ok": True})

        items_for_inventory = request_row.get("items") or []
        patch_cb: Dict[str, Any] = {"status": new_status, "updated_at": now_iso()}
        if new_status == "rejected":
            patch_cb["manager_note"] = None
        upd = supabase_patch(
            REQUESTS_TABLE,
            patch_cb,
            params={"id": f"eq.{raw_request_id}"},
            prefer_return=True,
        )
        if upd.ok:
            request_row = (upd.json() or [request_row])[0]
            if new_status == "approved":
                deduct_stems_from_inventory_for_items(items_for_inventory)

        if callback_id:
            tg_answer_callback(callback_id, answer_text)
        if admin_message_id and ADMIN_CHAT_ID:
            tg_clear_buttons(ADMIN_CHAT_ID, admin_message_id)
        if ADMIN_CHAT_ID:
            tg_send_message(ADMIN_CHAT_ID, f"Статус заявки #{raw_request_id}: <b>{request_status_label(new_status)}</b>")
        notify_client(request_row, new_status)
        return jsonify({"ok": True})
    except Exception as exc:
        if callback_id:
            tg_answer_callback(callback_id, "Ошибка обработки")
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
