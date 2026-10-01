// Keep installation instructions and navigation usable without JavaScript.
document.querySelectorAll('[data-copy]').forEach((button) => {
  const command = document.getElementById(button.dataset.copy);
  const status = button.closest('.command')?.nextElementSibling;
  if (!command || !status) return;
  button.hidden = false;
  button.addEventListener('click', async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(command.textContent.trim());
      status.textContent = 'Install command copied.';
    } catch {
      const range = document.createRange();
      range.selectNodeContents(command);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = 'Command selected. Use your device’s Copy action.';
    }
  });
});
