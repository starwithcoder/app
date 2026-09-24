<template>
  <div class="user-avatar-container" ref="avatarContainerRef">
    <!-- 头像，点击时切换用户信息显示状态 -->
    <img
      src="https://p3-flow-imagex-sign.byteimg.com/user-avatar/assets/e7b19241fb224cea967dfaea35448102_1080_1080.png~tplv-a9rns2rl98-icon-tiny.png?rcl=202511070904143F9B891FA2E40D7123F0&rk3s=8e244e95&rrcfp=76e58463&x-expires=1765155855&x-signature=nqQBx1W9ABfrm%2FRKkEYZUzsYjE0%3D"
      class="user-avatar"
      alt="用户头像"
      @click="toggleUserInfo"
      tabindex="0"
    />

    <!-- 用户信息下拉框，点击头像时显示/隐藏 -->
    <div class="user-info-dropdown" v-show="showUserInfo">
      <template v-if="currentUser">
        <span class="user-name">{{ currentUser }}</span>
        <button data-testid="setup_logout" class="btn-tertiary" style="width: 100%; justify-content: flex-start;" @click="emit('logout')">
          <span role="img" class="semi-icon semi-icon-default text-16">
            <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" fill="none" viewBox="0 0 24 24"><path fill="currentColor" fill-rule="evenodd" d="M14 3H4.5v18H14v-5h2v5a2 2 0 0 1-2 2H4.5a2 2 0 0 1-2-2V3a2 2 0 0 1 2-2H14a2 2 0 0 1 2 2v5h-2zm5.207 4.793a1 1 0 1 0-1.414 1.414L19.586 11H10.5a1 1 0 1 0 0 2h9.086l-1.793 1.793a1 1 0 0 0 1.414 1.414l3.5-3.5a1 1 0 0 0 0-1.414z" clip-rule="evenodd"></path></svg>
          </span>
          退出登录
        </button>
      </template>
      <template v-else>
        <span class="user-name">当前未登录</span>
        <button class="login-button btn-primary" @click="emit('login')">请登录</button>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue';

defineProps({
  currentUser: { type: String, default: '' }
});

const emit = defineEmits(['logout', 'login']);

const showUserInfo = ref(false);
const avatarContainerRef = ref(null);

const toggleUserInfo = () => {
  showUserInfo.value = !showUserInfo.value;
};

// 点击外部收起下拉菜单
const handleClickOutside = (event) => {
  if (showUserInfo.value && avatarContainerRef.value && !avatarContainerRef.value.contains(event.target)) {
    showUserInfo.value = false;
  }
};

onMounted(() => document.addEventListener('click', handleClickOutside));
onUnmounted(() => document.removeEventListener('click', handleClickOutside));
</script>

<style scoped>
.login-button {
  width: 100%;
  padding: 14px;
  background-color: var(--tech-primary);
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 16px;
  font-weight: 600;
  cursor: pointer;
  transition: background-color 0.3s ease;
}

.login-button:hover {
  background-color: var(--tech-primary-strong);
}
</style>
