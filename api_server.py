"""
Rudra Public API — lets developers/users check stock and buy accounts
programmatically, using an api_key (per Telegram user).

Runs as its OWN process (separate Railway service), talking to the SAME
MongoDB the bot uses — no data duplication, no separate credit system.

Run locally:
    uvicorn api_server:app --host 0.0.0.0 --port 8000

On Railway: create a second service in the same repo with start command:
    uvicorn api_server:app --host 0.0.0.0 --port $PORT
(set the same MONGO_URI / DB_NAME env vars as the bot service)
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from database import Repo, get_db

app = FastAPI(title="Rudra API", version="1.0")
repo = Repo(get_db())


@app.middleware("http")
async def log_requests(request: Request, call_next):
    response = await call_next(request)
    try:
        api_key = request.headers.get("x-api-key") or request.headers.get("X-API-Key")
        user_id = 0
        if api_key:
            u = await repo.get_user_by_api_key(api_key)
            if u:
                user_id = int(u["user_id"])
        client_ip = request.client.host if request.client else None
        await repo.log_api_call(
            user_id=user_id,
            endpoint=f"{request.method} {request.url.path}",
            ip=client_ip,
            ok=response.status_code < 400,
        )
    except Exception:
        pass
    return response


# ---------- helpers ----------
async def _auth(api_key: Optional[str]) -> dict:
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing api_key")
    user = await repo.get_user_by_api_key(api_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid api_key")
    return user


# ---------- schemas ----------
class BuyRequest(BaseModel):
    country: str
    year: Optional[int] = None


# ---------- endpoints ----------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/countries")
async def list_countries():
    """Public — no api_key needed. Live stock + price per country."""
    countries = await repo.list_available_countries()
    out = []
    for c in countries:
        code = c.get("country") or "?"
        count = c.get("count", 0)
        pr = await repo.available_price_range(country=code, year=None)
        out.append({
            "country": code,
            "stock": count,
            "min_price": pr.get("min_price"),
            "max_price": pr.get("max_price"),
        })
    return {"countries": out}


@app.get("/v1/balance")
async def balance(x_api_key: Optional[str] = Header(None)):
    user = await _auth(x_api_key)
    return {"user_id": user["user_id"], "credits": int(user.get("credits", 0))}


def _clean(doc: dict) -> dict:
    """Strip internal Mongo fields (_id, datetimes) so FastAPI can JSON-encode it."""
    out = {}
    for k, v in doc.items():
        if k == "_id":
            continue
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


@app.post("/v1/buy")
async def buy(req: BuyRequest, x_api_key: Optional[str] = Header(None)):
    user = await _auth(x_api_key)
    account, reason = await repo.buy_account_filtered(
        user_id=user["user_id"],
        username=user.get("username", ""),
        country=req.country,
        year=req.year,
    )
    if not account:
        raise HTTPException(status_code=400, detail=f"Purchase failed: {reason}")
    await repo.queue_otp_connect(account["_id"], user["user_id"])
    # NOTE: OTP forwarding now starts automatically within a few seconds —
    # the bot process picks this purchase up and begins watching for the
    # login code, then sends it to you in your Telegram chat with the bot
    # (same as buying directly inside the bot).
    return {
        "phone": account.get("phone"),
        "country": account.get("country"),
        "year": account.get("year"),
        "twofa_password": account.get("twofa_password"),
        "price": account.get("price"),
        "note": "Login with this number in Telegram. The OTP will be forwarded to you in your Telegram chat with the bot within a few seconds.",
    }


@app.get("/v1/history")
async def history(x_api_key: Optional[str] = Header(None), page: int = 0):
    user = await _auth(x_api_key)
    items = await repo.list_purchases_page(user_id=user["user_id"], page=page, page_size=20)
    return {"page": page, "items": [_clean(i) for i in items]}
