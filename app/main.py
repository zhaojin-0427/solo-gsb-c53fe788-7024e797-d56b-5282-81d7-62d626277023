"""FastAPI 入口：上传 EPUB、选择 spine 正文、预览拆分、下载新书。"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import quote, unquote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import epub_splitter as es
from .sessions import SessionStore

MAX_UPLOAD = 64 * 1024 * 1024  # 64 MiB

app = FastAPI(title="EPUB3 章节拆分台")
store = SessionStore()

CONTENT_TYPES = {
    ".xhtml": "application/xhtml+xml",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ncx": "application/x-dtbncx+xml",
    ".opf": "application/oebps-package+xml",
}


def content_type_for(name: str) -> str:
    return CONTENT_TYPES.get(posixpath.splitext(name)[1].lower(),
                             "application/octet-stream")


def get_session(sid: str):
    try:
        return store.get(sid)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新上传")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="未收到文件")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="文件过大（上限 64 MiB）")
    try:
        session = store.create(data, file.filename or "book.epub")
    except es.SplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # 非法 zip 等
        raise HTTPException(status_code=400,
                            detail="无法识别为 EPUB 文件：%s" % exc)
    return JSONResponse({"session_id": session.id})


@app.get("/api/spine")
def list_spine(session_id: str):
    session = get_session(session_id)
    try:
        book = es.Book(session.data)
    except es.SplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        items = [it.to_dict() for it in book.spine_items()]
        encrypted = [{"file": p, "algorithm": a}
                     for p, a in book.encryption_entries.items()]
        ncx = book.ncx_item_id()
        nav = book.nav_item_id()
    finally:
        book.close()
    return {"spine": items, "encrypted": encrypted,
            "has_ncx": bool(ncx), "has_nav": bool(nav)}


@app.post("/api/plan")
def make_plan(session_id: str = Form(...), href: str = Form(...)):
    session = get_session(session_id)
    href = unquote(href)
    book = None
    try:
        book = es.Book(session.data)
        if href not in book.names:
            raise HTTPException(status_code=400,
                                detail="所选文件不在 EPUB 中：%s" % href)
        plan = es.build_plan(book, href)
        session.source_href = href
        session.last_plan_meta = {
            "source_href": href,
            "valid": plan.valid,
            "parts": [{"index": p.index, "href": p.href, "title": p.title}
                      for p in plan.parts],
            "errors": [e.to_dict() for e in plan.errors],
        }
        return session.last_plan_meta
    except es.SplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if book:
            book.close()


@app.get("/api/preview")
def preview(session_id: str, href: str, index: int):
    """渲染拆分后第 index 篇的预览 HTML（外层页面）。"""
    session = get_session(session_id)
    try:
        index = int(index)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="index 非法")
    book = None
    try:
        book = es.Book(session.data)
        plan = es.build_plan(book, unquote(href))
        if not (0 <= index < len(plan.parts)):
            raise HTTPException(status_code=404, detail="篇章序号不存在")
        html = es.preview_html(book, plan, index, session_id)
        return HTMLResponse(content=html, status_code=200)
    except es.SplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if book:
            book.close()


@app.get("/api/file/{sid}/root/{path:path}")
def serve_book_file(sid: str, path: str):
    """提供预览所需的包内资源。

    * CSS/图片/字体等原始资源：原样返回；
    * 拆分后产生的新 XHTML：按切分结果渲染（供 iframe 内链点击跳转）；
    * 其余原始 XHTML：原样返回。
    """
    session = get_session(sid)
    target = posixpath.normpath(unquote(path))
    if target.startswith("../") or target.startswith("/"):
        raise HTTPException(status_code=400, detail="非法路径")
    book = None
    try:
        book = es.Book(session.data)
        if target in book.encryption_entries:
            raise HTTPException(status_code=404, detail="资源已加密")

        # 拆分后的新篇章文件
        if session.source_href:
            plan = es.build_plan(book, session.source_href)
            for p in plan.parts:
                if p.href == target:
                    html = es.preview_html(book, plan, p.index, sid)
                    return HTMLResponse(content=html)

        if target not in book.names:
            raise HTTPException(status_code=404, detail="资源不存在")
        data = book.zf.read(target)
        ctype = content_type_for(target)
        if target.endswith((".xhtml", ".html", ".htm")):
            ctype = "application/xhtml+xml"
        return Response(content=data, media_type=ctype)
    finally:
        if book:
            book.close()


@app.get("/api/download")
def download(session_id: str, href: str):
    """执行拆分并以 EPUB 附件形式返回新书；校验不通过则 422。"""
    session = get_session(session_id)
    source = unquote(href)
    book = None
    try:
        book = es.Book(session.data)
        plan = es.build_plan(book, source)
        if not plan.valid:
            return JSONResponse(
                status_code=422,
                content={"ok": False,
                         "errors": [e.to_dict() for e in plan.errors]})
        data = es.build_epub(book, plan)
    except es.SplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if book:
            book.close()

    stem = posixpath.splitext(posixpath.basename(session.filename or "book"))[0]
    part_titles = "-".join(
        re.sub(r"[^\w一-鿿]+", "", p.title)[:10] for p in plan.parts[:3])
    out_name = "%s-%s.epub" % (stem or "book", part_titles or "split")
    quoted = quote(out_name)
    return Response(
        content=data,
        media_type="application/epub+zip",
        headers={"Content-Disposition":
                 "attachment; filename*=UTF-8''%s" % quoted})


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
