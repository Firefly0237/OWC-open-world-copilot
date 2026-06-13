import { createRouter, createWebHistory } from "vue-router";
import ArchivePage from "./pages/ArchivePage.vue";
import AskPage from "./pages/AskPage.vue";
import AuditPage from "./pages/AuditPage.vue";
import CharactersPage from "./pages/CharactersPage.vue";
import CreationPage from "./pages/CreationPage.vue";
import ExportPage from "./pages/ExportPage.vue";
import ExtractionPage from "./pages/ExtractionPage.vue";
import GenesisPage from "./pages/GenesisPage.vue";
import ImpactPage from "./pages/ImpactPage.vue";
import OverviewPage from "./pages/OverviewPage.vue";
import ReferencesPage from "./pages/ReferencesPage.vue";
import ReviewPage from "./pages/ReviewPage.vue";
import SettingsPage from "./pages/SettingsPage.vue";
import SweepPage from "./pages/SweepPage.vue";
import TableImportPage from "./pages/TableImportPage.vue";
import WorkspacesPage from "./pages/WorkspacesPage.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    { path: "/overview", component: OverviewPage },
    { path: "/archive", component: ArchivePage },
    { path: "/genesis", component: GenesisPage },
    { path: "/characters", component: CharactersPage },
    { path: "/creation", component: CreationPage },
    { path: "/extraction", component: ExtractionPage },
    { path: "/import", component: TableImportPage },
    { path: "/references", component: ReferencesPage },
    { path: "/audit", component: AuditPage },
    { path: "/impact", component: ImpactPage },
    { path: "/sweep", component: SweepPage },
    { path: "/ask", component: AskPage },
    { path: "/review", component: ReviewPage },
    { path: "/export", component: ExportPage },
    // /worlds, not /workspaces: hard refreshes must not collide with GET /workspaces
    { path: "/worlds", component: WorkspacesPage },
    { path: "/settings", component: SettingsPage },
  ],
});
