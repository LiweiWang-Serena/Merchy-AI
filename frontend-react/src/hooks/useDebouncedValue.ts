import { useEffect, useState } from "react";

/**
 * `value`, but only once it has stopped changing for `delay` milliseconds.
 * The search box uses it so typing "carburetor" asks the server once,
 * not ten times. (The original page does the same with setTimeout.)
 */
export function useDebouncedValue<T>(value: T, delay: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);   // typed again before it fired: start over
  }, [value, delay]);
  return settled;
}
