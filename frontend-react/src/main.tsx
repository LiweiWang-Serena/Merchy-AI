import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../../frontend/store.css";   // the same stylesheet as the original shop
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
