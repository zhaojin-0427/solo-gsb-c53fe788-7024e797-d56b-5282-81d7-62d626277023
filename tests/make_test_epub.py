"""构造覆盖各类规则的测试 EPUB3。"""
import io
import zipfile

EPUB = "application/epub+zip"

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="zh">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:test-book-0001</dc:identifier>
    <dc:title>拆分测试书</dc:title>
    <dc:language>zh</dc:language>
    <meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="chap1" href="chap1.xhtml" media-type="application/xhtml+xml"/>
    <item id="other" href="other.xhtml" media-type="application/xhtml+xml"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="css" href="style/style.css" media-type="text/css"/>
    <item id="img" href="images/pic.png" media-type="image/png"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="chap1"/>
    <itemref idref="other"/>
  </spine>
</package>
"""

CSS = "body { line-height: 1.8; } h2 { color: navy; } .box { border: 1px solid #999; }\n"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000100ffff03000006000557bfab4000"
    "00000049454e44ae426082"
)

# 关键结构：
#  - h2 前有引导正文（并入首篇）
#  - 顶层 <section id="bigsec"> 包含全部三个切点（跨切点容器，h2 不是其首子）
#  - 第二个切点位于嵌套 <section id="nested"> 内
#  - 第三个切点是 <section id="sec3wrap"><h2 id="t3"> 形式（容器以 h2 开篇）
#  - 脚注 note1/note2 分布在不同切点，正文有往返链接；纯片段、相对路径链接
CHAP1 = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh">
<head>
  <title>第一章</title>
  <link rel="stylesheet" type="text/css" href="style/style.css"/>
</head>
<body id="ch1body">
  <p id="lead1">这是第一个 h2 之前的引导正文，应并入首篇。<a href="#note1">见脚注一</a></p>
  <section id="bigsec" class="box" epub:type="part">
    <span id="pre-h2">bigsec 在第一个 h2 前还有内容</span>
    <h2 id="t1">标题一</h2>
    <p id="p1">标题一正文，引用 <a href="other.xhtml#o1">另一章的锚点</a> 与 <a href="#t2">本篇标题二（跨篇）</a>。<a id="noteref1" href="#note1" epub:type="noteref">1</a></p>
    <aside id="note1" epub:type="footnote">脚注一，<a href="#noteref1">返回</a></aside>
    <img src="images/pic.png" alt="pic"/>
    <section id="nested">
      <h2 id="t2">标题二</h2>
      <p id="p2">标题二正文，纯片段 <a href="#t3">跳标题三</a>，相对路径 <a href="other.xhtml">去其他章</a>。<a id="noteref2" href="#note2">2</a></p>
      <aside id="note2" epub:type="footnote">脚注二，<a href="#noteref2">返回二</a></aside>
      <section id="sec3wrap">
        <h2 id="t3">标题三</h2>
        <p id="p3">标题三正文，<a href="#bigsec">跨段容器起点</a>、<a href="#nested">嵌套容器起点</a>、<a href="#lead1">首篇引导段</a>。</p>
      </section>
    </section>
  </section>
</body>
</html>
"""

OTHER = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="zh">
<head><title>另一章</title></head>
<body id="otherbody">
  <h2 id="o1">另一章锚点</h2>
  <p>另一章里有同名 id 也无妨：<a href="chap1.xhtml#note2">指向拆分文件的脚注二</a>、<a href="chap1.xhtml#t3">标题三</a>、<a href="chap1.xhtml">章首（无片段）</a>。</p>
  <p id="t1">这里也有叫 t1 的 id，但在别的文件。</p>
</body>
</html>
"""

NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh">
<head><title>目录</title></head>
<body>
<nav epub:type="toc" id="toc">
  <ol>
    <li><a href="chap1.xhtml">第一章（无片段，应展开）</a></li>
    <li><a href="other.xhtml#o1">另一章</a></li>
  </ol>
</nav>
<nav epub:type="landmarks">
  <ol><li><a epub:type="bodymatter" href="chap1.xhtml#t2">从标题二开始</a></li></ol>
</nav>
</body>
</html>
"""

NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="zh">
<head><meta name="dtb:uid" content="urn:uuid:test-book-0001"/></head>
<docTitle><text>拆分测试书</text></docTitle>
<navMap>
  <navPoint id="np1" playOrder="1"><navLabel><text>第一章</text></navLabel><content src="chap1.xhtml"/></navPoint>
  <navPoint id="np2" playOrder="2"><navLabel><text>另一章</text></navLabel><content src="other.xhtml#o1"/></navPoint>
</navMap>
</ncx>
"""


def build(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        z.writestr(info, EPUB)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/chap1.xhtml", CHAP1)
        z.writestr("OEBPS/other.xhtml", OTHER)
        z.writestr("OEBPS/nav.xhtml", NAV)
        z.writestr("OEBPS/toc.ncx", NCX)
        z.writestr("OEBPS/style/style.css", CSS)
        z.writestr("OEBPS/images/pic.png", PNG)


if __name__ == "__main__":
    build("/tmp/test.epub")
    print("wrote /tmp/test.epub")
