<template>
  <div class="markdown-body" v-html="html"></div>
</template>

<script setup>
import { computed } from 'vue';
import { renderMarkdown } from '../utils/markdown.js';

const props = defineProps({
  content: { type: String, default: '' }
});

const html = computed(() => renderMarkdown(props.content));
</script>

<style scoped>
/* v-html 内容不带 scoped 标识，需要用 :deep 才能生效 */
:deep(h1) {
  font-size: 24px;
  margin: 16px 0 8px;
  color: #333;
}

:deep(h2) {
  font-size: 20px;
  margin: 14px 0 7px;
  color: #444;
}

:deep(h3) {
  font-size: 18px;
  margin: 12px 0 6px;
  color: #555;
}

:deep(p) {
  margin: 8px 0;
  line-height: 1.6;
}

:deep(ul), :deep(ol) {
  margin: 8px 0;
  padding-left: 24px;
}

:deep(li) {
  margin: 4px 0;
}

:deep(pre) {
  background-color: #f5f5f5;
  padding: 12px;
  border-radius: 4px;
  overflow-x: auto;
  font-family: 'Courier New', monospace;
}

:deep(code) {
  background-color: #f5f5f5;
  padding: 2px 4px;
  border-radius: 3px;
  font-family: 'Courier New', monospace;
}

:deep(strong) {
  font-weight: bold;
}

:deep(em) {
  font-style: italic;
}

:deep(a) {
  color: #2196f3;
  text-decoration: none;
}

:deep(a:hover) {
  text-decoration: underline;
}
</style>
