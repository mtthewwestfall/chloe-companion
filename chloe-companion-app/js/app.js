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
  const CURATED_CHARACTERS = [
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
    { id: 'regan', name: 'Regan',
      avatar: 'https://companion.lockeddoor.ai/img/regan-avatar.jpg',
      videos: [],
      desc: 'Strong, flexible, hard to forget.',
      bestFor: 'Playful challenges, bold energy, and a spark that doesn\'t quit.' },
    { id: 'jake', name: 'Jake',
      avatar: 'https://companion.lockeddoor.ai/img/jake-avatar.jpg',
      videos: [],
      desc: 'Rugged, warm, and quietly confident.',
      bestFor: 'Slow evenings, steady comfort, and feeling looked after.' },
    { id: 'eli', name: 'Eli',
      avatar: 'https://companion.lockeddoor.ai/img/eli-avatar.jpg',
      videos: [],
      desc: 'Sun-kissed, easygoing, and always mid-laugh.',
      bestFor: 'Beach-day lightness, music at sunset, and talks that drift past midnight.' },
  ];
  // The live picker list: starts as the curated cards, then loadRoster()
  // reconciles it with the server roster (see below).
  let CHARACTERS = CURATED_CHARACTERS.slice();
  let currentGirl = CHARACTERS[1];
  let avatarCycleTimer = null;

  function startAvatarCycle() {
    const video = $('chatAvatarVideo');
    const clips = currentGirl.videos || [];
    if (!clips.length) {
      // No cycle clips yet (newer characters): show the avatar as a still poster.
      clearInterval(avatarCycleTimer);
      video.removeAttribute('src');
      video.load();
      video.poster = currentGirl.avatar;
      return;
    }
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
      const descBits = [];
      if (c.desc) descBits.push(c.desc);
      if (c.bestFor) descBits.push(`<b>Best for:</b> ${c.bestFor}`);
      const descHtml = descBits.length
        ? `<div class="girl-desc">${descBits.join('<br>')}</div>` : '';
      el.innerHTML = `
        <img class="girl-photo" src="${c.avatar}" alt="${c.name}" loading="lazy">
        <div class="girl-info">
          <div class="girl-name">${c.name}</div>
          <span class="online-dot">● Online Now</span>
          ${descHtml}
          <button class="start-chat-btn">[ Chat on Whispr ]</button>
        </div>`;
      el.addEventListener('click', () => openChat(c.id));
      wrap.appendChild(el);
    });
  }

  /* ---------- roster (server-driven picker) ---------- */
  // The picker list comes from GET /api/v1/characters (with the auth token
  // when signed in; owner-scoped characters appear only for their owner —
  // the server decides). Curated cards keep their look/copy and come first;
  // anything else on the roster (creations, future girls) is appended from
  // server fields. On any fetch failure the curated list stays as-is
  // (offline/demo fallback).
  async function loadRoster() {
    try {
      const headers = {};
      const tok = getToken();
      if (tok) headers['Authorization'] = 'Bearer ' + tok;
      const res = await fetch(CONFIG.API_BASE_URL + '/api/v1/characters', { headers });
      if (!res.ok) return;
      const data = await res.json();
      const roster = (data && data.characters) || [];
      if (!Array.isArray(roster) || !roster.length) return;
      const onRoster = new Set(roster.map((c) => String(c.id || '').toLowerCase()));
      const merged = CURATED_CHARACTERS.filter((c) => onRoster.has(c.id));
      const known = new Set(merged.map((c) => c.id));
      roster.forEach((rc) => {
        const id = String(rc.id || '').toLowerCase();
        if (!id || known.has(id)) return;
        known.add(id);
        merged.push({
          id,
          name: rc.name || id,
          avatar: rc.avatar_url || '',
          videos: [],
          desc: '',
          bestFor: '',
        });
      });
      if (!merged.length) return;
      CHARACTERS = merged;
      const cg = CHARACTERS.find((c) => c.id === currentGirl.id);
      if (cg) currentGirl = cg;
      renderGirlCards();
    } catch (e) { /* offline/demo: curated fallback stays */ }
  }

  async function openChat(girlId) {
    const girl = CHARACTERS.find((c) => c.id === girlId) || CHARACTERS[1];
    currentGirl = girl;
    $('chatName').textContent = girl.name;
    clearPendingAttachments();
    $('attachBtn').classList.toggle('hidden', !ASSISTANT_GIRLS.includes(girl.id));
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
    // Load last 8 turns so the conversation picks up where it left off
    let hasHistory = false;
    if (isLoggedIn()) {
      try {
        const res = await fetch(
          CONFIG.API_BASE_URL + '/api/v1/chat/history?character_id=' + girl.id + '&limit=8',
          { headers: { 'Authorization': 'Bearer ' + getToken() } }
        );
        const data = await res.json();
        const turns = (data.turns || []).slice(-8);
        hasHistory = turns.length > 0;
        turns.forEach(t => {
          const who = t.role === 'user' ? 'him' : 'her';
          if (Array.isArray(t.attachments) && t.attachments.length) {
            addAttachments(who, t.attachments);
          }
          if ((t.text || '').trim()) addText(who, t.text || '');
        });
        // Her stored login ask (or a failed attempt) is the last word:
        // re-offer the secure entry box under it.
        const lastTurn = turns[turns.length - 1];
        if (lastTurn && lastTurn.role !== 'user'
            && ASSISTANT_GIRLS.includes(girl.id)
            && /not signed into your sogni|sign-in didn't take/i.test(lastTurn.text || '')) {
          addSogniLoginButton();
        }
        scrollBottom();
      } catch (e) {}
    }
    bootChat(false, hasHistory);
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

  function addText(who, text, opts = {}) {
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

  function addGeneratedImage(url) {
    const wrap = addMsg('her', '', { bubbleClass: 'photo-bubble', caption: currentGirl.name + ' · ' + timeNow() });
    const b = wrap.firstChild;
    b.innerHTML = `
      <div class="photo-scene" style="height:auto;min-height:200px;"><img src="${url}" style="width:100%;display:block;" alt="Image Hannah made"></div>
      <div class="photo-caption">🎨 made this for you</div>`;
    scrollBottom();
  }

  /* Secure Sogni login box: her login ask (live or replayed from
     history) carries this button; it opens the sheet with the
     password field — credentials never travel as chat text. */
  function addSogniLoginButton() {
    const wrap = addMsg('her', '', { caption: currentGirl.name + ' · ' + timeNow() });
    const btn = document.createElement('button');
    btn.className = 'btn-primary full';
    btn.textContent = '🔐 Enter Sogni login';
    btn.addEventListener('click', () => openSheet('sogniLoginSheet'));
    wrap.firstChild.appendChild(btn);
    scrollBottom();
  }

  /* ---------- Hannah attachments ---------- */

  const ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024;
  const ATTACHMENT_VIDEO_MAX_BYTES = 50 * 1024 * 1024;
  // Threads that carry the full assistant suite (Hannah, Mia and
  // Jordan, his project lanes, plus Rocky, his main agent): the
  // attachment paperclip lives only there.
  const ASSISTANT_GIRLS = ['hannah', 'mia', 'jordan', 'rocky'];
  const ATTACHMENT_MAX_PER_MESSAGE = 4;
  const ATTACHMENT_EXTS = new Set([
    'jpg', 'jpeg', 'png', 'webp', 'gif',
    'mp4', 'mov', 'webm',
    'txt', 'md', 'csv', 'json', 'pdf', 'docx',
  ]);
  const ATTACHMENT_VIDEO_EXTS = new Set(['mp4', 'mov', 'webm']);
  let pendingAttachments = [];
  let attachmentUploadsInFlight = 0;

  const escAttr = (s) => esc(String(s || '')).replace(/"/g, '&quot;');

  function formatBytes(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function attachmentIcon(att) {
    const name = (att.name || att.filename || '').toLowerCase();
    if (att.kind === 'image') return '🖼️';
    if (att.kind === 'video') return '🎬';
    if (name.endsWith('.pdf')) return '📕';
    if (name.endsWith('.docx')) return '📘';
    if (name.endsWith('.csv')) return '📊';
    if (name.endsWith('.json')) return '🧾';
    return '📄';
  }

  function attachmentCard(att) {
    const name = att.name || att.filename || 'attachment';
    const url = att.url || att.previewUrl || '';
    if (att.kind === 'image' && url) {
      return `
        <div class="attachment-card image">
          <img src="${escAttr(url)}" alt="${escAttr(name)}">
          <div class="attachment-meta"><span>${esc(name)}</span><small>${formatBytes(att.size)}</small></div>
        </div>`;
    }
    if (att.kind === 'video' && url) {
      const poster = att.poster_url ? ` poster="${escAttr(att.poster_url)}"` : '';
      return `
        <div class="attachment-card video">
          <video src="${escAttr(url)}"${poster} controls preload="metadata" playsinline></video>
          <div class="attachment-meta"><span>${esc(name)}</span><small>${formatBytes(att.size)}</small></div>
        </div>`;
    }
    if (att.kind === 'audio' && url) {
      return `
      <div class="attachment-card audio">
        <audio src="${escAttr(url)}" controls preload="metadata"></audio>
        <div class="attachment-meta"><span>${esc(name)}</span><small>${formatBytes(att.size)}</small></div>
      </div>`;
    }
    const unreadable = att.extraction_status === 'unsupported'
      ? '<small class="attachment-note">stored — Hannah can’t read this type yet</small>' : '';
    return `
      <div class="attachment-card file">
        <div class="attachment-icon">${attachmentIcon(att)}</div>
        <div class="attachment-meta"><span>${esc(name)}</span><small>${formatBytes(att.size)}</small>${unreadable}</div>
      </div>`;
  }

  function addAttachments(who, attachments) {
    if (!Array.isArray(attachments) || !attachments.length) return;
    const wrap = addMsg(who, '', {
      bubbleClass: 'attachment-bubble',
      caption: who === 'her' ? currentGirl.name + ' · ' + timeNow() : timeNow(),
    });
    wrap.firstChild.innerHTML = attachments.map(attachmentCard).join('');
    scrollBottom();
  }

  function renderAttachmentPreview() {
    const box = $('attachmentPreview');
    if (!pendingAttachments.length) {
      box.innerHTML = '';
      box.classList.add('hidden');
      return;
    }
    box.classList.remove('hidden');
    box.innerHTML = pendingAttachments.map((entry) => {
      const att = entry.attachment || {
        kind: entry.kind,
        name: entry.file.name,
        size: entry.file.size,
        url: entry.previewUrl,
        extraction_status: entry.extraction_status,
      };
      const state = entry.status === 'uploading'
        ? '<small class="attachment-note">uploading…</small>'
        : (entry.status === 'error'
          ? `<small class="attachment-error">${esc(entry.error || 'upload failed')}</small>` : '');
      return `
        <div class="attachment-chip ${entry.status}">
          ${attachmentCard(att)}
          ${state}
          <button class="attachment-remove" data-remove="${entry.localId}" aria-label="Remove attachment">✕</button>
        </div>`;
    }).join('');
    box.querySelectorAll('[data-remove]').forEach((btn) => {
      btn.addEventListener('click', () => removePendingAttachment(btn.dataset.remove));
    });
    scrollBottom();
  }

  function removePendingAttachment(localId) {
    const idx = pendingAttachments.findIndex((entry) => entry.localId === localId);
    if (idx < 0) return;
    const [entry] = pendingAttachments.splice(idx, 1);
    if (entry.previewUrl && entry.previewUrl.startsWith('blob:')) {
      URL.revokeObjectURL(entry.previewUrl);
    }
    renderAttachmentPreview();
  }

  function clearPendingAttachments() {
    pendingAttachments.forEach((entry) => {
      if (entry.previewUrl && entry.previewUrl.startsWith('blob:')) {
        URL.revokeObjectURL(entry.previewUrl);
      }
    });
    pendingAttachments = [];
    attachmentUploadsInFlight = 0;
    renderAttachmentPreview();
  }

  async function uploadPendingAttachment(entry) {
    attachmentUploadsInFlight++;
    renderAttachmentPreview();
    try {
      const attachment = await ApiClient.uploadChatAttachment(entry.file, currentGirl.id);
      entry.attachment = attachment;
      entry.status = 'ready';
      entry.extraction_status = attachment.extraction_status;
    } catch (e) {
      entry.status = 'error';
      entry.error = e.message || 'upload failed';
      showToast(entry.error, 3600);
    } finally {
      attachmentUploadsInFlight = Math.max(0, attachmentUploadsInFlight - 1);
      renderAttachmentPreview();
    }
  }

  async function onAttachmentsPicked(files) {
    if (!ASSISTANT_GIRLS.includes(currentGirl.id)) {
      showToast('Attachments are only on an assistant chat right now', 3200);
      return;
    }
    const picked = Array.from(files || []);
    for (const file of picked) {
      if (pendingAttachments.length >= ATTACHMENT_MAX_PER_MESSAGE) {
        showToast('Four attachments max per message', 3200);
        break;
      }
      const ext = (file.name.split('.').pop() || '').toLowerCase();
      if (!ATTACHMENT_EXTS.has(ext)) {
        showToast('She can take images, videos, txt/md/csv/json, pdf, or docx', 3600);
        continue;
      }
      const isVideo = ATTACHMENT_VIDEO_EXTS.has(ext);
      const cap = isVideo ? ATTACHMENT_VIDEO_MAX_BYTES : ATTACHMENT_MAX_BYTES;
      if (file.size > cap) {
        showToast(isVideo ? 'That video is too big — 50 MB max' : 'That file is too big — 10 MB max', 3200);
        continue;
      }
      const entry = {
        localId: 'local-' + Date.now() + '-' + Math.random().toString(16).slice(2),
        file,
        kind: ['jpg', 'jpeg', 'png', 'webp', 'gif'].includes(ext) ? 'image' : (isVideo ? 'video' : 'file'),
        previewUrl: (file.type.startsWith('image/') || file.type.startsWith('video/')) ? URL.createObjectURL(file) : '',
        status: 'uploading',
        extraction_status: '',
      };
      pendingAttachments.push(entry);
      renderAttachmentPreview();
      uploadPendingAttachment(entry);
    }
  }

  $('attachBtn').addEventListener('click', () => $('attachmentInput').click());
  $('attachmentInput').addEventListener('change', (e) => {
    onAttachmentsPicked(e.target.files);
    e.target.value = '';
  });

  /* ---------- chat flow ---------- */

  let sending = false;

  async function sendMessage(text) {
    text = (text || '').trim();
    if (sending) return;
    if (attachmentUploadsInFlight > 0) {
      showToast('Wait for the upload to finish', 2600);
      return;
    }
    const readyAttachments = pendingAttachments
      .filter((entry) => entry.status === 'ready' && entry.attachment)
      .map((entry) => entry.attachment);
    if (pendingAttachments.some((entry) => entry.status === 'error')) {
      showToast('Remove the failed upload before sending', 3200);
      return;
    }
    if (!text && !readyAttachments.length) return;
    sending = true;
    if (readyAttachments.length) addAttachments('him', readyAttachments);
    if (text) addText('him', text);
    clearPendingAttachments();
    msgInput.value = '';
    typingRow.classList.remove('hidden');
    scrollBottom();

    // Let her "read" it for a beat — never cut her off mid-thought.
    await new Promise((r) => setTimeout(r, 900 + Math.random() * 900));

    try {
      const res = await ApiClient.chat(
        text, currentGirl.id, readyAttachments.map((att) => att.id));
      typingRow.classList.add('hidden');
      if (res.voice_note_url) {
        addVoiceNote(res.voice_note_url, res.reply_text);
      } else {
        addText('her', res.reply_text || '…');
      }
      if (res.photo_url) addPhotoDrop(res.photo_url, res.reply_text);
      if (res.image_url) addGeneratedImage(res.image_url);
      if (res.sogni_login_box) addSogniLoginButton();
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
    regan: [{ name: 'love', count: 6, emoji: '💕' }],
    jake: [{ name: 'love', count: 9, emoji: '💕' }],
    eli: [{ name: 'love', count: 9, emoji: '💕' }],
    brooklyn: [{ name: 'love', count: 11, emoji: '💕' }],
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
    // Custom video request entry point
    const customEl = document.createElement('div');
    customEl.className = 'preset-item';
    customEl.innerHTML = `
      <div class="preset-thumb">✨</div>
      <div><div class="preset-name">Request a custom video</div>
      <div class="preset-count">describe it — she'll try to make it</div></div>`;
    customEl.addEventListener('click', () => {
      closeSheet('presetSheet');
      openCustomVideoSheet();
    });
    list.appendChild(customEl);
    openSheet('presetSheet');
  });

  /* ---------- custom videos ---------- */

  function openCustomVideoSheet() {
    $('customVideoGirlName').textContent = currentGirl.name || 'her';
    $('customVideoDesc').value = '';
    loadCustomVideoHistory();
    openSheet('customVideoSheet');
  }

  $('customVideoSubmit').addEventListener('click', async () => {
    const desc = $('customVideoDesc').value.trim();
    if (!desc) { showToast('Describe what you want first'); return; }
    $('customVideoSubmit').disabled = true;
    try {
      await ApiClient.requestCustomVideo(currentGirl.id, desc);
      showToast('Requested — she\'ll try to make it');
      $('customVideoDesc').value = '';
      loadCustomVideoHistory();
    } catch (e) {
      showToast('Couldn\'t send that request');
    } finally {
      $('customVideoSubmit').disabled = false;
    }
  });

  async function loadCustomVideoHistory() {
    const box = $('customVideoHistory');
    box.innerHTML = '<div class="sheet-sub">Loading…</div>';
    try {
      const reqs = await ApiClient.customVideos();
      const mine = reqs.filter((r) => (r.girl_id || '').toLowerCase() === (currentGirl.id || '').toLowerCase());
      if (!mine.length) { box.innerHTML = '<div class="sheet-sub">No requests yet.</div>'; return; }
      box.innerHTML = mine.map((r) => {
        const label = r.status === 'fulfilled' ? '✅ ready'
          : r.status === 'generating' ? '⏳ making it…'
          : '🕐 queued';
        const media = r.status === 'fulfilled' && r.video_url
          ? `<div style="margin-top:6px;"><video src="${r.video_url}" controls playsinline style="width:100%;border-radius:10px;"></video></div>`
          : '';
        return `<div class="preset-item" style="cursor:default;"><div style="flex:1;">
          <div class="preset-name" style="font-size:13px;">${escapeHtml(r.description)}</div>
          <div class="preset-count">${label}</div>${media}</div></div>`;
      }).join('');
    } catch (e) {
      box.innerHTML = '<div class="sheet-sub">Couldn\'t load requests.</div>';
    }
  }

  function escapeHtml(s) {
    return (s || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

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
    $('authForgotForm').classList.add('hidden');
    $('authResetForm').classList.add('hidden');
  });

  // Forgot password flow
  $('forgotPwBtn').addEventListener('click', () => {
    $('authLoginForm').classList.add('hidden');
    $('authForgotForm').classList.remove('hidden');
  });
  $('forgotBackBtn').addEventListener('click', () => {
    $('authForgotForm').classList.add('hidden');
    $('authLoginForm').classList.remove('hidden');
  });
  $('forgotSendBtn').addEventListener('click', async () => {
    $('authError').classList.add('hidden');
    const email = $('forgotEmail').value.trim();
    if (!email) return authError('Enter your email');
    try {
      await fetch(CONFIG.API_BASE_URL + '/api/v1/auth/forgot-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email })
      });
      $('authVerifyMsg').textContent = 'If that email is registered, a reset link is on its way.';
      $('authVerifyMsg').classList.remove('hidden');
      $('authForgotForm').classList.add('hidden');
    } catch (e) {
      authError('Network error — try again');
    }
  });

  // Handle ?reset_token= from email link
  (function checkResetToken() {
    const params = new URLSearchParams(window.location.search);
    const rt = params.get('reset_token');
    if (rt) {
      localStorage.setItem('whispr_reset_token', rt);
      showAuth();
      $('authTabLogin').click();
      setTimeout(() => {
        $('authLoginForm').classList.add('hidden');
        $('authResetForm').classList.remove('hidden');
      }, 100);
    }
  })();
  $('resetBtn').addEventListener('click', async () => {
    $('authError').classList.add('hidden');
    const token = localStorage.getItem('whispr_reset_token');
    const pw = $('resetPassword').value;
    if (!token) return authError('Reset link expired — request a new one');
    if (pw.length < 8) return authError('Password must be 8+ characters');
    try {
      const res = await fetch(CONFIG.API_BASE_URL + '/api/v1/auth/reset-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, new_password: pw })
      });
      const data = await res.json();
      if (!res.ok) return authError(data.detail || 'Reset failed');
      localStorage.removeItem('whispr_reset_token');
      $('authVerifyMsg').textContent = 'Password updated! Log in with your new password.';
      $('authVerifyMsg').classList.remove('hidden');
      $('authResetForm').classList.add('hidden');
      $('authLoginForm').classList.remove('hidden');
    } catch (e) {
      authError('Network error — try again');
    }
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
      loadLandingReferral();
      loadRoster();
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

  /* ---------- referral on landing ---------- */

  async function loadLandingReferral() {
    if (!isLoggedIn()) return;
    try {
      const res = await fetch(CONFIG.API_BASE_URL + '/api/v1/referral/me', {
        headers: { 'Authorization': 'Bearer ' + getToken() }
      });
      const data = await res.json();
      if (data.referral_link) $('refLinkInputLanding').value = data.referral_link;
    } catch (e) {}
  }

  function shareReferral(network) {
    const link = $('refLinkInputLanding').value || $('refLinkInput').value;
    if (!link) { showToast('Log in to get your link'); return; }
    const msg = "I've been testing this new AI companion app called Whispr. Use my link for free VIP access:";
    if (network === 'telegram') {
      window.open('https://t.me/share/url?url=' + encodeURIComponent(link) + '&text=' + encodeURIComponent(msg), '_blank');
    } else if (network === 'x') {
      window.open('https://twitter.com/intent/tweet?text=' + encodeURIComponent("Check out Whispr on Lockeddoor.ai: " + link), '_blank');
    } else if (network === 'reddit') {
      window.open('https://www.reddit.com/submit?url=' + encodeURIComponent(link) + '&title=' + encodeURIComponent("Whispr AI Girlfriend - free VIP with referral"), '_blank');
    }
  }

  $('refCopyBtnLanding').addEventListener('click', () => {
    $('refLinkInputLanding').select();
    document.execCommand('copy');
    showToast('Link copied!');
  });
  $('refShareTelegramLanding').addEventListener('click', () => shareReferral('telegram'));
  $('refShareXLanding').addEventListener('click', () => shareReferral('x'));
  $('refShareRedditLanding').addEventListener('click', () => shareReferral('reddit'));

  // Load landing referral link on boot
  loadLandingReferral();

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

  /* ---------- Sogni secure login sheet ---------- */

  function sogniLoginError(msg) {
    const el = $('sogniLoginError');
    el.textContent = msg || '';
    el.classList.toggle('hidden', !msg);
  }

  async function submitSogniLogin() {
    const username = $('sogniLoginUser').value.trim();
    const passField = $('sogniLoginPass');
    const password = passField.value;
    passField.value = ''; // out of the field the moment it's read
    sogniLoginError('');
    if (!username || !password) {
      sogniLoginError('Put both the username and the password in.');
      return;
    }
    const submitBtn = $('sogniLoginSubmit');
    submitBtn.disabled = true;
    try {
      const res = await ApiClient.sogniLogin(username, password, currentGirl.id);
      if (res && res.ok) {
        $('sogniLoginUser').value = '';
        closeSheet('sogniLoginSheet');
        addText('her', res.reply || "got it, babe — i'm signed into your sogni now.");
      } else {
        sogniLoginError("That sign-in didn't take — check the login and try again.");
      }
    } catch (e) {
      const msg = String((e && e.message) || '');
      if (msg.includes('429')) {
        sogniLoginError('Too many tries — give it a few minutes, then try again.');
      } else if (msg.includes('403')) {
        sogniLoginError('That sign-in box is owner-only.');
      } else {
        sogniLoginError('Could not reach her just now — try again in a moment.');
      }
    }
    submitBtn.disabled = false;
  }

  $('sogniLoginSubmit').addEventListener('click', submitSogniLogin);
  $('sogniLoginPass').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitSogniLogin();
  });

  /* ---------- companion creator ---------- */
  const Creator = (() => {
    let gender = 'female';
    let photoDataUris = [];

    function setGender(g) {
      gender = g;
      $('creatorGenderF').classList.toggle('active', g === 'female');
      $('creatorGenderM').classList.toggle('active', g === 'male');
    }

    function downscaleImage(file) {
      return new Promise((resolve, reject) => {
        const img = new Image();
        const url = URL.createObjectURL(file);
        img.onload = () => {
          URL.revokeObjectURL(url);
          const maxDim = 1024;
          let w = img.width, h = img.height;
          if (Math.max(w, h) > maxDim) {
            const scale = maxDim / Math.max(w, h);
            w = Math.round(w * scale);
            h = Math.round(h * scale);
          }
          const canvas = document.createElement('canvas');
          canvas.width = w; canvas.height = h;
          canvas.getContext('2d').drawImage(img, 0, 0, w, h);
          resolve(canvas.toDataURL('image/jpeg', 0.85));
        };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('bad image')); };
        img.src = url;
      });
    }

    async function handlePhotos(files) {
      const list = Array.from(files).slice(0, 3);
      photoDataUris = [];
      const preview = $('creatorPhotoPreview');
      preview.innerHTML = '';
      for (const f of list) {
        try {
          const uri = await downscaleImage(f);
          photoDataUris.push(uri);
          const img = document.createElement('img');
          img.src = uri;
          preview.appendChild(img);
        } catch (e) { /* skip bad files */ }
      }
    }

    function showError(msg) {
      const el = $('creatorError');
      el.textContent = msg;
      el.classList.remove('hidden');
    }

    function clearError() {
      $('creatorError').classList.add('hidden');
    }

    async function submit() {
      clearError();
      const name = $('creatorName').value.trim();
      const persona = $('creatorPersona').value.trim();
      const blurb = $('creatorBlurb').value.trim();
      if (!name) return showError('Give your companion a name.');
      if (!photoDataUris.length) return showError('Add at least 1 photo.');
      if (!$('creatorAttest18').checked) return showError('Please confirm this companion is over 18.');
      if (!$('creatorAttestRights').checked) return showError('Please confirm you have the right to use these photos.');

      const btn = $('creatorSubmit');
      btn.disabled = true;
      btn.textContent = 'Creating…';
      try {
        const resp = await ApiClient.createCompanion({
          name, persona, blurb, gender,
          attest_18: true, attest_rights: true,
          images: photoDataUris,
        });
        closeSheet('creatorSheet');
        reset();
        showToast("She's live! Pick her from the girls list.");
        // Refresh the girl list so she appears (refetch: roster is server-scoped)
        loadRoster();
      } catch (e) {
        const msg = (e && e.message) || '';
        if (msg.includes('402')) showError('Creating a companion needs a paid plan.');
        else if (msg.includes('400')) showError('Couldn\'t create her — check your details and try again.');
        else showError('Something went wrong — try again.');
      } finally {
        btn.disabled = false;
        btn.textContent = 'Create Companion';
      }
    }

    function reset() {
      $('creatorName').value = '';
      $('creatorPersona').value = '';
      $('creatorBlurb').value = '';
      $('creatorPhotos').value = '';
      $('creatorPhotoPreview').innerHTML = '';
      $('creatorAttest18').checked = false;
      $('creatorAttestRights').checked = false;
      photoDataUris = [];
      setGender('female');
      clearError();
    }

    function init() {
      const openBtn = $('createCompanionBtn');
      if (openBtn) openBtn.addEventListener('click', () => {
        if (!isLoggedIn()) { showAuth(); return; }
        openSheet('creatorSheet');
      });
      $('creatorGenderF').addEventListener('click', () => setGender('female'));
      $('creatorGenderM').addEventListener('click', () => setGender('male'));
      $('creatorPhotos').addEventListener('change', (e) => handlePhotos(e.target.files));
      $('creatorSubmit').addEventListener('click', submit);
    }

    return { init };
  })();

  Creator.init();

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

  async function bootChat(isFirstHello, hasHistory = false) {
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
    } else if (returning && !hasHistory) {
      addText('her', MockEngine.returnOpener());
    }
  }

  /* ============================================================
     Whispr Feed — public landing page Instagram-style feed.
     Fetches from /api/v1/her-day/public (no auth). Vertical
     scrollable posts: avatar + name + time, media, caption,
     like toggle, chat CTA.
     ============================================================ */
  const WhisprFeed = (() => {
    let posts = [];
    let liked = {};
    try {
      liked = JSON.parse(localStorage.getItem('whisprFeedLikes') || '{}');
    } catch (e) { liked = {}; }

    function timeAgo(createdAt) {
      const created = new Date((createdAt || '').replace(' ', 'T') + 'Z');
      if (isNaN(created)) return '';
      const mins = Math.max(1, Math.round((Date.now() - created) / 60000));
      if (mins < 60) return mins + 'm ago';
      const hrs = Math.round(mins / 60);
      if (hrs < 24) return hrs + 'h ago';
      return Math.round(hrs / 24) + 'd ago';
    }

    function postKey(p) {
      return (p.girl_id || '') + '|' + (p.created_at || '') + '|' + (p.caption || '').slice(0, 40);
    }

    function toggleLike(key, btn) {
      if (liked[key]) { delete liked[key]; } else { liked[key] = 1; }
      try { localStorage.setItem('whisprFeedLikes', JSON.stringify(liked)); } catch (e) {}
      btn.classList.toggle('liked', !!liked[key]);
    }

    function renderMedia(p, mediaBox) {
      mediaBox.innerHTML = '';
      if (p.post_type === 'video' && p.media_url) {
        const v = document.createElement('video');
        v.src = p.media_url;
        v.controls = true;
        v.playsInline = true;
        v.preload = 'metadata';
        v.className = 'feed-media-video';
        mediaBox.appendChild(v);
      } else if (p.media_url) {
        const img = document.createElement('img');
        img.src = p.media_url;
        img.alt = '';
        img.loading = 'lazy';
        img.className = 'feed-media-img';
        mediaBox.appendChild(img);
      } else if (p.post_type === 'voice' && p.voice_url) {
        const a = document.createElement('audio');
        a.src = p.voice_url;
        a.controls = true;
        a.className = 'feed-media-audio';
        mediaBox.appendChild(a);
        const note = document.createElement('div');
        note.className = 'feed-voice-note';
        note.textContent = '\u266A voice note';
        mediaBox.appendChild(note);
      } else {
        const txt = document.createElement('div');
        txt.className = 'feed-text-post';
        txt.textContent = p.caption || '';
        mediaBox.appendChild(txt);
      }
    }

    function renderFeed() {
      const wrap = $('herdayFeed');
      if (!wrap) return;
      wrap.innerHTML = '';
      if (!posts.length) {
        const empty = document.createElement('div');
        empty.className = 'feed-empty';
        empty.textContent = 'New moments drop daily — check back soon.';
        wrap.appendChild(empty);
        return;
      }
      posts.forEach((p) => {
        const key = postKey(p);
        const post = document.createElement('article');
        post.className = 'feed-post';

        // Header: avatar + name + time
        const header = document.createElement('div');
        header.className = 'feed-header';
        const av = document.createElement('img');
        av.className = 'feed-avatar';
        av.src = p.girl_avatar_url || '';
        av.alt = p.girl_name || '';
        av.loading = 'lazy';
        const meta = document.createElement('div');
        meta.className = 'feed-meta';
        const name = document.createElement('span');
        name.className = 'feed-name';
        name.textContent = p.girl_name || '';
        const time = document.createElement('span');
        time.className = 'feed-time';
        time.textContent = timeAgo(p.created_at);
        meta.appendChild(name);
        meta.appendChild(time);
        header.appendChild(av);
        header.appendChild(meta);

        // Media
        const mediaBox = document.createElement('div');
        mediaBox.className = 'feed-media';
        renderMedia(p, mediaBox);

        // Actions: like + chat
        const actions = document.createElement('div');
        actions.className = 'feed-actions';
        const likeBtn = document.createElement('button');
        likeBtn.className = 'feed-like' + (liked[key] ? ' liked' : '');
        likeBtn.setAttribute('aria-label', 'Like');
        likeBtn.innerHTML = '\u2665';
        likeBtn.addEventListener('click', () => toggleLike(key, likeBtn));
        const chatBtn = document.createElement('button');
        chatBtn.className = 'feed-chat-btn';
        chatBtn.textContent = 'Chat';
        chatBtn.addEventListener('click', () => {
          const girl = CHARACTERS.find(c => c.id === (p.girl_id || '').toLowerCase());
          openChat(girl ? girl.id : CHARACTERS[1].id);
        });
        actions.appendChild(likeBtn);
        actions.appendChild(chatBtn);

        // Caption
        const cap = document.createElement('div');
        cap.className = 'feed-caption';
        const capName = document.createElement('span');
        capName.className = 'feed-caption-name';
        capName.textContent = p.girl_name || '';
        cap.appendChild(capName);
        cap.appendChild(document.createTextNode(' ' + (p.caption || '')));

        post.appendChild(header);
        post.appendChild(mediaBox);
        post.appendChild(actions);
        post.appendChild(cap);
        wrap.appendChild(post);
      });
    }

    async function load() {
      try {
        const data = await ApiClient.publicHerDay();
        posts = data.posts || [];
      } catch (e) {
        posts = [];
      }
      renderFeed();
    }

    function init() {
      load();
    }

    return { load, init };
  })();

  WhisprFeed.init();

  // Go.
  loadRoster();
  if (localStorage.getItem('chloe_name')) {
    showApp();
    bootChat(false);
  } else {
    showOnboarding();
  }


})();

