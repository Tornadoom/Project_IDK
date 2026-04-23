const state = {
  user: null,
  todos: [],
  cart: [],
  logs: [],
  labels: { agree_a: "A", agree_b: "B" },
  todoSortDue: false,
  logDate: "",
  cropImage: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    let message = "请求失败";
    try { message = (await res.json()).error || message; } catch {}
    throw new Error(message);
  }
  if (options.raw) return res;
  return res.json();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.remove("hidden");
  clearTimeout(node.timer);
  node.timer = setTimeout(() => node.classList.add("hidden"), 2200);
}

function formDataObject(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function setAuthMessage(message) {
  $("#authMessage").textContent = message || "";
}

async function boot() {
  bindEvents();
  try {
    state.user = await api("/api/me");
    showApp();
    await refreshAll();
  } catch {
    showAuth();
  }
}

function bindEvents() {
  $$("[data-auth-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$("[data-auth-tab]").forEach((item) => item.classList.toggle("active", item === btn));
      $("#loginForm").classList.toggle("hidden", btn.dataset.authTab !== "login");
      $("#registerForm").classList.toggle("hidden", btn.dataset.authTab !== "register");
      setAuthMessage("");
    });
  });

  $("#loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/login", { method: "POST", body: JSON.stringify(formDataObject(event.currentTarget)) });
      state.user = await api("/api/me");
      showApp();
      await refreshAll();
    } catch (err) { setAuthMessage(err.message); }
  });

  $("#registerForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/register", { method: "POST", body: JSON.stringify(formDataObject(event.currentTarget)) });
      setAuthMessage("注册成功，请登录");
      document.querySelector('[data-auth-tab="login"]').click();
    } catch (err) { setAuthMessage(err.message); }
  });

  $$(".nav-link").forEach((btn) => btn.addEventListener("click", () => setView(btn.dataset.view)));
  $("#logoutBtn").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST", body: "{}" });
    showAuth();
  });
  $("#saveNicknameBtn").addEventListener("click", saveNickname);
  $("#avatarBtn").addEventListener("click", () => $("#avatarInput").click());
  $("#avatarInput").addEventListener("change", loadAvatarFile);
  $("#cropZoom").addEventListener("input", drawCrop);
  $("#saveAvatarBtn").addEventListener("click", saveAvatar);

  $("#newTodoBtn").addEventListener("click", () => openTodoDialog());
  $("#sortTodosBtn").addEventListener("click", async () => {
    state.todoSortDue = !state.todoSortDue;
    await loadTodos();
  });
  $("#todoForm").addEventListener("submit", saveTodo);

  $("#newCartBtn").addEventListener("click", () => openCartDialog());
  $("#cartLabelBtn").addEventListener("click", openCartLabelDialog);
  $("#cartImageInput").addEventListener("change", loadCartImage);
  $("#cartForm").addEventListener("submit", saveCart);
  $("#cartLabelForm").addEventListener("submit", saveCartLabels);
  $("#logDateInput").addEventListener("change", async (event) => {
    state.logDate = event.target.value;
    await loadLogs();
  });
  $("#clearLogDateBtn").addEventListener("click", async () => {
    state.logDate = "";
    $("#logDateInput").value = "";
    await loadLogs();
  });

  $("#backupBtn").addEventListener("click", async () => {
    await api("/api/backup", { method: "POST", body: "{}" });
    toast("已完成数据库备份");
    await loadLogs();
  });
  $("#exportMdBtn").addEventListener("click", () => window.location.href = "/api/export?format=md");
  $("#exportXlsxBtn").addEventListener("click", () => window.location.href = "/api/export?format=xlsx");

  $$("[data-close]").forEach((btn) => btn.addEventListener("click", () => btn.closest("dialog").close()));
}

function showAuth() {
  $("#authView").classList.remove("hidden");
  $("#appView").classList.add("hidden");
}

function showApp() {
  $("#authView").classList.add("hidden");
  $("#appView").classList.remove("hidden");
  $("#nicknameInput").value = state.user.nickname;
  renderAvatar();
}

function renderAvatar() {
  const img = $("#avatarImg");
  const fallback = $("#avatarFallback");
  if (state.user.avatar_url) {
    img.src = state.user.avatar_url + "?v=" + Date.now();
    img.style.display = "block";
    fallback.style.display = "none";
  } else {
    img.style.display = "none";
    fallback.style.display = "inline";
  }
}

function setView(view) {
  const title = { home: "总览", todos: "待办事项", cart: "购物车", logs: "修改日志" }[view];
  $("#pageTitle").textContent = title;
  $$(".nav-link").forEach((btn) => btn.classList.toggle("active", btn.dataset.view === view));
  $$(".view").forEach((node) => node.classList.add("hidden"));
  $(`#${view}View`).classList.remove("hidden");
  if (view === "logs") loadLogs();
}

async function refreshAll() {
  await loadCartLabels();
  await Promise.all([loadTodos(), loadCart(), loadLogs()]);
  renderHome();
}

async function loadCartLabels() {
  state.labels = await api("/api/settings/cart-labels");
  renderCartLabels();
}

async function loadTodos() {
  const query = state.todoSortDue ? "?sort=due" : "";
  state.todos = await api("/api/todos" + query);
  renderTodos();
  renderHome();
}

async function loadCart() {
  state.cart = await api("/api/cart");
  renderCart();
  renderHome();
}

async function loadLogs() {
  const query = state.logDate ? `?date=${encodeURIComponent(state.logDate)}` : "";
  state.logs = await api("/api/logs" + query);
  renderLogs();
}

function renderHome() {
  $("#todoCount").textContent = state.todos.length;
  $("#p0Count").textContent = state.todos.filter((item) => item.priority === "P0").length;
  $("#buyCount").textContent = state.cart.filter((item) => item.agree_a && item.agree_b).length;
  $("#homeTodos").innerHTML = state.todos.slice(0, 5).map((item) => `
    <div class="compact-item">
      <div><strong>${escapeHtml(item.item)}</strong><br><small>${dueText(item)}</small></div>
      ${priorityTag(item.priority)}
    </div>
  `).join("") || `<small>暂无待办</small>`;
  $("#homeCart").innerHTML = state.cart.filter((item) => item.agree_a && item.agree_b).slice(0, 5).map((item) => `
    <div class="compact-item"><strong>${escapeHtml(item.product_name)}</strong><span class="status-buy">待购买</span></div>
  `).join("") || `<small>暂无待购买商品</small>`;
}

function renderTodos() {
  $("#todoTable").innerHTML = state.todos.map((item) => `
    <tr class="todo-row-${item.priority.toLowerCase()}">
      <td><strong>${escapeHtml(item.item)}</strong></td>
      <td>${dueText(item)}</td>
      <td>${priorityTag(item.priority)}</td>
      <td>${item.link ? `<a href="${escapeAttr(item.link)}" target="_blank" rel="noreferrer">打开链接</a>` : "-"}</td>
      <td>${escapeHtml(item.notes || "-")}</td>
      <td><div class="row-actions">
        <button class="ghost" onclick="editTodo(${item.id})">编辑</button>
        <button class="danger-text" onclick="deleteTodo(${item.id})">删除</button>
      </div></td>
    </tr>
  `).join("") || `<tr><td colspan="6">暂无数据</td></tr>`;
}

function renderCart() {
  $("#cartTable").innerHTML = state.cart.map((item) => `
    <tr>
      <td><strong>${escapeHtml(item.product_name)}</strong></td>
      <td>${item.image_url ? `<button class="ghost" onclick="showImage('${escapeAttr(item.image_url)}')">查看图片</button>` : "无图片"}</td>
      <td><label class="inline-check"><input type="checkbox" ${item.agree_a ? "checked" : ""} onchange="toggleCartAgree(${item.id}, 'agree_a', this.checked)" /> ${escapeHtml(state.labels.agree_a)}</label></td>
      <td><label class="inline-check"><input type="checkbox" ${item.agree_b ? "checked" : ""} onchange="toggleCartAgree(${item.id}, 'agree_b', this.checked)" /> ${escapeHtml(state.labels.agree_b)}</label></td>
      <td class="${item.status === "待购买" ? "status-buy" : ""}">${item.status}</td>
      <td><div class="row-actions">
        <button class="ghost" onclick="editCart(${item.id})">编辑</button>
        <button class="danger-text" onclick="deleteCart(${item.id})">删除</button>
      </div></td>
    </tr>
  `).join("") || `<tr><td colspan="6">暂无数据</td></tr>`;
}

function renderCartLabels() {
  $("#agreeAHead").textContent = `${state.labels.agree_a}同意`;
  $("#agreeBHead").textContent = `${state.labels.agree_b}同意`;
}

function renderLogs() {
  $("#logsTable").innerHTML = state.logs.map((item) => `
    <tr>
      <td>${escapeHtml(formatLogTime(item.created_at))}</td>
      <td>${escapeHtml(actionLabel(item.action))}</td>
      <td>${escapeHtml(entityLabel(item.entity))}</td>
      <td>${item.entity_id || "-"}</td>
      <td>${escapeHtml(logSummary(item))}</td>
    </tr>
  `).join("") || `<tr><td colspan="5">暂无日志</td></tr>`;
}

function openTodoDialog(item = null) {
  const form = $("#todoForm");
  form.reset();
  form.id.value = item?.id || "";
  form.item.value = item?.item || "";
  form.due_date.value = item?.due_date || "";
  form.due_time.value = item?.due_time || "";
  form.link.value = item?.link || "";
  form.priority.value = item?.priority || "P2";
  form.notes.value = item?.notes || "";
  $("#todoDialogTitle").textContent = item ? "编辑事项" : "新增事项";
  $("#todoDialog").showModal();
}

window.editTodo = (id) => openTodoDialog(state.todos.find((item) => item.id === id));
window.deleteTodo = async (id) => {
  if (!confirm("确认删除这条待办事项？")) return;
  await api(`/api/todos/${id}`, { method: "DELETE" });
  toast("已删除待办");
  await refreshAll();
};

async function saveTodo(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const body = formDataObject(form);
  const id = body.id;
  delete body.id;
  await api(id ? `/api/todos/${id}` : "/api/todos", {
    method: id ? "PUT" : "POST",
    body: JSON.stringify(body),
  });
  $("#todoDialog").close();
  toast("已保存待办");
  await refreshAll();
}

function openCartDialog(item = null) {
  const form = $("#cartForm");
  form.reset();
  form.id.value = item?.id || "";
  form.product_name.value = item?.product_name || "";
  form.imageData.value = "";
  $("#cartImageName").textContent = item?.image_url ? "已上传图片，可重新选择" : "未选择图片";
  $("#cartDialogTitle").textContent = item ? "编辑商品" : "新增商品";
  $("#cartDialog").showModal();
}

window.editCart = (id) => openCartDialog(state.cart.find((item) => item.id === id));
window.deleteCart = async (id) => {
  if (!confirm("确认删除这个商品？")) return;
  await api(`/api/cart/${id}`, { method: "DELETE" });
  toast("已删除商品");
  await refreshAll();
};

window.toggleCartAgree = async (id, field, checked) => {
  const item = state.cart.find((entry) => entry.id === id);
  if (!item) return;
  const previous = item[field];
  item[field] = checked ? 1 : 0;
  renderCart();
  renderHome();
  try {
    await api(`/api/cart/${id}`, {
      method: "PUT",
      body: JSON.stringify({
        product_name: item.product_name,
        agree_a: Boolean(item.agree_a),
        agree_b: Boolean(item.agree_b),
      }),
    });
    await refreshAll();
    toast("同意状态已更新");
  } catch (err) {
    item[field] = previous;
    renderCart();
    renderHome();
    toast(err.message);
  }
};

async function loadCartImage(event) {
  const file = event.target.files[0];
  if (!file) return;
  $("#cartImageName").textContent = file.name;
  $("#cartForm").imageData.value = await readFileDataUrl(file);
}

async function saveCart(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const body = {
    product_name: form.product_name.value,
    image: form.imageData.value || null,
    agree_a: id ? Boolean(state.cart.find((item) => item.id === Number(id))?.agree_a) : false,
    agree_b: id ? Boolean(state.cart.find((item) => item.id === Number(id))?.agree_b) : false,
  };
  const id = form.id.value;
  await api(id ? `/api/cart/${id}` : "/api/cart", {
    method: id ? "PUT" : "POST",
    body: JSON.stringify(body),
  });
  $("#cartDialog").close();
  toast("已保存商品");
  await refreshAll();
}

function openCartLabelDialog() {
  const form = $("#cartLabelForm");
  form.agree_a.value = state.labels.agree_a || "A";
  form.agree_b.value = state.labels.agree_b || "B";
  $("#cartLabelDialog").showModal();
}

async function saveCartLabels(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const labels = {
    agree_a: form.agree_a.value.trim() || "A",
    agree_b: form.agree_b.value.trim() || "B",
  };
  const saved = await api("/api/settings/cart-labels", { method: "PUT", body: JSON.stringify(labels) });
  state.labels = { agree_a: saved.agree_a, agree_b: saved.agree_b };
  renderCartLabels();
  renderCart();
  $("#cartLabelDialog").close();
  toast("同意人名称已更新");
  await loadLogs();
}

window.showImage = (url) => {
  $("#detailImage").src = url;
  $("#imageDialog").showModal();
};

async function saveNickname() {
  const nickname = $("#nicknameInput").value.trim();
  await api("/api/profile", { method: "PUT", body: JSON.stringify({ nickname }) });
  state.user.nickname = nickname;
  toast("昵称已更新");
}

async function loadAvatarFile(event) {
  const file = event.target.files[0];
  if (!file) return;
  const img = new Image();
  img.onload = () => {
    state.cropImage = img;
    $("#cropZoom").value = "1";
    drawCrop();
    $("#cropDialog").showModal();
  };
  img.src = await readFileDataUrl(file);
}

function drawCrop() {
  if (!state.cropImage) return;
  const canvas = $("#cropCanvas");
  const ctx = canvas.getContext("2d");
  const zoom = Number($("#cropZoom").value);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#f8fafc";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const img = state.cropImage;
  const base = Math.max(canvas.width / img.width, canvas.height / img.height) * zoom;
  const w = img.width * base;
  const h = img.height * base;
  ctx.drawImage(img, (canvas.width - w) / 2, (canvas.height - h) / 2, w, h);
}

async function saveAvatar() {
  const image = $("#cropCanvas").toDataURL("image/png");
  const data = await api("/api/profile/avatar", { method: "POST", body: JSON.stringify({ image }) });
  state.user.avatar_url = data.avatar_url;
  renderAvatar();
  $("#cropDialog").close();
  toast("头像已更新");
}

function readFileDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function dueText(item) {
  const date = item.due_date || "未设日期";
  const time = item.due_time || "未设时间";
  return `${date} ${time}`;
}

function priorityTag(priority) {
  const map = { P0: "P0 紧急", P1: "P1 较紧急", P2: "P2 自然推进" };
  return `<span class="tag tag-${priority.toLowerCase()}">${map[priority] || priority}</span>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

function formatLogTime(value) {
  if (!value) return "";
  return value.replace("T", " ").slice(0, 19);
}

function actionLabel(action) {
  return {
    create: "新增",
    update: "编辑",
    delete: "删除",
    register: "注册",
    login: "登录",
    backup: "备份",
    update_avatar: "头像",
  }[action] || action;
}

function entityLabel(entity) {
  return {
    todo: "待办事项",
    cart: "购物车",
    user: "用户",
    profile: "个人资料",
    database: "数据库",
  }[entity] || entity;
}

function logSummary(item) {
  try {
    const details = JSON.parse(item.details || "{}");
    if (details.item) return details.item;
    if (details.product_name) return details.product_name;
    if (details.nickname) return `昵称：${details.nickname}`;
    if (details.file) return details.file;
    if (details.username) return details.username;
  } catch {}
  return item.details || "";
}

boot();
