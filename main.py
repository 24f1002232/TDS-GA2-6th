import time
import uuid
import logging
import json
import re
from datetime import datetime
from collections import deque
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response
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
    re.compile(r"(?:from|Vendor|Bill(?:ed)? [Ff]rom|Seller)\s*[:\-]?\s*([A-Z][\w&\-\.]*(?:\s+[A-Z][\w&\-\.]*)*(?:\s+(?:" + VENDOR_SUFFIXES + r"))?)", re.MULTILINE),
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
