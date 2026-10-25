// Admin navigation bar. The page list lives here only, so adding a page
// means editing one line. Every admin page loads this first in <body>.
// The shop pages don't: shoppers shouldn't see links to the admin area.

(function () {
  const PAGES = [
    { href: "/admin", label: "Overview" },
    { href: "/admin/inventory", label: "Inventory", alsoActiveOn: ["/admin/item"] },
    { href: "/admin/add", label: "Add product" },
    { href: "/admin/listings", label: "Listings", alsoActiveOn: ["/admin/ebay-draft"] },
    { href: "/admin/ebay", label: "eBay" },
    { href: "/admin/support", label: "Support" },
    { href: "/admin/reports", label: "Reports" },
    { href: "/admin/research", label: "Research" },
  ];

  const current = location.pathname.replace(/\/+$/, "") || "/";

  // If the session expires while a page is open, the API answers 401:
  // send the person to sign in, then straight back to this page.
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    const url = typeof args[0] === "string" ? args[0] : args[0].url;
    if (response.status === 401 && url.startsWith("/api/")) {
      location.href = `/admin/login?next=${encodeURIComponent(location.pathname + location.search)}`;
    }
    return response;
  };

  const style = document.createElement("style");
  style.textContent = `
    .cl-nav {
      position: sticky; top: 0; z-index: 50;
      display: flex; align-items: center; gap: 28px; height: 56px;
      padding: 0 max(40px, calc((100% - 1100px) / 2));
      background: #ffffff; border-bottom: 1px solid #d9d9d6;
      font-family: 'Inter', system-ui, sans-serif;
    }
    .cl-nav a { text-decoration: none; }
    .cl-nav a:focus-visible { outline: 2px solid #000; outline-offset: 2px; }
    .cl-brand { font: 600 18px 'Source Serif 4', Georgia, serif; color: #000; white-space: nowrap; }
    .cl-links { display: flex; gap: 22px; flex: 1; height: 100%; overflow-x: auto; scrollbar-width: none; }
    .cl-links::-webkit-scrollbar { display: none; }
    .cl-links a {
      display: flex; align-items: center; height: 100%; padding-top: 2px;
      color: #5c5c5c; font-size: 14px; font-weight: 500; white-space: nowrap;
      border-bottom: 2px solid transparent;
    }
    .cl-links a:hover { color: #000; }
    .cl-links a.active { color: #000; border-bottom-color: #000; }
    .cl-end { display: flex; gap: 18px; white-space: nowrap; font-size: 13.5px; }
    .cl-end a { color: #5c5c5c; }
    .cl-end a:hover { color: #000; text-decoration: underline; }
    @media (max-width: 760px) {
      .cl-nav { gap: 16px; padding: 0 16px; }
      .cl-brand, .cl-end .cl-store { display: none; }
    }
    @media print { .cl-nav { display: none; } }
  `;
  document.head.appendChild(style);

  const links = PAGES.map(page => {
    const active = page.href === current || (page.alsoActiveOn || []).includes(current);
    return `<a href="${page.href}"${active ? ' class="active" aria-current="page"' : ""}>${page.label}</a>`;
  }).join("");

  const nav = document.createElement("nav");
  nav.className = "cl-nav";
  nav.setAttribute("aria-label", "Admin");
  nav.innerHTML = `
    <a class="cl-brand" href="/admin">Merchy-AI</a>
    <div class="cl-links">${links}</div>
    <div class="cl-end">
      <a class="cl-store" href="/" target="_blank" rel="noopener">View store</a>
      <a href="/admin/logout">Sign out</a>
    </div>`;
  document.body.prepend(nav);
})();
