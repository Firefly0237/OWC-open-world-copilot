import { createRouter, createWebHistory } from "vue-router";
import ArchivePage from "./pages/ArchivePage.vue";
import AskPage from "./pages/AskPage.vue";
import CharactersPage from "./pages/CharactersPage.vue";
import ExportPage from "./pages/ExportPage.vue";
import GenesisPage from "./pages/GenesisPage.vue";
import OverviewPage from "./pages/OverviewPage.vue";
import ReviewPage from "./pages/ReviewPage.vue";
import SettingsPage from "./pages/SettingsPage.vue";
import SweepPage from "./pages/SweepPage.vue";
import WorkspacesPage from "./pages/WorkspacesPage.vue";

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
    { path: "/sweep", component: SweepPage },
    // /worlds, not /workspaces: hard refreshes must not collide with GET /workspaces
    { path: "/worlds", component: WorkspacesPage },
    { path: "/export", component: ExportPage },
    { path: "/settings", component: SettingsPage },
  ],
});
