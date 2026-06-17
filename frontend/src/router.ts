import { createRouter, createWebHistory } from "vue-router";
import ArchivePage from "./pages/ArchivePage.vue";
import AnalyticsPage from "./pages/AnalyticsPage.vue";
import AskPage from "./pages/AskPage.vue";
import AuditPage from "./pages/AuditPage.vue";
import CharactersPage from "./pages/CharactersPage.vue";
import CompliancePage from "./pages/CompliancePage.vue";
import CreationPage from "./pages/CreationPage.vue";
import DialoguesPage from "./pages/DialoguesPage.vue";
import ExpandPage from "./pages/ExpandPage.vue";
import ExportPage from "./pages/ExportPage.vue";
import ExtractionPage from "./pages/ExtractionPage.vue";
import GenesisPage from "./pages/GenesisPage.vue";
import GraphPage from "./pages/GraphPage.vue";
import HistoryPage from "./pages/HistoryPage.vue";
import ImpactPage from "./pages/ImpactPage.vue";
import LocalizationPage from "./pages/LocalizationPage.vue";
import OverviewPage from "./pages/OverviewPage.vue";
import ReferencesPage from "./pages/ReferencesPage.vue";
import ReviewPage from "./pages/ReviewPage.vue";
import SettingsPage from "./pages/SettingsPage.vue";
import SweepPage from "./pages/SweepPage.vue";
import TableImportPage from "./pages/TableImportPage.vue";
import TemplatesPage from "./pages/TemplatesPage.vue";
import TimelinePage from "./pages/TimelinePage.vue";
import WorkspacesPage from "./pages/WorkspacesPage.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    { path: "/overview", component: OverviewPage },
    { path: "/archive", component: ArchivePage },
    { path: "/genesis", component: GenesisPage },
    { path: "/expand", component: ExpandPage },
    { path: "/characters", component: CharactersPage },
    { path: "/creation", component: CreationPage },
    { path: "/dialogues", component: DialoguesPage },
    { path: "/extraction", component: ExtractionPage },
    { path: "/import", component: TableImportPage },
    { path: "/templates", component: TemplatesPage },
    { path: "/references", component: ReferencesPage },
    { path: "/audit", component: AuditPage },
    { path: "/impact", component: ImpactPage },
    { path: "/analytics", component: AnalyticsPage },
    { path: "/timeline", component: TimelinePage },
    { path: "/graph", component: GraphPage },
    { path: "/sweep", component: SweepPage },
    { path: "/compliance", component: CompliancePage },
    { path: "/ask", component: AskPage },
    { path: "/review", component: ReviewPage },
    { path: "/localization", component: LocalizationPage },
    { path: "/export", component: ExportPage },
    // /worlds, not /workspaces: hard refreshes must not collide with GET /workspaces
    { path: "/worlds", component: WorkspacesPage },
    { path: "/history", component: HistoryPage },
    { path: "/settings", component: SettingsPage },
  ],
});
