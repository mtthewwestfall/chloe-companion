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
    addMsg(who, esc(text), { caption: who === 'her' ? charDisplayName() + ' · ' + timeNow() : timeNow() });
  }

  // Restore a saved turn: server stores UTC ("YYYY-MM-DD HH:MM:SS"), so
  // parse it as UTC and show the caption in the user's local time.
  function histDate(iso) {
    const d = new Date(String(iso || '').replace(' ', 'T') + 'Z');
    return isNaN(d) ? null : d;
  }
  function addHistoryTurn(role, text, created_at) {
    const who = role === 'user' ? 'him' : 'her';
    const t = histDate(created_at);
    const stamp = t ? t.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '';
    addMsg(who, esc(text || ''), {
      caption: who === 'her' ? charDisplayName() + (stamp ? ' · ' + stamp : '') : stamp,
    });
  }
  function historyDayDivider(created_at) {
    const d = histDate(created_at);
    if (!d) return;
    const el = document.createElement('div');
    el.className = 'day-divider';
    el.textContent = d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
    chatLog.appendChild(el);
    try { localStorage.setItem('chloe_last_day', d.toDateString()); } catch (e) {}
  }
  async function loadHistory() {
    // Pull the saved conversation and render it oldest-first, with day
    // dividers where the date changes. Returns true when anything was
    // rendered — the chat then picks up exactly where you left off.
    let turns = [];
    try { turns = await ApiClient.history(currentChar.id, 50); } catch (e) { turns = []; }
    if (!turns.length) return false;
    let lastDay = '';
    for (const t of turns) {
      const d = histDate(t.created_at);
      const day = d ? d.toDateString() : '';
      if (day && day !== lastDay) { historyDayDivider(t.created_at); lastDay = day; }
      addHistoryTurn(t.role, t.text, t.created_at);
    }
    scrollBottom();
    return true;
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
    const wrap = addMsg('her', '', { bubbleClass: 'photo-bubble', caption: charDisplayName() + ' · ' + timeNow() });
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

  /* ---------- character picker ---------- */

  const DEFAULT_CHAR = { id: 'chloe', name: 'Chloe', avatar_url: '' };
  let currentChar = { ...DEFAULT_CHAR };
  try {
    const savedId = localStorage.getItem('chloe_char_id');
    if (savedId) currentChar = { id: savedId, name: savedId === 'chloe' ? 'Chloe' : savedId, avatar_url: '' };
  } catch (e) {}

  function charDisplayName() { return currentChar.name || 'Chloe'; }

  // FaceTime tile: girls with video footage get the live tile; the rest show
  // their avatar (or an initial tile when no avatar is set).
  function updateCharTile() {
    const video = $('chloeVideo'), avatar = $('charAvatar'), initial = $('charInitial');
    if (hasVideo()) {
      video.classList.remove('hidden'); avatar.classList.add('hidden'); initial.classList.add('hidden');
      vidIdle();
    } else {
      try { video.pause(); } catch (e) {}
      video.classList.add('hidden');
      clearTimeout(idleTimer);
      if (currentChar.avatar_url) {
        avatar.src = currentChar.avatar_url;
        avatar.classList.remove('hidden'); initial.classList.add('hidden');
      } else {
        initial.textContent = (currentChar.name || '?').charAt(0).toUpperCase();
        initial.classList.remove('hidden'); avatar.classList.add('hidden');
      }
    }
    document.querySelector('.header-name').textContent = charDisplayName();
  }

  function charGreeting() {
    const name = localStorage.getItem('chloe_name') || 'friend';
    if (currentChar.id === 'chloe') {
      return `hey ${name}. i'm chloe. this is my little corner of the night — come talk to me anytime.`;
    }
    return `hey ${name}. i'm ${charDisplayName().toLowerCase()} — come talk to me.`;
  }

  async function renderCharPicker() {
    const bar = $('charPicker');
    let roster = null;
    try { roster = await ApiClient.characters(); } catch (e) { roster = null; }
    if (!roster || !roster.length) roster = [DEFAULT_CHAR];
    // Keep the saved selection if it's still on the roster.
    if (!roster.some((c) => c.id === currentChar.id)) {
      currentChar = { ...roster[0] };
      try { localStorage.setItem('chloe_char_id', currentChar.id); } catch (e) {}
    } else {
      const full = roster.find((c) => c.id === currentChar.id);
      currentChar = { ...full };
    }
    bar.innerHTML = '';
    roster.forEach((c) => {
      const chip = document.createElement('button');
      chip.className = 'char-chip' + (c.id === currentChar.id ? ' selected' : '');
      const face = c.avatar_url
        ? `<img class="char-chip-face" src="${esc(c.avatar_url)}" alt="">`
        : `<span class="char-chip-face char-chip-initial">${esc((c.name || '?').charAt(0).toUpperCase())}</span>`;
      chip.innerHTML = `${face}<span class="char-chip-name">${esc(c.name || c.id)}</span>`;
      chip.addEventListener('click', () => switchCharacter(c.id, roster));
      bar.appendChild(chip);
    });
    bar.classList.toggle('hidden', roster.length < 2);
    await loadCharFootage(currentChar.id);
    updateCharTile();
  }

  async function switchCharacter(id, roster) {
    if (id === currentChar.id) return;
    const next = (roster || []).find((c) => c.id === id) || { ...DEFAULT_CHAR, id };
    currentChar = { ...next };
    try { localStorage.setItem('chloe_char_id', currentChar.id); } catch (e) {}
    document.querySelectorAll('.char-chip').forEach((chip) => {
      chip.classList.toggle('selected',
        chip.querySelector('.char-chip-name').textContent === (next.name || next.id));
    });
    await loadCharFootage(currentChar.id);
    updateCharTile();
    chatLog.innerHTML = '';
    // Each character keeps their own saved conversation — restore it rather
    // than starting blank.
    if (await loadHistory()) return;
    dayDividerIfNeeded();
    addText('her', charGreeting());
    showToast('Now talking to ' + charDisplayName());
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
    stage1At = 0; videoNudgeSent = false;
    typingRow.classList.remove('hidden');
    scrollBottom();

    // Let her "read" it for a beat — never cut her off mid-thought.
    await new Promise((r) => setTimeout(r, 900 + Math.random() * 900));

    try {
      const res = await ApiClient.chat(text, currentChar.id);
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
      // 401s bounce to the login screen via onAuthRequired — don't also
      // drop a "lost you" bubble into the chat.
      if (e && e.status !== 401) {
        addText('her', 'hmm, lost you for a second. say that again?');
      }
    }
    sending = false;
  }

  $('sendBtn').addEventListener('click', () => sendMessage(msgInput.value));
  msgInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendMessage(msgInput.value);
  });

  // Mic: voice-note sending unlocks with her reply tiers.
  $('micBtn').addEventListener('click', () => {
    showToast('Voice notes come with her Plus and VIP plans');
  });


  /* ---------- FaceTime video tile ----------
     Chloe's clips are hardcoded (2026-09-29). Other girls play the footage
     labeled for them in the admin panel (typing-main / main / activity).
     'give' is Keyhole-only: the server never sends it to the app. */
  const MEDIA_BASE = 'https://keyhole-latest-production.up.railway.app/media/files/';
  const CHLOE_IDLE = 'chloe_3bcbf88d987b7621.mp4';
  const CHLOE_TYPING = 'chloe_6f26366653c891fc.mp4';
  const CHLOE_ACTS = [
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
  let actIdx = Math.floor(Math.random() * CHLOE_ACTS.length);
  let idleTimer = null;
  // Labeled footage for the current non-Chloe girl, from the server.
  let charFootage = { typing: null, main: null, acts: [] };

  async function loadCharFootage(id) {
    charFootage = { typing: null, main: null, acts: [] };
    actIdx = -1; // other girls' activities play sequentially from the first
    if (!id || id === 'chloe') { actIdx = Math.floor(Math.random() * CHLOE_ACTS.length); return; }
    try {
      const r = await fetch(CONFIG.API_BASE_URL + '/api/v1/characters/' + encodeURIComponent(id) + '/footage');
      const d = await r.json();
      for (const f of (d && d.footage) || []) {
        if (f.label === 'typing-main' && !charFootage.typing) charFootage.typing = f.url;
        else if (f.label === 'main' && !charFootage.main) charFootage.main = f.url;
        else if (f.label === 'activity' && f.url) charFootage.acts.push(f.url);
      }
    } catch (e) { /* tile falls back to her avatar */ }
  }

  function hasVideo() {
    return currentChar.id === 'chloe' ||
      !!(charFootage.main || charFootage.typing || charFootage.acts.length);
  }
  function vidIdleSrc() {
    if (currentChar.id === 'chloe') return MEDIA_BASE + CHLOE_IDLE;
    return charFootage.main || charFootage.typing || charFootage.acts[0] || null;
  }
  function vidTypingSrc() {
    if (currentChar.id === 'chloe') return MEDIA_BASE + CHLOE_TYPING;
    return charFootage.typing || charFootage.main || charFootage.acts[0] || null;
  }
  function vidActs() { return currentChar.id === 'chloe' ? CHLOE_ACTS : charFootage.acts; }

  function vidShowTapHint() { videoTapHint.classList.remove('hidden'); }
  function vidHideTapHint() { videoTapHint.classList.add('hidden'); }

  function vidPlay(src, loop) {
    if (!src) { vidShowTapHint(); return; }
    const url = /^https?:\/\//.test(src) ? src : MEDIA_BASE + src;
    chloeVideo.loop = !!loop;
    if (chloeVideo.dataset.rawSrc !== src) {
      chloeVideo.dataset.rawSrc = src;
      chloeVideo.src = url;
    }
    const pr = chloeVideo.play();
    if (pr && pr.then) pr.then(vidHideTapHint).catch(vidShowTapHint);
  }
  function vidIdle() {
    if (!hasVideo()) return;
    const src = vidIdleSrc();
    if (!src) return;
    vidMode = 'idle';
    vidPlay(src, true);
    clearTimeout(idleTimer);
    const acts = vidActs();
    if (!acts.length) return;
    // One activity at a time during idle, then back to idle.
    idleTimer = setTimeout(() => {
      if (vidMode !== 'idle' || document.hidden) { vidIdle(); return; }
      vidMode = 'activity';
      actIdx = (actIdx + 1) % acts.length;
      vidPlay(acts[actIdx], false);
    }, 20000);
  }
  function vidTyping() {
    if (!hasVideo()) return;
    const src = vidTypingSrc();
    if (!src) return;
    vidMode = 'typing'; clearTimeout(idleTimer); vidPlay(src, true);
  }
  chloeVideo.addEventListener('ended', () => { if (vidMode === 'activity') vidIdle(); });
  chloeVideo.addEventListener('error', vidShowTapHint, true);
  videoTapHint.addEventListener('click', (e) => { e.stopPropagation(); vidPlay(chloeVideo.dataset.rawSrc || vidIdleSrc(), vidMode !== 'activity'); });
  // iOS: retry autoplay on first touch and when the tab becomes visible.
  document.addEventListener('touchend', function once() {
    document.removeEventListener('touchend', once);
    if (chloeVideo.paused) vidPlay(chloeVideo.dataset.rawSrc || vidIdleSrc(), vidMode !== 'activity');
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && chloeVideo.paused) vidIdle();
  });

  /* ---------- proactive: she takes the lead when he's quiet ----------
     Stage 1: 90s of silence -> "hey. still with me?" (text, once).
     Stage 2: an hour after that, still nothing -> her video nudge
     (video + exactly what she says in it, as a text message, once).
     Then she leaves him alone until he talks. */
  const QUIET_MS = 90000;          // 90s of silence before she reaches out
  const VIDEO_NUDGE_MS = 3600000;  // 1h after stage 1 before she sends the video
  const VID_NUDGE = 'chloe_a5f216d755722372.mp4';  // quiet-nudge video (user pick 2026-09-30)
  const STAGE1_LINE = 'hey. still with me?';
  const STAGE2_LINE = "Hey, I was hoping you'd call. Yeah, I'm just in bed. Wish you were here. Sweet dreams.";  // her exact words in the quiet-nudge video (transcribed 2026-09-30)
  let lastUserMsgAt = Date.now();
  let stage1At = 0;          // timestamp of the stage-1 text nudge (0 = not sent)
  let videoNudgeSent = false;
  function armQuietNudge() {
    setInterval(() => {
      if (localStorage.getItem('chloe_proactive') === 'off') return;
      if (document.hidden || sending) return;
      // Stage 2: an hour after the text nudge and still quiet — the video,
      // plus exactly what she says in it as a chat message.
      if (stage1At && !videoNudgeSent && Date.now() - stage1At >= VIDEO_NUDGE_MS) {
        videoNudgeSent = true;
        if (currentChar.id === 'chloe') {
          vidMode = 'activity';
          vidPlay(VID_NUDGE, false);   // plays once, 'ended' drops back to idle
        }
        const myStage2 = stage1At;
        typingRow.classList.remove('hidden'); scrollBottom();
        setTimeout(() => {
          if (stage1At !== myStage2) return;  // he talked while she was typing — stand down on the text
          typingRow.classList.add('hidden');
          addText('her', STAGE2_LINE);
        }, 1600);
        return;
      }
      // Stage 1: 90s of silence — one text line, once.
      if (stage1At || Date.now() - lastUserMsgAt < QUIET_MS) return;
      const myStage = (stage1At = Date.now());
      typingRow.classList.remove('hidden'); scrollBottom(); vidTyping();
      setTimeout(() => {
        if (stage1At !== myStage) return;  // he talked while she was typing — stand down
        typingRow.classList.add('hidden');
        addText('her', STAGE1_LINE);
        vidIdle();
      }, 1600);
    }, 15000);
  }

  /* ---------- auth gate: email login required, no free/demo use ---------- */

  function showAuth() {
    $('authScreen').classList.remove('hidden');
    $('onboarding').classList.add('hidden');
    $('app').classList.add('hidden');
    refreshFreePromo();
  }
  async function refreshFreePromo() {
    const el = $('freeSpots');
    if (!el) return;
    try {
      const r = await fetch(CONFIG.API_BASE_URL + '/api/v1/free-spots');
      const d = await r.json();
      el.textContent = d.spots_left > 0
        ? ` — ${d.spots_left} of ${d.cap} left`
        : ' — all claimed';
    } catch (e) { el.textContent = ''; }
  }
  function hideAuth() { $('authScreen').classList.add('hidden'); }

  let authMode = 'login'; // 'login' | 'signup'
  function setAuthMode(mode) {
    authMode = mode;
    $('tabLogin').classList.toggle('active', mode === 'login');
    $('tabSignup').classList.toggle('active', mode === 'signup');
    $('authName').classList.toggle('hidden', mode === 'login');
    $('authPassword').setAttribute('autocomplete', mode === 'signup' ? 'new-password' : 'current-password');
    $('authGo').textContent = mode === 'signup' ? 'Sign up' : 'Log in';
    $('authMsg').textContent = '';
    $('authResend').classList.add('hidden');
  }
  $('tabLogin').addEventListener('click', () => setAuthMode('login'));
  $('tabSignup').addEventListener('click', () => setAuthMode('signup'));

  function authBusy(busy, label) {
    $('authGo').disabled = busy;
    $('authGo').textContent = busy ? (label || 'Working…') : (authMode === 'signup' ? 'Sign up' : 'Log in');
  }

  async function submitAuth() {
    const email = $('authEmail').value.trim();
    const password = $('authPassword').value;
    const name = $('authName').value.trim().slice(0, 24);
    $('authMsg').textContent = '';
    $('authResend').classList.add('hidden');
    if (!email || !password) { $('authMsg').textContent = 'Enter your email and password.'; return; }
    authBusy(true);
    try {
      if (authMode === 'signup') {
        await ApiClient.signup(email, password, name);
        // Stay on the signup tab so this message (and the resend button)
        // remain visible — switching modes would clear it.
        $('authMsg').textContent = 'Check your inbox — tap the link to verify your email, then log in.';
        $('authResend').classList.remove('hidden');
        $('authResend').onclick = async () => {
          try {
            await ApiClient.resendVerification(email);
            $('authMsg').textContent = 'Verification email re-sent. Check your inbox.';
          } catch (e2) {
            $('authMsg').textContent = ApiClient.friendlyDetail(e2.detail);
          }
        };
      } else {
        const data = await ApiClient.login(email, password);
        hideAuth();
        enterApp(data);
      }
    } catch (e) {
      const d = e.detail || '';
      $('authMsg').textContent = ApiClient.friendlyDetail(d);
      if (d === 'email_unverified') {
        $('authResend').classList.remove('hidden');
        $('authResend').onclick = async () => {
          try {
            await ApiClient.resendVerification(email);
            $('authMsg').textContent = 'Verification email re-sent. Check your inbox.';
          } catch (e2) {
            $('authMsg').textContent = ApiClient.friendlyDetail(e2.detail);
          }
        };
      }
    } finally {
      authBusy(false);
    }
  }
  $('authGo').addEventListener('click', submitAuth);
  $('authPassword').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitAuth(); });
  $('authEmail').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitAuth(); });

  // Any 401 anywhere bounces back to the login screen.
  ApiClient.onAuthRequired(() => showAuth());

  $('logoutBtn').addEventListener('click', async () => {
    await ApiClient.logout();
    try { localStorage.removeItem('chloe_name'); } catch (e) {}
    closeSheet('profileSheet');
    chatLog.innerHTML = '';
    showAuth();
  });

  /* ---------- entering the app (post-login) ---------- */

  function enterApp(me) {
    hideAuth();
    const serverName = (me && me.name) || '';
    if (serverName) {
      try { localStorage.setItem('chloe_name', serverName); } catch (e) {}
    }
    if (!localStorage.getItem('chloe_name')) {
      showOnboarding();
    } else {
      showApp();
      bootChat(false);
    }
  }

  /* ---------- onboarding-lite (name ask) ---------- */

  function showOnboarding() {
    hideAuth();
    $('onboarding').classList.remove('hidden');
    $('app').classList.add('hidden');
  }
  function showApp() {
    hideAuth();
    $('onboarding').classList.add('hidden');
    $('app').classList.remove('hidden');
  }

  async function finishOnboarding() {
    const name = $('nameInput').value.trim().slice(0, 24) || 'friend';
    try { await ApiClient.setName(name); } catch (e) { /* offline-safe: keep local */ }
    try { localStorage.setItem('chloe_name', name); } catch (e) {}
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
  // "Change my name" reuses the onboarding ask; finishOnboarding already
  // syncs the new name to the server via POST /api/v1/auth/name.

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
    let me = null;
    try { me = await ApiClient.me(); } catch (e) { me = null; }
    $('profileName').textContent = (me && me.name) || localStorage.getItem('chloe_name') || 'You';
    $('statMsgs').textContent = me ? me.messages_remaining : '—';
    $('statVoice').textContent = me ? me.voice_notes_remaining : '—';
    const tier = me && me.tier ? me.tier : 'basic';
    $('tierName').textContent = 'Companion — ' + tier.charAt(0).toUpperCase() + tier.slice(1);
    $('tierSub').textContent = me ? 'live on companion server' : 'couldn’t reach the server';
    updateConnLine();
    renderPlans(tier);
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
  async function renderPlans(currentTier) {
    const list = $('planList');
    let data = null;
    try { data = await ApiClient.tiers(); } catch (e) { data = null; }
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
      const once = p.interval === 'once';
      const benefits = once
        ? [p.messages_per_month.toLocaleString('en-US') + ' messages · one-time', 'No voice notes']
        : [
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
        <div class="plan-head"><div class="plan-name">${esc(p.name)}</div><div class="plan-price">$${p.price_usd.toFixed(2)}<span>${once ? 'one-time' : '/mo'}</span></div></div>
        <ul class="plan-benefits">${benefits.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>`;
      const btn = document.createElement('button');
      if (p.key === currentTier) {
        btn.className = 'btn-ghost'; btn.textContent = 'Current plan'; btn.disabled = true;
      } else {
        btn.className = 'btn-primary';
        btn.textContent = once ? 'Try · $' + p.price_usd.toFixed(2) + ' once' : 'Subscribe · $' + p.price_usd.toFixed(2) + '/mo';
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
      el.innerHTML = '<span class="bad">not connected</span>';
    }
  }
  ApiClient.onStatus(updateConnLine);
  $('reconnectBtn').addEventListener('click', async () => {
    $('connStatus').textContent = 'checking…';
    try { await ApiClient.me(); } catch (e) {}
    updateConnLine();
    showToast(ApiClient.isConnected() ? 'Connected to the companion server' : 'Still not connected — check your connection');
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
    $('apiBaseLine').textContent = 'api: ' + CONFIG.API_BASE_URL;
    $('proactiveToggle').checked = localStorage.getItem('chloe_proactive') !== 'off';

    // Character roster (admin-managed). Chloe-only until the server lists more.
    renderCharPicker();
    updateConnLine();

    const returning = !isFirstHello && !!localStorage.getItem('chloe_seen');
    localStorage.setItem('chloe_seen', '1');

    // Restore the saved conversation FIRST: logging back in picks up right
    // where you left off instead of resetting to a blank chat.
    if (await loadHistory()) return;

    dayDividerIfNeeded();
    await new Promise((r) => setTimeout(r, 600));
    typingRow.classList.remove('hidden'); scrollBottom();
    await new Promise((r) => setTimeout(r, 1400));
    typingRow.classList.add('hidden');

    if (isFirstHello) {
      addText('her', charGreeting());
    } else if (returning) {
      addText('her', 'hey, welcome back. missed you.');
    }
  }

  // Go: pick up the session from an email verify link, else require login.
  (function () {
    const m = (location.hash || '').match(/#session=([A-Za-z0-9\-_]+)/);
    if (m) {
      ApiClient.setToken(m[1]);
      try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {}
    }
  })();
  (async function boot() {
    if (ApiClient.getToken()) {
      try {
        const me = await ApiClient.me();
        enterApp(me);
        return;
      } catch (e) {
        ApiClient.clearToken(); // bad/expired token -> login screen
      }
    }
    showAuth();
  })();
})();
