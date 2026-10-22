interface PagerProps {
  page: number;
  pages: number;
  onPage: (page: number) => void;
}

export function Pager({ page, pages, onPage }: PagerProps) {
  if (pages <= 1) return null;
  return (
    <div className="shop-pager">
      <button type="button" className="pager-btn" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        Previous
      </button>
      <span>Page {page.toLocaleString()} of {pages.toLocaleString()}</span>
      <button type="button" className="pager-btn" disabled={page >= pages} onClick={() => onPage(page + 1)}>
        Next
      </button>
    </div>
  );
}
