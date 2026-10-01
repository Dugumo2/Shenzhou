import { createRouter, createWebHashHistory } from 'vue-router'
import { auth, hydrateSession } from './auth'
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/services' },
    { path: '/login', name: 'login', component: () => import('./pages/LoginPage.vue'), meta: { public: true } },
    { path: '/services', component: () => import('./pages/ServicesPage.vue') },
    { path: '/services/:id', component: () => import('./pages/ServicePage.vue') },
    { path: '/guides', component: () => import('./pages/GuidesPage.vue') },
    { path: '/account', component: () => import('./pages/AccountPage.vue') },
    { path: '/admin/services', component: () => import('./pages/AdminServicesPage.vue'), meta: { admin: true } },
    { path: '/admin/rules', component: () => import('./pages/RulesPage.vue'), meta: { admin: true } },
    { path: '/:pathMatch(.*)*', redirect: '/services' },
  ],
})
router.beforeEach(async to => {
  try { await hydrateSession() } catch {
    if (!to.meta.public) return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (!to.meta.public && !auth.session?.authenticated) return { path: '/login', query: { redirect: to.fullPath } }
  if (to.meta.admin && !auth.session?.user?.is_staff) return '/services'
  if (to.name === 'login' && auth.session?.authenticated) return '/services'
})
