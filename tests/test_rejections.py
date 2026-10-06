"""验证拒绝下载的各类场景。"""
import io
import sys
import zipfile

sys.path.insert(0, ".")
from app.splitter import Book, Splitter, SplitError

EPUB = "application/epub+zip"

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF_TPL = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">x</dc:identifier><dc:title>t</dc:title><dc:language>zh</dc:language>
  </metadata>
  <manifest>{manifest}</manifest>
  <spine>{spine}</spine>
</package>"""

results = []


def expect_reject(name, data, path, expect_file_hint=None):
    try:
        book = Book(data)
        sp = Splitter(book, path)
        sp.run()
        results.append((name, False, "未拒绝！"))
    except SplitError as e:
        ok = True
        detail = e.message
        if expect_file_hint and not any(expect_file_hint in f for f in e.files):
            ok = False
            detail += f"  (files={e.files} 未含 {expect_file_hint})"
        results.append((name, ok, detail))
    except Exception as e:  # noqa
        results.append((name, False, f"抛出非预期异常 {type(e).__name__}: {e}"))


def make_epub(files: dict[str, str], manifest: str, spine: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, EPUB)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF_TPL.format(manifest=manifest, spine=spine))
        for n, content in files.items():
            z.writestr(n, content)
    return buf.getvalue()


# 1) 无 h2
no_h2 = make_epub(
    {"OEBPS/c.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>x</p></body></html>'},
    '<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>',
    '<itemref idref="c"/>',
)
expect_reject("无 h2", no_h2, "OEBPS/c.xhtml", "c.xhtml")

# 2) 加密
enc_data = make_epub(
    {"OEBPS/c.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2 id="a">a</h2></body></html>'},
    '<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>',
    '<itemref idref="c"/>',
)
buf = io.BytesIO()
with zipfile.ZipFile(io.BytesIO(enc_data)) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        zout.writestr(item, zin.read(item.filename))
    zout.writestr(
        "META-INF/encryption.xml",
        """<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData><enc:CipherData><enc:CipherReference URI="OEBPS/c.xhtml"/></enc:CipherData></enc:EncryptedData>
</encryption>""",
    )
expect_reject("加密 EPUB", buf.getvalue(), "OEBPS/c.xhtml", "OEBPS/c.xhtml")

# 3) 源文件篇内 id 重复
dup_id = make_epub(
    {
        "OEBPS/c.xhtml": (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<h2 id="a">一</h2><p id="x">1</p>'
            '<h2 id="b">二</h2><p id="x">2</p>'
            "</body></html>"
        )
    },
    '<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>',
    '<itemref idref="c"/>',
)
expect_reject("篇内 id 重复", dup_id, "OEBPS/c.xhtml", "c.xhtml")

# 4) 拆分后悬空内链（链接指向只存在于被删掉部分的 id；这里模拟一个不存在的 id）
dangling = make_epub(
    {
        "OEBPS/c.xhtml": (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<h2 id="a">一</h2><p><a href="#ghost">不存在的锚点</a></p>'
            '<h2 id="b">二</h2><p>2</p>'
            "</body></html>"
        )
    },
    '<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>',
    '<itemref idref="c"/>',
)
expect_reject("悬空内链", dangling, "OEBPS/c.xhtml", "c.xhtml")

# 5) 其他章节里有指向源文件不存在 id 的链接
dangling2 = make_epub(
    {
        "OEBPS/c.xhtml": (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<h2 id="a">一</h2><p>1</p><h2 id="b">二</h2><p>2</p>'
            "</body></html>"
        ),
        "OEBPS/o.xhtml": (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
            '<a href="c.xhtml#gone">坏链</a></p></body></html>'
        ),
    },
    '<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="o" href="o.xhtml" media-type="application/xhtml+xml"/>',
    '<itemref idref="c"/><itemref idref="o"/>',
)
expect_reject("他章悬空链", dangling2, "OEBPS/c.xhtml", "o.xhtml")

# 6) 非 EPUB3（有正文，但 package version=2.0）
epub2 = make_epub(
    {"OEBPS/c.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2 id="a">a</h2></body></html>'},
    '<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>',
    '<itemref idref="c"/>',
)
buf = io.BytesIO()
with zipfile.ZipFile(io.BytesIO(epub2)) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "OEBPS/content.opf":
            data = data.replace(b'version="3.0"', b'version="2.0"')
        zout.writestr(item, data)
expect_reject("EPUB2 版本", buf.getvalue(), "OEBPS/c.xhtml", "content.opf")

failed = 0
for name, ok, detail in results:
    print(("✔" if ok else "�’"), name, "—", detail if not ok else "")
    if not ok:
        failed += 1
sys.exit(1 if failed else 0)
