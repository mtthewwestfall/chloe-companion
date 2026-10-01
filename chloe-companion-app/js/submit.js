/* SubmitFlow — custom-companion submissions (2026-10-01).
   Approval-only: customers design him or her (any gender), submit name +
   persona + up to 3 photos. Every photo is AI-reviewed (18+ / no nudity)
   before the owner ever sees it. Nothing goes live without owner approval.

   Plain script (no modules), exposes window.SubmitFlow.
   Uses window.ApiClient for server calls.
*/
window.SubmitFlow = (() => {
  'use strict';

  const MAX_PHOTOS = 3;
  let photos = []; // data URIs, downscaled
  let policyText = '';

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

  /* ---------- sheet ---------- */

  async function openSubmissionSheet() {
    photos = [];
    renderPhotoStrip();
    const nameEl = $('subName'), personaEl = $('subPersona'),
          blurbEl = $('subBlurb'), ackEl = $('subPolicyAck');
    if (nameEl) nameEl.value = '';
    if (personaEl) personaEl.value = '';
    if (blurbEl) blurbEl.value = '';
    if (ackEl) ackEl.checked = false;
    const status = $('subStatus');
    if (status) { status.textContent = ''; status.className = 'sheet-sub'; }
    if (!policyText) {
      try {
        const r = await window.ApiClient.submissionPolicy();
        policyText = r.policy || '';
      } catch (e) { policyText = ''; }
    }
    const pol = $('subPolicyText');
    if (pol) pol.textContent = policyText || 'Submission policy unavailable — try again in a bit.';
    $('submitSheet').classList.remove('hidden');
  }

  function closeSubmissionSheet() {
    $('submitSheet').classList.add('hidden');
  }

  async function submitForm() {
    const status = $('subStatus');
    const say = (t, bad) => {
      if (!status) return;
      status.textContent = t;
      status.className = 'sheet-sub' + (bad ? ' bad' : '');
    };
    const name = ($('subName').value || '').trim();
    const persona = ($('subPersona').value || '').trim();
    const blurb = ($('subBlurb').value || '').trim();
    const ack = $('subPolicyAck').checked;
    if (!name) { say('Give them a name.', true); return; }
    if (!ack) { say('Please accept the submission policy.', true); return; }
    if (!photos.length) { say('Add at least one photo.', true); return; }
    say('Submitting — your photos are being AI-reviewed…');
    $('subSubmitBtn').disabled = true;
    try {
      const res = await window.ApiClient.createSubmission({
        name, persona, blurb, images: photos, policy_ack: ack,
      });
      if (res.status === 'rejected') {
        say('Not approved: ' + (res.review && res.review.notes || 'did not pass review.'), true);
      } else if (res.status === 'flagged') {
        say('Received — it needs a closer look before approval. We\'ll be in touch.', false);
      } else {
        say('Sent for review! You\'ll see them here when approved.', false);
      }
      photos = [];
      renderPhotoStrip();
      refreshMySubmissions();
    } catch (e) {
      const d = (e && e.detail) || '';
      say(d === 'paid_tier_required'
        ? 'Custom companions are for paid members — pick a plan in your profile.'
        : 'Couldn\'t submit: ' + (window.ApiClient.friendlyDetail(d) || d || 'try again.'), true);
    } finally {
      $('subSubmitBtn').disabled = false;
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
    const btn = $('subSubmitBtn');
    if (btn) btn.addEventListener('click', submitForm);
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
