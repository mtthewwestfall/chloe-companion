/* your perfect girlfriend · AiMessage app shell.
   Conversation list, chat view, onboarding-lite, sheets, demo voice-note playback. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const chatLog = $('chatLog'), msgInput = $('msgInput');
  const typingRow = $('typingRow'), toast = $('toast');

  /* ---------- characters ---------- */
  const CHARACTERS = [
    { id: 'chloe', name: 'Chloe', color: '#e8a94c', initial: 'C' },
    { id: 'mara', name: 'Mara', color: '#c98f3a', initial: 'M' },
    { id: 'isabella', name: 'Isabella', color: '#8e6fb8', initial: 'I' },
  ];
  let currentGirl = CHARACTERS[0];

  function renderConvoList() {
    const items = $('convoItems');
    items.innerHTML = '';
    CHARACTERS.forEach((c) => {
      const el = document.createElement('div');
      el.className = 'convo-item';
      el.innerHTML = `
        <div class="convo-avatar" style="background:${c.color}">${c.initial}</div>
        <div class="convo-main">
          <div class="convo-name">${c.name}</div>
          <div class="convo-preview">AiMessage</div>
        </div>
        <div class="convo-time">›</div>`;
      el.addEventListener('click', () => openChat(c.id));
      items.appendChild(el);
    });
  }

  function openChat(girlId) {
    const girl = CHARACTERS.find((c) => c.id === girlId) || CHARACTERS[0];
    currentGirl = girl;
    $('chatName').textContent = girl.name;
    $('chatAvatar').style.background = girl.color;
    $('chatAvatar').textContent = '';
    // Clear and reboot chat for this girl
    chatLog.innerHTML = '';
    $('convoList').classList.add('hidden');
    $('chatView').classList.remove('hidden');
    bootChat(false);
  }

  $('backBtn').addEventListener('click', () => {
    $('chatView').classList.add('hidden');
    $('convoList').classList.remove('hidden');
    renderConvoList();
  });

  function showListView() {
    $('onboarding').classList.add('hidden');
    $('app').classList.remove('hidden');
    $('chatView').classList.add('hidden');
    $('convoList').classList.remove('hidden');
    renderConvoList();
  }

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
    addMsg(who, esc(text), { caption: who === 'her' ? currentGirl.name + ' · ' + timeNow() : timeNow() });
  }

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

  function addPhotoDrop(url, caption) {
    const wrap = addMsg('her', '', { bubbleClass: 'photo-bubble', caption: currentGirl.name + ' · ' + timeNow() });
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
    typingRow.classList.remove('hidden');
    scrollBottom();

    // Let her "read" it for a beat — never cut her off mid-thought.
    await new Promise((r) => setTimeout(r, 900 + Math.random() * 900));

    try {
      const res = await ApiClient.chat(text);
      typingRow.classList.add('hidden');
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

  /* ---------- onboarding-lite ---------- */

  function showOnboarding() { $('onboarding').classList.remove('hidden'); $('app').classList.add('hidden'); }
  function showApp() { showListView(); }

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
  });

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
      addText('her', `hey ${name}. i'm ${currentGirl.name.toLowerCase()}. this is my little corner — come talk to me anytime.`);
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
