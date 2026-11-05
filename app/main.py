import json
import logging
import secrets
import time
from datetime import date, timedelta
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.auth import admin, current_user, digest, hasher, new_session, verify
from app.catalog import (
    attach_image,
    count_query,
    product_images,
    product_output,
    product_query,
    save_product,
)
from app.config import settings
from app.images import store_image
from app.labels import render_pdf
from app.models import (
    Asset,
    Audit,
    Barcode,
    Campaign,
    Job,
    LoginAttempt,
    LoginSession,
    Product,
    ProductImage,
    Promotion,
    Template,
    User,
    audit,
    db,
    now,
)
from app.schemas import (
    CampaignInput,
    Credentials,
    PrintInput,
    ProductInput,
    PromoItem,
    TemplateInput,
    UserInput,
    UserUpdate,
)

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="Mieszko Promo Manager", version="1.0.0", docs_url=None, redoc_url=None)
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("promo.api")


@app.middleware("http")
async def security(request: Request, call_next):
    started = time.monotonic()
    request_id = secrets.token_hex(8)
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != settings.allowed_origin:
        return Response("Origin denied", status_code=403)
    response = await call_next(request)
    for key, value in {
        "X-Request-ID": request_id,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "same-origin",
        "Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
    }.items():
        response.headers[key] = value
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    log.info(
        json.dumps(
            {
                "event": "http",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )
    )
    return response


@app.exception_handler(IntegrityError)
async def conflict(request, exc):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=409,
        content={"detail": "A barcode, code or username already exists. Reload and review duplicates."},
    )


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(s=Depends(db)):
    s.execute(text("SELECT 1"))
    if s.scalar(text("SELECT version_num FROM alembic_version")) != "001_foundation":
        raise HTTPException(503, "Database migration is required")
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(data: Credentials, request: Request, response: Response, s=Depends(db)):
    identity = digest(request.client.host if request.client else "unknown")
    cutoff = now() - timedelta(minutes=15)
    s.execute(delete(LoginAttempt).where(LoginAttempt.created_at < cutoff))
    attempts = s.scalar(
        select(func.count()).select_from(LoginAttempt).where(LoginAttempt.identity == identity)
    )
    if attempts >= 10:
        raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes.")
    s.add(LoginAttempt(identity=identity))
    s.commit()
    user = s.scalar(select(User).where(User.username == data.username.lower()))
    valid = verify(user.password_hash if user else None, data.password)
    if not user or not valid or not user.active:
        raise HTTPException(401, "Incorrect username or password")
    s.execute(delete(LoginSession).where(LoginSession.expires_at < now()))
    token, session = new_session(s, user, settings.session_hours)
    audit(s, user.id, "auth.login", user.id)
    s.commit()
    response.set_cookie(
        "promo_session",
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_hours * 3600,
        path="/",
    )
    return {"id": user.id, "username": user.username, "role": user.role, "csrf": session.csrf}


@app.get("/api/auth/me")
def me(request: Request, user=Depends(current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "csrf": request.state.login_session.csrf,
    }


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, user=Depends(current_user), s=Depends(db)):
    s.execute(delete(LoginSession).where(LoginSession.token_hash == request.state.login_session.token_hash))
    audit(s, user.id, "auth.logout", user.id)
    s.commit()
    response.delete_cookie("promo_session", path="/")
    return {"ok": True}


@app.get("/api/stats")
def stats(user=Depends(current_user), s=Depends(db)):
    today = date.today()
    products = s.scalar(select(func.count()).select_from(Product).where(Product.active.is_(True)))
    missing = count_query(s, product_query(missing=True))
    return {
        "products": products,
        "with_images": products - missing,
        "missing_images": missing,
        "active_promotions": s.scalar(
            select(func.count())
            .select_from(Campaign)
            .where(Campaign.status == "active", Campaign.start_date <= today, Campaign.end_date >= today)
        ),
        "expired_promotions": s.scalar(
            select(func.count())
            .select_from(Campaign)
            .where(Campaign.status != "archived", Campaign.end_date < today)
        ),
        "failed_jobs": s.scalar(
            select(func.count()).select_from(Job).where(Job.status.in_(["failed", "partial"]))
        ),
        "recent_jobs": [job_output(j) for j in s.scalars(select(Job).order_by(Job.id.desc()).limit(8))],
    }


@app.get("/api/products")
def products(
    q: str = Query("", max_length=200),
    page: int = Query(1, ge=1, le=100000),
    limit: int = Query(30, ge=1, le=100),
    sort: str = "name",
    direction: str = "asc",
    active: str = Query("active", pattern="^(active|archived|all)$"),
    missing: bool = False,
    brand: str = Query("", max_length=180),
    user=Depends(current_user),
    s=Depends(db),
):
    query = product_query(q, active, missing, brand)
    total = count_query(s, query)
    column = {
        "name": Product.name,
        "code": Product.code,
        "created_at": Product.created_at,
        "id": Product.id,
    }.get(sort)
    if column is None or direction not in ("asc", "desc"):
        raise HTTPException(422, "Invalid sort")
    rows = s.scalars(
        query.order_by(column.desc() if direction == "desc" else column.asc(), Product.id)
        .offset((page - 1) * limit)
        .limit(limit)
    )
    return {"total": total, "page": page, "limit": limit, "items": [product_output(p) for p in rows]}


def require(s, model, key):
    obj = s.get(model, key)
    if not obj:
        raise HTTPException(404, "Record not found")
    return obj


@app.get("/api/products/{key}")
def get_product(key: int, user=Depends(current_user), s=Depends(db)):
    p = require(s, Product, key)
    return product_output(p, list(s.scalars(select(Barcode.value).where(Barcode.product_id == key)))) | {
        "images": product_images(s, p)
    }


@app.post("/api/products", status_code=201)
def create_product(data: ProductInput, user=Depends(current_user), s=Depends(db)):
    if not data.active and user.role != "admin":
        raise HTTPException(403, "Administrator access required")
    p = save_product(s, data)
    audit(s, user.id, "product.create", p.id)
    s.commit()
    return product_output(p)


@app.put("/api/products/{key}")
def edit_product(key: int, data: ProductInput, user=Depends(current_user), s=Depends(db)):
    existing = require(s, Product, key)
    if user.role != "admin" and data.active != existing.active:
        raise HTTPException(403, "Administrator access required to archive or restore")
    p = save_product(s, data, existing)
    audit(s, user.id, "product.update", key)
    s.commit()
    return product_output(p)


@app.post("/api/products/{key}/archive")
def archive_product(key: int, user=Depends(admin), s=Depends(db)):
    p = require(s, Product, key)
    p.active = False
    audit(s, user.id, "product.archive", key)
    s.commit()
    return {"ok": True}


@app.post("/api/products/{key}/restore")
def restore_product(key: int, user=Depends(admin), s=Depends(db)):
    p = require(s, Product, key)
    p.active = True
    audit(s, user.id, "product.restore", key)
    s.commit()
    return {"ok": True}


@app.post("/api/images")
async def upload_images(
    files: list[UploadFile] = File(...),
    product_id: int | None = None,
    user=Depends(current_user),
    s=Depends(db),
):
    if len(files) > 100:
        raise HTTPException(422, "Upload at most 100 images per batch")
    output = []
    for file in files:
        content = await file.read(8 * 1024 * 1024 + 1)
        try:
            with s.begin_nested():
                asset = store_image(s, content, file.filename or "image")
                product = (
                    require(s, Product, product_id)
                    if product_id
                    else s.scalar(
                        select(Product).join(Barcode).where(Barcode.value == Path(file.filename or "").stem)
                    )
                )
                if product:
                    attach_image(s, product, asset)
                output.append(
                    {"filename": file.filename, "id": asset.id, "product_id": product.id if product else None}
                )
        except HTTPException as exc:
            output.append({"filename": file.filename, "error": exc.detail})
        await file.close()
    audit(s, user.id, "images.upload", len(output))
    s.commit()
    return {"items": output}


@app.get("/api/images")
def image_library(page: int = Query(1, ge=1), user=Depends(current_user), s=Depends(db)):
    return {
        "total": s.scalar(select(func.count()).select_from(Asset)),
        "items": [
            {
                "id": a.id,
                "name": a.original_name,
                "width": a.width,
                "height": a.height,
                "url": f"/api/assets/{a.id}/thumbnail",
            }
            for a in s.scalars(select(Asset).order_by(Asset.id.desc()).offset((page - 1) * 48).limit(48))
        ],
    }


@app.get("/api/assets/{key}/{variant}")
def asset_file(key: int, variant: str, user=Depends(current_user), s=Depends(db)):
    a = require(s, Asset, key)
    if variant not in ("thumbnail", "original"):
        raise HTTPException(404)
    path = (
        settings.data_dir
        / ("thumbs" if variant == "thumbnail" else "images")
        / (a.digest + ".webp" if variant == "thumbnail" else a.filename)
    )
    if not path.is_file():
        raise HTTPException(404, "Image unavailable")
    return FileResponse(path, media_type="image/webp" if variant == "thumbnail" else None)


@app.get("/api/legacy-images/{filename}")
def legacy_image(filename: str, user=Depends(current_user)):
    if Path(filename).name != filename:
        raise HTTPException(404)
    path = settings.data_dir / "images" / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.put("/api/products/{key}/images/{asset_id}")
def select_image(key: int, asset_id: int, user=Depends(current_user), s=Depends(db)):
    attach_image(s, require(s, Product, key), require(s, Asset, asset_id))
    audit(s, user.id, "product.image.select", key)
    s.commit()
    return {"ok": True}


@app.delete("/api/products/{key}/images/{asset_id}")
def unlink_image(key: int, asset_id: int, user=Depends(current_user), s=Depends(db)):
    p = require(s, Product, key)
    s.execute(delete(ProductImage).where(ProductImage.product_id == key, ProductImage.asset_id == asset_id))
    if p.primary_image_id == asset_id:
        p.primary_image_id = None
    audit(s, user.id, "product.image.remove", key)
    s.commit()
    return {"ok": True}


@app.post("/api/imports", status_code=202)
async def import_workbook(file: UploadFile = File(...), user=Depends(admin), s=Depends(db)):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(422, "Expected XLSX workbook")
    name = secrets.token_hex(16) + ".xlsx"
    path = settings.data_dir / "uploads" / name
    size = 0
    try:
        with path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_mb * 1024 * 1024:
                    raise HTTPException(413, "Workbook exceeds upload limit")
                out.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    job = Job(
        kind="import",
        status="queued_preview",
        payload={"source": name, "phase": "preview", "name": Path(file.filename or "").name},
        owner_id=user.id,
    )
    s.add(job)
    s.flush()
    audit(s, user.id, "import.preview.request", job.id)
    s.commit()
    return job_output(job)


def job_output(j):
    return {
        k: getattr(j, k)
        for k in (
            "id",
            "kind",
            "status",
            "progress",
            "total",
            "result",
            "error",
            "created_at",
            "updated_at",
            "cancel_requested",
        )
    }


@app.get("/api/jobs")
def jobs(page: int = Query(1, ge=1), kind: str = "", user=Depends(current_user), s=Depends(db)):
    query = select(Job)
    if kind:
        query = query.where(Job.kind == kind)
    if user.role != "admin":
        query = query.where(Job.owner_id == user.id)
    return {
        "total": count_query(s, query),
        "items": [
            job_output(j) for j in s.scalars(query.order_by(Job.id.desc()).offset((page - 1) * 30).limit(30))
        ],
    }


def own_job(s, key, user):
    j = require(s, Job, key)
    if j.owner_id != user.id and user.role != "admin":
        raise HTTPException(403)
    return j


@app.get("/api/jobs/{key}")
def get_job(key: int, user=Depends(current_user), s=Depends(db)):
    return job_output(own_job(s, key, user))


@app.post("/api/jobs/{key}/{action}")
def job_action(key: int, action: str, user=Depends(current_user), s=Depends(db)):
    # Lock to prevent duplicate commit/retry transitions from concurrent requests.
    j = s.scalar(select(Job).where(Job.id == key).with_for_update())
    if not j:
        raise HTTPException(404)
    if j.owner_id != user.id and user.role != "admin":
        raise HTTPException(403)
    if j.kind == "import" and user.role != "admin":
        raise HTTPException(403)
    if action == "commit" and j.kind == "import" and j.status == "preview":
        j.payload = j.payload | {"phase": "commit"}
        j.status = "queued"
        j.total = j.result.get("valid", j.total)
    elif action == "cancel" and j.status in ("queued", "queued_preview", "running", "preview"):
        j.cancel_requested = True
        if j.status != "running":
            j.status = "cancelled"
    elif action == "retry" and j.status in ("failed", "partial", "cancelled"):
        j.status = "queued" if j.payload.get("phase") == "commit" or j.kind == "print" else "queued_preview"
        j.cancel_requested, j.error, j.progress = False, None, 0
    else:
        raise HTTPException(409, "This job cannot perform that action in its current state")
    audit(s, user.id, "job." + action, key)
    s.commit()
    return job_output(j)


@app.get("/api/jobs/{key}/download")
def job_download(key: int, user=Depends(current_user), s=Depends(db)):
    j = own_job(s, key, user)
    filename = j.result.get("file" if j.kind == "print" else "report")
    if not filename:
        raise HTTPException(404, "File is not ready")
    path = settings.data_dir / ("prints" if j.kind == "print" else "uploads") / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=f"mieszko-{j.kind}-{key}" + path.suffix)


def campaign_output(c, s, details=False):
    today = date.today()
    effective = (
        c.status
        if c.status != "active"
        else ("expired" if c.end_date < today else "scheduled" if c.start_date > today else "active")
    )
    result = {k: getattr(c, k) for k in ("id", "name", "start_date", "end_date", "status")}
    result["effective_status"] = effective
    if details:
        result["items"] = [
            {
                "id": pr.id,
                "product_id": p.id,
                "product": product_output(p),
                "price": str(pr.price),
                "previous_price": str(pr.previous_price) if pr.previous_price is not None else None,
                "image_id": pr.image_id,
            }
            for pr, p in s.execute(
                select(Promotion, Product)
                .join(Product)
                .where(Promotion.campaign_id == c.id)
                .order_by(Promotion.id)
            )
        ]
    return result


def save_campaign(data, s, user, campaign=None):
    for item in data.items:
        p = require(s, Product, item.product_id)
        if not p.active:
            raise HTTPException(409, "Archived products cannot be promoted")
        if item.image_id and not s.get(ProductImage, (p.id, item.image_id)):
            raise HTTPException(422, "Select an image associated with this product")
    if not campaign:
        campaign = Campaign()
        s.add(campaign)
    for field in ("name", "start_date", "end_date", "status"):
        setattr(campaign, field, getattr(data, field))
    s.flush()
    s.execute(delete(Promotion).where(Promotion.campaign_id == campaign.id))
    for item in data.items:
        s.add(
            Promotion(
                campaign_id=campaign.id,
                start_date=data.start_date,
                end_date=data.end_date,
                **item.model_dump(),
            )
        )
    audit(s, user.id, "campaign.save", campaign.id)
    s.commit()
    return campaign_output(campaign, s, True)


@app.get("/api/campaigns")
def campaigns(page: int = Query(1, ge=1), user=Depends(current_user), s=Depends(db)):
    query = select(Campaign)
    return {
        "total": count_query(s, query),
        "items": [
            campaign_output(c, s)
            for c in s.scalars(query.order_by(Campaign.id.desc()).offset((page - 1) * 30).limit(30))
        ],
    }


@app.get("/api/campaigns/{key}")
def campaign(key: int, user=Depends(current_user), s=Depends(db)):
    return campaign_output(require(s, Campaign, key), s, True)


@app.post("/api/campaigns", status_code=201)
def create_campaign(data: CampaignInput, user=Depends(current_user), s=Depends(db)):
    return save_campaign(data, s, user)


@app.put("/api/campaigns/{key}")
def edit_campaign(key: int, data: CampaignInput, user=Depends(current_user), s=Depends(db)):
    return save_campaign(data, s, user, require(s, Campaign, key))


@app.post("/api/campaigns/{key}/duplicate", status_code=201)
def duplicate_campaign(key: int, user=Depends(current_user), s=Depends(db)):
    old = campaign_output(require(s, Campaign, key), s, True)
    data = CampaignInput(
        name=old["name"][:170] + " (copy)",
        start_date=old["start_date"],
        end_date=old["end_date"],
        status="draft",
        items=[
            PromoItem.model_validate(
                {k: item[k] for k in ("product_id", "price", "previous_price", "image_id")}
            )
            for item in old["items"]
        ],
    )
    return save_campaign(data, s, user)


@app.post("/api/campaigns/{key}/archive")
def archive_campaign(key: int, user=Depends(current_user), s=Depends(db)):
    c = require(s, Campaign, key)
    c.status = "archived"
    audit(s, user.id, "campaign.archive", key)
    s.commit()
    return {"ok": True}


def snapshot(s, ids, config):
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "Duplicate label selection")
    result = []
    for pr, p in s.execute(
        select(Promotion, Product).join(Product).where(Promotion.id.in_(ids)).order_by(Promotion.id)
    ):
        asset = (
            s.get(Asset, pr.image_id or p.primary_image_id) if (pr.image_id or p.primary_image_id) else None
        )
        logo = s.get(Asset, config.logo_id) if config.logo_id else None
        if config.logo_id and not logo:
            raise HTTPException(422, "Logo image unavailable")
        image_file = (
            str((settings.data_dir / "images" / (asset.filename if asset else p.image)).resolve())
            if asset or p.image
            else None
        )
        if asset:
            derivative = settings.data_dir / "print_images" / (asset.digest + ".jpg")
            if derivative.is_file():
                image_file = str(derivative.resolve())
        result.append(
            {
                "promotion_id": pr.id,
                "price": f"{pr.price:.2f}",
                "previous_price": f"{pr.previous_price:.2f}" if pr.previous_price else None,
                "start_date": pr.start_date.strftime("%d-%m-%Y"),
                "end_date": pr.end_date.strftime("%d-%m-%Y"),
                "product": {k: getattr(p, k) for k in ("name", "barcode", "brand", "weight", "unit")},
                "image_file": image_file,
                "logo_file": str((settings.data_dir / "images" / logo.filename).resolve()) if logo else None,
            }
        )
    if len(result) != len(ids):
        raise HTTPException(404, "One or more promotions no longer exist")
    return result


@app.post("/api/prints", status_code=202)
def create_print(data: PrintInput, user=Depends(current_user), s=Depends(db)):
    rows = snapshot(s, data.promotion_ids, data.template)
    job = Job(
        kind="print",
        status="queued",
        payload={"rows": rows, "template": data.template.model_dump()},
        total=len(rows),
        owner_id=user.id,
    )
    s.add(job)
    s.flush()
    audit(s, user.id, "print.request", job.id)
    s.commit()
    return job_output(job)


@app.post("/api/preview")
def preview_label(data: PrintInput, user=Depends(current_user), s=Depends(db)):
    if len(data.promotion_ids) != 1:
        raise HTTPException(422, "Preview exactly one label")
    try:
        output = render_pdf(snapshot(s, data.promotion_ids, data.template), data.template.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return Response(output, media_type="application/pdf")


@app.get("/api/templates")
def templates(user=Depends(current_user), s=Depends(db)):
    return [
        {"id": t.id, "name": t.name, "config": t.config}
        for t in s.scalars(select(Template).order_by(Template.id))
    ]


@app.post("/api/templates")
def create_template(data: TemplateInput, user=Depends(admin), s=Depends(db)):
    t = Template(name=data.name, config=data.config.model_dump())
    s.add(t)
    s.flush()
    audit(s, user.id, "template.create", t.id)
    s.commit()
    return {"id": t.id}


@app.put("/api/templates/{key}")
def edit_template(key: int, data: TemplateInput, user=Depends(admin), s=Depends(db)):
    t = require(s, Template, key)
    t.name, t.config = data.name, data.config.model_dump()
    audit(s, user.id, "template.update", key)
    s.commit()
    return {"id": key}


@app.get("/api/users")
def users(user=Depends(admin), s=Depends(db)):
    return [
        {"id": u.id, "username": u.username, "role": u.role, "active": u.active}
        for u in s.scalars(select(User).order_by(User.id))
    ]


@app.post("/api/users", status_code=201)
def create_user(data: UserInput, user=Depends(admin), s=Depends(db)):
    u = User(username=data.username.lower(), password_hash=hasher.hash(data.password), role=data.role)
    s.add(u)
    s.flush()
    audit(s, user.id, "user.create", u.id)
    s.commit()
    return {"id": u.id}


@app.put("/api/users/{key}")
def edit_user(key: int, data: UserUpdate, user=Depends(admin), s=Depends(db)):
    # Serialise admin changes so two admins cannot disable each other concurrently.
    list(s.scalars(select(User).where(User.role == "admin").with_for_update()))
    u = require(s, User, key)
    if key == user.id and (not data.active or data.role != "admin"):
        raise HTTPException(409, "You cannot disable or demote your own administrator account")
    u.active, u.role = data.active, data.role
    if data.password:
        u.password_hash = hasher.hash(data.password)
    s.execute(delete(LoginSession).where(LoginSession.user_id == key))
    audit(s, user.id, "user.update", key)
    s.commit()
    return {"ok": True}


@app.get("/api/history")
def history(before: int | None = None, user=Depends(admin), s=Depends(db)):
    query = select(Audit)
    if before:
        query = query.where(Audit.id < before)
    rows = list(s.scalars(query.order_by(Audit.id.desc()).limit(100)))
    return {
        "items": [
            {k: getattr(a, k) for k in ("id", "user_id", "action", "target", "created_at")} for a in rows
        ],
        "next": rows[-1].id if len(rows) == 100 else None,
    }


@app.get("/api/settings")
def configuration(user=Depends(admin)):
    return {
        "currency": "GBP",
        "max_upload_mb": settings.max_upload_mb,
        "session_hours": settings.session_hours,
        "cookie_secure": settings.cookie_secure,
        "allowed_origin": settings.allowed_origin,
        "retention": "Archived records and original files are retained. Print jobs contain immutable snapshots.",
    }


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


if (ROOT / "static" / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=ROOT / "static" / "assets"), name="assets")
