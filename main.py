import time
import uuid
import logging
import json
from collections import deque
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

EMAIL = "YOUR_LOGIN_EMAIL_HERE"  # <-- replace with your actual login email
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


@app.get("/work")
def work(n: int = 1):
    total = 0
    for i in range(n):
        total += i * i  # trivial unit of work
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