"""校验拆分后的 EPUB：结构合法、清单一致、所有内链可达。"""
import posixpath
import sys
import zipfile
from urllib.parse import urldefrag
from lxml import etree

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
}

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/out.epub"
z = zipfile.ZipFile(path)
assert z.read("mimetype") == b"application/epub+zip", "mimetype 错误"
assert z.infolist()[0].filename == "mimetype", "mimetype 必须为首条目"

errors = []
trees = {}
for name in z.namelist():
    if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
        try:
            trees[name] = etree.fromstring(z.read(name))
        except etree.XMLSyntaxError as e:
            errors.append(f"{name} XML 不合法: {e}")

opf_name = "OEBPS/content.opf"
opf = trees[opf_name]
opf_dir = posixpath.dirname(opf_name)

manifest = {}
for item in opf.iter():
    if etree.QName(item).localname == "item":
        manifest[item.get("id")] = item.get("href")

# 所有 manifest 文件必须存在
for iid, href in manifest.items():
    full = posixpath.normpath(posixpath.join(opf_dir, href))
    if full not in z.namelist():
        errors.append(f"manifest 条目缺失文件: {iid} -> {full}")

# spine 顺序
spine_ids = []
for ref in opf.iter():
    if etree.QName(ref).localname == "itemref":
        spine_ids.append(ref.get("idref"))
print("spine:", spine_ids)
expected = ["chap1", "chap1-part02", "chap1-part03", "other"]
if spine_ids != expected:
    errors.append(f"spine 顺序不符: {spine_ids} != {expected}")

# 构建每篇 id 集合
ids_by_file = {}
for name, root in trees.items():
    if name.endswith(".xhtml"):
        ids = set()
        for el in root.iter():
            if el.get("id"):
                ids.add(el.get("id"))
        ids_by_file[name] = ids

# 校验所有 a[href] 内链
for name, root in trees.items():
    if not name.endswith((".xhtml", ".ncx")):
        continue
    attrs = []
    for el in root.iter():
        ln = etree.QName(el).localname
        if ln in ("a", "area") and el.get("href"):
            attrs.append(el.get("href"))
        if ln == "content" and el.get("src"):
            attrs.append(el.get("src"))
    base = posixpath.dirname(name)
    for href in attrs:
        if href.startswith(("http://", "https://", "mailto:")):
            continue
        bare, frag = urldefrag(href)
        if not bare:
            target = name
        else:
            target = posixpath.normpath(posixpath.join(base, bare))
        if target not in z.namelist():
            errors.append(f"{name}: 链接目标文件不存在 {href} -> {target}")
            continue
        if frag and target in ids_by_file and frag not in ids_by_file[target]:
            errors.append(f"{name}: 悬空片段 {href} (在 {target} 中找不到 #{frag})")

# nav 应有 3 个拆出篇的条目
nav = trees["OEBPS/nav.xhtml"]
anchors = [a.get("href") for a in nav.iter() if etree.QName(a).localname == "a"]
print("nav anchors:", anchors)
for part in ("chap1.xhtml#t1", "chap1-part02.xhtml#t2", "chap1-part03.xhtml#t3"):
    if not any(part in a for a in anchors):
        errors.append(f"nav 缺少 {part}")

ncx = trees["OEBPS/toc.ncx"]
srcs = [c.get("src") for c in ncx.iter() if etree.QName(c).localname == "content"]
print("ncx srcs:", srcs)
for part in ("chap1.xhtml#t1", "chap1-part02.xhtml#t2", "chap1-part03.xhtml#t3"):
    if not any(part in s for s in srcs):
        errors.append(f"ncx 缺少 {part}")

# 图片/CSS 仍可访问
for res in ("OEBPS/images/pic.png", "OEBPS/style/style.css"):
    if res not in z.namelist():
        errors.append(f"资源丢失: {res}")

# 首篇沿用源文件名，但内容必须已是拆分后的首篇（含 t1，不含 t2/t3）
first = trees["OEBPS/chap1.xhtml"]
first_ids = ids_by_file["OEBPS/chap1.xhtml"]
assert "t1" in first_ids and "t2" not in first_ids and "t3" not in first_ids, "首篇内容未被拆分版替换"

# 校验关键重定向
part1 = etree.tostring(trees["OEBPS/chap1.xhtml"], encoding="unicode")
part3 = etree.tostring(trees["OEBPS/chap1-part03.xhtml"], encoding="unicode")
other = etree.tostring(trees["OEBPS/other.xhtml"], encoding="unicode")
assert "chap1-part02.xhtml#t2" in part1
assert "chap1.xhtml#bigsec" in part3
assert "chap1-part02.xhtml#note2" in other, "other.xhtml 脚注重定向失败"
assert "chap1-part03.xhtml#t3" in other
# other 同名 t1 不受影响
assert 'id="t1"' in other

if errors:
    print("\n".join(errors))
    sys.exit(1)
print("\n全部校验通过 ✔  共", len(z.namelist()), "个条目")
