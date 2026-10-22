// Replaces stockLabel() in the original store.html.
interface StockLabelProps {
  stock: number;
}

export function StockLabel({ stock }: StockLabelProps) {
  if (stock <= 0) return <div className="p-stock out">Sold out</div>;
  if (stock <= 3) return <div className="p-stock low">Only {stock} left</div>;
  return <div className="p-stock">{stock} in stock</div>;
}
