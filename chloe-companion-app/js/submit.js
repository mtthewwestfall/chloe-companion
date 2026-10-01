/* CreatorFlow — custom-companion creator (2026-10-01), ported from the
   Sorority-house companion creator. Paid members create a companion with a
   name, gender, personality, tagline, and 1-3 REAL photos, plus 18+ / rights
   attestation. Every photo is AI-moderated server-side; the character goes
   live immediately with the real photos (no painted portrait, no queue).

   Plain script (no modules), exposes window.SubmitFlow (name kept for its
   existing callers). Uses window.ApiClient for server calls.
*/
window.SubmitFlow = (() => {
  'use strict';

  const MAX_PHOTOS = 3;
  let photos = []; // data URIs, downscaled
  let gender = 'female';

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function toast(msg) {
    const t = $('toast');
    if (!t) return;
    t.textContent = msg;
    t.classList.remove('hidden');
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.add('hidden'), 2600);
  }

  /* ---------- image picking + downscale ---------- */

  function downscale(file) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      const url = URL.createObjectURL(file);
      img.onload = () => {
        URL.revokeObjectURL(url);
        const maxDim = 1024;
        let w = img.naturalWidth, h = img.naturalHeight;
        const scale = Math.min(1, maxDim / Math.max(w, h));
        w = Math.round(w * scale); h = Math.round(h * scale);
        const c = document.createElement('canvas');
        c.width = w; c.height = h;
        c.getContext('2d').drawImage(img, 0, 0, w, h);
        resolve(c.toDataURL('image/jpeg', 0.85));
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('bad image')); };
      img.src = url;
    });
  }

  function renderPhotoStrip() {
    const strip = $('subPhotoStrip');
    if (!strip) return;
    strip.innerHTML = '';
    photos.forEach((uri, i) => {
      const wrap = document.createElement('div');
      wrap.className = 'sub-photo';
      wrap.innerHTML = `<img src="${uri}" alt=""><button type="button" data-i="${i}" aria-label="Remove">✕</button>`;
      wrap.querySelector('button').addEventListener('click', () => {
        photos.splice(i, 1);
        renderPhotoStrip();
      });
      strip.appendChild(wrap);
    });
    const add = $('subPhotoAdd');
    if (add) add.style.display = photos.length >= MAX_PHOTOS ? 'none' : '';
    const count = $('subPhotoCount');
    if (count) count.textContent = `${photos.length}/${MAX_PHOTOS} photos`;
  }

  function setGender(g) {
    gender = g === 'male' ? 'male' : 'female';
    document.querySelectorAll('#subGenderPills button').forEach((b) =>
      b.classList.toggle('active', b.dataset.gender === gender));
  }

  /* ---------- sheet ---------- */

  async function openSubmissionSheet() {
    // The "Create a companion" button lives inside the profile sheet.
    // Close it first — both sheets share z-index 40 and the profile sheet
    // sits later in the DOM, so it would otherwise cover this form.
    const prof = $('profileSheet');
    if (prof) prof.classList.add('hidden');
    photos = [];
    renderPhotoStrip();
    setGender('female');
    const nameEl = $('subName'), personaEl = $('subPersona'),
          blurbEl = $('subBlurb');
    if (nameEl) nameEl.value = '';
    if (personaEl) personaEl.value = '';
    if (blurbEl) blurbEl.value = '';
    const a18 = $('subAttest18'), ar = $('subAttestRights');
    if (a18) a18.checked = false;
    if (ar) ar.checked = false;
    const status = $('subStatus');
    if (status) { status.textContent = ''; status.className = 'sheet-sub'; }
    $('submitSheet').classList.remove('hidden');
  }

  function closeSubmissionSheet() {
    $('submitSheet').classList.add('hidden');
  }

  async function createCompanion() {
    const status = $('subStatus');
    const say = (t, bad) => {
      if (!status) return;
      status.textContent = t;
      status.className = 'sheet-sub' + (bad ? ' bad' : '');
    };
    const name = ($('subName').value || '').trim();
    const persona = ($('subPersona').value || '').trim();
    const blurb = ($('subBlurb').value || '').trim();
    const a18 = $('subAttest18').checked;
    const ar = $('subAttestRights').checked;
    if (!name) { say('Give them a name.', true); return; }
    if (!photos.length) { say('Add at least one photo.', true); return; }
    if (!a18) { say('Please confirm everyone in the photos is 18 or older.', true); return; }
    if (!ar) { say('Please confirm you have the right to use these photos.', true); return; }
    const btn = $('subSubmitBtn');
    btn.disabled = true;
    // Bounded visible phases — a stall shows progress, never a dead button.
    const t0 = Date.now();
    say('Checking your photos…');
    const phaseTimer = setInterval(() => {
      if (Date.now() - t0 > 12000) say('Building their personality…');
    }, 4000);
    try {
      const res = await window.ApiClient.createCompanion({
        name, persona, blurb, gender,
        attest_18: a18, attest_rights: ar, images: photos,
      });
      const comp = (res && res.companion) || {};
      closeSubmissionSheet();
      toast('Say hi to ' + (comp.name || name) + ' ✨');
      // Land on the new companion: the picker restores the saved id on boot.
      try { localStorage.setItem('chloe_char_id', comp.id); } catch (e) {}
      location.reload();
    } catch (e) {
      const d = (e && e.detail) || '';
      say(d === 'paid_tier_required'
        ? 'Custom companions are for paid members — pick a plan in your profile.'
        : 'Couldn\'t create: ' + (window.ApiClient.friendlyDetail(d) || d || 'try again.'), true);
    } finally {
      clearInterval(phaseTimer);
      btn.disabled = false;
    }
  }

  /* ---------- my submissions (profile section) ---------- */

  const STATUS_LABEL = {
    pending: 'Under review',
    flagged: 'Under review',
    approved: 'Live 🎉',
    rejected: 'Not approved',
  };

  async function refreshMySubmissions() {
    const list = $('mySubsList');
    if (!list) return;
    list.innerHTML = '<div class="sheet-sub">Loading…</div>';
    try {
      const r = await window.ApiClient.mySubmissions();
      const subs = r.submissions || [];
      if (!subs.length) {
        list.innerHTML = '<div class="sheet-sub dim">No submissions yet.</div>';
        return;
      }
      list.innerHTML = '';
      subs.forEach((s) => {
        const row = document.createElement('div');
        row.className = 'sub-row';
        const label = STATUS_LABEL[s.status] || s.status;
        row.innerHTML =
          `<span class="sub-row-name">${esc(s.name)}</span>` +
          `<span class="sub-row-status st-${esc(s.status)}">${esc(label)}</span>` +
          (s.status === 'rejected' && s.review_notes
            ? `<div class="sheet-sub dim">${esc(s.review_notes)}</div>` : '');
        list.appendChild(row);
      });
    } catch (e) {
      list.innerHTML = '<div class="sheet-sub dim">Couldn\'t load submissions.</div>';
    }
  }

  /* ---------- wire up ---------- */

  function init() {
    const fileInput = $('subPhotoInput');
    if (fileInput) {
      fileInput.addEventListener('change', async () => {
        const files = Array.from(fileInput.files || []).slice(0, MAX_PHOTOS - photos.length);
        for (const f of files) {
          if (!f.type.startsWith('image/')) continue;
          try {
            photos.push(await downscale(f));
          } catch (e) { /* skip bad file */ }
        }
        fileInput.value = '';
        renderPhotoStrip();
      });
    }
    const add = $('subPhotoAdd');
    if (add) add.addEventListener('click', () => $('subPhotoInput').click());
    document.querySelectorAll('#subGenderPills button').forEach((b) =>
      b.addEventListener('click', () => setGender(b.dataset.gender)));
    const btn = $('subSubmitBtn');
    if (btn) btn.addEventListener('click', createCompanion);
    document.querySelectorAll('[data-close="submitSheet"]').forEach((b) =>
      b.addEventListener('click', closeSubmissionSheet));
    const openBtn = $('createCompanionBtn');
    if (openBtn) openBtn.addEventListener('click', openSubmissionSheet);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return { openSubmissionSheet, closeSubmissionSheet, refreshMySubmissions };
})();
