(function () {
      const form = document.getElementById('session-review');
      if (!form) return;
      const pending = Array.from(form.querySelectorAll('.song[data-pending="true"]'));
      const count = document.getElementById('review-count');
      const unmarked = document.getElementById('review-unmarked');
      const dialog = document.getElementById('finish-dialog');
      function update() {
        const selected = pending.filter(row => row.querySelector('input:checked')).length;
        count.textContent = String(selected);
        unmarked.textContent = String(pending.length - selected);
        document.getElementById('finish-summary').textContent =
          (pending.length - selected) + ' unmarked song(s). Save the selected reviews and choose what happens to the rest. The listening playlist will be cleared.';
      }
      form.addEventListener('change', event => {
        const input = event.target;
        if (!input.matches('.song input[type="checkbox"]')) return;
        if (input.checked) {
          input.closest('.song').querySelectorAll('input[type="checkbox"]').forEach(other => {
            if (other !== input) other.checked = false;
          });
        }
        update();
      });
      document.getElementById('finish-open').addEventListener('click', () => {
        update();
        dialog.showModal();
      });
      document.getElementById('finish-cancel').addEventListener('click', () => dialog.close());
      update();
    }());
