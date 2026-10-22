import { useState } from "react";

// Replaces productImage() in the original cart.js: a real photo, or
// "No photo yet" for demo placeholders and pictures that fail to load.

function isRealImage(url: string | null): url is string {
  if (!url) return false;
  try {
    const host = new URL(url).hostname;
    return !(host === "example.com" || host.endsWith(".example.com"));
  } catch {
    return false;
  }
}

interface ProductImageProps {
  url: string | null;
  alt: string;
}

export function ProductImage({ url, alt }: ProductImageProps) {
  const [failed, setFailed] = useState(false);
  if (!isRealImage(url) || failed) return <span>No photo yet</span>;
  return <img src={url} alt={alt} loading="lazy" onError={() => setFailed(true)} />;
}
