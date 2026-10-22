// The shop's AI support chat. Add to any shop page with
//   <script src="/static/support-widget.js"></script>
// The conversation is kept for this browser tab, so it follows the
// customer from page to page. Replies are shown as plain text (never as
// HTML); product links in them become real links. When a person on the
// team takes over, the window checks for their replies every few seconds.

(function () {
  const STORE_KEY = "support_chat";
  const saved = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null") ||
                { id: null, messages: [], open: false, lastId: 0, waiting: false };
  let busy = false;
  let pollTimer = null;

  const style = document.createElement("style");
  style.textContent = `
    .sw-launch { position: fixed; right: 20px; bottom: 20px; z-index: 60; background: #000; color: #fff;
      border: 0; border-radius: 22px; padding: 12px 18px; font: 600 14px 'Inter', sans-serif; cursor: pointer;
      box-shadow: 0 4px 14px rgba(0,0,0,.18); }
    .sw-panel { position: fixed; right: 20px; bottom: 20px; z-index: 61; width: 360px; max-width: calc(100vw - 24px);
      height: 520px; max-height: calc(100vh - 40px); display: flex; flex-direction: column; background: #fff;
      border: 1px solid #dddddb; border-radius: 10px; box-shadow: 0 10px 34px rgba(0,0,0,.16);
      font: 14px/1.5 'Inter', sans-serif; color: #000; }
    .sw-panel[hidden], .sw-launch[hidden] { display: none; }
    .sw-head { padding: 14px 16px 10px; border-bottom: 1px solid #ececea; }
    .sw-title { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 15px; }
    .sw-tag { font: 600 11px 'Inter', sans-serif; border: 1px solid #000; border-radius: 3px; padding: 1px 6px; }
    .sw-close { margin-left: auto; background: none; border: 0; font-size: 22px; line-height: 1; cursor: pointer; color: #5c5c5c; }
    .sw-note { color: #5c5c5c; font-size: 12.5px; margin-top: 3px; }
    .sw-log { flex: 1; overflow-y: auto; padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
    .sw-msg { max-width: 85%; padding: 9px 12px; border-radius: 10px; white-space: pre-wrap; overflow-wrap: anywhere; }
    .sw-msg.assistant { background: #f3f3f1; align-self: flex-start; border-bottom-left-radius: 3px; }
    .sw-msg.customer { background: #000; color: #fff; align-self: flex-end; border-bottom-right-radius: 3px; }
    .sw-msg.system { align-self: center; background: none; color: #b3261e; font-size: 12.5px; text-align: center; }
    .sw-msg.notice { align-self: center; background: none; color: #5c5c5c; font-size: 12.5px; text-align: center; max-width: 92%; }
    .sw-msg.staff { background: #fff; border: 1px solid #000; align-self: flex-start; border-bottom-left-radius: 3px; }
    .sw-who { display: block; font-size: 11.5px; font-weight: 600; color: #5c5c5c; margin-bottom: 2px; }
    .sw-launch.has-news { background: #1b7f4b; }
    .sw-msg a { color: inherit; text-decoration: underline; }
    .sw-foot { border-top: 1px solid #ececea; padding: 10px 12px 12px; }
    .sw-row { display: flex; gap: 8px; }
    .sw-row input { flex: 1; min-width: 0; padding: 10px 12px; border: 1px solid #dddddb; border-radius: 6px; font: inherit; }
    .sw-row input:focus { outline: none; border-color: #000; }
    .sw-row button { background: #000; color: #fff; border: 0; border-radius: 6px; padding: 0 14px; font: 600 14px 'Inter', sans-serif; cursor: pointer; }
    .sw-row button:disabled { opacity: .45; cursor: default; }
    .sw-human { background: none; border: 0; padding: 0; margin-top: 8px; color: #5c5c5c; font: 12.5px 'Inter', sans-serif;
      text-decoration: underline; cursor: pointer; }
  `;
  document.head.appendChild(style);

  const launch = document.createElement("button");
  launch.className = "sw-launch";
  launch.type = "button";
  launch.textContent = "Questions? Ask us";
  launch.setAttribute("aria-haspopup", "dialog");

  const panel = document.createElement("section");
  panel.className = "sw-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Chat with the store's AI assistant");
  panel.hidden = true;
  panel.innerHTML = `
    <div class="sw-head">
      <div class="sw-title"><span class="sw-name">Store assistant</span><span class="sw-tag">AI</span>
        <button class="sw-close" type="button" aria-label="Close chat">&times;</button></div>
      <div class="sw-note">You're chatting with an AI assistant. Ask for a person anytime.</div>
    </div>
    <div class="sw-log" aria-live="polite"></div>
    <div class="sw-foot">
      <div class="sw-row">
        <input type="text" maxlength="1000" placeholder="Ask about a part, fitment or an order" aria-label="Your message">
        <button type="button" class="sw-send">Send</button>
      </div>
      <button type="button" class="sw-human">Talk to a person</button>
    </div>`;
  document.body.append(launch, panel);

  const log = panel.querySelector(".sw-log");
  const input = panel.querySelector("input");
  const send = panel.querySelector(".sw-send");

  function persist() { sessionStorage.setItem(STORE_KEY, JSON.stringify(saved)); }

  // Plain text, with /product?sku=... turned into links (built as elements, never as HTML).
  function bubble(role, text, who) {
    const el = document.createElement("div");
    el.className = `sw-msg ${role}`;
    if (who) {
      const label = document.createElement("span");
      label.className = "sw-who";
      label.textContent = who;
      el.appendChild(label);
    }
    for (const part of String(text).split(/(\/product\?sku=[A-Za-z0-9._-]+)/)) {
      if (/^\/product\?sku=/.test(part)) {
        const a = document.createElement("a");
        a.href = part;
        a.textContent = part.replace("/product?sku=", "View ");
        el.appendChild(a);
      } else if (part) {
        el.appendChild(document.createTextNode(part));
      }
    }
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  const staffLabel = name => `${name || "Our team"}, from our team`;

  function show(m) {
    if (m.role === "staff") bubble("staff", m.text, staffLabel(m.staff_name));
    else bubble(m.role, m.text);
  }

  function remember(m) {
    saved.messages.push(m);
    if (m.id) saved.lastId = Math.max(saved.lastId || 0, m.id);
    persist();
  }

  function setOpen(open) {
    saved.open = open;
    persist();
    panel.hidden = !open;
    launch.hidden = open;
    launch.setAttribute("aria-expanded", String(open));
    if (open) {
      launch.classList.remove("has-news");
      launch.textContent = "Questions? Ask us";
      input.focus();
    }
    schedulePoll();
  }

  // While a person is handling the conversation, ask for new messages every
  // 4 seconds (open or not); otherwise every 15 seconds while it's open.
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!saved.id) return;
    const every = saved.waiting ? 4000 : saved.open ? 15000 : 0;
    if (every) pollTimer = setTimeout(poll, every);
  }

  async function poll() {
    try {
      const res = await fetch(`/api/support/chat/${encodeURIComponent(saved.id)}/messages?after=${saved.lastId || 0}`);
      if (res.ok) {
        const data = await res.json();
        let news = false;
        for (const m of data.messages) {
          if (m.role !== "customer") { show(m); news = news || m.role === "staff"; }
          remember({ id: m.id, role: m.role, text: m.text, staff_name: m.staff_name });
        }
        saved.waiting = data.needs_human;
        persist();
        if (news && !saved.open) {
          launch.classList.add("has-news");
          launch.textContent = "New reply from our team";
        }
      }
    } catch { /* try again next time */ }
    schedulePoll();
  }

  async function ask(text) {
    if (busy || !text.trim()) return;
    busy = true;
    send.disabled = true;
    bubble("customer", text);
    input.value = "";
    const typing = bubble("assistant", "…");
    try {
      const res = await fetch("/api/support/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: saved.id, message: text }),
      });
      const data = await res.json();
      typing.remove();
      if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
      saved.id = data.conversation_id;
      remember({ id: data.customer_message_id, role: "customer", text });
      if (data.reply) {
        bubble("assistant", data.reply);
        remember({ id: data.message_id, role: "assistant", text: data.reply });
      }
      // Tell the customer once that a person will reply here.
      if (data.notice && !saved.waiting) {
        bubble("notice", data.notice);
        remember({ role: "notice", text: data.notice });
      }
      saved.waiting = data.needs_human;
      persist();
      schedulePoll();
    } catch (e) {
      typing.remove();
      bubble("system", e.message === "Failed to fetch" ? "Can't reach the store right now. Please try again." : e.message);
    }
    busy = false;
    send.disabled = false;
    input.focus();
  }

  fetch("/api/support/config").then(r => r.json()).then(config => {
    panel.querySelector(".sw-name").textContent = config.assistant_name;
    if (!saved.messages.length) bubble("assistant", config.greeting);
  }).catch(() => {});
  for (const m of saved.messages) show(m);

  launch.onclick = () => setOpen(true);
  panel.querySelector(".sw-close").onclick = () => setOpen(false);
  send.onclick = () => ask(input.value);
  input.onkeydown = e => { if (e.key === "Enter") ask(input.value); };
  panel.querySelector(".sw-human").onclick = () => ask("I'd like to talk to a person, please.");
  if (saved.open) setOpen(true);
  else schedulePoll();
})();
