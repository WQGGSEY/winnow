// operator_frontend client glue.
// Deliberately framework-free: small enough to read top-to-bottom.
//
// Concerns covered here:
//   - Settings dialog (subscription_ack / full_auto_mode)
//   - New-thread dialog
//   - Execute-ack modal that wraps "Start grilling / Advance to X" buttons
//   - SSE connection for live grilling chat
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
    const form = ev.target.closest('[data-action="grilling-reply"]');
    if (!form) return;
    ev.preventDefault();
    const threadId = form.dataset.threadId;
    const phase = 'grilling';
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
    // Thinking + reply-form state — do this BEFORE the chat-list lookup
    // so refine (which has its own refine-rounds renderer, no chat-${tid}
    // list) still gets the live counter and form toggle.
    if (ev.type === 'ask') {
      setReplyEnabled(threadId, true);
      setThinking(threadId, false);
    } else if (ev.type === 'user_reply') {
      setReplyEnabled(threadId, false);
      setThinking(threadId, true);
    }
    const list = document.getElementById(`chat-${threadId}`);
    if (!list) {
      // No live chat list (refine panel renders its own round cards
      // server-side). For ask, reload after a moment so the new pending_ask
      // and round are rendered. For user_reply, do NOT reload — the
      // thinking counter just started ticking. For other live events
      // (scaffold_*, propose_file, dry_import), reload to surface them.
      if (ev.type === 'ask' || ev.type === 'dry_import'
          || ev.type === 'propose_file' || ev.type === 'scaffold_start'
          || ev.type === 'scaffold_complete') {
        setTimeout(() => location.reload(), 200);
      }
      return;
    }
    const li = document.createElement('li');
    if (ev.type === 'ask') {
      li.className = 'msg ask';
      const idx = list.querySelectorAll('.msg.ask').length + 1;
      li.innerHTML = `<strong>Q${idx}</strong><div></div>`;
      li.querySelector('div').textContent = ev.question;
    } else if (ev.type === 'user_reply') {
      li.className = 'msg reply';
      const idx = list.querySelectorAll('.msg.reply').length + 1;
      li.innerHTML = `<strong>A${idx}</strong><div></div>`;
      li.querySelector('div').textContent = ev.text;
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
    const livePhases = new Set(['grilling', 'market', 'production']);
    if (threadId && livePhases.has(phase)
        && (status === 'running' || status === 'awaiting_input')) {
      openStream(threadId, phase);
    }
  });
})();

/* === Claim tree + per-node dialog renderer (production phase) ============ */
(function () {
  function buildTree(rootContainer, state, dialogs) {
    const nodes = state.nodes || [];
    if (!nodes.length) {
      rootContainer.innerHTML = "<p class='muted'>(no nodes)</p>";
      return;
    }
    // Build parent → children map.
    const byParent = new Map();
    let root = null;
    for (const n of nodes) {
      if (n.parent == null) {
        root = root || n; // first orphan
        continue;
      }
      if (!byParent.has(n.parent)) byParent.set(n.parent, []);
      byParent.get(n.parent).push(n);
    }
    if (!root) {
      // Fallback: pick any.
      root = nodes[0];
    }
    const ul = document.createElement("ul");
    ul.appendChild(renderNode(root, byParent, dialogs, rootContainer));
    rootContainer.innerHTML = "";
    rootContainer.appendChild(ul);
  }

  function renderNode(node, byParent, dialogs, rootContainer) {
    const li = document.createElement("li");
    const row = document.createElement("div");
    row.className = "tree-node status-" + (node.status || "unknown");
    row.dataset.nodeId = node.id;

    const dot = document.createElement("span");
    dot.className = "dot dot-" + (node.status || "ready");
    row.appendChild(dot);

    const typeBadge = document.createElement("span");
    typeBadge.className = "type-badge chip-" + (node.type || "");
    typeBadge.textContent = node.type || "?";
    row.appendChild(typeBadge);

    const idSpan = document.createElement("span");
    idSpan.textContent = " " + node.id;
    idSpan.style.fontWeight = "500";
    row.appendChild(idSpan);

    const claim = document.createElement("span");
    claim.className = "node-claim";
    claim.title = node.claim_under_test || "";
    claim.textContent = " — " + (node.claim_under_test || "(no claim)");
    row.appendChild(claim);

    row.addEventListener("click", function (ev) {
      ev.stopPropagation();
      // Mark selection.
      rootContainer
        .querySelectorAll(".tree-node.selected")
        .forEach((el) => el.classList.remove("selected"));
      row.classList.add("selected");
      // Render dialog panel.
      renderDialogPanel(node, dialogs);
    });
    li.appendChild(row);

    const children = byParent.get(node.id) || [];
    if (children.length) {
      const childUl = document.createElement("ul");
      for (const c of children) {
        childUl.appendChild(renderNode(c, byParent, dialogs, rootContainer));
      }
      li.appendChild(childUl);
    }
    return li;
  }

  function renderDialogPanel(node, dialogs) {
    const panel = document.querySelector(
      "[id^='dialog-panel-']"
    );
    if (!panel) return;
    const entries = (dialogs || {})[node.id] || [];
    if (!entries.length) {
      panel.innerHTML =
        "<p class='muted'>" +
        node.id +
        ": (no dialog recorded for this node)</p>";
      return;
    }
    let html =
      "<h4 style='margin:0 0 8px 0;font-size:13px;'>" +
      node.id +
      "</h4>";
    for (const e of entries) {
      const speakerClass = "speaker-" + e.speaker;
      const speakerLabel =
        e.speaker === "professor" ? "교수님" : "대학원생";
      html +=
        "<div class='dialog-entry'>" +
        "<span class='" +
        speakerClass +
        "'>" +
        speakerLabel +
        "</span>" +
        "<span class='dialog-intent'>" +
        (e.intent || "") +
        "</span>" +
        "<div class='dialog-text'>" +
        escapeHtml(stripToolLeak(e.text || "")) +
        "</div>" +
        "</div>";
    }
    panel.innerHTML = html;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Trim trailing Claude Code tool-call envelope leakage from dialog text
  // at display time so pre-fix entries on disk still render cleanly.
  // Matches the server-side _strip_tool_envelope_leak() in mcp_server.py.
  const TOOL_ENVELOPE_TAIL = new RegExp(
    [
      "</\\s*commentary\\s*>",
      "</\\s*response_to_grad_student\\s*>",
      "<\\s*parameter\\s+name\\s*=\\s*\"",
      "<\\s*/?\\s*(?:invoke|function_calls|antml:[a-z_]+)\\b",
    ].join("|") + "[\\s\\S]*$",
    "i"
  );
  function stripToolLeak(s) {
    if (s == null) return "";
    return String(s).replace(TOOL_ENVELOPE_TAIL, "").replace(/\s+$/, "");
  }

  function init() {
    document.querySelectorAll(".claim-tree[data-tree-state]").forEach((el) => {
      let state, dialogs;
      try {
        state = JSON.parse(el.dataset.treeState);
        dialogs = JSON.parse(el.dataset.nodeDialogs || "{}");
      } catch (err) {
        el.innerHTML = "<p class='muted'>(tree state parse error)</p>";
        return;
      }
      buildTree(el, state, dialogs);
    });
  }

  document.addEventListener("DOMContentLoaded", init);
  // HTMX swaps may re-render the production panel; rebind on every after-swap.
  document.body.addEventListener("htmx:afterSwap", init);
})();

/* (legacy MCP-handoff modal removed — replaced by thread_supervisor +
   the supervisor card on the production panel. Operator clicks
   ▶ Start supervisor instead of running commands by hand.) */

/* === Per-thread MCP model selector ====================================== */
(function () {
  document.addEventListener("change", async (ev) => {
    const sel = ev.target.closest("select[data-mcp-model-select]");
    if (!sel) return;
    const wrap = sel.closest(".mcp-model-picker");
    if (!wrap) return;
    const tid = wrap.dataset.threadId;
    const model = sel.value;
    try {
      const resp = await fetch(`/api/threads/${tid}/mcp_model`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ mcp_model: model }),
      });
      if (!resp.ok) {
        const text = await resp.text();
        alert("Failed to save model selection: " + text.slice(0, 200));
      }
    } catch (err) {
      alert("Network error saving model selection: " + err);
    }
  });
})();

/* === "last MCP commit Xs ago" relative-time updater ===================== */
(function () {
  function rel(mtime) {
    const dt = Date.now() / 1000 - mtime;
    if (dt < 0) return "just now";
    if (dt < 60) return `${Math.floor(dt)}s ago`;
    if (dt < 3600) return `${Math.floor(dt / 60)}m ${Math.floor(dt % 60)}s ago`;
    if (dt < 86400) return `${Math.floor(dt / 3600)}h ago`;
    return `${Math.floor(dt / 86400)}d ago`;
  }
  function refresh() {
    document.querySelectorAll("[data-mcp-since]").forEach((el) => {
      const mtime = parseFloat(el.dataset.mcpSince);
      if (!Number.isFinite(mtime)) return;
      el.textContent = rel(mtime);
    });
  }
  document.addEventListener("DOMContentLoaded", refresh);
  document.body.addEventListener("htmx:afterSwap", refresh);
  setInterval(refresh, 1000);
})();

/* === PR10: Supervisor start/stop buttons ====================================== */
(function () {
  async function postJson(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (_) {}
      alert('Supervisor request failed (' + r.status + '): ' + detail);
      throw new Error('failed: ' + r.status);
    }
    return r.json();
  }
  function refreshProductionPanel() {
    // Dispatch the custom event the production-panel-poll div listens
    // for via `hx-trigger="every 4s, supervisor-state-changed from:body"`.
    // HTMX fetches the partial, swaps outerHTML, scroll position is
    // preserved, no flicker. Falls back to location.reload only if
    // htmx is somehow missing.
    if (window.htmx) {
      document.body.dispatchEvent(new CustomEvent('supervisor-state-changed'));
    } else {
      setTimeout(() => location.reload(), 200);
    }
  }

  function showInlineFeedback(btn, text, klass) {
    let note = btn.parentElement.querySelector('.supervisor-inline-feedback');
    if (!note) {
      note = document.createElement('span');
      note.className = 'supervisor-inline-feedback';
      btn.parentElement.appendChild(note);
    }
    note.textContent = text;
    note.className = 'supervisor-inline-feedback ' + (klass || '');
  }

  function bind(root) {
    (root || document).querySelectorAll('.supervisor-start-btn').forEach((btn) => {
      if (btn.dataset._bound) return;
      btn.dataset._bound = '1';
      btn.addEventListener('click', async () => {
        const tid = btn.dataset.threadId;
        const sel = document.getElementById('supervisor-scope-' + tid);
        const target_scope = sel ? sel.value : 'directional';
        btn.disabled = true;
        const originalText = btn.textContent;
        btn.textContent = '⏳ Starting supervisor…';
        showInlineFeedback(btn, '', 'pending');
        try {
          const data = await postJson(
            '/api/threads/' + tid + '/supervisor/start',
            {target_scope: target_scope}
          );
          btn.textContent = '✓ Started (pid=' + data.pid + ')';
          showInlineFeedback(
            btn,
            'Supervisor pid=' + data.pid + ' spawned.',
            'ok'
          );
          // Trigger an immediate HTMX swap of the production panel so
          // the supervisor card flips from idle → running without a
          // full page reload (preserves scroll, no flicker).
          refreshProductionPanel();
        } catch (e) {
          btn.textContent = originalText;
          btn.disabled = false;
          showInlineFeedback(btn, 'Start failed: ' + e.message, 'error');
        }
      });
    });
    (root || document).querySelectorAll('.supervisor-stop-btn').forEach((btn) => {
      if (btn.dataset._bound) return;
      btn.dataset._bound = '1';
      btn.addEventListener('click', async () => {
        if (!confirm(
          'Stop supervisor?\n\n' +
          'Sends SIGTERM first (supervisor cascades it to the active ' +
          'claude subprocess so the cycle ends in seconds). If it does ' +
          'not exit within 3s, automatically escalates to SIGKILL. ' +
          'Lock file is cleaned up either way.'
        )) return;
        const tid = btn.dataset.threadId;
        btn.disabled = true;
        const originalText = btn.textContent;
        btn.textContent = '⏳ Stopping…';
        try {
          const data = await postJson('/api/threads/' + tid + '/supervisor/stop');
          const signals = (data.signals_sent || []).join(' → ') || 'already exited';
          showInlineFeedback(btn, '✓ Stopped (' + signals + ').', 'ok');
          // Optimistic DOM update: hide the Stop button immediately
          // so the operator sees instant feedback even before HTMX
          // swap fetches the fresh panel.
          btn.style.display = 'none';
          refreshProductionPanel();
        } catch (e) {
          btn.textContent = originalText;
          btn.disabled = false;
          showInlineFeedback(btn, 'Stop failed: ' + e.message, 'error');
        }
      });
    });
    // PR12: "advance-to-production-link" in the market panel — scrolls to
    // the production accordion section and opens it if collapsed.
    (root || document).querySelectorAll('.advance-to-production-link').forEach((a) => {
      if (a.dataset._bound) return;
      a.dataset._bound = '1';
      a.addEventListener('click', (ev) => {
        ev.preventDefault();
        const target = document.querySelector(
          '[data-phase="production"], .accordion-item[data-phase="production"], #production'
        );
        if (target) {
          // If wrapped in a <details>, open it.
          if (target.tagName === 'DETAILS') target.open = true;
          const details = target.closest('details');
          if (details) details.open = true;
          target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    });
  }
  document.addEventListener('DOMContentLoaded', () => bind(document));
  document.body.addEventListener('htmx:afterSwap', (e) => bind(e.target));
})();


/* === Live elapsed-since counter ============================================= */
(function () {
  function bind(root) {
    (root || document).querySelectorAll('[data-elapsed-since]').forEach((el) => {
      if (el.dataset._elapsedBound) return;
      el.dataset._elapsedBound = '1';
      const since = parseFloat(el.dataset.elapsedSince);
      if (!Number.isFinite(since)) {
        el.textContent = '—';
        return;
      }
      const tick = () => {
        if (!el.isConnected) {
          clearInterval(handle);
          return;
        }
        const sec = Date.now() / 1000 - since;
        if (sec < 0) {
          el.textContent = '0.0s';
        } else if (sec < 60) {
          el.textContent = `${sec.toFixed(1)}s`;
        } else if (sec < 3600) {
          const m = Math.floor(sec / 60);
          const s = Math.floor(sec % 60);
          el.textContent = `${m}m ${s.toString().padStart(2, '0')}s`;
        } else {
          const h = Math.floor(sec / 3600);
          const m = Math.floor((sec % 3600) / 60);
          el.textContent = `${h}h ${m}m`;
        }
      };
      tick();
      const handle = setInterval(tick, 200);
    });
  }
  document.addEventListener('DOMContentLoaded', () => bind(document));
  document.body.addEventListener('htmx:afterSwap', (e) => bind(e.target));
})();


/* === Supervisor live log stream ============================================= *
 * Body-level singleton. Survives htmx panel swaps: the EventSource lives on
 * window.__supervisorLogStreams[threadId], and after each swap we re-bind to
 * the new placeholder <pre> elements and re-render from in-memory ring buffers.
 * Source: /api/threads/{tid}/supervisor/log/stream (SSE, 500ms tail-follow).
 * ============================================================================ */
(function () {
  const RING_MAX = 600;
  const RENDER_CAP = 400;
  const w = window;
  if (!w.__supervisorLogStreams) w.__supervisorLogStreams = {};

  function streamFor(threadId) {
    let st = w.__supervisorLogStreams[threadId];
    if (st) return st;
    st = {
      threadId,
      es: null,
      buffers: { supervisor: [], subprocess: [] },
      statuses: { supervisor: 'connecting…', subprocess: 'connecting…' },
      stickyBottom: { supervisor: true, subprocess: true },
    };
    w.__supervisorLogStreams[threadId] = st;
    open(st);
    return st;
  }

  function push(st, kind, line) {
    const buf = st.buffers[kind];
    if (!buf) return;
    buf.push(line);
    if (buf.length > RING_MAX) buf.splice(0, buf.length - RING_MAX);
  }

  function setStatus(st, kind, text) {
    st.statuses[kind] = text;
    document
      .querySelectorAll(`[data-supervisor-log-status="${kind}"]`)
      .forEach((el) => {
        const sec = el.closest('[data-supervisor-log-section]');
        if (sec && sec.dataset.threadId && sec.dataset.threadId !== st.threadId) return;
        el.textContent = `live · ${text}`;
      });
  }

  function render(st, kind) {
    const pre = document.querySelector(
      `pre[data-supervisor-log="${kind}"][data-thread-id="${st.threadId}"]`
    );
    if (!pre) return;
    const buf = st.buffers[kind];
    const slice = buf.length > RENDER_CAP ? buf.slice(-RENDER_CAP) : buf;
    pre.textContent = slice.join('\n');
    if (st.stickyBottom[kind]) {
      pre.scrollTop = pre.scrollHeight;
    }
  }

  function bindScrollListeners(st) {
    ['supervisor', 'subprocess'].forEach((kind) => {
      const pre = document.querySelector(
        `pre[data-supervisor-log="${kind}"][data-thread-id="${st.threadId}"]`
      );
      if (!pre || pre.dataset._scrollBound) return;
      pre.dataset._scrollBound = '1';
      pre.addEventListener('scroll', () => {
        const nearBottom =
          pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
        st.stickyBottom[kind] = nearBottom;
      });
    });
  }

  function open(st) {
    if (st.es) return;
    try {
      const url = `/api/threads/${st.threadId}/supervisor/log/stream`;
      const es = new EventSource(url);
      st.es = es;
      setStatus(st, 'supervisor', 'connecting…');
      setStatus(st, 'subprocess', 'connecting…');

      es.onopen = () => {
        // Stream is alive. If a kind's snapshot hasn't arrived yet (log file
        // doesn't exist on disk yet, e.g. supervisor just started), reflect
        // that instead of leaving the badge stuck at "connecting…".
        ['supervisor', 'subprocess'].forEach((kind) => {
          if (!st.buffers[kind] || st.buffers[kind].length === 0) {
            setStatus(st, kind, 'connected · waiting for log');
          } else {
            setStatus(st, kind, 'connected');
          }
        });
      };

      es.addEventListener('snapshot', (ev) => {
        try {
          const data = JSON.parse(ev.data);
          const kind = data.kind;
          const lines = Array.isArray(data.lines) ? data.lines : [];
          if (!st.buffers[kind]) return;
          st.buffers[kind] = lines.slice(-RING_MAX);
          setStatus(st, kind, 'connected');
          render(st, kind);
        } catch (_) {}
      });

      es.addEventListener('append', (ev) => {
        try {
          const data = JSON.parse(ev.data);
          push(st, data.kind, data.line);
          render(st, data.kind);
        } catch (_) {}
      });

      es.onerror = () => {
        setStatus(st, 'supervisor', 'reconnecting…');
        setStatus(st, 'subprocess', 'reconnecting…');
      };
    } catch (err) {
      setStatus(st, 'supervisor', 'error');
      setStatus(st, 'subprocess', 'error');
    }
  }

  function close(threadId) {
    const st = w.__supervisorLogStreams[threadId];
    if (!st) return;
    if (st.es) {
      try { st.es.close(); } catch (_) {}
    }
    delete w.__supervisorLogStreams[threadId];
  }

  function bind(/* root */) {
    const sections = document.querySelectorAll('[data-supervisor-log-section]');
    const seen = new Set();
    sections.forEach((sec) => {
      const tid = sec.dataset.threadId;
      if (!tid) return;
      seen.add(tid);
      const st = streamFor(tid);
      bindScrollListeners(st);
      render(st, 'supervisor');
      render(st, 'subprocess');
      setStatus(st, 'supervisor', st.statuses.supervisor);
      setStatus(st, 'subprocess', st.statuses.subprocess);
    });
    Object.keys(w.__supervisorLogStreams).forEach((tid) => {
      if (!seen.has(tid)) close(tid);
    });
  }

  document.addEventListener('DOMContentLoaded', () => bind());
  document.body.addEventListener('htmx:afterSwap', () => bind());
})();
