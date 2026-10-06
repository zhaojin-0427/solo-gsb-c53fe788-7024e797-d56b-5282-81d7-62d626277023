# EPUB3 章节拆分台

用 Python + FastAPI + lxml + 原生 JavaScript 实现的 EPUB3 章节拆分工具。上传一本**未加密的 EPUB3**，在 spine 正文中选一篇，系统按文中每个 `h2` 切点把它拆成多篇，可在线逐篇预览，并一键下载拆分后的新 EPUB。

## 功能与拆分规则

- 以所选 spine 正文中的**每个 `h2` 为切点**，每个 `h2` 开启一篇新章节。
- **首个 `h2` 之前的正文**（引导段、引言等）自动并入第 1 篇。
- **嵌套 `section` 等容器允许跨切点**：切点处祖先容器会在新篇中被“重新打开”，保证拆后 XHTML 结构合法（不会出现未闭合标签或断裂的列表/表格等），样式与阅读顺序保持不变。
- 跨切点容器的 `id` 只保留在它实际出现的第一篇，其余各篇剥离该包装层的 `id`；指向该容器的链接仍指向**原容器起点**（自动重定向到对应篇）。
- 同步更新：
  - OPF 的 **manifest 与 spine**（新篇逐个登记，阅读顺序与原书一致）；
  - EPUB3 **导航文档**（`nav`，TOC 中原章节条目展开为各篇条目，其余链接重写）；
  - 存在时的 **NCX**（EPUB2 兼容目录，同步展开/重写并整理 `playOrder`）；
  - OPF `guide` 引用与 `dcterms:modified` 元数据。
- **所有 XHTML 内链按原目标文件与 id 重定向**，包括：
  - 脚注/尾注的往返链接；
  - 仅含片段（`#id`）的链接；
  - 相对路径链接（`../text/xxx.xhtml#note1`）；
  - SVG 内 `xlink:href`、`area@href`。
- 新篇与源文件位于同一目录，**图片与 CSS 等相对引用保持可用**。
- 其他章节可以存在与拆分文件同名的 `id`，二者互不影响。
- 预览页中图片/CSS 通过后端代理原书资源加载，跨篇内链可直接点击在各篇之间跳转（含脚注往返）。

## 拒绝下载的情形

出现以下任一情况时，服务端会指出问题所在文件并**拒绝生成新书**：

1. 所选正文 **没有任何 `h2`**；
2. EPUB **已加密**（存在 `META-INF/encryption.xml` 的 `EncryptedData` 条目，含字体混淆）；
3. 所选正文**篇内 id 重复**（会导致链接重定向歧义）；
4. 拆分后出现**悬空内链**（链接指向不存在的 id/文件）——错误信息会列出具体文件与链接。

另外，非 EPUB3（OPF `version` 非 3.x）、损坏的 ZIP/OPF/XHTML 也会在校验阶段被拒绝并指明文件。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后浏览器访问：

- 拆分台首页：<http://localhost:8000/>
- （FastAPI 自动接口文档：<http://localhost:8000/docs>）

上传的 EPUB 保存在 Docker 卷 `epub-data`（容器内 `/data`）中。停止服务：

```bash
docker compose down
```

## 本地直接运行（不用 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 使用步骤

1. 打开 <http://localhost:8000/>，在“1 · 上传 EPUB3”处选择一本未加密 `.epub`，点**上传**。校验失败（加密、非 EPUB3、XML 损坏等）时会直接显示原因及相关文件。
2. 在“2 · 选择 spine 正文”列表中选一篇想拆分的正文，点**分析 h2 切点**。没有 `h2` 时会提示并拒绝继续。
3. 在“3 · 逐篇预览”中用 **上一篇 / 下一篇** 翻看每一篇拆分结果（iframe 以 `application/xhtml+xml` 渲染真实 XHTML）：
   - 页面会标出每篇标题与 `h2` 的 id；
   - 图片、CSS 正常加载；脚注等跨篇链接可点击跳到对应预览篇。
4. 点**下载拆分后的新书 (.epub)**，浏览器会下载 `<原文件名>-split.epub`。该文件 mimetype 为首条且不压缩，可直接在 Apple Books、Thorium、calibre 等 EPUB3 阅读器中打开。若存在悬空内链或篇内 id 重复，页面会列出问题文件，且不会产出下载文件。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/upload` | 上传 EPUB（multipart 字段 `file`），返回 spine 正文清单 |
| `GET` | `/api/preview?book_id=&path=` | 返回 h2 切点数量、各篇标题/id、首篇是否含前置正文 |
| `GET` | `/api/segment?book_id=&path=&index=` | 返回拆分后第 N 篇的 XHTML（预览） |
| `GET` | `/api/raw/{book_id}/{path}` | 代理原书包内资源（预览时图片/CSS 用） |
| `POST` | `/api/split` | 表单 `book_id`、`path`，返回新 EPUB 文件流 |

## 目录结构

```
app/
  main.py        # FastAPI 路由：上传 / 预览 / 资源代理 / 下载
  splitter.py    # EPUB 解析、切点分析、拆分、链接重定向、OPF/NAV/NCX 更新、打包
static/
  index.html     # 单页界面
  app.js         # 原生 JavaScript 交互
  style.css
Dockerfile
docker-compose.yml
requirements.txt
```
