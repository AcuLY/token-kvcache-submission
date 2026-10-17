<template>
  <span class="brand-mark" aria-hidden="true">
    <svg viewBox="0 0 32 32" fill="none">
      <path d="M16 2.8 28 9.5 16 16.2 4 9.5 16 2.8Z" stroke="currentColor" stroke-width="2.4" stroke-linejoin="round" />
      <path d="m4 15.1 12 6.7 12-6.7" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" />
      <path d="m4 20.7 12 6.7 12-6.7" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" />
      <path d="m12.5 9.4 3.5-2 3.5 2-3.5 2-3.5-2Z" fill="currentColor" />
    </svg>
  </span>
</template>

<style scoped>
.brand-mark {
  display: inline-grid;
  width: 31px;
  height: 31px;
  color: var(--color-primary);
  place-items: center;
}

svg {
  display: block;
  width: 100%;
  height: 100%;
}
</style>
