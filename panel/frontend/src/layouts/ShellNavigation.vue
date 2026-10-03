<script setup lang="ts">
import type { NavigationGroup } from '../navigation'
import { isNavigationActive } from '../navigation'
import ShellIcon from './ShellIcon.vue'
defineProps<{ groups: readonly NavigationGroup[]; path: string; collapsed?: boolean; horizontal?: boolean }>()
defineEmits<{ navigate: [] }>()
</script>
<template>
  <nav :class="['shell-navigation', { 'shell-navigation-horizontal': horizontal, 'shell-navigation-collapsed': collapsed }]" :aria-label="horizontal ? '主导航' : '工作区导航'">
    <section v-for="group in groups.filter(item => item.enabled)" :key="group.id" class="shell-nav-group" :aria-label="group.label">
      <p v-if="!horizontal" class="shell-group-label" :class="{ 'visually-hidden': collapsed }">{{ group.label }}</p>
      <RouterLink v-for="item in group.items" :key="item.path" :to="item.path" custom v-slot="{ href, navigate }">
        <a :href="href" class="shell-nav-link" :class="{ selected: isNavigationActive(path, item.path) }" :aria-current="isNavigationActive(path, item.path) ? 'page' : undefined" :aria-label="collapsed ? item.label : undefined" :title="collapsed ? item.label : undefined" @click="navigate($event); $emit('navigate')">
          <ShellIcon :name="item.icon" /><span :class="{ 'visually-hidden': collapsed }">{{ item.label }}</span>
        </a>
      </RouterLink>
    </section>
  </nav>
</template>
