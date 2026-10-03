<script setup lang="ts">
import type { NavigationIcon } from '../navigation'
defineProps<{ name: NavigationIcon | 'menu' | 'collapse' | 'expand' | 'close' | 'chevron' }>()
const paths: Record<string, string> = {
  overview: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M16 3a4 4 0 0 1 0 8 M22 21v-2a4 4 0 0 0-3-3.87 M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  services: 'M4 4h16v16H4z M8 9h8 M8 13h8 M8 17h5',
  rules: 'M4 7h16 M4 17h16 M8 4v6 M16 14v6',
  servers: 'M3 3h18v7H3z M3 14h18v7H3z M7 6.5h.01 M7 17.5h.01 M11 6.5h6 M11 17.5h6',
  lines: 'M5 5h5v5H5z M14 14h5v5h-5z M10 7.5h7v6.5 M7.5 10v7H14',
  guide: 'M12 6c-3-2-6-2-10-1v15c4-1 7-1 10 1 3-2 6-2 10-1V5c-4-1-7-1-10 1Z M12 6v15',
  account: 'M20 21a8 8 0 0 0-16 0 M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  return: 'M9 5l-7 7 7 7 M2 12h19',
  menu: 'M4 6h16 M4 12h16 M4 18h16',
  collapse: 'M3 4h18v16H3z M9 4v16 M16 9l-3 3 3 3',
  expand: 'M3 4h18v16H3z M9 4v16 M13 9l3 3-3 3',
  close: 'M6 6l12 12 M6 18L18 6',
  chevron: 'M6 9l6 6 6-6',
}
</script>
<template><svg class="shell-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path :d="paths[name]" /></svg></template>
