(function () {
  const form = document.getElementById('session-review');
  if (!form) return;
  const rows = Array.from(form.querySelectorAll('.song'));
  const pending = rows.filter(row => row.dataset.pending === 'true');
  const count = document.getElementById('review-count');
  const unmarked = document.getElementById('review-unmarked');
  const dialog = document.getElementById('finish-dialog');
  const defaults = {next: 'j', previous: 'k', keep: '1', skip: '2', want: 'w', finish: 'f'};
  const storageKey = 'crate-digger.discover.shortcuts.v1';
  let shortcuts = {...defaults};
  let activeIndex = Math.max(0, rows.findIndex(row => row.dataset.pending === 'true'));

  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || '{}');
    if (saved && typeof saved === 'object') {
      const candidate = {...defaults};
      for (const action of Object.keys(defaults)) {
        if (typeof saved[action] === 'string' && saved[action].length === 1) candidate[action] = saved[action].toLowerCase();
      }
      if (new Set(Object.values(candidate)).size === Object.keys(defaults).length) shortcuts = candidate;
    }
  } catch (_) { /* Browser storage may be disabled. */ }

  function update() {
    if (!dialog) return;
    const selected = pending.filter(row => row.querySelector('.choices input:checked')).length;
    count.textContent = String(selected);
    unmarked.textContent = String(pending.length - selected);
    document.getElementById('finish-summary').textContent =
      (pending.length - selected) + ' unmarked song(s). Save the selected reviews and choose what happens to the rest. The listening playlist will be cleared.';
  }

  function selectRow(index) {
    if (!rows.length) return;
    rows[activeIndex]?.classList.remove('keyboard-active');
    activeIndex = (index + rows.length) % rows.length;
    const row = rows[activeIndex];
    row.classList.add('keyboard-active');
    row.querySelector('.song-name').focus({preventScroll: true});
    row.scrollIntoView({block: 'nearest'});
  }

  form.addEventListener('change', event => {
    const input = event.target;
    if (!input.matches('.choices input[type="checkbox"]')) return;
    if (input.checked) {
      input.closest('.choices').querySelectorAll('input[type="checkbox"]').forEach(other => {
        if (other !== input) other.checked = false;
      });
    }
    update();
  });

  form.addEventListener('focusin', event => {
    const index = rows.indexOf(event.target.closest('.song'));
    if (index >= 0) {
      rows[activeIndex]?.classList.remove('keyboard-active');
      activeIndex = index;
      rows[activeIndex].classList.add('keyboard-active');
    }
  });

  if (dialog) {
    document.getElementById('finish-open').addEventListener('click', () => {
      update();
      dialog.showModal();
    });
    document.getElementById('finish-cancel').addEventListener('click', () => dialog.close());
  }

  const settings = document.getElementById('shortcut-settings');
  const message = document.getElementById('shortcut-message');
  function showSettings() {
    for (const input of settings.querySelectorAll('[data-shortcut]')) input.value = shortcuts[input.dataset.shortcut];
  }
  document.getElementById('shortcut-save').addEventListener('click', () => {
    const candidate = {};
    for (const input of settings.querySelectorAll('[data-shortcut]')) {
      const key = input.value.trim().toLowerCase();
      if (key.length !== 1 || /\s/.test(key)) {
        message.textContent = 'Use one character for each shortcut.';
        return;
      }
      candidate[input.dataset.shortcut] = key;
    }
    if (new Set(Object.values(candidate)).size !== Object.keys(defaults).length) {
      message.textContent = 'Choose a different key for each action.';
      return;
    }
    shortcuts = candidate;
    try { localStorage.setItem(storageKey, JSON.stringify(shortcuts)); } catch (_) { /* Session-only settings. */ }
    message.textContent = 'Shortcuts saved in this browser.';
  });
  document.getElementById('shortcut-reset').addEventListener('click', () => {
    shortcuts = {...defaults};
    try { localStorage.removeItem(storageKey); } catch (_) { /* Session-only settings. */ }
    showSettings();
    message.textContent = 'Default shortcuts restored.';
  });
  showSettings();

  document.addEventListener('keydown', event => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.isComposing || dialog?.open) return;
    const target = event.target;
    if (target instanceof HTMLElement && (target.isContentEditable || target.matches('input[type="text"], textarea, select'))) return;
    const action = Object.keys(shortcuts).find(name => shortcuts[name] === event.key.toLowerCase());
    if (!action) return;
    if (action === 'finish') {
      if (!dialog) return;
      event.preventDefault();
      update();
      dialog.showModal();
      return;
    }
    if (!rows.length) return;
    event.preventDefault();
    if (action === 'next') selectRow(activeIndex + 1);
    if (action === 'previous') selectRow(activeIndex - 1);
    if (action === 'keep' || action === 'skip') {
      const choice = rows[activeIndex].querySelector(`.choices input[value="${action}"]`);
      if (choice && !choice.disabled) {
        choice.checked = !choice.checked;
        choice.dispatchEvent(new Event('change', {bubbles: true}));
      }
    }
    if (action === 'want') {
      const choice = rows[activeIndex].querySelector('.want-choice input[type="checkbox"]');
      choice.checked = !choice.checked;
    }
  });
  update();
}());
