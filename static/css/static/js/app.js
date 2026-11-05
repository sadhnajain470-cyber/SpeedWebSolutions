(() => {
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const lowPowerDevice = window.matchMedia('(max-width: 760px), (pointer: coarse)').matches;
  const installButton = document.getElementById('pwaInstall');
  let installPrompt;

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/service-worker.js').catch((error) => {
        console.error('CampusFlow service worker registration failed:', error);
      });
    });
  }

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    installPrompt = event;
    if (installButton) installButton.hidden = false;
  });

  installButton?.addEventListener('click', async () => {
    if (!installPrompt) return;
    installPrompt.prompt();
    await installPrompt.userChoice;
    installPrompt = null;
    installButton.hidden = true;
  });

  window.addEventListener('appinstalled', () => {
    installPrompt = null;
    if (installButton) installButton.hidden = true;
  });

  const canvas = document.getElementById('ambient-canvas');
  const ctx = canvas && canvas.getContext('2d');
  let particles = [], width = 0, height = 0, frame, lastFrame = 0;
  let hasUnsavedChanges = false, formIsSubmitting = false, reloadTimer;

  const sidebar = document.getElementById('workspaceSidebar');
  const menuToggle = document.getElementById('menuToggle');
  if (sidebar && menuToggle) {
    const closeMenu = () => {
      sidebar.classList.remove('is-open');
      menuToggle.setAttribute('aria-expanded', 'false');
      menuToggle.setAttribute('aria-label', 'Open navigation menu');
    };
    menuToggle.addEventListener('click', () => {
      const isOpen = sidebar.classList.toggle('is-open');
      menuToggle.setAttribute('aria-expanded', String(isOpen));
      menuToggle.setAttribute('aria-label', isOpen ? 'Close navigation menu' : 'Open navigation menu');
    });
    sidebar.querySelectorAll('.side-link').forEach((link) => link.addEventListener('click', closeMenu));
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMenu(); });
    document.addEventListener('click', (event) => {
      if (window.matchMedia('(max-width: 720px)').matches && !sidebar.contains(event.target)) closeMenu();
    });
    window.addEventListener('resize', () => { if (window.innerWidth > 720) closeMenu(); }, { passive: true });
  }

  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('input', () => { hasUnsavedChanges = true; }, { passive: true });
    form.addEventListener('change', () => { hasUnsavedChanges = true; }, { passive: true });
  });

  const liveStatus = document.getElementById('realtimeStatus');
  if (liveStatus && typeof window.io === 'function') {
    const liveSocket = window.io({ transports: ['websocket', 'polling'] });
    liveSocket.on('connect', () => {
      liveStatus.classList.remove('is-offline');
      liveStatus.classList.add('is-live');
      liveStatus.innerHTML = '<i></i> LIVE';
    });
    liveSocket.on('disconnect', () => {
      liveStatus.classList.remove('is-live');
      liveStatus.classList.add('is-offline');
      liveStatus.innerHTML = '<i></i> RECONNECTING';
    });
    liveSocket.on('connect_error', () => {
      liveStatus.classList.remove('is-live');
      liveStatus.classList.add('is-offline');
      liveStatus.innerHTML = '<i></i> OFFLINE';
    });
    liveSocket.on('data_changed', () => {
      if (formIsSubmitting) return;
      if (hasUnsavedChanges) {
        const stack = document.querySelector('.toast-stack') || document.body.appendChild(Object.assign(document.createElement('div'), { className: 'toast-stack' }));
        const toast = document.createElement('div');
        toast.className = 'flash info';
        toast.setAttribute('role', 'status');
        toast.innerHTML = '<span class="flash-dot"></span><span>New data is available. Finish or save your form to sync.</span>';
        stack.appendChild(toast);
        window.setTimeout(() => toast.remove(), 5000);
        return;
      }
      window.clearTimeout(reloadTimer);
      reloadTimer = window.setTimeout(() => window.location.reload(), 300);
    });
  } else if (liveStatus) {
    liveStatus.classList.add('is-offline');
    liveStatus.innerHTML = '<i></i> OFFLINE';
  }

  // A low-contrast neon particle field adds motion without competing with forms.
  if (ctx && !reduceMotion && !lowPowerDevice) {
    const resize = () => {
      const ratio = 1;
      width = window.innerWidth; height = window.innerHeight;
      canvas.width = width * ratio; canvas.height = height * ratio;
      canvas.style.width = `${width}px`; canvas.style.height = `${height}px`;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      const amount = Math.min(28, Math.max(14, Math.floor(width / 64)));
      particles = Array.from({ length: amount }, () => ({
        x: Math.random() * width, y: Math.random() * height,
        vx: (Math.random() - .5) * .22, vy: (Math.random() - .5) * .22,
        r: Math.random() * 1.7 + .5, hue: Math.random() > .5 ? 175 : 285,
        phase: Math.random() * Math.PI * 2
      }));
    };
    const draw = (time) => {
      frame = requestAnimationFrame(draw);
      if (time - lastFrame < 33) return;
      lastFrame = time;
      ctx.clearRect(0, 0, width, height);
      particles.forEach((p) => {
        p.x += p.vx; p.y += p.vy;
        if (p.x < -8) p.x = width + 8; if (p.x > width + 8) p.x = -8;
        if (p.y < -8) p.y = height + 8; if (p.y > height + 8) p.y = -8;
        const pulse = .35 + (Math.sin(time * .001 + p.phase) + 1) * .22;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `hsla(${p.hue}, 100%, 78%, ${pulse})`; ctx.fill();
      });
    };
    resize(); frame = requestAnimationFrame(draw);
    window.addEventListener('resize', resize, { passive: true });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) cancelAnimationFrame(frame);
      else if (!reduceMotion) frame = requestAnimationFrame(draw);
    });
  }

  // Stagger elements into view as the page scrolls.
  const revealItems = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window && !reduceMotion) {
    const observer = new IntersectionObserver((entries, current) => entries.forEach((entry) => {
      if (entry.isIntersecting) { entry.target.classList.add('visible'); current.unobserve(entry.target); }
    }), { threshold: .12 });
    revealItems.forEach((item, index) => { item.style.transitionDelay = `${Math.min(index * 100, 400)}ms`; observer.observe(item); });
  } else revealItems.forEach((item) => item.classList.add('visible'));

  // Ripple feedback, loading state and quick inline validation for forms.
  document.querySelectorAll('.btn').forEach((button) => button.addEventListener('click', (event) => {
    const rect = button.getBoundingClientRect(), size = Math.max(rect.width, rect.height);
    const ripple = document.createElement('span'); ripple.className = 'ripple';
    ripple.style.width = ripple.style.height = `${size}px`;
    ripple.style.left = `${event.clientX - rect.left - size / 2}px`; ripple.style.top = `${event.clientY - rect.top - size / 2}px`;
    button.appendChild(ripple); window.setTimeout(() => ripple.remove(), 700);
  }));
  document.querySelectorAll('form').forEach((form) => form.addEventListener('submit', (event) => {
    if (!form.checkValidity()) { event.preventDefault(); form.reportValidity(); return; }
    formIsSubmitting = true;
    hasUnsavedChanges = false;
    const button = event.submitter || form.querySelector('button[type="submit"]');
    if (button) { button.dataset.originalText = button.textContent.trim(); button.innerHTML = '<span class="spinner"></span> Saving…'; button.disabled = true; }
  }));

  const admissionForm = document.getElementById('admissionForm');
  if (admissionForm) {
    const requiredFields = [...admissionForm.querySelectorAll('[required]')];
    const progress = document.getElementById('formProgress');
    const progressLabel = document.getElementById('formProgressLabel');
    const updateProgress = () => {
      const completed = requiredFields.filter((field) => field.value.trim() !== '').length;
      const percent = requiredFields.length ? Math.round(completed * 100 / requiredFields.length) : 0;
      progress.style.width = `${percent}%`;
      progressLabel.textContent = `${percent}% complete`;
    };
    admissionForm.addEventListener('input', updateProgress);
    admissionForm.addEventListener('change', updateProgress);
    updateProgress();

    const password = document.getElementById('password');
    const toggle = document.getElementById('togglePassword');
    toggle?.addEventListener('click', () => {
      const showing = password.type === 'text';
      password.type = showing ? 'password' : 'text';
      toggle.textContent = showing ? 'SHOW' : 'HIDE';
      toggle.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
    });

    if (!reduceMotion && 'IntersectionObserver' in window) {
      const cinematicObserver = new IntersectionObserver((entries, observer) => entries.forEach((entry) => {
        if (entry.isIntersecting) { entry.target.classList.add('scene-visible'); observer.unobserve(entry.target); }
      }), { threshold: .15 });
      admissionForm.querySelectorAll('.cinema-in').forEach((section, index) => {
        section.style.setProperty('--scene-delay', `${index * 130}ms`);
        cinematicObserver.observe(section);
      });
    } else admissionForm.querySelectorAll('.cinema-in').forEach((section) => section.classList.add('scene-visible'));
  }

  // Dismissible, self-closing message toasts.
  document.querySelectorAll('.flash').forEach((toast) => {
    const close = () => { toast.classList.add('is-leaving'); window.setTimeout(() => toast.remove(), 360); };
    toast.querySelector('.flash-close')?.addEventListener('click', close);
    window.setTimeout(close, 6500);
  });

  // Draw the term score chart locally, so the page does not depend on a chart CDN.
  const scoreCanvas = document.getElementById('marksChart');
  if (scoreCanvas) {
    let scores = [];
    try { scores = JSON.parse(scoreCanvas.dataset.chart || '[]'); } catch (_) { scores = []; }
    const paintScores = () => {
      const rect = scoreCanvas.getBoundingClientRect();
      if (!rect.width || !scores.length) return;
      const rowHeight = 52, top = 28, bottom = 28;
      const logicalHeight = Math.max(150, top + scores.length * rowHeight + bottom);
      scoreCanvas.style.height = `${logicalHeight}px`;
      const ratio = Math.min(window.devicePixelRatio || 1, 2), width = rect.width;
      scoreCanvas.width = width * ratio; scoreCanvas.height = logicalHeight * ratio;
      const chart = scoreCanvas.getContext('2d'); chart.setTransform(ratio, 0, 0, ratio, 0, 0);
      const left = Math.min(132, Math.max(86, width * .25)), right = 42, plot = width - left - right;
      chart.clearRect(0, 0, width, logicalHeight);
      chart.font = '11px Space Grotesk, sans-serif'; chart.textBaseline = 'middle';
      [0, 25, 50, 75, 100].forEach((tick) => {
        const x = left + plot * tick / 100;
        chart.beginPath(); chart.moveTo(x, 16); chart.lineTo(x, logicalHeight - 12);
        chart.strokeStyle = 'rgba(220,205,255,.12)'; chart.setLineDash([3, 6]); chart.stroke(); chart.setLineDash([]);
        chart.fillStyle = '#aaa2c3'; chart.textAlign = 'center'; chart.fillText(String(tick), x, 8);
      });
      const elapsed = performance.now() - started, progress = reduceMotion ? 1 : Math.min(1, elapsed / 950);
      const eased = 1 - Math.pow(1 - progress, 3);
      scores.forEach((item, index) => {
        const y = top + index * rowHeight, value = Math.max(0, Math.min(100, Number(item.value) || 0));
        const label = String(item.label || 'Subject');
        chart.textAlign = 'right'; chart.fillStyle = '#e6ddf6';
        chart.fillText(label.length > 17 ? `${label.slice(0, 16)}…` : label, left - 12, y + 12);
        chart.fillStyle = 'rgba(255,255,255,.08)'; roundedRect(chart, left, y, plot, 22, 11); chart.fill();
        const fill = chart.createLinearGradient(left, 0, left + plot, 0);
        fill.addColorStop(0, '#ff61cb'); fill.addColorStop(.58, '#a873ff'); fill.addColorStop(1, '#56f3e0');
        chart.fillStyle = fill; chart.shadowColor = '#bd5cff88'; chart.shadowBlur = 14;
        roundedRect(chart, left, y, Math.max(2, plot * value / 100 * eased), 22, 11); chart.fill(); chart.shadowBlur = 0;
        chart.textAlign = 'left'; chart.fillStyle = '#ffffff'; chart.font = '700 12px Space Grotesk, sans-serif';
        chart.fillText(`${Math.round(value)}%`, left + plot + 10, y + 11);
      });
      if (progress < 1) requestAnimationFrame(paintScores);
    };
    const started = performance.now();
    requestAnimationFrame(paintScores);
    window.addEventListener('resize', () => requestAnimationFrame(paintScores), { passive: true });
  }

  function roundedRect(context, x, y, width, height, radius) {
    const r = Math.min(radius, width / 2, height / 2);
    context.beginPath(); context.moveTo(x + r, y); context.arcTo(x + width, y, x + width, y + height, r);
    context.arcTo(x + width, y + height, x, y + height, r); context.arcTo(x, y + height, x, y, r);
    context.arcTo(x, y, x + width, y, r); context.closePath();
  }

  // Small dashboard charts: attendance days and per-term score averages.
  document.querySelectorAll('[data-bar-chart]').forEach((barCanvas) => {
    let data = [];
    try { data = JSON.parse(barCanvas.dataset.barChart || '[]'); } catch (_) { data = []; }
    if (!data.length) return;
    const paint = () => {
      const bounds = barCanvas.getBoundingClientRect();
      if (!bounds.width) return;
      const rowHeight = 42, top = 25, bottom = 22, height = Math.max(190, top + rowHeight * data.length + bottom);
      barCanvas.style.height = `${height}px`;
      const dpr = Math.min(window.devicePixelRatio || 1, 2), width = bounds.width;
      barCanvas.width = width * dpr; barCanvas.height = height * dpr;
      const c = barCanvas.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, width, height);
      const left = Math.min(width * .31, 105), right = 56, plot = Math.max(50, width - left - right);
      const attendance = data.some((item) => Object.prototype.hasOwnProperty.call(item, 'total'));
      const max = attendance ? Math.max(1, ...data.map((item) => Number(item.total) || 0)) : 100;
      c.font = '10px Space Grotesk, sans-serif'; c.textBaseline = 'middle';
      data.forEach((item, i) => {
        const y = top + i * rowHeight, value = Math.max(0, Number(item.value) || 0);
        const count = Number(item.count ?? 1), label = String(item.label || '');
        c.textAlign = 'right'; c.fillStyle = '#d9d0ec'; c.fillText(label.length > 13 ? `${label.slice(0, 12)}…` : label, left - 10, y + 9);
        c.fillStyle = 'rgba(255,255,255,.075)'; roundedRect(c, left, y, plot, 17, 9); c.fill();
        if (count !== 0 || attendance) {
          const widthValue = plot * Math.min(value, max) / max;
          const grad = c.createLinearGradient(left, y, left + plot, y);
          grad.addColorStop(0, i % 2 ? '#55f1e0' : '#ff64c8'); grad.addColorStop(.55, '#a777ff'); grad.addColorStop(1, '#67f6df');
          c.fillStyle = grad; c.shadowColor = i % 2 ? '#54efe188' : '#ff54ca88'; c.shadowBlur = 10;
          if (widthValue > 0) { roundedRect(c, left, y, Math.max(3, widthValue), 17, 9); c.fill(); }
          c.shadowBlur = 0;
        }
        c.textAlign = 'left'; c.font = '700 10px Space Grotesk, sans-serif';
        c.fillStyle = count === 0 && !attendance ? '#867e9c' : '#f6edff';
        const result = attendance ? `${value}/${Number(item.total) || 0}` : count === 0 ? 'PENDING' : `${Math.round(value)}%`;
        c.fillText(result, left + plot + 9, y + 8); c.font = '10px Space Grotesk, sans-serif';
      });
    };
    paint(); window.addEventListener('resize', paint, { passive: true });
  });

  // Attendance donut chart and count-up numbers.
  document.querySelectorAll('.donut-chart, #attendanceChart').forEach((attendanceCanvas) => {
    const total = Number(attendanceCanvas.dataset.total) || 0;
    const present = Math.min(total, Number(attendanceCanvas.dataset.present) || 0);
    const ratio = total ? present / total : 0;
    const size = attendanceCanvas.clientWidth || 190, dpr = Math.min(window.devicePixelRatio || 1, 2);
    attendanceCanvas.width = size * dpr; attendanceCanvas.height = size * dpr;
    const ring = attendanceCanvas.getContext('2d'); ring.scale(dpr, dpr);
    const start = -Math.PI / 2, end = start + Math.PI * 2 * ratio;
    ring.lineWidth = 15; ring.lineCap = 'round';
    ring.beginPath(); ring.arc(size / 2, size / 2, size / 2 - 14, 0, Math.PI * 2);
    ring.strokeStyle = 'rgba(255,84,200,.26)'; ring.stroke();
    if (ratio > 0) {
      const gradient = ring.createLinearGradient(15, size - 15, size - 15, 15);
      gradient.addColorStop(0, '#ff5cc8'); gradient.addColorStop(.55, '#ad74ff'); gradient.addColorStop(1, '#55f5df');
      ring.beginPath(); ring.arc(size / 2, size / 2, size / 2 - 14, start, end);
      ring.strokeStyle = gradient; ring.shadowColor = '#b967ff'; ring.shadowBlur = 20; ring.stroke(); ring.shadowBlur = 0;
    }
  });

  document.querySelectorAll('[data-pie-chart]').forEach((pieCanvas) => {
    let items = [];
    try { items = JSON.parse(pieCanvas.dataset.pieChart || '[]'); } catch (_) { items = []; }
    const size = pieCanvas.clientWidth || 160, dpr = Math.min(window.devicePixelRatio || 1, 2);
    pieCanvas.width = size * dpr; pieCanvas.height = size * dpr;
    const c = pieCanvas.getContext('2d'); c.scale(dpr, dpr);
    const total = items.reduce((sum, item) => sum + (Number(item.value) || 0), 0);
    const cx = size / 2, cy = size / 2, radius = size / 2 - 8;
    c.lineWidth = 18;
    if (!total) {
      c.beginPath(); c.arc(cx, cy, radius, 0, Math.PI * 2); c.strokeStyle = 'rgba(255,255,255,.12)'; c.stroke();
    } else {
      let angle = -Math.PI / 2;
      items.forEach((item) => {
        const portion = (Number(item.value) || 0) / total;
        if (!portion) return;
        c.beginPath(); c.arc(cx, cy, radius, angle + .025, angle + portion * Math.PI * 2 - .025);
        c.strokeStyle = item.color || '#ae7aff'; c.lineCap = 'round'; c.shadowColor = item.color || '#ae7aff'; c.shadowBlur = 16; c.stroke(); c.shadowBlur = 0;
        angle += portion * Math.PI * 2;
      });
    }
  });

  document.querySelectorAll('[data-count-to]').forEach((node) => {
    const target = Number(node.dataset.countTo) || 0, started = performance.now();
    const count = (now) => {
      const p = reduceMotion ? 1 : Math.min(1, (now - started) / 900);
      node.textContent = String(Math.round(target * (1 - Math.pow(1 - p, 3))));
      if (p < 1) requestAnimationFrame(count);
    };
    requestAnimationFrame(count);
  });
})();
