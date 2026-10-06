"""FastAPI 应用：上传 EPUB3、预览切点、下载拆分后的新书。"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import splitter
from .splitter import (
    Book,
    SplitError,
    Splitter,
    build_epub,
    etree,
    preview as preview_plan,
    serialize,
)

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="EPUB3 章节拆分台", version="1.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# book_id -> 文件路径（单进程内存索引；data 目录持久保存上传文件）
BOOKS: dict[str, Path] = {}

SAFE_RE = re.compile(r"[^0-9A-Za-z._一-鿿-]+")


def _load_book(book_id: str) -> tuple[Book, Path]:
    path = BOOKS.get(book_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="上传记录已失效，请重新上传 EPUB。")
    try:
        return Book(path.read_bytes()), path
    except SplitError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": exc.message, "files": exc.files},
        )


def _error(exc: SplitError, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status, detail={"message": exc.message, "files": exc.files})


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.post("/api/upload")
def upload(file: bytes = File(...)) -> JSONResponse:
    if not file:
        raise HTTPException(status_code=400, detail="未收到文件。")
    book_id = uuid.uuid4().hex
    path = DATA_DIR / f"{book_id}.epub"
    path.write_bytes(file)

    try:
        book = Book(file)
    except SplitError as exc:
        path.unlink(missing_ok=True)
        raise _error(exc)

    BOOKS[book_id] = path
    return JSONResponse(
        {
            "book_id": book_id,
            "opf": book.container_path,
            "spine": book.list_spine_xhtml(),
        }
    )


@app.get("/api/preview")
def preview(book_id: str, path: str) -> JSONResponse:
    book, _ = _load_book(book_id)
    try:
        result = preview_plan(book, path)
    except SplitError as exc:
        raise _error(exc)
    return JSONResponse(result)


@app.get("/api/raw/{book_id}/{path:path}")
def raw(book_id: str, path: str) -> Response:
    """代理读取原书包内资源（供预览 XHTML 里的图片/CSS 相对路径使用）。"""
    book, _ = _load_book(book_id)
    norm = splitter._norm(path)
    if not book.exists(norm):
        raise HTTPException(status_code=404, detail=f"包内无此资源：{path}")
    ctype = "application/octet-stream"
    for item in book.manifest.values():
        if splitter._res(book.opf_dir, item.get("href", "")) == norm:
            ctype = item.get("media-type", ctype)
            break
    suffix = norm.lower().rsplit(".", 1)[-1]
    guess = {
        "css": "text/css",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "svg": "image/svg+xml",
        "woff": "font/woff",
        "woff2": "font/woff2",
        "ttf": "font/ttf",
        "otf": "font/otf",
    }
    if ctype == "application/octet-stream":
        ctype = guess.get(suffix, ctype)
    return Response(content=book.entries[norm], media_type=ctype)


@app.get("/api/segment")
def segment(book_id: str, path: str, index: int) -> Response:
    """返回拆分后某一篇的 XHTML（直接以 application/xhtml+xml 渲染预览）。"""
    book, _ = _load_book(book_id)
    try:
        sp = Splitter(book, path)
        sp._build_segments()
        if index < 0 or index >= len(sp.segments):
            raise SplitError(f"篇序号越界：{index}", [path])
        seg = sp.segments[index]

        # 预览专用链接改写（在正式重写之前，文档内仍是原始 href）：
        #  - 指向拆分文件的链接（纯片段/相对路径）-> /api/segment 逐篇翻看；
        #  - 指向包内其他文件的链接 -> /api/raw 代理；
        #  - 外链保持不变。
        source_dir = sp.source_dir
        preview_errors: list[str] = []
        for el, attr in sp._href_nodes(seg.doc.getroot()):
            old = el.get(attr)
            if not old or old.startswith(("http://", "https://", "mailto:")):
                continue
            kind, seg_i, frag = sp._resolve(
                sp.source_path, old, base_doc=sp.source_path
            )
            if kind == "external":
                continue
            if kind == "dangling":
                preview_errors.append(old)
                continue
            if kind == "split":
                if seg_i == index:
                    el.set(attr, ("#" + frag) if frag else "#")
                else:
                    el.set(
                        attr,
                        f"/api/segment?book_id={book_id}&path={path}&index={seg_i}"
                        + (f"#{frag}" if frag else ""),
                    )
            else:  # keep：其他包内文件，经资源代理访问
                from urllib.parse import urldefrag

                bare, fr = urldefrag(old)
                if bare:
                    target = splitter._res(source_dir, bare)
                    el.set(
                        attr,
                        f"/api/raw/{book_id}/{target}" + (f"#{fr}" if fr else ""),
                    )
        if preview_errors:
            raise SplitError(
                "预览发现悬空内链：" + ", ".join(preview_errors[:10]), [path]
            )

        # <base> 让图片/CSS 等相对路径从原书目录加载
        head = seg.doc.find(".//{http://www.w3.org/1999/xhtml}head")
        if head is not None:
            base_el = etree.Element(
                "{http://www.w3.org/1999/xhtml}base",
                attrib={"href": f"/api/raw/{book_id}/{source_dir}/"},
            )
            head.insert(0, base_el)

        data = serialize(seg.doc)
    except SplitError as exc:
        raise _error(exc)
    return Response(content=data, media_type="application/xhtml+xml; charset=utf-8")


@app.post("/api/split")
def split(book_id: str = Form(...), path: str = Form(...)) -> Response:
    book, _ = _load_book(book_id)
    try:
        sp = Splitter(book, path)
        result = sp.run()
        data = build_epub(book, result, path)
    except SplitError as exc:
        raise _error(exc)
    except etree.XMLSyntaxError as exc:
        raise HTTPException(status_code=422, detail={"message": f"XML 处理失败：{exc}", "files": [path]})

    base = SAFE_RE.sub("_", path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "book")
    fname = f"{base}-split.epub"
    return Response(
        content=data,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
