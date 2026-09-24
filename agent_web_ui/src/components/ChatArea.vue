<template>
  <div class="result-container" :class="{ 'processing': isProcessing }">
    <!-- 顶部区域，包含用户信息 -->
    <div class="top-user-section">
      <slot name="header" />
    </div>

    <!-- 统一的消息展示区域 -->
    <div class="chat-message-container">
      <div v-for="(msg, index) in messages" :key="index" :class="['message-wrapper', msg.type]">
        <!-- 消息头/角色标识 -->
        <div class="message-role-label" v-if="msg.type === 'THINKING'" @click="emit('toggle-thinking', index)">
          <div class="thinking-header">
            <span class="thinking-text">{{ isProcessing && index === messages.length - 1 ? '思考中...' : '思考过程' }}</span>
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              class="thinking-icon"
              :class="{ 'collapsed': msg.collapsed }"
            >
              <polyline points="6 9 12 15 18 9"></polyline>
            </svg>
          </div>
        </div>

        <!-- 消息内容 -->
        <div class="message-content" v-show="msg.type !== 'THINKING' || !msg.collapsed">
          <MarkdownRenderer :content="msg.content" />
        </div>
      </div>
    </div>

    <!-- 用户输入框 -->
    <slot name="input" />
  </div>
</template>

<script setup>
import MarkdownRenderer from './MarkdownRenderer.vue';

defineProps({
  messages: { type: Array, default: () => [] },
  isProcessing: { type: Boolean, default: false }
});

const emit = defineEmits(['toggle-thinking']);
</script>

<style scoped>
/* 思考过程头部样式 */
.thinking-header {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
  transition: color 0.2s;
}

.thinking-header:hover {
  color: var(--tech-text-main);
}

.thinking-text {
  font-weight: 500;
}

.thinking-icon {
  transition: transform 0.3s ease;
  opacity: 0.7;
}

.thinking-icon.collapsed {
  transform: rotate(-90deg);
}

.result-container {
  flex: 1;
  padding: 15px;
  display: flex;
  flex-direction: column;
  overflow: visible;
  height: auto;
  box-sizing: border-box;
  border-radius: 8px;
  border: 1px solid transparent; /* 默认无边框，处理中才高亮 */
}

/* 程序处理中时的渐变闪烁动画 */
.result-container.processing {
  animation: gradient-pulse 1.5s infinite ease-in-out;
}

@keyframes gradient-pulse {
  0% {
    border-color: transparent;
  }
  50% {
    border-color: var(--tech-primary); /* 品牌色边框 */
  }
  100% {
    border-color: transparent;
  }
}

.result-container h3 {
  margin: 0 0 10px 0;
  color: #333;
  font-size: 16px;
  font-weight: 600;
  display: flex;
  align-items: center;
}

@media (max-width: 768px) {
  .result-container {
    min-height: 180px;
  }
}

@media (max-width: 480px) {
  .result-container h3 {
    font-size: 16px;
  }
}
</style>
