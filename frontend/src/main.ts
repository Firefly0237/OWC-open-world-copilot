import { createApp } from "vue";
import App from "./App.vue";
import { router } from "./router";
import "./styles/tokens.css";
import "./styles/tokens-v2.css";

createApp(App).use(router).mount("#app");
