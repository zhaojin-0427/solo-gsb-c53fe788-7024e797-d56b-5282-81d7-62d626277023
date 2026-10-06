"""EPUB3 章节拆分核心逻辑。

只依赖标准库与 lxml。所有路径一律使用 EPUB ZIP 内的 POSIX 相对路径（以
容器根目录为基准）。主要入口：

* :class:`Book`         —— 读取 EPUB，提供 OPF/spine/manifest 信息；
* :func:`build_plan`    —— 对选中的 spine 文件做切分预览（含全部校验）；
* :func:`build_epub`    —— 基于预览计划生成新 EPUB 字节。
"""

from __future__ import annotations

import copy
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, unquote

from lxml import etree

# ---------------------------------------------------------------------------
# 命名空间与常量
# ---------------------------------------------------------------------------

NS_XHTML = "http://www.w3.org/1999/xhtml"
NS_OPF = "http://www.idpf.org/2007/opf"
NS_CONTAINER = "urn:oasis:names:tc:opf:xmlns:container"
NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"
NS_OPS = "http://www.idpf.org/2007/ops"          # epub:type
NS_XML = "http://www.w3.org/XML/1998/namespace"
NS_XLINK = "http://www.w3.org/1999/xlink"
NS_ENC = "http://www.w3.org/2001/04/xmlenc#"

XML_ID = "{%s}id" % NS_XML
XLINK_HREF = "{%s}href" % NS_XLINK
EPUB_TYPE = "{%s}type" % NS_OPS

CONTAINER_PATH = "META-INF/container.xml"
ENCRYPTION_PATH = "META-INF/encryption.xml"


class SplitError(Exception):
    """面向用户的可预期错误（中文消息）。"""


@dataclass
class ErrorItem:
    code: str
    message: str
    file: Optional[str] = None

    def to_dict(self) -> dict:
        return {"code": self.code, "file": self.file, "message": self.message}


@dataclass
class SpineItem:
    id: str          # manifest @id
    href: str        # 相对容器根的 ZIP 路径
    media_type: str
    linear: bool
    title: str

    def to_dict(self) -> dict:
        return {"id": self.id, "href": self.href,
                "media_type": self.media_type, "linear": self.linear,
                "title": self.title}


@dataclass
class Part:
    index: int
    href: str               # 新章节的 ZIP 路径
    title: str
    tree: object = field(repr=False, default=None)


@dataclass
class SplitPlan:
    source_href: str
    parts: list = field(default_factory=list)          # list[Part]
    errors: list = field(default_factory=list)         # list[ErrorItem]
    id_map: dict = field(default_factory=dict)         # 源 id -> 篇序号
    rewritten_docs: list = field(default_factory=list) # 链接被重写的其他 XHTML

    @property
    def valid(self) -> bool:
        return bool(self.parts) and not self.errors


# ---------------------------------------------------------------------------
# XML 工具
# ---------------------------------------------------------------------------

def parse_xml(data: bytes):
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    try:
        return etree.parse(io.BytesIO(data), parser)
    except etree.XMLSyntaxError as exc:
        raise SplitError("XML 解析失败：%s" % exc) from exc


def q(ns: str, tag: str) -> str:
    return "{%s}%s" % (ns, tag)


def localname(el) -> str:
    return etree.QName(el).localname


def find_child(el, names: set):
    for child in el:
        if isinstance(child.tag, str) and localname(child) in names:
            return child
    return None


def normalize_path(base_dir: str, ref: str) -> str:
    ref = unquote(ref)
    joined = posixpath.join(base_dir, ref) if base_dir else ref
    return posixpath.normpath(joined)


def relref(from_dir: str, target_path: str, fragment: str = "") -> str:
    """生成 from_dir 下文档指向 target_path 的相对引用。"""
    rel = posixpath.relpath(target_path, from_dir or ".")
    rel = quote(rel, safe="/.-_~%")
    return rel + ("#" + fragment if fragment else "")


def is_xhtml_item(media_type: str, href: str) -> bool:
    mt = (media_type or "").lower()
    if mt in ("application/xhtml+xml", "text/html"):
        return True
    return not mt and href.lower().endswith((".xhtml", ".html", ".htm", ".xht"))


def element_id(el) -> Optional[str]:
    return el.get("id", el.get(XML_ID))


def collect_ids(tree) -> set:
    ids = set()
    root = tree.getroot()
    if root is None:
        return ids
    for el in root.iter():
        v = el.get("id")
        if v is not None:
            ids.add(v)
        v = el.get(XML_ID)
        if v is not None:
            ids.add(v)
    return ids


def find_duplicate_ids(tree) -> list:
    """返回 [(id, 出现次数), ...]。"""
    seen = {}
    root = tree.getroot()
    if root is not None:
        for el in root.iter():
            for attr in ("id", XML_ID):
                v = el.get(attr)
                if v is not None:
                    seen[v] = seen.get(v, 0) + 1
    return [(v, n) for v, n in seen.items() if n > 1]


def get_body(tree):
    root = tree.getroot()
    if root is None:
        raise SplitError("XHTML 缺少根元素")
    body = root.find(q(NS_XHTML, "body"))
    if body is None:
        body = find_child(root, {"body"})
    if body is None:
        raise SplitError("XHTML 缺少 body 元素")
    return body


def serialize(tree) -> bytes:
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8")


def has_scheme(ref: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", ref))


def is_internal_ref(ref: str) -> bool:
    return bool(ref) and not has_scheme(ref)


# ---------------------------------------------------------------------------
# EPUB 读取
# ---------------------------------------------------------------------------

class Book:
    def __init__(self, data: bytes):
        self.data = data
        self.zf = zipfile.ZipFile(io.BytesIO(data), "r")
        self.names = set(self.zf.namelist())
        self.opf_path = self._find_opf_path()
        self.opf_dir = posixpath.dirname(self.opf_path)
        self.opf_tree = parse_xml(self.zf.read(self.opf_path))
        self.opf_root = self.opf_tree.getroot()
        self.manifest = {}      # id -> dict
        self.id_by_href = {}    # 容器路径 -> manifest id
        self._load_manifest()
        self.encryption_entries = self._read_encryption()
        self._id_cache = {}
        self._tree_cache = {}
        self.extra_names = set()    # 拆分产生的新文件

    def close(self):
        self.zf.close()

    def _find_opf_path(self) -> str:
        if CONTAINER_PATH not in self.names:
            raise SplitError("缺少 %s，不是合法的 EPUB 容器" % CONTAINER_PATH)
        tree = parse_xml(self.zf.read(CONTAINER_PATH))
        for rf in tree.getroot().iter(q(NS_CONTAINER, "rootfile")):
            if rf.get("media-type") == "application/oebps-package+xml":
                p = normalize_path("", rf.get("full-path"))
                if p in self.names:
                    return p
        raise SplitError("container.xml 中未找到 OPF 包文件")

    def _read_encryption(self) -> dict:
        if ENCRYPTION_PATH not in self.names:
            return {}
        tree = parse_xml(self.zf.read(ENCRYPTION_PATH))
        entries = {}
        for ed in tree.getroot().iter(q(NS_ENC, "EncryptedData")):
            method = ed.find(".//" + q(NS_ENC, "EncryptionMethod"))
            algo = method.get("Algorithm") if method is not None else "unknown"
            cr = ed.find(".//" + q(NS_ENC, "CipherReference"))
            if cr is not None and cr.get("URI"):
                entries[normalize_path("", cr.get("URI"))] = algo
        return entries

    def _load_manifest(self):
        manifest = find_child(self.opf_root, {"manifest"})
        if manifest is None:
            raise SplitError("OPF 缺少 manifest")
        for item in manifest:
            if not isinstance(item.tag, str) or localname(item) != "item":
                continue
            item_id, href = item.get("id"), item.get("href")
            if not item_id or not href:
                continue
            path = normalize_path(self.opf_dir, href)
            self.manifest[item_id] = {
                "href": path,
                "media_type": item.get("media-type", ""),
                "properties": item.get("properties", "") or "",
                "element": item,
            }
            self.id_by_href[path] = item_id

    def is_nav(self, item_id: str) -> bool:
        return "nav" in self.manifest[item_id]["properties"].split()

    def is_ncx(self, item_id: str) -> bool:
        return self.manifest[item_id]["media_type"].lower() == \
            "application/x-dtbncx+xml"

    def spine_items(self) -> list:
        spine = find_child(self.opf_root, {"spine"})
        if spine is None:
            raise SplitError("OPF 缺少 spine")
        result = []
        for ir in spine:
            if not isinstance(ir.tag, str) or localname(ir) != "itemref":
                continue
            idref = ir.get("idref")
            if not idref or idref not in self.manifest:
                continue
            info = self.manifest[idref]
            if not is_xhtml_item(info["media_type"], info["href"]):
                continue
            if self.is_nav(idref):
                continue
            linear = (ir.get("linear", "yes").lower() != "no")
            result.append(SpineItem(idref, info["href"],
                                    info["media_type"], linear,
                                    self._document_title(info["href"]) or idref))
        return result

    def _document_title(self, path: str) -> str:
        try:
            root = self.tree(path).getroot()
        except Exception:
            return ""
        title = root.find(q(NS_XHTML, "head") + "/" + q(NS_XHTML, "title"))
        return title.text.strip() if title is not None and title.text else ""

    def ncx_item_id(self) -> Optional[str]:
        spine = find_child(self.opf_root, {"spine"})
        toc_id = spine.get("toc") if spine is not None else None
        if toc_id and toc_id in self.manifest and self.is_ncx(toc_id):
            return toc_id
        for item_id, info in self.manifest.items():
            if self.is_ncx(item_id):
                return item_id
        return None

    def nav_item_id(self) -> Optional[str]:
        for item_id, info in self.manifest.items():
            if "nav" in info["properties"].split() and \
                    is_xhtml_item(info["media_type"], info["href"]):
                return item_id
        return None

    # -- 读取（带缓存） -----------------------------------------------------

    def tree(self, path: str):
        if path not in self._tree_cache:
            if path not in self.names and path not in self.extra_names:
                raise KeyError(path)
            if path in self.extra_names and path in self._tree_cache:
                return self._tree_cache[path]
            self._tree_cache[path] = parse_xml(self.zf.read(path))
        return self._tree_cache[path]

    def ids_of(self, path: str) -> set:
        if path not in self._id_cache:
            self._id_cache[path] = collect_ids(self.tree(path))
        return self._id_cache[path]

    def xhtml_paths(self) -> list:
        return [info["href"] for info in self.manifest.values()
                if is_xhtml_item(info["media_type"], info["href"])
                and (info["href"] in self.names
                     or info["href"] in self.extra_names)]

    def has_path(self, path: str) -> bool:
        return path in self.names or path in self.extra_names

    def register_part(self, path: str, tree):
        self.extra_names.add(path)
        self._tree_cache[path] = tree
        self._id_cache[path] = collect_ids(tree)


# ---------------------------------------------------------------------------
# 切分：事件展开 / 重放
# ---------------------------------------------------------------------------

def is_h2(el) -> bool:
    return isinstance(el.tag, str) and localname(el) == "h2" and \
        etree.QName(el).namespace in (NS_XHTML, None)


def heading_text(el) -> str:
    text = re.sub(r"\s+", " ", " ".join(el.itertext())).strip()
    return text[:80] if text else "未命名章节"


def flatten(body) -> list:
    """展开 body 为事件序列：("start"|"end"|"text", payload)。

    元素 text 紧随 start；tail 在 end 之后（属于父元素内容流）。
    """
    events = []

    def walk(node):
        events.append(("start", node))
        if node.text:
            events.append(("text", node.text))
        for child in node:
            walk(child)
            if child.tail:
                events.append(("text", child.tail))
        events.append(("end", node))

    if body.text:
        events.append(("text", body.text))
    for child in body:
        walk(child)
        if child.tail:
            events.append(("text", child.tail))
    return events


def compute_bounds(events: list):
    """按 h2 切界。

    标题前的全部内容并入第一篇：
      篇 0 = events[0 : cuts[1]]（含首个 h2 及其前正文）
      篇 i = events[cuts[i] : cuts[i+1]]
    返回 (bounds, cuts)，cuts 为各篇标题 h2 的事件下标。
    """
    cuts = [i for i, ev in enumerate(events)
            if ev[0] == "start" and is_h2(ev[1])]
    if not cuts:
        return [], []
    starts = [0] + cuts[1:]
    bounds = []
    for i, lo in enumerate(starts):
        hi = cuts[i + 1] if i + 1 < len(cuts) else len(events)
        bounds.append((lo, hi))
    return bounds, cuts


def safe_filename(title: str) -> str:
    name = re.sub(r"\s+", "-", title.strip())
    name = re.sub(r"[^\w一-鿿.\-]+", "-", name, flags=re.UNICODE).strip("-.")
    return (name or "section")[:60]


def part_href(source_href: str, index: int, title: str, used: set) -> str:
    directory = posixpath.dirname(source_href)
    stem = safe_filename(title)
    name = stem + ".xhtml"
    n = 2
    while name in used:
        name = "%s-%d.xhtml" % (stem, n)
        n += 1
    used.add(name)
    return posixpath.join(directory, name) if directory else name


def replay(events, lo, hi, part_index, id_map: dict):
    """重放 events[lo:hi] 为一个新 body，返回 body 元素。

    用“真身栈 + 克隆栈”双栈配对：边界处仍打开的祖先只克隆开标签
    （去子节点、去 id），从而允许嵌套 section 跨越切点；跨段容器的 id
    只保留在真正含其开标签的篇章（由 start 事件登记 id_map）。
    """
    body = etree.Element(q(NS_XHTML, "body"))
    clone_stack = [body]
    orig_stack = [None]

    # lo 处仍打开的祖先（外 -> 内）
    opened = []
    for ev in events[:lo]:
        if ev[0] == "start":
            opened.append(ev[1])
        elif ev[0] == "end" and opened:
            opened.pop()

    for orig in opened:
        clone = copy.deepcopy(orig)
        for kid in list(clone):
            clone.remove(kid)
        clone.text = None
        clone.tail = None
        if element_id(clone) is not None:
            clone.attrib.pop("id", None)
            clone.attrib.pop(XML_ID, None)
        clone_stack[-1].append(clone)
        clone_stack.append(clone)
        orig_stack.append(orig)

    for ev in events[lo:hi]:
        kind = ev[0]
        if kind == "start":
            orig = ev[1]
            clone = copy.deepcopy(orig)
            for kid in list(clone):
                clone.remove(kid)
            clone.text = None
            clone.tail = None
            eid = element_id(orig)
            if eid is not None:
                id_map.setdefault(eid, part_index)
            clone_stack[-1].append(clone)
            clone_stack.append(clone)
            orig_stack.append(orig)
        elif kind == "end":
            if len(orig_stack) > 1 and orig_stack[-1] is ev[1]:
                clone_stack.pop()
                orig_stack.pop()
        else:
            text = ev[1]
            parent = clone_stack[-1]
            if len(parent) == 0:
                parent.text = (parent.text or "") + text
            else:
                parent[-1].tail = (parent[-1].tail or "") + text

    return body


# ---------------------------------------------------------------------------
# 内链重定向
# ---------------------------------------------------------------------------

def split_ref(value: str):
    if "#" in value:
        p, frag = value.split("#", 1)
        return (p or None), (frag or "")
    return value or None, ""


def retarget_value(ref: str, doc_path: str, is_part_doc: bool,
                   part_index: int, plan: SplitPlan, book: "Book"):
    """计算重写后的 href 与悬空原因。

    返回 (new_ref, dangling_reason_or_None)。
    dangling_reason: "missing-file" | "missing-id"
    """
    doc_dir = posixpath.dirname(doc_path)
    path_part, frag = split_ref(ref)
    if path_part is None:
        # 拆分篇内的仅片段链接语义上指向被拆源文档的 id 空间
        target = plan.source_href if is_part_doc else doc_path
    else:
        target = normalize_path(doc_dir, path_part)

    # 目标是被拆源文件：一定按 id->篇 重定向
    if target == plan.source_href:
        if not frag:
            target_file = plan.parts[0].href
            return relref(doc_dir, target_file), None
        part_idx = plan.id_map.get(frag)
        if part_idx is None:
            return ref, "missing-id"
        target_file = plan.parts[part_idx].href
        # 篇内仅片段：保持仅片段形态
        if is_part_doc and part_idx == part_index and \
                split_ref(ref)[0] is None:
            return "#" + frag, None
        return relref(doc_dir, target_file, frag), None

    # 目标不是源文件：仅做最终态校验，引用保持原样
    if not book.has_path(target):
        return ref, "missing-file"
    if frag and frag not in book.ids_of(target):
        return ref, "missing-id"
    return ref, None


def retarget_tree(tree, doc_path: str, plan: SplitPlan, book: "Book",
                  is_part_doc: bool = False, part_index: int = -1,
                  errors: Optional[list] = None):
    """就地重写文档内全部内链并收集悬空问题。"""
    if errors is None:
        errors = []
    root = tree.getroot()

    # 元素 -> 作为“内链”处理的属性；命中即按目标文件/id 重定向
    link_tags = {"a", "area"}
    # 元素 -> 作为“资源引用”处理的属性；只校验文件存在，不重写片段
    resource_attrs = {
        "img": ("src",), "source": ("src",), "video": ("src", "poster"),
        "audio": ("src",), "track": ("src",), "script": ("src",),
        "iframe": ("src",), "embed": ("src",), "object": ("data",),
        "image": (XLINK_HREF, "href"),   # SVG <image>
        "use": (XLINK_HREF, "href"),
    }

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = localname(el)

        # 1) 内链：XHTML a/area@href、SVG a@xlink:href/@href
        if tag in link_tags:
            for attr in ("href", XLINK_HREF):
                ref = el.get(attr)
                if ref is None:
                    continue
                ref = ref.strip()
                if not is_internal_ref(ref):
                    continue
                new_ref, reason = retarget_value(
                    ref, doc_path, is_part_doc, part_index, plan, book)
                if reason:
                    errors.append(ErrorItem(
                        "dangling-link",
                        "悬空内链：%s（目标 %s 不存在）"
                        % (ref, ("id #" + split_ref(ref)[1])
                           if reason == "missing-id" else "文件"),
                        file=doc_path))
                elif new_ref != ref:
                    el.set(attr, new_ref)

        # 2) <link>：stylesheet/icon 等属资源；prev/next/contents 等是文档内链
        if tag == "link":
            rel = (el.get("rel", "") or "").lower().split()
            ref = el.get("href")
            if ref is not None and is_internal_ref(ref.strip()):
                ref = ref.strip()
                doc_rels = {"prev", "next", "up", "top", "contents",
                            "index", "glossary", "start"}
                if set(rel) & doc_rels:
                    new_ref, reason = retarget_value(
                        ref, doc_path, is_part_doc, part_index, plan, book)
                    if reason:
                        errors.append(ErrorItem(
                            "dangling-link",
                            "悬空内链：%s（目标不存在）" % ref, file=doc_path))
                    elif new_ref != ref:
                        el.set("href", new_ref)
                else:
                    check_resource(ref, doc_path, book, errors)

        # 3) 其他资源元素
        if tag in resource_attrs:
            for attr in resource_attrs[tag]:
                ref = el.get(attr)
                if ref is not None and is_internal_ref(ref.strip()):
                    check_resource(ref.strip(), doc_path, book, errors)

    return errors


def check_resource(ref, doc_path, book, errors):
    path_part = split_ref(ref)[0]
    if path_part is None:
        return
    target = normalize_path(posixpath.dirname(doc_path), path_part)
    if target and not book.has_path(target):
        errors.append(ErrorItem(
            "broken-resource",
            "资源引用不可用：%s（文件不存在）" % ref, file=doc_path))


# ---------------------------------------------------------------------------
# 切分计划（预览 + 校验）
# ---------------------------------------------------------------------------

def build_part_trees(book: "Book", source_href: str):
    src_tree = book.tree(source_href)
    body = get_body(src_tree)
    events = flatten(body)
    bounds, cuts = compute_bounds(events)
    if not bounds:
        return None

    id_map = {}
    new_bodies = [replay(events, lo, hi, i, id_map)
                  for i, (lo, hi) in enumerate(bounds)]

    # body 自身 id 保留在第一篇
    body_id = element_id(body)
    if body_id is not None:
        id_map[body_id] = 0
        new_bodies[0].set("id", body_id)

    titles = [heading_text(events[c][1]) for c in cuts]
    used_names = {posixpath.basename(p)
                  for p in book.names if posixpath.dirname(p)
                  == posixpath.dirname(source_href)}
    used_names.add(posixpath.basename(source_href))
    hrefs = [part_href(source_href, i, titles[i], used_names)
             for i in range(len(bounds))]

    parts = []
    for i, new_body in enumerate(new_bodies):
        tree = copy.deepcopy(src_tree)
        root = tree.getroot()
        old_body = root.find(q(NS_XHTML, "body"))
        kept_attrs = {k: v for k, v in old_body.attrib.items()
                      if k not in ("id", XML_ID)}
        new_body.attrib.update(kept_attrs)
        if i == 0 and body_id is not None:
            new_body.set("id", body_id)
        root.replace(old_body, new_body)
        head = root.find(q(NS_XHTML, "head"))
        if head is not None:
            title_el = head.find(q(NS_XHTML, "title"))
            if title_el is None:
                title_el = etree.SubElement(head, q(NS_XHTML, "title"))
            title_el.text = titles[i]
        parts.append(Part(index=i, href=hrefs[i],
                          title=titles[i], tree=tree))
    return parts, id_map


def build_plan(book: "Book", source_href: str) -> SplitPlan:
    plan = SplitPlan(source_href=source_href)

    # 1) 加密：逐文件报错并拒绝
    if book.encryption_entries:
        for path, algo in book.encryption_entries.items():
            plan.errors.append(ErrorItem(
                "encrypted",
                "文件已加密（算法 %s），不支持拆分加密 EPUB" % algo,
                file=path))
        return plan

    if source_href not in book.names:
        raise SplitError("所选文件不在 EPUB 包中：%s" % source_href)

    # 2) 篇内 id 重复（跨文件允许同名 id）
    for path in book.xhtml_paths():
        if path not in book.names:
            continue
        try:
            tree = book.tree(path)
        except Exception as exc:
            plan.errors.append(ErrorItem(
                "bad-xml", "XHTML 无法解析：%s" % exc, file=path))
            continue
        for value, count in find_duplicate_ids(tree):
            plan.errors.append(ErrorItem(
                "duplicate-id",
                "篇内 id 重复：%r 出现 %d 次" % (value, count),
                file=path))

    # 3) 构造切分树
    built = build_part_trees(book, source_href)
    if built is None:
        plan.errors.append(ErrorItem(
            "no-h2", "所选正文中没有任何 <h2>，无法作为切点",
            file=source_href))
        return plan
    plan.parts, plan.id_map = built

    # 4) 注册新文件后，对全部 XHTML 做最终态内链校验/重定向
    nav_path = None
    nav_id = book.nav_item_id()
    if nav_id:
        nav_path = book.manifest[nav_id]["href"]

    for p in plan.parts:
        book.register_part(p.href, p.tree)

    for p in plan.parts:
        retarget_tree(p.tree, p.href, plan, book,
                      is_part_doc=True, part_index=p.index,
                      errors=plan.errors)

    for path in book.xhtml_paths():
        if path == source_href or path in book.extra_names:
            continue
        if path == nav_path:
            continue   # 导航文档由 update_nav 专门处理
        retarget_tree(book.tree(path), path, plan, book,
                      errors=plan.errors)
        plan.rewritten_docs.append(path)

    return plan


# ---------------------------------------------------------------------------
# OPF / nav / NCX 同步更新
# ---------------------------------------------------------------------------

def href_from_opf(book: "Book", target_path: str) -> str:
    return quote(posixpath.relpath(target_path, book.opf_dir or "."),
                 safe="/.-_~%")


def unique_id(existing: set, base: str) -> str:
    candidate, n = base, 2
    while candidate in existing:
        candidate = "%s-%d" % (base, n)
        n += 1
    return candidate


def update_opf(book: "Book", plan: SplitPlan):
    root = book.opf_root
    manifest = find_child(root, {"manifest"})
    spine = find_child(root, {"spine"})

    source_item_id = book.id_by_href[plan.source_href]
    source_item = book.manifest[source_item_id]["element"]
    source_props = source_item.get("properties", "")

    new_ids = []
    for p in plan.parts:
        item_id = unique_id(
            {it.get("id") for it in manifest
             if isinstance(it.tag, str) and localname(it) == "item"},
            "split%d" % (p.index + 1))
        item = etree.SubElement(manifest, q(NS_OPF, "item"))
        item.set("id", item_id)
        item.set("href", href_from_opf(book, p.href))
        item.set("media-type", "application/xhtml+xml")
        if source_props:
            item.set("properties", source_props)
        new_ids.append(item_id)

    source_itemref = None
    for ir in spine:
        if isinstance(ir.tag, str) and localname(ir) == "itemref" \
                and ir.get("idref") == source_item_id:
            source_itemref = ir
            break
    if source_itemref is None:
        for item_id in new_ids:
            ir = etree.SubElement(spine, q(NS_OPF, "itemref"))
            ir.set("idref", item_id)
    else:
        parent = source_itemref.getparent()
        pos = list(parent).index(source_itemref)
        kept = {k: v for k, v in source_itemref.attrib.items()
                if k != "idref"}
        for offset, item_id in enumerate(new_ids):
            ir = etree.Element(q(NS_OPF, "itemref"))
            ir.attrib.update(kept)
            ir.set("idref", item_id)
            parent.insert(pos + offset, ir)
        parent.remove(source_itemref)

    manifest.remove(source_item)
    # manifest 索引同步
    for item_id in (source_item_id,):
        book.manifest.pop(item_id, None)
    book.id_by_href.pop(plan.source_href, None)
    for item_id, p in zip(new_ids, plan.parts):
        book.manifest[item_id] = {
            "href": p.href, "media_type": "application/xhtml+xml",
            "properties": source_props, "element": None}
        book.id_by_href[p.href] = item_id


def update_nav(book: "Book", plan: SplitPlan):
    """同步 EPUB3 导航文档：展开源章节目录条目，重定向其余源文件内链。"""
    nav_id = book.nav_item_id()
    if not nav_id:
        return
    nav_path = book.manifest[nav_id]["href"]
    tree = book.tree(nav_path)
    nav_dir = posixpath.dirname(nav_path)

    def resolves_source(href, top_only=False):
        if href is None:
            return False
        path_part, frag = split_ref(href)
        if path_part is None:
            return False
        if top_only and frag:
            return False
        return normalize_path(nav_dir, path_part) == plan.source_href

    # 1) 展开直接引用源文件顶部（无片段）的目录 <li>；
    #    带片段的条目（小节、page-list、landmarks）只做重定向
    replaced = set()
    for a in list(tree.getroot().iter(q(NS_XHTML, "a"))):
        if not resolves_source(a.get("href"), top_only=True):
            continue
        li = a.getparent()
        if li is None or localname(li) != "li" or id(li) in replaced:
            continue
        list_parent = li.getparent()
        if list_parent is None or localname(list_parent) not in ("ol", "ul"):
            continue
        replaced.add(id(li))
        pos = list(list_parent).index(li)
        nested = li.find(q(NS_XHTML, "ol"))
        if nested is None:
            nested = li.find(q(NS_XHTML, "ul"))
        for offset, p in enumerate(plan.parts):
            new_li = etree.Element(q(NS_XHTML, "li"))
            new_a = etree.SubElement(new_li, q(NS_XHTML, "a"))
            new_a.set("href", relref(nav_dir, p.href))
            new_a.text = p.title
            if offset == 0 and nested is not None:
                new_li.append(copy.deepcopy(nested))
            list_parent.insert(pos + offset, new_li)
        list_parent.remove(li)

    # 2) 若目录里没有源文件条目，则向 toc nav 追加全部篇章入口
    if not replaced:
        toc_nav = None
        for nav in tree.getroot().iter(q(NS_XHTML, "nav")):
            val = nav.get(EPUB_TYPE, "")
            if "toc" in val.split():
                toc_nav = nav
                break
        if toc_nav is None:
            toc_nav = next(iter(tree.getroot().iter(q(NS_XHTML, "nav"))), None)
        if toc_nav is not None:
            ol = toc_nav.find(q(NS_XHTML, "ol"))
            if ol is None:
                ol = etree.SubElement(toc_nav, q(NS_XHTML, "ol"))
            for p in plan.parts:
                li = etree.SubElement(ol, q(NS_XHTML, "li"))
                a = etree.SubElement(li, q(NS_XHTML, "a"))
                a.set("href", relref(nav_dir, p.href))
                a.text = p.title

    # 3) 其余仍指向源文件的链接（page-list、landmarks、片段等）统一重定向
    leftover = []
    retarget_tree(tree, nav_path, plan, book, errors=leftover)
    # 导航文档内残留的悬空链接并入构建期错误
    for err in leftover:
        if err not in plan.errors:
            plan.errors.append(err)


def update_ncx(book: "Book", plan: SplitPlan):
    """存在 NCX 时同步：重写 content/@src，展开源文件 navPoint。"""
    ncx_id = book.ncx_item_id()
    if not ncx_id:
        return
    ncx_path = book.manifest[ncx_id]["href"]
    tree = book.tree(ncx_path)
    ncx_dir = posixpath.dirname(ncx_path)
    root = tree.getroot()

    def parse_src(src):
        if "#" in src:
            p, f = src.split("#", 1)
        else:
            p, f = src, ""
        target = normalize_path(ncx_dir, p) if p else plan.source_href
        return target, (f or "")

    def content_of(np_el):
        c = np_el.find(q(NS_NCX, "content"))
        if c is None or not c.get("src"):
            return None, ""
        return parse_src(c.get("src").strip())

    def all_ids():
        return {el.get("id") for el in root.iter() if el.get("id")}

    used_ids = all_ids()

    def make_navpoint(p):
        np_el = etree.Element(q(NS_NCX, "navPoint"))
        nid = unique_id(used_ids, "splitnav%d" % (p.index + 1))
        used_ids.add(nid)
        np_el.set("id", nid)
        label = etree.SubElement(np_el, q(NS_NCX, "navLabel"))
        text = etree.SubElement(label, q(NS_NCX, "text"))
        text.text = p.title
        c = etree.SubElement(np_el, q(NS_NCX, "content"))
        c.set("src", relref(ncx_dir, p.href))
        return np_el

    # 1) 先展开 content 无片段指向源文件的 navPoint（必须在重写 src 之前）
    candidates = [np_el for np_el in list(root.iter(q(NS_NCX, "navPoint")))
                  if content_of(np_el) == (plan.source_href, "")]
    for np_el in candidates:
        parent = np_el.getparent()
        if parent is None:
            continue
        pos = list(parent).index(np_el)
        nested = [ch for ch in list(np_el)
                  if localname(ch) == "navPoint"]
        for offset, p in enumerate(plan.parts):
            new_np = make_navpoint(p)
            if offset == 0:
                for ch in nested:
                    new_np.append(copy.deepcopy(ch))
            parent.insert(pos + offset, new_np)
        parent.remove(np_el)

    # 2) 再重写所有仍指向源文件的 content/@src（带片段 -> 对应篇）
    for content in root.iter(q(NS_NCX, "content")):
        src = content.get("src")
        if not src or not is_internal_ref(src.strip()):
            continue
        target, frag = parse_src(src.strip())
        if target != plan.source_href:
            continue
        if frag and frag in plan.id_map:
            p = plan.parts[plan.id_map[frag]]
            content.set("src", relref(ncx_dir, p.href, frag))
        elif not frag:
            content.set("src", relref(ncx_dir, plan.parts[0].href))
        else:
            content.set("src", relref(ncx_dir, plan.parts[0].href))
            plan.errors.append(ErrorItem(
                "dangling-link",
                "NCX 内链指向不存在的 id：#%s" % frag, file=ncx_path))

    # 3) playOrder 按文档顺序重新编号
    order = 0
    for np_el in root.iter(q(NS_NCX, "navPoint")):
        order += 1
        np_el.set("playOrder", str(order))


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------

def build_epub(book: "Book", plan: SplitPlan, filename_hint: str = "") -> bytes:
    if not plan.valid:
        raise SplitError("校验未通过，拒绝下载")

    update_opf(book, plan)
    update_nav(book, plan)
    update_ncx(book, plan)
    if not plan.valid:
        raise SplitError("校验未通过，拒绝下载")

    nav_id = book.nav_item_id()
    ncx_id = book.ncx_item_id()
    nav_path = book.manifest[nav_id]["href"] if nav_id else None
    ncx_path = book.manifest[ncx_id]["href"] if ncx_id else None

    tree_outputs = {p.href: serialize(p.tree) for p in plan.parts}
    tree_outputs[book.opf_path] = serialize(book.opf_tree)
    if nav_path:
        tree_outputs[nav_path] = serialize(book.tree(nav_path))
    if ncx_path:
        tree_outputs[ncx_path] = serialize(book.tree(ncx_path))
    for doc_path in plan.rewritten_docs:
        tree_outputs[doc_path] = serialize(book.tree(doc_path))

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        # mimetype 必须为首项、STORED、无额外字段
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        zout.writestr(zi, b"application/epub+zip")

        emitted = {"mimetype"}
        for info in book.zf.infolist():
            name = info.filename
            if name == "mimetype" or name == plan.source_href:
                continue
            if name in tree_outputs:
                zout.writestr(name, tree_outputs[name])
            else:
                zout.writestr(name, book.zf.read(name),
                              compress_type=info.compress_type)
            emitted.add(name)
        for href, data in tree_outputs.items():
            if href not in emitted:
                zout.writestr(href, data)

    return out.getvalue()


def preview_html(book: "Book", plan: SplitPlan, index: int,
                 session_base: str) -> bytes:
    """生成浏览器预览用的单文件 HTML（注入 <base> 以复用包内 CSS/图片）。"""
    part = plan.parts[index]
    tree = copy.deepcopy(part.tree)
    head = tree.getroot().find(q(NS_XHTML, "head"))
    if head is None:
        head = etree.Element(q(NS_XHTML, "head"))
        tree.getroot().insert(0, head)
    base = etree.Element(q(NS_XHTML, "base"))
    # 预览资源根（与 /api/file/{sid}/root/{path} 路由对应）
    base.set("href", "/api/file/%s/root/%s/"
             % (session_base.strip("/"),
                quote(posixpath.dirname(part.href), safe="/.-_~%")))
    head.insert(0, base)
    meta = etree.SubElement(head, q(NS_XHTML, "meta"))
    meta.set("charset", "utf-8")
    return b"<!DOCTYPE html>\n" + serialize(tree)
