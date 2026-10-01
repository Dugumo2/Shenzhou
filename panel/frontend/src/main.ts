import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import App from './App.vue'
import { router } from './router'
import { clearSession } from './auth'
import { setUnauthorizedHandler } from './api'
import './style.css'
setUnauthorizedHandler(() => {
  const redirect = router.currentRoute.value.fullPath
  clearSession()
  if (router.currentRoute.value.name !== 'login') void router.replace({ path: '/login', query: { redirect, expired: '1' } })
})
createApp(App).use(router).use(ElementPlus, { locale: zhCn }).mount('#app')
