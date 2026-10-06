"""测试夹具：在内存中构造不同形态的 EPUB3。"""

import io
import zipfile

MIMETYPE = b"application/epub+zip"

CONTAINER = b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opf:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
      media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

CSS = b"""body { line-height: 1.7; }
h2 { color: navy; margin-top: 2em; }
.note-ref { vertical-align: super; font-size: .8em; }
"""

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082")


def opf(spine_ids=("ch1", "ch2"), nav="nav", ncx="ncx"):
    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="ch2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="fn" href="footnotes.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
        '<item id="img" href="images/pic.png" media-type="image/png"/>',
    ]
    refs = "".join('<itemref idref="%s"/>' % sid for sid in spine_ids
                   if sid not in ("nav", "ncx"))
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="zh">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:test-book</dc:identifier>
    <dc:title>测试书</dc:title>
    <dc:language>zh</dc:language>
    <meta name="dtb:uid" content="urn:uuid:test-book"/>
  </metadata>
  <manifest>%s</manifest>
  <spine toc="ncx">%s</spine>
</package>""" % ("".join(manifest_items), refs)).encode("utf-8")


NAV = ("""<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>导航</title></head>
<body>
<nav epub:type="toc" id="toc">
  <ol>
    <li><a href="chapter1.xhtml">第一章 原文</a>
      <ol>
        <li><a href="chapter1.xhtml#sec1">小节一</a></li>
        <li><a href="chapter1.xhtml#sec2">小节二</a></li>
      </ol>
    </li>
    <li><a href="chapter2.xhtml">第二章</a></li>
  </ol>
</nav>
<nav epub:type="landmarks">
  <ol><li><a epub:type="bodymatter" href="chapter1.xhtml#start">正文开始</a></li></ol>
</nav>
</body>
</html>
""").encode("utf-8")

NCX = ("""<?xml version="1.0" encoding="UTF-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:test-book"/></head>
  <docTitle><text>测试书</text></docTitle>
  <navMap>
    <navPoint id="np1" playOrder="1">
      <navLabel><text>第一章 原文</text></navLabel>
      <content src="chapter1.xhtml"/>
      <navPoint id="np1-1" playOrder="2">
        <navLabel><text>小节一</text></navLabel>
        <content src="chapter1.xhtml#sec1"/>
      </navPoint>
    </navPoint>
    <navPoint id="np2" playOrder="3">
      <navLabel><text>第二章</text></navLabel>
      <content src="chapter2.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
""").encode("utf-8")

# chapter1：标题前正文 + 嵌套 section 跨切点 + 脚注往返 + 图片/CSS + 同名 id
CHAPTER1 = ("""<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
<title>第一章 原文</title>
<link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body id="start">
<p id="intro">这是<strong>标题前</strong>的导语，应并入第一篇。<img src="images/pic.png" alt="图"/></p>
<section id="bigwrap" epub:type="chapter">
  <h2 id="sec1">第一节 开端</h2>
  <p>第一节正文，见<a id="fnref1" href="footnotes.xhtml#fn1" epub:type="noteref">注1</a>。</p>
  <p>仅片段跳转：<a href="#sec2">去第二节</a>；容器 id：<a href="#bigwrap">回到外层容器</a>。</p>
  <section id="subwrap">
    <h3>三级标题（非切点）</h3>
    <p>嵌套 section 内部内容随切分流动。</p>
  </section>
  <h2 id="sec2">第二节 发展</h2>
  <p>第二节正文，相对路径回链：<a href="chapter1.xhtml#sec1">回到第一节</a>。</p>
  <h2 id="sec3">第三节 结局</h2>
  <p>结尾，跨到第二章：<a href="chapter2.xhtml#c2">去第二章锚点</a>。</p>
</section>
</body>
</html>
""").encode("utf-8")

CHAPTER2 = ("""<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml">
<head><title>第二章</title><link rel="stylesheet" href="style.css"/></head>
<body>
<h2 id="c2">第二章标题（同名 id 在另一篇合法）</h2>
<p>第二章段落，指向被拆文件：<a href="chapter1.xhtml#sec2">见第二节</a>；
仅片段本文件锚点 <a href="#c2">本页顶</a>。</p>
<p>同名 id sec1（在本文件内唯一）：<span id="sec1">x</span></p>
</body>
</html>
""").encode("utf-8")

FOOTNOTES = ("""<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>脚注</title></head>
<body>
<aside id="fn1" epub:type="footnote">
  <p>脚注内容。<a href="chapter1.xhtml#fnref1" epub:type="backlink">返回</a></p>
</aside>
</body>
</html>
""").encode("utf-8")


def make_epub(files=None, encryption_xml=None):
    files = files or {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), MIMETYPE,
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER)
        if encryption_xml is not None:
            zf.writestr("META-INF/encryption.xml", encryption_xml)
        default = {
            "OEBPS/content.opf": opf(),
            "OEBPS/nav.xhtml": NAV,
            "OEBPS/toc.ncx": NCX,
            "OEBPS/chapter1.xhtml": CHAPTER1,
            "OEBPS/chapter2.xhtml": CHAPTER2,
            "OEBPS/footnotes.xhtml": FOOTNOTES,
            "OEBPS/style.css": CSS,
            "OEBPS/images/pic.png": PNG_1PX,
        }
        default.update(files)
        for name, data in default.items():
            zf.writestr(name, data)
    return buf.getvalue()


def encryption_xml(target="OEBPS/chapter1.xhtml"):
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
  xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
 <enc:EncryptedKey><enc:ReferenceList>
  <enc:DataReference URI="#key1"/>
 </enc:ReferenceList></enc:EncryptedKey>
 <enc:EncryptedData Id="ed1">
  <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
  <enc:KeyInfo><enc:KeyName>key1</enc:KeyName></enc:KeyInfo>
  <enc:CipherData><enc:CipherReference URI="%s"/></enc:CipherData>
 </enc:EncryptedData>
</encryption>""" % target).encode("utf-8")
