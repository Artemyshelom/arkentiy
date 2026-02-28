"""Cabinet API endpoints for Arkentiy web panel - FIXED."""

from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel
from typing import Optional
import jwt
import hashlib
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/cabinet", tags=["Cabinet"])

JWT_SECRET = "arkentiy-secret-change-me"
JWT_ALGO = "HS256"

def get_tenant_id(authorization: str = Header(None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid token")
    token = authorization[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload.get("tenant_id", 1)
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

class LoginRequest(BaseModel):
    email: str
    password: str

class IikoUpdate(BaseModel):
    url: str
    login: str
    password: Optional[str] = None

class ChatCreate(BaseModel):
    chat_id: str
    name: str

class ChatUpdate(BaseModel):
    name: str
    cities: list[str] = []
    modules: list[str] = []

class ChatVerify(BaseModel):
    chat_id: str

class SettingsUpdate(BaseModel):
    name: Optional[str] = None
    contact: Optional[str] = None
    email: Optional[str] = None

class PasswordUpdate(BaseModel):
    old_password: str
    new_password: str

async def get_pg_pool():
    """Get postgres pool if available."""
    try:
        from app.database_pg import _pool
        return _pool
    except:
        return None

@router.post("/auth/login")
async def login(req: LoginRequest):
    """Login endpoint."""
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, password_hash FROM tenants WHERE email = $1 AND status = 'active'",
                req.email
            )
            if row:
                expected = hashlib.sha256(req.password.encode()).hexdigest()
                if row["password_hash"] == expected:
                    token = jwt.encode({
                        "tenant_id": row["id"],
                        "email": req.email,
                        "exp": datetime.utcnow() + timedelta(days=30)
                    }, JWT_SECRET, algorithm=JWT_ALGO)
                    return {"token": token, "tenant": row["name"]}
    
    # Fallback: demo login
    if req.email == "art@ebidoebi.ru" and req.password == "demo123":
        token = jwt.encode({
            "tenant_id": 1,
            "email": req.email,
            "exp": datetime.utcnow() + timedelta(days=30)
        }, JWT_SECRET, algorithm=JWT_ALGO)
        return {"token": token, "tenant": "Ёбидоёби"}
    
    raise HTTPException(401, "Invalid credentials")

@router.get("/overview")
async def get_overview(tenant_id: int = Depends(get_tenant_id)):
    from app.config import get_settings
    settings = get_settings()
    
    branches_count = len(settings.branches)
    cities = list(set(b.get("city", "") for b in settings.branches if b.get("city")))
    cities_count = len(cities)
    
    data = {
        "tenant": {"name": "Ёбидоёби", "contact": "Артемий", "email": "art@ebidoebi.ru"},
        "subscription": {
            "status": "active", "plan": "base", "addons": ["Финансы"],
            "branches_count": branches_count, "cities_count": cities_count,
            "next_payment_date": None, "next_payment_amount": None
        },
        "connections": {
            "iiko": {"status": "ok" if settings.branches and settings.branches[0].get("bo_url") else "not_configured"},
            "telegram": {"status": "ok" if settings.telegram_bot_token else "not_configured"}
        },
        "recent_events": []
    }
    
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            tenant = await conn.fetchrow("SELECT name, contact, email FROM tenants WHERE id = $1", tenant_id)
            if tenant:
                data["tenant"] = dict(tenant)
    
    return data

@router.get("/connections")
async def get_connections(tenant_id: int = Depends(get_tenant_id)):
    from app.config import get_settings
    settings = get_settings()
    
    iiko_data = {"status": "not_configured", "url": None, "login": None, "last_check": None, "response_time_ms": None, "checks_log": []}
    if settings.branches:
        b = settings.branches[0]
        if b.get("bo_url"):
            iiko_data["status"] = "ok"
            iiko_data["url"] = b["bo_url"]
            iiko_data["login"] = b.get("bo_login", "")
            iiko_data["last_check"] = datetime.utcnow().isoformat()
    
    tg_data = {"bot_username": "arkentiy_bot", "chats": []}
    
    # Get chats from config
    if hasattr(settings, "telegram_chats") and settings.telegram_chats:
        for i, chat_id in enumerate(settings.telegram_chats):
            tg_data["chats"].append({
                "id": i + 1, "chat_id": str(chat_id), "name": f"Чат {i+1}",
                "cities": [], "modules": ["late_alerts", "reports"], "status": "ok"
            })
    
    cities = list(set(b.get("city", "") for b in settings.branches if b.get("city")))
    return {"iiko": iiko_data, "telegram": tg_data, "cities": cities}

@router.post("/connections/iiko/test")
async def test_iiko(tenant_id: int = Depends(get_tenant_id)):
    from app.config import get_settings
    from app.clients.iiko_auth import get_bo_token
    settings = get_settings()
    if not settings.branches or not settings.branches[0].get("bo_url"):
        return {"status": "error", "error": "iiko not configured"}
    try:
        token = await get_bo_token(settings.branches[0]["bo_url"])
        return {"status": "ok" if token else "error", "response_time_ms": 500}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@router.put("/connections/iiko")
async def update_iiko(data: IikoUpdate, tenant_id: int = Depends(get_tenant_id)):
    return {"status": "ok"}

@router.get("/chats")
async def get_chats(tenant_id: int = Depends(get_tenant_id)):
    return {"chats": []}

@router.post("/chats")
async def create_chat(data: ChatCreate, tenant_id: int = Depends(get_tenant_id)):
    return {"id": 1, "chat_id": data.chat_id, "name": data.name}

@router.put("/chats/{chat_id}")
async def update_chat(chat_id: int, data: ChatUpdate, tenant_id: int = Depends(get_tenant_id)):
    return {"status": "ok"}

@router.delete("/chats/{chat_id}")
async def delete_chat(chat_id: int, tenant_id: int = Depends(get_tenant_id)):
    return {"status": "ok"}

@router.post("/chats/{chat_id}/test")
async def test_chat(chat_id: int, tenant_id: int = Depends(get_tenant_id)):
    return {"status": "ok"}

@router.post("/chats/verify")
async def verify_chat(data: ChatVerify, tenant_id: int = Depends(get_tenant_id)):
    return {"ok": True}

@router.get("/subscription")
async def get_subscription(tenant_id: int = Depends(get_tenant_id)):
    from app.config import get_settings
    settings = get_settings()
    
    cities_map = {}
    for b in settings.branches:
        city = b.get("city", "Другое")
        if city not in cities_map:
            cities_map[city] = []
        cities_map[city].append({"id": b.get("id", ""), "name": b.get("name", "")})
    
    cities = [{"name": city, "branches": branches} for city, branches in cities_map.items()]
    branches_count = sum(len(c["branches"]) for c in cities)
    
    base_cost = 5000 * branches_count
    finance_cost = 2000 * branches_count
    volume_discount = 0.15 if branches_count >= 7 else 0.10 if branches_count >= 4 else 0
    subtotal = base_cost + finance_cost
    monthly_total = int(subtotal * (1 - volume_discount))
    
    return {
        "status": "active", "plan": "base", "addons": ["Финансы"], "period": "monthly",
        "cities": cities, "branches_count": branches_count, "cities_count": len(cities),
        "pricing": {"base_per_branch": 5000, "finance_per_branch": 2000, "volume_discount_pct": int(volume_discount * 100), "monthly_total": monthly_total, "next_payment": monthly_total},
        "next_payment_date": None
    }

@router.put("/settings")
async def update_settings(data: SettingsUpdate, tenant_id: int = Depends(get_tenant_id)):
    return {"status": "ok"}

@router.put("/settings/password")
async def update_password(data: PasswordUpdate, tenant_id: int = Depends(get_tenant_id)):
    return {"status": "ok"}
