<template>
  <div class="app-container">
    <LoginView
      v-if="!isLoggedIn"
      v-model:username="username"
      v-model:password="password"
      :login-error="loginError"
      @login="handleLogin"
    />

    <!-- 主界面（登录后显示） -->
    <template v-else>
      <div class="main-content">
        <AppSidebar
          :sessions="sessions"
          :selected-session-id="selectedSessionId"
          :is-loading-sessions="isLoadingSessions"
          :show-sessions="showSessions"
          :selected-nav-item="selectedNavItem"
          @create-session="handleCreateSession"
          @select-session="sessionsApi.selectSession"
          @toggle-sessions="sessionsApi.toggleSessions"
          @select-nav="handleSelectNav"
          @delete-session="handleDeleteSession"
        />

        <div class="main-container">
          <ChatArea
            :messages="chatMessages"
            :is-processing="isProcessing"
            @toggle-thinking="toggleThinking"
          >
            <template #header>
              <UserMenu :current-user="currentUser" @logout="handleLogout" @login="goToLogin" />
            </template>
            <template #input>
              <ChatInput
                v-model="userInput"
                :is-processing="isProcessing"
                @send="handleSend"
                @cancel="handleCancel"
              />
            </template>
          </ChatArea>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, watch, onMounted, onUnmounted, nextTick } from 'vue';
import LoginView from './components/LoginView.vue';
import AppSidebar from './components/AppSidebar.vue';
import UserMenu from './components/UserMenu.vue';
import ChatArea from './components/ChatArea.vue';
import ChatInput from './components/ChatInput.vue';
import { useAuth } from './composables/useAuth.js';
import { useSessions } from './composables/useSessions.js';
import { useChatStream } from './composables/useChatStream.js';

const { isLoggedIn, username, password, currentUser, loginError, handleLogin, logout, goToLogin, getUserId } = useAuth();

const chat = useChatStream({
  getUserId,
  getSessionId: () => selectedSessionId.value,
  onUnauthorized: () => { isLoggedIn.value = false; },
  onFinished: () => sessionsApi.fetchSessions()
});

const sessionsApi = useSessions({
  currentUser,
  onSessionChange: async (session) => {
    chat.reset();
    if (!session?.session_id) return;

    // 只有**明确为 0 条**才跳过（新建的空会话）。
    // 注意用 === 0 而不是 !session.total_messages：
    // 后端若没返回该字段会是 undefined，!undefined 为真会导致误跳过、聊天区空白。
    if (session.total_messages === 0) return;

    // 列表接口不再返回正文（memory 恒为空），
    // 点开会话时用单独接口按需加载聊天记录
    const requestedId = session.session_id;
    const messages = await sessionsApi.fetchSessionMessages(requestedId);

    // 竞态防护：请求期间用户可能又切到别的会话，
    // 此时丢弃这次响应，否则旧会话的消息会覆盖当前会话
    if (selectedSessionId.value !== requestedId) return;

    if (messages.length > 0) {
      chat.loadFromMemory(messages);
    }
  },
  onLoaded: () => chat.scrollToBottom()
});

const { sessions, selectedSessionId, isLoadingSessions, showSessions } = sessionsApi;

const selectedNavItem = ref('');

const {
  chatMessages,
  userInput,
  isProcessing,
  scrollToBottom,
  toggleThinking,
  handleSend,
  handleCancel
} = chat;

const handleSelectNav = (key) => {
  selectedNavItem.value = key;
  // 选中导航项时清除历史会话的选中状态
  selectedSessionId.value = '';
};

const handleCreateSession = () => sessionsApi.createNewSession();

/**
 * 删除会话。
 * 后端是软删除：调用后会话立刻从索引移除（列表里消失），
 * 真实清理（补归档 → 导出 → 真删 → 清 Redis）由后台协程异步完成。
 */
const handleDeleteSession = async (sessionId) => {
  const ok = await sessionsApi.deleteSession(sessionId);
  if (!ok) return;

  // 重新拉列表，被删的会话已不在索引里
  await sessionsApi.fetchSessions();

  // 若删掉的正是当前会话，清空聊天区与选中状态
  if (selectedSessionId.value === sessionId) {
    chat.reset();
    selectedSessionId.value = '';
  }
};

const handleLogout = () => {
  logout();
  chat.reset();
  sessionsApi.reset();
};

// Ctrl+K 快捷键新建会话
const handleKeyDown = (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'k') {
    event.preventDefault();
    handleCreateSession();
  }
};

watch(isLoggedIn, (newVal) => {
  if (newVal && currentUser.value) {
    sessionsApi.fetchSessions();
  }
});

onMounted(() => {
  if (isLoggedIn.value && currentUser.value) {
    sessionsApi.fetchSessions();
    nextTick(scrollToBottom);
  }
  document.addEventListener('keydown', handleKeyDown);
});

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeyDown);
});
</script>

<style scoped>
.app-container {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 5px;
  padding-bottom: 10px; /* 减小下边距 */
  box-sizing: border-box;
  min-height: 100vh;
  overflow: hidden; /* 防止页面整体滚动 */
}

/* 主内容区域布局 */
.main-content {
  display: flex;
  flex: 1;
  gap: 20px;
  overflow: hidden;
}

@media (max-width: 768px) {
  .app-container {
    padding: 8px;
    gap: 8px;
  }
}

@media (max-width: 480px) {
  .app-container {
    padding: 10px;
    gap: 10px;
  }
}
</style>
