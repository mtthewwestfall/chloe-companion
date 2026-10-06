/* your perfect girlfriend · AiMessage app shell.
   Conversation list, chat view, onboarding-lite, sheets, demo voice-note playback. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const chatLog = $('chatLog'), msgInput = $('msgInput');
  const typingRow = $('typingRow'), toast = $('toast');

  /* ---------- characters ---------- */
  const AVATAR_BASE = 'https://keyhole-latest-production.up.railway.app';
  const MEDIA_BASE = 'https://keyhole-latest-production.up.railway.app';
  const CHARACTERS = [
    { id: 'isabella', name: 'Isabella',
      avatar: AVATAR_BASE + '/media/files/isabella_6c8d820baed84544.png',
      videos: [
        MEDIA_BASE + '/media/files/isabella_971b3e57d80692ee.mp4',  // main
        MEDIA_BASE + '/media/files/isabella_b95153aa38c2db91.mp4',  // activity
        MEDIA_BASE + '/media/files/isabella_2313f8b06e00d8a9.mp4',  // typing
      ],
      desc: 'Bold, charming, and playfully confident.',
      bestFor: 'Flirty banter, late-night laughs, and someone who keeps you on your toes.' },
    { id: 'chloe', name: 'Chloe',
      avatar: AVATAR_BASE + '/media/files/chloe_a6647f142096672f.jpg',
      videos: [
        MEDIA_BASE + '/media/files/chloe_6f26366653c891fc.mp4',  // main
      ],
      desc: 'Bright, radiant, and effortlessly magnetic.',
      bestFor: 'Unwinding after a long day, deep late-night chats, and feel-good conversations.' },
    { id: 'mara', name: 'Mara',
      avatar: AVATAR_BASE + '/media/files/mara_bcd8ffe5d877fead.png',
      videos: [
        MEDIA_BASE + '/media/files/mara_d5ec0d3886014ae4.mp4',  // main
        MEDIA_BASE + '/media/files/mara_09578b3e20e2e7fa.mp4',  // typing
      ],
      desc: 'Sweet, witty, and subtly captivating.',
      bestFor: 'Honest check-ins, shared secrets, and effortless chemistry.' },
  ];
  let currentGirl = CHARACTERS[1];
  let avatarCycleTimer = null;

  function startAvatarCycle() {
    const video = $('chatAvatarVideo');
    const clips = currentGirl.videos || [];
    if (!clips.length) return;
    let idx = 0;
    const play = () => {
      video.src = clips[idx % clips.length];
      video.play().catch(() => {});
      idx++;
    };
    play();
    clearInterval(avatarCycleTimer);
    // Cycle every 12s: main -> activity -> typing -> main...
    avatarCycleTimer = setInterval(play, 12000);
  }

  function renderGirlCards() {
    const wrap = $('girlCards');
    wrap.innerHTML = '';
    CHARACTERS.forEach((c) => {
      const el = document.createElement('div');
      el.className = 'girl-card';
      el.innerHTML = `
        <img class="girl-photo" src="${c.avatar}" alt="${c.name}" loading="lazy">
        <div class="girl-info">
          <div class="girl-name">${c.name}</div>
          <span class="online-dot">● Online Now</span>
          <div class="girl-desc">${c.desc}<br><b>Best for:</b> ${c.bestFor}</div>
          <button class="start-chat-btn">[ Chat on Whispr ]</button>
        </div>`;
      el.addEventListener('click', () => openChat(c.id));
      wrap.appendChild(el);
    });
  }

  function openChat(girlId) {
    const girl = CHARACTERS.find((c) => c.id === girlId) || CHARACTERS[1];
    currentGirl = girl;
    $('chatName').textContent = girl.name;
    startAvatarCycle();
    // day divider + encryption note
    chatLog.innerHTML = '';
    const dayDiv = document.createElement('div');
    dayDiv.className = 'day-divider';
    dayDiv.textContent = 'Today ' + new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    chatLog.appendChild(dayDiv);
    const enc = document.createElement('div');
    enc.className = 'encryption-note';
    enc.textContent = `Whispr AiMessage with ${girl.name} · End-to-End Encrypted`;
    chatLog.appendChild(enc);
    $('landingView').classList.add('hidden');
    $('chatView').classList.remove('hidden');
    bootChat(false);
  }

  $('backBtn').addEventListener('click', () => {
    $('chatView').classList.add('hidden');
    $('landingView').classList.remove('hidden');
    renderGirlCards();
  });

  function showListView() {
    $('onboarding').classList.add('hidden');
    $('app').classList.remove('hidden');
    $('chatView').classList.add('hidden');
    $('landingView').classList.remove('hidden');
    renderGirlCards();
    loadPricing();
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
      const res = await ApiClient.chat(text, currentGirl.id);
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

  // Mic button removed in AiMessage redesign — voice notes via preset picker.

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

  /* ---------- preset videos ---------- */

  const PRESETS = {
    chloe: [{ name: 'love', count: 6, emoji: '💕' }],
    mara: [{ name: 'love', count: 1, emoji: '💕' }],
    isabella: [{ name: 'love', count: 1, emoji: '💕' }],
  };

  $('plusBtn').addEventListener('click', () => {
    const list = $('presetList');
    list.innerHTML = '';
    const presets = PRESETS[currentGirl.id] || [];
    if (!presets.length) {
      list.innerHTML = '<div class="sheet-sub">No presets yet for ' + currentGirl.name + '.</div>';
    }
    presets.forEach((p) => {
      const el = document.createElement('div');
      el.className = 'preset-item';
      el.innerHTML = `
        <div class="preset-thumb">${p.emoji}</div>
        <div><div class="preset-name">${p.name}</div>
        <div class="preset-count">${p.count} video${p.count === 1 ? '' : 's'}</div></div>`;
      el.addEventListener('click', () => {
        closeSheet('presetSheet');
        sendPresetVideo(p.name);
      });
      list.appendChild(el);
    });
    openSheet('presetSheet');
  });

  async function sendPresetVideo(presetName) {
    addText('him', `send me a ${presetName} video`);
    typingRow.classList.remove('hidden');
    scrollBottom();
    await new Promise((r) => setTimeout(r, 1200));
    typingRow.classList.add('hidden');
    try {
      const res = await ApiClient.requestPreset(currentGirl.id, presetName);
      if (res.video_url) {
        addPresetMediaBubble(res.video_url, res.media_type || 'video', presetName);
      } else {
        addText('her', 'let me find that for you… one sec');
      }
    } catch (e) {
      addText('her', 'hmm, couldn\'t pull that one up. try again?');
    }
  }

  function addPresetMediaBubble(url, mediaType, presetName) {
    const wrap = addMsg('her', '', {
      bubbleClass: 'photo-bubble',
      caption: currentGirl.name + ' · ' + timeNow(),
    });
    const b = wrap.firstChild;
    const mediaHtml = mediaType === 'image'
      ? `<img src="${url}" style="width:100%;display:block;" alt="${presetName}">`
      : `<video src="${url}" muted loop playsinline autoplay preload="metadata"
           style="width:100%;display:block;cursor:pointer;"
           onclick="this.muted=!this.muted;this.play();"></video>`;
    b.innerHTML = `
      <div class="photo-scene" style="height:auto;min-height:200px;">${mediaHtml}</div>
      <div class="photo-caption">💕 ${presetName} <span style="font-size:11px;color:var(--sub);">· tap for sound</span></div>`;
  }

  /* ---------- pricing ---------- */

  async function loadPricing() {
    try {
      const res = await fetch(CONFIG.API_BASE_URL + '/api/v1/tiers');
      const data = await res.json();
      const wrap = $('pricingCards');
      wrap.innerHTML = '';
      (data.plans || []).forEach((p) => {
        if (p.key === 'trial') return; // trial shown separately
        const el = document.createElement('div');
        el.className = 'price-card';
        el.innerHTML = `
          <div><div class="plan-name">${p.name}</div>
          <div class="plan-msgs">${p.messages_per_month.toLocaleString()} messages/mo</div></div>
          <div class="plan-price">$${p.price_usd}<span>/mo</span></div>`;
        el.style.cursor = 'pointer';
        el.addEventListener('click', () => {
          if (p.checkout_url) window.open(p.checkout_url, '_blank');
        });
        wrap.appendChild(el);
      });
    } catch (e) { /* pricing unavailable offline */ }
  }

  /* ---------- referral ---------- */

  async function openReferral() {
    openSheet('referralSheet');
    try {
      const res = await fetch(CONFIG.API_BASE_URL + '/api/v1/referral/me', {
        headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('whispr_token') || '') }
      });
      const data = await res.json();
      $('refLinkInput').value = data.referral_link || '';
      const active = data.active_referrals || 0;
      $('refProgressFill').style.width = Math.min(100, (active / 6) * 100) + '%';
      $('refProgressText').textContent = `${active} / 6 friends` +
        (active >= 6 ? ' — 1 Year VIP unlocked!' : active >= 1 ? ` — ${6 - active} more for a full year!` : '');
    } catch (e) {
      $('refProgressText').textContent = 'Sign in to get your referral link';
    }
  }

  $('referralBtn').addEventListener('click', () => { closeSheet('profileSheet'); openReferral(); });
  $('refCopyBtn').addEventListener('click', () => {
    $('refLinkInput').select();
    document.execCommand('copy');
    showToast('Link copied!');
  });
  $('refShareTelegram').addEventListener('click', () => {
    const link = $('refLinkInput').value;
    window.open('https://t.me/share/url?url=' + encodeURIComponent(link) +
      '&text=' + encodeURIComponent("I've been testing this new AI companion app called Whispr. Use my link for free VIP access:"), '_blank');
  });
  $('refShareX').addEventListener('click', () => {
    const link = $('refLinkInput').value;
    window.open('https://twitter.com/intent/tweet?text=' + encodeURIComponent("Check out Whispr on Lockeddoor.ai: " + link), '_blank');
  });
  $('refShareReddit').addEventListener('click', () => {
    const link = $('refLinkInput').value;
    window.open('https://www.reddit.com/submit?url=' + encodeURIComponent(link) + '&title=' + encodeURIComponent("Whispr AI Girlfriend - free VIP with referral"), '_blank');
  });

  // Check for ?ref= in URL at boot — store for signup
  (function checkRefParam() {
    const params = new URLSearchParams(window.location.search);
    const ref = params.get('ref');
    if (ref) localStorage.setItem('whispr_ref', ref);
  })();

  /* ---------- auth ---------- */

  function getToken() { return localStorage.getItem('whispr_token') || ''; }
  function setToken(t) { localStorage.setItem('whispr_token', t); }
  function isLoggedIn() { return !!getToken(); }

  function showAuth() { openSheet('authSheet'); }

  $('authTabSignup').addEventListener('click', () => {
    $('authTabSignup').classList.add('active');
    $('authTabLogin').classList.remove('active');
    $('authSignupForm').classList.remove('hidden');
    $('authLoginForm').classList.add('hidden');
  });
  $('authTabLogin').addEventListener('click', () => {
    $('authTabLogin').classList.add('active');
    $('authTabSignup').classList.remove('active');
    $('authLoginForm').classList.remove('hidden');
    $('authSignupForm').classList.add('hidden');
  });

  function authError(msg) {
    $('authError').textContent = msg;
    $('authError').classList.remove('hidden');
  }

  $('signupBtn').addEventListener('click', async () => {
    $('authError').classList.add('hidden');
    const name = $('signupName').value.trim();
    const email = $('signupEmail').value.trim();
    const password = $('signupPassword').value;
    if (!email || !password) return authError('Enter email and password');
    if (password.length < 8) return authError('Password must be 8+ characters');
    try {
      const res = await fetch(CONFIG.API_BASE_URL + '/api/v1/auth/signup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, name })
      });
      const data = await res.json();
      if (!res.ok) return authError(data.detail || 'Signup failed');
      // Track referral if ?ref= was present
      const refCode = localStorage.getItem('whispr_ref');
      if (refCode) {
        // Will link after login
        localStorage.setItem('whispr_pending_ref', refCode);
      }
      $('authVerifyMsg').classList.remove('hidden');
      $('authSignupForm').classList.add('hidden');
    } catch (e) {
      authError('Network error — try again');
    }
  });

  $('loginBtn').addEventListener('click', async () => {
    $('authError').classList.add('hidden');
    const email = $('loginEmail').value.trim();
    const password = $('loginPassword').value;
    try {
      const res = await fetch(CONFIG.API_BASE_URL + '/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password })
      });
      const data = await res.json();
      if (!res.ok) return authError(data.detail || 'Login failed');
      setToken(data.token);
      // Track pending referral
      const refCode = localStorage.getItem('whispr_pending_ref') || localStorage.getItem('whispr_ref');
      if (refCode) {
        try {
          await fetch(CONFIG.API_BASE_URL + '/api/v1/referral/track', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + data.token },
            body: JSON.stringify({ referral_code: refCode })
          });
          localStorage.removeItem('whispr_pending_ref');
        } catch (e) {}
      }
      closeSheet('authSheet');
      showToast('Welcome back!');
      updateBalance();
    } catch (e) {
      authError('Network error — try again');
    }
  });

  async function updateBalance() {
    try {
      const me = await ApiClient.me();
      if (typeof me.messages_remaining === 'number') $('statMsgs').textContent = me.messages_remaining;
      if (typeof me.voice_notes_remaining === 'number') $('statVoice').textContent = me.voice_notes_remaining;
    } catch (e) {}
  }

  // Show auth on boot if not logged in
  if (!isLoggedIn()) {
    setTimeout(showAuth, 800);
  } else {
    updateBalance();
  }

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
  $('videoCallBtn').addEventListener('click', async () => {
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
