# EPUB3 章节拆分台

上传一本**未加密的 EPUB3**，在 spine 正文中挑选一篇，以该文件中的每个
`<h2>` 为切点，把长章节拆成若干独立篇章：可逐篇在线预览样式与内容，确认
无误后一键下载新书。

- 后端：Python + FastAPI + lxml（纯 XML 事件展开/重放，不依赖 HTML 解析器）
- 前端：原生 HTML / CSS / JavaScript，无构建步骤、无前端框架
- 部署：Docker Compose 一键启动

## 功能与拆分规则

1. **切点与内容归属**
   - 以所选 spine 正文中的每个 `<h2>` 作为切点，每个切点开启一篇新书章节。
   - **第一个 `<h2>` 之前的全部正文并入第一篇**（导语、封面图等不丢失）。
   - 允许**嵌套 `<section>` 跨越切点**：切点上仍未闭合的祖先容器会在后续
     篇章中以“只保留开标签属性”的方式重新打开并在篇末闭合，保证每篇
     XHTML 都是良构、合法的。
   - 每篇沿用原文 `<head>`（含 CSS 引用），阅读顺序与样式保持不变。
2. **包结构同步**
   - 更新 OPF 的 `manifest`（新增各篇条目、删除原条目）与 `spine`（在原
     位置按切分顺序展开，保留原 `itemref` 属性，阅读顺序不变）。
   - 同步 EPUB3 导航文档（`properties="nav"`）：目录中的原章节点展开为
     各篇入口，小节/page-list/landmarks 等带片段的链接按锚点重定向。
   - 存在 NCX（`application/x-dtbncx+xml`）时同步 `navMap` 与 `content/@src`。
   - 新书 `mimetype` 仍为 ZIP 首项且不压缩，可在标准 EPUB3 阅读器中打开。
3. **内链全部按“原目标文件 + id”重定向**
   - 脚注往返（`noteref` 去、`backlink` 回）、仅片段链接（`#id`）、相对
     路径链接（`chapter.xhtml#id`）都指向拆分后的正确篇章。
   - **其他章节允许存在同名 id**：重定向只依据原目标文件，不做跨文件
     id 匹配。
   - **跨切点容器 id 的链接仍指向原容器起点**：该 id 只保留在真正包含其
     开标签的篇章，后续篇章注入的容器外壳不带该 id。
   - 图片、CSS、SVG 图片、字体等资源引用原样保留并校验文件存在。
4. **拒绝下载的情况**（页面会列出问题及所在文件）
   - 所选正文中没有任何 `<h2>`；
   - EPUB 已加密（`META-INF/encryption.xml` 中存在加密条目）；
   - 任一 XHTML **篇内 id 重复**（跨文件同名 id 允许）；
   - 拆分后出现**悬空内链**（目标文件或目标 id 不存在），或图片/CSS 等
     资源引用缺失。

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

启动后浏览器访问：

- 拆分台主页（上传 / 选章 / 预览 / 下载）：<http://localhost:8000/>
- 健康检查：<http://localhost:8000/healthz>

停止：

```bash
docker compose down
```

## 本地运行（不使用 Docker）

需要 Python 3.11+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

然后同样访问 <http://localhost:8000/>。

## 使用步骤

1. 打开 <http://localhost:8000/>，把 `.epub` 文件拖入上传区（或点击选择）。
   上传成功后会提示是否检测到 EPUB3 导航 / NCX / 加密条目。
2. 在“选择 spine 正文”下拉框中选中要拆分的章节文件。
3. 点击“以每个 h2 为切点预览”。左侧列出拆分后的各篇标题（文件名由标题
   自动生成），右侧 iframe 按原书样式渲染当前篇；点击左侧条目或使用
   “上一篇 / 下一篇”切换预览。预览页内的脚注、跨篇链接、图片均可直接
   点击查看。
4. 校验通过后点击“下载拆分后的 EPUB”，浏览器即下载
   `<原书名>-<篇标题…>.epub`。若存在错误，下载按钮会禁用，错误列表会标注
   **问题类型与所在 EPUB 内部文件路径**，不会产出坏书。

> 上传的文件只保存在服务进程内存中，闲置 1 小时自动清理；不会写入磁盘。

## HTTP 接口（供二次开发）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/upload` | multipart 上传 EPUB，返回 `session_id` |
| GET | `/api/spine?session_id=` | 列出 spine 正文、加密条目、nav/NCX 情况 |
| POST | `/api/plan` | 表单 `session_id`、`href`，返回切分篇章与全部校验错误 |
| GET | `/api/preview?session_id=&href=&index=` | 渲染第 `index` 篇的预览 HTML |
| GET | `/api/file/{sid}/root/{path}` | 预览页资源（CSS/图片）与拆分后篇章 |
| GET | `/api/download?session_id=&href=` | 下载新 EPUB；校验不通过返回 422 及错误明细 |

## 运行测试

```bash
pip install pytest   # 测试本身只依赖标准库断言，也可直接运行
PYTHONPATH=. python3 tests/test_split.py
# 或
PYTHONPATH=. python3 -m pytest tests/ -q
```

测试夹具在内存中构造带嵌套 section、脚注往返、同名 id、CSS、图片、nav、
NCX 的 EPUB3，覆盖正常拆分与各类拒绝场景。

## 目录结构

```
app/
  epub_splitter.py   # EPUB 解析、切分、链接重定向、OPF/nav/NCX 同步、打包
  sessions.py        # 上传会话（内存 + TTL）
  main.py            # FastAPI 路由
  static/index.html  # 原生 JS 前端
tests/
  make_epub.py       # 内存 EPUB 夹具
  test_split.py      # 核心逻辑测试
Dockerfile
docker-compose.yml
requirements.txt
```
