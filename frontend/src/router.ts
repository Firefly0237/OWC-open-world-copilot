import { createRouter, createWebHistory } from "vue-router";
import ArchivePage from "./pages/ArchivePage.vue";
import AskPage from "./pages/AskPage.vue";
import CharactersPage from "./pages/CharactersPage.vue";
import GenesisPage from "./pages/GenesisPage.vue";
import OverviewPage from "./pages/OverviewPage.vue";
import ReviewPage from "./pages/ReviewPage.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    { path: "/overview", component: OverviewPage },
    { path: "/archive", component: ArchivePage },
    { path: "/genesis", component: GenesisPage },
    { path: "/characters", component: CharactersPage },
    { path: "/ask", component: AskPage },
    { path: "/review", component: ReviewPage },
  ],
});
