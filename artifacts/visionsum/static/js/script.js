/* script.js — VisionSum CCTV Summarization System */

// ── Dark Mode ─────────────────────────────────────────────────────────────────
(function () {
  const stored = localStorage.getItem('darkMode');
  if (stored === 'on') document.body.classList.add('dark-mode');
})();

function toggleDark() {
  document.body.classList.toggle('dark-mode');
  const on = document.body.classList.contains('dark-mode');
  localStorage.setItem('darkMode', on ? 'on' : 'off');
  const btn = document.getElementById('darkToggle');
  if (btn) btn.textContent = on ? '☀️ Light' : '🌙 Dark';
}

// ── Sidebar toggle (mobile) ───────────────────────────────────────────────────
function toggleSidebar() {
  const sb = document.querySelector('.sidebar');
  if (sb) sb.classList.toggle('open');
}

// ── Upload: drag-and-drop + XHR with real progress ───────────────────────────
function initUpload() {
  const zone    = document.getElementById('uploadZone');
  const input   = document.getElementById('videoInput');
  if (!zone || !input) return;

  const preview    = document.getElementById('videoPreview');
  const infoBox    = document.getElementById('fileInfo');
  const progWrap   = document.getElementById('uploadProgress');
  const bar        = document.getElementById('uploadBar');
  const pctEl      = document.getElementById('uploadPct');
  const loadedEl   = document.getElementById('uploadLoaded');
  const speedEl    = document.getElementById('uploadSpeed');
  const etaEl      = document.getElementById('uploadEta');
  const statusLbl  = document.getElementById('uploadStatusLabel');
  const submitBtn  = document.getElementById('submitBtn');

  let selectedFile = null;

  // Zone interaction
  zone.addEventListener('click', () => input.click());

  zone.addEventListener('dragover', e => {
    e.preventDefault();
    zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
  });

  input.addEventListener('change', () => {
    if (input.files.length) handleFile(input.files[0]);
  });

  function handleFile(file) {
    const ext = file.name.split('.').pop().toLowerCase();
    if (!['mp4', 'avi', 'mov', 'mkv'].includes(ext)) {
      showAlert('Unsupported format. Please use MP4, AVI, MOV, or MKV.', 'danger');
      return;
    }

    selectedFile = file;
    const mb = (file.size / 1024 / 1024).toFixed(1);

    if (infoBox) {
      infoBox.innerHTML =
        `<span class="badge badge-success">&#10004; File ready</span>
         <strong style="margin-left:.6rem;">${file.name}</strong>
         <span style="color:var(--text-muted);margin-left:.5rem;">${mb} MB</span>`;
      infoBox.style.display = 'block';
    }

    const icon = document.getElementById('uploadIcon');
    const hint = document.getElementById('uploadHint');
    const sub  = document.getElementById('uploadSub');
    if (icon) icon.innerHTML = '<i class="fa-solid fa-circle-check" style="color:var(--orange);"></i>';
    if (hint) hint.textContent = '✔ File selected';
    if (sub)  sub.textContent  = 'Click "Upload Video" below to start.';

    if (preview) {
      preview.src = URL.createObjectURL(file);
      const wrap = document.getElementById('previewWrap');
      if (wrap) wrap.style.display = 'block';
    }
  }

  // Expose globally so the button's onclick="startUpload()" works
  window.startUpload = function () {
    if (!selectedFile) {
      showAlert('Please select a video file first.', 'warning');
      return;
    }

    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Uploading…';
    }
    if (progWrap) progWrap.style.display = 'block';

    const formData = new FormData();
    formData.append('video', selectedFile);

    const xhr   = new XMLHttpRequest();
    const start = Date.now();

    xhr.upload.addEventListener('progress', e => {
      if (!e.lengthComputable) return;

      const pct      = Math.round(e.loaded / e.total * 100);
      const loadedMB = (e.loaded / 1024 / 1024).toFixed(1);
      const totalMB  = (e.total  / 1024 / 1024).toFixed(1);
      const elapsed  = (Date.now() - start) / 1000;
      const speed    = elapsed > 0 ? e.loaded / 1024 / 1024 / elapsed : 0;
      const eta      = speed > 0 ? (e.total - e.loaded) / 1024 / 1024 / speed : 0;

      if (bar)      bar.style.width    = pct + '%';
      if (pctEl)    pctEl.textContent  = pct + '%';
      if (loadedEl) loadedEl.textContent = `${loadedMB} MB / ${totalMB} MB`;
      if (speedEl)  speedEl.textContent = speed > 0.01 ? `${speed.toFixed(1)} MB/s` : '';
      if (etaEl)    etaEl.textContent   = eta > 0 ? `ETA: ${fmtEta(eta)}` : '';
    });

    xhr.upload.addEventListener('load', () => {
      if (bar)      bar.style.width     = '100%';
      if (pctEl)    pctEl.textContent   = '100%';
      if (statusLbl) statusLbl.textContent = 'Saving on server…';
      if (speedEl)  speedEl.textContent = '';
      if (etaEl)    etaEl.textContent   = '';
    });

    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 400) {
        try {
          const resp = JSON.parse(xhr.responseText);
          if (resp.redirect) {
            if (statusLbl) statusLbl.textContent = '✔ Upload complete! Redirecting…';
            setTimeout(() => { window.location.href = resp.redirect; }, 500);
            return;
          }
        } catch (_) {}
        window.location.href = '/dashboard';
      } else {
        let msg = 'Upload failed. Please try again.';
        try { msg = JSON.parse(xhr.responseText).error || msg; } catch (_) {}
        showAlert(msg, 'danger');
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = '<i class="fa-solid fa-upload"></i> Upload Video';
        }
      }
    });

    xhr.addEventListener('error', () => {
      showAlert('Network error during upload. Please try again.', 'danger');
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<i class="fa-solid fa-upload"></i> Upload Video';
      }
    });

    xhr.open('POST', '/upload/ajax');
    xhr.send(formData);
  };
}

function fmtEta(secs) {
  if (secs < 60) return `${Math.ceil(secs)}s`;
  return `${Math.floor(secs / 60)}m ${Math.ceil(secs % 60)}s`;
}

// ── Processing status poller ──────────────────────────────────────────────────
function pollStatus(summaryId) {
  const bar    = document.getElementById('progressBar');
  const status = document.getElementById('statusText');
  const steps  = document.querySelectorAll('.process-step');

  // Map real server stage → [step index (0-based), bar width %, label]
  const STAGE_MAP = {
    'processing':  [0, 10,  'Starting pipeline…'],
    'analyzing':   [1, 30,  'Analysing frames for motion…'],
    'summarizing': [3, 70,  'Extracting motion segments…'],
    'done':        [5, 100, '✔ Processing complete! Redirecting…'],
    'error':       [-1, 0,  '✖ Processing failed. Please try again.'],
  };

  let lastStage = '';

  function applyStage(stage) {
    if (stage === lastStage && stage !== 'done' && stage !== 'error') return;
    lastStage = stage;

    const [stepIdx, pct, label] = STAGE_MAP[stage] || [0, 15, 'Working…'];
    if (bar)    bar.style.width    = pct + '%';
    if (status) status.textContent = label;
    steps.forEach((el, i) => el.classList.toggle('active', i <= stepIdx));
  }

  // Show immediate feedback before first poll returns
  applyStage('processing');

  const interval = setInterval(() => {
    fetch(`/vapi/summary_status/${summaryId}`)
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        const stage = data.status || 'processing';
        applyStage(stage);

        if (stage === 'done') {
          clearInterval(interval);
          setTimeout(() => { window.location.href = `/results/${summaryId}`; }, 1200);
        } else if (stage === 'error') {
          clearInterval(interval);
          showAlert('Processing failed. Please try again.', 'danger');
        }
      })
      .catch(() => {
        // Network hiccup — keep polling silently
      });
  }, 2000);
}

// ── Compression card selector ─────────────────────────────────────────────────
function initCompressionCards() {
  document.querySelectorAll('.compression-card').forEach(card => {
    card.addEventListener('click', () => {
      document.querySelectorAll('.compression-card').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      const radio = card.querySelector('input[type="radio"]');
      if (radio) radio.checked = true;
    });
  });
}

// ── Alert helper ──────────────────────────────────────────────────────────────
function showAlert(message, type = 'info') {
  const div = document.createElement('div');
  div.className = `alert alert-${type}`;
  div.innerHTML = `<span>${message}</span>
    <button onclick="this.parentElement.remove()"
            style="margin-left:auto;background:none;border:none;cursor:pointer;font-size:1.1rem;">×</button>`;
  div.style.cssText =
    'display:flex;position:fixed;top:1rem;right:1rem;z-index:9999;min-width:280px;max-width:420px;';
  document.body.appendChild(div);
  setTimeout(() => { if (div.parentElement) div.remove(); }, 5000);
}

// ── Video comparison ──────────────────────────────────────────────────────────
function initComparison() {
  const orig = document.getElementById('origVideo');
  const summ = document.getElementById('summVideo');
  if (!orig || !summ) return;
  document.getElementById('syncPlay')?.addEventListener('click',  () => { orig.play();  summ.play();  });
  document.getElementById('syncPause')?.addEventListener('click', () => { orig.pause(); summ.pause(); });
}

// ── Confirm before delete ─────────────────────────────────────────────────────
document.addEventListener('click', e => {
  const btn = e.target.closest('[data-confirm]');
  if (!btn) return;
  if (!confirm(btn.dataset.confirm)) e.preventDefault();
});

// ── Init all ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initUpload();
  initCompressionCards();
  initComparison();

  const darkBtn = document.getElementById('darkToggle');
  if (darkBtn) {
    darkBtn.textContent = document.body.classList.contains('dark-mode') ? '☀️ Light' : '🌙 Dark';
  }

  document.querySelectorAll('.alert[data-auto]').forEach(a => {
    setTimeout(() => { if (a.parentElement) a.remove(); }, 4000);
  });

  document.querySelectorAll('.nav-item').forEach(link => {
    if (link.getAttribute('href') === window.location.pathname) {
      link.classList.add('active');
    }
  });
});
