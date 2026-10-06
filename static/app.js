"use strict";

const state = {
  bookId: null,
  sourcePath: null,
  segments: [],
  current: 0,
};

const el = (id) => document.getElementById(id);

function setInfo(node, text, kind) {
  node.textContent = text || "";
  node.className = "info" + (kind ? " " + kind : "");
}

function showError(detail) {
  const box = el("errors");
  box.classList.remove("hidden");
  if (detail && typeof detail === "object") {
    el("errorMsg").textContent = detail.message || "处理失败。";
    const ul = el("errorFiles");
    ul.innerHTML = "";
    (detail.files || []).forEach((f) => {
      const li = document.createElement("li");
      li.textContent = f;
      ul.appendChild(li);
    });
  } else {
    el("errorMsg").textContent = detail || "处理失败。";
    el("errorFiles").innerHTML = "";
  }
  box.scrollIntoView({ behavior: "smooth" });
}

function clearError() {
  el("errors").classList.add("hidden");
}

// ------------------------------------------------------------ 上传

el("uploadBtn").addEventListener("click", async () => {
  const input = el("file");
  if (!input.files.length) {
    setInfo(el("uploadInfo"), "请先选择一个 .epub 文件。", "bad");
    return;
  }
  clearError();
  setInfo(el("uploadInfo"), "上传并校验中…");
  const fd = new FormData();
  fd.append("file", input.files[0]);
  try {
    const resp = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) {
      throw data.detail || data;
    }
    state.bookId = data.book_id;
    setInfo(
      el("uploadInfo"),
      `上传成功：${data.spine.length} 篇 spine 正文（OPF：${data.opf}）`,
      "ok"
    );
    renderSpine(data.spine);
    el("step2").classList.remove("hidden");
  } catch (err) {
    setInfo(el("uploadInfo"), "", "");
    showError(err);
  }
});

function renderSpine(items) {
  const sel = el("spineSelect");
  sel.innerHTML = "";
  items.forEach((it) => {
    const opt = document.createElement("option");
    opt.value = it.path;
    opt.textContent = `${it.title}  —  ${it.path}${it.linear ? "" : " (non-linear)"}`;
    sel.appendChild(opt);
  });
}

// ------------------------------------------------------------ 切点分析

el("previewBtn").addEventListener("click", async () => {
  const path = el("spineSelect").value;
  if (!path) return;
  clearError();
  state.sourcePath = path;
  setInfo(el("previewInfo"), "分析 h2 切点中…");
  try {
    const qs = new URLSearchParams({ book_id: state.bookId, path });
    const resp = await fetch("/api/preview?" + qs.toString());
    const data = await resp.json();
    if (!resp.ok) throw data.detail || data;

    state.segments = data.segments;
    state.current = 0;
    setInfo(
      el("previewInfo"),
      `共 ${data.count} 个 h2 切点。` +
        (data.leading_in_first
          ? "首个 h2 之前的正文将并入第 1 篇。"
          : "该文件首篇无前置正文。"),
      "ok"
    );
    el("step3").classList.remove("hidden");
    renderSegmentNav();
    loadSegment();
  } catch (err) {
    el("step3").classList.add("hidden");
    setInfo(el("previewInfo"), "", "");
    showError(err);
  }
});

function renderSegmentNav() {
  const seg = state.segments[state.current];
  el("segPos").textContent =
    `第 ${state.current + 1} / ${state.segments.length} 篇：${seg.title}` +
    (seg.h2_id ? `  (#${seg.h2_id})` : "");
  el("prevSeg").disabled = state.current === 0;
  el("nextSeg").disabled = state.current === state.segments.length - 1;
}

function loadSegment() {
  const params = new URLSearchParams({
    book_id: state.bookId,
    path: state.sourcePath,
    index: String(state.current),
  });
  el("previewFrame").src = "/api/segment?" + params.toString();
  renderSegmentNav();
}

el("prevSeg").addEventListener("click", () => {
  if (state.current > 0) {
    state.current -= 1;
    loadSegment();
  }
});
el("nextSeg").addEventListener("click", () => {
  if (state.current < state.segments.length - 1) {
    state.current += 1;
    loadSegment();
  }
});

// ------------------------------------------------------------ 下载

el("downloadBtn").addEventListener("click", async () => {
  clearError();
  const btn = el("downloadBtn");
  btn.disabled = true;
  btn.textContent = "正在拆分并打包…";
  try {
    const fd = new FormData();
    fd.append("book_id", state.bookId);
    fd.append("path", state.sourcePath);
    const resp = await fetch("/api/split", { method: "POST", body: fd });
    if (!resp.ok) {
      let detail;
      try {
        detail = (await resp.json()).detail;
      } catch (_) {
        detail = "拆分失败。";
      }
      throw detail;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const base = state.sourcePath.split("/").pop().replace(/\.[^.]+$/, "");
    a.href = url;
    a.download = `${base}-split.epub`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    showError(err);
  } finally {
    btn.disabled = false;
    btn.textContent = "下载拆分后的新书 (.epub)";
  }
});
