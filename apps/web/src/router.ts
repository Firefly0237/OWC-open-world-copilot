import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

// Every workbench page is loaded on demand. Graph and timeline views pull in sizeable
// visualisation dependencies, so eagerly importing the whole route table made even the
// workspace/settings screens download the entire application.
const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/overview" },
  { path: "/overview", component: () => import("./pages/OverviewPage.vue") },
  { path: "/archive", component: () => import("./pages/ArchivePage.vue") },
  { path: "/genesis", component: () => import("./pages/GenesisPage.vue") },
  { path: "/expand", component: () => import("./pages/ExpandPage.vue") },
  { path: "/characters", component: () => import("./pages/CharactersPage.vue") },
  { path: "/creation", component: () => import("./pages/CreationPage.vue") },
  { path: "/dialogues", component: () => import("./pages/DialoguesPage.vue") },
  { path: "/extraction", component: () => import("./pages/ExtractionPage.vue") },
  { path: "/intake", component: () => import("./pages/IntakePage.vue") },
  { path: "/canonical", component: () => import("./pages/CanonicalPage.vue") },
  { path: "/import", component: () => import("./pages/TableImportPage.vue") },
  { path: "/templates", component: () => import("./pages/TemplatesPage.vue") },
  { path: "/references", component: () => import("./pages/ReferencesPage.vue") },
  { path: "/audit", component: () => import("./pages/AuditPage.vue") },
  { path: "/agent", component: () => import("./pages/AgentPage.vue") },
  { path: "/multi-agent", component: () => import("./pages/MultiAgentPage.vue") },
  { path: "/engineering", component: () => import("./pages/EngineeringPage.vue") },
  { path: "/impact", component: () => import("./pages/ImpactPage.vue") },
  { path: "/analytics", component: () => import("./pages/AnalyticsPage.vue") },
  { path: "/timeline", component: () => import("./pages/TimelinePage.vue") },
  { path: "/graph", component: () => import("./pages/GraphPage.vue") },
  { path: "/sweep", component: () => import("./pages/SweepPage.vue") },
  { path: "/compliance", component: () => import("./pages/CompliancePage.vue") },
  { path: "/ask", component: () => import("./pages/AskPage.vue") },
  { path: "/review", component: () => import("./pages/ReviewPage.vue") },
  { path: "/localization", component: () => import("./pages/LocalizationPage.vue") },
  { path: "/export", component: () => import("./pages/ExportPage.vue") },
  // /worlds, not /workspaces: hard refreshes must not collide with GET /workspaces.
  { path: "/worlds", component: () => import("./pages/WorkspacesPage.vue") },
  { path: "/history", component: () => import("./pages/HistoryPage.vue") },
  { path: "/platform", component: () => import("./pages/PlatformPage.vue") },
  { path: "/settings", component: () => import("./pages/SettingsPage.vue") },
  { path: "/:pathMatch(.*)*", component: () => import("./pages/NotFoundPage.vue") },
];

// The light-theme component gallery is a development aid, not a second product skin.
// Keeping it out of production prevents an unlinked, visually conflicting route from shipping.
if (import.meta.env.DEV) {
  routes.splice(routes.length - 1, 0, {
    path: "/dev/components",
    component: () => import("./pages/v2/GalleryPage.vue"),
  });
}

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(_to, _from, savedPosition) {
    return savedPosition ?? { top: 0 };
  },
});
