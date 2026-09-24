<template>
  <div class="input-container">
    <div class="textarea-with-button">
      <textarea
        :value="modelValue"
        placeholder="请输入您的请求..."
        @input="emit('update:modelValue', $event.target.value)"
        @keyup.enter.exact="handleEnter"
        :disabled="isProcessing"
      ></textarea>
      <button
        class="send-button btn-primary"
        :class="{ 'cancel-button': isProcessing, 'disabled': !modelValue.trim() && !isProcessing }"
        :disabled="!modelValue.trim() && !isProcessing"
        @click="isProcessing ? emit('cancel') : emit('send')"
      >
        {{ isProcessing ? '■' : '发送' }}
      </button>
    </div>
  </div>
</template>

<script setup>
defineProps({
  modelValue: { type: String, default: '' },
  isProcessing: { type: Boolean, default: false }
});

const emit = defineEmits(['update:modelValue', 'send', 'cancel']);

const handleEnter = (event) => {
  // 阻止回车键的默认行为（插入换行）
  event.preventDefault();
  emit('send');
};
</script>

<style scoped>
.input-container {
  padding: 0;
  margin-top: auto;
}

.textarea-with-button {
  position: relative;
  display: inline-block;
  width: 100%;
  max-width: 50vw;
}

.textarea-with-button textarea {
  width: 100%;
  padding: 12px 48px 12px 12px;
  border: 1px solid #ccc;
  border-radius: 12px;
  resize: none;
  height: 100px;
  font-size: 16px;
  font-family: inherit;
}

.textarea-with-button .send-button {
  position: absolute;
  bottom: 12px;
  right: 12px;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  border: none;
  background-color: var(--tech-primary);
  color: white;
  font-size: 12px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.textarea-with-button textarea:focus {
  outline: none;
  border-color: var(--tech-primary);
  box-shadow: 0 0 0 2px var(--tech-accent-glow);
}

.textarea-with-button textarea:disabled {
  background-color: #f5f5f5;
  cursor: not-allowed;
}

.input-container button {
  padding: 12px 24px;
  background-color: var(--tech-primary);
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 16px;
  font-weight: 500;
  transition: background-color 0.3s ease;
}

.input-container button:hover {
  background-color: var(--tech-primary-strong);
}

.input-container button:active {
  background-color: var(--tech-primary-strong);
}

.input-container button.cancel-button {
  background-color: #f44336;
  width: 40px;
  padding: 12px;
  font-size: 16px;
  line-height: 1;
}

.input-container button.cancel-button:hover {
  background-color: #d32f2f;
}

@media (max-width: 768px) {
  .input-container textarea {
    height: 80px;
    font-size: 14px;
  }
}

@media (max-width: 480px) {
  .input-container {
    flex-direction: column;
  }

  .input-container button {
    align-self: flex-end;
    padding: 10px 20px;
  }
}
</style>
