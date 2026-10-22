import type { ChangeEvent } from "react";

// A "controlled" input: React state holds the text, and the box shows it.
interface SearchBoxProps {
  value: string;
  onChange: (value: string) => void;
}

export function SearchBox({ value, onChange }: SearchBoxProps) {
  return (
    <input
      type="search"
      placeholder="Search by part, model or SKU"
      aria-label="Search the shop"
      autoComplete="off"
      value={value}
      onChange={(event: ChangeEvent<HTMLInputElement>) => onChange(event.target.value)}
    />
  );
}
