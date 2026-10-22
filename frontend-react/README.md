# Shop home page in React + TypeScript

The same page as `frontend/store.html`, rewritten in React + TypeScript
so the two can be compared side by side. It uses the same Flask API, the
same stylesheet (`frontend/store.css`) and the same cart.

## Running it

Needs Node.js 18 or newer (`node -v`; install with `brew install node`).

```bash
cd frontend-react
npm install          # once: downloads React, Vite and TypeScript into node_modules/
```

Development (the Flask server must be running too, on port 5050):

```bash
npm run dev          # open http://localhost:5173/react/
```

Edit a file in `src/` and the page updates as soon as you save.

Build, so Flask serves it at http://127.0.0.1:5050/react/:

```bash
npm run build        # type-checks with tsc, then Vite compiles into dist/
```

Just the type check: `npm run typecheck`.

## How it maps to the original

| `frontend/store.html` (plain JavaScript) | here (React + TypeScript) |
|---|---|
| `card(p)` builds an HTML string | `components/ProductCard.tsx` |
| `stockLabel(qty)` | `components/StockLabel.tsx` |
| `productImage()` in `cart.js` | `components/ProductImage.tsx` |
| `loadProducts()` + `innerHTML = ...` | state + `useEffect` in `App.tsx` |
| `setTimeout` for the search box | `hooks/useDebouncedValue.ts` |
| data shapes live only in your head | `types.ts`, checked by TypeScript |

## Worth noticing

- Types: `types.ts` describes what the API returns. Misspell a field or
  pass text where a number belongs, and `npm run typecheck` says so
  before anything runs.
- Escaping: React escapes text by itself; the original has to call
  `escapeHtml()` on every title.
- The compile step: browsers can't run TypeScript or JSX. `tsc` checks
  types, then Vite (using esbuild) strips them and bundles everything,
  React included, into `dist/`. The built page is about 195 KB of
  JavaScript, mostly React itself; the original page needs no build and
  is a few KB.
