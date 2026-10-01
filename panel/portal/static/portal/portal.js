"use strict";

// 交互只在本机页面内完成，不将订阅地址发送给第三方。
document.addEventListener("DOMContentLoaded", function () {
  const feedback = document.getElementById("copy-feedback");
  let feedbackTimer;

  function notify(message) {
    if (!feedback) return;
    window.clearTimeout(feedbackTimer);
    feedback.textContent = message;
    feedback.hidden = false;
    feedbackTimer = window.setTimeout(function () {
      feedback.hidden = true;
    }, 4000);
  }

  const passwordForm = document.querySelector("[data-password-change]");
  if (passwordForm instanceof HTMLFormElement) {
    const oldPassword = passwordForm.elements.namedItem("old_password");
    const newPassword = passwordForm.elements.namedItem("new_password1");
    const repeatPassword = passwordForm.elements.namedItem("new_password2");
    const status = document.getElementById("password-check");
    if (oldPassword instanceof HTMLInputElement && newPassword instanceof HTMLInputElement &&
        repeatPassword instanceof HTMLInputElement && status) {
      function checkPassword() {
        const tooShort = newPassword.value.length > 0 && newPassword.value.length < 12;
        const reused = oldPassword.value.length > 0 && newPassword.value === oldPassword.value;
        const mismatch = repeatPassword.value.length > 0 && repeatPassword.value !== newPassword.value;
        newPassword.setCustomValidity(tooShort ? "新密码至少需要 12 位。" : reused ? "新密码不能与旧密码相同。" : "");
        repeatPassword.setCustomValidity(mismatch ? "两次新密码不一致。" : "");
        status.textContent = tooShort ? "新密码至少需要 12 位。" : reused ? "新密码不能与旧密码相同。" :
          mismatch ? "两次新密码不一致。" : repeatPassword.value ? "长度与两次输入一致；服务器还会检查密码强度。" :
          "请填写旧密码和两次新密码。";
      }
      for (const input of [oldPassword, newPassword, repeatPassword]) input.addEventListener("input", checkPassword);
      passwordForm.addEventListener("submit", function (event) {
        checkPassword();
        if (!passwordForm.checkValidity()) {
          event.preventDefault();
          passwordForm.reportValidity();
        }
      });
    }
  }

  document.addEventListener("click", async function (event) {
    if (!(event.target instanceof Element)) return;
    const revealButton = event.target.closest("[data-reveal]");
    if (revealButton) {
      const input = document.getElementById(revealButton.dataset.reveal);
      if (!(input instanceof HTMLInputElement)) return;
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      revealButton.textContent = show ? "隐藏" : "显示";
      revealButton.setAttribute("aria-pressed", String(show));
      return;
    }

    const copyButton = event.target.closest("[data-copy]");
    if (!copyButton) return;
    const source = document.getElementById(copyButton.dataset.copy);
    if (!source) return;
    const value = source instanceof HTMLInputElement ? source.value : source.textContent;
    if (!value) return;
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error("clipboard-unavailable");
      await navigator.clipboard.writeText(value);
      notify("已复制，请只粘贴到你信任的客户端。");
    } catch (_) {
      notify("浏览器未允许自动复制，请显示后手动选择并复制。");
    }
  });
});
