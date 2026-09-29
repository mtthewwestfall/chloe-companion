/* Chloe — AI Companion · app shell logic.
   Chat home screen, onboarding-lite, sheets, demo voice-note playback. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const chatLog = $('chatLog'), msgInput = $('msgInput');
  const typingRow = $('typingRow'), toast = $('toast');

  let toastTimer = null;
  function showToast(text, ms = 2200) {
    toast.textContent = text;
    toast.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.add('hidden'), ms);
  }

  function timeNow() {
    return new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  function scrollBottom() {
    requestAnimationFrame(() => { chatLog.scrollTop = chatLog.scrollHeight; });
  }

  /* ---------- message bubbles ---------- */

  function addMsg(who, html, opts = {}) {
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + who;
    const b = document.createElement('div');
    b.className = opts.bubbleClass || 'bubble';
    b.innerHTML = html;
    wrap.appendChild(b);
    if (opts.caption) {
      const t = document.createElement('div');
      t.className = 'msg-time';
      t.textContent = opts.caption;
      wrap.appendChild(t);
    }
    chatLog.appendChild(wrap);
    scrollBottom();
    return wrap;
  }

  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  function addText(who, text) {
    addMsg(who, esc(text), { caption: who === 'her' ? 'Chloe · ' + timeNow() : timeNow() });
  }

  // Voice-note bubble UI. In demo mode the URL is `demo:voice` and we play a
  // soft locally-synthesized tone so the UI is previewable (clearly labeled).
  function addVoiceNote(url, caption) {
    const isDemo = url === 'demo:voice';
    const bars = Array.from({ length: 22 }, (_, i) => {
      const h = 6 + Math.round(Math.abs(Math.sin(i * 0.9)) * 18);
      return `<i style="height:${h}px"></i>`;
    }).join('');
    const wrap = addMsg('her', '', { bubbleClass: 'voice-bubble', caption: (caption || 'Voice note') + ' · ' + timeNow() });
    const b = wrap.firstChild;
    b.innerHTML = `
      <button class="voice-play" aria-label="Play voice note">▶</button>
      <div class="voice-waves">${bars}</div>
      <div class="voice-meta">${isDemo ? 'demo<br>0:03' : '0:12'}</div>`;
    const btn = b.querySelector('.voice-play');
    btn.addEventListener('click', () => {
      if (isDemo) { playDemoTone(b); return; }
      // Real server: stream the voice note from voice_note_url.
      const a = new Audio(url);
      b.classList.add('playing'); btn.textContent = '⏸';
      a.onended = () => { b.classList.remove('playing'); btn.textContent = '▶'; };
      a.play().catch(() => { b.classList.remove('playing'); btn.textContent = '▶'; });
    });
  }

  let audioCtx = null;
  function playDemoTone(bubbleEl) {
    // DEMO only: a soft warm swell so the voice-note UI can be previewed.
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const t0 = audioCtx.currentTime;
      [220, 277.18, 329.63].forEach((f) => {
        const o = audioCtx.createOscillator(), g = audioCtx.createGain();
        o.type = 'sine'; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t0);
        g.gain.exponentialRampToValueAtTime(0.08, t0 + 0.4);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + 2.6);
        o.connect(g).connect(audioCtx.destination);
        o.start(t0); o.stop(t0 + 2.7);
      });
      bubbleEl.classList.add('playing');
      setTimeout(() => bubbleEl.classList.remove('playing'), 2700);
    } catch (e) { showToast('Audio not available here'); }
  }

  // Photo-drop bubble UI. Demo renders a tasteful night-window scene
  // placeholder (real photo curation happens later with the user).
  function addPhotoDrop(url, caption) {
    const wrap = addMsg('her', '', { bubbleClass: 'photo-bubble', caption: 'Chloe · ' + timeNow() });
    const b = wrap.firstChild;
    const isDemo = url === 'demo:photo';
    b.innerHTML = `
      <div class="photo-scene">
        <div class="photo-lamp"></div>
        <svg viewBox="0 0 240 90" preserveAspectRatio="xMidYMax slice">
          <g fill="#05081a">
            <rect x="0" y="40" width="34" height="50"/><rect x="38" y="22" width="26" height="68"/>
            <rect x="68" y="46" width="30" height="44"/><rect x="102" y="14" width="24" height="76"/>
            <rect x="130" y="34" width="32" height="56"/><rect x="166" y="24" width="22" height="66"/>
            <rect x="192" y="44" width="28" height="46"/>
          </g>
          <g fill="#e8a94c" opacity="0.9">
            <rect x="44" y="32" width="5" height="7"/><rect x="108" y="26" width="5" height="7"/>
            <rect x="136" y="46" width="5" height="7"/><rect x="172" y="36" width="5" height="7"/>
          </g>
          <circle cx="205" cy="20" r="11" fill="#f4d9a0" opacity="0.9"/>
          <circle cx="200" cy="17" r="9" fill="#131c44" opacity="0.95"/>
        </svg>
      </div>
      <div class="photo-caption">
        ${isDemo ? '<span class="demo-tag">DEMO PHOTO</span><br>' : ''}
        ${esc(caption || '')}
      </div>`;
  }

  /* ---------- chat flow ---------- */

  let sending = false;

  async function sendMessage(text) {
    text = (text || '').trim();
    if (!text || sending) return;
    sending = true;
    addText('him', text);
    msgInput.value = '';
    vidTyping();
    lastUserMsgAt = Date.now();
    consecutiveProactive = 0;
    typingRow.classList.remove('hidden');
    scrollBottom();

    // Let her "read" it for a beat — never cut her off mid-thought.
    await new Promise((r) => setTimeout(r, 900 + Math.random() * 900));

    try {
      const res = await ApiClient.chat(text);
      typingRow.classList.add('hidden');
      vidIdle();
      if (res.voice_note_url) {
        addVoiceNote(res.voice_note_url, res.reply_text);
      } else {
        addText('her', res.reply_text || '…');
      }
      if (res.photo_url) addPhotoDrop(res.photo_url, res.reply_text);
      if (typeof res.messages_remaining === 'number') $('statMsgs').textContent = res.messages_remaining;
      if (typeof res.voice_notes_remaining === 'number') $('statVoice').textContent = res.voice_notes_remaining;
    } catch (e) {
      typingRow.classList.add('hidden');
      vidIdle();
      addText('her', 'hmm, lost you for a second. say that again?');
    }
    sending = false;
  }

  $('sendBtn').addEventListener('click', () => sendMessage(msgInput.value));
  msgInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendMessage(msgInput.value);
  });

  // Mic: voice-note sending unlocks with the real server; demo explains that.
  $('micBtn').addEventListener('click', () => {
    showToast(ApiClient.isConnected()
      ? 'Hold to record (coming with the companion server)'
      : 'Voice messages unlock when the companion server is connected');
  });


  /* ---------- FaceTime video tile ---------- */
  const MEDIA_BASE = 'https://keyhole-latest-production.up.railway.app/media/files/';
  const VID_IDLE = 'chloe_3bcbf88d987b7621.mp4';
  const VID_TYPING = 'chloe_6f26366653c891fc.mp4';
  const VID_ACTS = [
    'chloe_8dc35b0d286f75c8.mp4', 'chloe_d7f526d8e173b8c1.mp4', 'chloe_b6e63f643b5d65b8.mp4',
    'chloe_f0444b783021a49b.mp4', 'chloe_af1ad9f4bdd4e640.mp4', 'chloe_d7916e4c02a3623d.mp4',
    'chloe_fb752057230ea48e.mp4', 'chloe_b7892a8396f0906b.mp4', 'chloe_2a699be08f3afa13.mp4',
    'chloe_951d4f3caa7d7c8b.mp4', 'chloe_9d1a71eaa989e464.mp4', 'chloe_e29224a69d9b8483.mp4',
    'chloe_cf36372b31ad8399.mp4', 'chloe_4e7fb93e53370b6a.mp4', 'chloe_526ff559f269109a.mp4',
    'chloe_da384645830fac4e.mp4', 'chloe_1f071cf867c8a3f0.mp4', 'chloe_e21c51182afc8381.mp4',
    'chloe_3cc3f88cc0051442.mp4', 'chloe_7fbf98e99a93841c.mp4', 'chloe_dbd46d293b36133e.mp4',
    'chloe_8727c4518ffea2c3.mp4', 'chloe_6ede839a2eb89a8e.mp4', 'chloe_7b45d3e668715e56.mp4',
    'chloe_1266ef81c5771819.mp4',
  ];
  const chloeVideo = $('chloeVideo'), videoTapHint = $('videoTapHint');
  let vidMode = 'idle';   // idle | typing | activity
  let actIdx = Math.floor(Math.random() * VID_ACTS.length);
  let idleTimer = null;

  function vidShowTapHint() { videoTapHint.classList.remove('hidden'); }
  function vidHideTapHint() { videoTapHint.classList.add('hidden'); }

  function vidPlay(src, loop) {
    const url = MEDIA_BASE + src;
    chloeVideo.loop = !!loop;
    if (chloeVideo.dataset.src !== url) {
      chloeVideo.dataset.src = url;
      chloeVideo.src = url;
    }
    const pr = chloeVideo.play();
    if (pr && pr.then) pr.then(vidHideTapHint).catch(vidShowTapHint);
  }
  function vidIdle() {
    vidMode = 'idle';
    vidPlay(VID_IDLE, true);
    clearTimeout(idleTimer);
    // One activity at a time during idle, then back to idle.
    idleTimer = setTimeout(() => {
      if (vidMode !== 'idle' || document.hidden) { vidIdle(); return; }
      vidMode = 'activity';
      actIdx = (actIdx + 1) % VID_ACTS.length;
      vidPlay(VID_ACTS[actIdx], false);
    }, 45000);
  }
  function vidTyping() { vidMode = 'typing'; clearTimeout(idleTimer); vidPlay(VID_TYPING, true); }
  chloeVideo.addEventListener('ended', () => { if (vidMode === 'activity') vidIdle(); });
  chloeVideo.addEventListener('error', vidShowTapHint, true);
  videoTapHint.addEventListener('click', (e) => { e.stopPropagation(); vidPlay(chloeVideo.dataset.src ? chloeVideo.dataset.src.slice(MEDIA_BASE.length) : VID_IDLE, vidMode !== 'activity'); });
  // iOS: retry autoplay on first touch and when the tab becomes visible.
  document.addEventListener('touchend', function once() {
    document.removeEventListener('touchend', once);
    if (chloeVideo.paused) vidPlay(chloeVideo.dataset.src ? chloeVideo.dataset.src.slice(MEDIA_BASE.length) : VID_IDLE, vidMode !== 'activity');
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && chloeVideo.paused) vidIdle();
  });

  /* ---------- proactive: she takes the lead when he's quiet ---------- */
  const QUIET_MS = 90000;   // 90s of silence before she reaches out
  let lastUserMsgAt = Date.now();
  let consecutiveProactive = 0;
  const PROACTIVE_LINES = [
    (n) => 'hey. still with me?',
    (n) => 'you went quiet — everything okay?',
    (n) => `i'm still here, ${n}. talk to me.`,
    (n) => 'mm. i miss your messages. come back?',
    (n) => `don't leave me hanging, ${n}. what's on your mind?`,
  ];
  function armQuietNudge() {
    setInterval(() => {
      if (localStorage.getItem('chloe_proactive') === 'off') return;
      if (document.hidden || sending) return;
      if (Date.now() - lastUserMsgAt < QUIET_MS) return;
      if (consecutiveProactive >= 2) return;   // never pushy: max two in a row
      consecutiveProactive++;
      lastUserMsgAt = Date.now();
      const nm = localStorage.getItem('chloe_name') || 'you';
      const line = PROACTIVE_LINES[Math.floor(Math.random() * PROACTIVE_LINES.length)](nm);
      typingRow.classList.remove('hidden'); scrollBottom(); vidTyping();
      setTimeout(() => {
        typingRow.classList.add('hidden');
        addText('her', line);
        vidIdle();
      }, 1600);
    }, 15000);
  }

  /* ---------- onboarding-lite ---------- */

  function showOnboarding() { $('onboarding').classList.remove('hidden'); $('app').classList.add('hidden'); }
  function showApp() { $('onboarding').classList.add('hidden'); $('app').classList.remove('hidden'); }

  function finishOnboarding() {
    const name = $('nameInput').value.trim().slice(0, 24) || 'friend';
    localStorage.setItem('chloe_name', name);
    showApp();
    bootChat(true);
  }
  $('nameGo').addEventListener('click', finishOnboarding);
  $('nameInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') finishOnboarding(); });
  $('resetNameBtn').addEventListener('click', () => {
    closeSheet('profileSheet');
    showOnboarding();
    $('nameInput').value = localStorage.getItem('chloe_name') || '';
    $('nameInput').focus();
  });

  /* ---------- sheets ---------- */

  function openSheet(id) { $(id).classList.remove('hidden'); }
  function closeSheet(id) { $(id).classList.add('hidden'); }
  document.querySelectorAll('.sheet-close').forEach((btn) => {
    btn.addEventListener('click', () => closeSheet(btn.dataset.close));
  });
  document.querySelectorAll('.sheet').forEach((s) => {
    s.addEventListener('click', (e) => { if (e.target === s) s.classList.add('hidden'); });
  });

  $('profileBtn').addEventListener('click', async () => {
    openSheet('profileSheet');
    const me = await ApiClient.me();
    $('profileName').textContent = me.name || localStorage.getItem('chloe_name') || 'You';
    $('statMsgs').textContent = me.messages_remaining;
    $('statVoice').textContent = me.voice_notes_remaining;
    // Tier card + mode pill reflect the real connection state, not the demo fallback.
    const live = ApiClient.isConnected();
    $('tierName').textContent = live && me.tier
      ? 'Companion — ' + me.tier.charAt(0).toUpperCase() + me.tier.slice(1)
      : 'Companion — Demo';
    $('tierSub').textContent = live ? 'live on companion server' : 'demo tier · not billed';
    $('modePill').textContent = live ? 'Live' : 'Demo mode';
    $('modeBlurb').textContent = live ? 'real memory on the companion server.' : 'connect the companion server for real memory.';
    updateConnLine();
    renderPlans(me.tier, live);
  });

  // Stripe fulfillment needs to know WHO paid: every checkout URL gets
  // ?client_reference_id=<user_id> so the server webhook can credit the
  // right account's banks (and the monthly Keyhole show).
  function withClientRef(url) {
    try {
      const uid = ApiClient.userId();
      if (!uid) return url;
      return url + (url.includes('?') ? '&' : '?') + 'client_reference_id=' + encodeURIComponent(uid);
    } catch (e) {
      return url;
    }
  }

  // Plans: subscribe/upgrade cards from the live server (names, prices,
  // benefits, Stripe checkout links). Rendered once per profile-sheet open;
  // buttons open the checkout link in a new tab.
  async function renderPlans(currentTier, live) {
    const list = $('planList');
    const data = live ? await ApiClient.tiers() : null;
    const plans = data && data.plans;
    if (!plans || !plans.length) {
      list.innerHTML = '<div class="sheet-sub">Plans appear here once you\'re connected to the companion server.</div>';
      return;
    }
    list.innerHTML = '';
    const promo = document.createElement('div');
    promo.className = 'promo-banner';
    promo.innerHTML = 'Launch special: buy one month, get the second free \u2014 enter code <b>BOGO2</b> at checkout.';
    list.appendChild(promo);
    plans.forEach((p) => {
      const card = document.createElement('div');
      card.className = 'plan-card' + (p.key === currentTier ? ' current' : '');
      const benefits = [
        p.messages_per_month.toLocaleString('en-US') + ' messages / month',
        p.voice_notes_unconfirmed
          ? 'Voice notes — count being finalized'
          : (p.voice_notes_per_month > 0
              ? p.voice_notes_per_month + ' voice notes / month'
              : 'No voice notes'),
      ];
      if (p.photo_drops) benefits.push('Photo drops');
      if (p.proactive) benefits.push('Proactive check-ins');
      card.innerHTML = `
        <div class="plan-head"><div class="plan-name">${esc(p.name)}</div><div class="plan-price">$${p.price_usd.toFixed(2)}<span>/mo</span></div></div>
        <ul class="plan-benefits">${benefits.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>`;
      const btn = document.createElement('button');
      if (p.key === currentTier) {
        btn.className = 'btn-ghost'; btn.textContent = 'Current plan'; btn.disabled = true;
      } else {
        btn.className = 'btn-primary';
        btn.textContent = 'Subscribe · $' + p.price_usd.toFixed(2) + '/mo';
        btn.addEventListener('click', () => window.open(withClientRef(p.checkout_url), '_blank', 'noopener'));
      }
      card.appendChild(btn);
      list.appendChild(card);
    });
  }

  $('proactiveToggle').addEventListener('change', (e) => {
    localStorage.setItem('chloe_proactive', e.target.checked ? 'on' : 'off');
    showToast(e.target.checked ? 'She’ll check in on you' : 'Check-ins paused');
  });

  // Shows: list from contract, ticket buttons deep-link to WEBSITE_URL.
  $('showsBtn').addEventListener('click', async () => {
    openSheet('showsSheet');
    const list = $('showsList');
    list.innerHTML = '<div class="sheet-sub">Loading…</div>';
    try {
      const data = await ApiClient.shows();
      list.innerHTML = '';
      (data.shows || []).forEach((s) => {
        const when = new Date(s.starts_at).toLocaleString([], {
          weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
        });
        const row = document.createElement('div');
        row.className = 'show-item';
        row.innerHTML = `<div><div class="show-title">${esc(s.title)}</div><div class="show-when">${esc(when)}</div></div>`;
        const btn = document.createElement('button');
        btn.className = 'btn-ghost'; btn.textContent = 'Get ticket';
        btn.addEventListener('click', () => window.open(s.ticket_url || CONFIG.WEBSITE_URL, '_blank', 'noopener'));
        row.appendChild(btn);
        list.appendChild(row);
      });
      if (!data.shows || !data.shows.length) list.innerHTML = '<div class="sheet-sub">No shows scheduled right now.</div>';
    } catch (e) {
      list.innerHTML = '<div class="sheet-sub">Couldn’t load shows.</div>';
    }
  });
  $('openSiteBtn').addEventListener('click', () => window.open(CONFIG.WEBSITE_URL, '_blank', 'noopener'));

  /* ---------- connection status ---------- */

  function updateConnLine() {
    const el = $('connStatus');
    if (ApiClient.isConnected()) {
      el.innerHTML = '<span class="ok">connected to companion server</span>';
    } else {
      el.innerHTML = '<span class="bad">demo mode</span>';
    }
  }
  ApiClient.onStatus(updateConnLine);
  $('reconnectBtn').addEventListener('click', async () => {
    $('connStatus').textContent = 'checking…';
    await ApiClient.probe();
    updateConnLine();
    showToast(ApiClient.isConnected() ? 'Connected to the companion server' : 'Still in demo mode');
  });

  /* ---------- boot ---------- */

  function dayDividerIfNeeded() {
    const today = new Date().toDateString();
    const last = localStorage.getItem('chloe_last_day');
    if (last !== today) {
      localStorage.setItem('chloe_last_day', today);
      const d = document.createElement('div');
      d.className = 'day-divider';
      d.textContent = new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
      chatLog.appendChild(d);
    }
  }

  async function bootChat(isFirstHello) {
    vidIdle();
    armQuietNudge();
    dayDividerIfNeeded();
    $('apiBaseLine').textContent = 'api: ' + CONFIG.API_BASE_URL;
    $('proactiveToggle').checked = localStorage.getItem('chloe_proactive') !== 'off';

    // Probe for the real server in the background; demo works regardless.
    ApiClient.probe().then(updateConnLine);
    updateConnLine();

    const returning = !isFirstHello && !!localStorage.getItem('chloe_seen');
    localStorage.setItem('chloe_seen', '1');

    await new Promise((r) => setTimeout(r, 600));
    typingRow.classList.remove('hidden'); scrollBottom();
    await new Promise((r) => setTimeout(r, 1400));
    typingRow.classList.add('hidden');

    if (isFirstHello) {
      const name = localStorage.getItem('chloe_name') || 'friend';
      addText('her', `hey ${name}. i'm chloe. this is my little corner of the night — come talk to me anytime.`);
    } else if (returning) {
      addText('her', MockEngine.returnOpener());
    }
  }

  // Go.
  if (localStorage.getItem('chloe_name')) {
    showApp();
    bootChat(false);
  } else {
    showOnboarding();
  }
})();
