import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
import "./wenkai-fonts.css";
import "./sidebar-controls.css";
import "./chat-density.css";

createRoot(document.getElementById("root")!).render(<App />);
