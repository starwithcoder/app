<template>
  <div class="sidebar-wrapper">
    <div class="sidebar-content" :class="{ 'expanded': isSidebarExpanded }">
      <!-- 扁平化Logo和侧边栏展开/收起按钮 -->
      <div class="app-branding">
        <div class="app-logo-flat">
          <img src="/logo.svg" alt="电脑维修助手 Logo" width="40" height="40"/>
        </div>

        <!-- 标题 - 仅在展开状态显示 -->
        <div v-show="isSidebarExpanded" class="sidebar-text-content">
          <h1 class="app-title">电脑维修助手</h1>
        </div>
        <button
          class="toggle-sidebar-btn"
          @click="isSidebarExpanded = !isSidebarExpanded"
          :title="isSidebarExpanded ? '收起侧边栏' : '展开侧边栏'"
        >
          {{ isSidebarExpanded ? '‹' : '›' }}
        </button>
      </div>

      <!-- 新建会话按钮 -->
      <div class="session-button-container" v-show="isSidebarExpanded">
        <a href="/" class="new-chat-btn" @click.prevent="emit('create-session')">
          <span class="icon">
            <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" role="img" style="" width="20" height="20" viewBox="0 0 1024 1024" name="AddConversation" class="iconify new-icon" data-v-9f34fd85="">
              <path d="M475.136 561.152v89.74336c0 20.56192 16.50688 37.23264 36.864 37.23264s36.864-16.67072 36.864-37.23264v-89.7024h89.7024c20.60288 0 37.2736-16.54784 37.2736-36.864 0-20.39808-16.67072-36.864-37.2736-36.864H548.864V397.63968A37.0688 37.0688 0 0 0 512 360.448c-20.35712 0-36.864 16.67072-36.864 37.2736v89.7024H385.4336a37.0688 37.0688 0 0 0-37.2736 36.864c0 20.35712 16.67072 36.864 37.2736 36.864h89.7024z" fill="currentColor"></path>
              <path d="M512 118.784c-223.96928 0-405.504 181.57568-405.504 405.504 0 78.76608 22.44608 152.3712 61.35808 214.6304l-44.27776 105.6768a61.44 61.44 0 0 0 56.68864 85.1968H512c223.92832 0 405.504-181.53472 405.504-405.504 0-223.92832-181.57568-405.504-405.504-405.504z m-331.776 405.504a331.776 331.776 0 1 1 331.73504 331.776H198.656l52.59264-125.5424-11.59168-16.62976A330.09664 330.09664 0 0 1 180.224 524.288z" fill="currentColor"></path>
            </svg>
          </span>
          <span class="text">新建会话</span>
          <span class="shortcut">
            <span class="key">Ctrl</span>
            <span>+</span>
            <span class="key">K</span>
          </span>
        </a>
      </div>

      <!-- 导航栏 -->
      <div class="navigation-container" v-show="isSidebarExpanded">
        <div class="navigation-item" :class="{ 'selected': selectedNavItem === 'knowledge' }" @click="emit('select-nav', 'knowledge')">
          <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" fill="none" viewBox="0 0 24 24" class="nav-icon">
            <path fill="currentColor" fill-rule="evenodd" d="M3.75 7h16.563c0 .48-.007 1.933-.016 3.685.703.172 1.36.458 1.953.837V5.937a2 2 0 0 0-2-2h-6.227a3 3 0 0 1-1.015-.176L9.992 2.677A3 3 0 0 0 8.979 2.5h-5.23a2 2 0 0 0-1.999 2v14.548a2 2 0 0 0 2 2h10.31a6.5 6.5 0 0 1-1.312-2H3.75S3.742 8.5 3.75 7m15.002 14.5a.514.514 0 0 0 .512-.454c.24-1.433.451-2.169.907-2.625.454-.455 1.186-.666 2.611-.907a.513.513 0 0 0-.002-1.026c-1.423-.241-2.155-.453-2.61-.908-.455-.457-.666-1.191-.906-2.622a.514.514 0 0 0-.512-.458.52.52 0 0 0-.515.456c-.24 1.432-.452 2.167-.907 2.624-.454.455-1.185.667-2.607.909a.514.514 0 0 0-.473.513.52.52 0 0 0 .47.512c1.425.24 2.157.447 2.61.9.455.454.666 1.19.907 2.634a.52.52 0 0 0 .515.452" clip-rule="evenodd"></path>
          </svg>
          <span class="nav-text">知识库查询</span>
        </div>
        <div class="navigation-item" :class="{ 'selected': selectedNavItem === 'service' }" @click="emit('select-nav', 'service')">
          <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" fill="none" viewBox="0 0 24 24" class="nav-icon">
            <path fill="currentColor" fill-rule="evenodd" d="M12 20.571a8.5 8.5 0 0 1 2.5-6.08c1.43-1.429 3.5-2.49 6.071-2.491-2.571.002-4.617-1.075-6.05-2.508S12 6 12 3.428C12 6 10.954 8.095 9.517 9.532 8.081 10.968 6 12 3.428 12a8.52 8.52 0 0 1 6.082 2.516c1.43 1.43 2.487 3.484 2.49 6.055m-9.853-7.314c3.485.588 5.053 1.331 6.163 2.44s1.847 2.667 2.435 6.198c.105.627.603 1.105 1.26 1.105.664 0 1.156-.479 1.25-1.11.588-3.502 1.329-5.085 2.441-6.2 1.111-1.114 2.677-1.845 6.16-2.433.638-.075 1.144-.586 1.144-1.253 0-.668-.5-1.188-1.147-1.254-3.481-.59-5.026-1.347-6.137-2.46-1.112-1.115-1.872-2.674-2.46-6.171C13.16 1.482 12.671 1 12.003 1c-.66 0-1.155.481-1.259 1.114-.588 3.5-1.323 5.087-2.435 6.203C7.2 9.43 5.632 10.159 2.156 10.75 1.503 10.816 1 11.333 1 12.004c0 .68.52 1.17 1.147 1.253" clip-rule="evenodd"></path>
          </svg>
          <span class="nav-text">服务站查询</span>
        </div>
        <div class="navigation-item" :class="{ 'selected': selectedNavItem === 'network' }" @click="emit('select-nav', 'network')">
          <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" fill="none" viewBox="0 0 24 24" class="nav-icon">
            <path fill="currentColor" fill-rule="evenodd" d="M11 4a7 7 0 1 0 6.993 7.328c-.039-.53-.586-.93-1.131-.891a5.5 5.5 0 1 1-6.203-6.203.75.75 0 0 0-1.317-.63C4.617 5.458 2.75 8.425 2.75 12c0 4.418 3.582 8 8 8s8-3.582 8-8a7.961 7.961 0 0 0-1.996-5.38" clip-rule="evenodd"></path>
            <path stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="m21 21-3.5-3.5"></path>
          </svg>
          <span class="nav-text">联网搜索</span>
        </div>
      </div>

      <!-- 历史会话列表 - 仅在展开状态显示 -->
      <div v-show="isSidebarExpanded" class="sidebar-main">
        <div class="navigation-item" @click="emit('toggle-sessions')">
          <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 1024 1024" class="nav-icon">
            <path d="M512 81.066667c-233.301333 0-422.4 189.098667-422.4 422.4s189.098667 422.4 422.4 422.4 422.4-189.098667 422.4-422.4-189.098667-422.4-422.4-422.4z m-345.6 422.4a345.6 345.6 0 1 1 691.2 0 345.6 345.6 0 1 1-691.2 0z m379.733333-174.933334a38.4 38.4 0 0 0-76.8 0v187.733334a38.4 38.4 0 0 0 11.264 27.136l93.866667 93.866666a38.4 38.4 0 1 0 54.272-54.272L546.133333 500.352V328.533333z" fill="currentColor"></path>
          </svg>
          <span class="nav-text">历史会话</span>
        </div>
        <div class="sessions-list" v-show="showSessions">
          <div v-if="isLoadingSessions" class="loading-sessions">
            加载历史对话中...
          </div>
          <div v-else-if="sessions.length === 0" class="no-sessions">
            暂无历史对话
          </div>
          <div
            v-for="session in sessions"
            :key="session.session_id"
            :class="['session-item', { 'selected': session.session_id === selectedSessionId }]"
            @click="emit('select-session', session.session_id)"
          >
            <div class="session-info">
              <div style="display: flex; align-items: center; gap: 8px;">
                <img alt="豆包" src="//lf-flow-web-cdn.doubao.com/obj/flow-doubao/doubao/chat/static/image/default.light.2ea4b2b4.png" class="session-icon" style="width: 24px; height: 24px; border-radius: 4px; object-fit: cover;">
                <div class="session-preview">{{ session.title || session.memory[0]?.content || '空对话' }}</div>
              </div>
            </div>
            <!-- 删除按钮：@click.stop 防止冒泡触发"选中会话" -->
            <button
              class="session-delete-btn"
              title="删除会话"
              @click.stop="emit('delete-session', session.session_id)"
            >
              ✕
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue';

defineProps({
  sessions: { type: Array, default: () => [] },
  selectedSessionId: { type: String, default: '' },
  isLoadingSessions: { type: Boolean, default: false },
  showSessions: { type: Boolean, default: true },
  selectedNavItem: { type: String, default: '' }
});

const emit = defineEmits([
  'create-session',
  'select-session',
  'toggle-sessions',
  'select-nav',
  'delete-session'
]);

// 侧边栏展开/收起状态属于纯 UI 状态，收在组件内部
const isSidebarExpanded = ref(true);
</script>

<style scoped>
.app-branding {
  display: flex;
  align-items: center;
  gap: 15px;
}

.app-logo-flat {
  display: flex;
  align-items: center;
  justify-content: center;
}

.app-logo-flat svg {
  filter: drop-shadow(0 2px 3px rgba(0, 0, 0, 0.1));
}

.sessions-list {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
}

/* 会话删除按钮：默认隐藏，鼠标悬停时才显示 */
.session-item {
  position: relative;
}

.session-delete-btn {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  opacity: 0;
  border: none;
  background: transparent;
  color: #999;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 4px 6px;
  border-radius: 4px;
  transition: opacity 0.15s, background 0.15s, color 0.15s;
}

.session-item:hover .session-delete-btn {
  opacity: 1;
}

.session-delete-btn:hover {
  background: rgba(0, 0, 0, 0.06);
  color: #e74c3c;
}
</style>
