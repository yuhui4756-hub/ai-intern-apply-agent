document.addEventListener("click", async (event) => {
  const button = event.target.closest(".copy-button");
  if (!button) return;

  const targetId = button.getAttribute("data-copy-target");
  const target = document.getElementById(targetId);
  if (!target) return;

  try {
    await navigator.clipboard.writeText(target.value || target.textContent || "");
    const original = button.textContent;
    button.textContent = "已复制";
    setTimeout(() => {
      button.textContent = original;
    }, 1200);
  } catch {
    target.focus();
    target.select();
    document.execCommand("copy");
  }
});
