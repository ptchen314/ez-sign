#!/usr/bin/env python3
import base64
import binascii
import datetime
import json
import os
import secrets
import shutil
import sqlite3
import threading
from pathlib import Path
import fitz  # PyMuPDF
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

# ---------------------------------------------------------------- 設定
BASE_DIR = Path(__file__).resolve().parent
BUNDLED_PDF = BASE_DIR / "會員福利.pdf"   
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
SIGNED_DIR = DATA_DIR / "signed"
DB_PATH = DATA_DIR / "ezsign.db"
PDF_PATH = DATA_DIR / "active.pdf"         
TEMPLATE_PATH = DATA_DIR / "template.json" 
DOC_TITLE = "羅文ㄉㄉ歡樂俱樂部—相關條款及說明"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
RENDER_DPI = 135  # 前台顯示 PDF 頁面圖片的解析度
MAX_PDF_BYTES = 25 * 1024 * 1024  # 上傳 PDF 大小上限(25 MB)
EDITABLE_TYPES = {"signature", "checkbox", "date"}  # 線上編輯器可圈選的欄位型別

# 預設欄位版面(對應內建「會員福利.pdf」第 2 頁,index 1)。
# 所有座標一律以 PDF point 儲存(原點在左上);線上圈選存檔後改以 template.json 為準。
DEFAULT_TEMPLATE = {
    "title": DOC_TITLE,
    "fields": [
        # 簽名圖:蓋在簽名底線正上方的矩形內
        {"type": "signature", "page": 1, "rect": [393.24, 642.74, 534.59, 694.74]},
        # 「我已知悉…」勾選框
        {"type": "checkbox", "page": 1, "rect": [57.69, 592.47, 70.44, 605.22]},
        # 內建的「年 / 月 / 日」三格日期(數字填在各字左側);此型別僅供內建版面沿用
        {"type": "date-parts", "page": 1, "baseline_y": 728.6,
         "labels_x0": [459.81, 489.12, 518.44]},
    ],
}

DATA_DIR.mkdir(exist_ok=True)
SIGNED_DIR.mkdir(exist_ok=True)

# 執行期狀態(startup 時載入 / 更換文件時更新)
TEMPLATE: dict = {}
PAGE_COUNT = 0
PAGE_SIZES: list[list[float]] = []  # 每頁 [寬, 高](point)

# ---------------------------------------------------------------- 資料庫
_db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS requests (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                token      TEXT UNIQUE NOT NULL,
                name       TEXT NOT NULL,
                email      TEXT DEFAULT '',
                note       TEXT DEFAULT '',
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                signed_at  TEXT,
                signed_ip  TEXT,
                signed_pdf TEXT
            )"""
        )


def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 文件與版面
_page_cache: dict[int, bytes] = {}
_render_lock = threading.Lock()


def ensure_active_pdf() -> None:
    """確保使用中文件存在;初次啟動時以內建 PDF 建立。"""
    if not PDF_PATH.exists():
        if not BUNDLED_PDF.exists():
            raise RuntimeError(f"找不到內建 PDF:{BUNDLED_PDF}")
        shutil.copyfile(BUNDLED_PDF, PDF_PATH)


def load_template() -> dict:
    """讀取版面設定;不存在或損毀時回傳一份預設版面。"""
    if TEMPLATE_PATH.exists():
        try:
            data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("fields"), list):
                data.setdefault("title", DOC_TITLE)
                return data
        except (ValueError, OSError):
            pass
    return json.loads(json.dumps(DEFAULT_TEMPLATE))  # 深拷貝預設值


def save_template(data: dict) -> None:
    TEMPLATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def reload_document() -> None:
    """重新讀取使用中文件的頁數與尺寸,並清除頁面圖片快取。"""
    global PAGE_COUNT, PAGE_SIZES
    with _render_lock:
        _page_cache.clear()
    with fitz.open(PDF_PATH) as doc:
        PAGE_COUNT = len(doc)
        PAGE_SIZES = [[round(p.rect.width, 2), round(p.rect.height, 2)] for p in doc]


def render_page_png(page_no: int) -> bytes:
    """把使用中 PDF 的某一頁轉成 PNG(有快取,所有簽署人共用)。"""
    with _render_lock:
        if page_no not in _page_cache:
            with fitz.open(PDF_PATH) as doc:
                pix = doc[page_no].get_pixmap(dpi=RENDER_DPI)
                _page_cache[page_no] = pix.tobytes("png")
        return _page_cache[page_no]


def _draw_check(page, r: fitz.Rect) -> None:
    """在矩形內畫勾選記號(✓),大小隨方框縮放。"""
    w, h = r.width, r.height
    page.draw_line((r.x0 + w * 0.18, r.y0 + h * 0.52),
                   (r.x0 + w * 0.42, r.y1 - h * 0.20),
                   color=(0, 0, 0), width=1.5)
    page.draw_line((r.x0 + w * 0.42, r.y1 - h * 0.20),
                   (r.x1 - w * 0.10, r.y0 + h * 0.12),
                   color=(0, 0, 0), width=1.5)


def _draw_date_box(page, r: fitz.Rect, when: datetime.datetime, fmt) -> None:
    """把日期寫進矩形內(靠左、垂直置中,字級隨框高並自動縮放以塞得下)。"""
    text = (fmt or "{y} 年 {m} 月 {d} 日").format(y=when.year, m=when.month, d=when.day)
    size = max(8.0, min(16.0, r.height * 0.7))
    tw = fitz.get_text_length(text, fontname="china-t", fontsize=size)
    if tw > r.width > 0:
        size = max(6.0, size * r.width / tw)
    baseline = r.y0 + (r.height + size * 0.72) / 2
    page.insert_text((r.x0 + 2, baseline), text,
                     fontname="china-t", fontsize=size, color=(0, 0, 0))


def _draw_date_parts(page, field: dict, when: datetime.datetime) -> None:
    """內建三格日期:把數字靠右填在各「年/月/日」字左側。"""
    baseline = field.get("baseline_y", 0)
    for text, label_x0 in zip((str(when.year), str(when.month), str(when.day)),
                              field.get("labels_x0", [])):
        width = fitz.get_text_length(text, fontname="helv", fontsize=12)
        page.insert_text((label_x0 - 3 - width, baseline), text,
                         fontname="helv", fontsize=12, color=(0, 0, 0))


def stamp_pdf(sig_png: bytes, name: str, when: datetime.datetime,
              token: str, ip: str, out_path: Path) -> None:
    """依版面設定把簽名圖、勾選記號、日期與稽核資訊蓋到 PDF 上並輸出。"""
    doc = fitz.open(PDF_PATH)
    n = len(doc)
    for f in TEMPLATE.get("fields", []):
        pno = f.get("page")
        if not isinstance(pno, int) or not 0 <= pno < n:
            continue
        page = doc[pno]
        ftype = f.get("type")
        if ftype == "signature":
            page.insert_image(fitz.Rect(f["rect"]), stream=sig_png,
                              keep_proportion=True)
        elif ftype == "checkbox":
            _draw_check(page, fitz.Rect(f["rect"]))
        elif ftype == "date":
            _draw_date_box(page, fitz.Rect(f["rect"]), when, f.get("format"))
        elif ftype == "date-parts":
            _draw_date_parts(page, f, when)

    # 頁尾稽核資訊(蓋在最後一頁底部)
    last = doc[-1]
    audit = (f"簽署人:{name}   簽署時間:{when.strftime('%Y-%m-%d %H:%M:%S')}"
             f"   簽署編號:{token[:12]}   IP:{ip}")
    last.insert_text((57, last.rect.height - 22), audit,
                     fontname="china-t", fontsize=7.5, color=(0.45, 0.45, 0.45))

    doc.save(out_path, deflate=True)
    doc.close()


def decode_signature(data_url: str) -> bytes:
    """驗證並解出前端傳來的簽名 PNG(data URL)。"""
    prefix = "data:image/png;base64,"
    if not data_url.startswith(prefix):
        raise HTTPException(400, "簽名格式錯誤")
    raw = data_url[len(prefix):]
    if len(raw) > 2_000_000:
        raise HTTPException(400, "簽名圖片過大")
    try:
        png = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "簽名資料無法解析")
    if not png.startswith(b"\x89PNG"):
        raise HTTPException(400, "簽名必須為 PNG 圖片")
    return png


def decode_pdf(data_url: str) -> bytes:
    """驗證並解出後台上傳的 PDF(data URL)。"""
    prefix = "data:application/pdf;base64,"
    if not data_url.startswith(prefix):
        raise HTTPException(400, "請上傳 PDF 檔")
    raw_b64 = data_url[len(prefix):]
    if len(raw_b64) > MAX_PDF_BYTES // 3 * 4 + 16:
        raise HTTPException(400, "PDF 檔案過大(上限 25 MB)")
    try:
        raw = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "PDF 資料無法解析")
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(400, "PDF 檔案過大(上限 25 MB)")
    if not raw.startswith(b"%PDF"):
        raise HTTPException(400, "檔案不是有效的 PDF")
    return raw


def validate_fields(fields: list) -> list:
    """驗證並正規化前端送來的欄位清單(座標為 PDF point)。"""
    out = []
    for f in fields:
        if not isinstance(f, dict) or f.get("type") not in EDITABLE_TYPES:
            continue
        page = f.get("page")
        rect = f.get("rect")
        if not isinstance(page, int) or not 0 <= page < PAGE_COUNT:
            raise HTTPException(400, "欄位頁碼超出範圍")
        if (not isinstance(rect, list) or len(rect) != 4
                or not all(isinstance(v, (int, float)) for v in rect)):
            raise HTTPException(400, "欄位座標格式錯誤")
        x0, y0, x1, y1 = rect
        r = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        if r[2] - r[0] < 3 or r[3] - r[1] < 3:
            raise HTTPException(400, "欄位範圍太小,請重新圈選")
        item = {"type": f["type"], "page": page, "rect": [round(v, 2) for v in r]}
        fmt = f.get("format")
        if item["type"] == "date" and isinstance(fmt, str) and fmt.strip():
            item["format"] = fmt.strip()[:40]
        out.append(item)
    return out


# ---------------------------------------------------------------- FastAPI
app = FastAPI(title="ez-sign", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    global TEMPLATE
    ensure_active_pdf()
    init_db()
    TEMPLATE = load_template()
    reload_document()
    if ADMIN_PASSWORD == "admin123":
        print("⚠️  後台密碼目前為預設值 admin123,可用環境變數 ADMIN_PASSWORD 修改")


def require_admin(x_admin_password: str = Header(default="")) -> None:
    if not secrets.compare_digest(x_admin_password, ADMIN_PASSWORD):
        raise HTTPException(401, "後台密碼錯誤")


def get_request_by_token(token: str) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM requests WHERE token=?", (token,)).fetchone()
    if row is None:
        raise HTTPException(404, "找不到這份簽署連結,請與發送人確認")
    return row


# ------------------------------ 頁面路由
@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/admin")


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page() -> str:
    return (STATIC_DIR / "admin.html").read_text(encoding="utf-8")


@app.get("/admin/template", response_class=HTMLResponse, include_in_schema=False)
def template_page() -> str:
    return (STATIC_DIR / "template.html").read_text(encoding="utf-8")


@app.get("/sign/{token}", response_class=HTMLResponse, include_in_schema=False)
def sign_page(token: str) -> str:
    return (STATIC_DIR / "sign.html").read_text(encoding="utf-8")


# ------------------------------ 後台 API
class CreateRequest(BaseModel):
    name: str
    email: str = ""
    note: str = ""


class LoginBody(BaseModel):
    password: str


@app.post("/api/admin/login")
def admin_login(body: LoginBody) -> dict:
    if not secrets.compare_digest(body.password, ADMIN_PASSWORD):
        raise HTTPException(401, "密碼錯誤")
    return {"ok": True}


def row_to_dict(row: sqlite3.Row, request: Request) -> dict:
    base = str(request.base_url).rstrip("/")
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "note": row["note"],
        "status": row["status"],
        "created_at": row["created_at"],
        "signed_at": row["signed_at"],
        "signed_ip": row["signed_ip"],
        "sign_url": f"{base}/sign/{row['token']}",
        "token": row["token"],
    }


@app.post("/api/admin/requests", dependencies=[])
def create_request(body: CreateRequest, request: Request,
                   x_admin_password: str = Header(default="")) -> dict:
    require_admin(x_admin_password)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "請填寫簽署人姓名")
    token = secrets.token_urlsafe(16)
    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO requests (token, name, email, note, created_at) "
            "VALUES (?,?,?,?,?)",
            (token, name, body.email.strip(), body.note.strip(), now_str()),
        )
        row = conn.execute("SELECT * FROM requests WHERE id=?",
                           (cur.lastrowid,)).fetchone()
    return row_to_dict(row, request)


@app.get("/api/admin/requests")
def list_requests(request: Request,
                  x_admin_password: str = Header(default="")) -> list:
    require_admin(x_admin_password)
    with db() as conn:
        rows = conn.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
    return [row_to_dict(r, request) for r in rows]


@app.delete("/api/admin/requests/{req_id}")
def delete_request(req_id: int,
                   x_admin_password: str = Header(default="")) -> dict:
    require_admin(x_admin_password)
    with _db_lock, db() as conn:
        row = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "資料不存在")
        conn.execute("DELETE FROM requests WHERE id=?", (req_id,))
    if row["signed_pdf"]:
        Path(row["signed_pdf"]).unlink(missing_ok=True)
        (SIGNED_DIR / f"sig_{row['token']}.png").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/admin/requests/{req_id}/signed.pdf")
def admin_download_signed(req_id: int,
                          x_admin_password: str = Header(default="")) -> FileResponse:
    require_admin(x_admin_password)
    with db() as conn:
        row = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if row is None or not row["signed_pdf"] or not Path(row["signed_pdf"]).exists():
        raise HTTPException(404, "尚未完成簽署")
    return FileResponse(row["signed_pdf"], media_type="application/pdf",
                        filename=f"會員福利_已簽署_{row['name']}.pdf")


# ------------------------------ 文件與欄位版面 API
class UploadPdfBody(BaseModel):
    pdf: str  # data:application/pdf;base64,...


class TemplateBody(BaseModel):
    title: str = ""
    fields: list = []


def editable_fields() -> list:
    """僅回傳線上編輯器可處理的欄位(簽名/勾選/日期)。"""
    return [f for f in TEMPLATE.get("fields", []) if f.get("type") in EDITABLE_TYPES]


def has_builtin_date() -> bool:
    return any(f.get("type") == "date-parts" for f in TEMPLATE.get("fields", []))


def document_meta_dict() -> dict:
    return {
        "pages": PAGE_COUNT,
        "page_sizes": PAGE_SIZES,       # 每頁 [寬, 高](point),供前端換算座標
        "title": TEMPLATE.get("title", DOC_TITLE),
        "fields": editable_fields(),
        "builtin_date": has_builtin_date(),
    }


@app.get("/api/admin/document/meta")
def document_meta(x_admin_password: str = Header(default="")) -> dict:
    require_admin(x_admin_password)
    return document_meta_dict()


@app.get("/api/admin/document/page/{page_no}.png")
def document_page_image(page_no: int,
                        x_admin_password: str = Header(default="")) -> Response:
    require_admin(x_admin_password)
    if not 0 <= page_no < PAGE_COUNT:
        raise HTTPException(404, "頁碼超出範圍")
    return Response(render_page_png(page_no), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/admin/document")
def upload_document(body: UploadPdfBody,
                    x_admin_password: str = Header(default="")) -> dict:
    global TEMPLATE
    require_admin(x_admin_password)
    raw = decode_pdf(body.pdf)
    try:
        with fitz.open(stream=raw, filetype="pdf") as d:
            if len(d) == 0:
                raise ValueError
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "PDF 檔案無法解析")
    PDF_PATH.write_bytes(raw)
    reload_document()
    # 換文件後內建三格日期已不符新版面,移除;可圈選欄位保留(超出新頁數者剔除),供調整
    kept = [f for f in editable_fields() if f.get("page", 0) < PAGE_COUNT]
    TEMPLATE = {"title": TEMPLATE.get("title", DOC_TITLE), "fields": kept}
    save_template(TEMPLATE)
    return document_meta_dict()


@app.put("/api/admin/template")
def update_template(body: TemplateBody,
                    x_admin_password: str = Header(default="")) -> dict:
    global TEMPLATE
    require_admin(x_admin_password)
    fields = validate_fields(body.fields)
    # 若使用者自訂了日期欄位,即以其取代內建三格日期;否則沿用內建版面的日期
    if not any(f["type"] == "date" for f in fields):
        fields += [f for f in TEMPLATE.get("fields", []) if f.get("type") == "date-parts"]
    title = (body.title or TEMPLATE.get("title") or DOC_TITLE).strip() or DOC_TITLE
    TEMPLATE = {"title": title, "fields": fields}
    save_template(TEMPLATE)
    return document_meta_dict()


# ------------------------------ 簽署 API
class SignBody(BaseModel):
    signature: str  # data:image/png;base64,...
    agree: bool = False


@app.get("/api/sign/{token}")
def sign_info(token: str) -> dict:
    row = get_request_by_token(token)
    return {
        "name": row["name"],
        "status": row["status"],
        "signed_at": row["signed_at"],
        "pages": PAGE_COUNT,
        "title": TEMPLATE.get("title", DOC_TITLE),
    }


@app.get("/api/sign/{token}/page/{page_no}.png")
def sign_page_image(token: str, page_no: int) -> Response:
    get_request_by_token(token)
    if not 0 <= page_no < PAGE_COUNT:
        raise HTTPException(404, "頁碼超出範圍")
    return Response(render_page_png(page_no), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/sign/{token}")
def submit_signature(token: str, body: SignBody, request: Request) -> dict:
    row = get_request_by_token(token)
    if row["status"] == "signed":
        raise HTTPException(409, "這份文件已完成簽署,無法重複簽署")
    if not body.agree:
        raise HTTPException(400, "請先勾選同意條款")
    sig_png = decode_signature(body.signature)

    when = datetime.datetime.now()
    ip = request.client.host if request.client else "unknown"
    out_path = SIGNED_DIR / f"signed_{row['id']}_{token[:8]}.pdf"

    stamp_pdf(sig_png, row["name"], when, token, ip, out_path)
    (SIGNED_DIR / f"sig_{token}.png").write_bytes(sig_png)  # 保留原始簽名圖備查

    with _db_lock, db() as conn:
        conn.execute(
            "UPDATE requests SET status='signed', signed_at=?, signed_ip=?, "
            "signed_pdf=? WHERE id=? AND status='pending'",
            (when.strftime("%Y-%m-%d %H:%M:%S"), ip, str(out_path), row["id"]),
        )
    return {"ok": True, "signed_at": when.strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/api/sign/{token}/signed.pdf")
def signer_download(token: str) -> FileResponse:
    row = get_request_by_token(token)
    if row["status"] != "signed" or not row["signed_pdf"] \
            or not Path(row["signed_pdf"]).exists():
        raise HTTPException(404, "尚未完成簽署")
    return FileResponse(row["signed_pdf"], media_type="application/pdf",
                        filename=f"會員福利_已簽署_{row['name']}.pdf")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8100)
