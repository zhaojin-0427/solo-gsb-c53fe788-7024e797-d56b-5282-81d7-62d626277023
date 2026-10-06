"""EPUB3 章节拆分核心引擎。

依据 spine 中选定的一篇 XHTML 正文，以每个 h2 为切点拆成多篇：
- 首个 h2 之前的正文并入首篇；
- 嵌套 section 等容器可跨越切点（切点处容器被“重新打开”）；
- 跨切点容器的 id 只保留在它实际出现的第一篇，其余各篇剥离；
- OPF manifest/spine、导航文档、存在时的 NCX 同步更新；
- 所有 XHTML 内链（脚注往返、纯片段、相对路径）按原目标文件与 id 重定向。
"""

from __future__ import annotations

import copy
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional
from urllib.parse import urldefrag, unquote

from lxml import etree

# ---------------------------------------------------------------- 命名空间

NS = {
    "xhtml": "http://www.w3.org/1999/xhtml",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "enc": "http://www.w3.org/2001/04/xmlenc#",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xlink": "http://www.w3.org/1999/xlink",
    "svg": "http://www.w3.org/2000/svg",
    "dcterms": "http://purl.org/dc/terms/",
}
XHTML = "{%s}" % NS["xhtml"]
OPF = "{%s}" % NS["opf"]
CONTAINER = "{%s}" % NS["container"]
ENC = "{%s}" % NS["enc"]
NCX = "{%s}" % NS["ncx"]
XLINK_HREF = "{%s}href" % NS["xlink"]
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"

EPUB_MIME = "application/epub+zip"
XHTML_MIME = "application/xhtml+xml"
NCX_MIME = "application/x-dtbncx+xml"
NAV_PROP = "nav"
SVG = "{%s}" % NS["svg"]

_XML_PARSER = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)


class SplitError(Exception):
    """拒绝下载类错误：携带问题所在文件列表。"""

    def __init__(self, message: str, files: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.files = files or []


# ---------------------------------------------------------------- 数据结构


@dataclass
class Segment:
    index: int
    name: str                      # 包内路径（zip 名称）
    title: str                     # 取自 h2 文本
    h2_id: str                     # 切点 h2 的 id（必要时自动生成）
    doc: etree._ElementTree        # 新 XHTML 文档树
    item_id: str                   # OPF item id
    has_leading: bool = False      # 首篇是否并入 h2 之前的正文


@dataclass
class _Token:
    kind: str                      # 'start' | 'end' | 'text' | 'tail'
    el: Optional[etree._Element] = None
    text: str = ""
    # start token：从 body 到该元素父节点的祖先链（含 body）
    chain: list = field(default_factory=list)
    # text token：文本所属父元素；tail token：tail 所属元素
    owner: Optional[etree._Element] = None


@dataclass
class _SpineItem:
    item_id: str
    href: str
    media_type: str
    properties: str
    linear: bool


# ---------------------------------------------------------------- 基础工具


def _norm(p: str) -> str:
    p = p.replace("\\", "/")
    while p.startswith("/"):
        p = p[1:]
    return posixpath.normpath(p)


def _res(base_dir: str, href: str) -> str:
    return _norm(posixpath.join(base_dir or ".", href))


def _rel(from_dir: str, target: str, frag: str = "") -> str:
    base = posixpath.relpath(target, from_dir or ".")
    return base + ("#" + frag if frag else "")


def _get_id(el: etree._Element) -> Optional[str]:
    return el.get("id", el.get(XML_ID))


def _strip_id(el: etree._Element) -> Optional[str]:
    val = _get_id(el)
    if val is not None:
        if "id" in el.attrib:
            del el.attrib["id"]
        if XML_ID in el.attrib:
            del el.attrib[XML_ID]
    return val


def _element_text(el: Optional[etree._Element]) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ====================================================================
#  读取与校验
# ====================================================================


class Book:
    def __init__(self, data: bytes):
        import zipfile

        self.raw = data
        try:
            self.zf = zipfile.ZipFile(BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise SplitError("上传的文件不是合法的 ZIP/EPUB 包。", []) from exc

        names = self.zf.namelist()
        if "mimetype" not in names:
            raise SplitError("缺少 mimetype 条目，不是 EPUB 包。", ["mimetype"])
        if self.zf.read("mimetype").strip() != EPUB_MIME.encode("ascii"):
            raise SplitError("mimetype 不是 application/epub+zip。", ["mimetype"])

        self.entries: dict[str, bytes] = {n: self.zf.read(n) for n in names if n != "mimetype"}

        self._check_encryption()
        self.container_path = self._find_opf()
        self.opf_dir = posixpath.dirname(self.container_path)
        self.opf_tree = self._parse_xml(self.container_path)
        self._parse_opf()
        self._check_epub3()

    def exists(self, name: str) -> bool:
        return _norm(name) in self.entries

    def _check_encryption(self) -> None:
        path = "META-INF/encryption.xml"
        if path not in self.entries:
            return
        try:
            root = etree.fromstring(self.entries[path], parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            raise SplitError("META-INF/encryption.xml 无法解析，文件可能已加密。", [path])
        encrypted = root.findall(".//enc:EncryptedData", namespaces=NS)
        if encrypted:
            files = []
            for ed in encrypted:
                ref = ed.find(".//enc:CipherReference", namespaces=NS)
                if ref is not None and ref.get("URI"):
                    files.append(ref.get("URI"))
            raise SplitError(
                "EPUB 含有加密条目（META-INF/encryption.xml），本工具仅接受未加密 EPUB3。",
                files or [path],
            )

    def _find_opf(self) -> str:
        container = "META-INF/container.xml"
        if container not in self.entries:
            raise SplitError("缺少 META-INF/container.xml。", [container])
        try:
            root = etree.fromstring(self.entries[container], parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            raise SplitError("META-INF/container.xml 无法解析。", [container])
        rf = root.find(".//container:rootfile", namespaces=NS)
        if rf is None or not rf.get("full-path"):
            raise SplitError("container.xml 中未找到 OPF rootfile。", [container])
        path = _norm(rf.get("full-path"))
        if not self.exists(path):
            raise SplitError(f"container.xml 指向的 OPF 不存在：{path}", [container, path])
        return path

    def _parse_xml(self, name: str) -> etree._ElementTree:
        try:
            data = self.entries[_norm(name)]
        except KeyError:
            raise SplitError(f"包内缺少文件：{name}", [name])
        try:
            return etree.parse(BytesIO(data), parser=_XML_PARSER)
        except etree.XMLSyntaxError as exc:
            raise SplitError(f"XML/XHTML 解析失败：{name}：{exc}", [name])

    # ------------------------------------------------------------------

    def _parse_opf(self) -> None:
        root = self.opf_tree.getroot()

        self.manifest: dict[str, etree._Element] = {}
        for item in root.iter():
            if _ln(item.tag) == "item" and item.get("id"):
                self.manifest[item.get("id")] = item

        spine = next((el for el in root.iter() if _ln(el.tag) == "spine"), None)
        if spine is None:
            raise SplitError("OPF 缺少 spine。", [self.container_path])
        self.spine_el = spine
        self.spine_toc = spine.get("toc")

        self.spine_items: list[_SpineItem] = []
        for ref in spine:
            if _ln(ref.tag) != "itemref":
                continue
            iid = ref.get("idref")
            item = self.manifest.get(iid)
            if item is None:
                raise SplitError(
                    f"spine 引用了不存在的 manifest item：{iid}", [self.container_path]
                )
            self.spine_items.append(
                _SpineItem(
                    item_id=iid,
                    href=_res(self.opf_dir, item.get("href", "")),
                    media_type=item.get("media-type", ""),
                    properties=(item.get("properties") or ""),
                    linear=ref.get("linear", "yes") != "no",
                )
            )
        if not self.spine_items:
            raise SplitError("spine 中没有任何正文。", [self.container_path])

        self.nav_item = self.nav_href = None
        for item in self.manifest.values():
            if NAV_PROP in (item.get("properties") or "").split():
                self.nav_item = item
                self.nav_href = _res(self.opf_dir, item.get("href", ""))
                break

        self.ncx_item = self.ncx_href = None
        if self.spine_toc and self.spine_toc in self.manifest:
            item = self.manifest[self.spine_toc]
            if item.get("media-type") == NCX_MIME:
                self.ncx_item = item
                self.ncx_href = _res(self.opf_dir, item.get("href", ""))
        if self.ncx_item is None:
            for item in self.manifest.values():
                if item.get("media-type") == NCX_MIME:
                    self.ncx_item = item
                    self.ncx_href = _res(self.opf_dir, item.get("href", ""))
                    break

    def _check_epub3(self) -> None:
        version = self.opf_tree.getroot().get("version") or ""
        if not version.startswith("3"):
            raise SplitError(
                f"仅支持 EPUB3（当前 OPF version={version or '未知'}）。",
                [self.container_path],
            )

    def list_spine_xhtml(self) -> list[dict]:
        out = []
        for it in self.spine_items:
            if it.media_type and it.media_type not in (XHTML_MIME, "text/html"):
                continue
            if not self.exists(it.href):
                continue
            title = ""
            try:
                tree = self._parse_xml(it.href)
                t = tree.find(".//xhtml:title", namespaces=NS)
                title = _element_text(t)
                if not title:
                    title = _element_text(tree.find(".//xhtml:h1", namespaces=NS))
            except SplitError:
                pass
            out.append(
                {
                    "item_id": it.item_id,
                    "path": it.href,
                    "linear": it.linear,
                    "title": title or posixpath.basename(it.href),
                }
            )
        return out


# ====================================================================
#  切点分析（事件流）
# ====================================================================


def _tokenize(body: etree._Element) -> list[_Token]:
    """把 body 内容线性化为 start/end/text/tail 事件。

    事件序列可直接在“新 body”上回放：
    text 事件 -> 当前栈顶的 .text；tail 事件 -> 刚闭合元素的 .tail。
    """
    tokens: list[_Token] = []

    def walk(el: etree._Element, chain: list) -> None:
        if el.text:
            tokens.append(_Token("text", owner=el, text=el.text))
        for child in el:
            tokens.append(_Token("start", el=child, chain=chain + [el]))
            walk(child, chain + [el])
            tokens.append(_Token("end", el=child))
            if child.tail:
                tokens.append(_Token("tail", el=child, owner=el, text=child.tail))

    walk(body, [])
    return tokens


def analyze_split_points(body: etree._Element, path: str) -> list[dict]:
    tokens = _tokenize(body)
    starts = [
        i for i, t in enumerate(tokens)
        if t.kind == "start" and _ln(t.el.tag) == "h2"
    ]
    if not starts:
        raise SplitError(f"所选文件没有任何 h2 切点：{path}", [path])

    points: list[dict] = []
    for n, si in enumerate(starts):
        h2 = tokens[si].el
        point = {
            "n": n,
            "start_index": si,
            "boundary": 0 if n == 0 else si,
            "pulled": [],
            "h2": h2,
            "title": _element_text(h2) or f"第 {n + 1} 节",
            "id": _get_id(h2),
        }
        if n > 0:
            # 把 h2 之前“一路无正文内容”的外层容器 start 拉进本篇，
            # 这样以 h2 开篇的容器 id 落在本篇而不是上一篇。
            # 判定：[lo, si) 中的 start 必须恰为 h2 祖先链的一段，
            # text/tail 仅允许纯空白（XML 缩进）；任何正文或元素结束都停止回拉。
            limit = starts[n - 1]
            ancestors = list(tokens[si].chain)  # body, ..., h2 的父节点
            lo = si - 1
            start_depth = 0
            while lo > limit:
                tk = tokens[lo]
                if tk.kind == "start":
                    start_depth += 1
                    if start_depth > len(ancestors):
                        break
                    if tk.el is not ancestors[len(ancestors) - start_depth]:
                        break
                    point["pulled"].append(tk.el)
                    lo -= 1
                elif tk.kind in ("text", "tail") and not tk.text.strip():
                    lo -= 1
                else:
                    break
            point["boundary"] = lo + 1
            point["pulled"].reverse()
        points.append(point)
    return points


# ====================================================================
#  拆分执行
# ====================================================================


class Splitter:
    def __init__(self, book: Book, source_path: str):
        self.book = book
        self.source_path = _norm(source_path)
        self.source_dir = posixpath.dirname(self.source_path)
        self.src_tree = book._parse_xml(self.source_path)
        self.src_body = self.src_tree.find(".//xhtml:body", namespaces=NS)
        if self.src_body is None:
            raise SplitError(f"文档缺少 body：{self.source_path}", [self.source_path])

        self.tokens = _tokenize(self.src_body)
        self.points = analyze_split_points(self.src_body, self.source_path)

        self.segments: list[Segment] = []
        self.seg_names: set[str] = set()
        # 原 id -> (落在哪篇, 输出片段(可能为 None 表示文档起点))
        self.id_map: dict[str, tuple[int, Optional[str]]] = {}
        self.body_id = _get_id(self.src_body)
        self.warnings: list[str] = []

        self._spine_entry = next(
            (s for s in book.spine_items if s.href == self.source_path), None
        )
        if self._spine_entry is None:
            raise SplitError(
                f"所选文件不在 spine 中：{self.source_path}", [self.source_path]
            )

    # ------------------------------------------------------------------
    #  命名
    # ------------------------------------------------------------------

    def _plan_names(self) -> None:
        existing = set(self.book.entries)
        stem_dot = self.source_path.rsplit(".", 1)
        if len(stem_dot) == 2 and "." in posixpath.basename(self.source_path):
            stem, ext = stem_dot[0], "." + stem_dot[1]
        else:
            stem, ext = self.source_path, ".xhtml"

        # 首篇沿用源文件名（复用原 manifest 条目，旧的无片段链接天然有效）；
        # 其余篇使用 -partNN 后缀，从 part02 起。
        names: list[str] = [self.source_path]
        k = 1
        while len(names) < len(self.points):
            k += 1
            cand = _norm(f"{stem}-part{k:02d}{ext}")
            if cand not in existing and cand not in names:
                names.append(cand)

        existing_ids = set(self.book.manifest)
        base_item = self._spine_entry.item_id
        item_ids = [base_item]
        k = 1
        while len(item_ids) < len(self.points):
            k += 1
            cand = f"{base_item}-part{k:02d}"
            if cand not in existing_ids and cand not in item_ids:
                item_ids.append(cand)

        self._names = names
        self._item_ids = item_ids
        self.seg_names = set(names)

    # ------------------------------------------------------------------
    #  h2 id
    # ------------------------------------------------------------------

    def _ensure_h2_ids(self) -> None:
        used = {_get_id(e) for e in self.src_tree.iter() if _get_id(e)}
        k = 0
        for p in self.points:
            if p["id"]:
                continue
            while True:
                k += 1
                cand = f"sec-{k}"
                if cand not in used:
                    break
            used.add(cand)
            p["h2"].set("id", cand)
            p["id"] = cand

    # ------------------------------------------------------------------
    #  构建单篇
    # ------------------------------------------------------------------

    def _build_tree(self, n: int) -> etree._ElementTree:
        point = self.points[n]
        boundary = point["boundary"]
        end = self.points[n + 1]["boundary"] if n + 1 < len(self.points) else len(self.tokens)

        tree = copy.deepcopy(self.src_tree)
        body = tree.find(".//xhtml:body", namespaces=NS)
        for child in list(body):
            body.remove(child)

        # 后续篇：在 body 内“重新打开”跨越切点的祖先容器（剥离 id），
        # 但紧邻 h2 开篇、已被拉进本篇的容器除外（它们在事件切片内，保留 id）。
        # orig_el -> clone，用于把文本事件准确归位。
        stack: list[etree._Element] = [body]
        orig_of: dict = {}
        if n > 0:
            chain = self.tokens[point["start_index"]].chain
            reopen = list(chain[1:])  # 去掉 body：[外层..., 父节点]
            if point["pulled"]:
                # pulled 是最内层的若干祖先（已进入切片、保留 id）；
                # reopen 只保留 pulled 之前（更外层）的祖先
                first_pulled = point["pulled"][0]
                for i, e in enumerate(reopen):
                    if e is first_pulled:
                        reopen = reopen[:i]
                        break
            for orig in reopen:
                clone = copy.deepcopy(orig)
                for c in list(clone):
                    clone.remove(c)
                clone.text = None
                _strip_id(clone)
                stack[-1].append(clone)
                stack.append(clone)
                orig_of[orig] = clone

        def _clone_of(orig: etree._Element) -> etree._Element:
            return orig_of[orig]

        for tok in self.tokens[boundary:end]:
            if tok.kind == "start":
                clone = etree.Element(tok.el.tag)
                for k, v in tok.el.attrib.items():
                    clone.set(k, v)
                stack[-1].append(clone)
                stack.append(clone)
                orig_of[tok.el] = clone
            elif tok.kind == "end":
                if len(stack) > 1:
                    stack.pop()
            elif tok.kind == "text":
                target = orig_of.get(tok.owner, stack[-1])
                target.text = (target.text or "") + tok.text
            else:  # tail：tok.el 的尾随文本，父节点为 tok.owner
                child_clone = orig_of.get(tok.el)
                parent_clone = orig_of.get(tok.owner, stack[-1])
                if child_clone is not None and child_clone.getparent() is parent_clone:
                    child_clone.tail = (child_clone.tail or "") + tok.text
                else:
                    # 兄弟元素位于切片外：空白降级为父节点起首文本，非空白追加到末尾
                    if len(parent_clone) == 0:
                        parent_clone.text = (parent_clone.text or "") + tok.text
                    else:
                        parent_clone[-1].tail = (parent_clone[-1].tail or "") + tok.text
        return tree

    # ------------------------------------------------------------------

    def _has_leading(self) -> bool:
        for tok in self.tokens[: self.points[0]["start_index"]]:
            if tok.kind in ("text", "tail") and tok.text.strip():
                return True
        return False

    def _update_title(self, seg: Segment) -> None:
        title_el = seg.doc.find(".//xhtml:title", namespaces=NS)
        if title_el is None:
            head = seg.doc.find(".//xhtml:head", namespaces=NS)
            if head is None:
                return
            title_el = etree.SubElement(head, XHTML + "title")
        if seg.index == 0 and seg.has_leading:
            src_title = self.src_tree.find(".//xhtml:title", namespaces=NS)
            base = _element_text(src_title)
            title_el.text = f"{base} · {seg.title}" if base else seg.title
        else:
            title_el.text = seg.title

    def _build_segments(self) -> None:
        self._ensure_h2_ids()
        self._plan_names()
        leading = self._has_leading()
        for n, p in enumerate(self.points):
            seg = Segment(
                index=n,
                name=self._names[n],
                title=p["title"],
                h2_id=p["id"],
                doc=self._build_tree(n),
                item_id=self._item_ids[n],
                has_leading=(n == 0 and leading),
            )
            self._update_title(seg)
            self.segments.append(seg)

        # body id 只在首篇保留；后续篇剥离，指向它的链接已重定向到首篇文档起点
        for seg in self.segments[1:]:
            new_body = seg.doc.find(".//xhtml:body", namespaces=NS)
            if new_body is not None:
                _strip_id(new_body)

        self._collect_ids()
        self._validate_ids()

    # ------------------------------------------------------------------
    #  id 映射与校验
    # ------------------------------------------------------------------

    def _collect_ids(self) -> None:
        for seg in self.segments:
            for el in seg.doc.getroot().iter():
                i = _get_id(el)
                if not i or i in self.id_map:
                    continue
                if _ln(el.tag) == "body":
                    self.id_map[i] = (seg.index, None)
                else:
                    self.id_map[i] = (seg.index, i)

    def _validate_ids(self) -> None:
        problems = []
        for seg in self.segments:
            seen: dict[str, int] = {}
            for el in seg.doc.getroot().iter():
                i = _get_id(el)
                if i:
                    seen[i] = seen.get(i, 0) + 1
            dups = sorted(i for i, c in seen.items() if c > 1)
            if dups:
                problems.append(f"{seg.name} 篇内 id 重复：{', '.join(dups[:10])}")
        if problems:
            raise SplitError(
                "拆分后出现篇内 id 重复，已拒绝下载：" + "；".join(problems),
                [self.source_path],
            )

        seen: dict[str, int] = {}
        for el in self.src_tree.getroot().iter():
            i = _get_id(el)
            if i:
                seen[i] = seen.get(i, 0) + 1
        dups = sorted(i for i, c in seen.items() if c > 1)
        if dups:
            raise SplitError(
                f"源文件 {self.source_path} 存在篇内重复 id（{', '.join(dups[:10])}），"
                "内链重定向会产生歧义，已拒绝下载。",
                [self.source_path],
            )

    # ------------------------------------------------------------------
    #  链接解析与重写
    # ------------------------------------------------------------------

    def _resolve(self, source_doc: str, href: str, base_doc: Optional[str] = None):
        """返回 (kind, seg_index, frag)：kind ∈ split/keep/dangling/external

        href 按其在 base_doc（默认即 source_doc）目录下的相对关系解析。
        """
        h = (href or "").strip()
        if not h:
            return ("keep", None, None)
        source_doc = _norm(source_doc)
        base_doc = _norm(base_doc or source_doc)

        if h.startswith("#"):
            if base_doc in self.seg_names:
                frag = h[1:]
                if not frag:
                    return ("split", 0, None)
                if frag == self.body_id:
                    return ("split", 0, None)
                if frag not in self.id_map:
                    return ("dangling", None, None)
                seg_i, out_frag = self.id_map[frag]
                return ("split", seg_i, out_frag)
            return ("keep", None, None)  # 其他文件内纯片段链接不受拆分影响

        bare, frag = urldefrag(h)
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", bare) or bare.startswith("//"):
            return ("external", None, None)

        target_file = _norm(posixpath.join(posixpath.dirname(base_doc), unquote(bare)))
        if target_file != self.source_path:
            return ("keep", None, None)

        if not frag:
            return ("split", 0, None)
        if frag == self.body_id:
            return ("split", 0, None)
        if frag not in self.id_map:
            return ("dangling", None, None)
        seg_i, out_frag = self.id_map[frag]
        return ("split", seg_i, out_frag)

    def _rewrite_href(self, source_doc: str, href: str, errors: list[str]) -> str:
        kind, seg_i, frag = self._resolve(source_doc, href)
        if kind in ("keep", "external"):
            return href
        if kind == "dangling":
            errors.append(f"{_norm(source_doc)}：悬空内链 “{href}”（目标 id 不存在）")
            return href
        seg = self.segments[seg_i]
        if _norm(source_doc) == seg.name:
            return "#" + frag if frag else "#"
        return _rel(posixpath.dirname(_norm(source_doc)), seg.name, frag or "")

    def _href_nodes(self, root: etree._Element):
        for el in root.iter():
            ln = _ln(el.tag)
            if ln in ("a", "area") and el.get("href") is not None:
                yield el, "href"
            elif ln == "a" and el.tag.startswith(SVG) and el.get(XLINK_HREF) is not None:
                yield el, XLINK_HREF

    def rewrite_document(self, path: str, tree: etree._ElementTree, errors: list[str]) -> bool:
        changed = False
        for el, attr in self._href_nodes(tree.getroot()):
            old = el.get(attr)
            new = self._rewrite_href(path, old, errors)
            if new != old:
                el.set(attr, new)
                changed = True
        return changed

    # ------------------------------------------------------------------
    #  OPF
    # ------------------------------------------------------------------

    def update_opf(self, errors: list[str]) -> None:
        root = self.book.opf_tree.getroot()
        manifest = next(el for el in root.iter() if _ln(el.tag) == "manifest")

        orig_item = self.book.manifest[self._spine_entry.item_id]
        pos = list(manifest).index(orig_item)
        props_all = set((orig_item.get("properties") or "").split())
        href_dir = self.book.opf_dir

        manifest.remove(orig_item)
        for off, seg in enumerate(self.segments):
            if seg.index == 0:
                el = orig_item
            else:
                el = etree.Element(OPF + "item")
            el.set("id", seg.item_id)
            el.set("href", posixpath.relpath(seg.name, href_dir))
            el.set("media-type", XHTML_MIME)
            props = props_all - {NAV_PROP}
            if seg.index == 0:
                props |= props_all & {NAV_PROP}
            if props:
                el.set("properties", " ".join(sorted(props)))
            manifest.insert(pos + off, el)

        spine = self.book.spine_el
        refs = [c for c in spine if _ln(c.tag) == "itemref"]
        orig_ref = next(r for r in refs if r.get("idref") == self._spine_entry.item_id)
        rpos = list(spine).index(orig_ref)
        attrs = dict(orig_ref.attrib)
        spine.remove(orig_ref)
        for off, seg in enumerate(self.segments):
            r = etree.Element(OPF + "itemref")
            for k, v in attrs.items():
                r.set(k, v)
            r.set("idref", seg.item_id)
            spine.insert(rpos + off, r)

        # EPUB2 guide（若存在）
        for ref in root.iter():
            if _ln(ref.tag) == "reference" and ref.get("href"):
                ref.set("href", self._rewrite_href(self.book.container_path, ref.get("href"), errors))

        self._bump_modified(root)

    def _bump_modified(self, root: etree._Element) -> None:
        meta = next((el for el in root.iter() if _ln(el.tag) == "metadata"), None)
        if meta is None:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # EPUB3 标准形式：<meta property="dcterms:modified">
        modified = next(
            (
                m
                for m in meta
                if _ln(m.tag) == "meta" and m.get("property") == "dcterms:modified"
            ),
            None,
        )
        if modified is None:
            # 兜底：dcterms:modified 元素形式
            modified = next(
                (m for m in meta if m.tag == "{%s}modified" % NS["dcterms"]), None
            )
        if modified is None:
            modified = etree.SubElement(meta, OPF + "meta")
            modified.set("property", "dcterms:modified")
        modified.text = now

    # ------------------------------------------------------------------
    #  导航文档 / NCX
    # ------------------------------------------------------------------

    def _nav_target_is_bare_source(self, nav_path: str, href: str) -> bool:
        kind, _, _ = self._resolve(nav_path, href)
        return kind == "split" and "#" not in href

    def _expand_nav(self, tree: etree._ElementTree, nav_path: str) -> bool:
        root = tree.getroot()
        changed = False
        for nav in root.findall(".//xhtml:nav", namespaces=NS):
            role = nav.get("{%s}type" % NS["opf"], "")
            if role and "toc" not in role.split():
                continue
            ol = nav.find("xhtml:ol", namespaces=NS)
            if ol is None:
                continue
            # 快照：展开时会修改 ol 的子节点序列
            for li in list(ol.findall("xhtml:li", namespaces=NS)):
                a = li.find("xhtml:a", namespaces=NS)
                if a is None or not a.get("href"):
                    continue
                if not self._nav_target_is_bare_source(nav_path, a.get("href")):
                    continue
                pos = list(ol).index(li)
                ol.remove(li)
                for off, seg in enumerate(self.segments):
                    new_li = etree.Element(XHTML + "li")
                    new_a = etree.SubElement(new_li, XHTML + "a")
                    href = _rel(posixpath.dirname(nav_path), seg.name, seg.h2_id)
                    new_a.set("href", href)
                    new_a.text = seg.title
                    ol.insert(pos + off, new_li)
                changed = True
        return changed

    def _expand_ncx(self, tree: etree._ElementTree, ncx_path: str) -> None:
        root = tree.getroot()
        nav_map = root.find(".//ncx:navMap", namespaces=NS)
        if nav_map is None:
            return
        for np in list(nav_map.findall("ncx:navPoint", namespaces=NS)):
            content = np.find("ncx:content", namespaces=NS)
            if content is None or not content.get("src"):
                continue
            src = content.get("src")
            if "#" in src or not self._nav_target_is_bare_source(ncx_path, src):
                continue
            pos = list(nav_map).index(np)
            base_id = np.get("id", "navpoint")
            nav_map.remove(np)
            for off, seg in enumerate(self.segments):
                node = etree.Element(NCX + "navPoint")
                node.set("id", f"{base_id}-part{off + 1}")
                label = etree.SubElement(node, NCX + "navLabel")
                text = etree.SubElement(label, NCX + "text")
                text.text = seg.title
                c = etree.SubElement(node, NCX + "content")
                c.set("src", _rel(posixpath.dirname(ncx_path), seg.name, seg.h2_id))
                nav_map.insert(pos + off, node)

        order = 1
        for el in root.iter():
            if _ln(el.tag) in ("navPoint", "pageTarget") and el.get("playOrder") is not None:
                el.set("playOrder", str(order))
                order += 1

    # ------------------------------------------------------------------
    #  总流程
    # ------------------------------------------------------------------

    def run(self) -> dict:
        self._build_segments()

        changed: dict[str, etree._ElementTree] = {}
        errors: list[str] = []

        # 1) 各新篇内链
        for seg in self.segments:
            if self.rewrite_document(seg.name, seg.doc, errors):
                pass
            changed[seg.name] = seg.doc

        # 2) 包内其余 XHTML/HTML
        for name in sorted(self.book.entries):
            n = _norm(name)
            if n in changed or n == self.source_path or n in self.seg_names:
                continue
            if not self._looks_xhtml(n):
                continue
            try:
                tree = self.book._parse_xml(n)
            except SplitError:
                self.warnings.append(f"{n} 不是合法 XML，已跳过其中的链接重写。")
                continue
            if self.rewrite_document(n, tree, errors):
                changed[n] = tree

        # 3) 导航文档：先展开章节条目，再统一重写
        if self.book.nav_href:
            np = self.book.nav_href
            tree = changed.get(np)
            if tree is None:
                try:
                    tree = self.book._parse_xml(np)
                except SplitError:
                    tree = None
            if tree is not None:
                self._expand_nav(tree, np)
                self.rewrite_document(np, tree, errors)
                changed[np] = tree

        # 4) NCX
        if self.book.ncx_href:
            cp = self.book.ncx_href
            tree = changed.get(cp)
            if tree is None:
                try:
                    tree = self.book._parse_xml(cp)
                except SplitError:
                    tree = None
            if tree is not None:
                self._expand_ncx(tree, cp)
                for content in tree.getroot().iter():
                    if _ln(content.tag) == "content" and content.get("src"):
                        content.set("src", self._rewrite_href(cp, content.get("src"), errors))
                changed[cp] = tree

        if errors:
            files = sorted({e.split("：", 1)[0] for e in errors})
            raise SplitError(
                "拆分后出现悬空内链，已拒绝下载：" + "；".join(errors[:20]), files
            )

        self.update_opf(errors)
        if errors:
            raise SplitError(
                "OPF guide 中存在悬空内链，已拒绝下载：" + "；".join(errors[:20]),
                [self.book.container_path],
            )
        changed[self.book.container_path] = self.book.opf_tree

        return {"segments": self.segments, "changed": changed, "warnings": self.warnings}

    def _looks_xhtml(self, name: str) -> bool:
        lower = name.lower()
        if lower.endswith((".xhtml", ".xht", ".xml", ".html", ".htm")):
            return True
        for item in self.book.manifest.values():
            if _res(self.book.opf_dir, item.get("href", "")) == _norm(name):
                return item.get("media-type") in (
                    XHTML_MIME, "text/xml", "application/xml", "text/html"
                )
        return False


# ====================================================================
#  预览
# ====================================================================


def preview(book: Book, source_path: str) -> dict:
    source_path = _norm(source_path)
    if not any(s.href == source_path for s in book.spine_items):
        raise SplitError(f"所选文件不在 spine 中：{source_path}", [source_path])
    tree = book._parse_xml(source_path)
    body = tree.find(".//xhtml:body", namespaces=NS)
    if body is None:
        raise SplitError(f"文档缺少 body：{source_path}", [source_path])
    points = analyze_split_points(body, source_path)

    leading = any(
        t.kind in ("text", "tail") and t.text.strip()
        for t in _tokenize(body)[: points[0]["start_index"]]
    )
    return {
        "source_path": source_path,
        "count": len(points),
        "leading_in_first": leading,
        "segments": [
            {"index": p["n"], "title": p["title"], "h2_id": p["id"]}
            for p in points
        ],
    }


# ====================================================================
#  序列化与打包
# ====================================================================


def serialize(tree: etree._ElementTree) -> bytes:
    # etree.tostring 对 ElementTree 已会写出 DOCTYPE，只需补 XML 声明
    decl = b'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
    body = etree.tostring(tree, xml_declaration=False, encoding="utf-8")
    if not body.lstrip().startswith(b"<!DOCTYPE"):
        doctype = tree.docinfo.doctype
        if doctype:
            return decl + doctype.encode("utf-8") + b"\n" + body
    return decl + body


def build_epub(book: Book, result: dict, source_path: str) -> bytes:
    import zipfile

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zout.writestr(info, EPUB_MIME)

        source_path = _norm(source_path)
        seg_names = {s.name for s in result["segments"]}

        # 1) 原有条目：源文件（首篇同名）由 changed 中的新内容覆盖
        for name in sorted(book.entries):
            n = _norm(name)
            if n in result["changed"]:
                data = serialize(result["changed"][n])
            elif n in seg_names - {source_path}:
                # 理论上不会发生（新篇都在 changed），保险跳过
                continue
            else:
                data = book.entries[name]
            zout.writestr(n, data)

        # 2) 源文件以外的新建篇
        for seg in result["segments"]:
            if seg.name not in {_norm(x) for x in book.entries}:
                zout.writestr(seg.name, serialize(seg.doc))

    return buf.getvalue()
