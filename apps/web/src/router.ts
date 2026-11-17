import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/creator" },
  { path: "/creator", component: () => import("./pages/StoryCreatorPage.vue") },
  { path: "/evolution", component: () => import("./pages/EvolutionStudioPage.vue") },
  { path: "/worlds", component: () => import("./pages/WorkspacesPage.vue") },
  { path: "/settings", component: () => import("./pages/SettingsPage.vue") },
  { path: "/:pathMatch(.*)*", component: () => import("./pages/NotFoundPage.vue") },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(_to, _from, savedPosition) {
    return savedPosition ?? { top: 0 };
  },
});
