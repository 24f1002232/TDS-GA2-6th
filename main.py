import time
import uuid
import logging
import json
import re
import base64
from datetime import datetime
from collections import deque
from typing import Optional

from fastapi import FastAPI, Request, Header, Query, Body, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response, JSONResponse
from pydantic import BaseModel
from dateutil import parser as dateparser

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

EMAIL = "24f1002232@ds.study.iitm.ac.in"
START_TIME = time.time()

REQUEST_COUNTER = Counter("http_requests_total", "Total HTTP requests")
LOG_BUFFER = deque(maxlen=1000)


def add_log(level: str, path: str, request_id: str, **extra):
    entry = {
        "level": level,
        "ts": time.time(),
        "path": path,
        "request_id": request_id,
    }
    entry.update(extra)
    LOG_BUFFER.append(entry)
    logging.info(json.dumps(entry))


# ---------- Rate limiting config ----------
RATE_LIMIT = 17
RATE_WINDOW = 10  # seconds
rate_buckets = {}  # client_id -> deque of timestamps


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_id = request.headers.get("X-Client-Id")
    if client_id:
        now = time.time()
        bucket = rate_buckets.setdefault(client_id, deque())
        while bucket and now - bucket[0] > RATE_WINDOW:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT:
            retry_after = max(1, int(RATE_WINDOW - (now - bucket[0])) + 1)
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limit_exceeded"},
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
    response = await call_next(request)
    return response


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    REQUEST_COUNTER.inc()
    add_log("info", request.url.path, request_id, method=request.method)
    response = await call_next(request)
    return response


# ---------- Observability endpoints ----------

@app.get("/work")
def work(n: int = 1):
    total = 0
    for i in range(n):
        total += i * i
    return {"email": EMAIL, "done": n}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "uptime_s": time.time() - START_TIME}


@app.get("/logs/tail")
def logs_tail(limit: int = 20):
    entries = list(LOG_BUFFER)[-limit:]
    return entries


# ---------- Invoice extraction endpoint ----------

class ExtractRequest(BaseModel):
    text: str = ""


class ExtractResponse(BaseModel):
    vendor: str
    amount: float
    currency: str
    date: str


CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}

VENDOR_SUFFIXES = (
    r"Industries|Ltd\.?|Inc\.?|LLC|Corp\.?|Corporation|Limited|"
    r"Company|Co\.?|GmbH|Solutions|Group|Enterprises|Systems|Technologies"
)

VENDOR_PATTERNS = [
    re.compile(r"(?:from|Vendor|Bill(?:ed)? [Ff]rom|Seller)\s*[:\-]?\s*([A-Z][\w&\-]*(?:\s+[A-Z][\w&\-]*)*(?:\s+(?:" + VENDOR_SUFFIXES + r")\.?)?)", re.MULTILINE),
    re.compile(r"\b([A-Z][\w&\-]*(?:\s+[A-Z][\w&\-]*)*\s+(?:" + VENDOR_SUFFIXES + r")\.?)"),
]

DATE_ISO_PATTERN = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

AMOUNT_CONTEXT_PATTERN = re.compile(
    r"(?:total(?:\s+due)?|amount(?:\s+due)?|balance(?:\s+due)?|grand\s+total|due)\s*[:\-]?\s*"
    r"[\$€£]?\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

ANY_AMOUNT_PATTERN = re.compile(r"[\$€£]?\s*([\d,]+\.\d{2})\b")
ANY_NUMBER_PATTERN = re.compile(r"\b(\d{2,5}(?:\.\d{1,2})?)\b")


def extract_vendor(text: str) -> str:
    for pattern in VENDOR_PATTERNS:
        m = pattern.search(text)
        if m:
            candidate = m.group(1).strip().rstrip(".,")
            if len(candidate) > 1:
                return candidate
    m = re.search(r"\b([A-Z][a-zA-Z\-]+(?:\s+[A-Z][a-zA-Z\-]+){0,3})\b", text)
    if m:
        return m.group(1).strip()
    return "Unknown Vendor"


def extract_currency(text: str) -> str:
    m = re.search(r"\b(USD|EUR|GBP)\b", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return "USD"


def extract_amount(text: str) -> float:
    m = AMOUNT_CONTEXT_PATTERN.search(text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = ANY_AMOUNT_PATTERN.search(text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = ANY_NUMBER_PATTERN.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return 0.0


def extract_date(text: str) -> str:
    m = DATE_ISO_PATTERN.search(text)
    if m:
        return m.group(1)
    for line in text.splitlines():
        if re.search(r"due|date", line, re.IGNORECASE):
            try:
                dt = dateparser.parse(line, fuzzy=True, dayfirst=False)
                if dt:
                    return dt.strftime("%Y-%m-%d")
            except (ValueError, OverflowError):
                continue
    try:
        dt = dateparser.parse(text, fuzzy=True, dayfirst=False)
        if dt:
            return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        pass
    return datetime.utcnow().strftime("%Y-%m-%d")


@app.post("/extract", response_model=ExtractResponse)
def extract(req: ExtractRequest):
    text = req.text or ""
    if not text.strip():
        return ExtractResponse(vendor="Unknown Vendor", amount=0.0, currency="USD", date=datetime.utcnow().strftime("%Y-%m-%d"))

    try:
        vendor = extract_vendor(text)
        amount = extract_amount(text)
        currency = extract_currency(text)
        date = extract_date(text)
    except Exception:
        return ExtractResponse(vendor="Unknown Vendor", amount=0.0, currency="USD", date=datetime.utcnow().strftime("%Y-%m-%d"))

    return ExtractResponse(vendor=vendor, amount=amount, currency=currency, date=date)


# ---------- Orders API: idempotency + pagination ----------

TOTAL_ORDERS = 41

ORDERS_CATALOG = [
    {"id": i, "email": EMAIL, "amount": round(10 + i * 3.37, 2), "product": f"Product-{i}"}
    for i in range(1, TOTAL_ORDERS + 1)
]

idempotency_store = {}  # key -> order dict
next_order_id = TOTAL_ORDERS + 1


def encode_cursor(offset: int) -> str:
    raw = str(offset).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        return int(raw.decode())
    except Exception:
        return 0


@app.post("/orders")
def create_order(
    payload: dict = Body(default={}),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    global next_order_id

    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header required")

    if idempotency_key in idempotency_store:
        order = idempotency_store[idempotency_key]
        return JSONResponse(status_code=200, content=order)

    order_id = next_order_id
    next_order_id += 1

    order = {"id": order_id, "email": EMAIL}
    if isinstance(payload, dict):
        order.update(payload)
    order["id"] = order_id  # id always authoritative

    idempotency_store[idempotency_key] = order
    return JSONResponse(status_code=201, content=order)


@app.get("/orders")
def list_orders(limit: int = Query(10, gt=0), cursor: Optional[str] = Query(None)):
    offset = decode_cursor(cursor) if cursor else 0
    items = ORDERS_CATALOG[offset: offset + limit]
    new_offset = offset + len(items)
    next_cursor = encode_cursor(new_offset) if new_offset < len(ORDERS_CATALOG) else None
    return {
        "items": items,
        "next_cursor": next_cursor,
        "next": next_cursor,
        "orders": items,
    }

@app.get("/debug/bucket")
def debug_bucket(client_id: str):
    now = time.time()
    bucket = rate_buckets.get(client_id, deque())
    active = [t for t in bucket if now - t <= RATE_WINDOW]
    return {"client_id": client_id, "count_in_window": len(active), "limit": RATE_LIMIT}
