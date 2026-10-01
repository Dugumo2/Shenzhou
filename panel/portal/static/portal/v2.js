"use strict";
// 页面只筛选展示；客户端支持范围和权限仍由服务端校验。
document.addEventListener("DOMContentLoaded", function () {
  const builder = document.querySelector("[data-client-builder]");
  if (builder) {
    const select = builder.querySelector('select[name="client"]');
    const buttons = Array.from(builder.querySelectorAll("[data-device]"));
    const form = builder.querySelector("[data-client-form]");
    const unavailable = builder.querySelector("[data-router-unavailable]");
    const hint = builder.querySelector("[data-client-hint]");
    const options = select ? Array.from(select.options).map(option => option.cloneNode(true)) : [];
    function changeDevice(device, preserve) {
      buttons.forEach(button => { const active = button.dataset.device === device; button.classList.toggle("selected", active); button.setAttribute("aria-pressed", String(active)); });
      form.hidden = device === "router";
      unavailable.hidden = device !== "router";
      if (!select || device === "router") return;
      const selected = select.value;
      const allowed = device === "computer" ? ["windows"] : ["v2rayng", "android"];
      select.replaceChildren(...options.filter(option => !option.value || allowed.includes(option.value)).map(option => option.cloneNode(true)));
      if (preserve && allowed.includes(selected)) select.value = selected;
      else if (!select.value) select.value = allowed.find(value => options.some(option => option.value === value)) || "";
      changeClient();
    }
    function changeClient() {
      const messages = {windows:"Windows · v2rayN：节点订阅、路由方案和规则资源分别展示与更新。",v2rayng:"Android · v2rayNG：节点订阅与路由资源分开；请使用对应安卓版本的资源。",android:"Android · SFA：交付完整远程配置，在 SFA 中更新该配置。"};
      hint.textContent = messages[select?.value] || "请选择你正在使用的软件。";
    }
    buttons.forEach(button => button.addEventListener("click", () => changeDevice(button.dataset.device, false)));
    if (select) { select.addEventListener("change", changeClient); changeDevice(select.value === "android" || select.value === "v2rayng" ? "phone" : "computer", true); }
  }
  const search = document.querySelector("[data-guide-search]");
  if (search) {
    const articles = Array.from(document.querySelectorAll("[data-guide-article]"));
    const filters = Array.from(document.querySelectorAll("[data-guide-filter]"));
    const count = document.querySelector("[data-guide-count]");
    const empty = document.querySelector("[data-guide-empty]");
    let client = "all";
    function update() {
      const words = search.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
      let visible = 0;
      articles.forEach(article => {
        const match = (client === "all" || article.dataset.clients === "all" || article.dataset.clients === client) && words.every(word => (article.textContent + " " + (article.dataset.keywords || "")).toLocaleLowerCase().includes(word));
        article.hidden = !match;
        if (match) { visible++; if (words.length) article.open = true; }
      });
      filters.forEach(button => { const active = button.dataset.guideFilter === client; button.classList.toggle("selected", active); button.setAttribute("aria-pressed", String(active)); });
      count.textContent = "找到 " + visible + " 篇指南。";
      empty.hidden = visible !== 0;
    }
    filters.forEach(button => button.addEventListener("click", () => { client = button.dataset.guideFilter; update(); }));
    search.addEventListener("input", update);
    const initial = new URLSearchParams(window.location.search).get("client");
    if (["windows", "v2rayng", "android"].includes(initial)) client = initial;
    update();
  }
});

// 服务中心只负责展示与输入联动；权益、去重与操作权限由后端再次检查。
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-service-clients]").forEach(center => {
    const buttons = Array.from(center.querySelectorAll("[data-service-device]"));
    const cards = Array.from(center.querySelectorAll("[data-client-device]"));
    const router = center.querySelector("[data-service-router]");
    function pick(device) {
      buttons.forEach(button => { const active = button.dataset.serviceDevice === device; button.classList.toggle("selected", active); button.setAttribute("aria-pressed", String(active)); });
      cards.forEach(card => { card.hidden = card.dataset.clientDevice !== device; });
      if (router) router.hidden = device !== "router";
    }
    buttons.forEach(button => button.addEventListener("click", () => pick(button.dataset.serviceDevice)));
    pick("computer");
  });
  document.querySelectorAll("form[data-confirm-message]").forEach(form => {
    form.addEventListener("submit", event => {
      const approved = window.confirm(form.dataset.confirmMessage);
      if (!approved) { event.preventDefault(); return; }
      const confirm = form.querySelector('input[name="confirm"]');
      if (confirm) confirm.value = "yes";
    });
  });
  document.querySelectorAll("[data-validity-form]").forEach(form => {
    const months = form.querySelector('[name="service_months"]');
    const expiry = form.querySelector('[name="expires_at"]');
    const output = form.querySelector("[data-validity-preview]");
    function chosen(name, fallback) {
      const selected = form.querySelector('[name="' + name + '"]:checked');
      const plain = form.querySelector('select[name="' + name + '"]');
      return selected?.value || plain?.value || fallback;
    }
    function toggle(name, enabled) {
      const wrap = form.querySelector('[data-field-name="' + name + '"]');
      if (!wrap) return;
      wrap.hidden = !enabled;
      wrap.querySelectorAll("input,select,textarea").forEach(input => { input.disabled = !enabled; });
    }
    function preview() {
      const mode = chosen("validity_mode", "months");
      toggle("service_months", mode === "months");
      toggle("expires_at", mode === "date");
      const customReset = chosen("reset_mode", "custom") === "custom";
      toggle("reset_day", customReset); toggle("reset_time", customReset);
      if (!output) return;
      if (mode === "date") { output.textContent = expiry?.value ? "指定到期时间：" + expiry.value.replace("T", " ") : "请选择到期日期与时间。"; return; }
      const operation = chosen("operation", "save");
      const currentExpiry = form.dataset.currentExpiry;
      if (operation === "save" && currentExpiry) {
        const current = new Date(Date.parse(currentExpiry) + 8 * 60 * 60 * 1000);
        if (Number.isFinite(current.getTime())) { output.textContent = "保存设置保留原到期时间：" + current.toISOString().slice(0, 16).replace("T", " ") + " · 需要延长请将本次操作改为续期"; return; }
      }
      const count = Number(months?.value);
      if (!Number.isInteger(count) || count < 1 || count > 120) { output.textContent = "请输入 1–120 个月的有效期。"; return; }
      const startAt = operation === "renew" ? form.dataset.renewStartAt : form.dataset.startAt;
      const parsed = startAt ? Date.parse(startAt) : Date.now();
      if (!Number.isFinite(parsed)) { output.textContent = "起算时间待核验，保存后以服务端结果为准。"; return; }
      // 将上海时刻平移到 UTC 字段中计算自然月，月底截断并保留分秒。
      const date = new Date(parsed + 8 * 60 * 60 * 1000);
      const originalDay = date.getUTCDate();
      date.setUTCDate(1); date.setUTCMonth(date.getUTCMonth() + count);
      const lastDay = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
      date.setUTCDate(Math.min(originalDay, lastDay));
      const two = value => String(value).padStart(2, "0");
      output.textContent = "预计到期：" + date.getUTCFullYear() + "-" + two(date.getUTCMonth() + 1) + "-" + two(date.getUTCDate()) + " " + two(date.getUTCHours()) + ":" + two(date.getUTCMinutes()) + " · 以实际应用结果为准";
    }
    form.querySelectorAll("[data-months]").forEach(button => button.addEventListener("click", () => { if (months) months.value = button.dataset.months; preview(); }));
    form.addEventListener("change", preview); form.addEventListener("input", preview); preview();
  });
});
