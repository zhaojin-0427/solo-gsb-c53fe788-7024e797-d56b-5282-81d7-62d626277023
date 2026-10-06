"""拆分核心逻辑测试。运行：python3 -m pytest tests/ -q（或 python tests/test_split.py）"""

import io
import posixpath
import zipfile
from urllib.parse import unquote

from lxml import etree

from app import epub_splitter as es
from tests import make_epub as mk

XHTML = es.NS_XHTML


def dec_list(hs):
    return [unquote(h) for h in hs]


def open_book(epub_bytes):
    return es.Book(epub_bytes)


def split(epub_bytes, source="OEBPS/chapter1.xhtml"):
    book = open_book(epub_bytes)
    plan = es.build_plan(book, source)
    assert plan.valid, [e.to_dict() for e in plan.errors]
    out = es.build_epub(book, plan)
    book.close()
    return out, plan


def read_out(out_bytes):
    zf = zipfile.ZipFile(io.BytesIO(out_bytes))
    return zf


# --------------------------------------------------------------------------

def test_basic_split_count_and_preamble():
    out, plan = split(mk.make_epub())
    zf = read_out(out)
    names = zf.namelist()
    # 3 篇 + 删除源文件
    assert len(plan.parts) == 3
    for p in plan.parts:
        assert p.href in names
    assert "OEBPS/chapter1.xhtml" not in names
    # 第一篇包含标题前导语
    first = zf.read(plan.parts[0].href).decode("utf-8")
    assert "标题前" in first and "导语" in first
    # 每篇恰好一个 h2
    for p in plan.parts:
        root = etree.fromstring(zf.read(p.href))
        h2s = root.iter("{%s}h2" % XHTML)
        assert len(list(h2s)) == 1
    print("ok test_basic_split_count_and_preamble")


def test_nested_section_across_cut():
    out, plan = split(mk.make_epub())
    zf = read_out(out)
    # bigwrap 跨全部三篇：第 2/3 篇顶层应注入 section（无 id），结构合法
    for p in plan.parts:
        root = etree.fromstring(zf.read(p.href))
        # XHTML 解析通过即说明 well-formed
        assert root.tag == "{%s}html" % XHTML
    third = zf.read(plan.parts[2].href).decode("utf-8")
    assert "第三节 结局" in third
    # 第三篇里注入的 section 开标签不带 id="bigwrap"
    assert 'id="bigwrap"' not in third
    # 第一篇保留 bigwrap
    first = zf.read(plan.parts[0].href).decode("utf-8")
    assert 'id="bigwrap"' in first and 'id="start"' in first
    print("ok test_nested_section_across_cut")


def test_links_retarget():
    out, plan = split(mk.make_epub())
    zf = read_out(out)
    hrefs = [p.href for p in plan.parts]
    base = "OEBPS/"

    def hrefs_in(name):
        root = etree.fromstring(zf.read(name))
        return [a.get("href") for a in root.iter("{%s}a" % XHTML)]

    p0 = dec_list(hrefs_in(hrefs[0]))
    first_name = posixpath.basename(hrefs[0])
    # 脚注引用保持指向 footnotes.xhtml
    assert any(h == "footnotes.xhtml#fn1" for h in p0), p0
    # 仅片段 #sec2 在第一篇 -> 重定向到第二篇文件#sec2
    assert any(h.endswith(".xhtml#sec2") and not h.startswith("#") for h in p0), p0
    # #bigwrap 是跨段容器 id，其 start 在第一篇 -> 保留篇内片段
    assert "#bigwrap" in p0, p0

    p1 = dec_list(hrefs_in(hrefs[1]))
    # 篇内相对回链 chapter1.xhtml#sec1 -> 第一篇文件#sec1
    assert any(h.endswith(first_name + "#sec1") for h in p1), p1

    # chapter2 指向 sec2 的链接重定向到第二篇文件
    c2 = dec_list(hrefs_in("OEBPS/chapter2.xhtml"))
    assert any(h.endswith(posixpath.basename(hrefs[1]) + "#sec2") for h in c2), c2
    # chapter2 的本文件仅片段保持
    assert "#c2" in c2

    # 脚注文件中的返回链接 chapter1.xhtml#fnref1 -> 第一篇文件#fnref1
    fn = dec_list(hrefs_in("OEBPS/footnotes.xhtml"))
    assert any(h.endswith(first_name + "#fnref1") for h in fn), fn
    print("ok test_links_retarget")


def test_opf_nav_ncx_sync():
    out, plan = split(mk.make_epub())
    zf = read_out(out)
    opf_tree = etree.fromstring(zf.read("OEBPS/content.opf"))
    OPF = es.NS_OPF
    manifest_hrefs = {unquote(i.get("href"))
                      for i in opf_tree.iter(es.q(es.NS_OPF, "item"))}
    for p in plan.parts:
        assert posixpath.relpath(p.href, "OEBPS") in manifest_hrefs
    assert "chapter1.xhtml" not in manifest_hrefs
    idrefs = [ir.get("idref") for ir in opf_tree.iter(es.q(es.NS_OPF, "itemref"))]
    assert len(idrefs) == len(plan.parts) + 1   # 三篇 + chapter2
    assert idrefs[-1] == "ch2"                   # 阅读顺序：拆分篇在前
    # manifest id 与 spine idref 一致
    manifest_ids = {i.get("id") for i in opf_tree.iter(es.q(es.NS_OPF, "item"))}
    assert all(r in manifest_ids for r in idrefs)

    # nav：每篇有目录入口，旧 chapter1.xhtml 链接消失
    nav = unquote(zf.read("OEBPS/nav.xhtml").decode("utf-8"))
    for p in plan.parts:
        assert posixpath.basename(p.href) in nav
    assert "chapter1.xhtml" not in nav
    # landmarks 中 #start 重定向到第一篇
    assert posixpath.basename(plan.parts[0].href) + "#start" in nav

    # NCX
    ncx = unquote(zf.read("OEBPS/toc.ncx").decode("utf-8"))
    for p in plan.parts:
        assert posixpath.basename(p.href) in ncx
    assert "chapter1.xhtml" not in ncx
    print("ok test_opf_nav_ncx_sync")


def test_css_and_images_intact():
    out, plan = split(mk.make_epub())
    zf = read_out(out)
    # 资源原样存在
    assert zf.read("OEBPS/style.css") == mk.CSS
    assert zf.read("OEBPS/images/pic.png") == mk.PNG_1PX
    first = zf.read(plan.parts[0].href).decode("utf-8")
    assert 'href="style.css"' in first
    assert 'src="images/pic.png"' in first
    print("ok test_css_and_images_intact")


def test_mimetype_first_and_stored():
    out, _ = split(mk.make_epub())
    zf = zipfile.ZipFile(io.BytesIO(out))
    assert zf.namelist()[0] == "mimetype"
    assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    assert zf.read("mimetype") == b"application/epub+zip"
    print("ok test_mimetype_first_and_stored")


def test_all_xhtml_wellformed():
    out, plan = split(mk.make_epub())
    zf = read_out(out)
    for name in zf.namelist():
        if name.endswith((".xhtml", ".opf", ".ncx")):
            etree.fromstring(zf.read(name))
    print("ok test_all_xhtml_wellformed")


# ---- 拒绝场景 -------------------------------------------------------------

def test_no_h2_rejected():
    no_h2 = ("""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title></head>
<body><p>没有切点</p></body></html>""").encode("utf-8")
    data = mk.make_epub({"OEBPS/chapter1.xhtml": no_h2})
    book = open_book(data)
    plan = es.build_plan(book, "OEBPS/chapter1.xhtml")
    assert not plan.valid and not plan.parts
    assert any(e.code == "no-h2" and e.file == "OEBPS/chapter1.xhtml"
               for e in plan.errors)
    book.close()
    print("ok test_no_h2_rejected")


def test_encryption_rejected():
    data = mk.make_epub(encryption_xml=mk.encryption_xml())
    book = open_book(data)
    plan = es.build_plan(book, "OEBPS/chapter1.xhtml")
    assert not plan.valid
    assert any(e.code == "encrypted" and "chapter1" in e.file
               for e in plan.errors)
    book.close()
    print("ok test_encryption_rejected")


def test_duplicate_id_rejected():
    dup = mk.CHAPTER1.replace(b'id="sec2"', b'id="sec1"', 1)
    # 制造同一文档内两个 sec1
    data = mk.make_epub({"OEBPS/chapter1.xhtml": dup})
    book = open_book(data)
    plan = es.build_plan(book, "OEBPS/chapter1.xhtml")
    assert not plan.valid
    assert any(e.code == "duplicate-id" and e.file.endswith("chapter1.xhtml")
               for e in plan.errors), [e.to_dict() for e in plan.errors]
    book.close()
    print("ok test_duplicate_id_rejected")


def test_same_id_in_other_document_ok():
    # chapter2 已有 id="sec1"，与 chapter1 的 sec1 同名但跨文件 -> 允许
    out, _ = split(mk.make_epub())
    zf = read_out(out)
    c2 = zf.read("OEBPS/chapter2.xhtml").decode("utf-8")
    assert 'id="sec1"' in c2
    print("ok test_same_id_in_other_document_ok")


def test_dangling_link_rejected():
    bad = mk.CHAPTER1.replace(b'href="#sec2"', b'href="#ghost"', 1)
    data = mk.make_epub({"OEBPS/chapter1.xhtml": bad})
    book = open_book(data)
    plan = es.build_plan(book, "OEBPS/chapter1.xhtml")
    assert not plan.valid
    assert any(e.code == "dangling-link" and "#ghost" in e.message
               for e in plan.errors), [e.to_dict() for e in plan.errors]
    book.close()
    print("ok test_dangling_link_rejected")


def test_missing_resource_rejected():
    data = mk.make_epub(files={"OEBPS/style.css": b"",
                               "OEBPS/images/pic.png": b""})
    # 从 zip 中真正删除：改用直接构造
    import zipfile as zfmod
    src = mk.make_epub()
    zin = zfmod.ZipFile(io.BytesIO(src))
    buf = io.BytesIO()
    with zfmod.ZipFile(buf, "w", zfmod.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if info.filename in ("OEBPS/images/pic.png",):
                continue
            zout.writestr(info, zin.read(info.filename))
    book = open_book(buf.getvalue())
    plan = es.build_plan(book, "OEBPS/chapter1.xhtml")
    assert not plan.valid
    assert any(e.code == "broken-resource" and "pic.png" in e.message
               for e in plan.errors), [e.to_dict() for e in plan.errors]
    book.close()
    print("ok test_missing_resource_rejected")


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in ALL:
        t()
    print("\n全部 %d 项测试通过" % len(ALL))
