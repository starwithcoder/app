<template>
  <div class="login-container">
    <div class="login-form">
      <div class="app-logo-flat login-logo">
        <img src="/logo.svg" alt="电脑维修助手 Logo" width="60" height="60"/>
      </div>
      <h1 class="login-title">电脑维修助手</h1>
      <p class="login-subtitle">请登录以继续使用</p>
      <div class="login-input-group">
        <label for="username">用户名</label>
        <input
          id="username"
          :value="username"
          type="text"
          placeholder="请输入用户名"
          @input="emit('update:username', $event.target.value)"
          @keyup.enter="emit('login')"
        />
      </div>
      <div class="login-input-group">
        <label for="password">密码</label>
        <input
          id="password"
          :value="password"
          type="password"
          placeholder="请输入密码"
          @input="emit('update:password', $event.target.value)"
          @keyup.enter="emit('login')"
        />
      </div>
      <div v-if="loginError" class="login-error">
        {{ loginError }}
      </div>
      <button class="login-button btn-primary" @click="emit('login')">
        登录
      </button>
      <div class="login-hint">
        <p>测试用户：root1, root2, root3</p>
        <p>密码：123456</p>
      </div>
    </div>
  </div>
</template>

<script setup>
defineProps({
  username: { type: String, default: '' },
  password: { type: String, default: '' },
  loginError: { type: String, default: '' }
});

const emit = defineEmits(['update:username', 'update:password', 'login']);
</script>

<style scoped>
/* 登录页面样式 */
.login-container {
  display: flex;
  justify-content: center;
  align-items: center;
  flex: 1;
  padding: 20px;
}

.login-form {
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 40px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
  width: 100%;
  max-width: 400px;
  text-align: center;
}

.login-logo {
  margin: 0 auto 20px;
}

.app-logo-flat {
  display: flex;
  align-items: center;
  justify-content: center;
}

.app-logo-flat svg {
  filter: drop-shadow(0 2px 3px rgba(0, 0, 0, 0.1));
}

.login-title {
  margin: 0 0 6px;
  font-size: 28px;
  font-weight: 700;
  color: #333;
  background: linear-gradient(90deg, #4CAF50, #2196F3);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

.login-subtitle {
  margin: 0 0 26px;
  font-size: 14px;
  color: #6b7280;
}

.login-input-group {
  margin-bottom: 20px;
  text-align: left;
}

.login-input-group label {
  display: block;
  margin-bottom: 8px;
  font-weight: 500;
  color: #555;
}

.login-input-group input {
  width: 100%;
  padding: 12px 16px;
  border: 1px solid #ddd;
  border-radius: 6px;
  font-size: 16px;
  transition: border-color 0.3s ease;
  box-sizing: border-box;
}

.login-input-group input:focus {
  outline: none;
  border-color: var(--tech-primary);
  box-shadow: 0 0 0 3px var(--tech-accent-glow);
}

.login-error {
  color: #f44336;
  margin-bottom: 20px;
  padding: 10px;
  background-color: #ffebee;
  border-radius: 4px;
}

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

.login-hint {
  margin-top: 20px;
  padding: 15px;
  background-color: #f5f5f5;
  border-radius: 6px;
  font-size: 14px;
  color: #666;
}

.login-hint p {
  margin: 5px 0;
}

@media (max-width: 768px) {
  .login-form {
    padding: 30px 20px;
  }

  .login-title {
    font-size: 24px;
  }
}

@media (max-width: 480px) {
  .login-form {
    padding: 20px 15px;
  }

  .login-logo svg {
    width: 50px;
    height: 50px;
  }
}
</style>
