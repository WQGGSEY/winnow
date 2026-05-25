// operator_frontend client glue.
// Deliberately framework-free: small enough to read top-to-bottom.
//
// Concerns covered here:
//   - Settings dialog (subscription_ack / full_auto_mode)
//   - New-thread dialog
//   - Execute-ack modal that wraps "Start grilling / Advance to X" buttons
//   - SSE connection for live grilling and refine chat
//   - Reply form submission (POSTs JSON, echoes into transcript)

(function () {
  'use strict';

  // ---- generic dialog helpers ---------------------------------

  function openDialog(id) {
    const dlg = document.getElementById(id);
    if (dlg && typeof dlg.showModal === 'function') dlg.showModal();
  }
  function closeDialog(id) {
    const dlg = document.getElementById(id);
    if (dlg && typeof dlg.close === 'function') dlg.close();
  }

  document.addEventListener('click', (ev) => {
    const t = ev.target;
    if (t.matches('[data-open-settings]')) { openDialog('settings-dialog'); }
    else if (t.matches('[data-close-settings]')) { closeDialog('settings-dialog'); }
    else if (t.matches('[data-open-new-thread]')) { openDialog('new-thread-dialog'); }
    else if (t.matches('[data-close-new-thread]')) { closeDialog('new-thread-dialog'); }
    else if (t.matches('[data-close-execute-ack]')) { closeDialog('execute-ack-dialog'); }
  });

  // ---- subscription / full auto -------------------------------

  async function postJson(url, body) {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : null,
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`${resp.status}: ${text}`);
    }
    return resp.json();
  }

  document.addEventListener('click', async (ev) => {
    const t = ev.target;
    if (t.dataset.action === 'grant-subscription') {
      try { await postJson('/api/settings/subscription_ack'); location.reload(); }
      catch (e) { alert(`grant failed: ${e}`); }
    } else if (t.dataset.action === 'revoke-subscription') {
      if (!confirm('Revoke subscription_ack? Live phases will be blocked until re-granted.')) return;
      try { await postJson('/api/settings/subscription_ack/revoke'); location.reload(); }
      catch (e) { alert(`revoke failed: ${e}`); }
    }
  });

  document.addEventListener('change', async (ev) => {
    if (ev.target.id !== 'full-auto-toggle') return;
    const enabled = ev.target.checked;
    if (enabled) {
      const ok = confirm(
        'Enable full_auto_mode?\n\n' +
        'Per-phase execute_ack modals will be skipped — live Claude phases ' +
        'launch the moment you click "Start" or "Advance".\n\n' +
        'subscription_ack is NOT bypassed.\n\n' +
        'A persistent ● AUTO badge in the top bar will indicate the mode is active.'
      );
      if (!ok) { ev.target.checked = false; return; }
    }
    try { await postJson('/api/settings/full_auto', { enabled }); location.reload(); }
    catch (e) { alert(`toggle failed: ${e}`); ev.target.checked = !enabled; }
  });

  // ---- execute_ack wrapper for every live-phase launch button -

  document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-execute-ack]');
    if (!btn) return;
    ev.preventDefault();
    const phase = btn.dataset.phase;
    const threadId = btn.dataset.threadId;
    const endpoint = btn.dataset.endpoint;
    const costNote = btn.dataset.costNote || '';
    const fullAuto = document.body.dataset.fullAuto === 'true'
      || document.querySelector('.badge.auto') !== null;

    const launch = async (mode) => {
      try {
        await postJson(endpoint, { mode });
        // Reload the thread page to show the new phase state.
        location.reload();
      } catch (e) {
        alert(`launch failed: ${e}`);
      }
    };

    if (fullAuto) { launch('auto'); return; }

    const body = document.getElementById('execute-ack-body');
    body.innerHTML =
      `<strong>Phase:</strong> ${phase}<br>` +
      `<strong>Thread:</strong> ${threadId}<br>` +
      (costNote ? `<em>${costNote}</em>` : '');
    const form = document.getElementById('execute-ack-form');
    form.onsubmit = (sev) => {
      sev.preventDefault();
      closeDialog('execute-ack-dialog');
      launch('manual');
    };
    openDialog('execute-ack-dialog');
  });

  // ---- rename + delete thread ---------------------------------

  document.addEventListener('click', async (ev) => {
    const renameBtn = ev.target.closest('[data-rename-thread]');
    if (renameBtn) {
      ev.preventDefault();
      const tid = renameBtn.dataset.threadId;
      const current = renameBtn.dataset.currentTitle || '';
      const next = prompt('Rename thread:', current);
      if (next == null) return;
      const trimmed = next.trim();
      if (!trimmed || trimmed === current) return;
      try {
        await postJson(`/api/threads/${tid}/rename`, { title: trimmed });
        location.reload();
      } catch (e) { alert(`rename failed: ${e}`); }
      return;
    }
    const deleteBtn = ev.target.closest('[data-delete-thread]');
    if (deleteBtn) {
      ev.preventDefault();
      const tid = deleteBtn.dataset.threadId;
      const title = deleteBtn.dataset.threadTitle || tid;
      if (!confirm(
        `Delete thread "${title}"?\n\n` +
        'This removes the entire thread directory under runs/threads/ ' +
        'including all phase artifacts. This cannot be undone.\n\n' +
        'If the thread is live, click Abandon first.'
      )) return;
      try {
        await postJson(`/api/threads/${tid}/delete`);
        location.href = '/';
      } catch (e) { alert(`delete failed: ${e}`); }
    }
  });

  // ---- abandon button -----------------------------------------

  document.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-abandon]');
    if (!btn) return;
    ev.preventDefault();
    if (!confirm(
      'Abandon this phase?\n\n' +
      'The running task will be cancelled and the single-active-run lock ' +
      'released. Persisted rounds are kept on disk for inspection.'
    )) return;
    try {
      await postJson(btn.dataset.endpoint);
      location.reload();
    } catch (e) {
      alert(`abandon failed: ${e}`);
    }
  });

  // ---- chat reply form ----------------------------------------

  document.addEventListener('submit', async (ev) => {
    const form = ev.target.closest('[data-action="grilling-reply"], [data-action="refine-reply"]');
    if (!form) return;
    ev.preventDefault();
    const threadId = form.dataset.threadId;
    const phase = form.dataset.action === 'refine-reply' ? 'refine' : 'grilling';
    const fd = new FormData(form);
    const reply = (fd.get('reply') || '').toString().trim();
    if (!reply) return;
    try {
      const resp = await fetch(`/api/threads/${threadId}/${phase}/reply`, {
        method: 'POST',
        body: fd,
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`${resp.status}: ${text}`);
      }
      form.reset();
      // SSE stream will push 'user_reply' so the transcript updates without reload.
    } catch (e) {
      alert(`send failed: ${e}`);
    }
  });

  // ---- SSE streams --------------------------------------------

  const streams = new Map();

  function openStream(threadId, phase) {
    const key = `${threadId}/${phase}`;
    if (streams.has(key)) return;
    const es = new EventSource(`/api/threads/${threadId}/${phase}/stream`);
    streams.set(key, es);
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        handleStreamEvent(threadId, phase, data);
      } catch (e) { console.error('bad SSE payload', e, ev.data); }
    };
    es.onerror = () => { es.close(); streams.delete(key); };
  }

  function handleStreamEvent(threadId, phase, ev) {
    if (ev.type === 'phase_complete' || ev.type === 'phase_failed') {
      // Refresh so the new completed / extracted-card view replaces the chat.
      location.reload();
      return;
    }
    const list = document.getElementById(`chat-${threadId}`);
    if (!list) {
      // Defensive: pre-empty chat panel didn't render for some reason.
      // Reload to recover.
      location.reload();
      return;
    }
    const li = document.createElement('li');
    if (ev.type === 'ask') {
      li.className = 'msg ask';
      const idx = list.querySelectorAll('.msg.ask').length + 1;
      li.innerHTML = `<strong>Q${idx}</strong><div></div>`;
      li.querySelector('div').textContent = ev.question;
      // First ASK arrived — enable reply form, hide "thinking" dots.
      setReplyEnabled(threadId, true);
      setThinking(threadId, false);
    } else if (ev.type === 'user_reply') {
      li.className = 'msg reply';
      const idx = list.querySelectorAll('.msg.reply').length + 1;
      li.innerHTML = `<strong>A${idx}</strong><div></div>`;
      li.querySelector('div').textContent = ev.text;
      // Reply submitted — disable form, show "thinking" until next ASK.
      setReplyEnabled(threadId, false);
      setThinking(threadId, true);
    } else if (ev.type === 'scaffold_start') {
      li.className = 'msg system';
      li.innerHTML = `<strong>Domain scaffolding started</strong>
        <div>Slug: <code>${ev.domain_slug}</code>. The agent will now propose
        every file under the new domain folder; each is syntax-checked before staging.</div>`;
    } else if (ev.type === 'propose_file') {
      li.className = `msg propose-file file-${ev.status}`;
      const checks = ev.checks || {};
      const checksHTML = Object.entries(checks).map(([k, v]) =>
        `<span class="propose-check check-${v}">${escapeHTML(k)}: ${escapeHTML(String(v)).slice(0, 60)}</span>`
      ).join(' ');
      const mp = ev.manifest_progress || [0, 0];
      li.innerHTML = `<strong>📄 PROPOSE_FILE</strong>
        <div>
          <code>${escapeHTML(ev.relative_path)}</code>
          <div class="propose-status status-${ev.status}">
            ${ev.status === 'ok' ? '✓ staged' : '✕ failed'} ${checksHTML}
          </div>
          <div class="manifest-progress">manifest ${mp[0]} / ${mp[1]}</div>
        </div>`;
    } else if (ev.type === 'dry_import') {
      li.className = `msg ${ev.status === 'ok' ? 'system' : 'done-rejected'}`;
      if (ev.status === 'ok') {
        li.innerHTML = `<strong>✓ Dry-import passed</strong>
          <div>All staged modules import cleanly. Finalizing domain on disk…</div>`;
      } else {
        const missing = (ev.manifest_missing || []).join(', ');
        li.innerHTML = `<strong>⚠ DONE rejected</strong>
          <div>
            ${missing ? `Missing manifest: <code>${escapeHTML(missing)}</code><br>` : ''}
            ${ev.error ? `Error: <code>${escapeHTML(ev.error).slice(0, 200)}</code>` : ''}
          </div>`;
      }
    } else if (ev.type === 'scaffold_complete') {
      li.className = 'msg system';
      li.innerHTML = `<strong>✓ Domain scaffolded</strong>
        <div>Created <code>${escapeHTML(ev.domain_slug)}</code> at
        <code>${escapeHTML(ev.final_destination || '')}</code>.</div>`;
    } else {
      return;
    }
    list.appendChild(li);
    li.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }

  function setReplyEnabled(threadId, enabled) {
    const forms = document.querySelectorAll(`form[data-thread-id="${threadId}"]`);
    forms.forEach((f) => {
      const ta = f.querySelector('textarea');
      const btn = f.querySelector('button[type="submit"]');
      if (ta) {
        ta.disabled = !enabled;
        if (enabled) ta.placeholder = 'Reply to the agent’s question…';
      }
      if (btn) btn.disabled = !enabled;
    });
  }

  const thinkingTimers = new Map();

  function setThinking(threadId, on) {
    const el = document.getElementById(`thinking-${threadId}`);
    if (!el) return;
    el.hidden = !on;
    // Maintain a per-thread "elapsed seconds since the latest reply"
    // counter. Honest signal — the agent is alive and we're waiting,
    // with no fake progress estimate. Resets on each transition into
    // thinking; stops cleanly on transition out.
    const prior = thinkingTimers.get(threadId);
    if (prior) {
      clearInterval(prior.handle);
      thinkingTimers.delete(threadId);
    }
    if (!on) return;
    const target = el.querySelector('[data-thinking-elapsed]');
    if (!target) return;
    const startedAt = Date.now();
    target.textContent = '0.0s';
    const handle = setInterval(() => {
      const seconds = (Date.now() - startedAt) / 1000;
      target.textContent = `${seconds.toFixed(1)}s`;
    }, 100);
    thinkingTimers.set(threadId, { handle, startedAt });
  }

  function escapeHTML(s) {
    const div = document.createElement('div');
    div.textContent = s == null ? '' : String(s);
    return div.innerHTML;
  }

  document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-resume-stream]');
    if (!btn) return;
    ev.preventDefault();
    openStream(btn.dataset.threadId, btn.dataset.phase || 'grilling');
  });

  // Auto-open stream when any live phase is running. Grilling and refine
  // use the SSE channel for chat (ask / user_reply / scaffold_*); market
  // and production use it only to signal phase_complete / phase_failed so
  // the page reloads automatically instead of requiring a manual refresh.
  document.addEventListener('DOMContentLoaded', () => {
    const accordion = document.querySelector('.accordion');
    if (!accordion) return;
    const threadId = accordion.dataset.threadId;
    const phase = accordion.dataset.currentPhase;
    const status = accordion.dataset.phaseStatus;
    const livePhases = new Set(['grilling', 'refine', 'market', 'production']);
    if (threadId && livePhases.has(phase)
        && (status === 'running' || status === 'awaiting_input')) {
      openStream(threadId, phase);
    }
  });
})();
