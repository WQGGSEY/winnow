/* Vanilla-SVG claim-graph renderer.
 *
 * Reads tree state + per-node dialogs that the server inlined into
 * data-tree and data-dialogs on the <svg id="claim-graph"> element.
 * Computes a hierarchical layout (depth-by-row, evenly spaced within row),
 * draws nodes (circles + labels) and edges (parent-child paths), wires
 * hover / click handlers, and supports pan + zoom.
 *
 * No external dependencies; runs on every browser that ships SVG + ES2017.
 */
(function () {
  const SVG_NS = "http://www.w3.org/2000/svg";

  const svg = document.getElementById("claim-graph");
  const viewport = document.getElementById("graph-viewport");
  const edgesLayer = document.getElementById("graph-edges");
  const nodesLayer = document.getElementById("graph-nodes");
  const inspectorTitle = document.getElementById("inspector-title");
  const inspectorSubtitle = document.getElementById("inspector-subtitle");
  const inspectorBody = document.getElementById("inspector-body");
  const tooltip = document.getElementById("edge-tooltip");
  const resetBtn = document.getElementById("graph-reset");

  if (!svg || !viewport) return;

  const treeRaw = svg.getAttribute("data-tree") || "{}";
  const dialogsRaw = svg.getAttribute("data-dialogs") || "{}";
  let tree = {};
  let dialogs = {};
  try { tree = JSON.parse(treeRaw); } catch (_) { tree = {}; }
  try { dialogs = JSON.parse(dialogsRaw); } catch (_) { dialogs = {}; }
  function withHypotheses(state, hypotheses) {
    return { ...state, nodes: [...(state.nodes || []), ...(hypotheses || [])] };
  }
  try { tree = withHypotheses(tree, JSON.parse(svg.dataset.hypotheses || "[]")); } catch (_) {}

  let nodes = tree.nodes || [];
  let preparation = {};
  try { preparation = JSON.parse(svg.dataset.preparation || "{}"); } catch (_) {}
  function showPreparation(data) {
    inspectorTitle.textContent = "No research claims yet";
    inspectorSubtitle.textContent = data.status === "preflight_required"
      ? "Baseline qualification is required before research claims can be generated."
      : "The research claim tree has not been created yet.";
    inspectorBody.replaceChildren();
    const explanation = document.createElement("p");
    explanation.textContent = data.required_work || "Progress will appear here when research preparation or claim generation begins.";
    inspectorBody.append(explanation);
    const runs = data.runs || [];
    if (!runs.length) return;
    const heading = document.createElement("h3");
    heading.textContent = `Baseline preparation · ${runs.length} runs`;
    const note = document.createElement("p");
    note.textContent = "Execution results are preparation evidence. Completion does not imply scientific approval or a supported research claim.";
    const list = document.createElement("ul");
    for (const run of runs) {
      const item = document.createElement("li");
      item.textContent = `${run.node_id}: ${run.status}`;
      list.append(item);
    }
    inspectorBody.append(heading, note, list);
  }
  if (!nodes.length) showPreparation(preparation);

  // --- 1. Hierarchical layout -------------------------------------------
  // These data structures are declared once and reused on every poll-driven
  // redraw (relayoutAndDraw clears + repopulates them).
  const byId = new Map();
  const children = new Map();
  const depthOf = new Map();
  const byDepth = new Map();
  const pos = new Map();
  const followUpRelations = new Map();
  let pinnedNodeId = null;

  if (nodes.length) initialDraw();

  function initialDraw() {
  for (const n of nodes) byId.set(n.id, n);
  const roots = [];
  for (const n of nodes) {
    if (!n.parent) {
      roots.push(n.id);
      continue;
    }
    if (!children.has(n.parent)) children.set(n.parent, []);
    children.get(n.parent).push(n.id);
  }
  if (!roots.length) roots.push(nodes[0].id);  // fallback

  // Depth assignment (BFS); detached children fall back to root depth + 1.
  const order = []; // BFS traversal order — used by the column packer.
  const queue = roots.slice();
  for (const rid of roots) depthOf.set(rid, 0);
  while (queue.length) {
    const nid = queue.shift();
    order.push(nid);
    const kids = children.get(nid) || [];
    for (const cid of kids) {
      if (depthOf.has(cid)) continue;
      depthOf.set(cid, depthOf.get(nid) + 1);
      queue.push(cid);
    }
  }
  // Any orphan nodes the BFS missed.
  for (const n of nodes) {
    if (!depthOf.has(n.id)) {
      depthOf.set(n.id, 0);
      order.push(n.id);
    }
  }

  // Group by depth.
  let maxDepth = 0;
  for (const nid of order) {
    const d = depthOf.get(nid);
    maxDepth = Math.max(maxDepth, d);
    if (!byDepth.has(d)) byDepth.set(d, []);
    byDepth.get(d).push(nid);
  }

  // Sort each depth's nodes so siblings are adjacent — group by parent id.
  for (const list of byDepth.values()) {
    list.sort((a, b) => {
      const pa = byId.get(a).parent || "";
      const pb = byId.get(b).parent || "";
      if (pa < pb) return -1;
      if (pa > pb) return 1;
      return a < b ? -1 : 1;
    });
  }

  // Compute positions. Generous spacing so labels don't collide.
  const verticalSpacing = 130;
  const horizontalSpacing = 180;
  const radius = 24;
  let widestRow = 0;
  for (const list of byDepth.values()) widestRow = Math.max(widestRow, list.length);
  const canvasWidth = Math.max(widestRow, 1) * horizontalSpacing;
  for (const [d, list] of byDepth.entries()) {
    const rowWidth = list.length * horizontalSpacing;
    const offset = (canvasWidth - rowWidth) / 2 + horizontalSpacing / 2;
    list.forEach((nid, i) => {
      pos.set(nid, {
        x: offset + i * horizontalSpacing,
        y: 60 + d * verticalSpacing,
      });
    });
  }
  const canvasHeight = 120 + maxDepth * verticalSpacing + 40;

  // Pad the layout so panning to the corners still shows context.
  const padX = 80;
  const padY = 60;
  svg.setAttribute(
    "viewBox",
    `${-padX} ${-padY} ${canvasWidth + padX * 2} ${canvasHeight + padY * 2}`
  );

  // --- 2. Draw edges -----------------------------------------------------
  // Distinguish parent-child (normal hierarchy) from follow_up_brief
  // relationships (semantic edges the Professor announced in dialog). The
  // follow_up edges land on the same parent-child link in the data model;
  // we tag the dialog metadata so the tooltip can describe it.
  // (Map declared at outer scope so the polling redraw can rebuild it.)
  for (const [parentId, entries] of Object.entries(dialogs)) {
    for (const e of entries) {
      if (e.intent === "follow_up_brief" && e.metadata && e.metadata.type) {
        // Look for a child of this parent matching the metadata.type.
        const kids = children.get(parentId) || [];
        for (const cid of kids) {
          if (byId.get(cid).type === e.metadata.type) {
            followUpRelations.set(cid, {
              type: e.metadata.type,
              successor_claim: e.metadata.successor_claim || "",
            });
          }
        }
      }
    }
  }

  for (const n of nodes) {
    if (!n.parent) continue;
    const a = pos.get(n.parent);
    const b = pos.get(n.id);
    if (!a || !b) continue;
    const path = document.createElementNS(SVG_NS, "path");
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    // Cubic bezier with vertical tangents for clean hierarchical curves.
    const c1y = a.y + dy * 0.5;
    const c2y = b.y - dy * 0.5;
    path.setAttribute("d", `M ${a.x} ${a.y + radius} C ${a.x} ${c1y} ${b.x} ${c2y} ${b.x} ${b.y - radius}`);
    const isFollowUp = followUpRelations.has(n.id);
    path.setAttribute(
      "class",
      "graph-edge " + (isFollowUp ? "kind-followup" : "kind-parent")
    );
    path.setAttribute(
      "marker-end",
      isFollowUp ? "url(#arrowhead-prof)" : "url(#arrowhead)"
    );
    path.dataset.from = n.parent;
    path.dataset.to = n.id;
    if (isFollowUp) {
      const meta = followUpRelations.get(n.id);
      path.dataset.intent = "follow_up";
      path.dataset.successorClaim = meta.successor_claim;
    } else {
      path.dataset.intent = "parent_child";
    }
    edgesLayer.appendChild(path);
  }

  // --- 3. Draw nodes -----------------------------------------------------
  for (const n of nodes) {
    const p = pos.get(n.id);
    if (!p) continue;
    const g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("class", "graph-node");
    g.setAttribute("transform", `translate(${p.x}, ${p.y})`);
    g.dataset.id = n.id;
    g.dataset.type = n.type || "unknown";
    g.dataset.status = n.status || "unknown";

    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("class", "node-circle");
    circle.setAttribute("r", radius);
    g.appendChild(circle);

    const typeLabel = document.createElementNS(SVG_NS, "text");
    typeLabel.setAttribute("class", "node-type-label");
    typeLabel.setAttribute("y", "2");
    typeLabel.textContent = (n.type || "?").slice(0, 8);
    g.appendChild(typeLabel);

    const idLabel = document.createElementNS(SVG_NS, "text");
    idLabel.setAttribute("class", "node-id-label");
    idLabel.setAttribute("y", radius + 12);
    idLabel.textContent = truncateMid(n.id, 24);
    g.appendChild(idLabel);

    nodesLayer.appendChild(g);
  }
  } // end initialDraw

  // --- 4. Hover + click handlers ----------------------------------------
  // pinnedNodeId is declared at outer scope above so the poll redraw can
  // preserve the operator's pinned node across rebuilds.

  function showNodeDetail(nid) {
    const n = byId.get(nid);
    if (!n) return;
    const entries = dialogs[nid] || [];
    inspectorTitle.textContent = nid;
    inspectorSubtitle.innerHTML =
      `<span class="chip chip-${n.type}">${escape(n.type)}</span> · ` +
      `status: <strong>${escape(n.status)}</strong>`;
    const lines = [];
    lines.push('<div class="inspector-node-detail">');
    lines.push('<dl class="node-meta">');
    if (n.parent) {
      lines.push(`<dt>parent</dt><dd><code>${escape(n.parent)}</code></dd>`);
    }
    if (n.verdict) {
      lines.push(`<dt>verdict</dt><dd>${escape(n.verdict)}</dd>`);
    }
    lines.push("</dl>");
    if (n.claim_under_test) {
      lines.push('<div class="claim-text">' + escape(n.claim_under_test) + "</div>");
    }
    if (n.hypothesis_detail) {
      const detail = n.hypothesis_detail;
      lines.push("<p><strong>Unverified hypothesis.</strong> A test proposal is not evidence that the claim is true.</p>");
      if (detail.selected_for_diagnostic) lines.push("<p>Selected for the next small diagnostic.</p>");
      lines.push("<h3>Mechanism, competing explanation and test</h3><pre>" + escape(JSON.stringify(detail.candidate, null, 2)) + "</pre>");
      if (detail.critique) lines.push("<h3>Independent critique</h3><pre>" + escape(JSON.stringify(detail.critique, null, 2)) + "</pre>");
    }
    if (entries.length) {
      lines.push("<h3>교수님 ↔ 대학원생 dialog</h3>");
      for (const e of entries) {
        const speaker = e.speaker === "professor" ? "교수님" : "대학원생";
        const mockTag =
          e.metadata && e.metadata.is_mock
            ? '<span class="mock-tag">mock</span>'
            : "";
        lines.push(
          `<div class="dialog-entry">` +
            `<span class="speaker-${escape(e.speaker)}">${speaker}</span>` +
            `<span class="intent">${escape(e.intent || "")}</span>` +
            mockTag +
            `<div class="text">${escape(stripToolLeak(e.text || ""))}</div>` +
          `</div>`
        );
      }
    } else {
      lines.push('<p class="muted">(no dialog recorded for this node)</p>');
    }
    lines.push("</div>");
    inspectorBody.innerHTML = lines.join("");
  }

  function clearSelection() {
    pinnedNodeId = null;
    nodesLayer.querySelectorAll(".graph-node.selected").forEach((el) =>
      el.classList.remove("selected")
    );
    inspectorTitle.textContent = "Inspector";
    inspectorSubtitle.textContent = "Hover a node or edge.";
    // Restore the default body (coverage + readiness + handoff).
    // We saved it once via clone; here we just leave whatever the user last
    // pinned. Simpler: keep the last hovered detail.
  }

  nodesLayer.addEventListener("mouseover", (ev) => {
    const g = ev.target.closest(".graph-node");
    if (!g || pinnedNodeId) return;
    showNodeDetail(g.dataset.id);
  });
  nodesLayer.addEventListener("click", (ev) => {
    const g = ev.target.closest(".graph-node");
    if (!g) return;
    const nid = g.dataset.id;
    if (pinnedNodeId === nid) {
      g.classList.remove("selected");
      pinnedNodeId = null;
      return;
    }
    nodesLayer.querySelectorAll(".graph-node.selected").forEach((el) =>
      el.classList.remove("selected")
    );
    g.classList.add("selected");
    pinnedNodeId = nid;
    showNodeDetail(nid);
  });

  edgesLayer.addEventListener("mouseover", (ev) => {
    const path = ev.target.closest(".graph-edge");
    if (!path) return;
    path.classList.add("hovered");
    const from = path.dataset.from;
    const to = path.dataset.to;
    const intent = path.dataset.intent;
    let text;
    if (intent === "follow_up") {
      text =
        `Professor opened ${to} as a follow-up successor of ${from}. ` +
        (path.dataset.successorClaim
          ? `Successor claim: ${path.dataset.successorClaim}`
          : "");
    } else {
      text = `Parent ${from} → child ${to}`;
    }
    tooltip.textContent = text;
    tooltip.hidden = false;
  });
  edgesLayer.addEventListener("mousemove", (ev) => {
    tooltip.style.left = ev.clientX + 12 + "px";
    tooltip.style.top = ev.clientY + 14 + "px";
  });
  edgesLayer.addEventListener("mouseout", (ev) => {
    const path = ev.target.closest(".graph-edge");
    if (path) path.classList.remove("hovered");
    tooltip.hidden = true;
  });

  // --- 5. Pan + zoom -----------------------------------------------------
  let panX = 0, panY = 0, scale = 1;
  function applyTransform() {
    viewport.setAttribute("transform", `translate(${panX} ${panY}) scale(${scale})`);
  }
  let dragging = false;
  let dragStart = null;
  svg.addEventListener("mousedown", (ev) => {
    if (ev.target.closest(".graph-node") || ev.target.closest(".graph-edge")) return;
    dragging = true;
    svg.classList.add("panning");
    dragStart = { x: ev.clientX, y: ev.clientY, panX, panY };
  });
  window.addEventListener("mousemove", (ev) => {
    if (!dragging || !dragStart) return;
    panX = dragStart.panX + (ev.clientX - dragStart.x);
    panY = dragStart.panY + (ev.clientY - dragStart.y);
    applyTransform();
  });
  window.addEventListener("mouseup", () => {
    dragging = false;
    svg.classList.remove("panning");
  });
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.1 : 0.9;
    const newScale = Math.min(4, Math.max(0.25, scale * factor));
    // Zoom toward cursor location.
    const rect = svg.getBoundingClientRect();
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;
    panX = cx - (cx - panX) * (newScale / scale);
    panY = cy - (cy - panY) * (newScale / scale);
    scale = newScale;
    applyTransform();
  }, { passive: false });

  resetBtn.addEventListener("click", () => {
    panX = 0;
    panY = 0;
    scale = 1;
    applyTransform();
  });

  // --- Live polling -----------------------------------------------------
  // Re-fetch graph_data every poll-interval-ms. When the node count or the
  // server-reported last_activity_mtime changes, redraw the SVG. pan/zoom
  // state (panX, panY, scale) and the currently-pinned node id survive
  // the redraw — operator's view isn't yanked around mid-inspection.
  const liveUrl = svg.dataset.liveUrl;
  const pollIntervalMs = parseInt(svg.dataset.pollIntervalMs || "0", 10);
  let lastSignature = `${nodes.length}|0`;
  function snapshotSignature(state, lastMtime) {
    return `${(state.nodes || []).length}|${lastMtime || 0}`;
  }
  function redraw(state, dialogsObj) {
    // Wipe layers without recreating the <svg>.
    edgesLayer.innerHTML = "";
    nodesLayer.innerHTML = "";
    // The closure above closed over `nodes` / `tree`; bind the new ones to
    // local names and re-run the same layout + draw logic by recursing
    // into the IIFE body. Simplest: reload the page-level state and call
    // the existing draw functions in place. We achieve this by reassigning
    // tree/dialogs/nodes and re-running the layout block.
    tree = state;
    dialogs = dialogsObj || {};
    const newNodes = state.nodes || [];
    if (!newNodes.length) {
      nodes = [];
      showPreparation(preparation);
      return;
    }
    // Recompute layout + edges + nodes by calling the same helpers. We
    // already have access to them via closure — just need to re-execute
    // the relevant sections. The cleanest way: dispatch a small helper.
    relayoutAndDraw(newNodes);
  }
  function relayoutAndDraw(newNodes) {
    // Update byId / children / depth maps from scratch.
    byId.clear();
    children.clear();
    for (const n of newNodes) byId.set(n.id, n);
    const newRoots = [];
    for (const n of newNodes) {
      if (!n.parent) newRoots.push(n.id);
      else {
        if (!children.has(n.parent)) children.set(n.parent, []);
        children.get(n.parent).push(n.id);
      }
    }
    if (!newRoots.length && newNodes.length) newRoots.push(newNodes[0].id);
    depthOf.clear();
    const newOrder = [];
    const newQueue = newRoots.slice();
    for (const rid of newRoots) depthOf.set(rid, 0);
    while (newQueue.length) {
      const nid = newQueue.shift();
      newOrder.push(nid);
      for (const cid of children.get(nid) || []) {
        if (depthOf.has(cid)) continue;
        depthOf.set(cid, depthOf.get(nid) + 1);
        newQueue.push(cid);
      }
    }
    for (const n of newNodes) {
      if (!depthOf.has(n.id)) {
        depthOf.set(n.id, 0);
        newOrder.push(n.id);
      }
    }
    byDepth.clear();
    let newMaxDepth = 0;
    for (const nid of newOrder) {
      const d = depthOf.get(nid);
      newMaxDepth = Math.max(newMaxDepth, d);
      if (!byDepth.has(d)) byDepth.set(d, []);
      byDepth.get(d).push(nid);
    }
    for (const list of byDepth.values()) {
      list.sort((a, b) => {
        const pa = byId.get(a).parent || "";
        const pb = byId.get(b).parent || "";
        if (pa < pb) return -1;
        if (pa > pb) return 1;
        return a < b ? -1 : 1;
      });
    }
    pos.clear();
    let newWidest = 0;
    for (const list of byDepth.values()) newWidest = Math.max(newWidest, list.length);
    const newCanvasWidth = Math.max(newWidest, 1) * 180;
    for (const [d, list] of byDepth.entries()) {
      const rowWidth = list.length * 180;
      const offset = (newCanvasWidth - rowWidth) / 2 + 90;
      list.forEach((nid, i) => {
        pos.set(nid, { x: offset + i * 180, y: 60 + d * 130 });
      });
    }
    const newCanvasHeight = 120 + newMaxDepth * 130 + 40;
    svg.setAttribute(
      "viewBox",
      `-80 -60 ${newCanvasWidth + 160} ${newCanvasHeight + 120}`
    );
    // Rebuild follow-up edge map from the fresh dialogs.
    followUpRelations.clear();
    for (const [parentId, entries] of Object.entries(dialogs)) {
      for (const e of entries) {
        if (e.intent === "follow_up_brief" && e.metadata && e.metadata.type) {
          const kids = children.get(parentId) || [];
          for (const cid of kids) {
            if (byId.get(cid).type === e.metadata.type) {
              followUpRelations.set(cid, {
                type: e.metadata.type,
                successor_claim: e.metadata.successor_claim || "",
              });
            }
          }
        }
      }
    }
    // Edges.
    for (const n of newNodes) {
      if (!n.parent) continue;
      const a = pos.get(n.parent);
      const b = pos.get(n.id);
      if (!a || !b) continue;
      const path = document.createElementNS(SVG_NS, "path");
      const dy = b.y - a.y;
      const c1y = a.y + dy * 0.5;
      const c2y = b.y - dy * 0.5;
      path.setAttribute(
        "d",
        `M ${a.x} ${a.y + 24} C ${a.x} ${c1y} ${b.x} ${c2y} ${b.x} ${b.y - 24}`
      );
      const isFollowUp = followUpRelations.has(n.id);
      path.setAttribute(
        "class",
        "graph-edge " + (isFollowUp ? "kind-followup" : "kind-parent")
      );
      path.setAttribute(
        "marker-end",
        isFollowUp ? "url(#arrowhead-prof)" : "url(#arrowhead)"
      );
      path.dataset.from = n.parent;
      path.dataset.to = n.id;
      if (isFollowUp) {
        const meta = followUpRelations.get(n.id);
        path.dataset.intent = "follow_up";
        path.dataset.successorClaim = meta.successor_claim;
      } else {
        path.dataset.intent = "parent_child";
      }
      edgesLayer.appendChild(path);
    }
    // Nodes.
    for (const n of newNodes) {
      const p = pos.get(n.id);
      if (!p) continue;
      const g = document.createElementNS(SVG_NS, "g");
      g.setAttribute("class", "graph-node");
      g.setAttribute("transform", `translate(${p.x}, ${p.y})`);
      g.dataset.id = n.id;
      g.dataset.type = n.type || "unknown";
      g.dataset.status = n.status || "unknown";
      const circle = document.createElementNS(SVG_NS, "circle");
      circle.setAttribute("class", "node-circle");
      circle.setAttribute("r", 24);
      g.appendChild(circle);
      const typeLabel = document.createElementNS(SVG_NS, "text");
      typeLabel.setAttribute("class", "node-type-label");
      typeLabel.setAttribute("y", "2");
      typeLabel.textContent = (n.type || "?").slice(0, 8);
      g.appendChild(typeLabel);
      const idLabel = document.createElementNS(SVG_NS, "text");
      idLabel.setAttribute("class", "node-id-label");
      idLabel.setAttribute("y", 36);
      idLabel.textContent = truncateMid(n.id, 24);
      g.appendChild(idLabel);
      if (pinnedNodeId === n.id) g.classList.add("selected");
      nodesLayer.appendChild(g);
    }
    // Preserve pan/zoom across redraws.
    applyTransform();
    // If a pinned node was redrawn, refresh the detail panel so any new
    // dialog entries land in the inspector.
    if (pinnedNodeId && byId.has(pinnedNodeId)) {
      showNodeDetail(pinnedNodeId);
    }
  }

  // Track which nodes Codex is currently processing. Server
  // returns this on /graph_data as `in_progress_nodes`. We mark the SVG
  // <g> with .in-progress so CSS can pulse the ring, and surface a small
  // "working on" pill in the topbar so the operator doesn't have to scan
  // the tree to find activity.
  const nowWorkingEl = document.getElementById("graph-now-working");
  const nowWorkingBody = nowWorkingEl
    ? nowWorkingEl.querySelector(".now-working-body")
    : null;
  const STATUS_LABELS = {
    running: "running experiment",
    completed_worker_report: "awaiting critic reviews",
    critic_reviewed: "awaiting professor decision",
    orchestrator_reduced: "awaiting professor decision",
  };

  function applyInProgress(inProgressNodes) {
    const want = new Set();
    const meta = new Map();
    (inProgressNodes || []).forEach((n) => {
      if (!n || !n.node_id) return;
      want.add(n.node_id);
      meta.set(n.node_id, n);
    });
    nodesLayer.querySelectorAll(".graph-node").forEach((g) => {
      const nid = g.dataset.id;
      if (want.has(nid)) {
        const m = meta.get(nid);
        g.classList.add("in-progress");
        if (m && m.next_tool) g.dataset.nextTool = m.next_tool;
        if (m && m.status) g.dataset.midStatus = m.status;
      } else {
        g.classList.remove("in-progress");
        delete g.dataset.nextTool;
        delete g.dataset.midStatus;
      }
    });
    if (!nowWorkingEl || !nowWorkingBody) return;
    const list = Array.from(want);
    if (list.length === 0) {
      nowWorkingEl.hidden = true;
      nowWorkingBody.textContent = "";
      return;
    }
    nowWorkingEl.hidden = false;
    const parts = list.slice(0, 3).map((nid) => {
      const m = meta.get(nid) || {};
      const label = STATUS_LABELS[m.status] || m.status || "in flight";
      const short = truncateMid(nid, 28);
      return `<a href="#" data-jump="${nid}" title="${escape(nid)}">${escape(short)}</a> <span class="muted">· ${escape(label)}</span>`;
    });
    const more = list.length > parts.length ? ` <span class="muted">(+${list.length - parts.length} more)</span>` : "";
    nowWorkingBody.innerHTML = parts.join(" &nbsp;|&nbsp; ") + more;
    nowWorkingBody.querySelectorAll("a[data-jump]").forEach((a) => {
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        const nid = a.dataset.jump;
        const g = nodesLayer.querySelector(`.graph-node[data-id="${CSS.escape(nid)}"]`);
        if (!g) return;
        pinnedNodeId = nid;
        nodesLayer.querySelectorAll(".graph-node.selected")
          .forEach((el) => el.classList.remove("selected"));
        g.classList.add("selected");
        showNodeDetail(nid);
      });
    });
  }

  async function poll() {
    if (!liveUrl) return;
    try {
      const resp = await fetch(liveUrl, { cache: "no-store" });
      if (!resp.ok) return;
      const data = await resp.json();
      const work = data.research_work || {};
      const decision = work.decision || {};
      const phase = work.status === 'running'
        ? ({ implementation_review: 'Reviewing implementation', experiment_running: 'Running experiment' }[work.execution_phase] || 'Work reserved')
        : (work.status || 'Not planned');
      document.getElementById('research-work-status').textContent =
        `${phase}${decision.kind ? ' · ' + decision.kind : ''}`;
      document.getElementById('research-work-question').textContent = decision.uncertainty || '';
      document.getElementById('research-work-test').textContent = decision.test || '';
      document.getElementById('research-work-review').textContent = work.implementation_review
        ? `Implementation review: ${work.implementation_review.decision} · ${work.implementation_review.reason}` : '';
      document.getElementById('research-work-outcome').textContent = work.outcome
        ? `${work.outcome.execution_result} · ${work.outcome.new_observation ? 'New observation recorded' : 'No new observation recorded'} · claim unverified${work.outcome.analysis ? ' · ' + (work.outcome.analysis.answer || work.outcome.analysis.reason) : ''}`
        : '';
      const state = withHypotheses(data.tree_state || { nodes: [] }, data.hypothesis_nodes);
      preparation = data.preparation || {};
      const sig = snapshotSignature(state, data.last_activity_mtime);
      if (sig !== lastSignature) {
        lastSignature = sig;
        redraw(state, data.node_dialogs);
      }
      // in_progress_nodes updates even when tree structure is unchanged
      // (e.g. a node moves running → completed_worker_report). Apply it on
      // every poll so the visual lag is bounded by pollIntervalMs.
      applyInProgress(data.in_progress_nodes);
      if (!(state.nodes || []).length) showPreparation(preparation);
    } catch (err) {
      // Network blip — wait for the next tick.
    }
  }
  if (liveUrl && pollIntervalMs > 0) {
    lastSignature = snapshotSignature(tree, 0);
    // First poll fires immediately so the in-progress markers + topbar
    // pill don't sit empty for a whole pollIntervalMs window after load.
    poll();
    setInterval(poll, pollIntervalMs);
  }

  // --- 6. Helpers --------------------------------------------------------

  // Older dialog.json entries can carry trailing agent tool-call
  // envelope leakage — closing </commentary>, </response_to_grad_student>,
  // </invoke>, or a stray <parameter name="...">JSON</parameter> block
  // appended after the real prose. The MCP server now sanitizes new
  // entries, but pre-fix entries on disk still show the leak. We trim it
  // at display time so the inspector stays clean without mutating
  // operator-owned thread state.
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

  function escape(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function truncateMid(s, max) {
    if (s.length <= max) return s;
    const head = Math.ceil((max - 1) / 2);
    const tail = Math.floor((max - 1) / 2);
    return s.slice(0, head) + "…" + s.slice(-tail);
  }
})();
