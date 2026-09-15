const bridge = window.AstrBotPluginPage;
const els = {};
let stickers = [];

function qs(id) {
  return document.getElementById(id);
}

function setStatus(msg, isError = false) {
  const el = els.status;
  el.textContent = msg || "";
  el.classList.toggle("error", Boolean(isError));
}

function thumbSrc(item) {
  return `stickers/image/${item.local_id}`;
}

function render() {
  const list = els.list;
  list.innerHTML = "";
  if (!stickers.length) {
    list.innerHTML = '<div class="hint">暂无表情包，先上传。</div>';
    return;
  }
  for (const it of stickers) {
    const card = document.createElement("div");
    card.className = "item";
    const wrap = document.createElement("div");
    wrap.className = "thumb-wrap";
    const img = document.createElement("img");
    img.className = "thumb";
    img.alt = it.description || it.file_name || String(it.local_id);
    img.loading = "lazy";
    img.src = thumbSrc(it);
    img.addEventListener("error", () => {
      if (it.url && img.src !== it.url) {
        img.onerror = () => { wrap.style.display = "none"; };
        img.src = it.url;
      } else {
        wrap.style.display = "none";
      }
    });
    img.addEventListener("click", () => {
      window.open(img.src, "_blank", "noopener,noreferrer");
    });
    img.title = "点击查看大图";
    img.style.cursor = "zoom-in";
    wrap.append(img);
    const meta = document.createElement("div");
    meta.className = "meta";
    const title = document.createElement("div");
    title.textContent = `#${it.local_id} ${it.file_name || ""}`.trim();
    const ta = document.createElement("textarea");
    ta.rows = 2;
    ta.placeholder = "适用情绪或场景；来源可写在后面";
    ta.value = it.description || "";
    const save = document.createElement("button");
    save.textContent = "保存备注";
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const response = await bridge.apiPost("stickers/save_desc", {
          local_id: it.local_id,
          description: ta.value.trim(),
        });
        await load();
        if (response?.favorite_sync_error) {
          setStatus(`#${it.local_id} 本地备注已保存，QQ 收藏备注同步失败`, true);
        } else {
          setStatus(`#${it.local_id} 备注已保存`);
        }
      } catch (e) {
        setStatus(String(e?.message || e), true);
      } finally {
        save.disabled = false;
      }
    });
    const del = document.createElement("button");
    del.textContent = "删除";
    let confirmTimer = null;
    del.addEventListener("click", async () => {
      if (del.dataset.confirm !== "1") {
        del.dataset.confirm = "1";
        del.textContent = "确认删除";
        del.classList.add("danger");
        setStatus(`再点一次确认删除 #${it.local_id}`, true);
        clearTimeout(confirmTimer);
        confirmTimer = setTimeout(() => {
          del.dataset.confirm = "0";
          del.textContent = "删除";
          del.classList.remove("danger");
          setStatus("");
        }, 4000);
        return;
      }
      clearTimeout(confirmTimer);
      del.dataset.confirm = "0";
      del.disabled = true;
      del.textContent = "删除中…";
      del.classList.remove("danger");
      setStatus(`正在删除 #${it.local_id}…`);
      try {
        const response = await bridge.apiPost("stickers/delete", { local_id: it.local_id });
        await load();
        if (response?.favorite_delete_error) {
          setStatus(`#${it.local_id} 已从本地删除，QQ 收藏删除失败`, true);
        } else {
          setStatus(`#${it.local_id} 已删除`);
        }
      } catch (e) {
        const msg = String(e?.message || e);
        setStatus(`删除失败 #${it.local_id}：${msg}`, true);
        del.disabled = false;
        del.textContent = "删除";
      }
    });
    const actions = document.createElement("div");
    actions.className = "actions";
    actions.append(save, del);
    meta.append(title, ta, actions);
    card.append(wrap, meta);
    list.append(card);
  }
}

async function load() {
  try {
    const data = await bridge.apiGet("stickers/list", {});
    stickers = Array.isArray(data?.stickers) ? data.stickers : (Array.isArray(data) ? data : []);
    render();
    setStatus(`共 ${stickers.length} 张`);
  } catch (e) {
    setStatus(String(e?.message || e), true);
  }
}

function buildPendingRows(files) {
  const box = els.pendingBox;
  box.innerHTML = "";
  if (!files.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  files.forEach((file, index) => {
    const row = document.createElement("div");
    row.className = "pending-row";
    const name = document.createElement("span");
    name.textContent = `${file.name} (${Math.round(file.size / 1024)} KB)`;
    const input = document.createElement("input");
    input.placeholder = "适用情绪或场景（可选）";
    input.dataset.index = String(index);
    row.append(name, input);
    box.append(row);
  });
}

async function doUpload() {
  const input = els.fileInput;
  const files = Array.from(input.files || []);
  if (!files.length) { setStatus("请选择文件", true); return; }
  const descriptions = [];
  for (const input of els.pendingBox.querySelectorAll("input[data-index]")) {
    descriptions[Number(input.dataset.index)] = input.value.trim();
  }
  els.uploadBtn.disabled = true;
  setStatus(`上传 ${files.length} 张中...`);
  let successCount = 0;
  let failureCount = 0;
  let lastError = "";
  for (const [index, file] of files.entries()) {
    try {
      const uploaded = await bridge.upload("stickers/upload", file);
      const description = descriptions[index] || "";
      const localId = uploaded?.sticker?.local_id;
      if (description && localId != null) {
        await bridge.apiPost("stickers/save_desc", {
          local_id: localId,
          description,
        });
      }
      successCount += 1;
    } catch (error) {
      failureCount += 1;
      lastError = String(error?.message || error);
    }
  }
  buildPendingRows([]);
  input.value = "";
  els.uploadBtn.disabled = false;
  await load();
  const summary = `完成：成功 ${successCount}，失败 ${failureCount}`;
  setStatus(lastError ? `${summary}；${lastError}` : summary, failureCount > 0);
}

async function init() {
  els.status = qs("status");
  els.list = qs("list");
  els.fileInput = qs("fileInput");
  els.uploadBtn = qs("uploadBtn");
  els.pendingBox = qs("pendingBox");
  els.refreshBtn = qs("refreshBtn");
  await bridge.ready();
  els.fileInput.addEventListener("change", () => buildPendingRows(Array.from(els.fileInput.files || [])));
  els.uploadBtn.addEventListener("click", doUpload);
  els.refreshBtn.addEventListener("click", load);
  await load();
}
init();
